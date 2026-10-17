"""The Disk tree the `download` and `verify` tests mirror (the `disk` fixture in `conftest.py` serves it), and helpers
to run `download` and look at its destination."""

import hashlib
from datetime import UTC, datetime

from typer.testing import CliRunner
from yandex_disk_rescue.cli import app

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
