"""Filesystem locations used by the tool, following the XDG Base Directory specification."""

import os
from pathlib import Path

APP_DIR_NAME = 'yandex-disk-rescue'


def config_dir() -> Path:
    """Return the configuration directory without creating it.

    `$XDG_CONFIG_HOME/yandex-disk-rescue`, or `~/.config/yandex-disk-rescue` when the variable is
    unset, empty or not an absolute path (the XDG spec says relative values must be ignored).
    """
    xdg_config_home = os.environ.get('XDG_CONFIG_HOME', '')
    base = Path(xdg_config_home) if os.path.isabs(xdg_config_home) else Path.home() / '.config'
    return base / APP_DIR_NAME
