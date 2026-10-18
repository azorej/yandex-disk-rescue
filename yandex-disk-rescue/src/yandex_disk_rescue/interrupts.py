"""SIGTERM as a clean stop: inside `sigterm_stops`, the first SIGTERM stops the run the way Ctrl+C does, through the
same cleanup, and ends it with `Terminated`; every later one is ignored until the scope exits."""

import asyncio
import signal
import threading
from collections.abc import Coroutine, Iterator
from contextlib import contextmanager
from types import FrameType
from typing import Any

EXIT_CODE = 128 + signal.SIGTERM


class Terminated(KeyboardInterrupt):
    """The run was stopped by SIGTERM. A `KeyboardInterrupt`, so that whatever cleans up on Ctrl+C cleans up on it
    too."""


class _Stop:
    def __init__(self) -> None:
        self.stopping = False
        self.task: asyncio.Task[Any] | None = None
        self.cancelled: asyncio.Task[Any] | None = None

    def on_sigterm(self, signum: int, frame: FrameType | None) -> None:
        if self.stopping:
            return
        self.stopping = True
        task = self.task
        if task is None or task.done():
            raise Terminated
        # Already stopping on Ctrl+C: another cancellation would cut its cleanup short.
        if task.cancelling():
            return
        task.cancel()
        self.cancelled = task
        # Wake the loop should it be blocked in select() with a long timeout.
        task.get_loop().call_soon_threadsafe(lambda: None)


_active: _Stop | None = None


def _in_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


@contextmanager
def sigterm_stops() -> Iterator[None]:
    """Make SIGTERM stop the run while the context is active: the first SIGTERM raises `Terminated` in the main thread,
    or, inside `run`, cancels the running coroutine's task, which then ends `run` with `Terminated`; every later
    SIGTERM, and one that arrives while Ctrl+C is stopping `run`, is ignored. The SIGTERM handler found is restored on
    every exit. Outside the main thread, which signals never reach, it does nothing."""
    global _active
    if not _in_main_thread():
        yield
        return
    stop, outer = _Stop(), _active
    previous = signal.signal(signal.SIGTERM, stop.on_sigterm)
    _active = stop
    try:
        yield
    finally:
        _active = outer
        signal.signal(signal.SIGTERM, previous)


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    """`asyncio.run(coro)`, with SIGINT handled as `asyncio.run` does. Inside `sigterm_stops`, the scope's first
    SIGTERM while `coro` runs cancels its task, and `run` raises `Terminated` once the task ends cancelled."""
    stop = _active if _in_main_thread() else None
    if stop is None:
        return asyncio.run(coro)
    with asyncio.Runner() as runner:
        task = runner.get_loop().create_task(coro)
        stop.task = task
        try:
            return runner.run(task)
        except asyncio.CancelledError:
            if stop.cancelled is task:
                raise Terminated from None
            raise
        finally:
            stop.task = None
