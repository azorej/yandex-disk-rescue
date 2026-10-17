"""The handlers of the `yandex_disk_rescue` logger: the rotating log file and the notices on stderr.

Every line either handler writes starts with a UTC timestamp; a multi-line message (a summary, a traceback) is
written as one prefixed line per line, so each line of the log file says when and which run wrote it.
"""

import contextlib
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

LOGGER_NAME = 'yandex_disk_rescue'
MAX_BYTES = 10 * 2**20
BACKUP_COUNT = 5
# A record logged with `extra={CONSOLE: False}` goes to the log file only.
CONSOLE = 'console'


def timestamp(created: float) -> str:
    """`created` (seconds since the epoch) as `2026-10-17T19:20:01.123Z`."""
    moment = datetime.fromtimestamp(created, UTC)
    return f'{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}Z'


class _PrefixedFormatter(logging.Formatter):
    """Formats a record, its traceback included, as lines each starting with `prefix(record)`."""

    def __init__(self, prefix: Callable[[logging.LogRecord], str]) -> None:
        super().__init__()
        self._prefix = prefix

    def format(self, record: logging.LogRecord) -> str:
        text = record.getMessage()
        if record.exc_info:
            text = f'{text}\n{self.formatException(record.exc_info).rstrip()}'
        prefix = self._prefix(record)
        return '\n'.join(f'{prefix}{line}' for line in text.splitlines() or [''])


class LogFile(logging.Handler):
    """The log file at `path`: lines `<UTC timestamp> <LEVEL> <command>[<pid>] <message>`, UTF-8 with
    `backslashreplace`, appended unbuffered.

    Before a record would take the file past `MAX_BYTES`, the file is rotated: `.1` … `.BACKUP_COUNT-1` move up one
    and the file becomes `.1`. Every file is created with mode 0600. The constructor raises `OSError` when the file
    cannot be opened. A write or a rotation that fails later turns the handler off for the rest of the run instead of
    raising: it keeps the reason in `failure` and calls `on_failure(reason)` once, from the thread whose write failed.
    """

    def __init__(self, path: Path, command: str, on_failure: Callable[[str], None]) -> None:
        super().__init__()
        pid = os.getpid()
        self.setFormatter(_PrefixedFormatter(lambda r: f'{timestamp(r.created)} {r.levelname} {command}[{pid}] '))
        self.path = path
        self.failure: str | None = None
        self._on_failure = on_failure
        self._fd: int | None = _open_log(path)

    def emit(self, record: logging.LogRecord) -> None:
        if self._fd is None:
            return
        data = f'{self.format(record)}\n'.encode('utf-8', 'backslashreplace')
        try:
            size = os.fstat(self._fd).st_size
            if size and size + len(data) > MAX_BYTES:
                self._rotate()
            _write_all(self._fd, data)
        except OSError as exc:
            self._fail(exc.strerror or str(exc))

    def close(self) -> None:
        with self.lock:
            fd, self._fd = self._fd, None
            if fd is not None:
                os.close(fd)
        super().close()

    def _rotate(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)
        for number in range(BACKUP_COUNT - 1, 0, -1):
            older = self.path.with_name(f'{self.path.name}.{number}')
            if os.path.lexists(older):
                os.replace(older, self.path.with_name(f'{self.path.name}.{number + 1}'))
        os.replace(self.path, self.path.with_name(f'{self.path.name}.1'))
        self._fd = _open_log(self.path)

    def _fail(self, reason: str) -> None:
        self.failure = reason
        fd, self._fd = self._fd, None
        if fd is not None:
            # The write already failed and its reason is reported; Linux releases the descriptor even when
            # close reports an error too.
            with contextlib.suppress(OSError):
                os.close(fd)
        self._on_failure(reason)


class Notices(logging.Handler):
    """The notices on stderr: `<UTC timestamp> <message>`, with no level name, written and flushed one record at a
    time. Records logged with `extra={CONSOLE: False}` are left to the log file."""

    def __init__(self, level: int) -> None:
        super().__init__(level)
        self.setFormatter(_PrefixedFormatter(lambda r: f'{timestamp(r.created)} '))
        self.addFilter(lambda record: getattr(record, CONSOLE, True))

    def emit(self, record: logging.LogRecord) -> None:
        write_stderr(self.format(record))


_stderr_lock = threading.Lock()


def notice(text: str) -> None:
    """Write `text` to stderr as a notice: `<UTC timestamp> <text>`."""
    write_stderr(f'{timestamp(time.time())} {text}')


def write_stderr(text: str) -> None:
    """Write `text` and a newline to the current `sys.stderr` and flush it, one caller at a time."""
    with _stderr_lock:
        sys.stderr.write(f'{text}\n')
        sys.stderr.flush()


def _open_log(path: Path) -> int:
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o600)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]
