"""`DiskClient`: every Yandex Disk REST API request, and every preview fetch, goes through it."""

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Self

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from yandex_disk_rescue.api.errors import (
    DiskApiError,
    DownloadRefusedError,
    NotAFolderError,
    PreviewRefusedError,
    RemotePathNotFoundError,
    TokenRejectedError,
)
from yandex_disk_rescue.api.limiter import AdaptiveLimiter, parse_retry_after
from yandex_disk_rescue.api.models import (
    Account,
    FilePreview,
    MalformedBodyError,
    Preview,
    PreviewLink,
    Resource,
    parse_account,
    parse_download_link,
    parse_file_preview,
    parse_folder_page,
    parse_resource,
)
from yandex_disk_rescue.api.remote_paths import ROOT, normalize_remote_path
from yandex_disk_rescue.api.user_agent import USER_AGENT
from yandex_disk_rescue.auth import validate_token
from yandex_disk_rescue.units import format_size

ACCOUNT_PATH = '/v1/disk/'
RESOURCES_PATH = '/v1/disk/resources'
DOWNLOAD_LINK_PATH = '/v1/disk/resources/download'
PAGE_LIMIT = 1000
PREVIEW_HOST = 'downloader.disk.yandex.ru'
PREVIEW_SIZE = 'XXXL'
PREVIEW_MAX_BYTES = 64 * 2**20
_log = logging.getLogger(__name__)
_RESOURCE_KEYS = ('path', 'type', 'size', 'md5', 'sha256', 'modified')
_RESOURCE_FIELDS = ','.join(_RESOURCE_KEYS)
_PREVIEW_FIELDS = ','.join((*_RESOURCE_KEYS, 'preview'))
_LISTING_FIELDS = ','.join(
    [
        'type',
        *(f'_embedded.items.{key}' for key in _RESOURCE_KEYS),
        '_embedded.total',
        '_embedded.limit',
        '_embedded.offset',
    ]
)


@dataclass(frozen=True)
class ApiLimits:
    """Pacing, timeout and retry policy of one `DiskClient`; `interval`, `timeout` and the backoff are seconds."""

    interval: float
    concurrency: int
    timeout: float
    retries: int
    backoff_initial: float = 0.5
    backoff_max: float = 30.0


class _TransientFailure(Exception):
    """One attempt failed in a way worth retrying; `message` is the user-facing text for it."""

    def __init__(self, message: str, *, retry_after: float | None) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


class DiskClient:
    """An async context manager that owns one `httpx.AsyncClient` authorized with the token.

    The token is checked for header safety on construction, before any request (`InvalidTokenError`).
    Every request goes through one `AdaptiveLimiter`; transport errors and throttling responses (429, 408,
    5xx) are retried up to `limits.retries` times, waiting `Retry-After` or an exponential backoff with
    jitter. Every API request maps 401 (and 403, except on `download_link`) to `TokenRejectedError` and other
    failures to `DiskApiError` whose message never includes the request headers or the token, and ends with
    ` (after N attempts)` when more than one attempt was made.

    The preview host is the second host the token is sent to, because it answers 403 without it. The token goes
    only to the origin of `base_url` and to `https://downloader.disk.yandex.ru` (`may_send_token_to`), and a
    preview fetch never follows a redirect, so the token cannot be forwarded anywhere else.
    """

    def __init__(self, token: str, *, base_url: str, limits: ApiLimits) -> None:
        self._base_url = base_url
        self._limits = limits
        self._limiter = AdaptiveLimiter(interval=limits.interval, concurrency=limits.concurrency)
        self._backoff = wait_exponential_jitter(
            initial=limits.backoff_initial, max=limits.backoff_max, jitter=limits.backoff_initial
        )
        api_url = httpx.URL(base_url)
        self._api_origin = (api_url.scheme, api_url.host, api_url.port)
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip('/'),
            headers={'Authorization': f'OAuth {validate_token(token)}', 'User-Agent': USER_AGENT},
            timeout=limits.timeout,
        )

    @property
    def limits(self) -> ApiLimits:
        return self._limits

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

    async def resource(self, path: str) -> Resource:
        """Return the file or folder at `path`, which must be below the root `disk:/`, as the API names it.

        Raises `RemotePathNotFoundError` when the path does not exist.
        """
        target = normalize_remote_path(path)
        if target == ROOT:
            raise ValueError('the root disk:/ has no resource record; list it with list_folder')
        context = f'Could not read {target}'
        response = await self._get(RESOURCES_PATH, {'path': target, 'fields': _RESOURCE_FIELDS}, context=context)
        if response.status_code == httpx.codes.NOT_FOUND:
            raise RemotePathNotFoundError(f"Remote path '{target}' does not exist.")
        self._raise_for_status(response, context=context)
        try:
            return parse_resource(response.content)
        except MalformedBodyError as exc:
            raise _unexpected_response(RESOURCES_PATH, str(exc), context=context) from None

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
                raise NotAFolderError(folder)
            for resource in page.items:
                yield resource
            offset += len(page.items)
            if offset >= page.total:
                return
            if not page.items:
                raise _unexpected_response(
                    RESOURCES_PATH, f'an empty page at offset {offset} of {page.total} items', context=context
                )

    async def download_link(self, path: str) -> str:
        """Return the signed link to fetch the file at `path` with a plain GET and no token.

        Raises `RemotePathNotFoundError` for a missing path and `DownloadRefusedError` for any other
        non-throttling 4xx but 401: on this endpoint a 403 refuses one file, not the token.
        """
        target = normalize_remote_path(path)
        context = f'Could not download {target}'
        params = {'path': target, 'fields': 'href,method,templated'}
        response = await self._get(DOWNLOAD_LINK_PATH, params, context=context)
        status = response.status_code
        if status == httpx.codes.NOT_FOUND:
            raise RemotePathNotFoundError(f"Remote path '{target}' does not exist.")
        if response.is_client_error and status != httpx.codes.UNAUTHORIZED:
            raise DownloadRefusedError(target, _status_failure(response))
        self._raise_for_status(response, context=context)
        try:
            return parse_download_link(response.content)
        except MalformedBodyError as exc:
            raise _unexpected_response(DOWNLOAD_LINK_PATH, str(exc), context=context) from None

    async def preview(self, path: str) -> FilePreview | None:
        """Return the metadata of the file at `path` and the link to its `PREVIEW_SIZE` preview, or None when Yandex
        offers no preview of it (an archive, for example).

        Fetch the link with `fetch_preview` right away: how long it stays valid is unknown.
        Raises `RemotePathNotFoundError` when the path does not exist. The message of a `DiskApiError` does not name
        the path, which its caller reports the error next to.
        """
        target = normalize_remote_path(path)
        if target == ROOT:
            raise ValueError('the root disk:/ has no preview')
        context = 'Could not read the preview'
        params = {'path': target, 'fields': _PREVIEW_FIELDS, 'preview_size': PREVIEW_SIZE}
        response = await self._get(RESOURCES_PATH, params, context=context)
        if response.status_code == httpx.codes.NOT_FOUND:
            raise RemotePathNotFoundError(f"Remote path '{target}' does not exist.")
        self._raise_for_status(response, context=context)
        try:
            return parse_file_preview(response.content, size_name=PREVIEW_SIZE)
        except MalformedBodyError as exc:
            raise _unexpected_response(RESOURCES_PATH, str(exc), context=context) from None

    def may_send_token_to(self, url: str) -> bool:
        """Whether `url` is on the origin of the API URL, or over https on exactly `PREVIEW_HOST` and its default
        port: the only places the token is sent to."""
        try:
            target = httpx.URL(url)
        except httpx.InvalidURL:
            return False
        origin = (target.scheme, target.host, target.port)
        return origin in (self._api_origin, ('https', PREVIEW_HOST, None))

    async def fetch_preview(self, link: PreviewLink) -> Preview:
        """Fetch the preview `link` points to, with the token, through the limiter and with the API's retries.

        Raises `PreviewRefusedError`, before any request, for a link `may_send_token_to` refuses; and for an answer
        that is a redirect (never followed), not 200, not `image/*`, or larger than `PREVIEW_MAX_BYTES` (reading
        stops there). A transport error or throttling past the retries raises `DiskApiError`. No reason quotes the
        link.
        """
        if not self.may_send_token_to(link.url):
            raise PreviewRefusedError(
                f'the preview link points to {_origin_of(link.url)}, not to the Yandex Disk preview host '
                f'https://{PREVIEW_HOST}; the token is not sent there'
            )
        return await self._retrying(self._preview_attempt, link.url, context='Could not fetch the preview')

    async def _get(self, api_path: str, params: dict[str, str | int], *, context: str | None) -> httpx.Response:
        return await self._retrying(self._attempt, api_path, params, context=context)

    async def _retrying[*Args, T](
        self, attempt: Callable[[*Args], Awaitable[T]], *args: *Args, context: str | None
    ) -> T:
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._limits.retries + 1),
            wait=self._wait,
            retry=retry_if_exception_type(_TransientFailure),
        )
        try:
            return await retrying(attempt, *args)
        except RetryError as exc:
            failure = exc.last_attempt.exception()
            if not isinstance(failure, _TransientFailure):
                raise
            attempts = exc.last_attempt.attempt_number
            message = failure.message if attempts == 1 else f'{failure.message} (after {attempts} attempts)'
            raise DiskApiError(_in_context(message, context)) from None

    async def _attempt(self, api_path: str, params: dict[str, str | int]) -> httpx.Response:
        async with self._limiter.slot():
            try:
                response = await self._http.get(api_path, params=params)
            except httpx.TransportError as exc:
                reason = describe_transport_error(exc, timeout=self._limits.timeout)
                raise _TransientFailure(
                    f'Could not reach the Yandex Disk API at {self._base_url}: {reason}', retry_after=None
                ) from None
        self._settle(response, _status_failure)
        return response

    async def _preview_attempt(self, url: str) -> Preview:
        async with self._limiter.slot():
            try:
                async with self._http.stream('GET', url, follow_redirects=False) as response:
                    self._settle(response, _preview_status)
                    return await _read_preview(response)
            except httpx.TransportError as exc:
                reason = describe_transport_error(exc, timeout=self._limits.timeout)
                raise _TransientFailure(
                    f'Could not reach the Yandex Disk preview host {_origin_of(url)}: {reason}', retry_after=None
                ) from None

    def _settle(self, response: httpx.Response, describe: Callable[[httpx.Response], str]) -> None:
        """Count a response that is not throttling as a success; for a throttling one, slow the limiter down and
        raise `_TransientFailure` with `describe(response)`."""
        if not _is_throttling(response.status_code):
            self._limiter.succeeded()
            return
        retry_after = parse_retry_after(response.headers.get('Retry-After'), now=datetime.now(UTC))
        self._limiter.throttled(retry_after)
        _log.info(
            'Disk API throttling: HTTP %d; request interval now %d ms; %s',
            response.status_code,
            round(self._limiter.interval * 1000),
            'no Retry-After' if retry_after is None else f'Retry-After {retry_after:g} s',
        )
        raise _TransientFailure(describe(response), retry_after=retry_after)

    def _wait(self, state: RetryCallState) -> float:
        failure = state.outcome.exception() if state.outcome else None
        if isinstance(failure, _TransientFailure) and failure.retry_after is not None:
            wait = failure.retry_after
        else:
            wait = self._backoff(state)
        _log.info(
            'Disk API request failed: %s; retry %d of %d in %.1f s',
            failure.message if isinstance(failure, _TransientFailure) else failure,
            state.attempt_number,
            self._limits.retries,
            wait,
        )
        return wait

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
            raise DiskApiError(_in_context(_status_failure(response), context))


def _is_throttling(status: int) -> bool:
    return status in (httpx.codes.TOO_MANY_REQUESTS, httpx.codes.REQUEST_TIMEOUT) or 500 <= status <= 599


def _status_failure(response: httpx.Response) -> str:
    return f'Yandex Disk API returned HTTP {response.status_code}: {_error_description(response)}'


def _preview_status(response: httpx.Response) -> str:
    return f'the preview host returned HTTP {response.status_code}'


async def _read_preview(response: httpx.Response) -> Preview:
    status = response.status_code
    if 300 <= status <= 399:
        raise PreviewRefusedError(f'the preview host redirected (HTTP {status}); not followed')
    if status != httpx.codes.OK:
        raise PreviewRefusedError(_preview_status(response))
    content_type = response.headers.get('Content-Type', '').partition(';')[0].strip().lower()
    if not content_type.startswith('image/'):
        raise PreviewRefusedError(f'not an image ({content_type or "no Content-Type"})')
    too_large = PreviewRefusedError(f'larger than {format_size(PREVIEW_MAX_BYTES)}')
    declared = response.headers.get('Content-Length', '')
    if declared.isdigit() and int(declared) > PREVIEW_MAX_BYTES:
        raise too_large
    content = bytearray()
    async for chunk in response.aiter_bytes():
        content += chunk
        if len(content) > PREVIEW_MAX_BYTES:
            raise too_large
    return Preview(content=bytes(content), content_type=content_type)


def _origin_of(url: str) -> str:
    """`scheme://host[:port]` of `url`, which never shows its path, query or user info."""
    try:
        target = httpx.URL(url)
    except httpx.InvalidURL:
        return 'an invalid URL'
    return f'{target.scheme}://{target.netloc.decode("ascii")}'


def describe_transport_error(exc: httpx.TransportError, *, timeout: float) -> str:
    """Describe a transport error without echoing the request, whose URL or headers may carry a secret.

    Only errors raised by the connection, the server or the URL scheme keep their text; anything else
    (such as httpx refusing to build the request) is reported by its type alone. Timeouts get their own
    text because httpx raises them with an empty message.
    """
    if isinstance(exc, httpx.TimeoutException):
        return f'the request timed out after {timeout:g} s ({type(exc).__name__})'
    if isinstance(exc, httpx.NetworkError | httpx.RemoteProtocolError | httpx.UnsupportedProtocol):
        return str(exc) or type(exc).__name__
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
