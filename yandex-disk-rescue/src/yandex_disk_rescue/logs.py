"""The handlers of the `yandex_disk_rescue` logger: the rotating log file and the notices on stderr.

Every line of the log file, and every notice on a stderr that is not a terminal, starts with a UTC timestamp. A
message is one line, every control character in it, a newline too, escaped (`display.display_line`), in the log file
as on stderr; a traceback is written as one prefixed line per line, so each line of the log file says when and which
run wrote it.

A write to stderr that fails (a `2> file` on a full disk, a terminal that went away) turns stderr off for the rest of
the run instead of raising: `sys.stderr` is replaced by a stream that discards everything, so every later write to it,
the tool's or a library's, goes nowhere, and the `on_failure` that `stderr_failures` installed is called once with the
reason.

Lock order: the `Notices` handler's lock, then the stderr lock, the live view's and rich's locks; the log file's
handler lock is taken last and nothing is taken under it. A failure of stderr or of the log file is only recorded
where it happens, and reported once those locks are released, so no record is logged under the stderr lock (or one
taken after it) and nothing is written to stderr under the log file's lock.
"""

import contextlib
import io
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from yandex_disk_rescue.display import display_line, display_text

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
        text = display_line(record.getMessage())
        if record.exc_info:
            text = f'{text}\n{display_text(self.formatException(record.exc_info).rstrip())}'
        prefix = self._prefix(record)
        return '\n'.join(f'{prefix}{line}' for line in text.splitlines() or [''])


class LogFile(logging.Handler):
    """The log file at `path`: lines `<UTC timestamp> <LEVEL> <command>[<pid>] <message>`, UTF-8 with
    `backslashreplace`, appended unbuffered.

    Before a record would take the file past `MAX_BYTES`, the file is rotated: `.1` … `.BACKUP_COUNT-1` move up one
    and the file becomes `.1`. Every file is created with mode 0600. The constructor raises `OSError` when the file
    cannot be opened. A write or a rotation that fails later turns the handler off for the rest of the run instead of
    raising: it keeps the reason in `failure` and calls `on_failure(reason)` once, from the thread whose write failed,
    after the handler's lock is released.
    """

    def __init__(self, path: Path, command: str, on_failure: Callable[[str], None]) -> None:
        super().__init__()
        pid = os.getpid()
        self.setFormatter(_PrefixedFormatter(lambda r: f'{timestamp(r.created)} {r.levelname} {command}[{pid}] '))
        self.path = path
        self.failure: str | None = None
        self._on_failure = on_failure
        self._failure_lock = threading.Lock()
        self._failure_reported = False
        self._fd: int | None = _open_log(path)

    def handle(self, record: logging.LogRecord) -> logging.LogRecord | bool:
        handled = super().handle(record)
        self._report_failure()
        return handled

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

    def _report_failure(self) -> None:
        with self._failure_lock:
            if self.failure is None or self._failure_reported:
                return
            self._failure_reported = True
        self._on_failure(self.failure)


class Notices(logging.Handler):
    """The notices on stderr: `<UTC timestamp> <message>`, or the message alone when not `timestamped`, with no level
    name, written one record at a time through `write_stderr`. Records logged with `extra={CONSOLE: False}` are left
    to the log file."""

    def __init__(self, level: int, *, timestamped: bool) -> None:
        super().__init__(level)
        self.setFormatter(_PrefixedFormatter(lambda r: f'{timestamp(r.created)} ' if timestamped else ''))
        self.addFilter(lambda record: getattr(record, CONSOLE, True))

    def emit(self, record: logging.LogRecord) -> None:
        write_stderr(self.format(record))


class _Discarded(io.TextIOBase):
    def write(self, text: str) -> int:
        return len(text)


class _Stderr:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.writer: Callable[[str], None] | None = None
        # Taken only for a few attribute changes, never while logging or writing.
        self.off_lock = threading.Lock()
        self.off = False
        self.failure: str | None = None
        self.reported = False
        self.discarded = _Discarded()
        self.on_failure: Callable[[str], None] | None = None


_stderr = _Stderr()


class StderrFile:
    """A file for writers that must never fail on stderr, such as a rich `Console`: it writes to the current
    `sys.stderr`, and a write or a flush that fails there turns stderr off."""

    @property
    def encoding(self) -> str:
        return sys.stderr.encoding

    def isatty(self) -> bool:
        return sys.stderr.isatty()

    def fileno(self) -> int:
        return sys.stderr.fileno()

    def write(self, text: str) -> int:
        try:
            sys.stderr.write(text)
        except OSError as exc:
            _turn_off(exc)
        return len(text)

    def flush(self) -> None:
        try:
            sys.stderr.flush()
        except OSError as exc:
            _turn_off(exc)


STDERR = StderrFile()


def stderr_off() -> bool:
    """Whether a write to stderr has failed in this process, so stderr is off."""
    return _stderr.off


@contextmanager
def stderr_failures(on_failure: Callable[[str], None]) -> Iterator[None]:
    """Report a write to stderr that fails while the context runs to `on_failure(reason)`, once: when the
    `write_stderr` that met it returns, at the next `report_stderr_failure`, or at the latest when the context exits.
    Call it with no lock held."""
    _stderr.on_failure = on_failure
    try:
        yield
    finally:
        report_stderr_failure()
        _stderr.on_failure = None


def report_stderr_failure() -> None:
    """Pass the failure of stderr, if there is one and it is not reported yet, to the `on_failure` of
    `stderr_failures`. Call it with no lock held."""
    with _stderr.off_lock:
        on_failure = _stderr.on_failure
        if _stderr.failure is None or _stderr.reported or on_failure is None:
            return
        _stderr.reported = True
    on_failure(_stderr.failure)


def _turn_off(exc: OSError) -> None:
    with _stderr.off_lock:
        if _stderr.off:
            return
        _stderr.off, _stderr.failure = True, exc.strerror or str(exc)
        # The failed stream keeps the bytes it could not write and no API discards them; Python would retry them at
        # exit and turn that failure into exit code 120.
        sys.stderr = _stderr.discarded


def notice(text: str, *, timestamped: bool) -> None:
    """Write `text`, as one line, to stderr as a notice: `<UTC timestamp> <text>`, or `text` alone when not
    `timestamped`."""
    text = display_line(text)
    write_stderr(f'{timestamp(time.time())} {text}' if timestamped else text)


def write_stderr(text: str) -> None:
    """Write `text`, its control characters but newlines escaped, and a newline to stderr, one caller at a time:
    through the writer `stderr_through` installed, or else to the current `sys.stderr`, flushed. Nothing is written
    once stderr is off; a failure of stderr this write met, or an earlier one, is reported when it returns."""
    if not _stderr.off:
        text = display_text(text)
        with _stderr.lock:
            if _stderr.writer is not None:
                _stderr.writer(text)
            else:
                STDERR.write(f'{text}\n')
                STDERR.flush()
    report_stderr_failure()


@contextmanager
def stderr_through(writer: Callable[[str], None]) -> Iterator[None]:
    """Send every line `write_stderr` gets to `writer(text)` while the context runs."""
    with _stderr.lock:
        _stderr.writer = writer
    try:
        yield
    finally:
        with _stderr.lock:
            _stderr.writer = None


def _open_log(path: Path) -> int:
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o600)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]
