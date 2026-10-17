"""`download`: scan a remote folder, mirror it into a local folder with every file verified, and summarize the run."""

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from yandex_disk_rescue import api, auth, paths, settings
from yandex_disk_rescue.destination import Destination, check_destination
from yandex_disk_rescue.errors import RescueError
from yandex_disk_rescue.manifest import Manifest
from yandex_disk_rescue.mirroring import MirrorReport, MirrorRun, mirror_summary
from yandex_disk_rescue.scanning import resolve_scan_root, scan_summary, scan_tree
from yandex_disk_rescue.units import format_size


class FilesFailedError(RescueError):
    def __init__(self, count: int) -> None:
        super().__init__(f'{count} failed; see the list above.')


def download(
    locations: Annotated[
        list[str],
        typer.Argument(
            metavar='[REMOTE_PATH] DEST',
            help='Remote folder to mirror (default disk:/, the whole Disk), then the local destination folder.',
            show_default=False,
        ),
    ],
    download_timeout: settings.DownloadTimeout = settings.DOWNLOAD_TIMEOUT.default,
    api_interval_ms: settings.ApiIntervalMs = settings.API_INTERVAL_MS.default,
    api_concurrency: settings.ApiConcurrency = settings.API_CONCURRENCY.default,
    api_timeout: settings.ApiTimeout = settings.API_TIMEOUT.default,
    api_retries: settings.ApiRetries = settings.API_RETRIES.default,
    api_url: settings.ApiUrl = settings.API_URL.default,
) -> None:
    """Scan REMOTE_PATH, then mirror it into DEST: every file verified, verified files skipped on a rerun."""
    remote_path, dest = _split_locations(locations)
    root = api.normalize_remote_path(remote_path)
    destination = check_destination(dest)
    token = auth.get_token()
    limits = settings.api_limits(
        interval_ms=api_interval_ms, concurrency=api_concurrency, timeout=api_timeout, retries=api_retries
    )
    manifest_path = paths.manifest_path()
    try:
        report = asyncio.run(
            _run(
                token,
                root,
                destination,
                base_url=api_url,
                limits=limits,
                download_timeout=download_timeout,
                manifest_path=manifest_path,
            )
        )
    except KeyboardInterrupt:
        typer.echo('Interrupted; rerun to continue the download.', err=True)
        raise
    for line in mirror_summary(report):
        typer.echo(line)
    typer.echo(f'Manifest: {manifest_path}')
    if report.failed:
        raise FilesFailedError(len(report.failed))


def _split_locations(locations: list[str]) -> tuple[str, str]:
    match locations:
        case [dest]:
            remote_path = api.ROOT
        case [remote_path, dest]:
            pass
        case _:
            raise typer.BadParameter(
                f'expected DEST or REMOTE_PATH DEST, got {len(locations)} paths.', param_hint="'[REMOTE_PATH] DEST'"
            )
    if dest.startswith('disk:'):
        raise typer.BadParameter(
            f'{dest} is a Disk path; give the remote folder first: download [REMOTE_PATH] DEST.',
            param_hint="'[REMOTE_PATH] DEST'",
        )
    return remote_path, dest


async def _run(
    token: str,
    root: str,
    destination_path: Path,
    *,
    base_url: str,
    limits: api.ApiLimits,
    download_timeout: int,
    manifest_path: Path,
) -> MirrorReport:
    async with api.DiskClient(token, base_url=base_url, limits=limits) as client:
        account = await client.account()
        scan_root = await resolve_scan_root(client, root)
        with Destination.open(destination_path) as destination, Manifest.open(manifest_path) as manifest:
            mirror = manifest.mirror(account.uid, scan_root.path, destination.path)
            scan = await scan_tree(client, manifest, account, scan_root)
            for line in scan_summary(scan):
                typer.echo(line)
            run = MirrorRun(mirror, destination, unencodable=scan.unencodable)
            files = 'file' if run.pending_files == 1 else 'files'
            typer.echo(
                f'Downloading {run.pending_files} {files} ({format_size(run.pending_size)}) to {destination.path}'
            )
            async with api.ContentClient(timeout=download_timeout) as content:
                return await run.run(client, content)
