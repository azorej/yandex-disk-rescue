"""Tests for `yandex-disk-rescue verify` over mirrors made by `download` from the fake Disk API into a real `tmp_path`
destination, with the real manifest in the XDG state directory."""

import errno
import fcntl
import hashlib
import json
import os
import random
import re
import signal
import sqlite3
import stat
from contextlib import closing
from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest
from mirror_tree import (
    CYRILLIC,
    FILES,
    MODIFIED,
    SIZE_ONLY,
    corrupted_bytes,
    local_file,
    quarantine_runs,
    replaced_runs,
    run_download,
    runner,
    slow_reads,
    start_command,
    status_lines,
    stderr_lines,
    wait_until,
)
from yandex_disk_rescue import localfs, verifying
from yandex_disk_rescue.cli import app
from yandex_disk_rescue.commands import verify as verify_command
from yandex_disk_rescue.destination import ReadOnlyDestination
from yandex_disk_rescue.manifest import Mirror
from yandex_disk_rescue.paths import log_path, manifest_path
from yandex_disk_rescue.units import format_size

CORRUPT = 'disk:/Photos/2024/a.jpg'
EDITED = 'disk:/Photos/2024/b.jpg'
README = 'disk:/Docs/readme.txt'
TOTAL = format_size(sum(map(len, FILES.values())))
_RETRYING = 'Retrying 3 failed, quarantined, unfinished or broken files'
_CONTENT_PATHS = ('/v1/disk/resources/download', '/download', '/storage')


def _verify(*args: str):
    return runner.invoke(app, ['verify', *args])


def _md5(content: bytes) -> str:
    return hashlib.md5(content).hexdigest()


def _flip_keeping_mtime(local, offset: int) -> bytes:
    """Flip the byte at `offset` of `local` in place, keeping its size, mtime and inode; the new content."""
    before = local.stat()
    content = bytearray(local.read_bytes())
    content[offset] ^= 0xFF
    with local.open('r+b') as file:
        file.write(content)
    os.utime(local, ns=(before.st_atime_ns, before.st_mtime_ns))
    return bytes(content)


def _query(sql: str) -> list[tuple]:
    """The rows of `sql` over the manifest, read with a read-only connection of its own."""
    with closing(sqlite3.connect(f'{manifest_path().as_uri()}?mode=ro', uri=True)) as connection:
        return connection.execute(sql).fetchall()


def _findings() -> list[tuple[str, str, str]]:
    """The (remote path, finding, reason) of every finding in the manifest."""
    return _query(
        'SELECT e.path, v.finding, v.reason FROM verify_findings v JOIN entries e ON e.id = v.entry_id ORDER BY e.path'
    )


def _verify_pass() -> tuple[datetime, str | None] | None:
    """The start and the cursor of the verify pass in progress, if any."""
    rows = _query('SELECT started_at, checked_through FROM verify_passes')
    return None if not rows else (datetime.fromisoformat(rows[0][0]), rows[0][1])


def _started(moment: datetime) -> str:
    return f'Continuing the verification started {moment:%Y-%m-%d %H:%M} UTC:'


def _content_requests(fake, since: int = 0) -> list[str]:
    return [r.params['path'] for r in fake.requests[since:] if r.path in _CONTENT_PATHS]


def _count_reads(monkeypatch) -> list[int]:
    """Record the inode of every file read, once per file, in the order first read."""
    read = localfs.read
    reads: list[int] = []

    def counting(fd, size, offset):
        inode = os.fstat(fd).st_ino
        if inode not in reads:
            reads.append(inode)
        return read(fd, size, offset)

    monkeypatch.setattr(localfs, 'read', counting)
    return reads


def _interrupt_reads(monkeypatch, at: int) -> None:
    """Raise `KeyboardInterrupt`, as Ctrl+C does, at the first read of the `at`-th file read."""
    read = localfs.read
    seen: set[int] = set()

    def interrupting(fd, size, offset):
        seen.add(os.fstat(fd).st_ino)
        if len(seen) == at:
            raise KeyboardInterrupt
        return read(fd, size, offset)

    monkeypatch.setattr(localfs, 'read', interrupting)


def _fail_reads(monkeypatch, inode: int) -> list[int]:
    """Make every read of the file with `inode` fail with `EIO`; the list of the inodes of the reads refused."""
    read = localfs.read
    refused: list[int] = []

    def failing(fd, size, offset):
        if os.fstat(fd).st_ino == inode:
            refused.append(inode)
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return read(fd, size, offset)

    monkeypatch.setattr(localfs, 'read', failing)
    return refused


def _intact_lines(dest, intact: int, *, missing=0, modified=0, corrupt=0, quarantined=0, not_downloaded=0) -> str:
    return (
        f'Verified {dest} (disk:/): {intact} intact, {missing} missing, {modified} modified, {corrupt} corrupt, '
        f'{quarantined} quarantined, {not_downloaded} not downloaded\n'
        f'Checked by size only (no cloud checksum): 1\n  {SIZE_ONLY}\n'
    )


@pytest.mark.parametrize('partials', ['in-dest', 'separate'])
def test_a_complete_mirror_is_intact_with_no_token_and_no_network(
    disk, dest, monkeypatch, closed_port_url, partials, request
):
    partial_root = request.getfixturevalue('partial_root') if partials == 'separate' else None
    args = [] if partial_root is None else ['--partial-dir', str(partial_root)]
    assert run_download(*args, str(dest)).exit_code == 0
    monkeypatch.delenv('YANDEX_DISK_RESCUE_TOKEN')
    monkeypatch.setenv('YANDEX_DISK_RESCUE_API_URL', closed_port_url)
    earlier = len(disk.requests)
    separate = [] if partial_root is None else [partial_root, *partial_root.rglob('*')]
    mtimes = [folder.stat().st_mtime_ns for folder in separate]
    opened: list[str] = []
    os_open = os.open

    def logged_open(path, *args, **kwargs):
        opened.append(os.fspath(path))
        return os_open(path, *args, **kwargs)

    monkeypatch.setattr(os, 'open', logged_open)

    result = _verify(str(dest))

    assert result.exit_code == 0, result.output
    assert result.stdout == (
        f'Verifying 6 files ({TOTAL}) in {dest}\n'
        + _intact_lines(dest, 6)
        + f'Manifest: {manifest_path()}\n'
        + f'Log: {log_path()}\n'
    )
    assert stderr_lines(result.stderr) == []
    assert len(disk.requests) == earlier
    assert _findings() == []
    assert [folder.stat().st_mtime_ns for folder in separate] == mtimes
    if partial_root is not None:
        assert str(dest) in opened
        assert [path for path in opened if path.startswith(str(partial_root))] == []


def test_each_kind_of_damage_is_reported_and_recorded_and_a_link_is_never_followed(disk, dest, tmp_path):
    assert run_download(str(dest)).exit_code == 0
    corrupt = _flip_keeping_mtime(local_file(dest, CORRUPT), 10)
    edited = b'B' * len(FILES[EDITED])
    local_file(dest, EDITED).write_bytes(edited)
    local_file(dest, README).unlink()
    outside = tmp_path / 'outside.txt'
    outside.write_bytes(b'not part of the mirror')
    linked = local_file(dest, CYRILLIC)
    linked.unlink()
    linked.symlink_to(outside)
    before = outside.stat()

    result = _verify(str(dest))

    assert result.exit_code == 1
    corrupt_reason = f'MD5 mismatch (expected {_md5(FILES[CORRUPT])}, got {_md5(corrupt)})'
    edited_reason = f'MD5 mismatch (expected {_md5(FILES[EDITED])}, got {_md5(edited)})'
    linked_reason = f'{linked.relative_to(dest)} is no longer a regular file'
    assert result.stdout == (
        f'Verifying 6 files ({TOTAL}) in {dest}\n'
        + _intact_lines(dest, 2, missing=1, modified=2, corrupt=1)
        + f'Missing: 1\n  {README}: Docs/readme.txt is missing\n'
        f'Modified: 2\n  {CYRILLIC}: {linked_reason}\n  {EDITED}: {edited_reason}\n'
        f'Corrupt: 1\n  {CORRUPT}: {corrupt_reason}\n'
        f'Manifest: {manifest_path()}\n'
        f'Log: {log_path()}\n'
    )
    assert stderr_lines(result.stderr) == [
        f'{README} missing: Docs/readme.txt is missing',
        f'{CYRILLIC} modified: {linked_reason}',
        f'{CORRUPT} corrupt: {corrupt_reason}',
        f'{EDITED} modified: {edited_reason}',
        'Error: 4 problems found; see the lists above.',
    ]
    assert _findings() == [
        (README, 'missing', 'Docs/readme.txt is missing'),
        (CYRILLIC, 'modified', linked_reason),
        (CORRUPT, 'corrupt', corrupt_reason),
        (EDITED, 'modified', edited_reason),
    ]
    document = json.loads(_verify('--json', str(dest)).stdout)
    assert document['counts'] == {
        'intact': 2,
        'size_only': 1,
        'missing': 1,
        'modified': 2,
        'corrupt': 1,
        'quarantined': 0,
        'not_downloaded': 0,
        'problems': 4,
    }
    assert (document['missing'], document['modified'], document['corrupt']) == (
        [{'path': README, 'reason': 'Docs/readme.txt is missing', 'repair_quarantined': None}],
        [
            {'path': CYRILLIC, 'reason': linked_reason, 'repair_quarantined': None},
            {'path': EDITED, 'reason': edited_reason, 'repair_quarantined': None},
        ],
        [{'path': CORRUPT, 'reason': corrupt_reason, 'repair_quarantined': None}],
    )
    assert (document['exit_code'], document['error']) == (1, {'message': '4 problems found; see the lists above.'})
    assert linked.is_symlink()
    assert os.readlink(linked) == str(outside)
    assert outside.read_bytes() == b'not part of the mirror'
    assert outside.stat().st_mtime_ns == before.st_mtime_ns


def test_an_unreadable_file_is_corrupt_and_the_others_are_still_checked(disk, dest, monkeypatch):
    assert run_download(str(dest)).exit_code == 0
    _fail_reads(monkeypatch, local_file(dest, README).stat().st_ino)

    result = _verify(str(dest))

    assert result.exit_code == 1
    reason = f'could not read Docs/readme.txt: {os.strerror(errno.EIO)}'
    assert _intact_lines(dest, 5, corrupt=1) + f'Corrupt: 1\n  {README}: {reason}\n' in result.stdout
    assert stderr_lines(result.stderr) == [f'{README} corrupt: {reason}', 'Error: 1 problem found; see the list above.']
    assert _findings() == [(README, 'corrupt', reason)]


@pytest.mark.parametrize('code', [errno.EIO, errno.ENODEV], ids=errno.errorcode.get)
def test_a_destination_gone_mid_check_stops_it_with_nothing_recorded(disk, dest, monkeypatch, code):
    assert run_download(str(dest)).exit_code == 0
    read, seen = localfs.read, set()
    gone = False

    def failing(fd, size, offset):
        nonlocal gone
        seen.add(os.fstat(fd).st_ino)
        if len(seen) >= 3:
            gone = True
            raise OSError(code, os.strerror(code))
        return read(fd, size, offset)

    def reachable(self) -> bool:
        return not gone

    with monkeypatch.context() as patch:
        patch.setattr(localfs, 'read', failing)
        if code == errno.EIO:
            patch.setattr(ReadOnlyDestination, 'reachable', reachable)
        stopped = _verify(str(dest))

    assert stopped.exit_code == 1
    assert stopped.stdout == f'Verifying 6 files ({TOTAL}) in {dest}\n'
    assert stderr_lines(stopped.stderr) == [
        f'Error: Could not read {dest}: {os.strerror(code)}; verify stopped. Rerun it once the disk is back.'
    ]
    assert _findings() == []
    started, through = _verify_pass()
    assert through == CYRILLIC

    rerun = _verify(str(dest))

    assert rerun.exit_code == 0, rerun.output
    left = format_size(sum(len(content) for path, content in FILES.items() if path > CYRILLIC))
    assert rerun.stdout.startswith(f'{_started(started)} 4 files ({left}) left\n')
    assert _intact_lines(dest, 6) in rerun.stdout
    assert _verify_pass() is None


@pytest.mark.parametrize('code', [errno.EMFILE, errno.ENFILE, errno.ENOMEM], ids=errno.errorcode.get)
def test_a_process_out_of_descriptors_or_memory_stops_the_check_with_the_copy_not_recorded(
    disk, dest, monkeypatch, code
):
    assert run_download(str(dest)).exit_code == 0
    readme = local_file(dest, README)
    original = localfs.open_file

    def refusing(name, *args, **kwargs):
        if name == readme.name:
            raise OSError(code, os.strerror(code))
        return original(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(localfs, 'open_file', refusing)
        stopped = _verify(str(dest))

    assert stopped.exit_code == 1
    assert stderr_lines(stopped.stderr) == [f'Error: Could not read {readme}: {os.strerror(code)}.']
    assert 'Verified' not in stopped.stdout
    assert _findings() == []
    _, through = _verify_pass()
    assert through is None or through < README

    rerun = _verify(str(dest))

    assert rerun.exit_code == 0, rerun.output
    assert _intact_lines(dest, 6) in rerun.stdout
    assert _verify_pass() is None


def test_files_not_downloaded_and_quarantined_are_listed_without_being_read(disk, dest, monkeypatch):
    disk.corrupt(CORRUPT, offset=0)
    _, quarantine_reason = corrupted_bytes(FILES[CORRUPT], 0)
    disk.inject('GET', '/download', 503, where={'path': README})
    assert run_download('--download-passes', '1', str(dest)).exit_code == 1
    [run] = quarantine_runs(dest)
    disk.add_file('disk:/New/n.txt', b'new', modified=MODIFIED)
    disk.add_file(EDITED, b'edited on the Disk', modified=MODIFIED)
    assert runner.invoke(app, ['scan']).exit_code == 0
    reads = _count_reads(monkeypatch)

    result = _verify(str(dest))

    assert result.exit_code == 1
    copy = PurePosixPath('.yandex-disk-rescue', 'quarantine', run.name, 'Photos', '2024', 'a.jpg')
    assert result.stdout.startswith(f'Verifying 3 files ({format_size(len(FILES[CYRILLIC]) + 6)}) in {dest}\n')
    assert (
        _intact_lines(dest, 3, quarantined=1, not_downloaded=3)
        + f'Quarantined: 1\n  {CORRUPT}: {quarantine_reason}; kept at {copy}\n'
        'Not downloaded: 3\n'
        f'  {README}: the download host 127.0.0.1 returned HTTP 503\n'
        '  disk:/New/n.txt: not downloaded yet\n'
        f'  {EDITED}: changed on the Disk since it was downloaded\n'
    ) in result.stdout
    assert stderr_lines(result.stderr) == ['Error: 4 problems found; see the lists above.']
    assert local_file(dest, EDITED).stat().st_ino not in reads
    assert (run / 'Photos' / '2024' / 'a.jpg').stat().st_ino not in reads
    assert _findings() == []


def _paths_touched(monkeypatch) -> list[str]:
    """Record the full path of every name opened, looked up or listed through a folder descriptor."""
    touched: list[str] = []
    calls = {'open': os.open, 'stat': os.stat, 'scandir': os.scandir}

    def where(name, dir_fd) -> str:
        if isinstance(name, int):
            return os.readlink(f'/proc/self/fd/{name}')
        base = os.getcwd() if dir_fd is None else os.readlink(f'/proc/self/fd/{dir_fd}')
        return os.path.join(base, name)

    def recording(call):
        def recorded(path='.', *args, dir_fd=None, **kwargs):
            touched.append(where(path, dir_fd))
            return calls[call](path, *args, dir_fd=dir_fd, **kwargs) if call != 'scandir' else calls[call](path)

        return recorded

    for call in calls:
        monkeypatch.setattr(os, call, recording(call))
    return touched


def test_verify_names_a_saved_preview_and_never_reads_the_previews_folder(disk, dest, monkeypatch):
    napkin = 'disk:/Dead/napkin.jpg'
    disk.add_file(napkin, b'napkin', modified=MODIFIED, preview=b'\xff\xd8 reduced')
    disk.inject('GET', '/download', 503, where={'path': napkin})
    assert run_download('--download-passes', '1', str(dest)).exit_code == 1
    preview = PurePosixPath('.yandex-disk-rescue', 'previews', 'Dead', 'napkin.jpg.preview.jpg')
    reason = 'the download host 127.0.0.1 returned HTTP 503'
    listed = f'Not downloaded: 1\n  {napkin}: {reason}; a preview is saved at {preview}\n'

    result = _verify(str(dest))

    assert result.exit_code == 1
    assert _intact_lines(dest, 6, not_downloaded=1) + listed in result.stdout
    as_json = _verify('--json', str(dest))
    assert json.loads(as_json.stdout)['not_downloaded'] == [{'path': napkin, 'reason': reason, 'preview': str(preview)}]
    (dest / preview).chmod(0)
    (dest / preview).parent.parent.joinpath('extra.jpg').write_bytes(b'stray')
    touched = _paths_touched(monkeypatch)

    again = _verify(str(dest))

    assert (again.exit_code, again.stdout) == (result.exit_code, result.stdout)
    assert 'extra.jpg' not in again.output
    assert any(path.startswith(f'{dest}/') for path in touched)
    assert [path for path in touched if '/previews' in path] == []


def test_a_file_with_a_finding_whose_repair_was_quarantined_is_listed_under_its_finding_with_the_repair(disk, dest):
    assert run_download(str(dest)).exit_code == 0
    local = _flip_keeping_mtime(local_file(dest, CORRUPT), 10)
    assert _verify(str(dest)).exit_code == 1
    finding = _md5_mismatch(FILES[CORRUPT], local)
    assert _findings() == [(CORRUPT, 'corrupt', finding)]
    disk.corrupt(CORRUPT, offset=0)
    _, quarantine_reason = corrupted_bytes(FILES[CORRUPT], 0)

    repair = run_download('--retry-failed', str(dest))

    assert repair.exit_code == 1, repair.output
    assert '0 downloaded (0 B), 0 skipped, 1 quarantined, 0 failed\n' in repair.stdout
    assert local_file(dest, CORRUPT).read_bytes() == local
    assert _findings() == [(CORRUPT, 'corrupt', finding)]

    [run] = quarantine_runs(dest)
    copy = PurePosixPath('.yandex-disk-rescue', 'quarantine', run.name, 'Photos', '2024', 'a.jpg')

    again = _verify(str(dest))

    assert again.exit_code == 1, again.output
    assert (
        _intact_lines(dest, 5, corrupt=1)
        + f'Corrupt: 1\n  {CORRUPT}: {finding}; repair quarantined: {quarantine_reason}; kept at {copy}\n'
        + f'Manifest: {manifest_path()}\n'
        + f'Log: {log_path()}\n'
    ) in again.stdout
    assert 'Quarantined:' not in again.stdout
    assert stderr_lines(again.stderr) == ['Error: 1 problem found; see the list above.']

    as_json = _verify('--json', str(dest))

    assert as_json.exit_code == 1
    document = json.loads(as_json.stdout)
    assert document['corrupt'] == [
        {'path': CORRUPT, 'reason': finding, 'repair_quarantined': {'reason': quarantine_reason, 'copy': str(copy)}}
    ]
    assert (document['quarantined'], document['counts']['problems']) == ([], 1)
    assert (document['exit_code'], document['error']) == (1, {'message': '1 problem found; see the list above.'})
    assert as_json.stderr.splitlines()[-1] == 'Error: 1 problem found; see the list above.'


def _md5_mismatch(expected: bytes, got: bytes) -> str:
    return f'MD5 mismatch (expected {_md5(expected)}, got {_md5(got)})'


def _snapshot(root) -> dict[str, tuple]:
    """Every path below `root` and `root` itself, with its kind, size, mode, inode, mtime, ctime and content (a link's
    target), never following a link; not the access time, which reads may change."""
    found = {}
    for folder, dirs, files in os.walk(root):
        for name in ['', *dirs, *files]:
            path = os.path.join(folder, name) if name else folder
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                content = os.readlink(path)
            elif stat.S_ISREG(info.st_mode):
                with open(path, 'rb') as file:
                    content = file.read()
            else:
                content = None
            fields = (info.st_size, info.st_mode, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, content)
            found[os.path.relpath(path, root)] = fields
    return found


def test_verify_writes_nothing_in_the_destination_and_never_reports_local_only_content(disk, dest):
    local_only = {
        '.Spotlight-V100/store.db': b'spotlight',
        '.DS_Store': b'finder',
        'notes.txt': b'my notes',
        'Photos/local-only.jpg': b'my photo',
        'Загрузки/файл.pdf': b'download',
    }
    for relative, content in local_only.items():
        (dest / relative).parent.mkdir(parents=True, exist_ok=True)
        (dest / relative).write_bytes(content)
    (dest / 'Загрузки' / 'link').symlink_to(dest / 'notes.txt')
    assert run_download(str(dest)).exit_code == 0
    _flip_keeping_mtime(local_file(dest, CORRUPT), 0)
    local_file(dest, README).unlink()
    before = _snapshot(dest)

    result = _verify(str(dest))

    assert result.exit_code == 1
    assert '2 problems found' in result.stderr
    assert _snapshot(dest) == before
    for name in ('.Spotlight-V100', '.DS_Store', 'notes.txt', 'local-only.jpg', 'Загрузки'):
        assert name not in result.stdout


@pytest.mark.parametrize(
    ('option', 'start', 'skipped'),
    [('--no-scan', 'Downloading 3 files ({size}) to', 3), ('--retry-failed', _RETRYING + ' ({size}) in', 0)],
)
def test_download_fetches_again_exactly_what_verify_found_and_moves_the_bad_copies_aside(
    disk, dest, option, start, skipped
):
    assert run_download(str(dest)).exit_code == 0
    corrupt = _flip_keeping_mtime(local_file(dest, CORRUPT), 10)
    edited = b'B' * len(FILES[EDITED])
    local_file(dest, EDITED).write_bytes(edited)
    local_file(dest, README).unlink()
    assert _verify(str(dest)).exit_code == 1
    earlier = len(disk.requests)

    repair = run_download(option, str(dest))

    assert repair.exit_code == 0, repair.output
    size = format_size(sum(len(FILES[path]) for path in (CORRUPT, EDITED, README)))
    assert f'{start.format(size=size)} {dest}\n' in repair.stdout
    assert f'3 downloaded ({size}), {skipped} skipped, 0 quarantined, 0 failed\n' in repair.stdout
    assert sorted(set(_content_requests(disk, earlier))) == sorted([CORRUPT, EDITED, README])
    for path in (CORRUPT, EDITED, README):
        assert local_file(dest, path).read_bytes() == FILES[path]
    [run] = replaced_runs(dest)
    assert (run / 'Photos' / '2024' / 'a.jpg').read_bytes() == corrupt
    assert (run / 'Photos' / '2024' / 'b.jpg').read_bytes() == edited
    assert not (run / 'Docs').exists()
    assert _findings() == []

    again = _verify(str(dest))

    assert again.exit_code == 0, again.output
    assert _intact_lines(dest, 6) in again.stdout


@pytest.mark.parametrize('repair', ['succeeds', 'fails', 'interrupted'])
def test_download_never_reads_a_copy_verify_found_unreadable_and_a_failed_repair_keeps_the_finding(
    disk, dest, monkeypatch, repair
):
    assert run_download(str(dest)).exit_code == 0
    refused = _fail_reads(monkeypatch, local_file(dest, README).stat().st_ino)
    assert _verify(str(dest)).exit_code == 1
    finding = [(README, 'corrupt', f'could not read Docs/readme.txt: {os.strerror(errno.EIO)}')]
    assert _findings() == finding
    refused.clear()

    if repair == 'fails':
        disk.inject('GET', '/download', 503, where={'path': README})
        failed = run_download('--no-scan', '--download-passes', '1', str(dest))

        assert failed.exit_code == 1
        assert f'Failed: 1\n  {README}: the download host 127.0.0.1 returned HTTP 503\n' in failed.stdout
    elif repair == 'interrupted':

        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt

        with monkeypatch.context() as patch:
            patch.setattr(Mirror, 'record_partial', interrupted)
            failed = run_download('--no-scan', str(dest))

        assert failed.exit_code == 130
    if repair != 'succeeds':
        assert _findings() == finding
        assert refused == []

    repaired = run_download('--no-scan', str(dest))

    assert repaired.exit_code == 0, repaired.output
    assert '1 downloaded (7 B), 5 skipped, 0 quarantined, 0 failed\n' in repaired.stdout
    assert refused == []
    assert local_file(dest, README).read_bytes() == FILES[README]
    [run] = replaced_runs(dest)
    assert (run / 'Docs' / 'readme.txt').exists()
    assert _findings() == []


def test_a_destination_that_is_not_a_recorded_mirror_folder_is_a_usage_error_and_creates_no_manifest(
    disk, dest, tmp_path
):
    missing = _verify(str(tmp_path / 'missing'))

    assert missing.exit_code == 2
    assert missing.stderr == f'Error: Destination {tmp_path / "missing"} is not an existing folder.\n'
    assert not (tmp_path / 'missing').exists()

    (tmp_path / 'file').write_bytes(b'')
    a_file = _verify(str(tmp_path / 'file'))

    assert a_file.exit_code == 2
    assert a_file.stderr == f'Error: Destination {tmp_path / "file"} is not an existing folder.\n'

    dest.mkdir()
    no_manifest = _verify(str(dest))
    created = manifest_path().exists()
    assert runner.invoke(app, ['scan']).exit_code == 0
    unrecorded = _verify(str(dest))

    not_a_mirror = f'Error: {dest} is not a mirror recorded in the manifest {manifest_path()}; nothing to verify.\n'
    assert (no_manifest.exit_code, no_manifest.stderr) == (2, not_a_mirror)
    assert not created
    assert (unrecorded.exit_code, unrecorded.stderr) == (2, not_a_mirror)
    assert list(dest.iterdir()) == []


def test_verify_holds_the_destination_lock_and_waits_for_no_other_run(disk, dest, monkeypatch):
    assert run_download(str(dest)).exit_code == 0
    lock = dest / '.yandex-disk-rescue' / 'lock'
    read = localfs.read
    probes: list[bool] = []

    def probing(fd, size, offset):
        probe = os.open(lock, os.O_RDONLY)
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            probes.append(True)
        else:
            probes.append(False)
        finally:
            os.close(probe)
        return read(fd, size, offset)

    with monkeypatch.context() as patch:
        patch.setattr(localfs, 'read', probing)
        assert _verify(str(dest)).exit_code == 0

    assert probes
    assert all(probes)

    holder = os.open(lock, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        busy = _verify(str(dest))
    finally:
        os.close(holder)

    assert busy.exit_code == 1
    assert (
        busy.stderr == f'Error: Another yandex-disk-rescue run is using {dest}; wait for it to finish and try again.\n'
    )


MANY = [f'disk:/Many/f{n:02}.bin' for n in range(40)]
EARLY, LATE = MANY[1], MANY[38]
MANY_SIZE = format_size(1024 * len(MANY))


@pytest.fixture
def many(disk, dest):
    """A mirror of `disk:/Many` in `dest`: the 1 KiB files `MANY`, with a byte of `EARLY` and of `LATE` flipped since
    they were verified."""
    rng = random.Random(40)
    for path in MANY:
        disk.add_file(path, rng.randbytes(1024), modified=MODIFIED)
    assert run_download('disk:/Many', str(dest)).exit_code == 0
    for path in (EARLY, LATE):
        _flip_keeping_mtime(_many_file(dest, path), 0)
    return disk


def _many_file(dest, path: str):
    return dest / path.removeprefix('disk:/Many/')


def _after(through: str) -> list[str]:
    return [path for path in MANY if path > through]


STOPS = pytest.mark.parametrize(
    ('stop', 'exit_code'), [(signal.SIGINT, 130), (signal.SIGTERM, 143)], ids=['sigint', 'sigterm']
)


@STOPS
def test_ctrl_c_or_sigterm_keeps_the_pass_and_the_rerun_continues_it_reporting_what_an_uninterrupted_verify_does(
    many, dest, tmp_path, monkeypatch, stop, exit_code
):
    gate = slow_reads(tmp_path, monkeypatch)
    child = start_command('verify', '--progress-interval', '1', str(dest))
    try:
        # Each file takes two reads, so 20 files take 2 s at least, the time of one status line and more.
        wait_until(lambda: len(list(gate.iterdir())) >= 20, child, 'read 20 files')
        child.send_signal(stop)
        stdout, stderr = child.communicate(timeout=60)
    finally:
        child.kill()
        child.communicate()
    monkeypatch.delenv('PYTHONPATH')

    assert child.returncode == exit_code, stderr
    assert stdout == f'Verifying 40 files ({MANY_SIZE}) in {dest}\n'
    *notices, result_line = stderr_lines(stderr)
    assert [notice.partition(' ')[0] for notice in notices] == [EARLY]
    assert result_line == 'Interrupted; rerun verify to continue where it stopped.'
    *verifying, stopped_line = status_lines(stderr)
    assert verifying
    checked = [
        int(re.match(r'Verifying: (\d+) of 40 files, .*; 1 problem(?:$|; reading )', line)[1]) for line in verifying
    ]
    assert checked == sorted(checked)
    assert stopped_line.startswith('Verified: ')
    started, through = _verify_pass()
    assert EARLY <= through < LATE
    assert [path for path, *_ in _findings()] == [EARLY]

    resumed = _verify(str(dest))

    left = _after(through)
    assert resumed.stdout.startswith(f'{_started(started)} {len(left)} files ({format_size(1024 * len(left))}) left\n')
    [resumed_line] = status_lines(resumed.stderr)
    assert resumed_line.startswith(f'Verified: {len(left)} of {len(left)} files, {format_size(1024 * len(left))} in ')
    assert resumed_line.endswith('; 2 problems')
    assert _verify_pass() is None

    uninterrupted = _verify(str(dest))

    assert uninterrupted.stdout.startswith(f'Verifying 40 files ({MANY_SIZE}) in {dest}\n')
    assert resumed.exit_code == uninterrupted.exit_code == 1
    *found_now, result_line = stderr_lines(resumed.stderr)
    *found_in_full, full_result_line = stderr_lines(uninterrupted.stderr)
    assert result_line == full_result_line == 'Error: 2 problems found; see the list above.'
    assert [notice.partition(' ')[0] for notice in found_now] == [LATE]
    assert [notice.partition(' ')[0] for notice in found_in_full] == [EARLY, LATE]
    assert resumed.stdout.splitlines()[1:] == uninterrupted.stdout.splitlines()[1:]
    assert f'Verified {dest} (disk:/Many): 38 intact, 0 missing, 0 modified, 2 corrupt,' in resumed.stdout
    assert [path for path, *_ in _findings()] == [EARLY, LATE]


@STOPS
def test_json_of_a_continued_pass_says_so_and_counts_what_an_uninterrupted_pass_does(
    many, dest, tmp_path, monkeypatch, stop, exit_code
):
    gate = slow_reads(tmp_path, monkeypatch)
    child = start_command('verify', '--json', str(dest))
    try:
        wait_until(lambda: len(list(gate.iterdir())) >= 20, child, 'read 20 files')
        child.send_signal(stop)
        stopped, _ = child.communicate(timeout=60)
    finally:
        child.kill()
        child.communicate()
    monkeypatch.delenv('PYTHONPATH')
    started, through = _verify_pass()

    resumed = json.loads(_verify('--json', str(dest)).stdout)
    uninterrupted = json.loads(_verify('--json', str(dest)).stdout)

    assert child.returncode == exit_code
    interrupted = json.loads(stopped)
    assert (interrupted['exit_code'], interrupted['error']) == (exit_code, {'message': verify_command.INTERRUPTED})
    assert 'pass' not in interrupted
    left = _after(through)
    assert resumed['pass'] == {
        'started_at': f'{started.astimezone(UTC):%Y-%m-%dT%H:%M:%S}.{started.microsecond // 1000:03d}Z',
        'continued': True,
        'files_read': len(left),
        'size_read': 1024 * len(left),
    }
    assert uninterrupted['pass']['continued'] is False
    assert (uninterrupted['pass']['files_read'], uninterrupted['pass']['size_read']) == (40, 40 * 1024)
    assert resumed['counts'] == uninterrupted['counts']
    assert resumed['counts']['corrupt'] == 2
    assert resumed['corrupt'] == uninterrupted['corrupt']


def test_the_cursor_is_committed_every_cursor_seconds_and_never_past_the_file_in_flight(many, dest, monkeypatch):
    monkeypatch.setattr(verifying, 'CURSOR_SECONDS', 0)
    read = localfs.read
    cursors: dict[int, str | None] = {}

    def watching(fd, size, offset):
        cursors.setdefault(os.fstat(fd).st_ino, _verify_pass()[1])
        return read(fd, size, offset)

    with monkeypatch.context() as patch:
        patch.setattr(localfs, 'read', watching)
        result = _verify(str(dest))

    assert result.exit_code == 1
    inodes = [_many_file(dest, path).stat().st_ino for path in MANY]
    assert [cursors[inode] for inode in inodes] == [None, *MANY[:-1]]
    assert _verify_pass() is None


def test_restart_starts_a_new_pass_and_a_completed_pass_is_not_continued(many, dest, monkeypatch):
    with monkeypatch.context() as patch:
        _interrupt_reads(patch, at=6)
        assert _verify(str(dest)).exit_code == 130
    assert _verify_pass()[1] == MANY[4]

    with monkeypatch.context() as patch:
        reads = _count_reads(patch)
        restarted = _verify('--restart', str(dest))

    assert restarted.exit_code == 1
    assert restarted.stdout.startswith(f'Verifying 40 files ({MANY_SIZE}) in {dest}\n')
    assert len(reads) == 40
    assert _verify_pass() is None

    with monkeypatch.context() as patch:
        reads = _count_reads(patch)
        again = _verify(str(dest))

    assert again.stdout.startswith(f'Verifying 40 files ({MANY_SIZE}) in {dest}\n')
    assert len(reads) == 40
    assert again.stdout.splitlines()[1:] == restarted.stdout.splitlines()[1:]


def test_a_download_between_two_parts_of_a_pass_counts_what_it_repaired_and_the_rest_is_still_hashed(
    many, dest, monkeypatch
):
    with monkeypatch.context() as patch:
        _interrupt_reads(patch, at=6)
        assert _verify(str(dest)).exit_code == 130
    started, through = _verify_pass()
    assert [path for path, *_ in _findings()] == [EARLY]

    repair = run_download('--retry-failed', 'disk:/Many', str(dest))

    assert repair.exit_code == 0, repair.output
    assert '1 downloaded (1.0 KiB), 0 skipped, 0 quarantined, 0 failed\n' in repair.stdout
    with monkeypatch.context() as patch:
        reads = _count_reads(patch)
        resumed = _verify(str(dest))

    assert resumed.exit_code == 1
    assert resumed.stdout.startswith(f'{_started(started)} 35 files (35.0 KiB) left\n')
    assert reads == [_many_file(dest, path).stat().st_ino for path in _after(through)]
    assert f'Verified {dest} (disk:/Many): 39 intact, 0 missing, 0 modified, 1 corrupt,' in resumed.stdout
    assert [path for path, *_ in _findings()] == [LATE]
