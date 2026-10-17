"""Storage and resolution of the Yandex OAuth token."""

import enum
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from yandex_disk_rescue.errors import RescueError
from yandex_disk_rescue.paths import config_dir

TOKEN_ENV_VAR = 'YANDEX_DISK_RESCUE_TOKEN'


class NoTokenError(RescueError):
    pass


class InvalidTokenError(RescueError):
    pass


class InsecureTokenFileError(RescueError):
    pass


class TokenSource(enum.Enum):
    ENV = 'env'
    FILE = 'file'


@dataclass(frozen=True)
class Token:
    value: str = field(repr=False)
    source: TokenSource


def token_path() -> Path:
    return config_dir() / 'token'


def resolve_token() -> Token:
    """Return the token from the environment variable, else from the token file.

    Raises `NoTokenError` when neither provides one (a set-but-blank variable is an error, not a
    fallback to the file) and `InsecureTokenFileError` when the file is readable by group or others.
    """
    env_value = os.environ.get(TOKEN_ENV_VAR)
    if env_value is not None:
        if not env_value.strip():
            raise NoTokenError(f'{TOKEN_ENV_VAR} is set but empty. Unset it or set it to a Yandex OAuth token.')
        return Token(value=env_value.strip(), source=TokenSource.ENV)

    path = token_path()
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        raise NoTokenError(
            f"No Yandex OAuth token. Run 'yandex-disk-rescue auth login' or set {TOKEN_ENV_VAR}."
        ) from None
    with os.fdopen(fd, encoding='utf-8') as token_file:
        if stat.S_IMODE(os.fstat(token_file.fileno()).st_mode) & 0o077:
            raise InsecureTokenFileError(f'Token file {path} is accessible by other users; run: chmod 600 {path}')
        value = token_file.read().strip()
    if not value:
        raise NoTokenError(f"Token file {path} is empty. Run 'yandex-disk-rescue auth login' or set {TOKEN_ENV_VAR}.")
    return Token(value=value, source=TokenSource.FILE)


def get_token() -> str:
    return resolve_token().value


def save_token(value: str) -> Path:
    """Store the token atomically as a 0600 file in a 0700 config directory and return its path.

    Raises `InvalidTokenError` for an empty value or one with internal whitespace.
    """
    token = value.strip()
    if not token:
        raise InvalidTokenError('The token is empty.')
    if any(char.isspace() for char in token):
        raise InvalidTokenError('The token must not contain whitespace.')

    path = token_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # mkstemp creates the file with mode 0600, so the token is never on disk with wider permissions.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix='.token-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as tmp_file:
            tmp_file.write(token + '\n')
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)
    return path


def delete_token() -> bool:
    """Delete the token file; return False when there was none."""
    try:
        token_path().unlink()
    except FileNotFoundError:
        return False
    return True


def _fsync_dir(directory: Path) -> None:
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
