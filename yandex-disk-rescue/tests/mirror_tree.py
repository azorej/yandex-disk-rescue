"""The Disk tree the `download` and `verify` tests mirror (the `disk` fixture in `conftest.py` serves it), and helpers
to run `download`, run a command in a child process, look at its destination, and read the log file."""

import hashlib
import os
import re
import sqlite3
import subprocess
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner
from yandex_disk_rescue.cli import app
from yandex_disk_rescue.paths import log_path, manifest_path

runner = CliRunner()

TOKEN = 'y0_AgAAAAB-download-sentinel-token'
CYRILLIC = 'disk:/Docs/Отчёт за 2024 год.txt'
MODIFIED = datetime(2023, 2, 3, 4, 5, 6, 789012, tzinfo=UTC)
FILES = {
    'disk:/Photos/2024/a.jpg': b'a' * 70_000,
    'disk:/Photos/2024/b.jpg': b'b' * 3000,
    'disk:/Photos/old.bmp': b'bitmap',
    'disk:/Docs/readme.txt': b'read me',
    CYRILLIC: 'годовой отчёт'.encode(),
    'disk:/top.txt': b'',
}
SIZE_ONLY = 'disk:/Photos/old.bmp'


def run_download(*args: str):
    return runner.invoke(app, ['download', *args])


def start_command(command: str, *args: str, sigint_ignored: bool = False) -> subprocess.Popen[str]:
    """Start `yandex-disk-rescue <command> <args>` in a child process with this process's environment; with
    `sigint_ignored`, as a shell script starts a background job: with SIGINT ignored."""
    argv = ['yandex-disk-rescue', command, *args]
    if sigint_ignored:
        argv = ['sh', '-c', 'trap "" INT; exec "$0" "$@"', *argv]
    return subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )


def _command(child: subprocess.Popen[str]) -> str:
    """The command `start_command` started `child` with."""
    args = list(child.args)
    return args[args.index('yandex-disk-rescue') + 1]


def wait_until(condition, child: subprocess.Popen[str], what: str) -> None:
    """Wait up to 60 s for `condition()`, failing the test if `child` exits first."""
    deadline = time.monotonic() + 60
    while not condition():
        if child.poll() is not None:
            pytest.fail(f'{_command(child)} exited with {child.returncode} before it {what}: {child.communicate()}')
        if time.monotonic() > deadline:
            pytest.fail(f'{_command(child)} never {what}')
        time.sleep(0.01)


def failure_records() -> dict[str, tuple[str, int, int, datetime | None]]:
    """The (reason, failed turns, silent turns, start of the silent streak) of every failure record in the manifest, by
    remote path in path order, read with a connection of its own."""
    with closing(sqlite3.connect(f'{manifest_path().as_uri()}?mode=ro', uri=True)) as connection:
        rows = connection.execute(
            'SELECT e.path, f.reason, f.turns, f.silent_turns, f.silent_since FROM failures f'
            ' JOIN entries e ON e.id = f.entry_id ORDER BY e.path'
        ).fetchall()
    return {
        path: (reason, turns, silent_turns, None if since is None else datetime.fromisoformat(since))
        for path, reason, turns, silent_turns, since in rows
    }


def failure_record(path: str) -> tuple[str, int, int, datetime | None]:
    """The failure record of `path`, as `failure_records` gives it."""
    return failure_records()[path]


def age_silence(path: str, hours: float) -> datetime:
    """Move the start of the silent streak in the failure record of `path` back by `hours`, in the manifest, as if
    that much time had passed since; the new start."""
    *_, since = failure_record(path)
    assert since is not None, f'{path} has no silent streak'
    aged = since - timedelta(hours=hours)
    with closing(sqlite3.connect(manifest_path())) as connection, connection:
        connection.execute(
            'UPDATE failures SET silent_since = ? WHERE entry_id = (SELECT id FROM entries WHERE path = ?)',
            (aged.astimezone(UTC).isoformat(timespec='microseconds'), path),
        )
    return aged


def local_file(dest, remote_path: str):
    """The path in `dest` that mirrors `remote_path` of a mirror of `disk:/`."""
    return dest / remote_path.removeprefix('disk:/')


def quarantine_runs(dest) -> list:
    folder = dest / '.yandex-disk-rescue' / 'quarantine'
    return sorted(folder.iterdir()) if folder.is_dir() else []


def replaced_runs(dest) -> list:
    return sorted((dest / '.yandex-disk-rescue' / 'replaced').iterdir())


def corrupted_bytes(content: bytes, offset: int) -> tuple[bytes, str]:
    """`content` with the byte at `offset` flipped, as the fake serves a corrupted file, and the MD5 mismatch reason
    those bytes fail with."""
    flipped = bytearray(content)
    flipped[offset] ^= 0xFF
    good, bad = hashlib.md5(content).hexdigest(), hashlib.md5(flipped).hexdigest()
    return bytes(flipped), f'MD5 mismatch (expected {good}, got {bad})'


LOG_LINE = re.compile(
    r'(?P<time>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z) (?P<level>[A-Z]+) (?P<command>[a-z]+)\[(?P<pid>\d+)\] '
)


def log_messages(path=None) -> list[str]:
    """The message of every line of the log file (default: the current one), each line checked for its prefix."""
    lines = (path or log_path()).read_text(encoding='utf-8').splitlines()
    for line in lines:
        assert LOG_LINE.match(line), line
    return [LOG_LINE.sub('', line, count=1) for line in lines]


NOTICE_TIMESTAMP = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z ')
STATUS_LINE = re.compile(
    NOTICE_TIMESTAMP.pattern
    + r'(?:(?:Scanning|Scanned) \S.*: \d+ files?, \d+ folders? \(.*\) listed'
    + r'|Checking \d+ files? already in |Checked \d+ of \d+ files? already in '
    + r'|(?:Moving|Moved|Downloading|Downloaded|Verifying|Verified): \d+ of \d+ files?, )'
)


def stderr_lines(stderr: str) -> list[str]:
    """The lines of a command's stderr but its status lines, each notice without its timestamp: the notices, then
    the result line."""
    return [NOTICE_TIMESTAMP.sub('', line, count=1) for line in stderr.splitlines() if not STATUS_LINE.match(line)]


def status_lines(stderr: str) -> list[str]:
    """The status lines of a command's stderr, without their timestamps."""
    return [NOTICE_TIMESTAMP.sub('', line, count=1) for line in stderr.splitlines() if STATUS_LINE.match(line)]


# Installed as `sitecustomize` in a child process: every `pread` sleeps 50 ms first, and leaves a marker named after
# the inode it reads in the folder `READ_GATE`.
_SLOW_READS = """
import os, pathlib, time

_gate, _pread = pathlib.Path(os.environ['READ_GATE']), os.pread


def pread(fd, size, offset):
    (_gate / str(os.fstat(fd).st_ino)).touch()
    time.sleep(0.05)
    return _pread(fd, size, offset)


os.pread = pread
"""


def slow_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make every `pread` of the child processes started from now on sleep 50 ms first and leave a marker named after
    the inode it reads in the folder returned."""
    site, gate = tmp_path / 'site', tmp_path / 'gate'
    site.mkdir()
    gate.mkdir()
    (site / 'sitecustomize.py').write_text(_SLOW_READS)
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join(filter(None, [str(site), os.environ.get('PYTHONPATH')])))
    monkeypatch.setenv('READ_GATE', str(gate))
    return gate
