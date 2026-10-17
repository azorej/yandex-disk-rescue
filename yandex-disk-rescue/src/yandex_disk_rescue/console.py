"""The output of the `scan`, `download` and `verify` commands: the report on stdout, the notices on stderr, and the
log file every run writes.

stdout carries the report only, whatever the verbosity. stderr carries the notices (WARNING records; INFO too with
`--verbose`; none with `--quiet`) and the final `Error: …` or `Interrupted; …` line. The log file gets every INFO
record (DEBUG too with `--verbose`) whatever the console flags, and a start and an end record per run.
"""

import logging
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import typer

from yandex_disk_rescue import __version__, paths
from yandex_disk_rescue.errors import RescueError
from yandex_disk_rescue.logs import CONSOLE, LOGGER_NAME, LogFile, Notices, notice

type Verbosity = Literal['quiet', 'normal', 'verbose']

Verbose = Annotated[
    bool,
    typer.Option(
        '-v',
        '--verbose',
        help='Also show the INFO notices on stderr, and log DEBUG records to the log file.',
    ),
]
Quiet = Annotated[
    bool,
    typer.Option('-q', '--quiet', help='Show nothing on stderr but the final error line; stdout is unchanged.'),
]

FILE_ONLY = {CONSOLE: False}
_log = logging.getLogger(LOGGER_NAME)


class LogFileError(RescueError):
    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f'Could not open the log file {path}: {reason}.')


@dataclass(frozen=True)
class OutputOptions:
    verbosity: Verbosity


def output_options(*, verbose: bool, quiet: bool) -> OutputOptions:
    """The options of `--verbose` and `--quiet`; raises `typer.BadParameter` when both are given."""
    if verbose and quiet:
        raise typer.BadParameter('--verbose and --quiet cannot be used together.', param_hint="'-v' / '-q'")
    return OutputOptions(verbosity='verbose' if verbose else 'quiet' if quiet else 'normal')


class Output:
    """The running command's output: `line` writes one report line to stdout."""

    def __init__(self, log_file: LogFile) -> None:
        self._log_file = log_file

    @property
    def log_path(self) -> Path:
        return self._log_file.path

    def line(self, text: str) -> None:
        typer.echo(text)

    def summary(self, lines: Callable[..., list[str]]) -> None:
        """Log the summary `lines(limit=None)`, every list in full, to the log file, then print `lines()`, whose lists
        show their first few members."""
        _log.info('Summary:\n%s', '\n'.join(lines(limit=None)), extra=FILE_ONLY)
        for line in lines():
            self.line(line)

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

    def log_failed(reason: str) -> None:
        notice(f'Warning: could not write the log file {path}: {reason}; logging to it is off for this run.')

    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        log_file = LogFile(path, command, log_failed)
    except OSError as exc:
        raise LogFileError(path, exc.strerror or str(exc)) from None
    handlers: list[logging.Handler] = [log_file]
    if options.verbosity != 'quiet':
        handlers.append(Notices(logging.INFO if options.verbosity == 'verbose' else logging.WARNING))
    level = _log.level
    _log.setLevel(logging.DEBUG if options.verbosity == 'verbose' else logging.INFO)
    for handler in handlers:
        _log.addHandler(handler)
    try:
        arguments = ', '.join(f'{name}={value}' for name, value in {**details, 'verbosity': options.verbosity}.items())
        _log.info('%s started: version %s, pid %d; %s', command, __version__, os.getpid(), arguments, extra=FILE_ONLY)
        try:
            yield Output(log_file)
        except BaseException as exc:
            _log_end(command, exc)
            raise
        _log.info('%s completed (exit 0)', command, extra=FILE_ONLY)
    finally:
        for handler in handlers:
            _log.removeHandler(handler)
            handler.close()
        _log.setLevel(level)


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
