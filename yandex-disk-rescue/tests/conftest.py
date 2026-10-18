"""Shared fixtures for the yandex-disk-rescue tests."""

import os
import shutil
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_disk_server import FakeDiskApi, serve
from mirror_tree import FILES, MODIFIED, SIZE_ONLY, TOKEN
from yandex_disk_rescue import mirroring


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Keep tests away from the developer's real token, config and the `.env` that mise loads."""
    for name in list(os.environ):
        if name.startswith('YANDEX_DISK_RESCUE_'):
            monkeypatch.delenv(name)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg-config'))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'xdg-state'))


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture
def fake_disk_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiskApi]:
    """A real HTTP server on 127.0.0.1 standing in for the Yandex Disk API, wired in via YANDEX_DISK_RESCUE_API_URL."""
    with serve() as api:
        monkeypatch.setenv('YANDEX_DISK_RESCUE_API_URL', api.base_url)
        yield api


@pytest.fixture
def closed_port_url() -> str:
    """The URL of a 127.0.0.1 port nothing listens on, so a connection to it is refused."""
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    return f'http://127.0.0.1:{port}'


@pytest.fixture
def disk(fake_disk_api, monkeypatch):
    """The fake API serving `mirror_tree.FILES`: a nested tree, an empty folder, a size-only file, a zero-byte file and
    a Cyrillic name."""
    monkeypatch.setenv('YANDEX_DISK_RESCUE_TOKEN', TOKEN)
    monkeypatch.setenv('YANDEX_DISK_RESCUE_API_INTERVAL_MS', '25')
    fake_disk_api.token = TOKEN
    for path, content in FILES.items():
        checksums = {'md5': None, 'sha256': None} if path == SIZE_ONLY else {}
        fake_disk_api.add_file(path, content, modified=MODIFIED, **checksums)
    fake_disk_api.add_dir('disk:/Empty')
    return fake_disk_api


@pytest.fixture
def dest(tmp_path):
    return tmp_path / 'dest'


@pytest.fixture
def partial_root(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A folder on `/dev/shm`, a filesystem other than the one `tmp_path` (and so `dest`) is on, removed afterwards.

    `/dev/shm` is far smaller than `mirroring.PARTIAL_RESERVE`, so runs in this process keep no reserve there.
    """
    monkeypatch.setattr(mirroring, 'PARTIAL_RESERVE', 0)
    root = Path(tempfile.mkdtemp(dir='/dev/shm'))
    try:
        assert root.stat().st_dev != tmp_path.stat().st_dev, '/dev/shm must be a filesystem of its own'
        yield root
    finally:
        shutil.rmtree(root)
