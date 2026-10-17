"""Shared fixtures for the yandex-disk-rescue tests."""

import os
import socket
from collections.abc import Iterator

import pytest
from fake_disk_server import FakeDiskApi, start_fake_disk_server


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
    api, server, thread = start_fake_disk_server()
    monkeypatch.setenv('YANDEX_DISK_RESCUE_API_URL', api.base_url)
    try:
        yield api
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def closed_port_url() -> str:
    """The URL of a 127.0.0.1 port nothing listens on, so a connection to it is refused."""
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    return f'http://127.0.0.1:{port}'
