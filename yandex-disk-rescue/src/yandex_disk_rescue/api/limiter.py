"""`AdaptiveLimiter`: paces the start of every request of one `DiskClient` and backs off when the API throttles."""

import asyncio
import email.utils
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

MAX_PAUSE_SECONDS = 300.0
SUCCESSES_TO_HALVE = 10
_MIN_INTERVAL_CAP = 2.0
_CAP_FACTOR = 8


class AdaptiveLimiter:
    """Shared by every request (retries included) of one client.

    - Request starts are at least the current interval apart, and at most `concurrency` requests are in flight.
    - `throttled()` doubles the interval up to `max(2 s, 8 × base)` and, given a `Retry-After`, pauses every start
      until then (at most `MAX_PAUSE_SECONDS`).
    - Every `SUCCESSES_TO_HALVE` consecutive `succeeded()` calls halve the interval, never below the base.

    `clock` and `sleep` are injectable so the pacing can be driven by a fake clock.
    """

    def __init__(
        self,
        *,
        interval: float,
        concurrency: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.base_interval = interval
        self.max_interval = max(_MIN_INTERVAL_CAP, _CAP_FACTOR * interval)
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._slots = asyncio.Semaphore(concurrency)
        self._turn = asyncio.Lock()
        self._last_start = float('-inf')
        self._paused_until = float('-inf')
        self._successes = 0

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Wait for a free slot and this request's start time; the request runs inside the block."""
        async with self._slots:
            async with self._turn:
                # Re-read on every pass: a throttle seen while sleeping can raise the interval or add a pause.
                while (wait := max(self._last_start + self.interval, self._paused_until) - self._clock()) > 0:
                    await self._sleep(wait)
                self._last_start = self._clock()
            yield

    def throttled(self, retry_after: float | None) -> None:
        self._successes = 0
        self.interval = min(self.interval * 2, self.max_interval)
        if retry_after is not None:
            until = self._clock() + min(retry_after, MAX_PAUSE_SECONDS)
            self._paused_until = max(self._paused_until, until)

    def succeeded(self) -> None:
        self._successes += 1
        if self._successes >= SUCCESSES_TO_HALVE:
            self._successes = 0
            self.interval = max(self.interval / 2, self.base_interval)


def parse_retry_after(value: str | None, *, now: datetime) -> float | None:
    """Return the wait a `Retry-After` header asks for, in seconds, clamped to 0..`MAX_PAUSE_SECONDS`.

    The header is delta-seconds or an HTTP date. A missing or unparseable header gives None, so the caller
    falls back to its own backoff (RFC 9110 lets a client ignore an invalid `Retry-After`).
    """
    if value is None:
        return None
    text = value.strip()
    if text.isascii() and text.isdigit():
        seconds = float(text)
    else:
        try:
            when = email.utils.parsedate_to_datetime(text)
        except TypeError, ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - now).total_seconds()
    return min(max(seconds, 0.0), MAX_PAUSE_SECONDS)
