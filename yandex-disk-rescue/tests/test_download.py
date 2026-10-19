"""Tests for `yandex-disk-rescue download` against the fake Disk API and download host, into a real `tmp_path`
destination, with the real manifest in the XDG state directory."""

import asyncio
import errno
import fcntl
import hashlib
import itertools
import json
import os
import random
import re
import shutil
import signal
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, closing, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path, PurePosixPath

import pytest
from fake_disk_server import Delay, FakeResponse, Truncate
from mirror_tree import (
    FILES,
    MODIFIED,
    SIZE_ONLY,
    TOKEN,
    age_silence,
    corrupted_bytes,
    failure_record,
    failure_records,
    local_file,
    log_messages,
    quarantine_runs,
    replaced_runs,
    run_download,
    runner,
    start_command,
    status_lines,
    stderr_lines,
    wait_until,
)
from yandex_disk_rescue import api, localfs, mirroring
from yandex_disk_rescue import destination as destination_module
from yandex_disk_rescue.api import Resource
from yandex_disk_rescue.cli import app
from yandex_disk_rescue.destination import Destination, PartialFile
from yandex_disk_rescue.manifest import Manifest, Mirror, Segment
from yandex_disk_rescue.mirroring import MirrorReport, MirrorRun
from yandex_disk_rescue.paths import log_path, manifest_path
from yandex_disk_rescue.progress import MirrorProgress, MoveProgress, ScanProgress
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


def _separate_folder(dest, partial_root):
    """The folder of `dest` below the partial folder `partial_root`, named by the SHA-256 of its path."""
    return partial_root / hashlib.sha256(os.fsencode(dest)).hexdigest()[:16]


def _partials(dest, partial_root=None) -> list:
    """The partial files of `dest`: in its own partial folder, or in its folder below `partial_root`, without the
    run's transient probes."""
    folder = dest / '.yandex-disk-rescue' / 'partial' if partial_root is None else _separate_folder(dest, partial_root)
    if not folder.is_dir():
        return []
    transient = (*destination_module._PROBE_NAMES, destination_module._SPARSE_PROBE_NAME)
    return sorted(path for path in folder.iterdir() if path.name not in transient)


def _partial_sizes(dest) -> list[int]:
    """The sizes of the partial files of `dest` while a run changes them: a file deleted after it is listed is left
    out."""
    sizes = []
    for partial in _partials(dest):
        try:
            sizes.append(partial.stat().st_size)
        except FileNotFoundError:
            continue
    return sizes


def _partial_args(partial_root) -> tuple[str, ...]:
    return () if partial_root is None else ('--partial-dir', str(partial_root))


def _io_log(monkeypatch, calls: tuple[str, ...]) -> list[tuple[str, str, int, int]]:
    """Log `(call, path, offset, length)` for every call of `os.<call>`, the path read from `/proc/self/fd`;
    `os.write` and `os.read` log the file position as the offset (-1 when there is none), `os.ftruncate` the new
    length."""
    log: list[tuple[str, str, int, int]] = []

    def position(fd: int) -> int:
        try:
            return os.lseek(fd, 0, os.SEEK_CUR)
        except OSError:
            return -1

    measures = {
        'pwrite': lambda fd, data, offset: (offset, len(data)),
        'pread': lambda fd, size, offset: (offset, size),
        'write': lambda fd, data: (position(fd), len(data)),
        'read': lambda fd, size: (position(fd), size),
        'ftruncate': lambda fd, length: (length, 0),
    }

    def logged(call: str):
        original, measure = getattr(os, call), measures[call]

        def wrapper(fd, *args):
            log.append((call, os.readlink(f'/proc/self/fd/{fd}'), *measure(fd, *args)))
            return original(fd, *args)

        return wrapper

    for call in calls:
        monkeypatch.setattr(os, call, logged(call))
    return log


@dataclass
class ZeroFill:
    """The zeros an emulated filesystem without sparse files wrote, by the name of the file it extended."""

    written: dict[str, int] = field(default_factory=dict)


@pytest.fixture
def non_sparse(monkeypatch) -> Callable[[Path], ZeroFill]:
    """`emulate(folder)` makes the files below `folder` behave as on a filesystem without sparse files (HFS+, exFAT,
    FAT), as Linux `hfsplus` does: an `os.ftruncate` that extends a file, or an `os.pwrite` past its end, first writes
    the zeros of the gap, one call at a time. There is no such filesystem an unprivileged test can mount; everything
    else is real: the files, their `st_blocks`, the fake server and the CLI."""
    ftruncate, pwrite = os.ftruncate, os.pwrite
    lock = threading.Lock()

    def emulate(folder: Path) -> ZeroFill:
        zero_fill = ZeroFill()

        def fill_to(fd: int, end: int) -> None:
            path = os.readlink(f'/proc/self/fd/{fd}')
            offset = os.fstat(fd).st_size
            if not _below(path, folder) or end <= offset:
                return
            name = Path(path).name
            zero_fill.written[name] = zero_fill.written.get(name, 0) + end - offset
            while offset < end:
                offset += pwrite(fd, bytes(min(MiB, end - offset)), offset)

        def extending_ftruncate(fd: int, length: int) -> None:
            with lock:
                fill_to(fd, length)
                ftruncate(fd, length)

        def extending_pwrite(fd: int, data, offset: int) -> int:
            with lock:
                fill_to(fd, offset)
                return pwrite(fd, data, offset)

        monkeypatch.setattr(os, 'ftruncate', extending_ftruncate)
        monkeypatch.setattr(os, 'pwrite', extending_pwrite)
        return zero_fill

    return emulate


def _below(path: str, folder) -> bool:
    return path.startswith(f'{folder}/')


def _partial_size(dest) -> int:
    return sum(_partial_sizes(dest))


def _fetches(fake, path: str, since: int = 0) -> list[str | None]:
    """The `Range` header (`None` when absent) of each content request for `path` after the first `since` requests."""
    return [r.headers.get('range') for r in fake.requests[since:] if r.path == '/download' and r.params['path'] == path]


def _tail(start: int) -> str:
    return f'bytes={start}-{len(BIG) - 1}'


def _leave_partial(fake, dest, *, after_bytes: int = 300_000, partial_root=None):
    """Run a download of `disk:/Big` cut short with no retry and no later pass, which keeps its partial file, in DEST
    or below `partial_root`."""
    fake.inject('GET', '/download', Truncate(after_bytes=after_bytes), where={'path': BIG_PATH})
    args = ('--download-retries', '0', '--download-passes', '1', *_partial_args(partial_root))
    result = run_download(*args, 'disk:/Big', str(dest))
    assert result.exit_code == 1
    [partial] = _partials(dest, partial_root)
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


def _failure_records() -> list[tuple[str, str, int]]:
    """The (remote path, reason, failed turns) of every failure record in the manifest."""
    return [(path, reason, turns) for path, (reason, turns, *_) in failure_records().items()]


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
        f'Log: {log_path()}',
    ]
    assert lines[0].startswith('Scanned disk:/: 6 files, 4 folders')
    if redirect:
        assert len([r for r in disk.requests if r.path == '/storage']) == len(FILES)
    request_kinds = {'/v1/disk/', '/v1/disk/resources', '/v1/disk/resources/download', '/download'}
    assert {r.path for r in disk.requests} >= request_kinds | ({'/storage'} if redirect else set())
    assert {r.headers['user-agent'] for r in disk.requests} == {
        f'yandex-disk-rescue/{version("yandex-disk-rescue")} (+https://github.com/azorej/yandex-disk-rescue)'
    }


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


@pytest.mark.parametrize('partials', ['in-dest', 'separate'])
@pytest.mark.parametrize('segmented', [False, True], ids=['one-stream', 'segmented'])
def test_a_corrupted_download_is_quarantined_and_fetched_again_only_with_retry_failed(
    disk, dest, segmented, partials, request
):
    path = 'disk:/Photos/2024/a.jpg'
    # Segmented, the corrupt byte lies in the third segment and only reading the whole file back, or hashing the copy
    # to DEST, can catch it.
    content, offset, args = (SEGMENTED, 2 * MiB + 10, SEGMENTED_ARGS) if segmented else (FILES[path], 10, ())
    partial_root = request.getfixturevalue('partial_root') if partials == 'separate' else None
    args = (*args, *_partial_args(partial_root))
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
    copy = run / 'Photos' / '2024' / 'a.jpg'
    assert stderr_lines(result.stderr) == [
        f'{path} quarantined: {reason}; kept at {copy.relative_to(dest)}',
        'Error: 1 quarantined; see the list above.',
    ]
    assert copy.read_bytes() == corrupted
    assert copy.stat().st_mtime_ns == _mtime_ns(MODIFIED)
    assert not local_file(dest, path).exists()
    assert _partials(dest) == []
    assert _partials(dest, partial_root) == []
    assert _journal() == []
    assert _failure_records() == []

    earlier = len(disk.requests)
    held = run_download(*args, '--scan-max-age', '0', str(dest))

    assert held.exit_code == 1
    assert f'Downloading 0 files (0 B) to {dest}\n' in held.stdout
    assert '0 downloaded (0 B), 5 skipped, 1 quarantined, 0 failed\n' in held.stdout
    assert _quarantined(run, path, reason) in held.stdout
    assert stderr_lines(held.stderr) == ['Error: 1 quarantined; see the list above.']
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
    assert _partials(dest, partial_root) == []
    assert _partials(dest) == []

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
    assert [failed for failed, *_ in _failure_records()] == [path]

    plain = run_download(str(dest))

    assert plain.exit_code == 1
    assert shown in plain.stdout
    assert f'; kept at .yandex-disk-rescue/quarantine/{run.name}/Docs/readme.txt\n' in plain.stdout
    assert 'Failed' not in plain.stdout
    assert json.loads(run_download('--json', str(dest)).stdout)['quarantined'] == [
        {
            'path': path,
            'reason': reason,
            'last_retry': 'the download host 127.0.0.1 returned HTTP 503',
            'copy': f'.yandex-disk-rescue/quarantine/{run.name}/Docs/readme.txt',
        }
    ]


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
    assert stderr_lines(stopped.stderr) == ['Interrupted; rerun to continue the download.']
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
    *notices, result_line = stderr_lines(result.stderr)
    assert result_line == 'Error: 1 failed and 1 quarantined; see the lists above.'
    quarantined, failed = sorted(notices)
    assert quarantined == (
        f'disk:/Checks/sha.bin quarantined: SHA-256 mismatch (expected {"0" * 64}, got {actual}); '
        f'kept at .yandex-disk-rescue/quarantine/{run.name}/sha.bin'
    )
    assert failed.startswith('disk:/Checks/short.bin failed: the download failed: ')
    assert (dest / 'size-only.bin').read_bytes() == bytes([ord('s') ^ 0xFF]) + b'ize only'
    assert not (dest / 'sha.bin').exists()
    assert (run / 'sha.bin').read_bytes() == content
    assert not (dest / 'short.bin').exists()


def test_json_reports_a_run_with_the_counts_of_its_summary_and_every_failure_and_quarantined_copy(
    disk, dest, tmp_path, monkeypatch
):
    content = b'sha-checked'
    disk.add_file('disk:/Checks/sha.bin', content, md5=None, sha256='0' * 64)
    disk.add_file('disk:/Checks/size-only.bin', b'size only', md5=None, sha256=None)
    disk.add_file('disk:/Checks/short.bin', b'0123456789', md5=None, sha256=None)
    for _ in range(2):
        disk.inject('GET', '/download', Truncate(after_bytes=4), where={'path': 'disk:/Checks/short.bin'})
    args = ('--download-retries', '0', '--download-passes', '1', 'disk:/Checks')
    human = run_download(*args, str(tmp_path / 'human'))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'json-state'))

    result = run_download('--json', *args, str(dest))

    assert human.exit_code == result.exit_code == 1
    document = json.loads(result.stdout)
    head = re.search(r': (\d+) downloaded \((.+)\), (\d+) skipped, (\d+) quarantined, (\d+) failed\n', human.stdout)
    counts = document['counts']
    shown = (counts['downloaded'], format_size(counts['downloaded_size']), counts['skipped'])
    assert shown + (counts['quarantined'], counts['failed']) == (int(head[1]), head[2], int(head[3]), 1, 1)
    assert (int(head[4]), int(head[5])) == (1, 1)
    assert counts == {
        'downloaded': 1,
        'downloaded_size': 9,
        'skipped': 0,
        'adopted': 0,
        'size_only': 1,
        'renamed': 0,
        'unreadable': 0,
        'moved_aside': 0,
        'quarantined': 1,
        'failed': 1,
        'unavailable': 0,
        'preview_only': 0,
        'preview_errors': 0,
        'previews_pending': 0,
    }
    assert (document['preview_only'], document['preview_errors']) == ([], [])
    assert (document['schema_version'], document['command'], document['exit_code']) == (1, 'download', 1)
    assert document['error'] == {'message': '1 failed and 1 quarantined; see the lists above.'}
    assert (document['remote_root'], document['destination'], document['retry_failed']) == (
        'disk:/Checks',
        str(dest),
        False,
    )
    assert document['scan']['reused'] is False
    assert (document['scan']['root'], document['scan']['files']) == ('disk:/Checks', 3)
    assert document['to_download'] == {'files': 3, 'size': len(content) + len(b'size only') + 10}
    assert document['passes'] == {'used': 1, 'limit': 1}
    assert document['size_only'] == ['disk:/Checks/size-only.bin']
    [quarantined] = document['quarantined']
    assert quarantined['path'] == 'disk:/Checks/sha.bin'
    assert quarantined['reason'].startswith('SHA-256 mismatch (expected ')
    assert quarantined['last_retry'] is None
    assert (dest / quarantined['copy']).read_bytes() == content
    [failed] = document['failed']
    assert (failed['path'], failed['unavailable']) == ('disk:/Checks/short.bin', False)
    assert failed['reason'].startswith('the download failed: ')
    assert (document['partial_dir'], document['partial_move'], document['partial_room']) == (None, None, None)
    assert document['sparse_partials'] is True
    assert f'Partial folder {_own_partial_folder(dest)} keeps sparse files: yes' in log_messages()
    assert document['partials_left'] == []
    report = stderr_lines(result.stderr)
    assert report[report.index('Failed: 1') + 1] == f'  disk:/Checks/short.bin: {failed["reason"]}'
    assert report[-1] == 'Error: 1 failed and 1 quarantined; see the lists above.'


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
    assert stderr_lines(stopped.stderr) == [f'Error: Could not write {dest / converted}: {os.strerror(errno.EIO)}.']

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
        session.commit(unencodable=(), unlisted=(), vanished=())
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


README = 'disk:/Docs/readme.txt'


def _untracked_readme(dest) -> tuple[Path, int]:
    """An untracked copy of `README` with the Disk content and an mtime of its own; its path and inode."""
    readme = dest / 'Docs' / 'readme.txt'
    readme.parent.mkdir(parents=True)
    readme.write_bytes(FILES[README])
    os.utime(readme, ns=(10**18, 10**18))
    return readme, readme.stat().st_ino


def _refuse_reads(patch, inode: int, code: int, before: Callable[[], None] = lambda: None) -> list[int]:
    """Make every read of the file with `inode` call `before`, then fail with `code`; the inodes of the reads
    refused."""
    read = localfs.read
    refused: list[int] = []

    def failing(fd, size, offset):
        if os.fstat(fd).st_ino == inode:
            refused.append(inode)
            before()
            raise OSError(code, os.strerror(code))
        return read(fd, size, offset)

    patch.setattr(localfs, 'read', failing)
    return refused


def _findings() -> list[tuple[str, str, str]]:
    """The (remote path, finding, reason) of every finding in the manifest, read with a connection of its own."""
    with closing(sqlite3.connect(f'{manifest_path().as_uri()}?mode=ro', uri=True)) as connection:
        return connection.execute(
            'SELECT e.path, v.finding, v.reason FROM verify_findings v JOIN entries e ON e.id = v.entry_id ORDER BY 1'
        ).fetchall()


@pytest.mark.parametrize(
    ('code', 'output'), [(errno.EIO, 'human'), (errno.EBADMSG, 'json')], ids=['EIO-human', 'EBADMSG-json']
)
def test_an_unreadable_local_copy_is_moved_aside_and_downloaded_again(disk, dest, monkeypatch, code, output):
    readme, inode = _untracked_readme(dest)
    reason = f'could not read Docs/readme.txt: {os.strerror(code)}'
    args = ('--json',) if output == 'json' else ()

    with monkeypatch.context() as patch:
        refused = _refuse_reads(patch, inode, code)
        result = run_download(*args, str(dest))

    assert result.exit_code == 0, result.output
    summary = result.stderr if output == 'json' else result.stdout
    assert f'Mirrored disk:/ to {dest}: 6 downloaded ' in summary
    assert ', 0 skipped, 0 quarantined, 0 failed\n' in summary
    [run] = replaced_runs(dest)
    assert (
        f'Unreadable in DEST, downloaded again: 1\n  {README}: {reason}\n'
        f'Moved aside to .yandex-disk-rescue/replaced/{run.name}/: 1\n  Docs/readme.txt\n'
    ) in summary
    assert f'{README} corrupt: {reason}' in stderr_lines(result.stderr)
    if output == 'json':
        document = json.loads(result.stdout)
        assert (document['exit_code'], document['counts']['unreadable']) == (0, 1)
        assert document['unreadable'] == [{'path': README, 'reason': reason}]
    assert refused == [inode]
    moved = run / 'Docs' / 'readme.txt'
    assert (moved.stat().st_ino, moved.read_bytes()) == (inode, FILES[README])
    assert readme.read_bytes() == FILES[README]
    assert readme.stat().st_ino != inode
    assert _findings() == []


def test_an_unreadable_local_copy_whose_repair_fails_is_not_read_again(disk, dest, monkeypatch):
    readme, inode = _untracked_readme(dest)
    reason = f'could not read Docs/readme.txt: {os.strerror(errno.EIO)}'
    attempts = 2  # one per pass: a 503 before any byte is not retried within the pass
    disk.inject('GET', '/download', *[503] * attempts, where={'path': README})
    args = ('--download-passes', '2', str(dest))

    with monkeypatch.context() as patch:
        refused = _refuse_reads(patch, inode, errno.EIO)
        first = run_download(*args)

        assert first.exit_code == 1
        assert len(_fetches(disk, README)) == attempts
        assert ', 0 quarantined, 1 failed\n' in first.stdout
        assert f'Unreadable in DEST, downloaded again: 1\n  {README}: {reason}\n' in first.stdout
        assert f'Failed: 1\n  {README}: ' in first.stdout
        assert readme.stat().st_ino == inode
        assert not (dest / '.yandex-disk-rescue' / 'replaced').exists()

        checked = runner.invoke(app, ['verify', str(dest)])

        assert checked.exit_code == 1
        assert f'Corrupt: 1\n  {README}: {reason}\n' in checked.stdout

        earlier = len(disk.requests)
        second = run_download(*args)

    assert second.exit_code == 0, second.output
    assert _fetches(disk, README, earlier) == [None]
    assert 'Unreadable in DEST' not in second.stdout
    [run] = replaced_runs(dest)
    assert f'Moved aside to .yandex-disk-rescue/replaced/{run.name}/: 1\n  Docs/readme.txt\n' in second.stdout
    moved = run / 'Docs' / 'readme.txt'
    assert (moved.stat().st_ino, moved.read_bytes()) == (inode, FILES[README])
    assert readme.read_bytes() == FILES[README]
    assert refused == [inode]
    assert _findings() == []


@pytest.mark.parametrize('code', [errno.EIO, errno.ENODEV], ids=errno.errorcode.get)
def test_an_unreadable_local_copy_on_a_disk_that_is_gone_stops_the_run(disk, dest, monkeypatch, code):
    _, inode = _untracked_readme(dest)
    gone = dest.with_name('gone')

    def unplug() -> None:
        if code == errno.EIO:
            dest.rename(gone)

    with monkeypatch.context() as patch:
        _refuse_reads(patch, inode, code, unplug)
        result = run_download(str(dest))

    assert result.exit_code == 1
    assert stderr_lines(result.stderr) == [f'Error: Could not read {dest}/Docs/readme.txt: {os.strerror(code)}.']
    assert 'Mirrored' not in result.stdout
    assert _findings() == []
    found = gone if code == errno.EIO else dest
    assert not (found / '.yandex-disk-rescue' / 'replaced').exists()
    assert (found / 'Docs' / 'readme.txt').stat().st_ino == inode


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


LINK_500 = 'Yandex Disk API returned HTTP 500: Injected HTTP 500 (after 2 attempts)'


def _account_checks(fake) -> int:
    return sum(1 for r in fake.requests if r.path == '/v1/disk/')


def test_a_link_request_failing_for_one_file_defers_only_that_file_and_retry_failed_covers_it(disk, dest):
    readme = 'disk:/Docs/readme.txt'
    disk.inject('GET', '/v1/disk/resources/download', *[500] * 4, where={'path': readme})

    result = run_download('--api-retries', '1', '--download-passes', '2', str(dest))

    assert result.exit_code == 1, result.output
    assert '5 downloaded' in result.stdout
    assert 'Passes: 2 of 2\n' in result.stdout
    assert f'Failed: 1\n  {readme}: {LINK_500} (after 2 passes)\n' in result.stdout
    assert _failure_records() == [(readme, f'{LINK_500} (after 2 passes)', 2)]
    linked = _linked(disk)
    turns = [index for index, path in enumerate(linked) if path == readme]
    assert len(turns) == 4
    assert linked[turns[1] + 1] != readme
    assert turns[2] == turns[1] + 2
    assert linked[turns[3] + 1 :] == [linked[turns[3] + 1]]
    assert linked[-1] != readme
    assert _account_checks(disk) == 1

    again = run_download('--retry-failed', str(dest))

    assert again.exit_code == 0, again.output
    assert '1 downloaded' in again.stdout
    assert _failure_records() == []
    assert local_file(dest, readme).read_bytes() == FILES[readme]


@pytest.mark.parametrize('status', [404, 403])
def test_a_reference_file_the_disk_refuses_still_shows_the_api_answers(disk, dest, status):
    readme = 'disk:/Docs/readme.txt'
    assert run_download(str(dest)).exit_code == 0
    local_file(dest, readme).unlink()
    earlier, accounts = len(disk.requests), _account_checks(disk)
    disk.inject('GET', '/v1/disk/resources/download', 500, 500, status)

    result = run_download('--api-retries', '1', '--download-passes', '1', str(dest))

    assert result.exit_code == 1, result.output
    assert f'Failed: 1\n  {readme}: {LINK_500}\n' in result.stdout
    assert stderr_lines(result.stderr)[-1] == 'Error: 1 failed; see the list above.'
    [reference] = {r.params['path'] for r in disk.requests[earlier:] if r.path == '/v1/disk/resources/download'} - {
        readme
    }
    assert reference in FILES
    assert _account_checks(disk) == accounts + 1


def _assert_stopped_by_an_outage(result) -> None:
    assert result.exit_code == 1, result.output
    error = stderr_lines(result.stderr)[-1]
    assert error.startswith('Error: Could not download disk:/'), error
    assert error.endswith('(after 2 attempts)'), error
    assert 'Mirrored' not in result.stdout


def test_a_link_endpoint_outage_stops_a_run_over_a_mirror_with_verified_files(disk, dest):
    assert run_download(str(dest)).exit_code == 0
    local_file(dest, 'disk:/Docs/readme.txt').unlink()
    local_file(dest, 'disk:/Photos/2024/b.jpg').unlink()
    disk.inject('GET', '/v1/disk/resources/download', *[500] * 50)
    accounts = _account_checks(disk)

    result = run_download('--api-retries', '1', str(dest))

    _assert_stopped_by_an_outage(result)
    assert _account_checks(disk) == accounts + 1
    assert _failure_records() == []


def test_a_disk_api_outage_during_the_first_run_stops_it(disk, dest):
    disk.inject('GET', '/v1/disk/resources/download', *[500] * 50)
    disk.inject('GET', '/v1/disk/', Delay(0), *[500] * 50)

    result = run_download('--api-retries', '1', str(dest))

    _assert_stopped_by_an_outage(result)
    assert _failure_records() == []


def test_a_fresh_mirror_whose_links_all_fail_stops_after_ten_files(disk, dest):
    for index in range(12):
        disk.add_file(f'disk:/Many/{index:02}.txt', b'many', modified=MODIFIED)
    disk.inject('GET', '/v1/disk/resources/download', *[500] * 50)

    result = run_download('--api-retries', '0', '--download-connections', '1', 'disk:/Many', str(dest))

    assert result.exit_code == 1, result.output
    error = stderr_lines(result.stderr)[-1]
    assert error.startswith('Error: Could not download disk:/Many/'), error
    assert error.endswith(
        'Yandex Disk API returned HTTP 500: Injected HTTP 500 '
        '(no file of this mirror got a download link: 10 failed the same way)'
    ), error
    assert 'Mirrored' not in result.stdout
    assert _account_checks(disk) == 1 + mirroring.UNREFERENCED_FAILURES


SOLO = 'disk:/Solo/a.txt'


def _reject_the_token_on_the_link(disk, dest) -> None:
    disk.inject('GET', '/v1/disk/resources/download', 401)


def _reject_the_token_on_the_account_probe(disk, dest) -> None:
    disk.inject('GET', '/v1/disk/resources/download', 500, where={'path': SOLO})
    disk.inject('GET', '/v1/disk/', Delay(0), 401)


def _reject_the_token_on_the_reference_link_probe(disk, dest) -> None:
    reference = 'disk:/Solo/b.txt'
    disk.add_file(reference, b'reference')
    assert run_download('disk:/Solo', str(dest)).exit_code == 0
    (dest / 'a.txt').unlink()
    disk.inject('GET', '/v1/disk/resources/download', 500, where={'path': SOLO})
    disk.inject('GET', '/v1/disk/resources/download', 401, where={'path': reference})


@pytest.mark.parametrize(
    'reject',
    [
        _reject_the_token_on_the_link,
        _reject_the_token_on_the_account_probe,
        _reject_the_token_on_the_reference_link_probe,
    ],
)
def test_a_token_rejected_on_a_link_request_stops_the_run(disk, dest, reject):
    disk.add_file(SOLO, b'solo')
    reject(disk, dest)

    result = run_download('--api-retries', '0', 'disk:/Solo', str(dest))

    assert result.exit_code == 1
    [error] = stderr_lines(result.stderr)
    assert error.startswith('Error: Yandex rejected the token (invalid, expired or revoked).')
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
    *notices, result_line = stderr_lines(result.stderr)
    assert sorted(notices) == [
        'disk:/Bad\\udc81 failed: the folder name is not valid Unicode, so the Disk API cannot list it or download '
        'anything in it',
        'disk:/Bad\\udc81 left out: the name is not valid Unicode',
        'disk:/Docs/bad\\udc80.txt failed: the name is not valid Unicode, so the Disk API cannot download it',
        'disk:/Docs/bad\\udc80.txt left out: the name is not valid Unicode',
    ]
    assert result_line == 'Error: 2 failed; see the list above.'
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


def test_an_unusable_destination_or_partial_folder_is_a_usage_error_that_creates_nothing(
    disk, tmp_path, partial_root, monkeypatch
):
    monkeypatch.setenv('COLUMNS', '300')
    regular = tmp_path / 'file'
    regular.write_bytes(b'')
    orphan = tmp_path / 'missing' / 'dest'
    new, existing = tmp_path / 'x', tmp_path / 'existing'
    (existing / 'inside').mkdir(parents=True)
    (tmp_path / 'link').symlink_to(existing / 'inside')

    def not_a_folder(raw) -> str:
        return f'Error: The partial folder {raw} is not an existing folder; download creates only its own folder inside it.\n'

    def inside(raw, dest) -> str:
        return f'Error: The partial folder {raw} is inside the destination {dest}; choose a folder outside it.\n'

    cases = [
        ([str(regular)], f'Error: Destination {regular} exists and is not a folder.\n'),
        (
            [str(orphan)],
            f'Error: The parent folder of {orphan} does not exist; download creates only the last folder of the '
            'destination.\n',
        ),
        ([str(new), 'disk:/A'], 'disk:/A is a Disk path'),
        (['--partial-dir', str(partial_root), str(orphan)], 'Error: The parent folder of '),
        (['--partial-dir', 'relative', str(new)], 'must be an absolute path, or empty to keep partial files in DEST.'),
        (['--partial-dir', str(tmp_path / 'nope'), str(new)], not_a_folder(tmp_path / 'nope')),
        (['--partial-dir', str(regular), str(new)], not_a_folder(regular)),
        (['--partial-dir', str(existing), str(existing)], inside(existing, existing)),
        (['--partial-dir', str(existing / 'inside'), str(existing)], inside(existing / 'inside', existing)),
        (['--partial-dir', str(tmp_path / 'link'), str(existing)], inside(tmp_path / 'link', existing)),
    ]
    for args, message in cases:
        result = run_download(*args)

        assert result.exit_code == 2, args
        assert message in result.stderr, args

    monkeypatch.setenv('YANDEX_DISK_RESCUE_PARTIAL_DIR', 'relative')
    from_env = run_download(str(new))

    assert from_env.exit_code == 2
    assert 'must be an absolute path, or empty to keep partial files in DEST.' in from_env.stderr

    assert not orphan.parent.exists()
    assert not new.exists()
    assert [p.name for p in existing.rglob('*')] == ['inside']
    assert list(partial_root.iterdir()) == []
    assert not manifest_path().exists()
    assert not log_path().exists()
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


@pytest.mark.parametrize('partials', ['in-dest', 'separate'])
@pytest.mark.parametrize('code', [errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS], ids=errno.errorcode.get)
def test_a_full_or_failing_destination_stops_the_run_and_a_rerun_goes_on(
    segmented, dest, monkeypatch, request, code, partials
):
    """In DEST the placing of `top.txt` fails; with a partial folder a write to the partial file of `SEGMENTED` past
    its first checkpoint does, and the rerun resumes it from that checkpoint."""
    partial_root = request.getfixturevalue('partial_root') if partials == 'separate' else None
    args = ('--download-checkpoint-mib', '1', *_partial_args(partial_root))
    rename_noreplace, pwrite = localfs.rename_noreplace, os.pwrite
    refused: list[str] = []

    def failing_rename(src, dst, *, src_dir_fd, dst_dir_fd):
        if dst == 'top.txt':
            refused.append(str(dest / dst))
            raise OSError(code, os.strerror(code), dst)
        rename_noreplace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    def failing_write(fd, data, offset):
        path = os.readlink(f'/proc/self/fd/{fd}')
        if _below(path, partial_root) and offset >= MiB + MiB // 2:
            refused.append(path)
            raise OSError(code, os.strerror(code))
        return pwrite(fd, data, offset)

    with monkeypatch.context() as patch:
        if partial_root is None:
            patch.setattr(localfs, 'rename_noreplace', failing_rename)
        else:
            patch.setattr(os, 'pwrite', failing_write)
        result = run_download(*args, str(dest))

    assert result.exit_code == 1
    [path] = set(refused)
    assert stderr_lines(result.stderr) == [f'Error: Could not write {path}: {os.strerror(code)}.']
    assert 'Mirrored' not in result.stdout
    if partial_root is None:
        assert not (dest / 'top.txt').exists()
    else:
        assert path in [str(p) for p in _partials(dest, partial_root)]
        assert not local_file(dest, SEGMENTED_PATH).exists()
        assert MiB <= _confirmed() < MiB + MiB // 2

    confirmed, earlier = _confirmed(), len(segmented.requests)
    rerun = run_download(*args, str(dest))

    assert rerun.exit_code == 0, rerun.output
    counts = re.search(
        rf'Mirrored disk:/ to {re.escape(str(dest))}: (\d+) downloaded \(.+\), (\d+) skipped, 0 quarantined, 0 failed\n',
        rerun.stdout,
    )
    assert counts is not None, rerun.stdout
    assert int(counts[1]) + int(counts[2]) == len(FILES) + 1
    assert _others_verified(dest, failed='')
    assert local_file(dest, SEGMENTED_PATH).read_bytes() == SEGMENTED
    if partial_root is not None:
        assert _fetches(segmented, SEGMENTED_PATH, earlier) == [f'bytes={confirmed}-{len(SEGMENTED) - 1}']
        assert _partials(dest, partial_root) == []


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
    assert stderr_lines(result.stderr) in [
        [f'Error: Could not write {partial}: {os.strerror(code)}.'] for partial in partials
    ]
    assert 'Mirrored' not in result.stdout


@pytest.mark.parametrize(
    'obstacle', ['mtime-not-permitted', 'unreadable', 'untraversable', 'EMFILE', 'ENFILE', 'ENOMEM']
)
def test_a_copy_out_of_reach_fails_that_file_only(disk, dest, monkeypatch, obstacle):
    readme, local = 'disk:/Docs/readme.txt', dest / 'Docs' / 'readme.txt'
    if obstacle == 'untraversable':
        assert run_download(str(dest)).exit_code == 0
        _refuse(monkeypatch, 'lstat', errno.EACCES, lambda name: name in {'a.jpg', 'b.jpg'})
        denied = os.strerror(errno.EACCES)
        failed = [
            (f'disk:/Photos/2024/{name}', f'could not inspect Photos/2024/{name}: {denied}')
            for name in ('a.jpg', 'b.jpg')
        ]
    else:
        local.parent.mkdir(parents=True)
        local.write_bytes(FILES[readme])
        if obstacle != 'mtime-not-permitted':
            code = errno.EACCES if obstacle == 'unreadable' else getattr(errno, obstacle)
            _refuse(monkeypatch, 'open_file', code, lambda name: name == 'readme.txt')
            failed = [(readme, f'could not read Docs/readme.txt: {os.strerror(code)}')]
        else:
            inode = local.stat().st_ino
            _refuse(monkeypatch, 'set_times', errno.EPERM, lambda fd: os.fstat(fd).st_ino == inode)
            failed = [(readme, f'could not set the modification time of Docs/readme.txt: {os.strerror(errno.EPERM)}')]

    result = run_download(str(dest))

    assert result.exit_code == 1
    listed = ''.join(f'  {path}: {reason}\n' for path, reason in failed)
    assert f', {len(failed)} failed\n' in result.stdout
    assert f'Failed: {len(failed)}\n{listed}' in result.stdout
    *notices, result_line = stderr_lines(result.stderr)
    assert sorted(notices) == [f'{path} failed: {reason}' for path, reason in failed]
    assert result_line == f'Error: {len(failed)} failed; see the list above.'
    assert _others_verified(dest, failed='')
    assert not (dest / '.yandex-disk-rescue' / 'replaced').exists()
    assert _findings() == []
    if obstacle != 'untraversable':
        assert local.stat().st_mtime_ns != _mtime_ns(MODIFIED)


@pytest.mark.parametrize('copy', ['untracked', 'verified'])
def test_an_io_error_inspecting_a_path_of_the_mirror_stops_the_run_naming_it(disk, dest, monkeypatch, copy):
    local = dest / 'Docs' / 'readme.txt'
    if copy == 'verified':
        assert run_download(str(dest)).exit_code == 0
    else:
        local.parent.mkdir(parents=True)
        local.write_bytes(FILES['disk:/Docs/readme.txt'])
    _refuse(monkeypatch, 'lstat', errno.EIO, lambda name: name == 'readme.txt')

    result = run_download(str(dest))

    assert result.exit_code == 1
    assert stderr_lines(result.stderr) == [f'Error: Could not inspect {local}: {os.strerror(errno.EIO)}.']
    assert 'Mirrored' not in result.stdout
    assert local.read_bytes() == FILES['disk:/Docs/readme.txt']
    assert not (dest / '.yandex-disk-rescue' / 'replaced').exists()


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


def _content_order(fake, since: int = 0) -> list[str]:
    """The remote path of each content request after the first `since` requests, in the order they started."""
    return [r.params['path'] for r in fake.requests[since:] if r.path == '/download']


def test_files_failing_in_a_pass_are_tried_again_only_after_every_healthy_file_finished(disk, dest):
    failing = [f'disk:/Mixed/a-bad{n}.bin' for n in range(3)]
    healthy = [f'disk:/Mixed/b-good{n}.bin' for n in range(8)]
    for path in failing + healthy:
        disk.add_file(path, random.Random(path).randbytes(4 * 64 * 1024))
    for path in failing:
        disk.inject('GET', '/download', 503, 503, where={'path': path})
    disk.chunk_delay = 0.01

    result = run_download('--download-connections', '4', '--download-passes', '2', 'disk:/Mixed', str(dest))

    assert result.exit_code == 1
    assert 'Passes: 2 of 2\nFailed: 3\n' in result.stdout
    content = [r for r in disk.requests if r.path == '/download']
    assert all(_fetches(disk, path) == [None, None] for path in failing)
    second_tries = [[r for r in content if r.params['path'] == path][1].started for path in failing]
    healthy_ends = [r.finished for r in content if r.params['path'] in healthy]
    assert len(healthy_ends) == 8
    assert max(healthy_ends) < min(second_tries)


def test_a_rerun_starts_files_never_failed_first_then_the_failed_ones_fewest_failures_first(disk, dest):
    args = ('--download-connections', '1', '--download-passes', '1', '--scan-max-age', '0', 'disk:/M', str(dest))
    disk.add_file('disk:/M/a-x.bin', b'x' * 1000)
    disk.inject('GET', '/download', 503, where={'path': 'disk:/M/a-x.bin'})
    assert run_download(*args).exit_code == 1
    first_log = log_messages()
    disk.add_file('disk:/M/c-y.bin', b'y' * 1000)
    for path in ('disk:/M/a-x.bin', 'disk:/M/c-y.bin'):
        disk.inject('GET', '/download', 503, 503, where={'path': path})
    assert run_download(*args).exit_code == 1
    disk.add_file('disk:/M/d-z.bin', b'z' * 1000)
    logged = len(log_messages())
    earlier = len(disk.requests)

    result = run_download(*args)

    assert result.exit_code == 1
    assert _content_order(disk, earlier) == ['disk:/M/d-z.bin', 'disk:/M/c-y.bin', 'disk:/M/a-x.bin']
    assert [(path, turns) for path, _, turns in _failure_records()] == [('disk:/M/a-x.bin', 3), ('disk:/M/c-y.bin', 2)]
    assert (dest / 'd-z.bin').read_bytes() == b'z' * 1000
    assert f'Pass 1 of 1 over 3 files ({format_size(3000)}) starts; 2 that failed before' in (log_messages()[logged:])
    assert f'Pass 1 of 1 over 1 file ({format_size(1000)}) starts' in first_log


def test_a_failed_partial_download_resumes_from_its_journal_after_the_untried_files(big, dest):
    _leave_partial(big, dest)
    big.add_file('disk:/Big/c-new.bin', b'new' * 1000, modified=MODIFIED)
    earlier = len(big.requests)

    result = run_download('--download-connections', '1', '--scan-max-age', '0', 'disk:/Big', str(dest))

    assert result.exit_code == 0, result.output
    assert _content_order(big, earlier) == ['disk:/Big/c-new.bin', BIG_PATH]
    assert _fetches(big, BIG_PATH, earlier) == [_tail(300_000)]
    assert (dest / 'big.bin').read_bytes() == BIG
    assert (dest / 'c-new.bin').read_bytes() == b'new' * 1000
    assert _failure_records() == []


def test_retry_failed_starts_the_files_with_no_failure_record_first(disk, dest):
    failed, missing = 'disk:/Docs/readme.txt', 'disk:/Photos/2024/a.jpg'
    disk.inject('GET', '/download', 503, where={'path': failed})
    assert run_download('--download-passes', '1', str(dest)).exit_code == 1
    local_file(dest, missing).unlink()
    checked = runner.invoke(app, ['verify', str(dest)])
    assert checked.exit_code == 1
    assert [path for path, *_ in _failure_records()] == [failed]
    earlier = len(disk.requests)

    retry = run_download('--retry-failed', '--download-connections', '1', str(dest))

    assert retry.exit_code == 0, retry.output
    assert _content_order(disk, earlier) == [missing, failed]
    assert local_file(dest, missing).read_bytes() == FILES[missing]
    assert local_file(dest, failed).read_bytes() == FILES[failed]


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
    assert _failure_records() == []


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
    stalled = f'the download stalled: no data for {STALL_TIMEOUT} s (after 2 passes)'
    assert _failure_records() == [(BIG_PATH, stalled, 2)]
    big.inject('GET', '/download', STALL, STALL, where={'path': BIG_PATH})

    again = run_download('--download-timeout', STALL_TIMEOUT, '--download-passes', '2', str(dest))

    assert again.exit_code == 1
    assert _fetches(big, BIG_PATH) == [None, None, None, None]
    assert _failure_records() == [(BIG_PATH, stalled, 4)]


DEAD_PATH = 'disk:/Dead/napkin.jpg'
DEAD = b'napkin' * 1000
SERVICE_UNAVAILABLE = 'the download host 127.0.0.1 returned HTTP 503'
NEVER_STARTED = 'Yandex Disk never started sending this file: no data in its last'
UNAVAILABLE_HINT = (
    '1 of them never got data from Yandex Disk for a day or more: open them in the Yandex Disk web interface, and ask '
    'Yandex support about those that do not open there either.'
)


@pytest.fixture
def dead(disk):
    """The `disk` tree plus `DEAD` at `DEAD_PATH`."""
    disk.add_file(DEAD_PATH, DEAD, modified=MODIFIED)
    return disk


def _silent_run(fake, dest, path: str, turns: int, *args: str, root: str = 'disk:/'):
    """A run of `root` whose download host answers 503 on each of the `turns` passes it gets for `path`."""
    fake.inject('GET', '/download', *[503] * turns, where={'path': path})
    return run_download('--download-passes', str(turns), *args, root, str(dest))


def _since(moment: datetime) -> str:
    return f'since {moment.astimezone(UTC):%Y-%m-%d %H:%M} UTC, each with a new download link'


def _unavailable_run(fake, dest, *args: str):
    """Seven silent turns of `DEAD_PATH` in one run, aged by a day, then one more silent turn with `--retry-failed`:
    the result of that last run, with `args` added, and the start of the streak."""
    assert _silent_run(fake, dest, DEAD_PATH, 7).exit_code == 1
    aged = age_silence(DEAD_PATH, 25)
    return _silent_run(fake, dest, DEAD_PATH, 1, '--retry-failed', *args), aged


def test_seven_silent_turns_in_one_run_are_not_yet_unavailable(dead, dest):
    started = datetime.now(UTC)

    result = _silent_run(dead, dest, DEAD_PATH, 7)

    assert result.exit_code == 1
    assert f'Failed: 1\n  {DEAD_PATH}: {SERVICE_UNAVAILABLE} (after 7 passes)\n' in result.stdout
    assert 'Yandex Disk' not in result.stdout
    assert len(_fetches(dead, DEAD_PATH)) == 7
    reason, turns, silent_turns, since = failure_record(DEAD_PATH)
    assert (reason, turns, silent_turns) == (f'{SERVICE_UNAVAILABLE} (after 7 passes)', 7, 7)
    assert since is not None
    assert started <= since <= datetime.now(UTC)


def test_a_file_silent_for_a_day_fails_as_unavailable_and_the_others_succeed(dead, dest):
    result, aged = _unavailable_run(dead, dest)

    assert result.exit_code == 1
    assert ', 0 quarantined, 1 failed\n' in result.stdout
    reason = f'{NEVER_STARTED} 8 attempts {_since(aged)} (the last: {SERVICE_UNAVAILABLE})'
    assert f'Failed: 1\n  {DEAD_PATH}: {reason}\n{UNAVAILABLE_HINT}\n' in result.stdout
    assert stderr_lines(result.stderr) == [f'{DEAD_PATH} failed: {reason}', 'Error: 1 failed; see the list above.']
    assert failure_record(DEAD_PATH) == (reason, 8, 8, aged)
    assert _others_verified(dest, DEAD_PATH)


def test_json_flags_a_file_unavailable_on_yandex_disk(dead, dest):
    result, aged = _unavailable_run(dead, dest, '--json')

    assert result.exit_code == 1
    document = json.loads(result.stdout)
    assert (document['counts']['failed'], document['counts']['unavailable']) == (1, 1)
    reason = f'{NEVER_STARTED} 8 attempts {_since(aged)} (the last: {SERVICE_UNAVAILABLE})'
    assert document['failed'] == [{'path': DEAD_PATH, 'reason': reason, 'unavailable': True}]


def test_any_byte_ends_the_silent_streak(dead, dest):
    assert _unavailable_run(dead, dest)[0].exit_code == 1
    dead.inject('GET', '/download', Truncate(after_bytes=4), where={'path': DEAD_PATH})

    result = run_download('--retry-failed', '--download-retries', '0', '--download-passes', '1', str(dest))

    assert result.exit_code == 1
    truncated = (
        'the download failed: peer closed connection without sending complete message body '
        f'(received 4 bytes, expected {len(DEAD)})'
    )
    assert f'Failed: 1\n  {DEAD_PATH}: {truncated}\n' in result.stdout
    assert 'Yandex Disk' not in result.stdout
    assert failure_record(DEAD_PATH) == (truncated, 9, 0, None)


@pytest.mark.parametrize(
    ('answer', 'throttled'),
    [
        (429, 'the download host 127.0.0.1 returned HTTP 429'),
        (FakeResponse.error(503, {'Retry-After': '0'}), SERVICE_UNAVAILABLE),
    ],
    ids=['429', '503-retry-after'],
)
def test_throttling_is_not_silence(dead, dest, answer, throttled):
    def throttled_run(passes: int, *args: str):
        dead.inject('GET', '/download', *[answer] * passes, where={'path': DEAD_PATH})
        result = run_download('--download-passes', str(passes), *args, str(dest))
        assert result.exit_code == 1
        return result

    throttled_run(7)
    assert failure_record(DEAD_PATH) == (f'{throttled} (after 7 passes)', 7, 0, None)
    assert _silent_run(dead, dest, DEAD_PATH, 7).exit_code == 1
    *_, since = failure_record(DEAD_PATH)

    again = throttled_run(7)

    assert f'Failed: 1\n  {DEAD_PATH}: {throttled} (after 7 passes)\n' in again.stdout
    assert failure_record(DEAD_PATH) == (f'{throttled} (after 7 passes)', 21, 7, since)
    aged = age_silence(DEAD_PATH, 25)

    dead.inject('GET', '/download', 429, answer, where={'path': DEAD_PATH})

    late = run_download('--download-passes', '2', '--retry-failed', str(dest))

    assert late.exit_code == 1
    reason = f'{NEVER_STARTED} 7 attempts {_since(aged)} (the last: {throttled})'
    assert f'Failed: 1\n  {DEAD_PATH}: {reason}\n{UNAVAILABLE_HINT}\n' in late.stdout
    assert failure_record(DEAD_PATH) == (reason, 23, 7, aged)


def test_a_file_that_stops_sending_midway_is_reported_as_stopped_after_its_confirmed_bytes(big, dest):
    _leave_partial(big, dest)
    assert _silent_run(big, dest, BIG_PATH, 7, root='disk:/Big').exit_code == 1
    assert failure_record(BIG_PATH)[1:3] == (8, 7)
    aged = age_silence(BIG_PATH, 25)

    result = _silent_run(big, dest, BIG_PATH, 1, '--retry-failed', root='disk:/Big')

    assert result.exit_code == 1
    reason = (
        f'Yandex Disk stopped sending this file after {format_size(300_000)}: no data in its last 8 attempts '
        f'{_since(aged)} (the last: {SERVICE_UNAVAILABLE})'
    )
    assert f'Failed: 1\n  {BIG_PATH}: {reason}\n{UNAVAILABLE_HINT}\n' in result.stdout
    assert failure_record(BIG_PATH) == (reason, 9, 8, aged)
    assert _partial_size(dest) == 300_000


def test_a_local_error_ends_the_silent_streak(dead, dest, monkeypatch):
    assert _silent_run(dead, dest, DEAD_PATH, 7).exit_code == 1
    age_silence(DEAD_PATH, 25)
    local = local_file(dest, DEAD_PATH)
    local.write_bytes(DEAD)
    _refuse(monkeypatch, 'open_file', errno.EACCES, lambda name: name == local.name)

    result = run_download('--retry-failed', '--download-passes', '1', str(dest))

    assert result.exit_code == 1
    denied = f'could not read Dead/napkin.jpg: {os.strerror(errno.EACCES)}'
    assert f'Failed: 1\n  {DEAD_PATH}: {denied}\n' in result.stdout
    assert 'Yandex Disk' not in result.stdout
    assert failure_record(DEAD_PATH) == (denied, 8, 0, None)


def test_a_plain_rerun_tries_an_unavailable_file_once_and_fails_it_at_once(dead, dest):
    assert _unavailable_run(dead, dest)[0].exit_code == 1
    *_, aged = failure_record(DEAD_PATH)
    dead.inject('GET', '/download', *[503] * 7, where={'path': DEAD_PATH})
    earlier, logged = len(dead.requests), len(log_messages())

    result = run_download('--download-passes', '7', str(dest))

    assert result.exit_code == 1
    assert len(_fetches(dead, DEAD_PATH, earlier)) == 1
    assert 'Passes:' not in result.stdout
    reason = f'{NEVER_STARTED} 9 attempts {_since(aged)} (the last: {SERVICE_UNAVAILABLE})'
    assert f'Failed: 1\n  {DEAD_PATH}: {reason}\n{UNAVAILABLE_HINT}\n' in result.stdout
    assert failure_record(DEAD_PATH) == (reason, 9, 9, aged)
    [pass_one] = [message for message in log_messages()[logged:] if message.startswith('Pass 1 of 7 ')]
    assert pass_one.endswith('; 1 that failed before; 1 unavailable on Yandex Disk get one try')


@pytest.mark.parametrize(
    ('passes', 'shown'),
    [('10', 'Passes: 6 of 10'), ('0', 'Passes: 6 of unlimited')],
    ids=['ten_passes', 'unlimited_passes'],
)
def test_a_file_becoming_unavailable_mid_run_gets_no_more_passes(dead, dest, passes, shown):
    assert _silent_run(dead, dest, DEAD_PATH, 1).exit_code == 1
    aged = age_silence(DEAD_PATH, 25)
    dead.inject('GET', '/download', *[503] * 20, where={'path': DEAD_PATH})
    earlier = len(dead.requests)

    result = run_download('--download-passes', passes, str(dest))

    assert result.exit_code == 1
    assert len(_fetches(dead, DEAD_PATH, earlier)) == 6
    assert f'{shown}\n' in result.stdout
    reason = f'{NEVER_STARTED} 7 attempts {_since(aged)} (the last: {SERVICE_UNAVAILABLE})'
    assert f'Failed: 1\n  {DEAD_PATH}: {reason}\n{UNAVAILABLE_HINT}\n' in result.stdout
    assert failure_record(DEAD_PATH) == (reason, 7, 7, aged)
    assert _others_verified(dest, DEAD_PATH)


def test_a_throttled_turn_of_an_unavailable_file_defers_it_to_the_next_pass(dead, dest):
    assert _unavailable_run(dead, dest)[0].exit_code == 1
    *_, aged = failure_record(DEAD_PATH)
    dead.inject('GET', '/download', 429, 503, 503, where={'path': DEAD_PATH})
    earlier = len(dead.requests)

    result = run_download('--download-passes', '7', str(dest))

    assert result.exit_code == 1
    assert len(_fetches(dead, DEAD_PATH, earlier)) == 2
    assert 'Passes: 2 of 7\n' in result.stdout
    reason = f'{NEVER_STARTED} 9 attempts {_since(aged)} (the last: {SERVICE_UNAVAILABLE})'
    assert f'Failed: 1\n  {DEAD_PATH}: {reason}\n' in result.stdout
    assert failure_record(DEAD_PATH) == (reason, 10, 9, aged)


def test_a_link_failure_of_an_unavailable_file_defers_it_and_leaves_its_streak(dead, dest):
    assert _unavailable_run(dead, dest)[0].exit_code == 1
    *_, aged = failure_record(DEAD_PATH)
    dead.inject('GET', '/v1/disk/resources/download', 500, 500, where={'path': DEAD_PATH})
    dead.inject('GET', '/download', 503, where={'path': DEAD_PATH})
    earlier = len(dead.requests)

    result = run_download('--api-retries', '1', '--download-passes', '7', str(dest))

    assert result.exit_code == 1
    assert len(_fetches(dead, DEAD_PATH, earlier)) == 1
    assert 'Passes: 2 of 7\n' in result.stdout
    reason = f'{NEVER_STARTED} 9 attempts {_since(aged)} (the last: {SERVICE_UNAVAILABLE})'
    assert f'Failed: 1\n  {DEAD_PATH}: {reason}\n' in result.stdout
    assert failure_record(DEAD_PATH) == (reason, 10, 9, aged)


def test_retry_failed_still_gives_an_unavailable_file_every_pass(dead, dest):
    assert _unavailable_run(dead, dest)[0].exit_code == 1
    *_, aged = failure_record(DEAD_PATH)
    dead.inject('GET', '/download', *[503] * 3, where={'path': DEAD_PATH})
    earlier = len(dead.requests)

    result = run_download('--retry-failed', '--download-passes', '3', str(dest))

    assert result.exit_code == 1
    assert len(_fetches(dead, DEAD_PATH, earlier)) == 3
    assert 'Passes: 3 of 3\n' in result.stdout
    reason = f'{NEVER_STARTED} 11 attempts {_since(aged)} (the last: {SERVICE_UNAVAILABLE})'
    assert f'Failed: 1\n  {DEAD_PATH}: {reason}\n' in result.stdout
    assert failure_record(DEAD_PATH) == (reason, 11, 11, aged)


def test_a_file_yandex_restores_is_fetched_by_the_next_plain_run(dead, dest):
    assert _unavailable_run(dead, dest)[0].exit_code == 1

    result = run_download(str(dest))

    assert result.exit_code == 0, result.output
    assert local_file(dest, DEAD_PATH).read_bytes() == DEAD
    assert _others_verified(dest, DEAD_PATH)
    assert _failure_records() == []


PHOTO_PREVIEW = b'\xff\xd8\xff\xe0 the napkin, reduced'
PREVIEW_FILE = '.yandex-disk-rescue/previews/Dead/napkin.jpg.preview.jpg'
SAVED_ONLY = 'Saved only as a preview (a reduced image, not verified)'


@pytest.fixture
def photo(disk):
    """The `disk` tree plus `DEAD_PATH` with a preview."""
    disk.add_file(DEAD_PATH, DEAD, modified=MODIFIED, preview=PHOTO_PREVIEW)
    return disk


def _failing_run(fake, dest, *args: str):
    """A run with one pass whose download host answers 503 for `DEAD_PATH`."""
    fake.inject('GET', '/download', 503, where={'path': DEAD_PATH})
    return run_download('--download-passes', '1', *args, str(dest))


def _second_failed_photo(fake) -> str:
    """Add a second photo with a preview whose download host answers 503, and return its path."""
    second = 'disk:/Dead/second.jpg'
    fake.add_file(second, b'second', modified=MODIFIED, preview=PHOTO_PREVIEW)
    fake.inject('GET', '/download', 503, where={'path': second})
    return second


def _preview_requests(fake, since: int = 0) -> list[str]:
    """The preview requests after the first `since`: `meta <path>` for a resource request with `preview_size`,
    `fetch <path>` for a request to the preview host."""
    return [
        f'{"fetch" if r.path == "/preview" else "meta"} {r.params["path"]}'
        for r in fake.requests[since:]
        if r.path == '/preview' or (r.path == '/v1/disk/resources' and 'preview_size' in r.params)
    ]


def _preview_records() -> list[tuple[str, str, int, str, str]]:
    """The (remote path, preview path, bytes, content type, size name) of every preview record in the manifest."""
    with closing(sqlite3.connect(f'{manifest_path().as_uri()}?mode=ro', uri=True)) as connection:
        return connection.execute(
            'SELECT e.path, p.path, p.bytes, p.content_type, p.size_name FROM previews p'
            ' JOIN entries e ON e.id = p.entry_id ORDER BY e.path'
        ).fetchall()


def _mentions_previews(stdout: str) -> bool:
    return any(
        line in stdout
        for line in ('Saving the previews', SAVED_ONLY, 'Previews not saved or deleted', 'Previews interr')
    )


def _previews_folder(dest) -> Path:
    return dest / '.yandex-disk-rescue' / 'previews'


def test_a_failed_photo_gets_its_preview_saved_apart_and_the_summary_says_so(photo, dest, tmp_path, monkeypatch):
    result = _failing_run(photo, dest)

    assert result.exit_code == 1
    assert f'Saving the previews of 1 failed file to {_previews_folder(dest)}\n' in result.stdout
    assert (
        f'Failed: 1\n  {DEAD_PATH}: {SERVICE_UNAVAILABLE}\n{SAVED_ONLY}: 1\n  {DEAD_PATH} -> {PREVIEW_FILE}\n'
    ) in result.stdout
    assert 'Previews not saved or deleted' not in result.stdout
    assert (dest / PREVIEW_FILE).read_bytes() == PHOTO_PREVIEW
    assert not local_file(dest, DEAD_PATH).exists()
    assert _others_verified(dest, DEAD_PATH)
    assert _preview_records() == [(DEAD_PATH, PREVIEW_FILE, len(PHOTO_PREVIEW), 'image/jpeg', 'XXXL')]
    assert _preview_requests(photo) == [f'meta {DEAD_PATH}', f'fetch {DEAD_PATH}']
    [meta] = [r for r in photo.requests if 'preview_size' in r.params]
    assert meta.params['preview_size'] == 'XXXL'
    [fetched] = [r for r in photo.requests if r.path == '/preview']
    assert fetched.headers['authorization'] == f'OAuth {TOKEN}'
    assert stderr_lines(result.stderr)[-1] == 'Error: 1 failed; see the list above.'
    assert f'{DEAD_PATH}: preview saved at {PREVIEW_FILE} ({format_size(len(PHOTO_PREVIEW))})' in log_messages()
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'json-state'))

    twin = _failing_run(photo, tmp_path / 'twin', '--json')

    assert twin.exit_code == 1
    document = json.loads(twin.stdout)
    counts = document['counts']
    assert (counts['failed'], counts['preview_only'], counts['preview_errors'], counts['previews_pending']) == (
        1,
        1,
        0,
        0,
    )
    assert document['preview_only'] == [{'path': DEAD_PATH, 'preview': PREVIEW_FILE, 'bytes': len(PHOTO_PREVIEW)}]
    assert document['preview_errors'] == []
    assert document['failed'] == [{'path': DEAD_PATH, 'reason': SERVICE_UNAVAILABLE, 'unavailable': False}]


def test_a_file_whose_link_request_keeps_failing_gets_its_preview(photo, dest):
    photo.inject('GET', '/v1/disk/resources/download', *[500] * 10, where={'path': DEAD_PATH})

    result = run_download('--api-retries', '1', '--download-passes', '1', str(dest))

    assert result.exit_code == 1
    assert f'Failed: 1\n  {DEAD_PATH}: {LINK_500}\n{SAVED_ONLY}: 1\n  {DEAD_PATH} -> {PREVIEW_FILE}\n' in result.stdout
    assert (dest / PREVIEW_FILE).read_bytes() == PHOTO_PREVIEW


def test_a_failed_file_without_a_preview_ends_failed_with_no_preview_and_no_error(disk, dest):
    archive = 'disk:/Dead/archive.zip'
    disk.add_file(archive, b'PK\x03\x04 zipped', modified=MODIFIED)
    disk.inject('GET', '/download', 503, where={'path': archive})

    result = run_download('--download-passes', '1', str(dest))

    assert result.exit_code == 1
    assert f'Failed: 1\n  {archive}: {SERVICE_UNAVAILABLE}\nManifest: ' in result.stdout
    assert SAVED_ONLY not in result.stdout
    assert 'Previews not saved or deleted' not in result.stdout
    assert _preview_requests(disk) == [f'meta {archive}']
    assert f'{archive}: no preview on Yandex Disk' in log_messages()
    assert not _previews_folder(dest).exists()
    assert _preview_records() == []


def test_a_preview_already_saved_for_the_current_version_is_not_fetched_again(photo, dest):
    assert _failing_run(photo, dest).exit_code == 1
    earlier = len(photo.requests)

    again = _failing_run(photo, dest)

    assert again.exit_code == 1
    assert _preview_requests(photo, earlier) == []
    assert f'{SAVED_ONLY}: 1\n  {DEAD_PATH} -> {PREVIEW_FILE}\n' in again.stdout
    assert (dest / PREVIEW_FILE).read_bytes() == PHOTO_PREVIEW
    (dest / PREVIEW_FILE).unlink()

    lost = _failing_run(photo, dest)

    assert lost.exit_code == 1
    assert len(_preview_requests(photo, earlier)) == 2
    assert (dest / PREVIEW_FILE).read_bytes() == PHOTO_PREVIEW


def test_a_new_version_that_fails_too_replaces_the_preview_of_the_old_one(photo, dest):
    assert _failing_run(photo, dest).exit_code == 1
    png = b'\x89PNG a newer napkin'
    photo.add_file(DEAD_PATH, DEAD * 2, modified=MODIFIED, preview=png, preview_type='image/png')

    result = _failing_run(photo, dest, '--scan-max-age', '0')

    assert result.exit_code == 1
    newer = PREVIEW_FILE.removesuffix('.jpg') + '.png'
    assert f'{SAVED_ONLY}: 1\n  {DEAD_PATH} -> {newer}\n' in result.stdout
    assert (dest / newer).read_bytes() == png
    assert not (dest / PREVIEW_FILE).exists()
    assert _preview_records() == [(DEAD_PATH, newer, len(png), 'image/png', 'XXXL')]


@pytest.mark.parametrize('retry_failed', [False, True], ids=['plain', 'retry-failed'])
def test_once_the_original_downloads_its_preview_is_deleted(photo, dest, retry_failed):
    assert _failing_run(photo, dest).exit_code == 1
    earlier = len(photo.requests)

    result = run_download(*(['--retry-failed'] if retry_failed else []), str(dest))

    assert result.exit_code == 0, result.output
    assert local_file(dest, DEAD_PATH).read_bytes() == DEAD
    assert not _mentions_previews(result.stdout)
    assert not (dest / PREVIEW_FILE).exists()
    assert list(_previews_folder(dest).iterdir()) == []
    assert _preview_records() == []
    assert _preview_requests(photo, earlier) == []
    assert f'{DEAD_PATH}: its preview {PREVIEW_FILE} deleted' in log_messages()


def test_no_previews_still_deletes_the_preview_of_a_file_now_downloaded(photo, dest, tmp_path, monkeypatch):
    assert _failing_run(photo, dest).exit_code == 1
    _second_failed_photo(photo)
    earlier = len(photo.requests)

    kept = _failing_run(photo, dest, '--no-previews', '--scan-max-age', '0', '--json')

    assert kept.exit_code == 1
    document = json.loads(kept.stdout)
    assert document['previews'] is False
    assert document['preview_only'] == [{'path': DEAD_PATH, 'preview': PREVIEW_FILE, 'bytes': len(PHOTO_PREVIEW)}]
    assert (document['counts']['failed'], document['preview_errors']) == (2, [])
    assert _preview_requests(photo, earlier) == []
    assert 'Saving the previews' not in kept.stderr
    assert (dest / PREVIEW_FILE).read_bytes() == PHOTO_PREVIEW

    result = run_download('--no-previews', str(dest))

    assert result.exit_code == 0, result.output
    assert local_file(dest, DEAD_PATH).read_bytes() == DEAD
    assert not _mentions_previews(result.stdout)
    assert list(_previews_folder(dest).iterdir()) == []
    assert _preview_records() == []
    assert _preview_requests(photo, earlier) == []
    assert f'{DEAD_PATH}: its preview {PREVIEW_FILE} deleted' in log_messages()
    assert json.loads(_failing_run(photo, tmp_path / 'twin', '--json').stdout)['previews'] is True


def test_a_preview_left_unfinished_by_a_crash_is_deleted_by_the_next_run(photo, dest, tmp_path, monkeypatch):
    assert _failing_run(photo, dest).exit_code == 1
    preview = dest / PREVIEW_FILE
    unfinished = [preview.parent / '0123456789abcdef.preview-saving', _previews_folder(dest) / 'f.preview-saving']
    for path in unfinished:
        path.write_bytes(b'half a preview')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'kept.preview-saving').write_bytes(b'not ours')
    (preview.parent / 'linked').symlink_to(outside)
    (preview.parent / 'link.preview-saving').symlink_to(outside / 'kept.preview-saving')
    (preview.parent / 'folder.preview-saving').mkdir()

    result = _failing_run(photo, dest, '--no-previews')

    assert result.exit_code == 1
    assert not any(path.exists() for path in unfinished)
    assert (outside / 'kept.preview-saving').read_bytes() == b'not ours'
    assert (preview.parent / 'link.preview-saving').is_symlink()
    assert (preview.parent / 'folder.preview-saving').is_dir()
    assert preview.read_bytes() == PHOTO_PREVIEW
    assert '2 unfinished previews deleted' in log_messages()
    unfinished[0].write_bytes(b'half a preview')
    unlink = os.unlink

    def refused(name, *args, **kwargs):
        if str(name).endswith('.preview-saving'):
            raise OSError(errno.EACCES, os.strerror(errno.EACCES))
        unlink(name, *args, **kwargs)

    monkeypatch.setattr(os, 'unlink', refused)

    unreadable = _failing_run(photo, dest, '--no-previews')

    assert unreadable.exit_code == 1
    assert unfinished[0].exists()
    assert f'{SAVED_ONLY}: 1\n  {DEAD_PATH} -> {PREVIEW_FILE}\n' in unreadable.stdout
    denied = os.strerror(errno.EACCES)
    assert f'Could not delete the unfinished previews in {_previews_folder(dest)}: {denied}' in stderr_lines(
        unreadable.stderr
    )


@pytest.mark.parametrize(
    ('route', 'where', 'reason'),
    [
        (
            '/v1/disk/resources',
            {'preview_size': 'XXXL'},
            'Could not read the preview: Yandex Disk API returned HTTP 500: Injected HTTP 500 (after 2 attempts)',
        ),
        ('/preview', None, 'Could not fetch the preview: the preview host returned HTTP 500 (after 2 attempts)'),
    ],
    ids=['metadata', 'image'],
)
def test_a_preview_that_cannot_be_fetched_is_reported_and_the_run_is_unchanged(
    photo, dest, tmp_path, route, where, reason
):
    photo.inject('GET', route, *[500] * 10, where=where)

    result = _failing_run(photo, dest, '--api-retries', '1')

    assert result.exit_code == 1
    assert (
        f'Failed: 1\n  {DEAD_PATH}: {SERVICE_UNAVAILABLE}\nPreviews not saved or deleted: 1\n  {DEAD_PATH}: {reason}\n'
        in (result.stdout)
    )
    assert SAVED_ONLY not in result.stdout
    assert stderr_lines(result.stderr)[-2:] == [
        f'{DEAD_PATH}: preview not saved: {reason}',
        'Error: 1 failed; see the list above.',
    ]
    assert _others_verified(dest, DEAD_PATH)
    assert _preview_records() == []
    assert list(_previews_folder(dest).rglob('*')) == []
    earlier = len(photo.requests)

    healthy = run_download('disk:/Photos', str(tmp_path / 'healthy'))

    assert healthy.exit_code == 0, healthy.output
    assert not _mentions_previews(healthy.stdout)
    assert _preview_requests(photo, earlier) == []


@pytest.mark.parametrize('code', [errno.ENOSPC, errno.EACCES], ids=['full-disk', 'permission'])
def test_an_error_in_the_previews_folder_while_saving_a_preview_ends_the_preview_step_only(
    photo, dest, monkeypatch, code
):
    second = _second_failed_photo(photo)
    fsync = os.fsync

    def refused(fd: int) -> None:
        if os.readlink(f'/proc/self/fd/{fd}').endswith('.preview-saving'):
            raise OSError(code, os.strerror(code))
        fsync(fd)

    monkeypatch.setattr(os, 'fsync', refused)

    result = _failing_run(photo, dest, '--api-concurrency', '1')

    assert result.exit_code == 1
    reason = f'could not use previews in {_previews_folder(dest)}: {os.strerror(code)}'
    assert f'Previews not saved or deleted: 2\n  {DEAD_PATH}: {reason}\n  {second}: {reason}\n' in result.stdout
    assert _preview_requests(photo) == [f'meta {DEAD_PATH}', f'fetch {DEAD_PATH}']
    assert _preview_records() == []
    assert list(_previews_folder(dest).rglob('*.preview*')) == []
    assert stderr_lines(result.stderr)[-1] == 'Error: 2 failed; see the list above.'


def test_a_token_rejected_while_asking_for_a_preview_ends_the_preview_step_only(photo, dest):
    second = _second_failed_photo(photo)
    photo.inject('GET', '/v1/disk/resources', 401, where={'preview_size': 'XXXL'})

    result = _failing_run(photo, dest, '--api-concurrency', '1')

    assert result.exit_code == 1
    reason = "Yandex rejected the token (invalid, expired or revoked). Run 'yandex-disk-rescue auth login' to store a new one."
    assert f'Previews not saved or deleted: 2\n  {DEAD_PATH}: {reason}\n  {second}: {reason}\n' in result.stdout
    assert _preview_requests(photo) == [f'meta {DEAD_PATH}']
    assert _preview_records() == []
    assert stderr_lines(result.stderr)[-1] == 'Error: 2 failed; see the list above.'


@pytest.mark.parametrize(
    ('code', 'failing'),
    [
        (errno.EBUSY, f'could not delete the preview {PREVIEW_FILE}'),
        (errno.EACCES, 'could not use previews in {previews}'),
    ],
    ids=['busy', 'permission'],
)
def test_a_preview_of_a_verified_file_that_cannot_be_deleted_keeps_its_record(photo, dest, monkeypatch, code, failing):
    assert _failing_run(photo, dest).exit_code == 1
    unlink = os.unlink

    def refused(name, *args, **kwargs):
        if str(name).endswith('.preview.jpg'):
            raise OSError(code, os.strerror(code))
        unlink(name, *args, **kwargs)

    monkeypatch.setattr(os, 'unlink', refused)

    result = run_download(str(dest))

    assert result.exit_code == 0, result.output
    assert local_file(dest, DEAD_PATH).read_bytes() == DEAD
    reason = f'{failing.format(previews=_previews_folder(dest))}: {os.strerror(code)}'
    assert f'Previews not saved or deleted: 1\n  {DEAD_PATH}: {reason}\n' in result.stdout
    assert f'{DEAD_PATH}: {reason}' in stderr_lines(result.stderr)
    assert (dest / PREVIEW_FILE).read_bytes() == PHOTO_PREVIEW
    assert _preview_records() == [(DEAD_PATH, PREVIEW_FILE, len(PHOTO_PREVIEW), 'image/jpeg', 'XXXL')]
    monkeypatch.setattr(os, 'unlink', unlink)

    again = run_download(str(dest))

    assert again.exit_code == 0, again.output
    assert not _mentions_previews(again.stdout)
    assert not (dest / PREVIEW_FILE).exists()
    assert _preview_records() == []


def test_a_quarantined_file_gets_no_preview(disk, dest):
    disk.add_file(DEAD_PATH, DEAD, modified=MODIFIED, preview=PHOTO_PREVIEW)
    disk.corrupt(DEAD_PATH, 0)

    result = run_download(str(dest))

    assert result.exit_code == 1
    assert 'Quarantined: 1\n' in result.stdout
    assert not _mentions_previews(result.stdout)
    assert _preview_requests(disk) == []
    assert not _previews_folder(dest).exists()


@pytest.mark.parametrize('cause', ['folder-not-made', 'permission'])
def test_failures_with_a_local_cause_get_no_preview(disk, dest, monkeypatch, cause):
    if cause == 'folder-not-made':
        failing = ['disk:/Q?', 'disk:/Q?/napkin.jpg']
        disk.add_file(failing[-1], DEAD, preview=PHOTO_PREVIEW)
        _refuse_names(monkeypatch, lambda name: '?' in name or '_' in name)
    else:
        failing = ['disk:/Docs/napkin.jpg']
        disk.add_file(failing[-1], DEAD, preview=PHOTO_PREVIEW)
        (dest / 'Docs').mkdir(parents=True)
        (dest / 'Docs' / 'napkin.jpg').write_bytes(b'x' * len(DEAD))
        _refuse(monkeypatch, 'open_file', errno.EACCES, lambda name: name == 'napkin.jpg')

    result = run_download(str(dest))

    assert result.exit_code == 1
    assert f'Failed: {len(failing)}\n' in result.stdout
    assert not _mentions_previews(result.stdout)
    assert _preview_requests(disk) == []


def test_a_preview_for_a_version_changed_since_the_scan_is_not_saved(photo, dest):
    assert runner.invoke(app, ['scan']).exit_code == 0
    photo.add_file(DEAD_PATH, DEAD * 2, preview=PHOTO_PREVIEW)

    result = _failing_run(photo, dest)

    assert result.exit_code == 1
    reason = 'changed on the Disk since the scan; rerun with --scan-max-age 0'
    assert f'Previews not saved or deleted: 1\n  {DEAD_PATH}: {reason}\n' in result.stdout
    assert _preview_requests(photo) == [f'meta {DEAD_PATH}']
    assert _preview_records() == []


def _stopped_child(fake, dest, stop: signal.Signals, *args: str) -> tuple[int, str, str]:
    """A `download` child with `args` stopped by `stop` once it has asked the preview host for a preview: its exit
    code, stdout and stderr."""
    child = start_command('download', '--download-passes', '1', *args, str(dest))
    try:
        wait_until(lambda: any(r.path == '/preview' for r in fake.requests), child, 'fetched a preview')
        child.send_signal(stop)
        stdout, stderr = child.communicate(timeout=60)
    finally:
        child.kill()
        child.communicate()
    return child.returncode, stdout, stderr


@pytest.mark.parametrize('stop', [signal.SIGINT, signal.SIGTERM], ids=['sigint', 'sigterm'])
def test_an_interrupt_during_the_preview_phase_keeps_the_summary_and_the_exit_code(
    photo, dest, tmp_path, monkeypatch, stop
):
    photo.inject('GET', '/download', 503, where={'path': DEAD_PATH})
    photo.inject('GET', '/preview', Delay(30))

    exit_code, stdout, stderr = _stopped_child(photo, dest, stop)

    assert exit_code == 1, stderr
    assert f'Failed: 1\n  {DEAD_PATH}: {SERVICE_UNAVAILABLE}\n' in stdout
    assert (
        'Previews interrupted: 1 of 1 files not handled; the next run saves them.\n'
        f'Manifest: {manifest_path()}\nLog: {log_path()}\n'
    ) in stdout
    assert SAVED_ONLY not in stdout
    assert stderr_lines(stderr)[-1] == 'Error: 1 failed; see the list above.'
    assert 'Interrupted' not in stderr
    assert _preview_records() == []

    again = _failing_run(photo, dest)

    assert again.exit_code == 1
    assert f'{SAVED_ONLY}: 1\n  {DEAD_PATH} -> {PREVIEW_FILE}\n' in again.stdout
    assert (dest / PREVIEW_FILE).read_bytes() == PHOTO_PREVIEW
    photo.requests.clear()
    photo.inject('GET', '/download', 503, where={'path': DEAD_PATH})
    photo.inject('GET', '/preview', Delay(30))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'json-state'))

    exit_code, stdout, stderr = _stopped_child(photo, tmp_path / 'twin', stop, '--json')

    assert exit_code == 1, stderr
    document = json.loads(stdout)
    assert (document['exit_code'], document['counts']['previews_pending']) == (1, 1)
    assert (document['preview_only'], document['preview_errors']) == ([], [])


# Installed as `sitecustomize` in a download subprocess: with `FSYNC_GATE` set to a folder, an fsync of a preview being
# saved creates the marker `blocked` there and waits for the marker `release`, then runs.
_PREVIEW_FSYNC_GATE = """
import os, pathlib, time

_gate = pathlib.Path(os.environ['FSYNC_GATE'])
_fsync = os.fsync


def fsync(fd):
    if os.readlink(f'/proc/self/fd/{fd}').endswith('.preview-saving'):
        (_gate / 'blocked').touch()
        while not (_gate / 'release').exists():
            time.sleep(0.01)
    return _fsync(fd)


os.fsync = fsync
"""


@pytest.mark.parametrize('stop', [signal.SIGINT, signal.SIGTERM], ids=['sigint', 'sigterm'])
def test_an_interrupt_while_a_preview_is_saved_waits_for_the_save_and_its_record(
    photo, dest, tmp_path, monkeypatch, stop
):
    gate = _gated(monkeypatch, tmp_path, _PREVIEW_FSYNC_GATE)
    photo.inject('GET', '/download', 503, where={'path': DEAD_PATH})
    child = start_command('download', '--download-passes', '1', str(dest))
    try:
        wait_until((gate / 'blocked').exists, child, 'began saving the preview')
        child.send_signal(stop)
        time.sleep(0.5)
        (gate / 'release').touch()
        stdout, stderr = child.communicate(timeout=60)
    finally:
        child.kill()
        child.communicate()

    assert child.returncode == 1, stderr
    assert f'Failed: 1\n  {DEAD_PATH}: {SERVICE_UNAVAILABLE}\n{SAVED_ONLY}: 1\n  {DEAD_PATH} -> {PREVIEW_FILE}\n' in (
        stdout
    )
    assert 'Previews interrupted' not in stdout
    assert (dest / PREVIEW_FILE).read_bytes() == PHOTO_PREVIEW
    assert _preview_records() == [(DEAD_PATH, PREVIEW_FILE, len(PHOTO_PREVIEW), 'image/jpeg', 'XXXL')]


@pytest.mark.parametrize('stop', ['sigint', 'run-stopping-error'])
def test_an_interrupted_or_stopped_transfer_phase_fetches_no_previews(photo, dest, stop):
    photo.inject('GET', '/download', 503, where={'path': DEAD_PATH})
    if stop == 'sigint':
        photo.inject('GET', '/download', Delay(30), where={'path': 'disk:/Docs/readme.txt'})
        child = start_command('download', '--download-passes', '1', str(dest))
        try:
            wait_until(lambda: _fetches(photo, 'disk:/Docs/readme.txt'), child, 'fetched readme.txt')
            wait_until(lambda: _fetches(photo, DEAD_PATH), child, 'fetched the photo')
            child.send_signal(signal.SIGINT)
            _, stderr = child.communicate(timeout=60)
        finally:
            child.kill()
            child.communicate()
        assert child.returncode == 130, stderr
    else:
        photo.inject('GET', '/v1/disk/resources/download', *[500] * 50)
        photo.inject('GET', '/v1/disk/', Delay(0), *[500] * 50)
        result = run_download('--api-retries', '1', '--download-passes', '1', str(dest))
        assert result.exit_code == 1
        assert stderr_lines(result.stderr)[-1].startswith('Error: Could not download disk:/')

    assert _preview_requests(photo) == []
    assert not _previews_folder(dest).exists()


def test_retry_failed_fetches_only_the_failed_files_and_never_looks_at_the_others(disk, dest):
    failed = 'disk:/Docs/readme.txt'
    disk.inject('GET', '/download', 503, where={'path': failed})
    first = run_download('--download-passes', '1', str(dest))

    assert first.exit_code == 1
    assert f'Failed: 1\n  {failed}: ' in first.stdout
    assert [path for path, *_ in _failure_records()] == [failed]

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
    assert [path for path, *_ in _failure_records()] == ['disk:/Q?', 'disk:/Q?/x.txt']

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


def test_a_resume_check_failing_for_one_file_waits_for_the_next_pass_and_keeps_its_bytes(big, dest):
    _leave_partial(big, dest)
    confirmed = _confirmed()
    big.inject('GET', '/v1/disk/resources', 500, 500, where={'path': BIG_PATH})
    earlier = len(big.requests)

    result = run_download('--api-retries', '1', 'disk:/Big', str(dest))

    assert result.exit_code == 0, result.output
    assert '1 downloaded' in result.stdout
    assert 'Passes: 2 of 7\n' in result.stdout
    lookups = [r for r in big.requests[earlier:] if r.path == '/v1/disk/resources' and r.params['path'] == BIG_PATH]
    assert len(lookups) == 3
    assert sum(1 for r in big.requests[earlier:] if r.path == '/v1/disk/') == 2
    assert _fetches(big, BIG_PATH, earlier) == [_tail(confirmed)]
    assert (dest / 'big.bin').read_bytes() == BIG
    assert _failure_records() == []


def test_with_a_partial_folder_each_file_reaches_dest_as_one_sequential_copy(disk, dest, partial_root, monkeypatch):
    files = {**FILES, SEGMENTED_PATH: SEGMENTED, BIG_PATH: BIG}
    disk.add_file(SEGMENTED_PATH, SEGMENTED, modified=MODIFIED)
    disk.add_file(BIG_PATH, BIG, modified=MODIFIED)
    log = _io_log(monkeypatch, ('pwrite', 'write', 'ftruncate'))

    result = run_download(
        '-v', '--partial-dir', str(partial_root), '--download-segments', '4', *SEGMENTED_ARGS, str(dest)
    )
    calls = list(log)

    assert result.exit_code == 0, result.output
    folder = _separate_folder(dest, partial_root)
    total = format_size(sum(map(len, files.values())))
    room = rf'Partial files in {re.escape(str(folder))} \([0-9.]+ MiB available\)'
    assert re.search(rf'^{re.escape(f"Downloading 8 files ({total}) to {dest}")}\n{room}$', result.stdout, re.M)
    messages = log_messages()
    assert any(re.fullmatch(room, message) for message in messages)
    assert any(f', partial_dir={partial_root}, ' in m for m in messages if m.startswith('download started: '))
    copied = [m.partition(' copied to ')[0] for m in messages if re.search(f' copied to {dest} in [0-9.]+ s$', m)]
    assert sorted(copied) == sorted(files)
    assert f'8 downloaded ({total}), 0 skipped, 0 quarantined, 0 failed\n' in result.stdout
    for path, content in files.items():
        local = local_file(dest, path)
        assert local.read_bytes() == content
        assert local.stat().st_mtime_ns == _mtime_ns(MODIFIED)
    assert list(folder.iterdir()) == []
    assert _partials(dest) == []
    assert any(call == 'pwrite' and _below(path, folder) for call, path, _, _ in calls)
    in_dest = [(call, path, offset, length) for call, path, offset, length in calls if _below(path, dest)]
    assert {(call, path.endswith('.copy')) for call, path, _, _ in in_dest} == {('pwrite', True)}
    copies = [path for path, _ in itertools.groupby(path for _, path, _, _ in in_dest)]
    assert len(copies) == len(set(copies)), 'the copies were written interleaved'
    sizes = []
    for copy in copies:
        written = 0
        for _, _, offset, length in (call for call in in_dest if call[1] == copy):
            assert offset == written, f'{copy} was not written in order from offset 0'
            written += length
        sizes.append(written)
    assert sorted(sizes) == sorted(len(content) for content in files.values() if content)


@pytest.mark.parametrize('obstacle', ['file', 'link'])
def test_a_destinations_partial_folder_that_is_not_a_folder_stops_the_run_before_any_download(
    disk, dest, partial_root, tmp_path, obstacle
):
    folder = _separate_folder(dest, partial_root)
    if obstacle == 'file':
        folder.write_bytes(b'')
    else:
        folder.symlink_to(tmp_path)

    result = run_download('--partial-dir', str(partial_root), str(dest))

    assert result.exit_code == 1
    assert result.stderr == f'Error: Could not write {folder}: it is not a folder; move it away and run again.\n'
    assert _transfers(disk) == []


def test_with_a_partial_folder_a_resume_reads_that_folder_only(big, dest, partial_root, monkeypatch):
    partial = _leave_partial(big, dest, partial_root=partial_root)
    assert _partials(dest) == []
    log = _io_log(monkeypatch, ('pread', 'read'))
    earlier = len(big.requests)

    result = run_download('--partial-dir', str(partial_root), 'disk:/Big', str(dest))
    calls = list(log)

    assert result.exit_code == 0, result.output
    assert (dest / 'big.bin').read_bytes() == BIG
    assert _fetches(big, BIG_PATH, earlier) == [_tail(300_000)]
    assert [call for call in calls if _below(call[1], dest)] == []
    assert [(offset, length) for _, path, offset, length in calls if path == str(partial)][:2] == [
        (0, 300_000),
        (0, MiB),
    ]
    assert _partials(dest, partial_root) == []
    assert _partials(dest) == []


_HASHED_ROOT = 'disk:/Hashed'


def _slow_preads(monkeypatch) -> list[tuple[str, str, int, int]]:
    """The `_io_log` of every `os.pread`, each of which first sleeps 20 ms, so readers running at once interleave."""
    log = _io_log(monkeypatch, ('pread',))
    logged = os.pread

    def slow(fd, size, offset):
        time.sleep(0.02)
        return logged(fd, size, offset)

    monkeypatch.setattr(os, 'pread', slow)
    return log


def _finishing_together(monkeypatch, folder, count: int, size: int) -> None:
    """Hold the write that completes each of the first `count` `size`-byte partial files in `folder` until all of them
    are there, so the read-backs that follow start together."""
    together = threading.Barrier(count, timeout=10)
    written: dict[str, int] = {}
    lock = threading.Lock()
    pwrite = os.pwrite

    def gated(fd, data, offset):
        path = os.readlink(f'/proc/self/fd/{fd}')
        done = pwrite(fd, data, offset)
        if _below(path, folder) and Path(path).name.isdigit():
            with lock:
                written[path] = written.get(path, 0) + done
                complete = written[path] == size
            if complete:
                together.wait()
        return done

    monkeypatch.setattr(os, 'pwrite', gated)


@pytest.mark.parametrize('phase', ['resume', 'read back', 'adopt'])
def test_bytes_on_disk_are_hashed_for_one_file_at_a_time(disk, dest, monkeypatch, phase):
    monkeypatch.setattr(destination_module, '_READ_SIZE', 256 * 1024)
    size = {'resume': 2 * MiB, 'read back': 4 * MiB, 'adopt': MiB}[phase]
    count = {'resume': 4, 'read back': 2, 'adopt': 3}[phase]
    files = {f'{_HASHED_ROOT}/{n}.bin': random.Random(40 + n).randbytes(size) for n in range(count)}
    for path, content in files.items():
        disk.add_file(path, content, modified=MODIFIED)
    partials = dest / '.yandex-disk-rescue' / 'partial'
    hashed = dest if phase == 'adopt' else partials
    args: tuple[str, ...] = ()
    if phase == 'resume':
        for path in files:
            disk.inject('GET', '/download', Truncate(after_bytes=MiB), where={'path': path})
        cut = run_download('--download-retries', '0', '--download-passes', '1', _HASHED_ROOT, str(dest))
        assert cut.exit_code == 1, cut.output
        assert [partial.stat().st_size for partial in _partials(dest)] == [MiB] * count
    elif phase == 'read back':
        args = SEGMENTED_ARGS
        _finishing_together(monkeypatch, partials, count, size)
    else:
        dest.mkdir()
        for path, content in files.items():
            (dest / PurePosixPath(path).name).write_bytes(content)
    log = _slow_preads(monkeypatch)

    result = run_download('-v', *args, _HASHED_ROOT, str(dest))
    calls = list(log)

    assert result.exit_code == 0, result.output
    for path, content in files.items():
        assert (dest / PurePosixPath(path).name).read_bytes() == content
    paths = [path for _, path, _, _ in calls if _below(path, hashed) and Path(path).name[0].isdigit()]
    assert len(set(paths)) == count
    runs = [path for path, _ in itertools.groupby(paths)]
    assert len(runs) == len(set(runs)), f'the files were hashed interleaved: {runs}'
    assert any(message.endswith(" waits for another file's hash") for message in log_messages())


def test_with_a_partial_folder_resume_re_hashes_do_not_wait_for_an_adoption_hash_of_dest(
    big, dest, partial_root, monkeypatch
):
    partial = _leave_partial(big, dest, partial_root=partial_root)
    copy = random.Random(5).randbytes(MiB)
    big.add_file('disk:/Big/a.bin', copy, modified=MODIFIED)
    (dest / 'a.bin').write_bytes(copy)
    monkeypatch.setattr(destination_module, '_READ_SIZE', 256 * 1024)
    resumed = threading.Event()
    timed_out: list[str] = []
    pread = os.pread

    def adoption_waits_for_the_resume(fd, size, offset):
        path = os.readlink(f'/proc/self/fd/{fd}')
        if path == str(partial):
            resumed.set()
        elif path == str(dest / 'a.bin') and offset == 0 and not resumed.wait(10):
            timed_out.append(path)
        return pread(fd, size, offset)

    monkeypatch.setattr(os, 'pread', adoption_waits_for_the_resume)
    earlier = len(big.requests)

    result = run_download('--partial-dir', str(partial_root), '--scan-max-age', '0', 'disk:/Big', str(dest))

    assert timed_out == []
    assert result.exit_code == 0, result.output
    assert (dest / 'a.bin').read_bytes() == copy
    assert (dest / 'big.bin').read_bytes() == BIG
    assert _fetches(big, BIG_PATH, earlier) == [_tail(300_000)]
    assert _fetches(big, 'disk:/Big/a.bin', earlier) == []


@pytest.mark.anyio
async def test_a_copy_waiting_for_the_hash_of_another_shows_it_waits_to_hash(disk, dest, monkeypatch):
    files = {f'{_HASHED_ROOT}/{n}.bin': random.Random(50 + n).randbytes(MiB) for n in range(2)}
    dest.mkdir()
    for path, content in files.items():
        disk.add_file(path, content, modified=MODIFIED)
        (dest / PurePosixPath(path).name).write_bytes(content)
    monkeypatch.setattr(destination_module, '_READ_SIZE', 256 * 1024)
    held, release = threading.Event(), threading.Event()
    pread = os.pread

    def held_at_the_first_read(fd, size, offset):
        if not held.is_set() and _below(os.readlink(f'/proc/self/fd/{fd}'), dest):
            held.set()
            release.wait(10)
        return pread(fd, size, offset)

    monkeypatch.setattr(os, 'pread', held_at_the_first_read)
    progress = MirrorProgress()

    def states() -> list[str]:
        return sorted(transfer.state for transfer in progress.snapshot().active)

    async with _mirror_run(disk, _HASHED_ROOT, dest, progress=progress) as run:
        task = asyncio.create_task(run())
        try:
            await _until(lambda: states() == ['hashing', 'waiting to hash'], 'one copy hashing, the other waiting')
        finally:
            release.set()
        report = await task

    assert report.adopted == list(files)


def test_with_a_partial_folder_every_run_deletes_the_staged_copies_left_in_dest_and_nothing_else(
    big, dest, partial_root, monkeypatch
):
    _leave_partial(big, dest, partial_root=partial_root)
    own = dest / '.yandex-disk-rescue' / 'partial'
    for name in ('987654321.copy', '987654321', 'notes.copy', '12a.copy'):
        (own / name).write_bytes(b'left by hand or by a crash')
    (own / '123.copy').mkdir()
    durability = _durability_log(monkeypatch)

    result = run_download('--partial-dir', str(partial_root), 'disk:/Big', str(dest))

    assert result.exit_code == 0, result.output
    assert [p.name for p in _partials(dest)] == ['123.copy', '12a.copy', '987654321', 'notes.copy']
    unlinked = durability.index(('unlink', str(own / '987654321.copy')))
    assert ('fsync', str(own)) in durability[unlinked:]


def _staged_copies(dest) -> list[str]:
    return [p.name for p in _partials(dest) if p.name.endswith('.copy')]


def _rerun_finishes_without_fetching(disk, dest, partial_root, monkeypatch, *, adopted: bool) -> None:
    """Rerun the mirror of `disk:/Seg` with the partial folder `partial_root` after a run stopped while copying,
    placing or recording `SEGMENTED`, and check it ends verified with no content fetched and no partial file or staged
    copy left; a file fetched again is copied anew, after the run deleted the staged copy left by the stop."""
    seen: list[list[str]] = []
    partial = Destination.partial

    @contextmanager
    def watched(self, entry_id):
        seen.append(_staged_copies(dest))
        with partial(self, entry_id) as opened:
            yield opened

    monkeypatch.setattr(Destination, 'partial', watched)
    earlier = len(disk.requests)

    rerun = run_download('--partial-dir', str(partial_root), 'disk:/Seg', str(dest))

    assert rerun.exit_code == 0, rerun.output
    assert _transfers(disk, earlier) == []
    if adopted:
        assert '0 downloaded (0 B), 1 skipped, 0 quarantined, 0 failed\n' in rerun.stdout
    else:
        assert f'1 downloaded ({format_size(len(SEGMENTED))}), 0 skipped' in rerun.stdout
    assert seen == ([] if adopted else [[]])
    local = dest / 'large.bin'
    assert local.read_bytes() == SEGMENTED
    assert local.stat().st_mtime_ns == _mtime_ns(MODIFIED)
    assert _partials(dest) == []
    assert _partials(dest, partial_root) == []
    assert _journal() == []


def _killed(*args, **kwargs):
    raise KeyboardInterrupt


def _killed_after_the_first_chunk(copied):
    def copying(chunks, target, progress):
        yield next(copied(chunks, target, progress))
        raise KeyboardInterrupt

    return copying


@pytest.mark.parametrize('point', ['mid-copy', 'copied', 'placed', 'recorded'])
def test_with_a_partial_folder_a_run_stopped_on_the_way_to_dest_finishes_on_the_rerun_without_a_fetch(
    segmented, dest, partial_root, monkeypatch, point
):
    with monkeypatch.context() as patch:
        match point:
            case 'mid-copy':
                patch.setattr(mirroring, '_copied', _killed_after_the_first_chunk(mirroring._copied))
            case 'copied':
                patch.setattr(Destination, 'place', _killed)
            case 'placed':
                patch.setattr(Mirror, 'record_verified', _killed)
            case 'recorded':
                patch.setattr(Destination, 'remove_partial', _killed)
        stopped = run_download('--partial-dir', str(partial_root), 'disk:/Seg', str(dest))

    assert stopped.exit_code == 130
    assert stderr_lines(stopped.stderr) == ['Interrupted; rerun to continue the download.']
    local = dest / 'large.bin'
    placed = point in ('placed', 'recorded')
    assert local.read_bytes() == SEGMENTED if placed else not local.exists()
    [kept] = _partials(dest, partial_root)
    assert kept.read_bytes() == SEGMENTED
    full = (0, len(SEGMENTED), len(SEGMENTED), hashlib.sha256(SEGMENTED).hexdigest())
    assert _journal() == ([] if point == 'recorded' else [full])
    staged = _partials(dest)
    assert [p.name for p in staged] == ([] if placed else [f'{kept.name}.copy'])
    if point == 'mid-copy':
        assert staged[0].read_bytes() == SEGMENTED[:MiB]

    _rerun_finishes_without_fetching(segmented, dest, partial_root, monkeypatch, adopted=placed)


# Installed as `sitecustomize` in a download subprocess: with `FSYNC_GATE` set to a folder, an fsync of a staged copy
# creates the marker `blocked` there and never returns; every other fsync runs as usual. The run keeps no reserve in
# its partial folder, as `partial_root` does in this process.
_COPY_FSYNC_GATE = """
import os, pathlib, time

from yandex_disk_rescue import mirroring

mirroring.PARTIAL_RESERVE = 0
_gate = pathlib.Path(os.environ['FSYNC_GATE'])
_fsync = os.fsync


def fsync(fd):
    if os.readlink(f'/proc/self/fd/{fd}').endswith('.copy'):
        (_gate / 'blocked').touch()
        while True:
            time.sleep(1)
    return _fsync(fd)


os.fsync = fsync
"""


def _gated(monkeypatch, tmp_path, gate_code: str) -> Path:
    """Install `gate_code` as the `sitecustomize` of the subprocesses this test starts; the gate folder."""
    site, gate = tmp_path / 'site', tmp_path / 'gate'
    site.mkdir()
    gate.mkdir()
    (site / 'sitecustomize.py').write_text(gate_code)
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join(filter(None, [str(site), os.environ.get('PYTHONPATH')])))
    monkeypatch.setenv('FSYNC_GATE', str(gate))
    return gate


def test_with_a_partial_folder_a_run_killed_while_its_copy_is_fsynced_finishes_on_the_rerun_without_a_fetch(
    segmented, dest, partial_root, tmp_path, monkeypatch
):
    gate = _gated(monkeypatch, tmp_path, _COPY_FSYNC_GATE)
    child = start_command('download', '--partial-dir', str(partial_root), 'disk:/Seg', str(dest))
    try:
        wait_until(lambda: (gate / 'blocked').exists(), child, 'fsynced the staged copy')
        child.kill()
        child.communicate(timeout=60)
    finally:
        child.kill()
        child.communicate()
    monkeypatch.delenv('PYTHONPATH')

    assert child.returncode == -signal.SIGKILL
    assert not (dest / 'large.bin').exists()
    [kept] = _partials(dest, partial_root)
    assert _staged_copies(dest) == [f'{kept.name}.copy']

    _rerun_finishes_without_fetching(segmented, dest, partial_root, monkeypatch, adopted=False)


# The bytes each of the four segments of `SEGMENTED` confirms when `_leave_two_partials` cuts them short.
_SEGMENT_CUTS = (100_000, 250_000, 400_000, 0)
# The partial folder of the run leaving the partial files, then of the rerun: DEST's own (`None`), `partial_root`
# itself, or a folder of that name in it.
_PARTIAL_FOLDER_CHANGES = pytest.mark.parametrize(
    ('before', 'after'), [(None, '.'), ('.', None), ('a', 'b')], ids=['on', 'off', 'moved']
)


def _partial_root(partial_root, name: str | None):
    if name is None:
        return None
    root = partial_root / name
    root.mkdir(exist_ok=True)
    return root


def _partial_folder(dest, partial_root):
    """The folder of the partial files of `dest`: its own, or its folder below `partial_root`."""
    return dest / '.yandex-disk-rescue' / 'partial' if partial_root is None else _separate_folder(dest, partial_root)


def _entry_id(path: str) -> int:
    with closing(sqlite3.connect(f'{manifest_path().as_uri()}?mode=ro', uri=True)) as connection:
        return connection.execute('SELECT id FROM entries WHERE path = ?', (path,)).fetchone()[0]


def _leave_two_partials(fake, dest, partial_root):
    """Run a mirror of the Disk cut short with no retry and no later pass, which keeps the partial files of `BIG`, 300
    000 bytes confirmed, and of `SEGMENTED`, each segment confirming its `_SEGMENT_CUTS`, in DEST or below
    `partial_root`; then write unconfirmed bytes past every confirmed offset. Their partial files, `BIG`'s first."""
    fake.add_file(BIG_PATH, BIG, modified=MODIFIED)
    fake.add_file(SEGMENTED_PATH, SEGMENTED, modified=MODIFIED)
    fake.inject('GET', '/download', Truncate(after_bytes=300_000), where={'path': BIG_PATH})
    for segment, cut in zip(SEGMENT_RANGES, _SEGMENT_CUTS, strict=True):
        where = {'path': SEGMENTED_PATH}
        fake.inject('GET', '/download', Truncate(after_bytes=cut), where=where, where_headers={'range': segment})
    args = ('--download-retries', '0', '--download-passes', '1', *SEGMENTED_ARGS, *_partial_args(partial_root))
    result = run_download(*args, str(dest))

    assert result.exit_code == 1, result.output
    folder = _partial_folder(dest, partial_root)
    big, segmented = folder / str(_entry_id(BIG_PATH)), folder / str(_entry_id(SEGMENTED_PATH))
    assert sorted(_partials(dest, partial_root)) == sorted([big, segmented])
    assert _journals() == {
        int(big.name): [(0, len(BIG), 300_000, hashlib.sha256(BIG[:300_000]).hexdigest())],
        int(segmented.name): _segmented_journal(_SEGMENT_CUTS),
    }
    with big.open('ab') as file:
        file.write(b'\xee' * 50_000)
    with segmented.open('r+b') as file:
        for n, cut in enumerate(_SEGMENT_CUTS):
            file.seek(n * MiB + cut)
            file.write(b'\xee' * 1000)
    return big, segmented


def _journals() -> dict[int, list[tuple[int, int, int, str | None]]]:
    """The segments of each download journal in the manifest by entry id, read as `_journal` reads them."""
    with closing(sqlite3.connect(f'{manifest_path().as_uri()}?mode=ro', uri=True)) as connection:
        rows = connection.execute(
            'SELECT entry_id, start, end, confirmed, digest FROM download_segments ORDER BY entry_id, start'
        ).fetchall()
    return {entry_id: [tuple(row[1:]) for row in group] for entry_id, group in itertools.groupby(rows, lambda r: r[0])}


def _segmented_journal(cuts) -> list[tuple[int, int, int, str | None]]:
    """The journal of `SEGMENTED` whose segment `n` confirms its first `cuts[n]` bytes."""
    journal = []
    for n, cut in enumerate(cuts):
        start = n * MiB
        journal.append((start, start + MiB, start + cut, hashlib.sha256(SEGMENTED[start : start + cut]).hexdigest()))
    return [(start, end, confirmed, digest if confirmed > start else None) for start, end, confirmed, digest in journal]


def _segment_tails(cuts) -> list[str]:
    """The `Range` of each fetch of the rest of `SEGMENTED`'s segments after the first `cuts[n]` bytes of segment `n`,
    sorted."""
    return sorted(f'bytes={n * MiB + cut}-{(n + 1) * MiB - 1}' for n, cut in enumerate(cuts))


def _durability_log(monkeypatch) -> list[tuple[str, str]]:
    """Log `('fsync', path)` for every `os.fsync` and `('unlink', path)` for every `os.unlink` relative to a folder
    descriptor, in call order, the paths read from `/proc/self/fd`."""
    log: list[tuple[str, str]] = []
    fsync, unlink = os.fsync, os.unlink

    def logged_fsync(fd):
        log.append(('fsync', os.readlink(f'/proc/self/fd/{fd}')))
        return fsync(fd)

    def logged_unlink(path, *, dir_fd=None):
        if dir_fd is not None:
            log.append(('unlink', f'{os.readlink(f"/proc/self/fd/{dir_fd}")}/{path}'))
        return unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, 'fsync', logged_fsync)
    monkeypatch.setattr(os, 'unlink', logged_unlink)
    return log


def _moving(size: int, downloads: str, source, target) -> str:
    return f'Moving the confirmed bytes ({format_size(size)}) of {downloads} in progress from {source} to {target}\n'


@_PARTIAL_FOLDER_CHANGES
def test_a_changed_partial_folder_gets_only_the_confirmed_bytes_of_each_download_in_progress(
    disk, dest, partial_root, monkeypatch, before, after
):
    old_root, new_root = _partial_root(partial_root, before), _partial_root(partial_root, after)
    sources = _leave_two_partials(disk, dest, old_root)
    confirmed = {entry_id: [(s, c) for s, _, c, _ in segments] for entry_id, segments in _journals().items()}
    log = _io_log(monkeypatch, ('pread', 'read'))
    durability = _durability_log(monkeypatch)
    earlier = len(disk.requests)

    result = run_download('-v', *_partial_args(new_root), *SEGMENTED_ARGS, str(dest))
    calls = list(log)

    assert result.exit_code == 0, result.output
    old, new = _partial_folder(dest, old_root), _partial_folder(dest, new_root)
    size = format_size(300_000 + sum(_SEGMENT_CUTS))
    moving = _moving(300_000 + sum(_SEGMENT_CUTS), '2 downloads', old, new)
    assert moving in result.stdout
    messages = log_messages()
    assert moving.rstrip('\n') in messages
    moved = [m for m in messages if ': partial file moved ' in m or m.startswith('Moved the partial files ')]
    assert moved[:2] == [
        f'{BIG_PATH}: partial file moved from {old} to {new} ({format_size(300_000)} confirmed)',
        f'{SEGMENTED_PATH}: partial file moved from {old} to {new} ({format_size(sum(_SEGMENT_CUTS))} confirmed)',
    ]
    [end] = moved[2:]
    assert end.startswith(f'Moved the partial files of 2 downloads in progress ({size}) from {old} to {new} in ')
    [final] = [line for line in status_lines(result.stderr) if line.startswith('Mov')]
    assert re.fullmatch(rf'Moved: 2 of 2 files, {re.escape(size)} in \d+ s, average .+/s', final)
    if new_root is not None:
        assert result.stdout.index(moving) < result.stdout.index(f'Partial files in {new} (')
    for source in sources:
        reads = [(offset, length) for _, path, offset, length in calls if path == str(source)]
        assert reads
        for offset, length in reads:
            assert any(start <= offset and offset + length <= end for start, end in confirmed[int(source.name)])
        steps = [('fsync', str(new / source.name)), ('fsync', str(new)), ('unlink', str(source)), ('fsync', str(old))]
        at = durability.index(steps[0])
        for step in steps[1:]:
            at = durability.index(step, at + 1)
    assert _fetches(disk, BIG_PATH, earlier) == [_tail(300_000)]
    assert sorted(_fetches(disk, SEGMENTED_PATH, earlier)) == _segment_tails(_SEGMENT_CUTS)
    assert local_file(dest, BIG_PATH).read_bytes() == BIG
    assert local_file(dest, SEGMENTED_PATH).read_bytes() == SEGMENTED
    if old_root is None:
        assert _partials(dest) == []
    else:
        assert not old.exists()
        assert old_root.exists()
    assert _partials(dest, new_root) == []
    again = run_download(*_partial_args(new_root), str(dest))
    assert again.exit_code == 0, again.output
    assert 'Moving' not in again.stdout
    assert not [line for line in status_lines(again.stderr) if line.startswith('Mov')]


@_PARTIAL_FOLDER_CHANGES
def test_a_move_of_partial_files_stopped_after_the_first_file_goes_on_in_the_rerun_losing_no_confirmed_byte(
    disk, dest, partial_root, monkeypatch, before, after
):
    old_root, new_root = _partial_root(partial_root, before), _partial_root(partial_root, after)
    big, segmented = _leave_two_partials(disk, dest, old_root)
    new = _partial_folder(dest, new_root)
    pwrite = os.pwrite

    def stopped_on_the_second_file(fd, data, offset):
        if os.readlink(f'/proc/self/fd/{fd}') == str(new / segmented.name):
            raise KeyboardInterrupt
        return pwrite(fd, data, offset)

    with monkeypatch.context() as patch:
        patch.setattr(os, 'pwrite', stopped_on_the_second_file)
        stopped = run_download(*_partial_args(new_root), str(dest))

    assert stopped.exit_code == 130
    assert _moving(300_000 + sum(_SEGMENT_CUTS), '2 downloads', big.parent, new) in stopped.stdout
    assert not big.exists()
    assert (new / big.name).read_bytes() == BIG[:300_000]
    assert segmented.exists()
    earlier = len(disk.requests)

    rerun = run_download(*_partial_args(new_root), str(dest))

    assert rerun.exit_code == 0, rerun.output
    assert _moving(sum(_SEGMENT_CUTS), '1 download', big.parent, new) in rerun.stdout
    assert _fetches(disk, BIG_PATH, earlier) == [_tail(300_000)]
    assert sorted(_fetches(disk, SEGMENTED_PATH, earlier)) == _segment_tails(_SEGMENT_CUTS)
    assert local_file(dest, BIG_PATH).read_bytes() == BIG
    assert local_file(dest, SEGMENTED_PATH).read_bytes() == SEGMENTED
    assert _partials(dest, new_root) == []


def test_a_move_of_partial_files_shows_its_status_lines_then_its_last_one(disk, dest, partial_root, monkeypatch):
    big, _ = _leave_two_partials(disk, dest, None)
    pread = os.pread
    slowed: list[int] = []

    def slow_first_read(fd, size, offset):
        if not slowed and os.readlink(f'/proc/self/fd/{fd}') == str(big):
            slowed.append(fd)
            time.sleep(1.5)
        return pread(fd, size, offset)

    monkeypatch.setattr(os, 'pread', slow_first_read)

    result = run_download('--json', '--progress-interval', '1', *_partial_args(partial_root), str(dest))

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    folder = _partial_folder(dest, partial_root)
    assert document['partial_dir'] == str(partial_root)
    assert document['partial_move'] == {
        'source': str(big.parent),
        'target': str(folder),
        'downloads': 2,
        'size': 300_000 + sum(_SEGMENT_CUTS),
        'missing': False,
    }
    assert document['partial_room']['folder'] == str(folder)
    assert document['partial_room']['available'] > 0
    assert f'Partial files in {folder} ({format_size(document["partial_room"]["available"])} available)' in (
        stderr_lines(result.stderr)
    )
    size = re.escape(format_size(300_000 + sum(_SEGMENT_CUTS)))
    lines = [line for line in status_lines(result.stderr) if line.startswith('Mov')]
    assert lines[:-1]
    assert all(
        re.fullmatch(rf'Moving: [01] of 2 files, .+ of {size} \(\d+\.\d%\), (?:.+/s, )?ETA .+', line)
        for line in lines[:-1]
    )
    assert lines[:-1]
    assert re.fullmatch(rf'Moved: 2 of 2 files, {size} in \d+ s, average .+/s', lines[-1])
    status = status_lines(result.stderr)
    assert status.index(lines[-1]) < status.index(next(line for line in status if line.startswith('Downloaded: ')))


def _refuse_partial_reads(
    patch,
    partials: set[Path],
    code: int,
    *,
    every: bool = True,
    from_offset: int = 0,
    before: Callable[[], None] = lambda: None,
) -> list[int]:
    """Make the reads (`os.pread`) at `from_offset` or later of the open files at `partials` call `before`, then fail
    with `code`: every such read, or only the first one; the offsets of the reads refused."""
    pread = os.pread
    paths = {str(partial) for partial in partials}
    refused: list[int] = []

    def failing(fd, size, offset):
        if (every or not refused) and offset >= from_offset and os.readlink(f'/proc/self/fd/{fd}') in paths:
            refused.append(offset)
            before()
            raise OSError(code, os.strerror(code))
        return pread(fd, size, offset)

    patch.setattr(os, 'pread', failing)
    return refused


def _dropped_notice(path: str, partial: Path, code: int) -> str:
    return (
        f'{path}: could not read its partial file {partial}: {os.strerror(code)}; '
        'downloading it again from its first byte'
    )


@pytest.mark.parametrize('read', ['first', 'later'])
@pytest.mark.parametrize('code', [errno.EIO, errno.EBADMSG], ids=errno.errorcode.get)
def test_an_unreadable_partial_file_being_moved_is_dropped_and_downloaded_again(
    disk, dest, partial_root, monkeypatch, code, read
):
    big, segmented = _leave_two_partials(disk, dest, None)
    from_first_byte = {BIG_PATH: [None], SEGMENTED_PATH: sorted(SEGMENT_RANGES)}
    resumed = {BIG_PATH: [_tail(300_000)], SEGMENTED_PATH: _segment_tails(_SEGMENT_CUTS)}
    kept = {BIG_PATH: 300_000, SEGMENTED_PATH: sum(_SEGMENT_CUTS)}
    # The first read of `BIG`'s, or a read past the first segment of `SEGMENTED`'s once that segment is copied.
    (path, partial, offset), (other, _, _) = [(BIG_PATH, big, 0), (SEGMENTED_PATH, segmented, MiB)][
        :: 1 if read == 'first' else -1
    ]
    earlier = len(disk.requests)

    with monkeypatch.context() as patch:
        refused = _refuse_partial_reads(patch, {partial}, code, from_offset=offset)
        result = run_download('--json', *_partial_args(partial_root), *SEGMENTED_ARGS, str(dest))

    assert result.exit_code == 0, result.output
    notice = _dropped_notice(path, partial, code)
    assert stderr_lines(result.stderr).count(notice) == 1
    assert refused == [offset]
    assert sorted(_fetches(disk, path, earlier), key=str) == from_first_byte[path]
    assert sorted(_fetches(disk, other, earlier)) == resumed[other]
    assert local_file(dest, BIG_PATH).read_bytes() == BIG
    assert local_file(dest, SEGMENTED_PATH).read_bytes() == SEGMENTED
    assert not big.exists()
    assert not segmented.exists()
    assert _partials(dest, partial_root) == []
    move = json.loads(result.stdout)['partial_move']
    assert (move['downloads'], move['size']) == (2, 300_000 + sum(_SEGMENT_CUTS))
    messages = log_messages()
    assert messages.count(notice) == 1
    [end] = [m for m in messages if m.startswith('Moved the partial files ')]
    old, new = big.parent, _partial_folder(dest, partial_root)
    carried = re.escape(format_size(kept[other]))
    assert re.fullmatch(
        rf'Moved the partial files of 2 downloads in progress \({carried}\) '
        + re.escape(f'from {old} to {new} in ')
        + r'\d+\.\d s, 1 dropped as unreadable',
        end,
    )
    [moved] = [line for line in status_lines(result.stderr) if line.startswith('Moved: ')]
    assert re.fullmatch(rf'Moved: 2 of 2 files, {carried} in \d+ s, average .+/s', moved)


def test_a_partial_file_dropped_in_the_move_and_unreadable_again_fails_that_file(disk, dest, partial_root, monkeypatch):
    big, _ = _leave_two_partials(disk, dest, None)
    new = _partial_folder(dest, partial_root) / big.name

    with monkeypatch.context() as patch:
        _refuse_partial_reads(patch, {big, new}, errno.EIO)
        result = run_download(*_partial_args(partial_root), *SEGMENTED_ARGS, str(dest))

    assert result.exit_code == 1
    assert stderr_lines(result.stderr).count(_dropped_notice(BIG_PATH, big, errno.EIO)) == 1
    reason = f'could not read its partial file {new}: {os.strerror(errno.EIO)} (a second time in this run)'
    assert f'Failed: 1\n  {BIG_PATH}: {reason}\n' in result.stdout
    assert local_file(dest, SEGMENTED_PATH).read_bytes() == SEGMENTED


@pytest.mark.parametrize('code', [errno.EIO, errno.ENODEV], ids=errno.errorcode.get)
def test_an_unreadable_partial_file_being_moved_from_a_folder_that_is_gone_stops_the_run(
    disk, dest, partial_root, monkeypatch, code
):
    big, _ = _leave_two_partials(disk, dest, partial_root)
    journals = _journals()
    folder = _separate_folder(dest, partial_root)
    gone = folder.with_name('gone')

    def unplug() -> None:
        if code == errno.EIO:
            folder.rename(gone)

    with monkeypatch.context() as patch:
        _refuse_partial_reads(patch, {big}, code, before=unplug)
        result = run_download(*SEGMENTED_ARGS, str(dest))

    assert result.exit_code == 1
    assert stderr_lines(result.stderr) == [f'Error: Could not read {big}: {os.strerror(code)}.']
    assert _journals() == journals
    assert not [message for message in log_messages() if 'could not read its partial file' in message]


@pytest.mark.parametrize('file', ['one stream', 'segmented'])
@pytest.mark.parametrize('code', [errno.EIO, errno.EBADMSG], ids=errno.errorcode.get)
@pytest.mark.parametrize('layout', ['in-dest', 'separate'])
def test_an_unreadable_partial_file_is_emptied_and_downloaded_again_from_its_first_byte(
    disk, dest, partial_root, monkeypatch, layout, code, file
):
    root = partial_root if layout == 'separate' else None
    big, segmented = _leave_two_partials(disk, dest, root)
    (path, partial), (other, _) = [(BIG_PATH, big), (SEGMENTED_PATH, segmented)][:: 1 if file == 'one stream' else -1]
    earlier = len(disk.requests)

    with monkeypatch.context() as patch:
        refused = _refuse_partial_reads(patch, {partial}, code, every=False)
        calls = _io_log(patch, ('ftruncate', 'pwrite'))
        result = run_download(*_partial_args(root), *SEGMENTED_ARGS, str(dest))

    assert result.exit_code == 0, result.output
    notice = _dropped_notice(path, partial, code)
    assert stderr_lines(result.stderr).count(notice) == 1
    assert log_messages().count(notice) == 1
    from_first_byte = {BIG_PATH: [None], SEGMENTED_PATH: sorted(SEGMENT_RANGES)}
    resumed = {BIG_PATH: [_tail(300_000)], SEGMENTED_PATH: _segment_tails(_SEGMENT_CUTS)}
    assert sorted(_fetches(disk, path, earlier), key=str) == from_first_byte[path]
    assert sorted(_fetches(disk, other, earlier)) == resumed[other]
    assert local_file(dest, BIG_PATH).read_bytes() == BIG
    assert local_file(dest, SEGMENTED_PATH).read_bytes() == SEGMENTED
    assert _journals() == {}
    assert _partials(dest, root) == []
    assert len(refused) == 1
    assert next(call for call in calls if call[1] == str(partial)) == ('ftruncate', str(partial), 0, 0)


def _scanned_partial(dest, path: str, partial_root) -> Path:
    """Scan the Disk, so the entry ids are known, and return where the partial file of `path` will be."""
    assert runner.invoke(app, ['scan']).exit_code == 0
    return _partial_folder(dest, partial_root) / str(_entry_id(path))


# The file whose partial file is read only once it is fetched, its partial folder and its extra arguments: `SEGMENTED`
# read back in DEST, or `BIG` copied to DEST from a separate partial folder.
_FETCHED_READERS = pytest.mark.parametrize('reader', ['read back', 'copy'])


def _fetched_reader(
    fake, partial_root, reader: str
) -> tuple[str, bytes, Path | None, tuple[str, ...], list[str | None]]:
    """The path, content, partial folder, arguments and one turn's fetches of the file `reader` reads."""
    if reader == 'read back':
        fake.add_file(SEGMENTED_PATH, SEGMENTED, modified=MODIFIED)
        return SEGMENTED_PATH, SEGMENTED, None, SEGMENTED_ARGS, list(SEGMENT_RANGES)
    fake.add_file(BIG_PATH, BIG, modified=MODIFIED)
    return BIG_PATH, BIG, partial_root, _partial_args(partial_root), [None]


@_FETCHED_READERS
def test_a_partial_file_unreadable_once_fetched_is_downloaded_again_in_a_later_pass(
    disk, dest, partial_root, monkeypatch, reader
):
    path, content, root, args, fetches = _fetched_reader(disk, partial_root, reader)
    partial = _scanned_partial(dest, path, root)

    with monkeypatch.context() as patch:
        refused = _refuse_partial_reads(patch, {partial}, errno.EIO, every=False)
        result = run_download(*args, str(dest))

    assert result.exit_code == 0, result.output
    assert stderr_lines(result.stderr).count(_dropped_notice(path, partial, errno.EIO)) == 1
    assert ', 0 failed\n' in result.stdout
    assert sorted(_fetches(disk, path), key=str) == sorted(fetches * 2, key=str)
    assert local_file(dest, path).read_bytes() == content
    reason = f'could not read its partial file {partial}: {os.strerror(errno.EIO)}'
    assert f'{path} deferred to a later pass: {reason}' in log_messages()
    assert refused == [0]


def test_a_partial_file_unreadable_once_fetched_with_no_pass_left_is_a_local_failure_with_no_preview(
    disk, dest, partial_root, monkeypatch
):
    path, _, root, args, _ = _fetched_reader(disk, partial_root, 'read back')
    partial = _scanned_partial(dest, path, root)

    with monkeypatch.context() as patch:
        _refuse_partial_reads(patch, {partial}, errno.EIO, every=False)
        result = run_download('--download-passes', '1', '--json', *args, str(dest))

    assert result.exit_code == 1
    document = json.loads(result.stdout)
    reason = f'could not read its partial file {partial}: {os.strerror(errno.EIO)}'
    assert document['failed'] == [{'path': path, 'reason': reason, 'unavailable': False}]
    assert document['preview_only'] == []
    assert _preview_requests(disk) == []


@_FETCHED_READERS
def test_a_partial_file_unreadable_twice_in_one_run_fails_that_file(disk, dest, partial_root, monkeypatch, reader):
    path, _, root, args, fetches = _fetched_reader(disk, partial_root, reader)
    partial = _scanned_partial(dest, path, root)

    with monkeypatch.context() as patch:
        _refuse_partial_reads(patch, {partial}, errno.EIO)
        result = run_download('--download-passes', '0', *args, str(dest))

    assert result.exit_code == 1
    assert sorted(_fetches(disk, path), key=str) == sorted(fetches * 2, key=str)
    assert stderr_lines(result.stderr).count(_dropped_notice(path, partial, errno.EIO)) == 1
    reason = f'could not read its partial file {partial}: {os.strerror(errno.EIO)} (a second time in this run)'
    assert f'Failed: 1\n  {path}: {reason}\n' in result.stdout
    assert _preview_requests(disk) == []
    [journal] = _journals().values()
    assert journal
    assert all((confirmed, digest) == (start, None) for start, _, confirmed, digest in journal)
    assert _others_verified(dest, path)


@pytest.mark.parametrize('code', [errno.EIO, errno.ENODEV, errno.EMFILE, errno.EACCES], ids=errno.errorcode.get)
def test_an_unreadable_partial_file_stops_the_run_when_every_file_would_fail(disk, dest, monkeypatch, code):
    big, _ = _leave_two_partials(disk, dest, None)
    journals = _journals()
    gone = dest.with_name('gone')

    def unplug() -> None:
        if code == errno.EIO:
            dest.rename(gone)

    with monkeypatch.context() as patch:
        _refuse_partial_reads(patch, {big}, code, before=unplug)
        result = run_download(*SEGMENTED_ARGS, str(dest))

    assert result.exit_code == 1
    assert stderr_lines(result.stderr) == [f'Error: Could not read {big}: {os.strerror(code)}.']
    if code == errno.EIO:
        gone.rename(dest)
    assert _journals() == journals
    assert big.read_bytes()[:300_000] == BIG[:300_000]
    assert not [message for message in log_messages() if 'could not read its partial file' in message]


@pytest.mark.parametrize(('before', 'after'), [('.', None), ('a', 'b')], ids=['off', 'moved'])
def test_a_lost_partial_folder_is_reported_and_its_downloads_start_again_from_byte_0(
    disk, dest, partial_root, before, after
):
    old_root, new_root = _partial_root(partial_root, before), _partial_root(partial_root, after)
    big, _ = _leave_two_partials(disk, dest, old_root)
    shutil.rmtree(big.parent)
    earlier = len(disk.requests)

    result = run_download('--json', *_partial_args(new_root), str(dest))

    assert result.exit_code == 0, result.output
    missing = (
        f'The partial folder of the last run, {big.parent}, is missing; its downloads in progress start again from '
        'their first byte.'
    )
    assert missing in stderr_lines(result.stderr)
    document = json.loads(result.stdout)
    move = document['partial_move']
    assert (move['source'], move['target'], move['downloads'], move['missing']) == (
        str(big.parent),
        str(_partial_folder(dest, new_root)),
        2,
        True,
    )
    assert missing in log_messages()
    assert not [line for line in status_lines(result.stderr) if line.startswith('Mov')]
    assert _fetches(disk, BIG_PATH, earlier) == [None]
    # Its journal keeps no byte once the partial folder is lost, so it is planned again with this run's (default)
    # segment threshold.
    assert _fetches(disk, SEGMENTED_PATH, earlier) == [None]
    assert local_file(dest, BIG_PATH).read_bytes() == BIG
    assert local_file(dest, SEGMENTED_PATH).read_bytes() == SEGMENTED
    again = run_download(*_partial_args(new_root), str(dest))
    assert again.exit_code == 0, again.output
    assert 'is missing' not in again.stdout


def _recorded_partial_folder() -> Path | None:
    with closing(sqlite3.connect(f'{manifest_path().as_uri()}?mode=ro', uri=True)) as connection:
        [(folder,)] = connection.execute('SELECT partial_folder FROM mirrors').fetchall()
    return None if folder is None else Path(os.fsdecode(folder))


def test_a_lost_partial_folder_with_no_download_in_progress_is_not_reported(disk, dest, partial_root):
    assert run_download(*_partial_args(partial_root), str(dest)).exit_code == 0
    shutil.rmtree(_partial_folder(dest, partial_root))

    result = run_download(str(dest))

    assert result.exit_code == 0, result.output
    assert 'is missing' not in result.stdout
    assert _recorded_partial_folder() is None


def test_the_recorded_partial_folder_reached_by_another_path_is_kept_as_it_is_and_the_new_path_recorded(
    disk, dest, partial_root
):
    root = _partial_root(partial_root, 'a')
    big, _ = _leave_two_partials(disk, dest, root)
    # The folder the last run recorded, reached through a link as through a bind mount.
    alias = partial_root / 'alias'
    alias.symlink_to(root)
    with closing(sqlite3.connect(manifest_path())) as connection, connection:
        connection.execute('UPDATE mirrors SET partial_folder = ?', (os.fsencode(alias / big.parent.name),))
    earlier = len(disk.requests)

    result = run_download(*_partial_args(root), *SEGMENTED_ARGS, str(dest))

    assert result.exit_code == 0, result.output
    assert 'Moving' not in result.stdout
    assert _fetches(disk, BIG_PATH, earlier) == [_tail(300_000)]
    assert sorted(_fetches(disk, SEGMENTED_PATH, earlier)) == _segment_tails(_SEGMENT_CUTS)
    assert local_file(dest, BIG_PATH).read_bytes() == BIG
    assert local_file(dest, SEGMENTED_PATH).read_bytes() == SEGMENTED
    assert _recorded_partial_folder() == big.parent


@_PARTIAL_FOLDER_CHANGES
def test_a_moved_partial_file_cut_short_below_a_confirmed_offset_fetches_that_segment_again(
    disk, dest, partial_root, before, after
):
    old_root, new_root = _partial_root(partial_root, before), _partial_root(partial_root, after)
    big, segmented = _leave_two_partials(disk, dest, old_root)
    os.truncate(segmented, 2 * MiB + 1000)
    kept = (_SEGMENT_CUTS[0], _SEGMENT_CUTS[1], 0, 0)
    earlier = len(disk.requests)

    result = run_download(*_partial_args(new_root), str(dest))

    assert result.exit_code == 0, result.output
    new = _partial_folder(dest, new_root)
    assert _moving(300_000 + sum(kept), '2 downloads', big.parent, new) in result.stdout
    assert _fetches(disk, BIG_PATH, earlier) == [_tail(300_000)]
    assert sorted(_fetches(disk, SEGMENTED_PATH, earlier)) == _segment_tails(kept)
    assert local_file(dest, BIG_PATH).read_bytes() == BIG
    assert local_file(dest, SEGMENTED_PATH).read_bytes() == SEGMENTED


BIG2_PATH = 'disk:/Big/big2.bin'
BIG2 = random.Random(5).randbytes(MiB)


def _free(monkeypatch, free: int) -> None:
    """Make the filesystem of the partial folder report `free` bytes free; `partial_root` keeps no reserve, so the
    room of a run is `free` and the bytes its kept partial files allocate."""
    monkeypatch.setattr(localfs, 'free_bytes', lambda dir_fd: free)


def _room_line(dest, partial_root, capacity: int) -> str:
    return f'Partial files in {_separate_folder(dest, partial_root)} ({format_size(capacity)} available)\n'


def _content_started(fake, path: str) -> list[float]:
    return [r.started for r in fake.requests if r.path == '/download' and r.params['path'] == path]


def test_a_partial_folder_with_room_for_one_large_file_fetches_the_large_files_one_at_a_time(
    big, dest, partial_root, monkeypatch
):
    big.add_file(BIG2_PATH, BIG2, modified=MODIFIED)
    big.chunk_delay = 0.01
    _free(monkeypatch, MiB + MiB // 2)
    ended: dict[int, float] = {}
    remove_partial = Destination.remove_partial

    def recording_remove_partial(destination, entry_id):
        remove_partial(destination, entry_id)
        ended[entry_id] = time.monotonic()

    monkeypatch.setattr(Destination, 'remove_partial', recording_remove_partial)

    result = run_download('-v', *_partial_args(partial_root), str(dest))

    assert result.exit_code == 0, result.output
    assert _room_line(dest, partial_root, MiB + MiB // 2) in result.stdout
    folder = _separate_folder(dest, partial_root)
    messages = log_messages()
    waits = [m for m in messages if ' waits for room ' in m]
    assert waits == [f'{BIG2_PATH} waits for room in the partial folder {folder} (1.0 MiB needed, 512.0 KiB available)']
    turn = re.compile(rf'(.+) waits for its turn of the partial folder {re.escape(str(folder))} behind \d+ files? .+')
    assert sorted(match[1] for m in messages if (match := turn.fullmatch(m))) == sorted(
        p for p, c in FILES.items() if c
    )
    assert _others_verified(dest, failed='')
    assert local_file(dest, BIG_PATH).read_bytes() == BIG
    assert local_file(dest, BIG2_PATH).read_bytes() == BIG2
    assert min(_content_started(big, BIG2_PATH)) > ended[_entry_id(BIG_PATH)]
    assert _partials(dest, partial_root) == []


def test_a_file_larger_than_the_room_of_the_partial_folder_fails_and_a_retry_with_room_fetches_it(
    big, dest, partial_root, monkeypatch
):
    with monkeypatch.context() as patch:
        _free(patch, MiB // 2)
        result = run_download(*_partial_args(partial_root), str(dest))

    assert result.exit_code == 1
    folder = _separate_folder(dest, partial_root)
    reason = f'larger than the room in the partial folder {folder} (1.0 MiB needed, 512.0 KiB available)'
    assert _room_line(dest, partial_root, MiB // 2) in result.stdout
    assert f'Failed: 1\n  {BIG_PATH}: {reason}\n' in result.stdout
    assert _failure_records() == [(BIG_PATH, reason, 1)]
    assert _fetches(big, BIG_PATH) == []
    assert _partials(dest, partial_root) == []
    assert _others_verified(dest, BIG_PATH)

    retry = run_download(*_partial_args(partial_root), '--retry-failed', str(dest))

    assert retry.exit_code == 0, retry.output
    assert local_file(dest, BIG_PATH).read_bytes() == BIG
    assert _failure_records() == []
    assert _partials(dest, partial_root) == []


def test_a_file_waiting_for_room_is_deferred_when_the_last_running_file_is_deferred_and_a_pass_with_room_ends_both(
    big, dest, partial_root, monkeypatch
):
    big.add_file(BIG2_PATH, BIG2, modified=MODIFIED)
    big.chunk_delay = 0.01
    big.inject('GET', '/download', Truncate(after_bytes=300_000), where={'path': BIG_PATH})
    # One large file fits, and the second one does not fit next to the bytes the first one keeps once deferred.
    free = MiB + MiB // 8
    _free(monkeypatch, free)
    args = ('--download-retries', '0', *_partial_args(partial_root))

    result = run_download(*args, '--download-passes', '1', 'disk:/Big', str(dest))

    assert result.exit_code == 1
    [kept] = _partials(dest, partial_root)
    assert kept.name == str(_entry_id(BIG_PATH))
    allocated = kept.stat().st_blocks * 512
    folder = _separate_folder(dest, partial_root)
    waited = f'no room yet in the partial folder {folder} (1.0 MiB needed, {format_size(free - allocated)} available)'
    assert [(reason, turns) for path, reason, turns in _failure_records() if path == BIG2_PATH] == [(waited, 1)]
    assert _content_started(big, BIG2_PATH) == []
    earlier = len(big.requests)

    rerun = run_download(*args, 'disk:/Big', str(dest))

    assert rerun.exit_code == 0, rerun.output
    assert _room_line(dest, partial_root, free + allocated) in rerun.stdout
    assert _fetches(big, BIG_PATH, earlier) == [_tail(300_000)]
    assert _fetches(big, BIG2_PATH, earlier) == [None]
    assert (dest / 'big.bin').read_bytes() == BIG
    assert (dest / 'big2.bin').read_bytes() == BIG2
    assert _partials(dest, partial_root) == []


def test_a_turn_waiting_for_room_leaves_the_silent_streak_as_it_was(big, dest, partial_root, monkeypatch):
    big.add_file(BIG2_PATH, BIG2, modified=MODIFIED)
    args = ('--download-retries', '0', *_partial_args(partial_root))
    for path in (BIG_PATH, BIG2_PATH):
        big.inject('GET', '/download', *[503] * 3, where={'path': path})
    assert run_download(*args, '--download-passes', '3', 'disk:/Big', str(dest)).exit_code == 1
    *_, since = failure_record(BIG2_PATH)
    assert failure_record(BIG2_PATH) == (f'{SERVICE_UNAVAILABLE} (after 3 passes)', 3, 3, since)
    big.inject('GET', '/download', Truncate(after_bytes=300_000), where={'path': BIG_PATH})
    big.inject('GET', '/download', 503, where={'path': BIG2_PATH})
    # One large file fits: big2.bin waits for room until big.bin, cut short in pass 1, is done in pass 2.
    _free(monkeypatch, MiB + MiB // 8)
    logged = len(log_messages())

    result = run_download(*args, '--download-passes', '3', 'disk:/Big', str(dest))

    assert result.exit_code == 1
    assert any(m.startswith(f'{BIG2_PATH} deferred to a later pass: no room yet ') for m in log_messages()[logged:])
    assert (dest / 'big.bin').read_bytes() == BIG
    assert failure_record(BIG2_PATH) == (f'{SERVICE_UNAVAILABLE} (after 3 passes)', 4, 4, since)


def test_a_file_failed_once_and_then_only_waiting_for_room_counts_one_failed_turn_with_the_reason_it_is_reported_with(
    big, dest, partial_root, monkeypatch
):
    big.add_file(BIG2_PATH, BIG2, modified=MODIFIED)
    big.inject('GET', '/download', STALL, where={'path': BIG_PATH})
    big.inject('GET', '/download', Truncate(after_bytes=600_000), STALL, where={'path': BIG2_PATH})
    # One large file fits; once big2.bin keeps its partial file, big.bin no longer fits next to it.
    _free(monkeypatch, MiB + MiB // 8)
    args = ('--download-timeout', STALL_TIMEOUT, '--download-retries', '0', *_partial_args(partial_root))

    result = run_download(*args, '--download-passes', '2', 'disk:/Big', str(dest))

    assert result.exit_code == 1
    assert 'Passes: 2 of 2\n' in result.stdout
    assert _fetches(big, BIG_PATH) == [None]
    records = _failure_records()
    assert [(path, turns) for path, _, turns in records] == [(BIG_PATH, 1), (BIG2_PATH, 2)]
    [(_, waited, _), (_, stalled, _)] = records
    folder = _separate_folder(dest, partial_root)
    assert waited.startswith(f'no room yet in the partial folder {folder} (1.0 MiB needed, ')
    assert waited.endswith(' (after 2 passes)')
    assert stalled == f'the download stalled: no data for {STALL_TIMEOUT} s (after 2 passes)'
    assert f'Failed: 2\n  {BIG_PATH}: {waited}\n  {BIG2_PATH}: {stalled}\n' in result.stdout


def test_with_a_partial_folder_an_untried_file_waits_for_the_room_a_failed_files_kept_partial_frees(
    big, dest, partial_root, monkeypatch
):
    kept = _leave_partial(big, dest, partial_root=partial_root)
    allocated = kept.stat().st_blocks * 512
    untried = 'disk:/Big/c-untried.bin'
    big.add_file(untried, BIG2, modified=MODIFIED)
    # big.bin fits next to its own kept partial file; c-untried.bin fits only once that partial file is gone.
    free = MiB - allocated // 2
    _free(monkeypatch, free)
    logged = len(log_messages())
    earlier = len(big.requests)

    result = run_download(
        '--download-connections', '1', '--scan-max-age', '0', *_partial_args(partial_root), 'disk:/Big', str(dest)
    )

    assert result.exit_code == 0, result.output
    assert ', 0 failed\nPasses: 2 of 7\n' in result.stdout
    assert _content_order(big, earlier) == [BIG_PATH, untried]
    assert _fetches(big, BIG_PATH, earlier) == [_tail(300_000)]
    folder = _separate_folder(dest, partial_root)
    waited = f'no room yet in the partial folder {folder} (1.0 MiB needed, {format_size(free)} available)'
    messages = log_messages()[logged:]
    deferred = messages.index(f'{untried} deferred to a later pass: {waited}')
    assert deferred < next(i for i, m in enumerate(messages) if m.startswith(f'{BIG_PATH} downloaded: '))
    assert (dest / 'big.bin').read_bytes() == BIG
    assert (dest / 'c-untried.bin').read_bytes() == BIG2
    assert _failure_records() == []
    assert _partials(dest, partial_root) == []


def test_a_kept_sparse_partial_file_takes_only_the_room_it_allocates(segmented, dest, partial_root, monkeypatch):
    for segment, cut in zip(SEGMENT_RANGES, _SEGMENT_CUTS, strict=True):
        where = {'path': SEGMENTED_PATH}
        segmented.inject('GET', '/download', Truncate(after_bytes=cut), where=where, where_headers={'range': segment})
    args = ('--download-retries', '0', '--download-passes', '1', *SEGMENTED_ARGS, *_partial_args(partial_root))
    assert run_download(*args, str(dest)).exit_code == 1
    [kept] = _partials(dest, partial_root)
    allocated = kept.stat().st_blocks * 512
    assert kept.stat().st_size == len(SEGMENTED)
    # Room for the segmented file at full size, which leaves no room for `BIG` next to it at full size.
    capacity = len(SEGMENTED) + MiB // 2
    assert allocated + len(BIG) <= capacity < len(SEGMENTED) + len(BIG)
    _free(monkeypatch, capacity - allocated)
    segmented.add_file(BIG_PATH, BIG, modified=MODIFIED)

    result = run_download(*args, '--scan-max-age', '0', str(dest))

    assert result.exit_code == 0, result.output
    assert _room_line(dest, partial_root, capacity) in result.stdout
    assert local_file(dest, BIG_PATH).read_bytes() == BIG
    assert local_file(dest, SEGMENTED_PATH).read_bytes() == SEGMENTED
    assert _partials(dest, partial_root) == []


def test_the_kept_partial_file_of_an_adopted_file_frees_its_room_for_the_next_file(
    segmented, dest, partial_root, monkeypatch
):
    with monkeypatch.context() as patch:
        patch.setattr(Mirror, 'record_verified', _killed)
        stopped = run_download('--partial-dir', str(partial_root), 'disk:/Seg', str(dest))
    assert stopped.exit_code == 130
    [kept] = _partials(dest, partial_root)
    allocated = kept.stat().st_blocks * 512
    # `after.bin` walks first and fits only once the partial file of the adopted `large.bin` is deleted.
    segmented.add_file('disk:/Seg/after.bin', BIG, modified=MODIFIED)
    _free(monkeypatch, MiB // 2)

    result = run_download('--partial-dir', str(partial_root), '--scan-max-age', '0', 'disk:/Seg', str(dest))

    assert result.exit_code == 0, result.output
    assert _room_line(dest, partial_root, MiB // 2 + allocated) in result.stdout
    assert 'Already on disk and verified: 1\n  disk:/Seg/large.bin\n' in result.stdout
    assert (dest / 'after.bin').read_bytes() == BIG
    assert (dest / 'large.bin').read_bytes() == SEGMENTED
    assert _partials(dest, partial_root) == []


def test_kept_partial_files_that_never_fit_next_to_each_other_fail_at_once_with_no_pass_limit(
    big, dest, partial_root, monkeypatch
):
    big.add_file(BIG2_PATH, BIG2, modified=MODIFIED)
    for path in (BIG_PATH, BIG2_PATH):
        big.inject('GET', '/download', Truncate(after_bytes=600_000), where={'path': path})
        big.inject('GET', '/download', *[503] * 3, where={'path': path})
    args = ('--download-retries', '0', *_partial_args(partial_root))
    assert run_download(*args, '--download-passes', '4', 'disk:/Big', str(dest)).exit_code == 1
    kept = _partials(dest, partial_root)
    allocated = sum(p.stat().st_blocks * 512 for p in kept)
    assert len(kept) == 2
    assert [failure_record(path)[1:3] for path in (BIG_PATH, BIG2_PATH)] == [(4, 3), (4, 3)]
    _free(monkeypatch, 0)
    earlier = len(big.requests)

    result = run_download(*args, '--download-passes', '0', 'disk:/Big', str(dest))

    assert result.exit_code == 1
    folder = _separate_folder(dest, partial_root)
    reasons = []
    for partial in sorted(kept, key=lambda p: int(p.name)):
        needed = format_size(MiB - partial.stat().st_blocks * 512)
        reasons.append(
            f'no room in the partial folder {folder} next to the partial files kept there ({needed} needed, '
            '0 B available); free space there, then run again or with --retry-failed'
        )
    assert _room_line(dest, partial_root, allocated) in result.stdout
    assert 'Passes: 1 of unlimited\n' in result.stdout
    assert ''.join(f'  {path}: {reason}\n' for path, reason in zip((BIG_PATH, BIG2_PATH), reasons, strict=True)) in (
        result.stdout
    )
    assert _transfers(big, earlier) == []
    assert [failure_record(path) for path in (BIG_PATH, BIG2_PATH)] == [(reason, 5, 0, None) for reason in reasons]
    assert sorted(_partials(dest, partial_root)) == sorted(kept)


def test_a_run_stopped_while_a_file_waits_for_room_ends_at_once_and_the_rerun_goes_on(
    big, dest, partial_root, monkeypatch
):
    big.add_file(BIG2_PATH, BIG2, modified=MODIFIED)
    big.chunk_delay = 0.01
    _free(monkeypatch, MiB + MiB // 2)
    folder = _separate_folder(dest, partial_root)
    pwrite = os.pwrite

    def failing_write(fd, data, offset):
        if os.readlink(f'/proc/self/fd/{fd}') == str(folder / str(_entry_id(BIG_PATH))) and offset >= MiB // 2:
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        return pwrite(fd, data, offset)

    with monkeypatch.context() as patch:
        patch.setattr(os, 'pwrite', failing_write)
        result = run_download(*_partial_args(partial_root), 'disk:/Big', str(dest))

    assert result.exit_code == 1
    assert stderr_lines(result.stderr) == [
        f'Error: Could not write {folder / str(_entry_id(BIG_PATH))}: No space left on device.'
    ]
    assert _content_started(big, BIG2_PATH) == []

    rerun = run_download(*_partial_args(partial_root), 'disk:/Big', str(dest))

    assert rerun.exit_code == 0, rerun.output
    assert (dest / 'big.bin').read_bytes() == BIG
    assert (dest / 'big2.bin').read_bytes() == BIG2


@pytest.mark.anyio
@pytest.mark.parametrize(
    ('free', 'waiting'), [(4 * MiB, 'waiting for the copy'), (MiB + MiB // 2, 'waiting for room')], ids=['copy', 'room']
)
async def test_with_a_partial_folder_a_file_in_progress_shows_how_far_its_copy_to_dest_got_and_what_it_waits_for(
    big, dest, partial_root, monkeypatch, free, waiting
):
    big.add_file(BIG2_PATH, BIG2, modified=MODIFIED)
    _free(monkeypatch, free)
    monkeypatch.setattr(destination_module, '_READ_SIZE', MiB // 4)
    held, release = threading.Event(), threading.Event()
    copied = mirroring._copied

    def held_after_the_first_chunk(chunks, target, file_progress):
        hold = not held.is_set()
        for chunk in copied(chunks, target, file_progress):
            yield chunk
            if hold:
                held.set()
                release.wait(10)
                hold = False

    monkeypatch.setattr(mirroring, '_copied', held_after_the_first_chunk)
    progress = MirrorProgress()

    def states() -> list[str]:
        return sorted(transfer.state for transfer in progress.snapshot().active)

    async with _mirror_run(big, 'disk:/Big', dest, partial_dir=partial_root, progress=progress) as run:
        task = asyncio.create_task(run())
        try:
            await _until(
                lambda: held.is_set() and states() == ['copying', waiting],
                f'one file held after its first chunk and the other {waiting}',
            )
            [copying] = [transfer for transfer in progress.snapshot().active if transfer.state == 'copying']
        finally:
            release.set()
        report = await task

    assert 0 < copying.copied < copying.size == copying.held

    assert report.downloaded == 2
    assert (dest / 'big.bin').read_bytes() == BIG
    assert (dest / 'big2.bin').read_bytes() == BIG2


@pytest.mark.parametrize(
    ('way', 'partials'),
    [('downloaded', 'separate'), ('quarantined', 'separate'), ('adopted', 'in-dest'), ('adopted', 'separate')],
)
def test_a_partial_file_that_cannot_be_deleted_once_its_file_is_recorded_is_reported_and_fails_nothing(
    disk, dest, monkeypatch, request, way, partials
):
    path = 'disk:/Docs/readme.txt'
    partial_root = request.getfixturevalue('partial_root') if partials == 'separate' else None
    folder = dest / '.yandex-disk-rescue' / 'partial' if partial_root is None else _separate_folder(dest, partial_root)
    if way == 'quarantined':
        disk.corrupt(path, offset=0)
    if way == 'adopted':
        (dest / 'readme.txt').parent.mkdir(parents=True)
        (dest / 'readme.txt').write_bytes(FILES[path])
    record = 'record_quarantined' if way == 'quarantined' else 'record_verified'
    original = getattr(Mirror, record)
    in_the_way: list = []

    def recorded_then_obstructed(self, entry_id, identity, *args):
        original(self, entry_id, identity, *args)
        if identity.size != len(FILES[path]):
            return
        obstacle = folder / str(entry_id)
        obstacle.unlink(missing_ok=True)
        obstacle.mkdir()
        in_the_way.append(obstacle)

    monkeypatch.setattr(Mirror, record, recorded_then_obstructed)

    result = run_download('--json', *_partial_args(partial_root), 'disk:/Docs', str(dest))

    [obstacle] = in_the_way
    report = stderr_lines(result.stderr)
    assert f'Partial files not deleted: 1\n  {obstacle}: {os.strerror(errno.EISDIR)}' in '\n'.join(report)
    assert json.loads(result.stdout)['partials_left'] == [{'path': str(obstacle), 'reason': os.strerror(errno.EISDIR)}]
    notice = f'{path}: partial file {obstacle} not deleted: {os.strerror(errno.EISDIR)}; the outcome stands'
    assert notice in report
    assert notice in log_messages()
    assert 'Failed' not in result.stderr
    if way == 'quarantined':
        assert result.exit_code == 1
        assert '1 quarantined, 0 failed\n' in result.stderr
    else:
        assert result.exit_code == 0, result.output
        assert (dest / 'readme.txt').read_bytes() == FILES[path]
        assert ', 0 quarantined, 0 failed\n' in result.stderr
    assert _failure_records() == []
    assert obstacle.is_dir()


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
    # The commands install no signal handler outside the main thread, so the command runs as it would there.
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
            while sum(size >= 64 * 1024 for size in _partial_sizes(dest)) < len(slow):
                assert time.monotonic() < deadline, 'the slow files never received a chunk'
                time.sleep(0.01)
            failing.set()
            raise OSError(errno.EIO, os.strerror(errno.EIO), dst)
        rename_noreplace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(localfs, 'rename_noreplace', failing_once_the_others_have_bytes)

    result = run_download('disk:/Stop', str(dest))

    assert result.exit_code == 1
    assert stderr_lines(result.stderr) == [f'Error: Could not write {dest / "z.txt"}: {os.strerror(errno.EIO)}.']
    assert 'Mirrored' not in result.stdout
    assert len(failed) == len(slow)
    assert sorted(_journal()) == [(0, len(b'placed last'), 0, None)] + [(0, 1024 * 1024, 0, None)] * len(slow)
    assert len(_partials(dest)) == len(slow) + 1


@pytest.mark.parametrize(
    ('stop', 'exit_code', 'end_record'),
    [
        (signal.SIGINT, 130, 'download interrupted (exit 130)'),
        (signal.SIGTERM, 143, 'download interrupted by SIGTERM (exit 143)'),
    ],
    ids=['sigint', 'sigterm'],
)
def test_ctrl_c_or_sigterm_mid_file_checkpoints_every_byte_written_and_the_rerun_resumes_there(
    big, dest, stop, exit_code, end_record
):
    big.chunk_delay = 0.2
    child = start_command('download', 'disk:/Big', str(dest))
    try:
        wait_until(lambda: _partial_size(dest) > 256 * 1024, child, 'wrote 256 KiB')
        child.send_signal(stop)
        _, stderr = child.communicate(timeout=60)
    finally:
        child.kill()
        child.communicate()

    assert child.returncode == exit_code, stderr
    assert stderr_lines(stderr) == ['Interrupted; rerun to continue the download.']
    assert log_messages()[-1] == end_record
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
    assert stderr_lines(stderr) == ['Interrupted; rerun to continue the download.']
    assert len(_fetches(big, BIG_PATH)) == 2
    assert _partial_size(dest) == 300_000
    stalled = 'the download stalled: no data for 1 s (after 2 attempts)'
    assert _failure_records() == [(BIG_PATH, stalled, 1)]

    checked = runner.invoke(app, ['verify', str(dest)])

    assert checked.exit_code == 1
    assert f'Not downloaded: 1\n  {BIG_PATH}: {stalled}\n' in checked.stdout

    retry = run_download('--retry-failed', 'disk:/Big', str(dest))

    assert retry.exit_code == 0, retry.output
    assert f'Retrying 1 failed, quarantined, unfinished or broken file ({format_size(len(BIG))}) in {dest}\n' in (
        retry.stdout
    )
    assert (dest / 'big.bin').read_bytes() == BIG
    assert _failure_records() == []


def test_ctrl_c_between_passes_records_the_unavailable_reason_once_it_applies(dead, dest):
    assert _silent_run(dead, dest, DEAD_PATH, 6).exit_code == 1
    aged = age_silence(DEAD_PATH, 25)
    dead.inject('GET', '/download', 503, Delay(30), where={'path': DEAD_PATH})
    child = start_command('download', '--retry-failed', '--download-passes', '3', str(dest))
    try:
        wait_until(lambda: failure_record(DEAD_PATH)[1] == 7, child, 'recorded the deferred turn')
        time.sleep(1)
        child.send_signal(signal.SIGINT)
        _, stderr = child.communicate(timeout=60)
    finally:
        child.kill()
        child.communicate()

    assert child.returncode == 130, stderr
    assert stderr_lines(stderr)[-1] == 'Interrupted; rerun to continue the download.'
    assert len(_fetches(dead, DEAD_PATH)) == 7
    reason = f'{NEVER_STARTED} 7 attempts {_since(aged)} (the last: {SERVICE_UNAVAILABLE})'
    assert failure_record(DEAD_PATH) == (reason, 7, 7, aged)

    checked = runner.invoke(app, ['verify', str(dest)])

    assert checked.exit_code == 1
    assert f'Not downloaded: 1\n  {DEAD_PATH}: {reason}\n' in checked.stdout


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
    gate = _gated(monkeypatch, tmp_path, _FSYNC_GATE)
    child = start_command('download', 'disk:/Stop', str(dest))
    try:
        wait_until(
            lambda: sum(size >= 64 * 1024 for size in _partial_sizes(dest)) == len(slow),
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
    assert stderr_lines(stderr) == ['Interrupted; rerun to continue the download.']
    assert sorted(_journal()) == [(0, len(b'placed last'), 0, None)] + [(0, MiB, 0, None)] * len(slow)


# Installed as `sitecustomize` in a download subprocess: with `FSYNC_GATE` set to a folder, once the marker `go` exists
# there an fsync of a partial file creates a `blocked-<n>` marker and waits for the marker `release`, then runs.
_HOLDING_FSYNC_GATE = """
import itertools, os, pathlib, threading, time

_gate = pathlib.Path(os.environ['FSYNC_GATE'])
_fsync, _calls, _lock = os.fsync, itertools.count(1), threading.Lock()


def fsync(fd):
    if (_gate / 'go').exists() and '/.yandex-disk-rescue/partial/' in os.readlink(f'/proc/self/fd/{fd}'):
        with _lock:
            call = next(_calls)
        (_gate / f'blocked-{call}').touch()
        while not (_gate / 'release').exists():
            time.sleep(0.01)
    return _fsync(fd)


os.fsync = fsync
"""


@pytest.mark.parametrize(
    ('first', 'sigint_ignored', 'exit_code', 'logged'),
    [
        (signal.SIGTERM, True, 143, 'download interrupted by SIGTERM (exit 143)'),
        (signal.SIGINT, False, 130, 'download interrupted (exit 130)'),
    ],
    ids=['sigterm-in-a-background-job-ignoring-sigint', 'ctrl-c'],
)
def test_a_sigterm_while_the_run_stops_keeps_the_final_checkpoint(
    big, dest, tmp_path, monkeypatch, first, sigint_ignored, exit_code, logged
):
    big.chunk_delay = 0.2
    gate = _gated(monkeypatch, tmp_path, _HOLDING_FSYNC_GATE)
    child = start_command('download', 'disk:/Big', str(dest), sigint_ignored=sigint_ignored)
    try:
        wait_until(lambda: _partial_size(dest) > 256 * 1024, child, 'wrote 256 KiB')
        (gate / 'go').touch()
        child.send_signal(first)
        wait_until(lambda: any(gate.glob('blocked-*')), child, 'took its final checkpoint')
        child.send_signal(signal.SIGTERM)
        time.sleep(0.5)
        (gate / 'release').touch()
        _, stderr = child.communicate(timeout=60)
    finally:
        child.kill()
        child.communicate()
    monkeypatch.delenv('PYTHONPATH')

    assert child.returncode == exit_code, stderr
    assert stderr_lines(stderr) == ['Interrupted; rerun to continue the download.']
    assert log_messages()[-1] == logged
    held = _partial_size(dest)
    assert 256 * 1024 < held < len(BIG)
    assert _journal() == [(0, len(BIG), held, hashlib.sha256(BIG[:held]).hexdigest())]


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


def _own_partial_folder(dest) -> Path:
    return dest.resolve() / '.yandex-disk-rescue' / 'partial'


def _one_stream_headline(folder) -> str:
    return f'Partial files in {folder} cannot be sparse: new downloads are fetched as one stream each, not as segments.'


@pytest.mark.parametrize('partials', ['in-dest', 'separate'])
def test_a_partial_folder_without_sparse_files_gets_each_file_as_one_stream_without_zero_fill(
    segmented, dest, non_sparse, request, partials
):
    """DEST is on a filesystem without sparse files; with a partial folder, the partial files are on `/dev/shm`,
    which keeps them."""
    partial_root = request.getfixturevalue('partial_root') if partials == 'separate' else None
    zero_fill = non_sparse(dest)

    result = run_download('--json', *SEGMENTED_ARGS, *_partial_args(partial_root), 'disk:/Seg', str(dest))

    assert result.exit_code == 0, result.output
    assert (dest / 'large.bin').read_bytes() == SEGMENTED
    assert _journal() == []
    document = json.loads(result.stdout)
    notices = stderr_lines(result.stderr)
    if partial_root is None:
        folder = _own_partial_folder(dest)
        assert _fetches(segmented, SEGMENTED_PATH) == [None]
        assert zero_fill.written == {destination_module._SPARSE_PROBE_NAME: MiB}
        assert _one_stream_headline(folder) in notices
        assert notices.index(_one_stream_headline(folder)) > notices.index(f'Downloading 1 file (4.0 MiB) to {dest}')
        assert document['sparse_partials'] is False
        assert f'Partial folder {folder} keeps sparse files: no' in log_messages()
    else:
        folder = _separate_folder(dest, partial_root)
        assert sorted(_fetches(segmented, SEGMENTED_PATH)) == SEGMENT_RANGES
        assert zero_fill.written == {}
        assert not [line for line in notices if 'cannot be sparse' in line]
        assert document['sparse_partials'] is True
        assert f'Partial folder {folder} keeps sparse files: yes' in log_messages()


def test_the_partial_folder_probe_reports_sparse_files_and_leaves_nothing_behind(dest, non_sparse):
    with Destination.open(dest, partial_dir=None) as destination:
        assert destination.sparse_partials is True
        assert list(destination.partial_folder.iterdir()) == []
    non_sparse(dest)

    with Destination.open(dest, partial_dir=None) as destination:
        assert destination.sparse_partials is False
        assert list(destination.partial_folder.iterdir()) == []


PREVIEWS = PurePosixPath('.yandex-disk-rescue/previews')


def test_a_preview_is_saved_below_the_work_folder_replaced_by_a_newer_one_and_never_through_a_link(dest, tmp_path):
    with Destination.open(dest, partial_dir=None) as destination:
        path = destination.preview_path(PurePosixPath('Photos/2024/a.jpg'), 'image/jpeg')
        destination.save_preview(path, b'first')
        destination.save_preview(path, b'second')
        saved = sorted(p.relative_to(dest) for p in (dest / PREVIEWS).rglob('*'))
        content = (dest / path).read_bytes()

        outside = tmp_path / 'outside'
        outside.mkdir()
        shutil.rmtree(dest / PREVIEWS / 'Photos')
        (dest / PREVIEWS / 'Photos').symlink_to(outside)
        with pytest.raises(NotADirectoryError):
            destination.save_preview(path, b'third')

        folder_path = destination.preview_path(PurePosixPath('b.png'), 'image/png')
        (dest / folder_path).mkdir()
        with pytest.raises(FileExistsError):
            destination.save_preview(folder_path, b'png')

    assert path == PREVIEWS / 'Photos/2024/a.jpg.preview.jpg'
    assert saved == [PREVIEWS / 'Photos', PREVIEWS / 'Photos/2024', PREVIEWS / 'Photos/2024/a.jpg.preview.jpg']
    assert content == b'second'
    assert list(outside.iterdir()) == []
    assert (dest / folder_path).is_dir()
    assert list((dest / folder_path).iterdir()) == []
    assert sorted(p.name for p in (dest / PREVIEWS).iterdir()) == ['Photos', 'b.png.preview.png']


@pytest.mark.parametrize(
    ('content_type', 'ending'),
    [('image/jpeg', '.preview.jpg'), ('image/heic', '.preview.heic'), ('image/x-portable-pixmap', '.preview.img')],
)
def test_a_long_name_gets_a_shortened_preview_name_that_fits(content_type, ending):
    name = 'Я' * 120 + '.tiff'

    path = Destination.preview_path(PurePosixPath('Scans', name), content_type)

    assert path.parent == PREVIEWS / 'Scans'
    assert len(path.name.encode()) <= 255
    assert path.name.endswith(f'.tiff{ending}')
    assert path.name.startswith('Я' * 100)
    assert re.search(r'~[0-9a-f]{8}\.tiff\.preview', path.name)
    assert Destination.preview_path(PurePosixPath('Scans', name[1:]), content_type).name != path.name
    with pytest.raises(ValueError, match="a preview is an image, not 'text/html'"):
        Destination.preview_path(PurePosixPath('a.jpg'), 'text/html')


def test_removing_a_preview_deletes_the_folders_it_left_empty_but_not_previews(dest):
    with Destination.open(dest, partial_dir=None) as destination:
        deep = destination.preview_path(PurePosixPath('A/B/C/deep.jpg'), 'image/jpeg')
        beside = destination.preview_path(PurePosixPath('A/beside.jpg'), 'image/jpeg')
        top = destination.preview_path(PurePosixPath('top.jpg'), 'image/jpeg')
        for path in (deep, beside, top):
            destination.save_preview(path, b'jpeg')

        destination.remove_preview(deep)
        after_deep = sorted(str(p.relative_to(dest / PREVIEWS)) for p in (dest / PREVIEWS).rglob('*'))
        destination.remove_preview(deep)
        destination.remove_preview(destination.preview_path(PurePosixPath('X/Y/never.jpg'), 'image/jpeg'))
        destination.remove_preview(beside)
        destination.remove_preview(top)
        emptied = list((dest / PREVIEWS).iterdir())

        destination.save_preview(top, b'jpeg')
        (dest / top).unlink()
        (dest / top).mkdir()
        with pytest.raises(FileExistsError):
            destination.remove_preview(top)

    assert after_deep == ['A', 'A/beside.jpg.preview.jpg', 'top.jpg.preview.jpg']
    assert emptied == []
    assert (dest / top).is_dir()


def test_a_probe_error_in_the_separate_partial_folder_names_that_folder(disk, dest, partial_root, monkeypatch):
    folder = _separate_folder(dest, partial_root)
    ftruncate = os.ftruncate

    def full(fd: int, length: int) -> None:
        if _below(os.readlink(f'/proc/self/fd/{fd}'), folder):
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        ftruncate(fd, length)

    monkeypatch.setattr(os, 'ftruncate', full)

    result = run_download('--partial-dir', str(partial_root), str(dest))

    assert result.exit_code == 1
    assert stderr_lines(result.stderr) == [f'Error: Could not write {folder}: {os.strerror(errno.ENOSPC)}.']
    assert _transfers(disk) == []
    assert list(folder.iterdir()) == []


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


def _leave_empty_segmented_journal(fake, dest) -> None:
    """Run a segmented download of `SEGMENTED_PATH` whose 4 segments all get HTTP 503, with no later pass: its journal
    keeps 4 segments confirming no byte."""
    fake.inject('GET', '/download', 503, 503, 503, 503, where={'path': SEGMENTED_PATH})
    result = run_download(*SEGMENTED_ARGS, '--download-passes', '1', 'disk:/Seg', str(dest))
    assert result.exit_code == 1, result.output
    assert _journal() == [(n * MiB, (n + 1) * MiB, n * MiB, None) for n in range(4)]


@pytest.mark.parametrize(
    ('segments', 'fetched'), [('1', [None]), ('2', [f'bytes=0-{2 * MiB - 1}', f'bytes={2 * MiB}-{4 * MiB - 1}'])]
)
def test_a_segmented_download_that_kept_no_byte_starts_again_with_the_current_segments(
    segmented, dest, segments, fetched
):
    _leave_empty_segmented_journal(segmented, dest)
    earlier = len(segmented.requests)

    result = run_download(*SEGMENTED_ARGS, '--download-segments', segments, 'disk:/Seg', str(dest))

    assert result.exit_code == 0, result.output
    assert sorted(_fetches(segmented, SEGMENTED_PATH, earlier), key=str) == fetched
    assert (dest / 'large.bin').read_bytes() == SEGMENTED


def test_an_empty_segmented_journal_in_a_folder_without_sparse_files_is_fetched_as_one_stream(
    segmented, dest, non_sparse
):
    _leave_empty_segmented_journal(segmented, dest)
    zero_fill = non_sparse(dest)
    earlier = len(segmented.requests)

    result = run_download(*SEGMENTED_ARGS, 'disk:/Seg', str(dest))

    assert result.exit_code == 0, result.output
    assert _fetches(segmented, SEGMENTED_PATH, earlier) == [None]
    assert zero_fill.written == {destination_module._SPARSE_PROBE_NAME: MiB}
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


def test_a_segmented_file_whose_link_refresh_keeps_failing_waits_for_the_next_pass(segmented, dest):
    for segment in SEGMENT_RANGES:
        segmented.inject('GET', '/download', 410, where={'path': SEGMENTED_PATH}, where_headers={'range': segment})
    segmented.inject('GET', '/v1/disk/resources/download', Delay(0), 500, 500, where={'path': SEGMENTED_PATH})

    result = run_download('--api-retries', '1', *SEGMENTED_ARGS, 'disk:/Seg', str(dest))

    assert result.exit_code == 0, result.output
    assert 'Passes: 2 of 7\n' in result.stdout
    assert (dest / 'large.bin').read_bytes() == SEGMENTED
    fetches = [i for i, r in enumerate(segmented.requests) if r.path == '/download']
    assert len(fetches) == 8
    assert _linked(segmented) == [SEGMENTED_PATH] * 4
    links = [i for i, r in enumerate(segmented.requests) if r.path == '/v1/disk/resources/download']
    # The first link, its 4 rejected fetches, 2 attempts at a refresh, then the link and the fetches of pass 2.
    assert links[0] < fetches[0] and fetches[3] < links[1] < links[2] < links[3] < fetches[4]
    assert _account_checks(segmented) == 2


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
    fake,
    root: str,
    dest,
    policy: mirroring.TransferPolicy = POLICY,
    *,
    partial_dir: Path | None = None,
    progress: MirrorProgress | None = None,
) -> AsyncIterator[Callable[[], Awaitable[MirrorReport]]]:
    """Scan `root` in this event loop, then yield a call running the mirror run of `root` into `dest` here too, its
    partial files below `partial_dir` when given, with real clients, the real manifest and a real destination, fetching
    as `policy` says and reporting to `progress`."""
    async with (
        api.DiskClient(TOKEN, base_url=fake.base_url, limits=FAST_API) as client,
        api.ContentClient(timeout=5, connections=20) as content,
    ):
        account = await client.account()
        scan_root = await resolve_scan_root(client, root)
        with Manifest.open(manifest_path()) as manifest, Destination.open(dest, partial_dir=partial_dir) as destination:
            await scan_tree(client, manifest, account, scan_root, ScanProgress())
            state = progress or MirrorProgress()
            run = MirrorRun(
                manifest.mirror(account.uid, scan_root.path, destination.path),
                destination,
                retry_failed=False,
                progress=state,
            )

            async def mirrored() -> MirrorReport:
                await run.move_partials(MoveProgress())
                state.start_transferring(run.pending_files, run.pending_size, passes=1)
                return await run.run(client, content, policy, passes=1, room_measured=lambda _: None)

            yield mirrored


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
    held = len(written[0]) + len(written[1])
    assert partial.read_bytes() == BIG[:held]
    assert _journal() == [(0, len(BIG), held, hashlib.sha256(BIG[:held]).hexdigest())]


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
