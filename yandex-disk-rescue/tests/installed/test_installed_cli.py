"""End-to-end tests of the `yandex-disk-rescue` program found on `PATH`, run in child processes against the fake API.

They import only the standard library, pytest and `fake_disk_server`, never `yandex_disk_rescue`: they also run
against an installed wheel in a container that holds no source tree. The child's environment is built from scratch,
so the developer's token, config and state never reach it.
"""

import json
import os
import random
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_disk_server import FakeDiskApi, serve

TOKEN = 'y0_AgAAAAB-installed-sentinel-token'
MiB = 1024 * 1024
SEGMENTS = 4
SEGMENTED_PATH = 'disk:/Video/big.bin'
FILES = {
    'disk:/Photos/2024/a.jpg': random.Random(1).randbytes(70_000),
    'disk:/Docs/Отчёт за 2024 год.txt': 'годовой отчёт'.encode(),
    'disk:/top.txt': b'',
    SEGMENTED_PATH: random.Random(3).randbytes(3 * MiB),
}
EMPTY_FOLDER = 'disk:/Empty'
DOWNLOAD_ARGS = (
    'download',
    '--json',
    '--download-segment-threshold-mib',
    '1',
    '--download-segments',
    str(SEGMENTS),
    'disk:/',
)


@pytest.fixture
def disk() -> Iterator[FakeDiskApi]:
    with serve() as api:
        api.token = TOKEN
        for path, content in FILES.items():
            api.add_file(path, content)
        api.add_dir(EMPTY_FOLDER)
        yield api


@pytest.fixture
def env(tmp_path: Path, disk: FakeDiskApi) -> dict[str, str]:
    home = tmp_path / 'home'
    home.mkdir()
    return {
        'PATH': os.environ['PATH'],
        'HOME': str(home),
        'XDG_CONFIG_HOME': str(home / '.config'),
        'XDG_STATE_HOME': str(home / '.local' / 'state'),
        'YANDEX_DISK_RESCUE_TOKEN': TOKEN,
        'YANDEX_DISK_RESCUE_API_URL': disk.base_url,
        'YANDEX_DISK_RESCUE_API_INTERVAL_MS': '25',
    }


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ['yandex-disk-rescue', *args], capture_output=True, text=True, env=env, timeout=120, check=False
    )
    assert result.returncode == 0, f'{args} exited with {result.returncode}: {result.stderr}'
    return result


def _version(env: dict[str, str]) -> str:
    stdout = _run(env, '--version').stdout
    match = re.fullmatch(r'yandex-disk-rescue (\S+)\n', stdout)
    assert match, stdout
    return match.group(1)


def test_version_prints_the_program_name_and_its_version(env):
    _version(env)


def test_help_lists_the_commands(env):
    stdout = _run(env, '--help').stdout

    for command in ('scan', 'download', 'verify', 'auth'):
        assert command in stdout


def test_auth_status_names_the_account(env, disk):
    stdout = _run(env, 'auth', 'status').stdout

    assert disk.account['user']['login'] in stdout


def test_a_download_mirrors_every_file_a_rerun_downloads_nothing_and_verify_finds_every_file_intact(
    env, disk, tmp_path
):
    dest = tmp_path / 'dest'
    version = _version(env)

    first = json.loads(_run(env, *DOWNLOAD_ARGS, str(dest)).stdout)

    assert first['version'] == version
    assert first['counts']['downloaded'] == len(FILES)
    assert first['counts']['failed'] == first['counts']['quarantined'] == 0
    for path, content in FILES.items():
        assert (dest / path.removeprefix('disk:/')).read_bytes() == content, path
    assert (dest / EMPTY_FOLDER.removeprefix('disk:/')).is_dir()
    ranges = [r.headers.get('range') for r in disk.requests if r.params.get('path') == SEGMENTED_PATH]
    assert len([r for r in ranges if r is not None]) == SEGMENTS, ranges

    rerun = json.loads(_run(env, *DOWNLOAD_ARGS, str(dest)).stdout)

    assert rerun['counts']['downloaded'] == 0
    assert rerun['counts']['skipped'] == len(FILES)

    verify = json.loads(_run(env, 'verify', '--json', str(dest)).stdout)

    assert verify['counts']['intact'] == len(FILES)
    assert verify['counts']['problems'] == 0
