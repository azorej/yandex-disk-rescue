"""`scan`: record the tree of a remote folder in the manifest and summarize what changed."""

from functools import partial
from pathlib import Path
from typing import Annotated

import typer

from yandex_disk_rescue import api, auth, console, interrupts, paths, settings
from yandex_disk_rescue.errors import RescueError
from yandex_disk_rescue.manifest import Manifest
from yandex_disk_rescue.progress import ScanProgress
from yandex_disk_rescue.scanning import ScanResult, resolve_scan_root, scan_document, scan_summary, scan_tree

INTERRUPTED = 'Interrupted; the manifest holds the last completed scan.'


class FoldersNotListedError(RescueError):
    """The scan committed, but some folders could not be listed."""

    def __init__(self, count: int) -> None:
        folders = 'folder' if count == 1 else 'folders'
        super().__init__(f'{count} {folders} could not be listed; see the list above.')


def scan(
    remote_path: Annotated[
        str,
        typer.Argument(
            metavar='REMOTE_PATH',
            help='Remote folder to scan: disk:/ (the whole Disk) or a folder such as disk:/Photos.',
        ),
    ] = api.ROOT,
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
    """Record every file and folder under REMOTE_PATH in the manifest and summarize what changed."""
    options = console.output_options(
        verbose=verbose,
        quiet=quiet,
        no_progress=no_progress,
        progress_interval=progress_interval,
        json_document=json_document,
    )
    root = api.normalize_remote_path(remote_path)
    limits = settings.api_limits(
        interval_ms=api_interval_ms, concurrency=api_concurrency, timeout=api_timeout, retries=api_retries
    )
    details = {
        'remote_path': root,
        'api_interval_ms': api_interval_ms,
        'api_concurrency': api_concurrency,
        'api_timeout': api_timeout,
        'api_retries': api_retries,
        'api_url': api_url,
    }
    with console.command_output('scan', options, details=details, interrupted=INTERRUPTED) as output:
        token = auth.get_token()
        manifest_path = paths.manifest_path()
        result = interrupts.run(_run(token, root, output, base_url=api_url, limits=limits, manifest_path=manifest_path))
        output.report(scan_document(result))
        output.summary(partial(scan_summary, result))
        output.footer(manifest_path)
        if result.unlisted:
            raise FoldersNotListedError(len(result.unlisted))


async def _run(
    token: str, root: str, output: console.Output, *, base_url: str, limits: api.ApiLimits, manifest_path: Path
) -> ScanResult:
    async with api.DiskClient(token, base_url=base_url, limits=limits) as client:
        account = await client.account()
        scan_root = await resolve_scan_root(client, root)
        with Manifest.open(manifest_path) as manifest:
            progress = ScanProgress()
            with output.progress(progress):
                return await scan_tree(client, manifest, account, scan_root, progress)
