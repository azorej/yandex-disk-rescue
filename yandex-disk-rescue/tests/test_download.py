"""Tests for `yandex-disk-rescue download` against the fake Disk API and download host, into a real `tmp_path`
destination, with the real manifest in the XDG state directory."""

import asyncio
import errno
import fcntl
import hashlib
import itertools
import os
import random
import re
import shutil
import signal
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

import pytest
from fake_disk_server import Delay, FakeResponse, Truncate
from mirror_tree import (
    FILES,
    MODIFIED,
    SIZE_ONLY,
    TOKEN,
    corrupted_bytes,
    local_file,
    quarantine_runs,
    replaced_runs,
    run_download,
    runner,
    start_command,
    wait_until,
)
from yandex_disk_rescue import api, localfs, mirroring
from yandex_disk_rescue.api import Resource
from yandex_disk_rescue.cli import app
from yandex_disk_rescue.destination import Destination, PartialFile
from yandex_disk_rescue.manifest import Manifest, Mirror, Segment
from yandex_disk_rescue.mirroring import MirrorReport, MirrorRun
from yandex_disk_rescue.paths import manifest_path
from yandex_disk_rescue.scanning import resolve_scan_root, scan_tree
from yandex_disk_rescue.units import format_size

BIG_PATH = 'disk:/Big/big.bin'
BIG = random.Random(3).randbytes(1024 * 1024)
QUARANTINE_HINT = (
    'Download them again with --retry-failed (add --scan-max-age 0 if they changed on the Disk since the scan).'
)
CHANGED = 'changed in the cloud since the scan; rerun with --scan-max-age 0 to fetch the new version'
# A stall timeout no ordinary local transfer comes near, and an injected silence well past it.
STALL_TIMEOUT = '3'
STALL = Delay(4.5)
MiB = 2**20
# A 4 MiB file above a 1 MiB segment threshold: four segments of 1 MiB, 16 chunks each.
SEGMENTED_PATH = 'disk:/Seg/large.bin'
SEGMENTED = random.Random(17).randbytes(4 * MiB)
SEGMENTED_ARGS = ('--download-segment-threshold-mib', '1')
SEGMENT_RANGES = [f'bytes={n * MiB}-{(n + 1) * MiB - 1}' for n in range(4)]
_CONTENT_PATHS = ('/v1/disk/resources/download', '/download', '/storage')
_STAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z')


def _transfers(fake, since: int = 0) -> list[str]:
    """The paths of the link requests and content requests after the first `since` requests."""
    return [r.path for r in fake.requests[since:] if r.path in _CONTENT_PATHS]


def _linked(fake) -> list[str]:
    """The remote paths download links were requested for."""
    return [r.params['path'] for r in fake.requests if r.path == '/v1/disk/resources/download']


def _listings(fake, since: int = 0) -> list[str]:
    """The folders listed after the first `since` requests (a listing is paged; a resource lookup is not)."""
    return [r.params['path'] for r in fake.requests[since:] if r.path == '/v1/disk/resources' and 'offset' in r.params]


def _set_scan_times(moment: datetime) -> None:
    """Set the completion time of every scan recorded in the real manifest file, outside any run."""
    with closing(sqlite3.connect(manifest_path())) as connection, connection:
        connection.execute('UPDATE scans SET completed_at = ?', (moment.isoformat(timespec='microseconds'),))


def _overwrite_keeping_mtime(local, content: bytes) -> None:
    before = local.stat()
    with local.open('r+b') as file:
        file.write(content)
    os.utime(local, ns=(before.st_atime_ns, before.st_mtime_ns))


def _mtime_ns(moment: datetime) -> int:
    return int(moment.timestamp()) * 10**9 + moment.microsecond * 1000


@pytest.fixture(autouse=True)
def no_pass_pause(monkeypatch):
    """Later passes start at once; the Ctrl+C test, which runs the command in a subprocess, keeps the real pause."""
    monkeypatch.setattr(mirroring, 'PASS_PAUSE_SECONDS', 0)


@pytest.fixture
def big(disk):
    """The `disk` tree plus a 1 MiB file, sent in 16 chunks."""
    disk.add_file(BIG_PATH, BIG, modified=MODIFIED)
    return disk


@pytest.fixture
def segmented(disk):
    """The `disk` tree plus `SEGMENTED` at `SEGMENTED_PATH`."""
    disk.add_file(SEGMENTED_PATH, SEGMENTED, modified=MODIFIED)
    return disk


def _partials(dest) -> list:
    folder = dest / '.yandex-disk-rescue' / 'partial'
    return sorted(folder.iterdir()) if folder.is_dir() else []


def _partial_size(dest) -> int:
    return sum(partial.stat().st_size for partial in _partials(dest))


def _fetches(fake, path: str, since: int = 0) -> list[str | None]:
    """The `Range` header (`None` when absent) of each content request for `path` after the first `since` requests."""
    return [r.headers.get('range') for r in fake.requests[since:] if r.path == '/download' and r.params['path'] == path]


def _tail(start: int) -> str:
    return f'bytes={start}-{len(BIG) - 1}'


def _leave_partial(fake, dest, *, after_bytes: int = 300_000):
    """Run a download of `disk:/Big` cut short with no retry and no later pass, which keeps its partial file."""
    fake.inject('GET', '/download', Truncate(after_bytes=after_bytes), where={'path': BIG_PATH})
    result = run_download('--download-retries', '0', '--download-passes', '1', 'disk:/Big', str(dest))
    assert result.exit_code == 1
    [partial] = _partials(dest)
    assert partial.stat().st_size == after_bytes
    assert _journal() == [(0, len(BIG), after_bytes, hashlib.sha256(BIG[:after_bytes]).hexdigest())]
    return partial


def _journal() -> list[tuple[int, int, int, str | None]]:
    """The segments of every download journal in the manifest (start, end, confirmed, digest), read with a connection
    of its own; none while the manifest or its journal table does not exist yet."""
    try:
        with closing(sqlite3.connect(f'{manifest_path().as_uri()}?mode=ro', uri=True)) as connection:
            return connection.execute(
                'SELECT start, end, confirmed, digest FROM download_segments ORDER BY entry_id, start'
            ).fetchall()
    except sqlite3.OperationalError:
        return []


def _failure_records() -> list[tuple[str, str]]:
    """The (remote path, reason) of every failure record in the manifest, read with a connection of its own."""
    with closing(sqlite3.connect(f'{manifest_path().as_uri()}?mode=ro', uri=True)) as connection:
        return connection.execute(
            'SELECT e.path, f.reason FROM failures f JOIN entries e ON e.id = f.entry_id ORDER BY e.path'
        ).fetchall()


def _confirmed() -> int:
    return sum(confirmed - start for start, _, confirmed, _ in _journal())


def _others_verified(dest, failed: str) -> bool:
    return all(local_file(dest, path).read_bytes() == content for path, content in FILES.items() if path != failed)


@pytest.mark.parametrize('redirect', [False, True], ids=['direct', 'redirected'])
def test_a_fresh_mirror_holds_every_file_with_its_bytes_and_cloud_mtime(disk, dest, redirect):
    disk.redirect_downloads = redirect

    result = run_download(str(dest))

    assert result.exit_code == 0, result.output
    for path, content in FILES.items():
        local = local_file(dest, path)
        assert local.read_bytes() == content
        assert local.stat().st_mtime_ns == _mtime_ns(MODIFIED)
    assert (dest / 'Empty').is_dir()
    assert list((dest / '.yandex-disk-rescue' / 'partial').iterdir()) == []
    total = format_size(sum(len(content) for content in FILES.values()))
    lines = result.stdout.splitlines()
    assert lines[lines.index(f'Downloading 6 files ({total}) to {dest}') :] == [
        f'Downloading 6 files ({total}) to {dest}',
        f'Mirrored disk:/ to {dest}: 6 downloaded ({total}), 0 skipped, 0 quarantined, 0 failed',
        'Verified by size only (no cloud checksum): 1',
        f'  {SIZE_ONLY}',
        f'Manifest: {manifest_path()}',
    ]
    assert lines[0].startswith('Scanned disk:/: 6 files, 4 folders')
    if redirect:
        assert len([r for r in disk.requests if r.path == '/storage']) == len(FILES)


def test_a_rerun_skips_verified_files_without_reading_them(disk, dest):
    assert run_download(str(dest)).exit_code == 0
    altered = {}
    for path, content in FILES.items():
        altered[path] = bytes(byte ^ 0xFF for byte in content)
        _overwrite_keeping_mtime(local_file(dest, path), altered[path])
    earlier = len(disk.requests)

    rerun = run_download(str(dest))

    assert rerun.exit_code == 0, rerun.output
    assert f'Downloading 0 files (0 B) to {dest}\n' in rerun.stdout
    assert f'Mirrored disk:/ to {dest}: 0 downloaded (0 B), 6 skipped, 0 quarantined, 0 failed\n' in rerun.stdout
    assert f'Verified by size only (no cloud checksum): 1\n  {SIZE_ONLY}\n' in rerun.stdout
    assert _transfers(disk, earlier) == []
    assert all(local_file(dest, path).read_bytes() == content for path, content in altered.items())
    assert not (dest / '.yandex-disk-rescue' / 'replaced').exists()


def _quarantined(run, path: str, reason: str) -> str:
    """The `Quarantined` list of a run holding the one copy of `path` in the quarantine folder `run`."""
    copy = PurePosixPath('.yandex-disk-rescue', 'quarantine', run.name, path.removeprefix('disk:/'))
    return f'Quarantined: 1\n  {path}: {reason}; kept at {copy}\n{QUARANTINE_HINT}\n'


@pytest.mark.parametrize('segmented', [False, True], ids=['one-stream', 'segmented'])
def test_a_corrupted_download_is_quarantined_and_fetched_again_only_with_retry_failed(disk, dest, segmented):
    path = 'disk:/Photos/2024/a.jpg'
    # Segmented, the corrupt byte lies in the third segment and only reading the whole file back can catch it.
    content, offset, args = (SEGMENTED, 2 * MiB + 10, SEGMENTED_ARGS) if segmented else (FILES[path], 10, ())
    disk.add_file(path, content, modified=MODIFIED)
    disk.corrupt(path, offset=offset)
    corrupted, reason = corrupted_bytes(content, offset)

    result = run_download(*args, str(dest))

    assert result.exit_code == 1
    others = sum(map(len, FILES.values())) - len(FILES[path])
    assert f'5 downloaded ({format_size(others)}), 0 skipped, 1 quarantined, 0 failed\n' in result.stdout
    [run] = quarantine_runs(dest)
    assert _STAMP.fullmatch(run.name)
    assert _quarantined(run, path, reason) in result.stdout
    assert 'Failed' not in result.stdout
    assert result.stderr == 'Error: 1 quarantined; see the list above.\n'
    copy = run / 'Photos' / '2024' / 'a.jpg'
    assert copy.read_bytes() == corrupted
    assert copy.stat().st_mtime_ns == _mtime_ns(MODIFIED)
    assert not local_file(dest, path).exists()
    assert _partials(dest) == []
    assert _journal() == []
    assert _failure_records() == []

    earlier = len(disk.requests)
    held = run_download(*args, '--scan-max-age', '0', str(dest))

    assert held.exit_code == 1
    assert f'Downloading 0 files (0 B) to {dest}\n' in held.stdout
    assert '0 downloaded (0 B), 5 skipped, 1 quarantined, 0 failed\n' in held.stdout
    assert _quarantined(run, path, reason) in held.stdout
    assert held.stderr == 'Error: 1 quarantined; see the list above.\n'
    assert _transfers(disk, earlier) == []

    disk.repair(path)
    earlier = len(disk.requests)
    retry = run_download(*args, '--retry-failed', str(dest))

    assert retry.exit_code == 0, retry.output
    assert (
        f'Retrying 1 failed, quarantined, unfinished or broken file ({format_size(len(content))}) in {dest}\n'
        in retry.stdout
    )
    assert f'1 downloaded ({format_size(len(content))}), 0 skipped, 0 quarantined, 0 failed\n' in retry.stdout
    assert local_file(dest, path).read_bytes() == content
    assert copy.read_bytes() == corrupted
    assert _transfers(disk, earlier) == ['/v1/disk/resources/download'] + ['/download'] * (4 if segmented else 1)
    assert sorted(_fetches(disk, path, earlier), key=str) == (SEGMENT_RANGES if segmented else [None])

    again = run_download(*args, str(dest))

    assert again.exit_code == 0, again.output
    assert '0 downloaded (0 B), 6 skipped, 0 quarantined, 0 failed\n' in again.stdout
    assert 'Quarantined' not in again.stdout
    assert quarantine_runs(dest) == [run]


def test_a_retry_failing_verification_again_adds_a_copy_under_its_own_run_folder(disk, dest):
    path = 'disk:/Docs/readme.txt'
    disk.corrupt(path, offset=0)
    corrupted, reason = corrupted_bytes(FILES[path], 0)
    assert run_download(str(dest)).exit_code == 1
    [first] = quarantine_runs(dest)
    before = (first / 'Docs' / 'readme.txt').stat()

    retry = run_download('--retry-failed', str(dest))

    assert retry.exit_code == 1
    assert '0 downloaded (0 B), 0 skipped, 1 quarantined, 0 failed\n' in retry.stdout
    [kept, second] = quarantine_runs(dest)
    assert kept == first
    assert _quarantined(second, path, reason) in retry.stdout
    assert (second / 'Docs' / 'readme.txt').read_bytes() == corrupted
    old = first / 'Docs' / 'readme.txt'
    assert old.read_bytes() == corrupted
    assert (old.stat().st_ino, old.stat().st_mtime_ns) == (before.st_ino, before.st_mtime_ns)

    plain = run_download(str(dest))

    assert plain.exit_code == 1
    assert _quarantined(second, path, reason) in plain.stdout


def test_a_quarantined_file_whose_retry_fails_is_still_listed_as_quarantined_with_that_failure(disk, dest):
    path = 'disk:/Docs/readme.txt'
    disk.corrupt(path, offset=0)
    _, reason = corrupted_bytes(FILES[path], 0)
    assert run_download(str(dest)).exit_code == 1
    [run] = quarantine_runs(dest)
    disk.repair(path)
    disk.inject('GET', '/download', 503, where={'path': path})

    retry = run_download('--retry-failed', '--download-passes', '1', str(dest))

    assert retry.exit_code == 1
    assert '0 downloaded (0 B), 0 skipped, 1 quarantined, 0 failed\n' in retry.stdout
    shown = f'Quarantined: 1\n  {path}: {reason}; last retry: the download host 127.0.0.1 returned HTTP 503'
    assert shown in retry.stdout
    assert 'Failed' not in retry.stdout
    assert [failed for failed, _ in _failure_records()] == [path]

    plain = run_download(str(dest))

    assert plain.exit_code == 1
    assert shown in plain.stdout
    assert f'; kept at .yandex-disk-rescue/quarantine/{run.name}/Docs/readme.txt\n' in plain.stdout
    assert 'Failed' not in plain.stdout


def test_a_new_disk_version_of_a_quarantined_file_is_not_held(disk, dest):
    path = 'disk:/Docs/readme.txt'
    disk.corrupt(path, offset=0)
    assert run_download(str(dest)).exit_code == 1
    [run] = quarantine_runs(dest)
    disk.repair(path)
    disk.add_file(path, b'read me, edited', modified=MODIFIED)

    rescanned = run_download('--scan-max-age', '0', str(dest))

    assert rescanned.exit_code == 0, rescanned.output
    assert '1 downloaded (15 B), 5 skipped, 0 quarantined, 0 failed\n' in rescanned.stdout
    assert local_file(dest, path).read_bytes() == b'read me, edited'
    assert (run / 'Docs' / 'readme.txt').exists()

    again = run_download(str(dest))

    assert again.exit_code == 0, again.output
    assert 'Quarantined' not in again.stdout


def test_a_run_killed_between_quarantining_a_copy_and_recording_it_downloads_the_file_again(disk, dest, monkeypatch):
    path = 'disk:/Docs/readme.txt'
    disk.corrupt(path, offset=0)
    corrupted, _ = corrupted_bytes(FILES[path], 0)

    def killed(*args, **kwargs):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(Mirror, 'record_quarantined', killed)
        stopped = run_download('disk:/Docs', str(dest))

    assert stopped.exit_code == 130
    assert stopped.stderr == 'Interrupted; rerun to continue the download.\n'
    [run] = quarantine_runs(dest)
    copy = run / 'readme.txt'
    assert copy.read_bytes() == corrupted

    disk.repair(path)
    rerun = run_download('disk:/Docs', str(dest))

    assert rerun.exit_code == 0, rerun.output
    assert (dest / 'readme.txt').read_bytes() == FILES[path]
    assert copy.read_bytes() == corrupted
    assert quarantine_runs(dest) == [run]


def test_a_quarantined_copy_whose_name_the_destination_refuses_gets_the_converted_name(disk, dest, monkeypatch):
    path = 'disk:/Odd/a?b.txt'
    disk.add_file(path, b'question')
    disk.corrupt(path, offset=0)
    _refuse_names(monkeypatch, lambda name: '?' in name)

    result = run_download('disk:/Odd', str(dest))

    assert result.exit_code == 1
    [run] = quarantine_runs(dest)
    converted = f'a_b~{_hex("a?b.txt")}.txt'
    assert (run / converted).read_bytes() == bytes([ord('q') ^ 0xFF]) + b'uestion'
    assert f'; kept at .yandex-disk-rescue/quarantine/{run.name}/{converted}\n' in result.stdout


def test_each_checksum_the_cloud_provides_is_checked_and_size_only_files_only_by_size(disk, dest):
    content = b'sha-checked'
    disk.add_file('disk:/Checks/sha.bin', content, md5=None, sha256='0' * 64)
    disk.add_file('disk:/Checks/size-only.bin', b'size only', md5=None, sha256=None)
    disk.corrupt('disk:/Checks/size-only.bin', offset=0)
    disk.add_file('disk:/Checks/short.bin', b'0123456789', md5=None, sha256=None)
    disk.inject('GET', '/download', Truncate(after_bytes=4), where={'path': 'disk:/Checks/short.bin'})

    result = run_download('--download-retries', '0', '--download-passes', '1', 'disk:/Checks', str(dest))

    assert result.exit_code == 1
    actual = hashlib.sha256(content).hexdigest()
    [run] = quarantine_runs(dest)
    assert (
        f'Mirrored disk:/Checks to {dest}: 1 downloaded (9 B), 0 skipped, 1 quarantined, 1 failed\n'
        'Passes: 1 of 1\n'
        'Verified by size only (no cloud checksum): 1\n'
        '  disk:/Checks/size-only.bin\n'
        'Quarantined: 1\n'
        f'  disk:/Checks/sha.bin: SHA-256 mismatch (expected {"0" * 64}, got {actual}); '
        f'kept at .yandex-disk-rescue/quarantine/{run.name}/sha.bin\n'
        f'{QUARANTINE_HINT}\n'
        'Failed: 1\n'
        '  disk:/Checks/short.bin: the download failed: '
    ) in result.stdout
    assert result.stderr == 'Error: 1 failed and 1 quarantined; see the lists above.\n'
    assert (dest / 'size-only.bin').read_bytes() == bytes([ord('s') ^ 0xFF]) + b'ize only'
    assert not (dest / 'sha.bin').exists()
    assert (run / 'sha.bin').read_bytes() == content
    assert not (dest / 'short.bin').exists()


def test_names_are_mapped_for_the_destination_and_kept_on_a_rerun(disk, dest):
    long_name = 'я' * 150 + '.txt'
    disk.add_file('disk:/Names/A.txt', b'upper')
    disk.add_file('disk:/Names/a.txt', b'lower')
    disk.add_file(f'disk:/Names/{long_name}', b'long')

    result = run_download('disk:/Names', str(dest))

    assert result.exit_code == 0, result.output
    lower = f'a~{hashlib.sha256(b"a.txt").hexdigest()[:8]}.txt'
    [shortened] = [p.name for p in dest.iterdir() if p.name.startswith('я')]
    assert shortened.endswith(f'~{hashlib.sha256(long_name.encode()).hexdigest()[:8]}.txt')
    assert len(shortened.encode()) <= 255
    assert (dest / 'A.txt').read_bytes() == b'upper'
    assert (dest / lower).read_bytes() == b'lower'
    assert (dest / shortened).read_bytes() == b'long'
    assert (
        f'Renamed for the destination: 2\n  disk:/Names/a.txt -> {lower}\n  disk:/Names/{long_name} -> {shortened}\n'
    ) in result.stdout

    earlier = len(disk.requests)
    rerun = run_download('disk:/Names', str(dest))

    assert rerun.exit_code == 0, rerun.output
    assert f'  disk:/Names/a.txt -> {lower}\n' in rerun.stdout
    assert _transfers(disk, earlier) == []
    assert sorted(p.name for p in dest.iterdir()) == sorted(['.yandex-disk-rescue', 'A.txt', lower, shortened])


def _hex(name: str, digits: int = 8) -> str:
    return hashlib.sha256(name.encode()).hexdigest()[:digits]


def _refuse_names(monkeypatch, refused: Callable[[str], bool], code: int = errno.EINVAL) -> list[str]:
    """Make the folder creation, the no-replace rename and a look-up relative to a folder fail with `code` for every
    name `refused` accepts, as exFAT does for a name holding `?`; return every name they were asked to create."""
    seen: list[str] = []
    make_dir, rename_noreplace, lstat = localfs.make_dir, localfs.rename_noreplace, localfs.lstat

    def refuse(name: str) -> None:
        if refused(name):
            raise OSError(code, os.strerror(code), name)

    def check(name: str) -> None:
        seen.append(name)
        refuse(name)

    def refusing_lstat(name, *, dir_fd):
        refuse(name)
        return lstat(name, dir_fd=dir_fd)

    def refusing_make_dir(name, *, dir_fd):
        check(name)
        make_dir(name, dir_fd=dir_fd)

    def refusing_rename(src, dst, *, src_dir_fd, dst_dir_fd):
        check(dst)
        rename_noreplace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(localfs, 'make_dir', refusing_make_dir)
    monkeypatch.setattr(localfs, 'rename_noreplace', refusing_rename)
    monkeypatch.setattr(localfs, 'lstat', refusing_lstat)
    return seen


@pytest.mark.parametrize('code', [errno.EINVAL, errno.EILSEQ, errno.ENAMETOOLONG], ids=errno.errorcode.get)
def test_names_the_destination_refuses_are_converted_recorded_and_kept_on_a_rerun(disk, dest, monkeypatch, code):
    lookalike = f'a_b~{_hex("a?b.txt")}.txt'
    disk.add_file('disk:/Odd/a?b.txt', b'question')
    disk.add_file(f'disk:/Odd/{lookalike}', b'lookalike')
    disk.add_file('disk:/Odd/Q?/x.txt', b'inside')
    seen = _refuse_names(monkeypatch, lambda name: '?' in name, code)

    result = run_download('disk:/Odd', str(dest))

    assert result.exit_code == 0, result.output
    converted_file = f'a_b~{_hex("a?b.txt", 16)}.txt'
    converted_folder = f'Q_~{_hex("Q?")}'
    assert (dest / converted_file).read_bytes() == b'question'
    assert (dest / lookalike).read_bytes() == b'lookalike'
    assert (dest / converted_folder / 'x.txt').read_bytes() == b'inside'
    assert sorted(p.name for p in dest.iterdir()) == sorted(
        ['.yandex-disk-rescue', converted_file, converted_folder, lookalike]
    )
    renamed = (
        f'Renamed for the destination: 2\n  disk:/Odd/Q? -> {converted_folder}\n'
        f'  disk:/Odd/a?b.txt -> {converted_file}\n'
    )
    assert renamed in result.stdout
    assert f'Mirrored disk:/Odd to {dest}: 3 downloaded' in result.stdout

    seen.clear()
    earlier = len(disk.requests)
    rerun = run_download('disk:/Odd', str(dest))

    assert rerun.exit_code == 0, rerun.output
    assert _transfers(disk, earlier) == []
    assert [name for name in seen if '?' in name] == []
    assert renamed in rerun.stdout
    assert f'Mirrored disk:/Odd to {dest}: 0 downloaded (0 B), 3 skipped, 0 quarantined, 0 failed\n' in rerun.stdout


def test_a_converted_name_the_destination_refuses_too_fails_that_entry_only(disk, dest, monkeypatch):
    disk.add_file('disk:/a?b.txt', b'question')
    disk.add_file('disk:/Q?/x.txt', b'inside')
    _refuse_names(monkeypatch, lambda name: '?' in name or '_' in name)

    result = run_download(str(dest))

    assert result.exit_code == 1
    strerror = os.strerror(errno.EINVAL)
    folder_reason = (
        f'the destination filesystem refuses the name Q? ({strerror}), and its converted form Q_~{_hex("Q?")} too'
    )
    assert (
        f'Failed: 3\n  disk:/Q?: {folder_reason}\n  disk:/Q?/x.txt: {folder_reason}\n'
        f'  disk:/a?b.txt: the destination filesystem refuses the name a?b.txt ({strerror}), '
        f'and its converted form a_b~{_hex("a?b.txt")}.txt too\n'
    ) in result.stdout
    assert 'Renamed for the destination' not in result.stdout
    assert _others_verified(dest, failed='')
    assert sorted(p.name for p in dest.iterdir()) == sorted(
        ['.yandex-disk-rescue', 'Docs', 'Empty', 'Photos', 'top.txt']
    )

    rerun = run_download(str(dest))

    assert rerun.exit_code == 1
    assert result.stdout.split('Failed: 3\n')[1] == rerun.stdout.split('Failed: 3\n')[1]


def test_a_run_stopped_once_a_converted_file_is_placed_finds_it_there_on_the_next_run(disk, dest, monkeypatch):
    disk.add_file('disk:/Odd/a?b.txt', b'question')
    _refuse_names(monkeypatch, lambda name: '?' in name)
    converted = f'a_b~{_hex("a?b.txt")}.txt'
    rename_noreplace = localfs.rename_noreplace

    def placed_then_failed(src, dst, *, src_dir_fd, dst_dir_fd):
        rename_noreplace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        if dst == converted:
            raise OSError(errno.EIO, os.strerror(errno.EIO), dst)

    monkeypatch.setattr(localfs, 'rename_noreplace', placed_then_failed)
    stopped = run_download('disk:/Odd', str(dest))

    assert stopped.exit_code == 1
    assert stopped.stderr == f'Error: Could not write {dest / converted}: {os.strerror(errno.EIO)}.\n'

    monkeypatch.setattr(localfs, 'rename_noreplace', rename_noreplace)
    earlier = len(disk.requests)
    rerun = run_download('disk:/Odd', str(dest))

    assert rerun.exit_code == 0, rerun.output
    assert 'Already on disk and verified: 1\n  disk:/Odd/a?b.txt\n' in rerun.stdout
    assert f'Renamed for the destination: 1\n  disk:/Odd/a?b.txt -> {converted}\n' in rerun.stdout
    assert _transfers(disk, earlier) == []
    assert sorted(p.name for p in dest.iterdir()) == ['.yandex-disk-rescue', converted]
    assert not (dest / '.yandex-disk-rescue' / 'replaced').exists()


def test_converting_names_in_a_large_mirror_costs_one_folder_not_the_whole_mirror(tmp_path):
    folders = [Resource(f'disk:/F{n:03}', 'dir', None, None, None, MODIFIED) for n in range(100)]
    files = [Resource(f'disk:/F{n % 100:03}/f{n}.txt', 'file', 1, None, None, MODIFIED) for n in range(50_000)]
    with Manifest.open(tmp_path / 'manifest.sqlite3') as manifest:
        session = manifest.begin_scan('uid', 'disk:/')
        session.add([*folders, *files])
        session.commit(unencodable=())
        mirror = manifest.mirror('uid', 'disk:/', tmp_path / 'dest')
        ids = {e.resource.path: e.entry_id for e in mirror.entries()}
        mirror.assign_names({entry_id: PurePosixPath(path.removeprefix('disk:/')) for path, entry_id in ids.items()})
        names = mirroring.LocalNames(mirror, mirror.entries())

        began = time.monotonic()
        for refused in [*(f'disk:/F000/f{n * 100}.txt' for n in range(10)), 'disk:/F001']:
            names.rename(ids[refused], names.conversion(ids[refused]))
        elapsed = time.monotonic() - began
        recorded = {e.entry_id: e.local_path for e in mirror.entries()}

    assert elapsed < 1
    assert names[ids['disk:/F000/f100.txt']] == PurePosixPath(f'F000/f100~{_hex("f100.txt")}.txt')
    assert names[ids['disk:/F001/f1.txt']] == PurePosixPath(f'F001~{_hex("F001")}/f1.txt')
    assert all(names[entry_id] == recorded[entry_id] for entry_id in ids.values())


@pytest.fixture
def removed_dest(dest):
    """`dest`, removed after the test: tools walking the temporary folder by full path fail on a tree deeper than
    PATH_MAX."""
    yield dest
    shutil.rmtree(dest)


def test_a_path_deeper_than_path_max_is_mirrored(disk, removed_dest):
    dest = removed_dest
    folders = '/'.join(f'{level:02}' + 'd' * 248 for level in range(17))
    disk.add_file(f'disk:/Deep/{folders}/deep.txt', b'deep')

    result = run_download('disk:/Deep', str(dest))

    assert result.exit_code == 0, result.output
    assert len(os.fsencode(dest / folders / 'deep.txt')) > os.pathconf(dest, 'PC_PATH_MAX')
    assert f'Mirrored disk:/Deep to {dest}: 1 downloaded (4 B), 0 skipped, 0 quarantined, 0 failed\n' in result.stdout


def test_an_untracked_identical_file_is_adopted_without_a_download(disk, dest):
    readme = dest / 'Docs' / 'readme.txt'
    readme.parent.mkdir(parents=True)
    readme.write_bytes(FILES['disk:/Docs/readme.txt'])
    inode = readme.stat().st_ino

    result = run_download(str(dest))

    assert result.exit_code == 0, result.output
    assert f'Mirrored disk:/ to {dest}: 5 downloaded ' in result.stdout
    assert (
        ', 1 skipped, 0 quarantined, 0 failed\nAlready on disk and verified: 1\n  disk:/Docs/readme.txt\n'
        in result.stdout
    )
    assert 'disk:/Docs/readme.txt' not in _linked(disk)
    assert readme.stat().st_ino == inode
    assert readme.stat().st_mtime_ns == _mtime_ns(MODIFIED)
    assert not (dest / '.yandex-disk-rescue' / 'replaced').exists()

    _overwrite_keeping_mtime(readme, b'X' * readme.stat().st_size)
    earlier = len(disk.requests)
    rerun = run_download(str(dest))

    assert rerun.exit_code == 0, rerun.output
    assert '0 downloaded (0 B), 6 skipped, 0 quarantined, 0 failed\n' in rerun.stdout
    assert 'Already on disk' not in rerun.stdout
    assert _transfers(disk, earlier) == []
    assert readme.read_bytes() == b'X' * len(FILES['disk:/Docs/readme.txt'])


def test_a_mirror_whose_manifest_was_lost_is_adopted_without_any_transfer(disk, dest, tmp_path, monkeypatch):
    assert run_download(str(dest)).exit_code == 0
    inodes = {path: local_file(dest, path).stat().st_ino for path in FILES}
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'new-state'))
    earlier = len(disk.requests)

    result = run_download(str(dest))

    assert result.exit_code == 0, result.output
    assert f'Mirrored disk:/ to {dest}: 0 downloaded (0 B), 6 skipped, 0 quarantined, 0 failed\n' in result.stdout
    assert 'Already on disk and verified: 6\n  disk:/Docs/readme.txt\n' in result.stdout
    assert _transfers(disk, earlier) == []
    assert {path: local_file(dest, path).stat().st_ino for path in FILES} == inodes
    assert not (dest / '.yandex-disk-rescue' / 'replaced').exists()


@pytest.mark.parametrize('mine', [b'my own notes', b'not mine'], ids=['other-size', 'same-size'])
def test_an_untracked_different_file_is_moved_aside_and_the_cloud_file_placed(disk, dest, mine):
    readme = dest / 'Docs' / 'readme.txt'
    readme.parent.mkdir(parents=True)
    readme.write_bytes(mine)
    os.utime(readme, ns=(10**18, 10**18))

    result = run_download(str(dest))

    assert result.exit_code == 0, result.output
    [run] = replaced_runs(dest)
    assert _STAMP.fullmatch(run.name)
    assert (
        f'Moved aside to .yandex-disk-rescue/replaced/{run.name}/: 1\n  Docs/readme.txt\nManifest: '
    ) in result.stdout
    assert (run / 'Docs' / 'readme.txt').read_bytes() == mine
    assert (run / 'Docs' / 'readme.txt').stat().st_mtime_ns == 10**18
    assert readme.read_bytes() == FILES['disk:/Docs/readme.txt']
    assert '6 downloaded' in result.stdout


def test_a_run_never_reuses_the_replaced_folder_of_an_earlier_run(disk, dest):
    replaced = dest / '.yandex-disk-rescue' / 'replaced'
    now = datetime.now(UTC).replace(microsecond=0)
    taken = [(now + timedelta(seconds=s)).strftime('%Y-%m-%dT%H-%M-%SZ') for s in range(-1, 30)]
    for name in taken:
        (replaced / name).mkdir(parents=True)
    local_file(dest, 'disk:/top.txt').write_bytes(b'mine')

    result = run_download(str(dest))

    assert result.exit_code == 0, result.output
    [run] = [p for p in replaced.iterdir() if p.name not in taken]
    assert run.name.rpartition('-')[0] in taken
    assert run.name.endswith('-2')
    assert (run / 'top.txt').read_bytes() == b'mine'
    assert all(list((replaced / name).iterdir()) == [] for name in taken)


@pytest.mark.parametrize('fault', [None, 'corrupt'])
def test_a_file_changed_in_the_cloud_replaces_the_old_copy_only_once_verified(disk, dest, fault):
    path = 'disk:/Docs/readme.txt'
    readme = local_file(dest, path)
    assert run_download(str(dest)).exit_code == 0
    before = readme.stat()
    disk.add_file(path, b'read me, edited', modified=MODIFIED)
    if fault:
        disk.corrupt(path, offset=0)

    result = run_download('--scan-max-age', '0', str(dest))

    if fault:
        assert result.exit_code == 1
        [run] = quarantine_runs(dest)
        assert f'Quarantined: 1\n  {path}: MD5 mismatch' in result.stdout
        assert readme.read_bytes() == FILES[path]
        assert (readme.stat().st_ino, readme.stat().st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
        assert (run / 'Docs' / 'readme.txt').read_bytes() == bytes([ord('r') ^ 0xFF]) + b'ead me, edited'
        assert not (dest / '.yandex-disk-rescue' / 'replaced').exists()
    else:
        assert result.exit_code == 0, result.output
        [run] = replaced_runs(dest)
        assert '1 downloaded (15 B), 5 skipped, 0 quarantined, 0 failed\n' in result.stdout
        assert f'Moved aside to .yandex-disk-rescue/replaced/{run.name}/: 1\n  Docs/readme.txt\n' in result.stdout
        assert (run / 'Docs' / 'readme.txt').read_bytes() == FILES[path]
        assert readme.read_bytes() == b'read me, edited'


def test_things_in_the_way_are_moved_aside_and_links_are_never_followed(disk, dest, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    secret = outside / 'secret.txt'
    secret.write_bytes(b'linked')
    disk.add_file('disk:/Links/target.txt', b'linked')
    dest.mkdir()
    (dest / 'Photos').write_bytes(b'not a folder')
    (dest / 'Docs').symlink_to(outside)
    (dest / 'Links').mkdir()
    (dest / 'Links' / 'target.txt').symlink_to(secret)
    (dest / 'top.txt').mkdir()
    (dest / 'top.txt' / 'inside.txt').write_bytes(b'kept')

    result = run_download(str(dest))

    assert result.exit_code == 0, result.output
    [run] = replaced_runs(dest)
    assert (
        f'Moved aside to .yandex-disk-rescue/replaced/{run.name}/: 4\n  Docs\n  Links/target.txt\n  Photos\n  top.txt\n'
    ) in result.stdout
    assert f'Mirrored disk:/ to {dest}: 7 downloaded ' in result.stdout
    assert (run / 'Photos').read_bytes() == b'not a folder'
    assert (run / 'Docs').readlink() == outside
    assert (run / 'Links' / 'target.txt').readlink() == secret
    assert (run / 'top.txt' / 'inside.txt').read_bytes() == b'kept'
    assert sorted(outside.iterdir()) == [secret]
    assert secret.read_bytes() == b'linked'
    for path, content in {**FILES, 'disk:/Links/target.txt': b'linked'}.items():
        local = local_file(dest, path)
        assert not local.is_symlink()
        assert local.read_bytes() == content


@pytest.mark.parametrize('change', ['content', 'mtime'])
def test_a_verified_file_touched_locally_is_hashed_again(disk, dest, change):
    path = 'disk:/Photos/2024/b.jpg'
    local = local_file(dest, path)
    assert run_download(str(dest)).exit_code == 0
    if change == 'content':
        local.write_bytes(b'B' * len(FILES[path]))
    else:
        os.utime(local, ns=(10**18, 10**18))
    earlier = len(disk.requests)

    result = run_download(str(dest))

    assert result.exit_code == 0, result.output
    assert local.read_bytes() == FILES[path]
    assert local.stat().st_mtime_ns == _mtime_ns(MODIFIED)
    if change == 'content':
        [run] = replaced_runs(dest)
        assert (run / 'Photos' / '2024' / 'b.jpg').read_bytes() == b'B' * len(FILES[path])
        assert _transfers(disk, earlier) == ['/v1/disk/resources/download', '/download']
    else:
        assert f'Already on disk and verified: 1\n  {path}\n' in result.stdout
        assert _transfers(disk, earlier) == []
        assert not (dest / '.yandex-disk-rescue' / 'replaced').exists()


def test_local_only_content_is_never_touched_or_reported(disk, dest):
    local_only = {
        '.Spotlight-V100/store.db': b'spotlight',
        '.DS_Store': b'finder',
        'notes.txt': b'my notes',
        'Photos/local-only.jpg': b'my photo',
        'Загрузки/файл.pdf': b'download',
        'Загрузки/inner/more.bin': b'more',
    }
    for relative, content in local_only.items():
        (dest / relative).parent.mkdir(parents=True, exist_ok=True)
        (dest / relative).write_bytes(content)
        os.utime(dest / relative, ns=(10**18, 10**18))
    stats = {relative: (dest / relative).stat() for relative in local_only}

    for _ in range(2):
        result = run_download(str(dest))

        assert result.exit_code == 0, result.output
        for relative, content in local_only.items():
            now = (dest / relative).stat()
            assert (dest / relative).read_bytes() == content
            assert (now.st_mtime_ns, now.st_ino) == (stats[relative].st_mtime_ns, stats[relative].st_ino)
            assert PurePosixPath(relative).name not in result.stdout
        assert '.Spotlight-V100' not in result.stdout
        assert 'Загрузки' not in result.stdout
        assert 'Moved aside' not in result.stdout
        assert 'Already on disk' not in result.stdout
        assert not (dest / '.yandex-disk-rescue' / 'replaced').exists()


def test_a_hard_link_to_another_verified_file_is_never_replaced(disk, dest):
    disk.add_file('disk:/Pair/a.bin', b'first')
    disk.add_file('disk:/Pair/b.bin', b'second')
    assert run_download('disk:/Pair', str(dest)).exit_code == 0
    (dest / 'b.bin').unlink()
    os.link(dest / 'a.bin', dest / 'b.bin')
    disk.add_file('disk:/Pair/b.bin', b'second, edited')
    inode = (dest / 'a.bin').stat().st_ino

    for _ in range(2):
        result = run_download('--scan-max-age', '0', 'disk:/Pair', str(dest))

        assert result.exit_code == 1
        assert (
            'Failed: 1\n  disk:/Pair/b.bin: b.bin is the same file as disk:/Pair/a.bin on this filesystem; '
            'it was left untouched\n'
        ) in result.stdout
        assert (dest / 'a.bin').read_bytes() == b'first'
        assert (dest / 'a.bin').stat().st_ino == inode
        assert not (dest / '.yandex-disk-rescue' / 'replaced').exists()


def test_hard_linked_untracked_copies_handled_at_once_adopt_only_one(disk, tmp_path, monkeypatch):
    disk.add_file('disk:/Pair/a.bin', b'same bytes')
    disk.add_file('disk:/Pair/b.bin', b'same bytes')

    for attempt in range(20):
        monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / f'state-{attempt}'))
        dest = tmp_path / f'dest-{attempt}'
        dest.mkdir()
        (dest / 'a.bin').write_bytes(b'same bytes')
        os.link(dest / 'a.bin', dest / 'b.bin')

        result = run_download('disk:/Pair', str(dest))

        assert result.exit_code == 1, result.output
        match = re.search(r'Already on disk and verified: 1\n  disk:/Pair/(\w)\.bin\n', result.stdout)
        assert match is not None, result.stdout
        adopted, other = match[1], {'a': 'b', 'b': 'a'}[match[1]]
        assert (
            f'Failed: 1\n  disk:/Pair/{other}.bin: {other}.bin is the same file as disk:/Pair/{adopted}.bin on this '
            'filesystem; it was left untouched\n'
        ) in result.stdout
        assert ', 1 skipped, 0 quarantined, 1 failed\n' in result.stdout
        assert (dest / 'a.bin').stat().st_ino == (dest / 'b.bin').stat().st_ino
        assert not (dest / '.yandex-disk-rescue' / 'replaced').exists()
        with closing(sqlite3.connect(manifest_path())) as connection:
            assert connection.execute("SELECT count(*) FROM downloads WHERE state = 'verified'").fetchone() == (1,)


@pytest.mark.parametrize(
    ('status', 'reason'),
    [
        (403, 'Yandex Disk API returned HTTP 403: Injected HTTP 403'),
        (404, 'removed from the Disk after the scan'),
    ],
)
def test_a_link_refused_for_one_file_fails_only_that_file(disk, dest, status, reason):
    disk.inject('GET', '/v1/disk/resources/download', status, where={'path': 'disk:/Docs/readme.txt'})

    result = run_download(str(dest))

    assert result.exit_code == 1
    assert '5 downloaded' in result.stdout
    assert f'Failed: 1\n  disk:/Docs/readme.txt: {reason}\n' in result.stdout
    assert not (dest / 'Docs' / 'readme.txt').exists()


def test_a_token_rejected_on_a_link_request_stops_the_run(disk, dest):
    disk.inject('GET', '/v1/disk/resources/download', 401)

    result = run_download(str(dest))

    assert result.exit_code == 1
    assert result.stderr.startswith('Error: Yandex rejected the token (invalid, expired or revoked).')
    assert 'Mirrored' not in result.stdout


def test_a_name_that_is_not_valid_unicode_fails_that_entry_only(disk, dest):
    disk.add_file('disk:/Docs/bad\udc80.txt', b'unreachable')
    disk.add_file('disk:/Bad\udc81/inside.txt', b'unreachable too')

    failed = (
        'Failed: 2\n'
        '  disk:/Bad\\udc81: the folder name is not valid Unicode, so the Disk API cannot list it or download '
        'anything in it\n'
        '  disk:/Docs/bad\\udc80.txt: the name is not valid Unicode, so the Disk API cannot download it\n'
    )

    result = run_download(str(dest))

    assert result.exit_code == 1
    assert failed in result.stdout
    assert '6 downloaded' in result.stdout
    assert all(local_file(dest, path).read_bytes() == content for path, content in FILES.items())

    for reuse in (['--no-scan'], [], ['--retry-failed']):
        earlier = len(disk.requests)
        rerun = run_download(*reuse, str(dest))

        assert rerun.exit_code == 1
        assert 'Using the scan of disk:/ completed ' in rerun.stdout
        assert failed in rerun.stdout
        assert _listings(disk, earlier) == []

    disk.remove('disk:/Docs/bad\udc80.txt')
    disk.remove('disk:/Bad\udc81')
    rescan = run_download('--scan-max-age', '0', str(dest))

    assert rescan.exit_code == 0, rescan.output
    assert 'not valid Unicode' not in rescan.stdout


_REUSED = re.compile(r'Using the scan of (disk:/\S*) completed \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC \((.+)\)')


def _reused_scan(result) -> tuple[str, str]:
    """The root and the parenthesized note of the `Using the scan of` line, which is the run's first line."""
    match = _REUSED.fullmatch(result.stdout.splitlines()[0])
    assert match is not None, result.stdout
    return match.group(1), match.group(2)


def test_a_rerun_inside_the_scan_window_lists_nothing_and_a_window_of_zero_scans_again(disk, dest):
    assert run_download(str(dest)).exit_code == 0
    earlier = len(disk.requests)

    rerun = run_download(str(dest))

    assert rerun.exit_code == 0, rerun.output
    assert _reused_scan(rerun) == ('disk:/', '0 min ago')
    assert 'Scanned' not in rerun.stdout
    assert _listings(disk, earlier) == []
    assert f'Mirrored disk:/ to {dest}: 0 downloaded (0 B), 6 skipped, 0 quarantined, 0 failed\n' in rerun.stdout

    earlier = len(disk.requests)
    always = run_download('--scan-max-age', '0', str(dest))

    assert always.exit_code == 0, always.output
    assert always.stdout.startswith('Scanned disk:/: 6 files, 4 folders')
    assert 'Using the scan of' not in always.stdout
    assert 'disk:/' in _listings(disk, earlier)


def test_a_scan_of_an_ancestor_inside_the_window_is_reused(disk, dest):
    assert runner.invoke(app, ['scan']).exit_code == 0
    earlier = len(disk.requests)

    result = run_download('disk:/Photos', str(dest))

    assert result.exit_code == 0, result.output
    assert _reused_scan(result) == ('disk:/', '0 min ago')
    assert _listings(disk, earlier) == []
    assert (dest / '2024' / 'a.jpg').read_bytes() == FILES['disk:/Photos/2024/a.jpg']


@pytest.mark.parametrize('age', [timedelta(hours=2), timedelta(hours=-1)], ids=['old', 'future'])
def test_a_scan_outside_the_window_is_done_again(disk, dest, age):
    assert run_download(str(dest)).exit_code == 0
    _set_scan_times(datetime.now(UTC) - age)
    earlier = len(disk.requests)

    result = run_download(str(dest))

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith('Scanned disk:/: 6 files, 4 folders')
    assert 'Using the scan of' not in result.stdout
    assert 'disk:/' in _listings(disk, earlier)


def test_no_scan_needs_a_scan_covering_the_folder_and_then_never_lists(disk, dest):
    assert runner.invoke(app, ['scan', 'disk:/Docs']).exit_code == 0
    earlier = len(disk.requests)

    refused = run_download('--no-scan', 'disk:/Photos', str(dest))

    assert refused.exit_code == 2
    assert refused.stderr == (
        "Error: disk:/Photos has never been scanned; run 'yandex-disk-rescue scan disk:/Photos' or drop --no-scan.\n"
    )
    assert not dest.exists()
    assert _transfers(disk, earlier) == []

    _set_scan_times(datetime.now(UTC) - timedelta(days=30))
    earlier = len(disk.requests)
    result = run_download('--no-scan', 'disk:/Docs', str(dest))

    assert result.exit_code == 0, result.output
    assert _reused_scan(result) == ('disk:/Docs', '--no-scan')
    assert _listings(disk, earlier) == []
    assert (dest / 'readme.txt').read_bytes() == FILES['disk:/Docs/readme.txt']


def test_a_scan_max_age_out_of_range_is_a_usage_error(disk, dest):
    result = run_download('--scan-max-age', '10081', str(dest))

    assert result.exit_code == 2
    assert 'must be an integer from 0 to 10080' in result.stderr
    assert disk.requests == []


@pytest.mark.parametrize(
    ('edited', 'listed', 'reason', 'rerun_reason'),
    [
        (
            b'READ ME',
            'Quarantined',
            f'MD5 mismatch (expected {hashlib.md5(b"read me").hexdigest()}, got ',
            'MD5 mismatch',
        ),
        (
            b'read me, edited after the scan',
            'Failed',
            'the download host sent more than the 7 bytes requested',
            CHANGED,
        ),
    ],
    ids=['same-size', 'longer'],
)
def test_a_file_changed_after_the_reused_scan_is_never_kept_as_verified(
    disk, dest, edited, listed, reason, rerun_reason
):
    path = 'disk:/Docs/readme.txt'
    assert runner.invoke(app, ['scan']).exit_code == 0
    disk.add_file(path, edited, modified=MODIFIED)

    result = run_download(str(dest))

    assert result.exit_code == 1
    assert _reused_scan(result)[0] == 'disk:/'
    assert f'{listed}: 1\n  {path}: {reason}' in result.stdout
    assert not local_file(dest, path).exists()

    rerun = run_download(str(dest))

    assert rerun.exit_code == 1
    assert f'{listed}: 1\n  {path}: {rerun_reason}' in rerun.stdout
    assert not local_file(dest, path).exists()
    assert _partials(dest) == []

    rescanned = run_download('--scan-max-age', '0', str(dest))

    assert rescanned.exit_code == 0, rescanned.output
    assert local_file(dest, path).read_bytes() == edited


@pytest.mark.parametrize('args', [[], ['a', 'b', 'c']], ids=['none', 'three'])
def test_download_takes_one_or_two_paths(disk, args):
    result = run_download(*args)

    assert result.exit_code == 2
    assert disk.requests == []


def test_an_unusable_destination_is_a_usage_error_that_creates_nothing(disk, tmp_path):
    regular = tmp_path / 'file'
    regular.write_bytes(b'')
    orphan = tmp_path / 'missing' / 'dest'

    cases = [
        ([str(regular)], f'Error: Destination {regular} exists and is not a folder.\n'),
        (
            [str(orphan)],
            f'Error: The parent folder of {orphan} does not exist; download creates only the last folder of the '
            'destination.\n',
        ),
        ([str(tmp_path / 'x'), 'disk:/A'], 'disk:/A is a Disk path'),
    ]
    for args, message in cases:
        result = run_download(*args)

        assert result.exit_code == 2
        assert message in result.stderr

    assert not orphan.parent.exists()
    assert not (tmp_path / 'x').exists()
    assert not manifest_path().exists()
    assert disk.requests == []


def test_a_destination_mirrors_one_remote_folder(disk, dest):
    assert run_download('disk:/Photos', str(dest)).exit_code == 0

    result = run_download('disk:/Docs', str(dest))

    assert result.exit_code == 2
    assert result.stderr == (
        f'Error: {dest} already mirrors disk:/Photos; download disk:/Photos there or choose another destination.\n'
    )
    assert not (dest / 'readme.txt').exists()


@pytest.mark.parametrize(
    ('flag', 'value', 'valid'),
    [
        ('--download-timeout', '0', '1 to 600'),
        ('--download-retries', '99', '0 to 20'),
        ('--download-passes', '1001', '0 to 1000'),
        ('--download-connections', '0', '1 to 64'),
        ('--download-connections', '65', '1 to 64'),
        ('--download-checkpoint-mib', '0', '1 to 1024'),
        ('--download-checkpoint-mib', '1025', '1 to 1024'),
        ('--download-segments', '0', '1 to 8'),
        ('--download-segments', '9', '1 to 8'),
        ('--download-segment-threshold-mib', '0', '1 to 65536'),
        ('--download-segment-threshold-mib', '65537', '1 to 65536'),
    ],
)
def test_a_transfer_option_out_of_range_is_a_usage_error(disk, dest, monkeypatch, flag, value, valid):
    monkeypatch.setenv('COLUMNS', '200')
    result = run_download(flag, value, str(dest))

    assert result.exit_code == 2
    assert f'must be an integer from {valid}' in result.stderr
    assert disk.requests == []


def test_a_destination_in_use_by_another_run_is_refused_whatever_the_manifest(disk, dest, tmp_path, monkeypatch):
    partial = dest / '.yandex-disk-rescue' / 'partial' / '999'
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b'another run is writing this')
    lock_fd = os.open(dest / '.yandex-disk-rescue' / 'lock', os.O_RDWR | os.O_CREAT)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'other-state'))
    try:
        result = run_download(str(dest))
    finally:
        os.close(lock_fd)

    assert result.exit_code == 1
    assert result.stderr == (
        f'Error: Another yandex-disk-rescue run is using {dest}; wait for it to finish and try again.\n'
    )
    assert partial.read_bytes() == b'another run is writing this'
    assert _transfers(disk) == []


@pytest.mark.parametrize('rename', ['refused', 'replacing'])
def test_a_filesystem_without_the_no_replace_rename_stops_the_run_before_any_download(disk, dest, monkeypatch, rename):
    def unsupported(src, dst, *, src_dir_fd, dst_dir_fd):
        raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))

    def replacing(src, dst, *, src_dir_fd, dst_dir_fd):
        os.rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(localfs, 'rename_noreplace', unsupported if rename == 'refused' else replacing)

    result = run_download(str(dest))

    assert result.exit_code == 1
    assert result.stderr == (
        f'Error: Could not write {dest}: its filesystem does not support renaming without replacing '
        '(RENAME_NOREPLACE), which download needs so it never overwrites a file.\n'
    )
    assert _transfers(disk) == []
    assert sorted(p.name for p in dest.iterdir()) == ['.yandex-disk-rescue']


@pytest.mark.parametrize('code', [errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS], ids=errno.errorcode.get)
def test_a_full_or_failing_destination_stops_the_run_and_a_rerun_goes_on(disk, dest, monkeypatch, code):
    rename_noreplace = localfs.rename_noreplace

    def failing(src, dst, *, src_dir_fd, dst_dir_fd):
        if dst == 'top.txt':
            raise OSError(code, os.strerror(code), dst)
        rename_noreplace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(localfs, 'rename_noreplace', failing)
    result = run_download(str(dest))

    assert result.exit_code == 1
    assert result.stderr == f'Error: Could not write {dest / "top.txt"}: {os.strerror(code)}.\n'
    assert 'Mirrored' not in result.stdout
    assert not (dest / 'top.txt').exists()

    monkeypatch.setattr(localfs, 'rename_noreplace', rename_noreplace)
    rerun = run_download(str(dest))

    assert rerun.exit_code == 0, rerun.output
    counts = re.search(
        rf'Mirrored disk:/ to {re.escape(str(dest))}: (\d+) downloaded \(.+\), (\d+) skipped, 0 quarantined, 0 failed\n',
        rerun.stdout,
    )
    assert counts is not None, rerun.stdout
    assert int(counts[1]) + int(counts[2]) == len(FILES)
    assert _others_verified(dest, failed='')


def _refuse(monkeypatch, call: str, code: int, refused: Callable[[str | int], bool]) -> None:
    """Make `localfs.<call>` fail with `code` whenever `refused` accepts its first argument, a name or a
    descriptor."""
    original = getattr(localfs, call)

    def refusing(target, *args, **kwargs):
        if refused(target):
            raise OSError(code, os.strerror(code))
        return original(target, *args, **kwargs)

    monkeypatch.setattr(localfs, call, refusing)


@pytest.mark.parametrize('code', [errno.EACCES, errno.EPERM], ids=errno.errorcode.get)
def test_a_permission_error_in_the_work_directory_stops_the_run(disk, dest, monkeypatch, code):
    _refuse(monkeypatch, 'set_times', code, lambda fd: True)

    result = run_download(str(dest))

    assert result.exit_code == 1
    partials = _partials(dest)
    assert result.stderr in {f'Error: Could not write {partial}: {os.strerror(code)}.\n' for partial in partials}
    assert 'Mirrored' not in result.stdout


@pytest.mark.parametrize('obstacle', ['mtime-not-permitted', 'unreadable', 'untraversable'])
def test_a_permission_error_on_a_path_of_the_mirror_fails_that_file_only(disk, dest, monkeypatch, obstacle):
    readme, local = 'disk:/Docs/readme.txt', dest / 'Docs' / 'readme.txt'
    if obstacle == 'untraversable':
        assert run_download(str(dest)).exit_code == 0
        _refuse(monkeypatch, 'lstat', errno.EACCES, lambda name: name in {'a.jpg', 'b.jpg'})
        denied = os.strerror(errno.EACCES)
        failed = [
            (f'disk:/Photos/2024/{name}', f'could not read Photos/2024/{name}: {denied}') for name in ('a.jpg', 'b.jpg')
        ]
    else:
        local.parent.mkdir(parents=True)
        local.write_bytes(FILES[readme])
        if obstacle == 'unreadable':
            _refuse(monkeypatch, 'open_file', errno.EACCES, lambda name: name == 'readme.txt')
            failed = [(readme, f'could not read Docs/readme.txt: {os.strerror(errno.EACCES)}')]
        else:
            inode = local.stat().st_ino
            _refuse(monkeypatch, 'set_times', errno.EPERM, lambda fd: os.fstat(fd).st_ino == inode)
            failed = [(readme, f'could not set the modification time of Docs/readme.txt: {os.strerror(errno.EPERM)}')]

    result = run_download(str(dest))

    assert result.exit_code == 1
    listed = ''.join(f'  {path}: {reason}\n' for path, reason in failed)
    assert f', {len(failed)} failed\n' in result.stdout
    assert f'Failed: {len(failed)}\n{listed}' in result.stdout
    assert result.stderr == f'Error: {len(failed)} failed; see the list above.\n'
    assert _others_verified(dest, failed='')
    assert not (dest / '.yandex-disk-rescue' / 'replaced').exists()
    if obstacle != 'untraversable':
        assert local.stat().st_mtime_ns != _mtime_ns(MODIFIED)


def test_a_stall_before_the_first_byte_ends_the_files_turn_after_one_attempt(disk, dest):
    path = 'disk:/Photos/2024/a.jpg'
    disk.inject('GET', '/download', STALL, STALL, where={'path': path})

    result = run_download('--download-timeout', STALL_TIMEOUT, '--download-passes', '1', str(dest))

    assert result.exit_code == 1
    assert 'Passes: 1 of 1\n' in result.stdout
    assert f'Failed: 1\n  {path}: the download stalled: no data for {STALL_TIMEOUT} s\n' in result.stdout
    assert _fetches(disk, path) == [None]
    assert _others_verified(dest, path)


def test_dead_files_do_not_hold_up_the_healthy_ones(disk, dest):
    dead = [f'disk:/Mixed/a-dead{n}.bin' for n in range(3)]
    healthy = [f'disk:/Mixed/b-healthy{n:02}.bin' for n in range(12)]
    for path in dead + healthy:
        disk.add_file(path, path.encode() * 100)
    for path in dead:
        disk.inject('GET', '/download', STALL, where={'path': path})

    result = run_download('--download-timeout', STALL_TIMEOUT, '--download-passes', '1', 'disk:/Mixed', str(dest))

    assert result.exit_code == 1
    stalled = ''.join(f'  {path}: the download stalled: no data for {STALL_TIMEOUT} s\n' for path in dead)
    assert (
        f'12 downloaded ({format_size(sum(len(p) * 100 for p in healthy))}), 0 skipped, 0 quarantined, 3 failed\n'
        in result.stdout
    )
    assert f'Passes: 1 of 1\nFailed: 3\n{stalled}' in result.stdout
    assert all(local_file(dest, path.replace('Mixed/', '')).read_bytes() == path.encode() * 100 for path in healthy)
    started = {r.params['path']: r.started for r in disk.requests if r.path == '/download'}
    first_dead = min(started[path] for path in dead)
    assert max(started[path] for path in healthy) - first_dead < float(STALL_TIMEOUT)


def test_throughput_scales_with_the_connection_cap_and_never_passes_it(disk, tmp_path):
    for n in range(8):
        disk.add_file(f'disk:/Load/f{n}.bin', random.Random(n).randbytes(10 * 64 * 1024))
    disk.chunk_delay = 0.05
    spans = {}

    for cap in (1, 4):
        disk.peak_content_in_flight = 0
        earlier = len(disk.requests)

        result = run_download('--download-connections', str(cap), 'disk:/Load', str(tmp_path / f'dest-{cap}'))

        assert result.exit_code == 0, result.output
        assert disk.peak_content_in_flight == cap
        content = [r for r in disk.requests[earlier:] if r.path == '/download']
        assert len(content) == 8
        spans[cap] = max(r.finished for r in content) - min(r.started for r in content)

    assert spans[4] < spans[1] / 2


def test_a_file_stalled_in_one_pass_resumes_in_the_next_with_a_fresh_link(big, dest):
    big.inject('GET', '/download', Truncate(after_bytes=300_000), STALL, where={'path': BIG_PATH})

    result = run_download('--download-timeout', STALL_TIMEOUT, 'disk:/Big', str(dest))

    assert result.exit_code == 0, result.output
    assert (
        f'Mirrored disk:/Big to {dest}: 1 downloaded ({format_size(len(BIG))}), 0 skipped, 0 quarantined, 0 failed\nPasses: 2 of 7\n'
    ) in result.stdout
    assert (dest / 'big.bin').read_bytes() == BIG
    assert _fetches(big, BIG_PATH) == [None, _tail(300_000), _tail(300_000)]
    assert _linked(big).count(BIG_PATH) == 2
    assert _partials(dest) == []


def test_a_file_stalled_on_every_pass_fails_after_the_last_and_the_others_succeed(big, dest):
    big.inject('GET', '/download', STALL, STALL, where={'path': BIG_PATH})

    result = run_download('--download-timeout', STALL_TIMEOUT, '--download-passes', '2', str(dest))

    assert result.exit_code == 1
    assert ', 1 failed\nPasses: 2 of 2\n' in result.stdout
    assert (
        f'Failed: 1\n  {BIG_PATH}: the download stalled: no data for {STALL_TIMEOUT} s (after 2 passes)\n'
    ) in result.stdout
    assert _fetches(big, BIG_PATH) == [None, None]
    assert _others_verified(dest, BIG_PATH)
    assert _failure_records() == [(BIG_PATH, f'the download stalled: no data for {STALL_TIMEOUT} s (after 2 passes)')]


def test_retry_failed_fetches_only_the_failed_files_and_never_looks_at_the_others(disk, dest):
    failed = 'disk:/Docs/readme.txt'
    disk.inject('GET', '/download', 503, where={'path': failed})
    first = run_download('--download-passes', '1', str(dest))

    assert first.exit_code == 1
    assert f'Failed: 1\n  {failed}: ' in first.stdout
    assert [path for path, _ in _failure_records()] == [failed]

    disk.add_file('disk:/New/n.txt', b'new', modified=MODIFIED)
    # A plain run would fetch this one again; a retry never looks at it.
    local_file(dest, 'disk:/Photos/2024/b.jpg').unlink()
    earlier = len(disk.requests)
    retry = run_download('--retry-failed', '--scan-max-age', '0', str(dest))

    assert retry.exit_code == 0, retry.output
    assert f'Retrying 1 failed, quarantined, unfinished or broken file (7 B) in {dest}\n' in retry.stdout
    assert f'Mirrored disk:/ to {dest}: 1 downloaded (7 B), 0 skipped, 0 quarantined, 0 failed\n' in retry.stdout
    assert [r.params['path'] for r in disk.requests[earlier:] if r.path in _CONTENT_PATHS] == [failed, failed]
    assert local_file(dest, failed).read_bytes() == FILES[failed]
    assert not (dest / 'New').exists()
    assert not local_file(dest, 'disk:/Photos/2024/b.jpg').exists()
    assert _failure_records() == []

    earlier = len(disk.requests)
    again = run_download('--retry-failed', str(dest))

    assert again.exit_code == 0, again.output
    assert f'Retrying 0 failed, quarantined, unfinished or broken files (0 B) in {dest}\n' in again.stdout
    assert f'Mirrored disk:/ to {dest}: 0 downloaded (0 B), 0 skipped, 0 quarantined, 0 failed\n' in again.stdout
    assert _transfers(disk, earlier) == []


def test_retry_failed_makes_a_failed_folder_and_the_files_under_it(disk, dest, monkeypatch):
    disk.add_file('disk:/Q?/x.txt', b'inside')
    refusing = True
    _refuse_names(monkeypatch, lambda name: refusing and ('?' in name or '_' in name))
    first = run_download(str(dest))

    assert first.exit_code == 1
    assert 'Failed: 2\n  disk:/Q?: the destination filesystem refuses the name Q? ' in first.stdout
    assert [path for path, _ in _failure_records()] == ['disk:/Q?', 'disk:/Q?/x.txt']

    refusing = False
    retry = run_download('--retry-failed', str(dest))

    assert retry.exit_code == 0, retry.output
    assert f'Retrying 1 failed, quarantined, unfinished or broken file (6 B) in {dest}\n' in retry.stdout
    assert f'Mirrored disk:/ to {dest}: 1 downloaded (6 B), 0 skipped, 0 quarantined, 0 failed\n' in retry.stdout
    assert (dest / 'Q?' / 'x.txt').read_bytes() == b'inside'
    assert _failure_records() == []

    earlier = len(disk.requests)
    again = run_download('--retry-failed', str(dest))

    assert again.exit_code == 0, again.output
    assert f'Retrying 0 failed, quarantined, unfinished or broken files (0 B) in {dest}\n' in again.stdout
    assert _transfers(disk, earlier) == []


def test_a_corrupt_file_is_not_retried_in_a_later_pass(disk, dest):
    corrupt, deferred = 'disk:/Photos/2024/a.jpg', 'disk:/Docs/readme.txt'
    disk.corrupt(corrupt, offset=0)
    disk.inject('GET', '/download', 503, where={'path': deferred})

    result = run_download(str(dest))

    assert result.exit_code == 1
    assert 'Passes: 2 of 7\n' in result.stdout
    assert ', 1 quarantined, 0 failed\n' in result.stdout
    [run] = quarantine_runs(dest)
    [line] = [line for line in result.stdout.splitlines() if line.startswith(f'  {corrupt}: ')]
    assert line.startswith(f'  {corrupt}: MD5 mismatch (expected ')
    assert line.endswith(f'; kept at .yandex-disk-rescue/quarantine/{run.name}/Photos/2024/a.jpg')
    assert '(after' not in line
    assert _linked(disk).count(corrupt) == 1
    assert _fetches(disk, deferred) == [None, None]
    assert _others_verified(dest, corrupt)


def test_unlimited_passes_go_on_until_the_file_is_done(big, dest):
    big.inject('GET', '/download', 503, 503, 503, where={'path': BIG_PATH})

    result = run_download('--download-passes', '0', 'disk:/Big', str(dest))

    assert result.exit_code == 0, result.output
    assert ', 0 failed\nPasses: 4 of unlimited\n' in result.stdout
    assert _fetches(big, BIG_PATH) == [None] * 4
    assert (dest / 'big.bin').read_bytes() == BIG


def test_the_next_pass_waits_as_long_as_the_download_host_asks(big, dest):
    big.inject('GET', '/download', FakeResponse.error(503, {'Retry-After': '1'}), where={'path': BIG_PATH})

    result = run_download('disk:/Big', str(dest))

    assert result.exit_code == 0, result.output
    assert 'Passes: 2 of 7\n' in result.stdout
    first, second = [r.started for r in big.requests if r.path == '/download']
    assert second - first >= 0.9


def test_a_download_cut_short_resumes_at_once_from_the_bytes_received(big, dest):
    big.inject('GET', '/download', Truncate(after_bytes=300_000), where={'path': BIG_PATH})

    result = run_download('disk:/Big', str(dest))

    assert result.exit_code == 0, result.output
    assert 'Passes' not in result.stdout
    assert (dest / 'big.bin').read_bytes() == BIG
    assert _fetches(big, BIG_PATH) == [None, _tail(300_000)]
    assert _linked(big).count(BIG_PATH) == 1


@pytest.mark.parametrize(('cuts', 'requests'), [(2, 3), (6, 6)], ids=['within-budget', 'budget-spent'])
def test_retries_after_new_data_are_bounded_by_download_retries(big, dest, cuts, requests):
    big.inject('GET', '/download', *[Truncate(after_bytes=1000)] * cuts, where={'path': BIG_PATH})

    result = run_download('--download-passes', '1', 'disk:/Big', str(dest))

    assert _fetches(big, BIG_PATH) == [None] + [_tail(1000 * n) for n in range(1, requests)]
    if requests > cuts:
        assert result.exit_code == 0, result.output
        assert (dest / 'big.bin').read_bytes() == BIG
    else:
        assert result.exit_code == 1
        [failure] = [line for line in result.stdout.splitlines() if line.startswith(f'  {BIG_PATH}: ')]
        assert failure.startswith(f'  {BIG_PATH}: the download failed: ')
        assert failure.endswith(' (after 6 attempts)')
        assert _partial_size(dest) == 6000


@pytest.mark.parametrize(
    ('expiries', 'passes'), [(1, '7'), (2, '7'), (2, '1')], ids=['refreshed', 'next-pass', 'no-pass-left']
)
def test_an_expired_link_is_refreshed_once_per_pass(big, dest, expiries, passes):
    big.inject('GET', '/download', Truncate(after_bytes=300_000), *[410] * expiries, where={'path': BIG_PATH})

    result = run_download('--download-passes', passes, str(dest))

    assert _others_verified(dest, BIG_PATH)
    if expiries == 1:
        assert result.exit_code == 0, result.output
        assert 'Passes' not in result.stdout
        assert _linked(big).count(BIG_PATH) == 2
        assert _fetches(big, BIG_PATH) == [None, _tail(300_000), _tail(300_000)]
        assert (dest / 'Big' / 'big.bin').read_bytes() == BIG
    elif passes == '7':
        assert result.exit_code == 0, result.output
        assert 'Passes: 2 of 7\n' in result.stdout
        assert _linked(big).count(BIG_PATH) == 3
        assert _fetches(big, BIG_PATH) == [None, *[_tail(300_000)] * 3]
        assert (dest / 'Big' / 'big.bin').read_bytes() == BIG
    else:
        assert result.exit_code == 1
        assert 'Passes: 1 of 1\n' in result.stdout
        assert f'Failed: 1\n  {BIG_PATH}: download link rejected again after a refresh (HTTP 410)\n' in result.stdout
        assert _linked(big).count(BIG_PATH) == 2
        assert _partial_size(dest) == 300_000


@pytest.mark.parametrize('ignore_range', [False, True], ids=['ranged', 'range-ignored'])
def test_a_partial_file_is_resumed_by_the_next_run_once_the_cloud_file_is_confirmed(big, dest, ignore_range):
    partials = _leave_partial(big, dest).parent
    (partials / '987654321').write_bytes(b'no entry has this id')
    (partials / 'notes').write_bytes(b'not an entry id')
    (partials / 'a folder').mkdir()
    big.ignore_range = ignore_range
    earlier = len(big.requests)

    result = run_download('disk:/Big', str(dest))

    assert result.exit_code == 0, result.output
    assert (dest / 'big.bin').read_bytes() == BIG
    lookups = [r for r in big.requests[earlier:] if r.path == '/v1/disk/resources' and r.params['path'] == BIG_PATH]
    assert len(lookups) == 1
    assert _fetches(big, BIG_PATH, earlier) == ([_tail(300_000), None] if ignore_range else [_tail(300_000)])
    assert _partials(dest) == [partials / 'a folder']


@pytest.mark.parametrize('change', ['rescanned', 'changed', 'removed'])
def test_a_partial_file_of_a_file_changed_in_the_cloud_is_never_resumed(big, dest, change):
    _leave_partial(big, dest)
    edited = random.Random(9).randbytes(len(BIG))
    if change == 'removed':
        big.remove(BIG_PATH)
    else:
        big.add_file(BIG_PATH, edited, modified=MODIFIED)
    earlier = len(big.requests)

    result = run_download(*(['--scan-max-age', '0'] if change == 'rescanned' else []), 'disk:/Big', str(dest))

    assert _partials(dest) == []
    if change == 'rescanned':
        assert result.exit_code == 0, result.output
        assert _fetches(big, BIG_PATH, earlier) == [None]
        assert (dest / 'big.bin').read_bytes() == edited
        return
    reason = CHANGED if change == 'changed' else 'removed from the Disk after the scan'
    assert result.exit_code == 1
    assert f'Failed: 1\n  {BIG_PATH}: {reason}\n' in result.stdout
    assert _transfers(big, earlier) == []
    assert not (dest / 'big.bin').exists()


@pytest.mark.parametrize('held', ['confirmed', 'unconfirmed-tail'])
def test_a_partial_file_keeps_only_what_its_journal_confirms(big, dest, held):
    partial = _leave_partial(big, dest)
    if held == 'confirmed':
        partial.write_bytes(BIG)
        with Manifest.open(manifest_path()) as manifest:
            mirror = manifest.mirror('1', 'disk:/Big', dest.resolve())
            mirror.checkpoint(int(partial.name), [Segment(0, len(BIG), len(BIG), hashlib.sha256(BIG).hexdigest())])
    else:
        with partial.open('ab') as file:
            file.write(b'never confirmed' * 1000)
    earlier = len(big.requests)

    result = run_download('disk:/Big', str(dest))

    assert result.exit_code == 0, result.output
    assert f'1 downloaded ({format_size(len(BIG))}), ' in result.stdout
    assert (dest / 'big.bin').read_bytes() == BIG
    assert _fetches(big, BIG_PATH, earlier) == ([] if held == 'confirmed' else [_tail(300_000)])
    assert _linked(big).count(BIG_PATH) == (1 if held == 'confirmed' else 2)
    assert _journal() == []


@pytest.mark.parametrize('change', ['tampered', 'shortened', 'deleted'])
def test_a_partial_file_whose_confirmed_bytes_changed_on_disk_is_fetched_again_from_byte_0(big, dest, change):
    partial = _leave_partial(big, dest)
    if change == 'tampered':
        held = bytearray(partial.read_bytes())
        held[123_456] ^= 0xFF
        partial.write_bytes(held)
    elif change == 'shortened':
        os.truncate(partial, 299_999)
    else:
        partial.unlink()
    earlier = len(big.requests)

    result = run_download('disk:/Big', str(dest))

    assert result.exit_code == 0, result.output
    assert _fetches(big, BIG_PATH, earlier) == [None]
    assert (dest / 'big.bin').read_bytes() == BIG


def test_a_file_deferred_after_new_bytes_resumes_in_the_next_pass_from_its_journal(big, dest):
    big.inject('GET', '/download', Truncate(after_bytes=300_000), where={'path': BIG_PATH})

    result = run_download('--download-retries', '0', 'disk:/Big', str(dest))

    assert result.exit_code == 0, result.output
    assert 'Passes: 2 of 7\n' in result.stdout
    assert _fetches(big, BIG_PATH) == [None, _tail(300_000)]
    assert (dest / 'big.bin').read_bytes() == BIG


def test_a_slow_download_takes_a_checkpoint_every_checkpoint_seconds(big, dest, monkeypatch):
    monkeypatch.setattr(mirroring, 'CHECKPOINT_SECONDS', 0.3)
    big.chunk_delay = 0.2
    results = []
    # `asyncio.run` installs no signal handler outside the main thread, so the command runs as it would there.
    worker = threading.Thread(target=lambda: results.append(run_download('disk:/Big', str(dest))))
    worker.start()
    while_running: set[int] = set()
    try:
        while worker.is_alive():
            fetches = [r for r in big.requests if r.path == '/download']
            confirmed = _confirmed()
            if fetches and fetches[0].finished is None:
                while_running.add(confirmed)
            time.sleep(0.02)
    finally:
        worker.join()

    [result] = results
    assert result.exit_code == 0, result.output
    assert (dest / 'big.bin').read_bytes() == BIG
    assert len({confirmed for confirmed in while_running if 0 < confirmed < len(BIG)}) >= 2


def _fail_fsync(monkeypatch) -> tuple[threading.Event, list[int]]:
    """Make `os.fsync` fail with EIO once the returned event is set; the list collects the descriptors it failed."""
    failing, failed = threading.Event(), []
    fsync = os.fsync

    def maybe_failing(fd):
        if failing.is_set():
            failed.append(fd)
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        fsync(fd)

    monkeypatch.setattr(os, 'fsync', maybe_failing)
    return failing, failed


def test_checkpoints_failing_while_a_stopped_run_cancels_its_files_add_no_error_line(disk, dest, monkeypatch):
    slow = [f'disk:/Stop/slow{n}.bin' for n in range(3)]
    for n, path in enumerate(slow):
        disk.add_file(path, random.Random(n).randbytes(1024 * 1024))
    disk.add_file('disk:/Stop/z.txt', b'placed last')
    disk.chunk_delay = 0.2
    failing, failed = _fail_fsync(monkeypatch)
    rename_noreplace = localfs.rename_noreplace

    def failing_once_the_others_have_bytes(src, dst, *, src_dir_fd, dst_dir_fd):
        if dst == 'z.txt':
            deadline = time.monotonic() + 10
            while sum(p.stat().st_size >= 64 * 1024 for p in _partials(dest)) < len(slow):
                assert time.monotonic() < deadline, 'the slow files never received a chunk'
                time.sleep(0.01)
            failing.set()
            raise OSError(errno.EIO, os.strerror(errno.EIO), dst)
        rename_noreplace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(localfs, 'rename_noreplace', failing_once_the_others_have_bytes)

    result = run_download('disk:/Stop', str(dest))

    assert result.exit_code == 1
    assert result.stderr == f'Error: Could not write {dest / "z.txt"}: {os.strerror(errno.EIO)}.\n'
    assert 'Mirrored' not in result.stdout
    assert len(failed) == len(slow)
    assert sorted(_journal()) == [(0, len(b'placed last'), 0, None)] + [(0, 1024 * 1024, 0, None)] * len(slow)
    assert len(_partials(dest)) == len(slow) + 1


def test_ctrl_c_mid_file_checkpoints_every_byte_written_and_the_rerun_resumes_there(big, dest):
    big.chunk_delay = 0.2
    child = start_command('download', 'disk:/Big', str(dest))
    try:
        wait_until(lambda: _partial_size(dest) > 256 * 1024, child, 'wrote 256 KiB')
        child.send_signal(signal.SIGINT)
        _, stderr = child.communicate(timeout=60)
    finally:
        child.kill()
        child.communicate()

    assert child.returncode == 130, stderr
    assert stderr == 'Interrupted; rerun to continue the download.\n'
    held = _partial_size(dest)
    assert 256 * 1024 < held < len(BIG)
    assert _journal() == [(0, len(BIG), held, hashlib.sha256(BIG[:held]).hexdigest())]

    big.chunk_delay = 0
    earlier = len(big.requests)
    result = run_download('--retry-failed', 'disk:/Big', str(dest))

    assert result.exit_code == 0, result.output
    assert (
        f'Retrying 1 failed, quarantined, unfinished or broken file ({format_size(len(BIG))}) in {dest}\n'
        in result.stdout
    )
    assert _fetches(big, BIG_PATH, earlier) == [_tail(held)]
    assert _transfers(big, earlier) == ['/v1/disk/resources/download', '/download']
    assert (dest / 'big.bin').read_bytes() == BIG


def test_a_download_killed_mid_file_fetches_at_most_one_checkpoint_interval_again(disk, dest):
    path, content = 'disk:/Kill/k.bin', random.Random(5).randbytes(4 * 1024 * 1024)
    disk.add_file(path, content, modified=MODIFIED)
    disk.chunk_delay = 0.25
    child = start_command('download', '--download-checkpoint-mib', '1', 'disk:/Kill', str(dest))
    try:
        wait_until(lambda: _confirmed() >= 1024 * 1024, child, 'confirmed 1 MiB')
        child.kill()
        child.communicate(timeout=60)
    finally:
        child.kill()
        child.communicate()

    [(_, _, confirmed, digest)] = _journal()
    assert digest == hashlib.sha256(content[:confirmed]).hexdigest()
    [served] = [r for r in disk.requests if r.path == '/download']
    deadline = time.monotonic() + 10
    while served.finished is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert served.finished is not None
    # The interval itself, plus one chunk crossing it, one received but not yet written when the fsync began, and two
    # sent but not yet read by the client.
    assert served.sent - confirmed <= 1024 * 1024 + 4 * 64 * 1024

    disk.chunk_delay = 0
    earlier = len(disk.requests)
    result = run_download('disk:/Kill', str(dest))

    assert result.exit_code == 0, result.output
    assert _fetches(disk, path, earlier) == [f'bytes={confirmed}-{len(content) - 1}']
    assert (dest / 'k.bin').read_bytes() == content


def test_ctrl_c_in_the_pause_between_passes_stops_the_run_and_keeps_the_partial_file(big, dest, monkeypatch):
    monkeypatch.setenv('YANDEX_DISK_RESCUE_DOWNLOAD_TIMEOUT', '1')
    big.inject('GET', '/download', Truncate(after_bytes=300_000), Delay(30), where={'path': BIG_PATH})
    child = start_command('download', 'disk:/Big', str(dest))
    try:
        wait_until(lambda: len(_fetches(big, BIG_PATH)) == 2, child, 'resumed the file')
        resumed = [r.started for r in big.requests if r.path == '/download'][-1]
        time.sleep(max(0.0, resumed + 2.5 - time.monotonic()))
        child.send_signal(signal.SIGINT)
        _, stderr = child.communicate(timeout=60)
    finally:
        child.kill()
        child.communicate()

    assert child.returncode == 130, stderr
    assert stderr == 'Interrupted; rerun to continue the download.\n'
    assert len(_fetches(big, BIG_PATH)) == 2
    assert _partial_size(dest) == 300_000


# Installed as `sitecustomize` in a download subprocess: with `FSYNC_GATE` set to a folder, an fsync of a partial file
# waits for the marker `go` there; the first one after it fails with EIO at once, each later one creates a `blocked-<n>`
# marker and fails with EIO once the marker `release` exists.
_FSYNC_GATE = """
import errno, itertools, os, pathlib, threading, time

_gate = pathlib.Path(os.environ['FSYNC_GATE'])
_fsync, _calls, _lock = os.fsync, itertools.count(1), threading.Lock()


def _wait(marker):
    while not (_gate / marker).exists():
        time.sleep(0.01)


def fsync(fd):
    if '/.yandex-disk-rescue/partial/' not in os.readlink(f'/proc/self/fd/{fd}'):
        return _fsync(fd)
    _wait('go')
    with _lock:
        call = next(_calls)
    if call > 1:
        (_gate / f'blocked-{call}').touch()
        _wait('release')
    raise OSError(errno.EIO, os.strerror(errno.EIO))


os.fsync = fsync
"""


def test_ctrl_c_while_a_run_stopping_error_cancels_the_files_wins_over_the_error(disk, dest, tmp_path, monkeypatch):
    slow = [f'disk:/Stop/slow{n}.bin' for n in range(3)]
    for n, path in enumerate(slow):
        disk.add_file(path, random.Random(n).randbytes(MiB))
    disk.add_file('disk:/Stop/z.txt', b'placed last')
    disk.chunk_delay = 0.2
    site, gate = tmp_path / 'site', tmp_path / 'gate'
    site.mkdir()
    gate.mkdir()
    (site / 'sitecustomize.py').write_text(_FSYNC_GATE)
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join(filter(None, [str(site), os.environ.get('PYTHONPATH')])))
    monkeypatch.setenv('FSYNC_GATE', str(gate))
    child = start_command('download', 'disk:/Stop', str(dest))
    try:
        wait_until(
            lambda: sum(p.stat().st_size >= 64 * 1024 for p in _partials(dest)) == len(slow),
            child,
            'wrote a chunk of every slow file',
        )
        # z.txt fails as it is finished, which stops the run; each slow file's checkpoint then hangs in its fsync.
        (gate / 'go').touch()
        wait_until(lambda: len(list(gate.glob('blocked-*'))) == len(slow), child, 'checkpointed the cancelled files')
        child.send_signal(signal.SIGINT)
        time.sleep(0.5)
        (gate / 'release').touch()
        _, stderr = child.communicate(timeout=60)
    finally:
        child.kill()
        child.communicate()

    assert child.returncode == 130, stderr
    assert stderr == 'Interrupted; rerun to continue the download.\n'
    assert sorted(_journal()) == [(0, len(b'placed last'), 0, None)] + [(0, MiB, 0, None)] * len(slow)


@pytest.mark.parametrize(
    ('size', 'args', 'ranges', 'peak'),
    [
        (4 * MiB, SEGMENTED_ARGS, SEGMENT_RANGES, 4),
        (4 * MiB, (*SEGMENTED_ARGS, '--download-connections', '2'), SEGMENT_RANGES, 2),
        (4 * MiB, (*SEGMENTED_ARGS, '--download-connections', '1'), SEGMENT_RANGES, 1),
        (1 * MiB, SEGMENTED_ARGS, [None], 1),
        (4 * MiB, (*SEGMENTED_ARGS, '--download-segments', '1'), [None], 1),
    ],
    ids=['four-segments', 'two-connections', 'one-connection', 'at-the-threshold', 'one-segment'],
)
def test_a_file_above_the_segment_threshold_is_fetched_as_parallel_segments_tiling_it(
    disk, dest, size, args, ranges, peak
):
    content = random.Random(17).randbytes(size)
    disk.add_file(SEGMENTED_PATH, content, modified=MODIFIED)
    disk.chunk_delay = 0.02

    result = run_download(*args, 'disk:/Seg', str(dest))

    assert result.exit_code == 0, result.output
    assert (
        f'Mirrored disk:/Seg to {dest}: 1 downloaded ({format_size(size)}), 0 skipped, 0 quarantined, 0 failed\n'
        in result.stdout
    )
    assert sorted(_fetches(disk, SEGMENTED_PATH), key=str) == ranges
    assert disk.peak_content_in_flight == peak
    assert _linked(disk) == [SEGMENTED_PATH]
    local = dest / 'large.bin'
    assert local.read_bytes() == content
    assert local.stat().st_mtime_ns == _mtime_ns(MODIFIED)
    assert _partials(dest) == []
    assert _journal() == []


def _segments_confirmed(at_least: int) -> bool:
    journal = _journal()
    return len(journal) == 4 and all(confirmed - start >= at_least for start, _, confirmed, _ in journal)


def test_a_segmented_download_killed_midway_fetches_at_most_one_checkpoint_interval_per_segment_again(disk, dest):
    path, content = 'disk:/Kill/k.bin', random.Random(19).randbytes(16 * MiB)
    disk.add_file(path, content, modified=MODIFIED)
    disk.chunk_delay = 0.25
    ranges = [f'bytes={n * 4 * MiB}-{(n + 1) * 4 * MiB - 1}' for n in range(4)]
    child = start_command(
        'download',
        *SEGMENTED_ARGS,
        '--download-segments',
        '4',
        '--download-checkpoint-mib',
        '1',
        'disk:/Kill',
        str(dest),
    )
    try:
        wait_until(lambda: _segments_confirmed(MiB), child, 'confirmed 1 MiB of every segment')
        child.kill()
        child.communicate(timeout=60)
    finally:
        child.kill()
        child.communicate()

    journal = _journal()
    assert [(start, end) for start, end, _, _ in journal] == [(n * 4 * MiB, (n + 1) * 4 * MiB) for n in range(4)]
    served = {r.headers['range']: r for r in disk.requests if r.path == '/download'}
    assert sorted(served) == sorted(ranges)
    deadline = time.monotonic() + 10
    while any(r.finished is None for r in served.values()) and time.monotonic() < deadline:
        time.sleep(0.01)
    for (start, end, confirmed, digest), segment in zip(journal, ranges, strict=True):
        assert start < confirmed < end
        assert digest == hashlib.sha256(content[start:confirmed]).hexdigest()
        # As for one stream: the interval, one chunk crossing it, one not yet written when the fsync began, and two
        # sent but not yet read. A checkpoint another segment triggered only lowers this segment's share.
        assert served[segment].finished is not None
        assert served[segment].sent - (confirmed - start) <= MiB + 4 * 64 * 1024

    disk.chunk_delay = 0
    earlier = len(disk.requests)
    result = run_download(*SEGMENTED_ARGS, 'disk:/Kill', str(dest))

    assert result.exit_code == 0, result.output
    assert sorted(_fetches(disk, path, earlier)) == sorted(
        f'bytes={confirmed}-{end - 1}' for _, end, confirmed, _ in journal
    )
    assert (dest / 'k.bin').read_bytes() == content


def _each_segment_sent(fake, at_least: int) -> bool:
    fetches = [r for r in fake.requests if r.path == '/download' and r.params['path'] == SEGMENTED_PATH]
    return len(fetches) == 4 and all(r.sent >= at_least for r in fetches)


def _interrupt_segmented(fake, dest) -> list[tuple[int, int, int, str | None]]:
    """Start a segmented download of `SEGMENTED_PATH`, stop it with Ctrl+C once every segment has received some data,
    and return its journal."""
    fake.chunk_delay = 0.2
    child = start_command('download', *SEGMENTED_ARGS, 'disk:/Seg', str(dest))
    try:
        wait_until(lambda: _each_segment_sent(fake, 256 * 1024), child, 'sent 256 KiB of every segment')
        child.send_signal(signal.SIGINT)
        _, stderr = child.communicate(timeout=60)
    finally:
        child.kill()
        child.communicate()

    assert child.returncode == 130, stderr
    journal = _journal()
    assert [(start, end) for start, end, _, _ in journal] == [(n * MiB, (n + 1) * MiB) for n in range(4)]
    for start, end, confirmed, digest in journal:
        assert start + 64 * 1024 <= confirmed < end
        assert digest == hashlib.sha256(SEGMENTED[start:confirmed]).hexdigest()
    return journal


@pytest.mark.parametrize('change', ['none', 'tampered', 'fewer-segments'])
def test_an_interrupted_segmented_download_resumes_each_segment_from_its_own_checkpoint(segmented, dest, change):
    journal = _interrupt_segmented(segmented, dest)
    [partial] = _partials(dest)
    assert partial.stat().st_size == len(SEGMENTED)
    resumed_from = {start: confirmed for start, _, confirmed, _ in journal}
    args = list(SEGMENTED_ARGS)
    if change == 'tampered':
        second = journal[1][0]
        held = bytearray(partial.read_bytes())
        held[second + 1000] ^= 0xFF
        partial.write_bytes(held)
        resumed_from[second] = second
    elif change == 'fewer-segments':
        args += ['--download-segments', '1']
    segmented.chunk_delay = 0
    earlier = len(segmented.requests)

    result = run_download(*args, 'disk:/Seg', str(dest))

    assert result.exit_code == 0, result.output
    assert sorted(_fetches(segmented, SEGMENTED_PATH, earlier)) == sorted(
        f'bytes={resumed_from[start]}-{end - 1}' for start, end, _, _ in journal
    )
    assert (dest / 'large.bin').read_bytes() == SEGMENTED


def test_a_stalled_segment_alone_is_fetched_again_in_the_next_pass(segmented, dest):
    segmented.inject(
        'GET', '/download', STALL, where={'path': SEGMENTED_PATH}, where_headers={'range': SEGMENT_RANGES[2]}
    )

    result = run_download('--download-timeout', STALL_TIMEOUT, *SEGMENTED_ARGS, 'disk:/Seg', str(dest))

    assert result.exit_code == 0, result.output
    assert (
        f'Mirrored disk:/Seg to {dest}: 1 downloaded ({format_size(len(SEGMENTED))}), 0 skipped, 0 quarantined, 0 failed\n'
        'Passes: 2 of 7\n'
    ) in result.stdout
    fetches = _fetches(segmented, SEGMENTED_PATH)
    assert sorted(fetches[:4]) == SEGMENT_RANGES
    assert fetches[4:] == [SEGMENT_RANGES[2]]
    assert _linked(segmented) == [SEGMENTED_PATH] * 2
    assert (dest / 'large.bin').read_bytes() == SEGMENTED


def test_a_segment_failing_for_good_fails_the_file_and_its_other_segments_keep_their_checkpoints(segmented, dest):
    segmented.chunk_delay = 0.2
    failing = SEGMENT_RANGES[2]
    segmented.inject(
        'GET',
        '/download',
        Delay(1, FakeResponse.error(404)),
        where={'path': SEGMENTED_PATH},
        where_headers={'range': failing},
    )

    result = run_download(*SEGMENTED_ARGS, 'disk:/Seg', str(dest))

    assert result.exit_code == 1
    assert f'Failed: 1\n  {SEGMENTED_PATH}: the download host 127.0.0.1 returned HTTP 404\n' in result.stdout
    journal = _journal()
    assert [(start, end) for start, end, _, _ in journal] == [(n * MiB, (n + 1) * MiB) for n in range(4)]
    for position, (start, end, confirmed, digest) in enumerate(journal):
        if position == 2:
            assert (confirmed, digest) == (start, None)
        else:
            assert start < confirmed < end
            assert digest == hashlib.sha256(SEGMENTED[start:confirmed]).hexdigest()
    assert len(_partials(dest)) == 1

    segmented.chunk_delay = 0
    earlier = len(segmented.requests)
    rerun = run_download(*SEGMENTED_ARGS, 'disk:/Seg', str(dest))

    assert rerun.exit_code == 0, rerun.output
    assert sorted(_fetches(segmented, SEGMENTED_PATH, earlier)) == sorted(
        f'bytes={confirmed}-{end - 1}' for _, end, confirmed, _ in journal
    )
    assert failing in _fetches(segmented, SEGMENTED_PATH, earlier)
    assert (dest / 'large.bin').read_bytes() == SEGMENTED


def test_a_file_takes_its_download_link_only_once_it_holds_a_connection(segmented, dest):
    segmented.add_file('disk:/Seg/small.txt', b'queued behind the segments', modified=MODIFIED)
    segmented.chunk_delay = 0.05

    result = run_download(*SEGMENTED_ARGS, '--download-connections', '2', 'disk:/Seg', str(dest))

    assert result.exit_code == 0, result.output
    assert (dest / 'small.txt').read_bytes() == b'queued behind the segments'
    [small_link] = [
        r
        for r in segmented.requests
        if r.path == '/v1/disk/resources/download' and r.params['path'] == 'disk:/Seg/small.txt'
    ]
    first_segment = min(r.started for r in segmented.requests if r.path == '/download')
    # Each segment takes 15 chunk delays; the small file's turn starts at once but waits for a connection first.
    assert small_link.started - first_segment > 0.5


def test_a_host_ignoring_the_range_of_a_segment_gets_the_file_as_one_stream(segmented, dest, monkeypatch):
    segmented.ignore_range = True
    monkeypatch.setattr(mirroring, '_PREALLOCATION_STEP', MiB)
    lengths: list[int] = []
    ends: list[int] = []
    write, truncate = PartialFile.write, PartialFile.truncate

    def recording_truncate(partial, length):
        lengths.append(length)
        truncate(partial, length)

    def recording_write(partial, offset, chunk):
        ends.append(offset + len(chunk))
        write(partial, offset, chunk)

    monkeypatch.setattr(PartialFile, 'truncate', recording_truncate)
    monkeypatch.setattr(PartialFile, 'write', recording_write)

    result = run_download(*SEGMENTED_ARGS, 'disk:/Seg', str(dest))

    assert result.exit_code == 0, result.output
    assert (dest / 'large.bin').read_bytes() == SEGMENTED
    fetches = _fetches(segmented, SEGMENTED_PATH)
    assert fetches[-1] is None
    assert fetches[:-1]
    assert set(fetches[:-1]) <= set(SEGMENT_RANGES)
    assert lengths == [MiB, 2 * MiB, 3 * MiB, 4 * MiB, 0]
    assert max(ends) == len(SEGMENTED)
    assert _journal() == []


FAST_API = api.ApiLimits(interval=0.001, concurrency=7, timeout=5, retries=0, backoff_initial=0.001, backoff_max=0.01)
POLICY = mirroring.TransferPolicy(retries=0, checkpoint_bytes=32 * 2**20, segments=4, segment_threshold=256 * 2**20)


@asynccontextmanager
async def _mirror_run(
    fake, root: str, dest, policy: mirroring.TransferPolicy = POLICY
) -> AsyncIterator[Callable[[], Awaitable[MirrorReport]]]:
    """Scan `root` in this event loop, then yield a call running the mirror run of `root` into `dest` here too, with
    real clients, the real manifest and a real destination, fetching as `policy` says."""
    async with (
        api.DiskClient(TOKEN, base_url=fake.base_url, limits=FAST_API) as client,
        api.ContentClient(timeout=5, connections=20) as content,
    ):
        account = await client.account()
        scan_root = await resolve_scan_root(client, root)
        with Manifest.open(manifest_path()) as manifest, Destination.open(dest) as destination:
            await scan_tree(client, manifest, account, scan_root)
            run = MirrorRun(
                manifest.mirror(account.uid, scan_root.path, destination.path), destination, retry_failed=False
            )
            yield lambda: run.run(client, content, policy, passes=1)


async def _until(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + 10
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(f'never {what}')
        await asyncio.sleep(0.01)


@pytest.mark.anyio
async def test_a_cancelled_run_waits_for_the_write_in_flight_before_closing_the_partial_file(big, dest, monkeypatch):
    big.chunk_delay = 0.01
    blocked, release = threading.Event(), threading.Event()
    written: list[bytes] = []
    open_after_write: list[os.stat_result] = []
    write = PartialFile.write

    def blocking_write(partial, offset, chunk):
        if written:
            blocked.set()
            release.wait(5)
        write(partial, offset, chunk)
        written.append(chunk)
        open_after_write.append(os.fstat(partial._fd))

    monkeypatch.setattr(PartialFile, 'write', blocking_write)
    async with _mirror_run(big, 'disk:/Big', dest) as run:
        task = asyncio.create_task(run())
        await _until(blocked.is_set, 'blocked in the second write')
        task.cancel()
        await asyncio.sleep(0.3)
        finished_while_blocked = task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert not finished_while_blocked
    assert len(written) == 2
    [partial] = _partials(dest)
    assert [stat.st_ino for stat in open_after_write] == [partial.stat().st_ino] * 2
    assert partial.read_bytes() == BIG[: len(written[0]) + len(written[1])]


@pytest.mark.anyio
async def test_the_event_loop_keeps_running_while_a_local_copy_is_hashed(disk, dest, monkeypatch):
    copy = random.Random(11).randbytes(8 * 1024 * 1024)
    disk.add_file('disk:/Hash/copy.bin', copy, modified=MODIFIED)
    dest.mkdir()
    (dest / 'copy.bin').write_bytes(copy)
    reads: list[float] = []
    read = localfs.read

    def slow_read(fd, size, offset):
        reads.append(time.monotonic())
        time.sleep(0.05)
        chunk = read(fd, size, offset)
        reads.append(time.monotonic())
        return chunk

    monkeypatch.setattr(localfs, 'read', slow_read)
    ticks: list[float] = []

    async def tick() -> None:
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    async with _mirror_run(disk, 'disk:/Hash', dest) as run:
        ticker = asyncio.create_task(tick())
        try:
            report = await run()
        finally:
            ticker.cancel()

    assert report.adopted == ['disk:/Hash/copy.bin']
    assert len(reads) > 16
    hashing_began, hashing_ended = reads[0], reads[-1]
    wakeups = [hashing_began, *(t for t in ticks if hashing_began < t < hashing_ended), hashing_ended]
    assert max(later - earlier for earlier, later in itertools.pairwise(wakeups)) < 0.2


@pytest.mark.anyio
async def test_a_file_downloads_while_another_ones_local_copy_is_hashed(disk, dest, monkeypatch):
    copy = random.Random(13).randbytes(8 * 1024 * 1024)
    disk.add_file('disk:/Overlap/a-copy.bin', copy, modified=MODIFIED)
    disk.add_file('disk:/Overlap/b-fetched.bin', b'fetched meanwhile', modified=MODIFIED)
    dest.mkdir()
    (dest / 'a-copy.bin').write_bytes(copy)
    reads: list[tuple[float, float]] = []
    read = localfs.read

    def slow_read(fd, size, offset):
        began = time.monotonic()
        time.sleep(0.1)
        chunk = read(fd, size, offset)
        reads.append((began, time.monotonic()))
        return chunk

    monkeypatch.setattr(localfs, 'read', slow_read)
    async with _mirror_run(disk, 'disk:/Overlap', dest) as run:
        report = await run()

    assert report.adopted == ['disk:/Overlap/a-copy.bin']
    assert report.downloaded == 1
    assert (dest / 'b-fetched.bin').read_bytes() == b'fetched meanwhile'
    [fetch] = [r for r in disk.requests if r.path == '/download']
    assert reads[0][0] < fetch.started
    assert fetch.finished < reads[-1][1]


@pytest.mark.anyio
async def test_a_file_cancelled_while_a_segment_meets_an_ignored_range_keeps_its_segments(segmented, dest, monkeypatch):
    segmented.chunk_delay = 0.1
    segmented.inject(
        'GET',
        '/download',
        Delay(0.5, FakeResponse(200)),
        where={'path': SEGMENTED_PATH},
        where_headers={'range': SEGMENT_RANGES[3]},
    )
    release = threading.Event()
    blocked: list[int] = []
    writes: dict[int, int] = {}
    write = PartialFile.write

    def write_one_chunk_per_segment(partial, offset, chunk):
        segment = offset // MiB
        writes[segment] = writes.get(segment, 0) + 1
        if writes[segment] > 1:
            blocked.append(segment)
            release.wait(10)
        write(partial, offset, chunk)

    monkeypatch.setattr(PartialFile, 'write', write_one_chunk_per_segment)

    def checkpointed_the_first_chunks() -> bool:
        received = [confirmed - start for start, _, confirmed, _ in _journal()]
        return len(received) == 4 and all(received[:3]) and received[3] == 0

    async with _mirror_run(segmented, 'disk:/Seg', dest, replace(POLICY, segment_threshold=MiB)) as run:
        task = asyncio.create_task(run())
        await _until(checkpointed_the_first_chunks, 'checkpointed after the ignored range')
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.sleep(0.1)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert sorted(blocked) == [0, 1, 2]
    assert None not in _fetches(segmented, SEGMENTED_PATH)
    assert [(start, end) for start, end, _, _ in _journal()] == [(n * MiB, (n + 1) * MiB) for n in range(4)]
    [partial] = _partials(dest)
    assert partial.stat().st_size == len(SEGMENTED)
    assert not (dest / 'large.bin').exists()


@pytest.mark.anyio
async def test_segments_meeting_an_expired_link_share_one_refresh(segmented, dest):
    segmented.chunk_delay = 0.1
    cut = 512 * 1024
    first, last = SEGMENT_RANGES[0], SEGMENT_RANGES[3]
    resumed = [f'bytes={cut}-{MiB - 1}', f'bytes={3 * MiB + cut}-{4 * MiB - 1}']
    # Both cut segments resume on the expired link before either learns it has expired, the first one earlier.
    for segment, resumed_segment, wait in ((first, resumed[0], 0.3), (last, resumed[1], 0.6)):
        where = {'path': SEGMENTED_PATH}
        segmented.inject('GET', '/download', Truncate(after_bytes=cut), where=where, where_headers={'range': segment})
        segmented.inject('GET', '/download', Delay(wait), where=where, where_headers={'range': resumed_segment})
    policy = replace(POLICY, retries=5, segment_threshold=MiB)

    async with _mirror_run(segmented, 'disk:/Seg', dest, policy) as run:
        task = asyncio.create_task(run())
        await _until(lambda: len(_fetches(segmented, SEGMENTED_PATH)) == 4, 'started every segment')
        segmented.expire_links()
        report = await task

    assert report.failed == []
    assert report.downloaded == 1
    assert (dest / 'large.bin').read_bytes() == SEGMENTED
    assert _linked(segmented) == [SEGMENTED_PATH] * 2
    later = [r for r in segmented.requests if r.path == '/download'][4:]
    assert sorted((r.params['link'], r.headers['range']) for r in later) == sorted(
        [
            ('1', resumed[0]),
            ('1', resumed[1]),
            ('2', resumed[0]),
            ('2', resumed[1]),
        ]
    )
