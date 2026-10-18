"""The output of the `scan`, `download` and `verify` commands: the report on stdout, the notices and status lines on
stderr, and the log file every run writes.

stdout carries the report only, whatever the verbosity. stderr carries the progress of each slow phase (none with
`--no-progress` or `--quiet`), the notices (WARNING records; INFO too with `--verbose`; none with `--quiet`) and the
final `Error: …` or `Interrupted; …` line. The log file gets every INFO record (DEBUG too with `--verbose`) and every
status line whatever the console flags, and a start and an end record per run.

On a terminal (stderr is one, and `TERM` is set and neither `dumb` nor `unknown`) the progress is a live view, and
notices have no timestamp. Anywhere else it is a timestamped status line every `progress_interval` seconds, and so is
each notice; nothing written then holds a control character, whatever the environment says about colours.
"""

import dataclasses
import logging
import os
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, TextIO

import typer

from yandex_disk_rescue import __version__, paths
from yandex_disk_rescue.display import display_text
from yandex_disk_rescue.errors import RescueError
from yandex_disk_rescue.logs import CONSOLE, LOGGER_NAME, LogFile, Notices, notice, stderr_through
from yandex_disk_rescue.progress import ProgressState
from yandex_disk_rescue.view import LiveView

type Verbosity = Literal['quiet', 'normal', 'verbose']

Verbose = Annotated[
    bool,
    typer.Option(
        '-v',
        '--verbose',
        help='Also show on stderr each file downloaded, adopted, renamed or moved aside, each pass and deferral, '
        'the start and commit of a scan, and API throttling and retries; log DEBUG records to the log file.',
    ),
]
Quiet = Annotated[
    bool,
    typer.Option('-q', '--quiet', help='Show nothing on stderr but the final error line; stdout is unchanged.'),
]
NoProgress = Annotated[
    bool,
    typer.Option(
        '--no-progress',
        help='Show no live view and write no status lines to stderr; the log file still gets them, and notices still '
        'show.',
    ),
]

FILE_ONLY = {CONSOLE: False}
_log = logging.getLogger(LOGGER_NAME)


class LogFileError(RescueError):
    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f'Could not open the log file {path}: {reason}.')


@dataclass(frozen=True)
class OutputOptions:
    """The verbosity; `progress`, whether stderr gets status lines; and the seconds between two status lines."""

    verbosity: Verbosity
    progress: bool
    progress_interval: int


def output_options(*, verbose: bool, quiet: bool, no_progress: bool, progress_interval: int) -> OutputOptions:
    """The options of `--verbose`, `--quiet`, `--no-progress` and `--progress-interval`; raises `typer.BadParameter`
    when both `--verbose` and `--quiet` are given."""
    if verbose and quiet:
        raise typer.BadParameter('--verbose and --quiet cannot be used together.', param_hint="'-v' / '-q'")
    verbosity: Verbosity = 'verbose' if verbose else 'quiet' if quiet else 'normal'
    return OutputOptions(verbosity=verbosity, progress=not no_progress, progress_interval=progress_interval)


def is_terminal(stream: TextIO) -> bool:
    """Whether `stream` is a terminal that takes cursor movement: a tty, and `TERM` set and neither `dumb` nor
    `unknown`."""
    return stream.isatty() and os.environ.get('TERM', 'dumb') not in {'dumb', 'unknown'}


class Output:
    """The running command's output: `line` writes one report line to stdout, and `progress` reports a slow phase,
    with a live view when `terminal`."""

    def __init__(self, log_file: LogFile, options: OutputOptions, *, terminal: bool) -> None:
        self._log_file = log_file
        self._options = options
        self._terminal = terminal
        self._view: LiveView | None = None

    @property
    def log_path(self) -> Path:
        return self._log_file.path

    def line(self, text: str) -> None:
        """Write the report line `text`, its control characters escaped, to stdout, above the live view when one is
        shown."""
        text = display_text(text)
        if self._view is None:
            typer.echo(text)
        else:
            self._view.above(lambda: typer.echo(text))

    def headline(self, text: str) -> None:
        """Write a report line saying what the run is about to do, and log it to the log file."""
        _log.info('%s', text, extra=FILE_ONLY)
        self.line(text)

    def summary(self, lines: Callable[..., list[str]]) -> None:
        """Log the summary `lines(limit=None)`, every list in full, to the log file, then print `lines()`, whose lists
        show their first few members."""
        _log.info('Summary:\n%s', '\n'.join(lines(limit=None)), extra=FILE_ONLY)
        for line in lines():
            self.line(line)

    @contextmanager
    def progress(self, state: ProgressState) -> Iterator[None]:
        """Report the phase `state` follows while the context runs: every `progress_interval` seconds its status line
        goes to the log file; when the context exits, on every path, the phase's last line goes there too. Unless
        `--no-progress` or `--quiet` was given, stderr shows the phase too: on a terminal as a live view, cleared on
        every exit, with every notice and report line printed above it; anywhere else each status line goes to stderr
        as a notice. Nothing is written for a phase that has not started. No line is written once the context has
        exited."""
        shown = self._options.progress and self._options.verbosity != 'quiet'
        to_stderr = shown and not self._terminal

        def emit(*, final: bool) -> None:
            line = state.snapshot().status_line(final=final)
            if line is None:
                return
            _log.info('%s', line, extra=FILE_ONLY)
            if to_stderr:
                notice(line, timestamped=True)

        stop = threading.Event()

        def tick() -> None:
            while not stop.wait(self._options.progress_interval):
                emit(final=False)

        ticker = threading.Thread(target=tick, name='progress', daemon=True)
        ticker.start()
        try:
            if shown and self._terminal:
                with self._live_view(state):
                    yield
            else:
                yield
        finally:
            stop.set()
            ticker.join()
            emit(final=True)

    @contextmanager
    def _live_view(self, state: ProgressState) -> Iterator[None]:
        with LiveView(state) as view, stderr_through(view.notice):
            self._view = view
            try:
                yield
            finally:
                self._view = None

    def footer(self, manifest: Path) -> None:
        """The last lines of the report: the manifest's path, then the log's (with why it is incomplete, if it is)."""
        self.line(f'Manifest: {manifest}')
        failure = self._log_file.failure
        self.line(f'Log: {self.log_path}' if failure is None else f'Log: {self.log_path} (incomplete: {failure})')


@contextmanager
def command_output(command: str, options: OutputOptions, *, details: Mapping[str, object]) -> Iterator[Output]:
    """Open the log file, attach the log and notice handlers to the `yandex_disk_rescue` logger for the run, and log
    its start (`command`, the version, the pid and `details`) and its end: completed, the error and its exit code,
    interrupted, or an unexpected exception with its traceback. The handlers are detached and closed on every exit.

    Raises `LogFileError` when the log file cannot be opened; nothing is logged then.
    """
    path = paths.log_path()
    terminal = is_terminal(sys.stderr)

    def log_failed(reason: str) -> None:
        notice(
            f'Warning: could not write the log file {path}: {reason}; logging to it is off for this run.',
            timestamped=not terminal,
        )

    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        log_file = LogFile(path, command, log_failed)
    except OSError as exc:
        raise LogFileError(path, exc.strerror or str(exc)) from None
    handlers: list[logging.Handler] = [log_file]
    if options.verbosity != 'quiet':
        level = logging.INFO if options.verbosity == 'verbose' else logging.WARNING
        handlers.append(Notices(level, timestamped=not terminal))
    previous = _log.level
    _log.setLevel(logging.DEBUG if options.verbosity == 'verbose' else logging.INFO)
    for handler in handlers:
        _log.addHandler(handler)
    try:
        arguments = ', '.join(f'{name}={value}' for name, value in {**details, **dataclasses.asdict(options)}.items())
        _log.info('%s started: version %s, pid %d; %s', command, __version__, os.getpid(), arguments, extra=FILE_ONLY)
        try:
            yield Output(log_file, options, terminal=terminal)
        except BaseException as exc:
            _log_end(command, exc)
            raise
        _log.info('%s completed (exit 0)', command, extra=FILE_ONLY)
    finally:
        for handler in handlers:
            _log.removeHandler(handler)
            handler.close()
        _log.setLevel(previous)


def _log_end(command: str, exc: BaseException) -> None:
    match exc:
        case RescueError():
            _log.error('%s failed (exit %d): %s', command, exc.exit_code, exc, extra=FILE_ONLY)
        case KeyboardInterrupt():
            _log.warning('%s interrupted (exit 130)', command, extra=FILE_ONLY)
        case typer.TyperException():
            _log.error('%s failed (exit %d): %s', command, exc.exit_code, exc.format_message(), extra=FILE_ONLY)
        case typer.Exit():
            _log.info('%s ended (exit %d)', command, exc.exit_code, extra=FILE_ONLY)
        case _:
            _log.error('%s failed with an unexpected error', command, exc_info=exc, extra=FILE_ONLY)
