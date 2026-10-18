"""`verify`: check every file of the mirror at a destination against the manifest, with no token and no network,
reading the destination only, continuing an interrupted pass, and summarize what is broken or missing."""

import os
from datetime import UTC
from functools import partial
from pathlib import Path
from typing import Annotated

import typer

from yandex_disk_rescue import console, paths, settings
from yandex_disk_rescue.destination import ReadOnlyDestination
from yandex_disk_rescue.errors import RescueError
from yandex_disk_rescue.logs import timestamp
from yandex_disk_rescue.manifest import Manifest, ManifestMissingError, VerifyPass
from yandex_disk_rescue.progress import VerifyProgress
from yandex_disk_rescue.units import format_size
from yandex_disk_rescue.verifying import VerifyReport, VerifyRun, verify_document, verify_summary

INTERRUPTED = 'Interrupted; rerun verify to continue where it stopped.'


class NotAMirrorError(RescueError):
    exit_code = 2

    def __init__(self, destination: Path, manifest: Path) -> None:
        super().__init__(f'{destination} is not a mirror recorded in the manifest {manifest}; nothing to verify.')


class ProblemsFoundError(RescueError):
    """The check found missing, modified, corrupt, quarantined or not downloaded files."""

    def __init__(self, report: VerifyReport) -> None:
        problems = 'problem' if report.problems == 1 else 'problems'
        lists = 'list' if report.lists_with_problems() == 1 else 'lists'
        super().__init__(f'{report.problems} {problems} found; see the {lists} above.')


def verify(
    dest: Annotated[
        str,
        typer.Argument(metavar='DEST', help='Destination folder of an earlier download.', show_default=False),
    ],
    restart: Annotated[
        bool,
        typer.Option('--restart', help='Start a new pass over every file instead of continuing an interrupted one.'),
    ] = False,
    progress_interval: settings.ProgressInterval = settings.PROGRESS_INTERVAL.default,
    no_progress: console.NoProgress = False,
    verbose: console.Verbose = False,
    quiet: console.Quiet = False,
    json_document: console.Json = False,
) -> None:
    """Re-hash every verified file of the mirror at DEST against the manifest, with no network and nothing written to
    DEST, and report missing, modified, corrupt, quarantined and not downloaded files. An interrupted pass continues
    where it stopped."""
    options = console.output_options(
        verbose=verbose,
        quiet=quiet,
        no_progress=no_progress,
        progress_interval=progress_interval,
        json_document=json_document,
    )
    path = Path(os.path.abspath(dest))
    details = {'dest': path, 'restart': restart}
    with console.command_output('verify', options, details=details, interrupted=INTERRUPTED) as output:
        manifest_path = paths.manifest_path()
        with ReadOnlyDestination.open(path) as destination:
            try:
                manifest = Manifest.open_existing(manifest_path)
            except ManifestMissingError:
                raise NotAMirrorError(destination.path, manifest_path) from None
            with manifest:
                mirror = manifest.mirror_at(destination.path)
                if mirror is None:
                    raise NotAMirrorError(destination.path, manifest_path)
                current = None if restart else mirror.verify_pass()
                verify_pass = current or mirror.start_verify_pass()
                progress = VerifyProgress()
                run = VerifyRun(mirror, destination, verify_pass, progress)
                output.headline(_start_line(run, current, destination.path))
                with output.progress(progress):
                    report = run.run()
        output.report(_document(report, run, verify_pass, continued=current is not None))
        output.summary(partial(verify_summary, report))
        output.footer(manifest_path)
        if report.problems:
            raise ProblemsFoundError(report)


def _document(report: VerifyReport, run: VerifyRun, verify_pass: VerifyPass, *, continued: bool) -> dict[str, object]:
    """The JSON object of the check, with the pass it belongs to and what this part of it read."""
    return {
        **verify_document(report),
        'pass': {
            'started_at': timestamp(verify_pass.started_at.timestamp()),
            'continued': continued,
            'files_read': run.files,
            'size_read': run.size,
        },
    }


def _start_line(run: VerifyRun, continued: VerifyPass | None, destination: Path) -> str:
    files = f'{run.files} {"file" if run.files == 1 else "files"} ({format_size(run.size)})'
    if continued is None:
        return f'Verifying {files} in {destination}'
    started = continued.started_at.astimezone(UTC).strftime('%Y-%m-%d %H:%M')
    return f'Continuing the verification started {started} UTC: {files} left'
