"""Filesystem locations used by the tool, following the XDG Base Directory specification."""

import os
from pathlib import Path

APP_DIR_NAME = 'yandex-disk-rescue'
MANIFEST_FILE_NAME = 'manifest.sqlite3'
LOG_FILE_NAME = 'yandex-disk-rescue.log'


def config_dir() -> Path:
    """Return `$XDG_CONFIG_HOME/yandex-disk-rescue` (default `~/.config/yandex-disk-rescue`) without creating it."""
    return _xdg_dir('XDG_CONFIG_HOME', '.config')


def state_dir() -> Path:
    """Return `$XDG_STATE_HOME/yandex-disk-rescue` (default `~/.local/state/yandex-disk-rescue`) without creating it."""
    return _xdg_dir('XDG_STATE_HOME', '.local/state')


def manifest_path() -> Path:
    """Return the manifest database path in the state directory, without creating anything."""
    return state_dir() / MANIFEST_FILE_NAME


def log_path() -> Path:
    """Return the log file path in the state directory, without creating anything."""
    return state_dir() / LOG_FILE_NAME


def _xdg_dir(env_var: str, default_under_home: str) -> Path:
    # The XDG spec says unset, empty and relative values must all be ignored in favour of the default.
    value = os.environ.get(env_var, '')
    base = Path(value) if os.path.isabs(value) else Path.home() / default_under_home
    return base / APP_DIR_NAME
