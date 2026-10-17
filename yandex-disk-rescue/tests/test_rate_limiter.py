"""Tests for `AdaptiveLimiter` driven by a fake clock, and for `Retry-After` parsing."""

from datetime import UTC, datetime

import anyio
import pytest
from yandex_disk_rescue import api

pytestmark = pytest.mark.anyio


class FakeClock:
    """A monotonic clock that only moves when the limiter sleeps on it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await anyio.lowlevel.checkpoint()


def _limiter(clock: FakeClock, *, interval: float, concurrency: int = 1) -> api.AdaptiveLimiter:
    return api.AdaptiveLimiter(interval=interval, concurrency=concurrency, clock=clock, sleep=clock.sleep)


async def _start(limiter: api.AdaptiveLimiter, clock: FakeClock) -> float:
    async with limiter.slot():
        return clock.now


async def test_starts_are_one_interval_apart():
    clock = FakeClock()
    limiter = _limiter(clock, interval=0.05)

    starts = [await _start(limiter, clock) for _ in range(4)]

    assert starts == pytest.approx([1000.0, 1000.05, 1000.10, 1000.15])


async def test_the_interval_doubles_per_throttle_up_to_the_cap_then_halves_back_to_the_base():
    clock = FakeClock()
    limiter = _limiter(clock, interval=0.05)

    seen = []
    for _ in range(7):
        limiter.throttled(None)
        seen.append(limiter.interval)
    assert seen == pytest.approx([0.1, 0.2, 0.4, 0.8, 1.6, 2.0, 2.0])

    before = await _start(limiter, clock)
    assert await _start(limiter, clock) - before == pytest.approx(2.0)

    halvings = []
    for _ in range(80):
        limiter.succeeded()
        halvings.append(limiter.interval)
    assert halvings[8] == pytest.approx(2.0)
    assert [halvings[i] for i in range(9, 80, 10)] == pytest.approx([1.0, 0.5, 0.25, 0.125, 0.0625, 0.05, 0.05, 0.05])


async def test_a_throttle_resets_the_success_streak():
    limiter = _limiter(FakeClock(), interval=0.05)
    limiter.throttled(None)
    for _ in range(9):
        limiter.succeeded()
    limiter.throttled(None)
    for _ in range(9):
        limiter.succeeded()

    assert limiter.interval == pytest.approx(0.2)


@pytest.mark.parametrize('base', [0.025, 0.5, 1.0])
async def test_the_cap_is_two_seconds_or_eight_times_the_base(base):
    limiter = _limiter(FakeClock(), interval=base)
    for _ in range(10):
        limiter.throttled(None)

    assert limiter.interval == pytest.approx(max(2.0, 8 * base))


@pytest.mark.parametrize(('retry_after', 'pause'), [(3.0, 3.0), (1000.0, api.MAX_PAUSE_SECONDS)])
async def test_a_retry_after_pauses_every_start_until_then(retry_after, pause):
    clock = FakeClock()
    limiter = _limiter(clock, interval=0.05, concurrency=3)
    await _start(limiter, clock)
    limiter.throttled(retry_after)

    starts = []

    async def start() -> None:
        starts.append(await _start(limiter, clock))

    async with anyio.create_task_group() as group:
        for _ in range(3):
            group.start_soon(start)

    assert starts == pytest.approx([1000.0 + pause, 1000.0 + pause + 0.1, 1000.0 + pause + 0.2])


async def test_at_most_concurrency_slots_are_held():
    clock = FakeClock()
    limiter = _limiter(clock, interval=0.001, concurrency=2)
    held = peak = 0

    async def hold() -> None:
        nonlocal held, peak
        async with limiter.slot():
            held += 1
            peak = max(peak, held)
            await clock.sleep(1.0)
            held -= 1

    async with anyio.create_task_group() as group:
        for _ in range(5):
            group.start_soon(hold)

    assert peak == 2


NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ('header', 'seconds'),
    [
        (None, None),
        ('7', 7.0),
        (' 0 ', 0.0),
        ('301', 300.0),
        ('Sat, 17 Oct 2026 12:00:42 GMT', 42.0),
        ('Sat, 17 Oct 2026 11:59:00 GMT', 0.0),
        ('Sun, 18 Oct 2026 12:00:00 GMT', 300.0),
        ('soon', None),
        ('-5', None),
        ('1.5', None),
    ],
)
def test_parse_retry_after(header, seconds):
    assert api.parse_retry_after(header, now=NOW) == seconds
