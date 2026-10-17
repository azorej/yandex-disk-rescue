"""`DiskClient`: every Yandex Disk REST API request goes through it."""

import json
from collections.abc import AsyncIterator
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

from yandex_disk_rescue.api.errors import DiskApiError, NotAFolderError, RemotePathNotFoundError, TokenRejectedError
from yandex_disk_rescue.api.limiter import AdaptiveLimiter, parse_retry_after
from yandex_disk_rescue.api.models import (
    Account,
    MalformedBodyError,
    Resource,
    parse_account,
    parse_folder_page,
    parse_resource,
)
from yandex_disk_rescue.api.remote_paths import ROOT, normalize_remote_path
from yandex_disk_rescue.auth import validate_token

ACCOUNT_PATH = '/v1/disk/'
RESOURCES_PATH = '/v1/disk/resources'
PAGE_LIMIT = 1000
_RESOURCE_KEYS = ('path', 'type', 'size', 'md5', 'sha256', 'modified')
_RESOURCE_FIELDS = ','.join(_RESOURCE_KEYS)
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
    jitter. Every request maps 401/403 to `TokenRejectedError` and other failures to `DiskApiError` whose
    message never includes the request headers or the token, and ends with ` (after N attempts)` when
    more than one attempt was made.
    """

    def __init__(self, token: str, *, base_url: str, limits: ApiLimits) -> None:
        self._base_url = base_url
        self._limits = limits
        self._limiter = AdaptiveLimiter(interval=limits.interval, concurrency=limits.concurrency)
        self._backoff = wait_exponential_jitter(
            initial=limits.backoff_initial, max=limits.backoff_max, jitter=limits.backoff_initial
        )
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip('/'),
            headers={'Authorization': f'OAuth {validate_token(token)}'},
            timeout=limits.timeout,
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

    async def _get(self, api_path: str, params: dict[str, str | int], *, context: str | None) -> httpx.Response:
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._limits.retries + 1),
            wait=self._wait,
            retry=retry_if_exception_type(_TransientFailure),
        )
        try:
            return await retrying(self._attempt, api_path, params)
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
                reason = _transport_failure(exc, timeout=self._limits.timeout)
                raise _TransientFailure(
                    f'Could not reach the Yandex Disk API at {self._base_url}: {reason}', retry_after=None
                ) from None
        if not _is_throttling(response.status_code):
            self._limiter.succeeded()
            return response
        retry_after = parse_retry_after(response.headers.get('Retry-After'), now=datetime.now(UTC))
        self._limiter.throttled(retry_after)
        raise _TransientFailure(_status_failure(response), retry_after=retry_after)

    def _wait(self, state: RetryCallState) -> float:
        failure = state.outcome.exception() if state.outcome else None
        if isinstance(failure, _TransientFailure) and failure.retry_after is not None:
            return failure.retry_after
        return self._backoff(state)

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


def _transport_failure(exc: httpx.TransportError, *, timeout: float) -> str:
    """Describe a transport error without echoing the request, whose headers carry the token.

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
