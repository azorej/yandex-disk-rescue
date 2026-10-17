"""Shared fixtures for the yandex-disk-rescue tests."""

import os

import pytest


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Keep tests away from the developer's real token, config and the `.env` that mise loads."""
    for name in list(os.environ):
        if name.startswith('YANDEX_DISK_RESCUE_'):
            monkeypatch.delenv(name)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg-config'))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'xdg-state'))
