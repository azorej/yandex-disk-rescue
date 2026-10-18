"""`download`: scan a remote folder (unless a recent scan covers it), mirror it into a local folder with every file
verified, or retry only the files whose last attempt failed, was quarantined or never finished and those `verify`
found broken, and summarize the run."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Annotated

import typer

from yandex_disk_rescue import api, auth, console, interrupts, paths, settings
from yandex_disk_rescue.destination import Destination, check_destination, check_partial_dir
from yandex_disk_rescue.errors import RescueError
from yandex_disk_rescue.logs import timestamp
from yandex_disk_rescue.manifest import Manifest, ScanTime
from yandex_disk_rescue.mirroring import (
    MirrorReport,
    MirrorRun,
    PartialMove,
    PartialRoom,
    TransferPolicy,
    mirror_document,
    mirror_summary,
)
from yandex_disk_rescue.progress import MirrorProgress, MoveProgress, ScanProgress
from yandex_disk_rescue.scanning import resolve_scan_root, scan_document, scan_summary, scan_tree
from yandex_disk_rescue.units import format_size

INTERRUPTED = 'Interrupted; rerun to continue the download.'


class FilesNotMirroredError(RescueError):
    """Files of the run failed or are quarantined."""

    def __init__(self, failed: int, quarantined: int) -> None:
        if failed and quarantined:
            super().__init__(f'{failed} failed and {quarantined} quarantined; see the lists above.')
        elif quarantined:
            super().__init__(f'{quarantined} quarantined; see the list above.')
        else:
            super().__init__(f'{failed} failed; see the list above.')


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
    retry_failed: Annotated[
        bool,
        typer.Option(
            '--retry-failed',
            help='Download only the files whose last attempt in DEST failed, was quarantined or never finished, the '
            'files verify found missing, modified or corrupt, and the folders that failed; leave everything else '
            'alone.',
        ),
    ] = False,
    partial_dir: settings.PartialDir = settings.PARTIAL_DIR.default,
    scan_max_age: settings.ScanMaxAge = settings.SCAN_MAX_AGE.default,
    download_timeout: settings.DownloadTimeout = settings.DOWNLOAD_TIMEOUT.default,
    download_retries: settings.DownloadRetries = settings.DOWNLOAD_RETRIES.default,
    download_passes: settings.DownloadPasses = settings.DOWNLOAD_PASSES.default,
    download_connections: settings.DownloadConnections = settings.DOWNLOAD_CONNECTIONS.default,
    download_checkpoint_mib: settings.DownloadCheckpointMib = settings.DOWNLOAD_CHECKPOINT_MIB.default,
    download_segments: settings.DownloadSegments = settings.DOWNLOAD_SEGMENTS.default,
    download_segment_threshold_mib: settings.DownloadSegmentThresholdMib = (
        settings.DOWNLOAD_SEGMENT_THRESHOLD_MIB.default
    ),
    api_interval_ms: settings.ApiIntervalMs = settings.API_INTERVAL_MS.default,
    api_concurrency: settings.ApiConcurrency = settings.API_CONCURRENCY.default,
    api_timeout: settings.ApiTimeout = settings.API_TIMEOUT.default,
    api_retries: settings.ApiRetries = settings.API_RETRIES.default,
    api_url: settings.ApiUrl = settings.API_URL.default,
    progress_interval: settings.ProgressInterval = settings.PROGRESS_INTERVAL.default,
    no_progress: console.NoProgress = False,
    verbose: console.Verbose = False,
    quiet: console.Quiet = False,
    json_document: console.Json = False,
) -> None:
    """Scan REMOTE_PATH unless a recent scan covers it, then mirror it into DEST: every file verified, verified files
    skipped on a rerun, or with --retry-failed only the failed, quarantined, unfinished and broken ones."""
    options = console.output_options(
        verbose=verbose,
        quiet=quiet,
        no_progress=no_progress,
        progress_interval=progress_interval,
        json_document=json_document,
    )
    remote_path, dest = _split_locations(locations)
    root = api.normalize_remote_path(remote_path)
    destination = check_destination(dest)
    partial_folder = check_partial_dir(partial_dir, destination)
    limits = settings.api_limits(
        interval_ms=api_interval_ms, concurrency=api_concurrency, timeout=api_timeout, retries=api_retries
    )
    transfers = _Transfers(
        timeout=download_timeout,
        retries=download_retries,
        passes=download_passes,
        connections=download_connections,
        checkpoint_mib=download_checkpoint_mib,
        segments=download_segments,
        segment_threshold_mib=download_segment_threshold_mib,
    )
    details = {
        'remote_path': root,
        'dest': destination,
        'no_scan': no_scan,
        'retry_failed': retry_failed,
        'partial_dir': partial_folder,
        'scan_max_age': scan_max_age,
        'download_timeout': download_timeout,
        'download_retries': download_retries,
        'download_passes': download_passes,
        'download_connections': download_connections,
        'download_checkpoint_mib': download_checkpoint_mib,
        'download_segments': download_segments,
        'download_segment_threshold_mib': download_segment_threshold_mib,
        'api_interval_ms': api_interval_ms,
        'api_concurrency': api_concurrency,
        'api_timeout': api_timeout,
        'api_retries': api_retries,
        'api_url': api_url,
    }
    with console.command_output('download', options, details=details, interrupted=INTERRUPTED) as output:
        token = auth.get_token()
        manifest_path = paths.manifest_path()
        report, facts = interrupts.run(
            _run(
                token,
                root,
                destination,
                output,
                partial_dir=partial_folder,
                base_url=api_url,
                limits=limits,
                scan_policy=_ScanPolicy(no_scan=no_scan, max_age=timedelta(minutes=scan_max_age)),
                retry_failed=retry_failed,
                transfers=transfers,
                manifest_path=manifest_path,
            )
        )
        output.report(
            {
                **mirror_document(report),
                'retry_failed': retry_failed,
                'partial_dir': None if partial_folder is None else str(partial_folder),
                **facts,
            }
        )
        output.summary(partial(mirror_summary, report))
        output.footer(manifest_path)
        if report.failed or report.quarantined:
            raise FilesNotMirroredError(len(report.failed), len(report.quarantined))


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


@dataclass(frozen=True)
class _Transfers:
    """How files are fetched: the stall timeout in seconds, the immediate retries after new data, the passes (0: no
    limit), the requests to the download host in flight at most, the MiB a segment receives between two checkpoints,
    and the segments of a file larger than `segment_threshold_mib` MiB."""

    timeout: int
    retries: int
    passes: int
    connections: int
    checkpoint_mib: int
    segments: int
    segment_threshold_mib: int


async def _run(
    token: str,
    root: str,
    destination_path: Path,
    output: console.Output,
    *,
    partial_dir: Path | None,
    base_url: str,
    limits: api.ApiLimits,
    scan_policy: _ScanPolicy,
    retry_failed: bool,
    transfers: _Transfers,
    manifest_path: Path,
) -> tuple[MirrorReport, dict[str, object]]:
    """The report of the run, and the JSON members it does not hold: the scan, the files to download, the move of
    the partial files and the room measured in the partial folder."""
    async with api.DiskClient(token, base_url=base_url, limits=limits) as client:
        account = await client.account()
        scan_root = await resolve_scan_root(client, root)
        with Manifest.open(manifest_path) as manifest:
            last = manifest.last_scan(account.uid, scan_root.path)
            reused = scan_policy.reused(scan_root.path, last)
            with Destination.open(destination_path, partial_dir=partial_dir) as destination:
                mirror = manifest.mirror(account.uid, scan_root.path, destination.path)
                scan: dict[str, object]
                if reused is None or last is None:
                    scan_progress = ScanProgress()
                    with output.progress(scan_progress):
                        scanned = await scan_tree(client, manifest, account, scan_root, scan_progress)
                    output.summary(partial(scan_summary, scanned))
                    scan = {'reused': False, **scan_document(scanned)}
                else:
                    output.headline(reused)
                    scan = {'reused': True, 'root': last.root, 'completed_at': timestamp(last.completed_at.timestamp())}
                progress = MirrorProgress()
                with output.progress(progress):
                    run = MirrorRun(mirror, destination, retry_failed=retry_failed, progress=progress)
                report, facts = await _mirror(
                    client, run, destination, output, progress, transfers, retry_failed=retry_failed
                )
                return report, {'scan': scan, **facts}


async def _mirror(
    client: api.DiskClient,
    run: MirrorRun,
    destination: Destination,
    output: console.Output,
    progress: MirrorProgress,
    transfers: _Transfers,
    *,
    retry_failed: bool,
) -> tuple[MirrorReport, dict[str, object]]:
    files = 'file' if run.pending_files == 1 else 'files'
    size = format_size(run.pending_size)
    if retry_failed:
        output.headline(
            f'Retrying {run.pending_files} failed, quarantined, unfinished or broken {files} ({size}) in '
            f'{destination.path}'
        )
    else:
        output.headline(f'Downloading {run.pending_files} {files} ({size}) to {destination.path}')
    if (move := run.partial_move) is not None and move.downloads:
        if move.missing:
            output.headline(
                f'The partial folder of the last run, {move.source}, is missing; its downloads in progress start '
                'again from their first byte.'
            )
        else:
            downloads = 'download' if move.downloads == 1 else 'downloads'
            output.headline(
                f'Moving the confirmed bytes ({format_size(move.size)}) of {move.downloads} {downloads} in progress '
                f'from {move.source} to {move.target}'
            )
    move_progress = MoveProgress()
    with output.progress(move_progress):
        await run.move_partials(move_progress)
    async with api.ContentClient(timeout=transfers.timeout, connections=transfers.connections) as content:
        policy = TransferPolicy(
            retries=transfers.retries,
            checkpoint_bytes=transfers.checkpoint_mib * 2**20,
            segments=transfers.segments,
            segment_threshold=transfers.segment_threshold_mib * 2**20,
        )
        room: dict[str, object] | None = None

        def room_measured(measured: PartialRoom) -> None:
            nonlocal room
            available = max(measured.capacity, 0)
            room = {'folder': str(measured.folder), 'available': available}
            output.headline(f'Partial files in {measured.folder} ({format_size(available)} available)')

        progress.start_transferring(run.pending_files, run.pending_size, passes=transfers.passes)
        with output.progress(progress):
            report = await run.run(client, content, policy, passes=transfers.passes, room_measured=room_measured)
    facts = {
        'to_download': {'files': run.pending_files, 'size': run.pending_size},
        'partial_move': None if move is None else _move_document(move),
        'partial_room': room,
    }
    return report, facts


def _move_document(move: PartialMove) -> dict[str, object]:
    return {
        'source': str(move.source),
        'target': str(move.target),
        'downloads': move.downloads,
        'size': move.size,
        'missing': move.missing,
    }
