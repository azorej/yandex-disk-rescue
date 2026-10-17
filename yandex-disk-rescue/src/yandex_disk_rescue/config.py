"""The config file: TOML with flat top-level keys named after the options in `settings.SETTINGS`."""

import tomllib
from pathlib import Path
from typing import Any

from yandex_disk_rescue import paths, settings
from yandex_disk_rescue.errors import RescueError

CONFIG_FILE_NAME = 'config.toml'
CONFIG_ENV_VAR = settings.ENV_VAR_PREFIX + 'CONFIG'


class ConfigError(RescueError):
    """The config file is missing (when named explicitly), unreadable, not TOML, or has a bad key or value."""

    exit_code = 2

    def __init__(self, path: Path, reason: str, *, key: str | None = None) -> None:
        where = f'config file {path}' if key is None else f'config file {path}: {key}'
        super().__init__(f'{where}: {reason}')


def default_config_path() -> Path:
    return paths.config_dir() / CONFIG_FILE_NAME


def load_config(path: Path | None) -> dict[str, Any]:
    """Read and validate the option values in the config file at `path`, or at the default path when it is None.

    A missing file at the default path means no values; a missing file named explicitly is an error.
    """
    explicit = path is not None
    path = path if path is not None else default_config_path()
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        if not explicit:
            return {}
        raise ConfigError(path, 'does not exist.') from None
    except OSError as err:
        raise ConfigError(path, f'cannot be read: {err.strerror}.') from None
    try:
        values = tomllib.loads(data.decode())
    except UnicodeDecodeError:
        raise ConfigError(path, 'is not UTF-8 text.') from None
    except tomllib.TOMLDecodeError as err:
        raise ConfigError(path, f'is not valid TOML: {err}.') from None
    for key, value in values.items():
        setting = settings.SETTINGS.get(key)
        if setting is None:
            raise ConfigError(path, f'unknown option; the options are {", ".join(settings.SETTINGS)}.', key=key)
        if (reason := setting.reason_invalid(value)) is not None:
            raise ConfigError(path, reason, key=key)
    return values
