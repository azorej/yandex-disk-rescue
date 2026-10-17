"""`ContentClient`: fetches file bytes from the download host a Disk API download link points to.

It owns no file I/O: bytes go to an async `write(offset, chunk)` callback. It never sends the OAuth token, and its
error reasons never include a link's path or query string, which carry the link's signature.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import TracebackType
from typing import Self

import httpx

from yandex_disk_rescue.api.client import describe_transport_error
from yandex_disk_rescue.api.limiter import parse_retry_after

_CONTENT_RANGE = re.compile(r'bytes (\d+)-(\d+)/(\d+)')


class TransferError(Exception):
    """A failed fetch; `reason` is the user-facing text, free of the link and of request headers."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class LinkExpiredError(TransferError):
    """The download host answered 401, 403 or 410: the link must be requested again."""

    def __init__(self, reason: str, *, status: int) -> None:
        super().__init__(reason)
        self.status = status


class TransientTransferError(TransferError):
    """A transport error, a timeout, 408, 429 or 5xx; every byte written before it is valid."""

    def __init__(self, reason: str, *, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.retry_after = retry_after


class RangeNotHonouredError(TransferError):
    """A ranged request got 200, or a 206 whose `Content-Range` is not the requested range of the file."""


class TransferRefusedError(TransferError):
    """Any other answer the host gives, or a link that cannot be fetched at all."""


class TooManyBytesError(TransferError):
    """The body runs past the end of the requested range; the chunk that does is not written."""


class ContentClient:
    """An async context manager that owns one tokenless `httpx.AsyncClient` following redirects.

    `timeout` (seconds) is a stall timeout: a fetch fails when connecting, or any wait for data (the response
    headers or the next piece of the body), takes longer. A slow but steady transfer never trips it, and time spent
    in the `write` callback does not count.

    At most `connections` fetches are in flight, each from obtaining its link and its first request (redirects
    included) until its body ends or fails; a fetch over the cap waits for a free one, and that wait does not count
    toward the stall timeout either. The connection pool itself never makes a request wait.
    """

    def __init__(self, *, timeout: float, connections: int) -> None:
        self._timeout = timeout
        self._connections = connections
        self._slots = asyncio.Semaphore(connections)
        self._http = httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            limits=httpx.Limits(max_connections=None, max_keepalive_connections=connections),
            headers={'Accept-Encoding': 'identity'},
        )

    @property
    def connections(self) -> int:
        return self._connections

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self._http.aclose()

    async def fetch(
        self,
        link: Callable[[], Awaitable[str]],
        *,
        start: int,
        end: int,
        size: int,
        write: Callable[[int, bytes], Awaitable[None]],
    ) -> None:
        """Fetch bytes `[start, end)` of a file of `size` bytes from the URL `link()` returns, awaiting
        `write(offset, chunk)` in offset order.

        `link()` is awaited once a connection is free, so no link is held while the fetch waits for one. Each `write`
        is awaited before the next chunk is read. The whole file (`start == 0`, `end == size`) is requested without
        `Range`, and 200 or the matching 206 is accepted; any other range sends `Range: bytes=<start>-<end-1>` and
        requires that 206. Exceptions from `link` and `write` propagate unchanged.
        """
        whole = start == 0 and end == size
        if not whole and not 0 <= start < end <= size:
            raise ValueError(f'invalid range [{start}, {end}) of a {size}-byte file')
        headers = {} if whole else {'Range': f'bytes={start}-{end - 1}'}
        async with self._slots:
            href = await link()
            await self._get(href, headers, start=start, end=end, size=size, write=write)

    async def _get(
        self,
        href: str,
        headers: dict[str, str],
        *,
        start: int,
        end: int,
        size: int,
        write: Callable[[int, bytes], Awaitable[None]],
    ) -> None:
        try:
            async with self._http.stream('GET', href, headers=headers) as response:
                _check_response(response, start=start, end=end, size=size, ranged='Range' in headers)
                await _write_body(response, start=start, end=end, write=write)
        except httpx.TimeoutException:
            raise TransientTransferError(f'the download stalled: no data for {self._timeout:g} s') from None
        except httpx.TransportError as exc:
            raise TransientTransferError(
                f'the download failed: {describe_transport_error(exc, timeout=self._timeout)}'
            ) from None
        except httpx.TooManyRedirects:
            raise TransferRefusedError('the download host redirected too many times') from None
        except httpx.InvalidURL:
            raise TransferRefusedError('the download link is not a valid URL') from None


def _check_response(response: httpx.Response, *, start: int, end: int, size: int, ranged: bool) -> None:
    status = response.status_code
    host = response.url.host
    if status in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN, httpx.codes.GONE):
        raise LinkExpiredError(f'the download host {host} rejected the link (HTTP {status})', status=status)
    if status in (httpx.codes.TOO_MANY_REQUESTS, httpx.codes.REQUEST_TIMEOUT) or 500 <= status <= 599:
        retry_after = parse_retry_after(response.headers.get('Retry-After'), now=datetime.now(UTC))
        raise TransientTransferError(f'the download host {host} returned HTTP {status}', retry_after=retry_after)
    if status == httpx.codes.OK and ranged:
        raise RangeNotHonouredError(f'the download host {host} ignored the Range header (HTTP 200)')
    if status == httpx.codes.PARTIAL_CONTENT:
        content_range = response.headers.get('Content-Range', '')
        if content_range != f'bytes {start}-{end - 1}/{size}':
            shown = content_range if _CONTENT_RANGE.fullmatch(content_range) else 'a malformed or missing one'
            raise RangeNotHonouredError(
                f'the download host {host} sent Content-Range {shown} for bytes {start}-{end - 1} of {size}'
            )
    elif status != httpx.codes.OK:
        raise TransferRefusedError(f'the download host {host} returned HTTP {status}')
    encoding = response.headers.get('Content-Encoding', 'identity')
    if encoding != 'identity':
        raise TransferRefusedError(f'the download host {host} sent Content-Encoding {encoding}')


async def _write_body(
    response: httpx.Response, *, start: int, end: int, write: Callable[[int, bytes], Awaitable[None]]
) -> None:
    offset = start
    async for chunk in response.aiter_raw():
        if offset + len(chunk) > end:
            raise TooManyBytesError(f'the download host sent more than the {end - start} bytes requested')
        await write(offset, chunk)
        offset += len(chunk)
    if offset < end:
        raise TransientTransferError(f'the download ended after {offset - start} of {end - start} bytes')
