"""`scan`: record the tree of a remote folder in the manifest and summarize what changed."""

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from yandex_disk_rescue import api, auth, paths, settings
from yandex_disk_rescue.manifest import Manifest
from yandex_disk_rescue.scanning import ScanResult, resolve_scan_root, scan_summary, scan_tree


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
) -> None:
    """Record every file and folder under REMOTE_PATH in the manifest and summarize what changed."""
    root = api.normalize_remote_path(remote_path)
    token = auth.get_token()
    limits = settings.api_limits(
        interval_ms=api_interval_ms, concurrency=api_concurrency, timeout=api_timeout, retries=api_retries
    )
    manifest_path = paths.manifest_path()
    try:
        result = asyncio.run(_run(token, root, base_url=api_url, limits=limits, manifest_path=manifest_path))
    except KeyboardInterrupt:
        typer.echo('Interrupted; the manifest holds the last completed scan.', err=True)
        raise
    for line in scan_summary(result):
        typer.echo(line)
    typer.echo(f'Manifest: {manifest_path}')


async def _run(token: str, root: str, *, base_url: str, limits: api.ApiLimits, manifest_path: Path) -> ScanResult:
    async with api.DiskClient(token, base_url=base_url, limits=limits) as client:
        account = await client.account()
        scan_root = await resolve_scan_root(client, root)
        with Manifest.open(manifest_path) as manifest:
            return await scan_tree(client, manifest, account, scan_root)
