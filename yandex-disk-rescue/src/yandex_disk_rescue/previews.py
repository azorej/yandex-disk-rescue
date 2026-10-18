"""The preview step at the end of a `download` run: the unfinished previews a crashed save left and the preview images
of verified files are deleted, and, unless previews are turned off, each file that failed on Yandex's side gets
Yandex's largest preview of it, saved below `.yandex-disk-rescue/previews/` and recorded in the manifest.

The step never fails the run. A preview that cannot be fetched, saved or deleted is reported in
`MirrorReport.preview_errors`, and the step goes on; a token Yandex rejects, or an error in the previews folder that no
other preview would escape (`WORK_FATAL_ERRNOS`: a full disk or quota, an I/O error, a read-only filesystem, a
permission error), ends the step, and every file not handled yet is reported with that reason. Ctrl+C or SIGTERM ends
the step only: the fetches in flight are dropped, a save already running in its worker thread is waited for and
recorded, and the step returns normally with the files it did not get to counted in `MirrorReport.previews_pending`.
"""

import asyncio
import logging
import stat
from collections.abc import Iterable, Iterator
from pathlib import PurePosixPath

from yandex_disk_rescue import api
from yandex_disk_rescue.destination import PREVIEWS_DIR_NAME, WORK_FATAL_ERRNOS, Destination
from yandex_disk_rescue.disk_threads import DiskThreads
from yandex_disk_rescue.local_names import WORK_DIR_NAME
from yandex_disk_rescue.manifest import Mirror, MirrorEntry, PreviewRecord, RemoteIdentity, verified_now
from yandex_disk_rescue.mirroring import Failure, MirrorReport, PreviewOnly, concurrently
from yandex_disk_rescue.progress import PreviewProgress
from yandex_disk_rescue.units import format_size

CHANGED = 'changed on the Disk since the scan; rerun with --scan-max-age 0'
_log = logging.getLogger(__name__)


def preview_candidates(report: MirrorReport) -> list[Failure]:
    """The failed files of `report` the preview step handles: those whose failure came from Yandex's side."""
    return [failure for failure in report.failed if failure.on_yandex]


async def save_previews(
    report: MirrorReport,
    mirror: Mirror,
    destination: Destination,
    client: api.DiskClient,
    progress: PreviewProgress,
    *,
    fetch: bool,
) -> None:
    """Run the preview step of the run `report` describes, reporting it to `progress` and in `report`.

    First the unfinished previews a crashed save left below `previews/` are deleted, and every preview whose file is
    verified for its current identity is deleted with its record; one that cannot be deleted keeps its record. Then
    each of `preview_candidates(report)`, up to the client's concurrency at once: a file whose preview of its current
    identity is saved, a regular file at the recorded path, is listed as it is; with `fetch`, any other asks the API
    for its preview, fetches it at once, saves it in a worker thread, records it, and deletes the file of an earlier
    record at another path. A file Yandex offers no preview of, or no longer has, is listed nowhere, so the next run
    asks again; one that changed on the Disk since the scan is a preview error, and nothing is fetched for it. Without
    `fetch` no request is made, and a file with no preview saved is listed nowhere. The lists of `report` end in path
    order.

    Returns normally when Ctrl+C or SIGTERM cancels it, with the cancellation taken back (`Task.uncancel`). Raises
    `ManifestError` when the manifest cannot be updated.
    """
    candidates = preview_candidates(report)
    entries = {entry.resource.path: entry for entry in mirror.entries()}
    workers = client.limits.concurrency
    with DiskThreads(workers) as threads:
        step = _PreviewStep(report, mirror, destination, client, progress, threads, fetch=fetch)
        if candidates:
            progress.start(len(candidates))
        try:
            await step.remove_unfinished()
            await step.clean_up(entries.values())
            queue = iter(candidates)
            await concurrently([step.work(queue, entries) for _ in range(workers)])
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is None:
                raise RuntimeError('the preview step runs outside a task') from None
            task.uncancel()
            report.previews_pending = len(candidates) - step.handled
            _log.info('Previews interrupted: %d of %d files not handled', report.previews_pending, len(candidates))
    report.preview_only.sort(key=lambda saved: saved.path)
    report.preview_errors.sort(key=lambda error: error.path)


class _PreviewStep:
    """The state the workers of one preview step share: the files handled so far, and the reason the step stopped,
    once it has."""

    def __init__(
        self,
        report: MirrorReport,
        mirror: Mirror,
        destination: Destination,
        client: api.DiskClient,
        progress: PreviewProgress,
        threads: DiskThreads,
        *,
        fetch: bool,
    ) -> None:
        self._report = report
        self._mirror = mirror
        self._destination = destination
        self._client = client
        self._progress = progress
        self._threads = threads
        self._fetch = fetch
        self._stopped: str | None = None
        self.handled = 0

    async def remove_unfinished(self) -> None:
        """Delete the unfinished previews below `previews/`; an error is logged as a warning, and the step goes on."""
        try:
            removed = await self._threads.run(self._destination.remove_unfinished_previews)
        except OSError as exc:
            folder = self._destination.path / WORK_DIR_NAME / PREVIEWS_DIR_NAME
            _log.warning('Could not delete the unfinished previews in %s: %s', folder, exc.strerror)
            return
        if removed:
            _log.info('%d unfinished previews deleted', removed)

    async def clean_up(self, entries: Iterable[MirrorEntry]) -> None:
        """Delete the preview and the record of each of `entries` verified for its current identity."""
        for entry in entries:
            if self._stopped is not None:
                return
            if entry.preview is not None and verified_now(entry):
                await self._remove(entry, entry.preview.path, forget=True)

    async def work(self, candidates: Iterator[Failure], entries: dict[str, MirrorEntry]) -> None:
        """Handle the next file of `candidates`, which every worker shares, until none is left."""
        for failure in candidates:
            entry = entries[failure.path]
            if self._stopped is None:
                await self._handle(entry)
            else:
                self._failed(entry, self._stopped)

    async def _handle(self, entry: MirrorEntry) -> None:
        identity, earlier = RemoteIdentity.of(entry.resource), entry.preview
        if earlier is not None and earlier.identity == identity:
            try:
                kept = await self._threads.run(self._destination.lstat, earlier.path)
            except OSError as exc:
                self._os_error(entry, f'could not inspect {earlier.path}', exc)
                return
            if kept is not None and stat.S_ISREG(kept.st_mode):
                _log.debug('%s: its preview is saved at %s already', entry.resource.path, earlier.path)
                self._listed(entry, earlier.path, earlier.bytes)
                return
        if not self._fetch:
            self._settled(saved=False)
            return
        try:
            found = await self._client.preview(entry.resource.path)
        except api.RemotePathNotFoundError:
            found = None
        except api.TokenRejectedError as exc:
            self._stopped = str(exc)
            self._failed(entry, self._stopped)
            return
        except (api.DiskApiError, api.PreviewRefusedError) as exc:
            self._failed(entry, str(exc))
            return
        if found is None:
            _log.info('%s: no preview on Yandex Disk', entry.resource.path)
            self._settled(saved=False)
            return
        if found.resource.kind != 'file' or RemoteIdentity.of(found.resource) != identity:
            self._failed(entry, CHANGED)
            return
        try:
            preview = await self._client.fetch_preview(found.link)
        except (api.DiskApiError, api.PreviewRefusedError) as exc:
            self._failed(entry, str(exc))
            return
        path = Destination.preview_path(_local_path(entry), preview.content_type)
        record = PreviewRecord(identity, path, len(preview.content), preview.content_type, found.link.size_name)
        await self._save(entry, record, preview.content)

    async def _save(self, entry: MirrorEntry, record: PreviewRecord, content: bytes) -> None:
        """Save `content` as the preview `record` names, then record it; once the save has started in its thread, a
        cancellation waits for the save and the record. The file of an earlier record at another path is deleted
        last."""

        def saved(_: None) -> None:
            self._mirror.record_preview(entry.entry_id, record)
            _log.info('%s: preview saved at %s (%s)', entry.resource.path, record.path, format_size(record.bytes))
            self._listed(entry, record.path, record.bytes)

        try:
            await self._threads.run_then(self._destination.save_preview, record.path, content, then=saved)
        except OSError as exc:
            self._os_error(entry, f'could not save the preview {record.path}', exc)
            return
        earlier = entry.preview
        if earlier is not None and earlier.path != record.path:
            await self._remove(entry, earlier.path, forget=False)

    async def _remove(self, entry: MirrorEntry, path: PurePosixPath, *, forget: bool) -> None:
        """Delete the preview of `entry` at `path`, and with `forget` its record once the file is gone. A failure is a
        preview error that does not count the file as handled."""

        def removed(_: None) -> None:
            if forget:
                self._mirror.forget_preview(entry.entry_id)
            _log.info('%s: its preview %s deleted', entry.resource.path, path)

        try:
            await self._threads.run_then(self._destination.remove_preview, path, then=removed)
        except OSError as exc:
            self._os_error(entry, f'could not delete the preview {path}', exc, settles=False)

    def _listed(self, entry: MirrorEntry, preview: PurePosixPath, size: int) -> None:
        self._report.preview_only.append(PreviewOnly(entry.resource.path, preview, size))
        self._settled(saved=True)

    def _os_error(self, entry: MirrorEntry, failing: str, exc: OSError, *, settles: bool = True) -> None:
        """Report `exc` from `failing` as the preview error of `entry`; one no other preview would escape stops the
        step."""
        if exc.errno in WORK_FATAL_ERRNOS:
            folder = self._destination.path / WORK_DIR_NAME / PREVIEWS_DIR_NAME
            self._stopped = f'could not use previews in {folder}: {exc.strerror}'
            self._failed(entry, self._stopped, settles=settles)
        else:
            self._failed(entry, f'{failing}: {exc.strerror}', settles=settles)

    def _failed(self, entry: MirrorEntry, reason: str, *, settles: bool = True) -> None:
        """Report the preview error `reason` of `entry`; with `settles`, its preview is not saved and the file is
        handled."""
        self._report.preview_errors.append(Failure(entry.resource.path, reason))
        if settles:
            _log.warning('%s: preview not saved: %s', entry.resource.path, reason)
            self._settled(saved=False)
        else:
            _log.warning('%s: %s', entry.resource.path, reason)

    def _settled(self, *, saved: bool) -> None:
        self.handled += 1
        self._progress.handled(saved=saved)


def _local_path(entry: MirrorEntry) -> PurePosixPath:
    if entry.local_path is None:
        raise ValueError(f'{entry.resource.path} has no local name')
    return entry.local_path
