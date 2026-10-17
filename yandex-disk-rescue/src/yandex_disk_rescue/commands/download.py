"""`download`: scan a remote folder (unless a recent scan covers it), mirror it into a local folder with every file
verified, and summarize the run."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from yandex_disk_rescue import api, auth, paths, settings
from yandex_disk_rescue.destination import Destination, check_destination
from yandex_disk_rescue.errors import RescueError
from yandex_disk_rescue.manifest import Manifest, ScanTime
from yandex_disk_rescue.mirroring import MirrorReport, MirrorRun, mirror_summary
from yandex_disk_rescue.scanning import resolve_scan_root, scan_summary, scan_tree
from yandex_disk_rescue.units import format_size


class FilesFailedError(RescueError):
    def __init__(self, count: int) -> None:
        super().__init__(f'{count} failed; see the list above.')


class NeverScannedError(RescueError):
    exit_code = 2

    def __init__(self, root: str) -> None:
        super().__init__(f"{root} has never been scanned; run 'yandex-disk-rescue scan {root}' or drop --no-scan.")


def download(
    locations: Annotated[
        list[str],
        typer.Argument(
            metavar='[REMOTE_PATH] DEST',
            help='Remote folder to mirror (default disk:/, the whole Disk), then the local destination folder.',
            show_default=False,
        ),
    ],
    no_scan: Annotated[
        bool,
        typer.Option(
            '--no-scan',
            help='Use the manifest as it is, whatever the age of its scan, instead of scanning REMOTE_PATH first.',
        ),
    ] = False,
    scan_max_age: settings.ScanMaxAge = settings.SCAN_MAX_AGE.default,
    download_timeout: settings.DownloadTimeout = settings.DOWNLOAD_TIMEOUT.default,
    api_interval_ms: settings.ApiIntervalMs = settings.API_INTERVAL_MS.default,
    api_concurrency: settings.ApiConcurrency = settings.API_CONCURRENCY.default,
    api_timeout: settings.ApiTimeout = settings.API_TIMEOUT.default,
    api_retries: settings.ApiRetries = settings.API_RETRIES.default,
    api_url: settings.ApiUrl = settings.API_URL.default,
) -> None:
    """Scan REMOTE_PATH unless a recent scan covers it, then mirror it into DEST: every file verified, verified files
    skipped on a rerun."""
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
                scan_policy=_ScanPolicy(no_scan=no_scan, max_age=timedelta(minutes=scan_max_age)),
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


@dataclass(frozen=True)
class _ScanPolicy:
    """When `download` scans: `no_scan` never scans; otherwise a scan younger than `max_age` is reused."""

    no_scan: bool
    max_age: timedelta

    def reused(self, root: str, last: ScanTime | None) -> str | None:
        """The line naming the scan to reuse for `root`, or `None` when the run must scan.

        Raises `NeverScannedError` under `--no-scan` when no completed scan covers `root`. A scan completed in the
        future (the clock went back) is never reused.
        """
        if last is None:
            if self.no_scan:
                raise NeverScannedError(root)
            return None
        completed = f'Using the scan of {last.root} completed {last.completed_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC'
        if self.no_scan:
            return f'{completed} (--no-scan)'
        age = datetime.now(UTC) - last.completed_at
        if timedelta(0) <= age < self.max_age:
            return f'{completed} ({age // timedelta(minutes=1)} min ago)'
        return None


async def _run(
    token: str,
    root: str,
    destination_path: Path,
    *,
    base_url: str,
    limits: api.ApiLimits,
    scan_policy: _ScanPolicy,
    download_timeout: int,
    manifest_path: Path,
) -> MirrorReport:
    async with api.DiskClient(token, base_url=base_url, limits=limits) as client:
        account = await client.account()
        scan_root = await resolve_scan_root(client, root)
        with Manifest.open(manifest_path) as manifest:
            reused = scan_policy.reused(scan_root.path, manifest.last_scan(account.uid, scan_root.path))
            with Destination.open(destination_path) as destination:
                mirror = manifest.mirror(account.uid, scan_root.path, destination.path)
                if reused is None:
                    for line in scan_summary(await scan_tree(client, manifest, account, scan_root)):
                        typer.echo(line)
                else:
                    typer.echo(reused)
                return await _mirror(client, MirrorRun(mirror, destination), destination, download_timeout)


async def _mirror(
    client: api.DiskClient, run: MirrorRun, destination: Destination, download_timeout: int
) -> MirrorReport:
    files = 'file' if run.pending_files == 1 else 'files'
    typer.echo(f'Downloading {run.pending_files} {files} ({format_size(run.pending_size)}) to {destination.path}')
    async with api.ContentClient(timeout=download_timeout) as content:
        return await run.run(client, content)
