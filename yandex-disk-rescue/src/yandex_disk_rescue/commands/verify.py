"""`verify`: check every file of the mirror at a destination against the manifest, with no token and no network,
reading the destination only, and summarize what is broken or missing."""

import os
from pathlib import Path
from typing import Annotated

import typer

from yandex_disk_rescue import paths
from yandex_disk_rescue.destination import ReadOnlyDestination
from yandex_disk_rescue.errors import RescueError
from yandex_disk_rescue.manifest import Manifest
from yandex_disk_rescue.units import format_size
from yandex_disk_rescue.verifying import VerifyReport, VerifyRun, verify_summary


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
) -> None:
    """Re-hash every verified file of the mirror at DEST against the manifest, with no network and nothing written to
    DEST, and report missing, modified, corrupt, quarantined and not downloaded files."""
    manifest_path = paths.manifest_path()
    try:
        with (
            ReadOnlyDestination.open(Path(os.path.abspath(dest))) as destination,
            Manifest.open(manifest_path) as manifest,
        ):
            mirror = manifest.mirror_at(destination.path)
            if mirror is None:
                raise NotAMirrorError(destination.path, manifest_path)
            run = VerifyRun(mirror, destination)
            files = 'file' if run.files == 1 else 'files'
            typer.echo(f'Verifying {run.files} {files} ({format_size(run.size)}) in {destination.path}')
            report = run.run()
    except KeyboardInterrupt:
        typer.echo('Interrupted; rerun verify to check the mirror again.', err=True)
        raise
    for line in verify_summary(report):
        typer.echo(line)
    typer.echo(f'Manifest: {manifest_path}')
    if report.problems:
        raise ProblemsFoundError(report)
