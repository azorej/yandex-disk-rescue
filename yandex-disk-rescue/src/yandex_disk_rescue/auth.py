"""Storage and resolution of the Yandex OAuth token."""

import enum
import json
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from yandex_disk_rescue.errors import RescueError
from yandex_disk_rescue.paths import config_dir

TOKEN_ENV_VAR = 'YANDEX_DISK_RESCUE_TOKEN'
API_URL_ENV_VAR = 'YANDEX_DISK_RESCUE_API_URL'
DEFAULT_API_URL = 'https://cloud-api.yandex.net'
API_TIMEOUT_SECONDS = 30.0


class NoTokenError(RescueError):
    pass


class InvalidTokenError(RescueError):
    pass


class InsecureTokenFileError(RescueError):
    pass


class TokenRejectedError(RescueError):
    pass


class DiskApiError(RescueError):
    pass


class TokenSource(enum.Enum):
    ENV = 'env'
    FILE = 'file'


@dataclass(frozen=True)
class Token:
    value: str = field(repr=False)
    source: TokenSource


@dataclass(frozen=True)
class Account:
    login: str
    total_space: int
    used_space: int


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


def api_base_url() -> str:
    return os.environ.get(API_URL_ENV_VAR) or DEFAULT_API_URL


def fetch_account(token: str) -> Account:
    """Return the account the token belongs to, with one `GET /v1/disk/` and no retries.

    Raises `TokenRejectedError` on HTTP 401/403 and `DiskApiError` on any other failure: another
    non-2xx status, a transport error, or a success body without a valid login and space counters.
    """
    base_url = api_base_url()
    try:
        response = httpx.get(
            f'{base_url.rstrip("/")}/v1/disk/',
            headers={'Authorization': f'OAuth {token}'},
            timeout=API_TIMEOUT_SECONDS,
        )
    except httpx.TransportError as exc:
        raise DiskApiError(f'Could not reach the Yandex Disk API at {base_url}: {exc}') from None
    if response.status_code == httpx.codes.UNAUTHORIZED:
        raise TokenRejectedError(
            'Yandex rejected the token (invalid, expired or revoked). '
            "Run 'yandex-disk-rescue auth login' to store a new one."
        )
    if response.status_code == httpx.codes.FORBIDDEN:
        raise TokenRejectedError(
            'Yandex accepted the token but denied access to Disk: the app it was issued for lacks Disk '
            "permissions. Run 'yandex-disk-rescue auth login' to store a token with Disk access."
        )
    if not response.is_success:
        raise DiskApiError(f'Yandex Disk API returned HTTP {response.status_code}: {_error_description(response)}')
    return _parse_account(response.content)


def _error_description(response: httpx.Response) -> str:
    try:
        body = json.loads(response.content)
    except ValueError:
        return response.reason_phrase
    if isinstance(body, dict) and isinstance(body.get('description'), str):
        return body['description']
    return response.reason_phrase


def _parse_account(content: bytes) -> Account:
    try:
        body = json.loads(content)
    except ValueError:
        raise _unexpected_response('the body is not JSON') from None
    if not isinstance(body, dict):
        raise _unexpected_response('the body is not a JSON object')
    user = body.get('user')
    login = user.get('login') if isinstance(user, dict) else None
    if not isinstance(login, str) or not login:
        raise _unexpected_response('user.login is missing or not a non-empty string')
    return Account(
        login=login, total_space=_byte_count(body, 'total_space'), used_space=_byte_count(body, 'used_space')
    )


def _byte_count(body: dict[str, object], key: str) -> int:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _unexpected_response(f'{key} is missing or not a non-negative integer')
    return value


def _unexpected_response(reason: str) -> DiskApiError:
    return DiskApiError(f'Yandex Disk API returned an unexpected response to GET /v1/disk/: {reason}.')


def _fsync_dir(directory: Path) -> None:
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
