"""Tests for `yandex-disk-rescue verify` over mirrors made by `download` from the fake Disk API into a real `tmp_path`
destination, with the real manifest in the XDG state directory."""

import errno
import fcntl
import hashlib
import os
import sqlite3
import stat
from contextlib import closing
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
)
from yandex_disk_rescue import localfs
from yandex_disk_rescue.cli import app
from yandex_disk_rescue.destination import ReadOnlyDestination
from yandex_disk_rescue.manifest import Mirror
from yandex_disk_rescue.paths import manifest_path
from yandex_disk_rescue.units import format_size

CORRUPT = 'disk:/Photos/2024/a.jpg'
EDITED = 'disk:/Photos/2024/b.jpg'
README = 'disk:/Docs/readme.txt'
TOTAL = format_size(sum(map(len, FILES.values())))
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


def _findings() -> list[tuple[str, str, str]]:
    """The (remote path, finding, reason) of every finding in the manifest, read with a connection of its own."""
    with closing(sqlite3.connect(f'{manifest_path().as_uri()}?mode=ro', uri=True)) as connection:
        return connection.execute(
            'SELECT e.path, v.finding, v.reason FROM verify_findings v JOIN entries e ON e.id = v.entry_id '
            'ORDER BY e.path'
        ).fetchall()


def _content_requests(fake, since: int = 0) -> list[str]:
    return [r.params['path'] for r in fake.requests[since:] if r.path in _CONTENT_PATHS]


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


def test_a_complete_mirror_is_intact_with_no_token_and_no_network(disk, dest, monkeypatch, closed_port_url):
    assert run_download(str(dest)).exit_code == 0
    monkeypatch.delenv('YANDEX_DISK_RESCUE_TOKEN')
    monkeypatch.setenv('YANDEX_DISK_RESCUE_API_URL', closed_port_url)
    earlier = len(disk.requests)

    result = _verify(str(dest))

    assert result.exit_code == 0, result.output
    assert result.stdout == (
        f'Verifying 6 files ({TOTAL}) in {dest}\n' + _intact_lines(dest, 6) + f'Manifest: {manifest_path()}\n'
    )
    assert result.stderr == ''
    assert len(disk.requests) == earlier
    assert _findings() == []


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
    )
    assert result.stderr == 'Error: 4 problems found; see the lists above.\n'
    assert _findings() == [
        (README, 'missing', 'Docs/readme.txt is missing'),
        (CYRILLIC, 'modified', linked_reason),
        (CORRUPT, 'corrupt', corrupt_reason),
        (EDITED, 'modified', edited_reason),
    ]
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
    assert result.stderr == 'Error: 1 problem found; see the list above.\n'
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
    assert stopped.stderr == (
        f'Error: Could not read {dest}: {os.strerror(code)}; verify stopped. Rerun it once the disk is back.\n'
    )
    assert _findings() == []

    rerun = _verify(str(dest))

    assert rerun.exit_code == 0, rerun.output
    assert _intact_lines(dest, 6) in rerun.stdout


def test_files_not_downloaded_and_quarantined_are_listed_without_being_read(disk, dest, monkeypatch):
    disk.corrupt(CORRUPT, offset=0)
    _, quarantine_reason = corrupted_bytes(FILES[CORRUPT], 0)
    disk.inject('GET', '/download', 503, where={'path': README})
    assert run_download('--download-passes', '1', str(dest)).exit_code == 1
    [run] = quarantine_runs(dest)
    disk.add_file('disk:/New/n.txt', b'new', modified=MODIFIED)
    disk.add_file(EDITED, b'edited on the Disk', modified=MODIFIED)
    assert runner.invoke(app, ['scan']).exit_code == 0
    read = localfs.read
    reads: list[int] = []

    def counting(fd, size, offset):
        reads.append(os.fstat(fd).st_ino)
        return read(fd, size, offset)

    monkeypatch.setattr(localfs, 'read', counting)

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
    assert result.stderr == 'Error: 4 problems found; see the lists above.\n'
    assert local_file(dest, EDITED).stat().st_ino not in reads
    assert (run / 'Photos' / '2024' / 'a.jpg').stat().st_ino not in reads
    assert _findings() == []


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


def test_download_fetches_again_exactly_what_verify_found_and_moves_the_bad_copies_aside(disk, dest):
    assert run_download(str(dest)).exit_code == 0
    corrupt = _flip_keeping_mtime(local_file(dest, CORRUPT), 10)
    edited = b'B' * len(FILES[EDITED])
    local_file(dest, EDITED).write_bytes(edited)
    local_file(dest, README).unlink()
    assert _verify(str(dest)).exit_code == 1
    earlier = len(disk.requests)

    repair = run_download('--no-scan', str(dest))

    assert repair.exit_code == 0, repair.output
    size = format_size(sum(len(FILES[path]) for path in (CORRUPT, EDITED, README)))
    assert f'Downloading 3 files ({size}) to {dest}\n' in repair.stdout
    assert f'3 downloaded ({size}), 3 skipped, 0 quarantined, 0 failed\n' in repair.stdout
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


def test_a_destination_that_is_not_a_recorded_mirror_folder_is_a_usage_error(disk, dest, tmp_path):
    missing = _verify(str(tmp_path / 'missing'))

    assert missing.exit_code == 2
    assert missing.stderr == f'Error: Destination {tmp_path / "missing"} is not an existing folder.\n'
    assert not (tmp_path / 'missing').exists()

    (tmp_path / 'file').write_bytes(b'')
    a_file = _verify(str(tmp_path / 'file'))

    assert a_file.exit_code == 2
    assert a_file.stderr == f'Error: Destination {tmp_path / "file"} is not an existing folder.\n'

    dest.mkdir()
    unrecorded = _verify(str(dest))

    assert unrecorded.exit_code == 2
    assert unrecorded.stderr == (
        f'Error: {dest} is not a mirror recorded in the manifest {manifest_path()}; nothing to verify.\n'
    )
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
