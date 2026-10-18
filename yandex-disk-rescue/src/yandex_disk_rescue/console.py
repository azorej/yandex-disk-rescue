"""The output of the `scan`, `download` and `verify` commands: the report on stdout, the notices and status lines on
stderr, and the log file every run writes.

stdout carries the report only, whatever the verbosity; with `--json` it carries one JSON document instead, written
when the command ends, and the report goes to stderr (nowhere with `--quiet`). stderr carries the progress of each slow phase (none with
`--no-progress` or `--quiet`), the notices (WARNING records; INFO too with `--verbose`; none with `--quiet`) and the
final `Error: …` or `Interrupted; …` line. The log file gets every INFO record (DEBUG too with `--verbose`) and every
status line whatever the console flags, and a start and an end record per run.

On a terminal (stderr is one, and `TERM` is set and neither `dumb` nor `unknown`) the progress is a live view, and
notices have no timestamp. Anywhere else it is a timestamped status line every `progress_interval` seconds, and so is
each notice; nothing written then holds a control character, whatever the environment says about colours.
"""

import dataclasses
import json
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, TextIO

import typer

from yandex_disk_rescue import __version__, paths
from yandex_disk_rescue.display import display_line
from yandex_disk_rescue.errors import RescueError
from yandex_disk_rescue.logs import (
    CONSOLE,
    LOGGER_NAME,
    LogFile,
    Notices,
    notice,
    stderr_through,
    timestamp,
    write_stderr,
)
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
Json = Annotated[
    bool,
    typer.Option(
        '--json',
        help='Print the report as one JSON document on stdout when the command ends; the human report goes to stderr.',
    ),
]

SCHEMA_VERSION = 1

FILE_ONLY = {CONSOLE: False}
# JSON leaves these unescaped inside strings; the document escapes them so that it is safe on a terminal too.
_JSON_UNSAFE = re.compile('[\x7f-\x9f\u2028\u2029]')
_log = logging.getLogger(LOGGER_NAME)


class LogFileError(RescueError):
    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f'Could not open the log file {path}: {reason}.')
        self.reason = reason


@dataclass(frozen=True)
class OutputOptions:
    """The verbosity; `progress`, whether stderr gets status lines; the seconds between two status lines; and `json`,
    whether stdout gets the JSON document instead of the report."""

    verbosity: Verbosity
    progress: bool
    progress_interval: int
    json: bool


def output_options(
    *, verbose: bool, quiet: bool, no_progress: bool, progress_interval: int, json_document: bool
) -> OutputOptions:
    """The options of `--verbose`, `--quiet`, `--no-progress`, `--progress-interval` and `--json`; raises
    `typer.BadParameter` when both `--verbose` and `--quiet` are given."""
    if verbose and quiet:
        raise typer.BadParameter('--verbose and --quiet cannot be used together.', param_hint="'-v' / '-q'")
    verbosity: Verbosity = 'verbose' if verbose else 'quiet' if quiet else 'normal'
    return OutputOptions(
        verbosity=verbosity, progress=not no_progress, progress_interval=progress_interval, json=json_document
    )


def is_terminal(stream: TextIO) -> bool:
    """Whether `stream` is a terminal that takes cursor movement: a tty, and `TERM` set and neither `dumb` nor
    `unknown`."""
    return stream.isatty() and os.environ.get('TERM', 'dumb') not in {'dumb', 'unknown'}


class Output:
    """The running command's output: `line` writes one report line, `progress` reports a slow phase, with a live view
    when `terminal`, and `report` gives the JSON object of the command's report."""

    def __init__(self, log_file: LogFile, options: OutputOptions, *, terminal: bool) -> None:
        self._log_file = log_file
        self._options = options
        self._terminal = terminal
        self._view: LiveView | None = None
        self._document: Mapping[str, object] | None = None

    @property
    def log_path(self) -> Path:
        return self._log_file.path

    def line(self, text: str) -> None:
        """Write the report line `text` as one line, its control characters (a newline too) escaped, to stdout, above
        the live view when one is shown; with `--json` to stderr as a notice instead (nowhere with `--quiet`)."""
        text = display_line(text)
        if self._options.json:
            if self._options.verbosity != 'quiet':
                write_stderr(text)
        elif self._view is None:
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
        _log.info('Summary:', extra=FILE_ONLY)
        for line in lines(limit=None):
            _log.info('%s', line, extra=FILE_ONLY)
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

    def report(self, document: Mapping[str, object]) -> None:
        """Make `document`, the JSON object of the command's report, what `--json` prints when the command ends,
        whatever its exit code."""
        self._document = document

    def footer(self, manifest: Path) -> None:
        """The last lines of the report: the manifest's path, then the log's (with why it is incomplete, if it is)."""
        self.line(f'Manifest: {manifest}')
        failure = self._log_file.failure
        self.line(f'Log: {self.log_path}' if failure is None else f'Log: {self.log_path} (incomplete: {failure})')


@contextmanager
def command_output(
    command: str, options: OutputOptions, *, details: Mapping[str, object], interrupted: str
) -> Iterator[Output]:
    """Run `command` with its output: open the log file, attach the log and notice handlers to the
    `yandex_disk_rescue` logger for the run, and log its start (`command`, the version, the pid and `details`) and its
    end: completed, the error and its exit code, interrupted, or an unexpected exception with its traceback. On Ctrl+C
    stderr gets `interrupted`. The handlers are detached and closed on every exit.

    With `--json`, stdout gets one JSON document once everything else is written, on every exit: the common keys
    (`schema_version`, the command, the version, the start and end times, the manifest and the log file, whether the
    log is incomplete, the exit code, and `error` unless it is 0) followed by the object `Output.report` was given, or
    the common keys alone when it was given none.

    Raises `LogFileError` when the log file cannot be opened; nothing is logged then.
    """
    started = time.time()
    path = paths.log_path()
    output: Output | None = None
    try:
        with _logged_run(command, options, details=details, path=path) as output:
            try:
                yield output
            except KeyboardInterrupt:
                typer.echo(interrupted, err=True)
                raise
    except BaseException as exc:
        if options.json:
            _print_document(command, started, path, output, exc, interrupted=interrupted)
        raise
    if options.json:
        _print_document(command, started, path, output, None, interrupted=interrupted)


@contextmanager
def _logged_run(command: str, options: OutputOptions, *, details: Mapping[str, object], path: Path) -> Iterator[Output]:
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


def _print_document(
    command: str, started: float, log: Path, output: Output | None, exc: BaseException | None, *, interrupted: str
) -> None:
    exit_code, error = _result(exc, interrupted)
    match exc:
        case LogFileError():
            log_incomplete = exc.reason
        case _:
            log_incomplete = None if output is None else output._log_file.failure
    document: dict[str, object] = {
        'schema_version': SCHEMA_VERSION,
        'command': command,
        'version': __version__,
        'started_at': timestamp(started),
        'finished_at': timestamp(time.time()),
        'manifest': str(paths.manifest_path()),
        'log': str(log),
        'log_incomplete': log_incomplete,
        'exit_code': exit_code,
    }
    if error is not None:
        document['error'] = {'message': error}
    if output is not None and output._document is not None:
        document.update(output._document)
    text = json.dumps(document, ensure_ascii=False, indent=2)
    text = _JSON_UNSAFE.sub(lambda match: f'\\u{ord(match.group()):04x}', text)
    # A lone surrogate becomes `\udcxx`, the JSON escape of that very code point.
    typer.echo(text.encode('utf-8', 'backslashreplace').decode('utf-8'))


def _result(exc: BaseException | None, interrupted: str) -> tuple[int, str | None]:
    """The exit code of a command that ended with `exc` (`None`: completed), and its error message, if any."""
    match exc:
        case None:
            return 0, None
        case RescueError():
            return exc.exit_code, str(exc)
        case KeyboardInterrupt():
            return 130, interrupted
        case typer.TyperException():
            return exc.exit_code, exc.format_message()
        case typer.Exit():
            return exc.exit_code, None if exc.exit_code == 0 else f'exit {exc.exit_code}'
        case _:
            return 1, f'unexpected error: {type(exc).__name__}: {exc}'


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
