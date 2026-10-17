"""The Yandex OAuth token: the authorization URL to obtain one, its storage and resolution, and the account check."""

import enum
import json
import os
import stat
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from yandex_disk_rescue.errors import RescueError
from yandex_disk_rescue.paths import config_dir

TOKEN_ENV_VAR = 'YANDEX_DISK_RESCUE_TOKEN'
API_URL_ENV_VAR = 'YANDEX_DISK_RESCUE_API_URL'
DEFAULT_API_URL = 'https://cloud-api.yandex.net'
API_TIMEOUT_SECONDS = 30.0
CLIENT_ID_ENV_VAR = 'YANDEX_DISK_RESCUE_CLIENT_ID'
DEFAULT_CLIENT_ID = '9e2a0382c430429c9d57913b61d7d48f'
REQUESTED_SCOPES = ('cloud_api:disk.read', 'cloud_api:disk.info')
AUTHORIZE_ENDPOINT = 'https://oauth.yandex.ru/authorize'


class NoTokenError(RescueError):
    pass


class InvalidTokenError(RescueError):
    pass


class InsecureTokenFileError(RescueError):
    pass


class TokenRejectedError(RescueError):
    pass


class TokenFileError(RescueError):
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
    return _validated_token(text)


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
    token = _validated_token(value)
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


def _validated_token(value: str) -> str:
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


def api_base_url() -> str:
    return os.environ.get(API_URL_ENV_VAR) or DEFAULT_API_URL


def fetch_account(token: str) -> Account:
    """Return the account the token belongs to, with one `GET /v1/disk/` and no retries.

    Raises `InvalidTokenError` before any request when the token cannot be sent in a header,
    `TokenRejectedError` on HTTP 401/403 and `DiskApiError` on any other failure: another non-2xx
    status, a transport error, or a success body without a valid login and space counters.
    """
    header_token = _validated_token(token)
    base_url = api_base_url()
    try:
        response = httpx.get(
            f'{base_url.rstrip("/")}/v1/disk/',
            headers={'Authorization': f'OAuth {header_token}'},
            timeout=API_TIMEOUT_SECONDS,
        )
    except httpx.TransportError as exc:
        raise DiskApiError(f'Could not reach the Yandex Disk API at {base_url}: {_transport_failure(exc)}') from None
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


def _transport_failure(exc: httpx.TransportError) -> str:
    """Describe a transport error without echoing the request, whose headers carry the token.

    Only errors raised by the connection, the server or the URL scheme keep their text; anything else
    (such as httpx refusing to build the request) is reported by its type alone.
    """
    if isinstance(
        exc, httpx.NetworkError | httpx.TimeoutException | httpx.RemoteProtocolError | httpx.UnsupportedProtocol
    ):
        return str(exc)
    return f'the request could not be sent ({type(exc).__name__})'


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
