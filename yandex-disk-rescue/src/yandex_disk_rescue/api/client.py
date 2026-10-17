"""`DiskClient`: every Yandex Disk REST API request goes through it."""

import json
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Self

import httpx

from yandex_disk_rescue.api.errors import DiskApiError, NotAFolderError, RemotePathNotFoundError, TokenRejectedError
from yandex_disk_rescue.api.models import Account, MalformedBodyError, Resource, parse_account, parse_folder_page
from yandex_disk_rescue.api.remote_paths import normalize_remote_path
from yandex_disk_rescue.auth import validate_token

ACCOUNT_PATH = '/v1/disk/'
RESOURCES_PATH = '/v1/disk/resources'
PAGE_LIMIT = 1000
_LISTING_FIELDS = ','.join(
    [
        'type',
        *(f'_embedded.items.{key}' for key in ('path', 'type', 'size', 'md5', 'sha256', 'modified')),
        '_embedded.total',
        '_embedded.limit',
        '_embedded.offset',
    ]
)


class DiskClient:
    """An async context manager that owns one `httpx.AsyncClient` authorized with the token.

    The token is checked for header safety on construction, before any request (`InvalidTokenError`).
    Every request maps 401/403 to `TokenRejectedError` and other failures to `DiskApiError` whose
    message never includes the request headers or the token.
    """

    def __init__(self, token: str, *, base_url: str, timeout: float) -> None:
        self._base_url = base_url
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip('/'),
            headers={'Authorization': f'OAuth {validate_token(token)}'},
            timeout=timeout,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self._http.aclose()

    async def account(self) -> Account:
        """Return the account the token belongs to (`GET /v1/disk/`)."""
        response = await self._get(ACCOUNT_PATH, {}, context=None)
        self._raise_for_status(response, context=None)
        try:
            return parse_account(response.content)
        except MalformedBodyError as exc:
            raise _unexpected_response(ACCOUNT_PATH, str(exc), context=None) from None

    async def list_folder(self, path: str) -> AsyncIterator[Resource]:
        """Yield every direct child of the folder at `path`, sorted by name, one `limit`/`offset` page at a time.

        Raises `RemotePathNotFoundError` when the path does not exist and `NotAFolderError` when it is a file.
        """
        folder = normalize_remote_path(path)
        context = f'Could not list {folder}'
        offset = 0
        while True:
            params = {'path': folder, 'limit': PAGE_LIMIT, 'offset': offset, 'sort': 'name', 'fields': _LISTING_FIELDS}
            response = await self._get(RESOURCES_PATH, params, context=context)
            if response.status_code == httpx.codes.NOT_FOUND:
                raise RemotePathNotFoundError(f"Remote path '{folder}' does not exist.")
            self._raise_for_status(response, context=context)
            try:
                page = parse_folder_page(response.content)
            except MalformedBodyError as exc:
                raise _unexpected_response(RESOURCES_PATH, str(exc), context=context) from None
            if page.kind == 'file':
                raise NotAFolderError(f"Remote path '{folder}' is a file, not a folder.")
            for resource in page.items:
                yield resource
            offset += len(page.items)
            if offset >= page.total:
                return
            if not page.items:
                raise _unexpected_response(
                    RESOURCES_PATH, f'an empty page at offset {offset} of {page.total} items', context=context
                )

    async def _get(self, api_path: str, params: dict[str, str | int], *, context: str | None) -> httpx.Response:
        try:
            return await self._http.get(api_path, params=params)
        except httpx.TransportError as exc:
            message = f'Could not reach the Yandex Disk API at {self._base_url}: {_transport_failure(exc)}'
            raise DiskApiError(_in_context(message, context)) from None

    @staticmethod
    def _raise_for_status(response: httpx.Response, *, context: str | None) -> None:
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
            message = f'Yandex Disk API returned HTTP {response.status_code}: {_error_description(response)}'
            raise DiskApiError(_in_context(message, context))


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


def _unexpected_response(api_path: str, reason: str, *, context: str | None) -> DiskApiError:
    message = f'Yandex Disk API returned an unexpected response to GET {api_path}: {reason}.'
    return DiskApiError(_in_context(message, context))


def _in_context(message: str, context: str | None) -> str:
    return f'{context}: {message}' if context else message
