"""The Disk tree the `download` and `verify` tests mirror (the `disk` fixture in `conftest.py` serves it), and helpers
to run `download`, run a command in a child process, look at its destination, and read the log file."""

import hashlib
import os
import re
import subprocess
import time
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner
from yandex_disk_rescue.cli import app
from yandex_disk_rescue.paths import log_path

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


def start_command(command: str, *args: str) -> subprocess.Popen[str]:
    """Start `yandex-disk-rescue <command> <args>` in a child process with this process's environment."""
    return subprocess.Popen(
        ['yandex-disk-rescue', command, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )


def wait_until(condition, child: subprocess.Popen[str], what: str) -> None:
    """Wait up to 60 s for `condition()`, failing the test if `child` exits first."""
    deadline = time.monotonic() + 60
    while not condition():
        if child.poll() is not None:
            pytest.fail(f'{child.args[1]} exited with {child.returncode} before it {what}: {child.communicate()}')
        if time.monotonic() > deadline:
            pytest.fail(f'{child.args[1]} never {what}')
        time.sleep(0.01)


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


def stderr_lines(stderr: str) -> list[str]:
    """The lines of a command's stderr, each notice without its timestamp: the notices, then the result line."""
    return [NOTICE_TIMESTAMP.sub('', line, count=1) for line in stderr.splitlines()]
