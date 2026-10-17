"""`DiskThreads`: the worker threads that keep a mirror run's disk work off the event loop.

Each call handed to it does a bounded amount of work (one chunk, one fsync, one rename), so a cancelled caller waits
for at most one call. A call that has started always returns before its caller goes on, also when the caller is
cancelled, so a descriptor the call uses is never closed under it.
"""

import asyncio
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from types import TracebackType
from typing import Self


class DiskThreads:
    """A dedicated pool of `threads` worker threads, used as a context manager; leaving it drops the calls not yet
    started and waits for the running ones."""

    def __init__(self, threads: int) -> None:
        self._executor = ThreadPoolExecutor(max_workers=threads, thread_name_prefix='disk')

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)

    async def run[T, *Ts](self, call: Callable[[*Ts], T], *args: *Ts) -> T:
        """The result of `call(*args)`, run in a worker thread; its exception propagates unchanged.

        When the awaiting task is cancelled, a call not yet started is dropped and a running one is waited for, through
        any further cancellation, before `CancelledError` propagates.
        """
        future = self._executor.submit(call, *args)
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            future.cancel()
            await _settled(future)
            raise


async def _settled[T](future: Future[T]) -> None:
    loop = asyncio.get_running_loop()
    done = asyncio.Event()
    future.add_done_callback(lambda _: loop.call_soon_threadsafe(done.set))
    while not done.is_set():
        try:
            await done.wait()
        except asyncio.CancelledError:
            continue
