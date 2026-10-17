"""The Yandex OAuth token: the authorization URL to obtain one, its storage, resolution and header-safety check."""

import enum
import os
import stat
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from yandex_disk_rescue.errors import RescueError
from yandex_disk_rescue.paths import config_dir

TOKEN_ENV_VAR = 'YANDEX_DISK_RESCUE_TOKEN'
REQUESTED_SCOPES = ('cloud_api:disk.read', 'cloud_api:disk.info')
AUTHORIZE_ENDPOINT = 'https://oauth.yandex.ru/authorize'


class NoTokenError(RescueError):
    pass


class InvalidTokenError(RescueError):
    pass


class InsecureTokenFileError(RescueError):
    pass


class TokenFileError(RescueError):
    pass


class TokenSource(enum.Enum):
    ENV = 'env'
    FILE = 'file'


@dataclass(frozen=True)
class Token:
    value: str = field(repr=False)
    source: TokenSource


def authorize_url(client_id: str) -> str:
    """Return the Yandex ID implicit-flow URL that shows a token for `client_id` after the user consents.

    Only `REQUESTED_SCOPES` are asked for, so the app must have both rights registered. Yandex expects
    the scopes space-separated and rejects commas with `invalid_scope`; the space is sent as `%20`.
    """
    query = urllib.parse.urlencode(
        {'response_type': 'token', 'client_id': client_id, 'scope': ' '.join(REQUESTED_SCOPES)},
        quote_via=urllib.parse.quote,
    )
    return f'{AUTHORIZE_ENDPOINT}?{query}'


def token_from_paste(pasted: str) -> str:
    """Return the token from what the user pasted: the bare token or a redirect URL with `access_token=`.

    Raises `InvalidTokenError` when the result is empty or contains whitespace or non-ASCII characters.
    """
    text = pasted.strip()
    if 'access_token=' in text:
        params = urllib.parse.parse_qs(text[text.index('access_token=') :])
        if 'access_token' not in params:
            raise InvalidTokenError('The pasted URL has an empty access_token parameter.')
        text = params['access_token'][0]
    return validate_token(text)


def token_path() -> Path:
    return config_dir() / 'token'


def resolve_token() -> Token:
    """Return the token from the environment variable, else from the token file.

    The value is stripped and must be usable in an HTTP header. Raises `NoTokenError` when neither
    provides one (a set-but-blank variable is an error, not a fallback to the file), `InvalidTokenError`
    when the value has whitespace or non-printable-ASCII characters (the message names the source, never
    the value), `InsecureTokenFileError` when the file is readable by group or others, and
    `TokenFileError` when the path is not a regular file or cannot be read.
    """
    env_value = os.environ.get(TOKEN_ENV_VAR)
    if env_value is not None:
        if not env_value.strip():
            raise NoTokenError(f'{TOKEN_ENV_VAR} is set but empty. Unset it or set it to a Yandex OAuth token.')
        if defect := _token_defect(env_value.strip()):
            raise InvalidTokenError(f'{TOKEN_ENV_VAR} is not a valid token: the token {defect}.')
        return Token(value=env_value.strip(), source=TokenSource.ENV)

    path = token_path()
    value = _read_token_file(path)
    if not value:
        raise NoTokenError(f"Token file {path} is empty. Run 'yandex-disk-rescue auth login' or set {TOKEN_ENV_VAR}.")
    if defect := _token_defect(value):
        raise InvalidTokenError(f'Token file {path} does not hold a valid token: the token {defect}.')
    return Token(value=value, source=TokenSource.FILE)


def _read_token_file(path: Path) -> str:
    # O_NONBLOCK keeps a FIFO at the token path from blocking the open; it has no effect on a regular file.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        raise NoTokenError(
            f"No Yandex OAuth token. Run 'yandex-disk-rescue auth login' or set {TOKEN_ENV_VAR}."
        ) from None
    except OSError as exc:
        raise TokenFileError(f'Cannot read token file {path}: {exc.strerror}.') from None
    mode = os.fstat(fd).st_mode
    if not stat.S_ISREG(mode):
        os.close(fd)
        raise TokenFileError(f'Token file {path} is not a regular file.')
    if stat.S_IMODE(mode) & 0o077:
        os.close(fd)
        raise InsecureTokenFileError(f'Token file {path} is accessible by other users; run: chmod 600 {path}')
    with os.fdopen(fd, encoding='utf-8') as token_file:
        try:
            return token_file.read().strip()
        except UnicodeDecodeError:
            raise InvalidTokenError(f'Token file {path} is not valid UTF-8 text.') from None
        except OSError as exc:
            raise TokenFileError(f'Cannot read token file {path}: {exc.strerror}.') from None


def get_token() -> str:
    return resolve_token().value


def save_token(value: str) -> Path:
    """Store the token atomically as a 0600 file in a 0700 config directory and return its path.

    An existing config directory is narrowed to 0700; its parents are left as they are. Raises
    `InvalidTokenError` for an empty value, internal whitespace or non-ASCII characters and
    `TokenFileError` when the directory or file cannot be written; no temporary file is left behind.
    """
    token = validate_token(value)
    path = token_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        _write_atomically(path, token + '\n')
        _fsync_dir(path.parent)
    except OSError as exc:
        raise TokenFileError(f'Could not save the token to {path}: {exc.strerror}.') from None
    return path


def _write_atomically(path: Path, content: str) -> None:
    # mkstemp creates the file with mode 0600, so the token is never on disk with wider permissions.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix='.token-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as tmp_file:
            tmp_file.write(content)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _token_defect(token: str) -> str | None:
    """Return why a stripped token cannot be sent in an `Authorization` header, as a predicate for "the token …"."""
    if not token:
        return 'is empty'
    if any(char.isspace() for char in token):
        return 'must not contain whitespace'
    if not (token.isascii() and token.isprintable()):
        return 'must contain only printable ASCII characters'
    return None


def validate_token(value: str) -> str:
    """Return the stripped token, or raise `InvalidTokenError` (without the value) if it cannot be sent in a header."""
    token = value.strip()
    if defect := _token_defect(token):
        raise InvalidTokenError(f'The token {defect}.')
    return token


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
