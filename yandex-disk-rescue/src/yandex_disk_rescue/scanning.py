"""The scan engine: walk a remote folder through the Disk API into the manifest, and the lines summarizing it."""

import asyncio
import bisect
from collections.abc import Sequence
from dataclasses import dataclass, field

from yandex_disk_rescue import api
from yandex_disk_rescue.manifest import Manifest, ScanDiff, ScanSession
from yandex_disk_rescue.units import format_size

SHOWN_PATHS = 20
# Rows are staged in batches: each `ScanSession.add` is one transaction with an fsync, run on the event loop.
_STAGE_BATCH = 1000


@dataclass(frozen=True)
class ScanRoot:
    """A folder ready to scan: `path` as the API spells it, and its resource (`None` for `disk:/`, which has no row)."""

    path: str
    resource: api.Resource | None


@dataclass
class PathSample:
    """The count of a set of paths and its `SHOWN_PATHS` smallest members, so the sample does not depend on timing."""

    count: int = 0
    smallest: list[str] = field(default_factory=list)

    def add(self, path: str) -> None:
        self.count += 1
        bisect.insort(self.smallest, path)
        del self.smallest[SHOWN_PATHS:]


@dataclass
class ScanTotals:
    files: int = 0
    folders: int = 0
    size: int = 0
    without_checksum: PathSample = field(default_factory=PathSample)

    def count(self, resource: api.Resource) -> None:
        if resource.kind == 'dir':
            self.folders += 1
            return
        self.files += 1
        self.size += resource.size or 0
        if resource.md5 is None and resource.sha256 is None:
            self.without_checksum.add(resource.path)


@dataclass(frozen=True)
class ScanResult:
    """A committed scan: its root as the API spells it, the totals of the recorded rows, the topmost folders that
    vanished during the walk (sorted), and the changes against the previous scan."""

    root: str
    totals: ScanTotals
    vanished: tuple[str, ...]
    diff: ScanDiff


async def resolve_scan_root(client: api.DiskClient, root: str) -> ScanRoot:
    """Look up `root` (a normalized `disk:/` path) before anything is written.

    Raises `RemotePathNotFoundError` for a missing folder and `NotAFolderError` for a file.
    """
    if root == api.ROOT:
        return ScanRoot(path=root, resource=None)
    resource = await client.resource(root)
    if resource.kind == 'file':
        raise api.NotAFolderError(resource.path)
    return ScanRoot(path=resource.path, resource=resource)


async def scan_tree(client: api.DiskClient, manifest: Manifest, account: api.Account, root: ScanRoot) -> ScanResult:
    """Scan `root` into the manifest; the scan is committed only when every folder was listed or had vanished.

    A folder root is itself a manifest row, as the API names it (so a scan of `disk:/A` keeps the `disk:/A` row a
    scan of `disk:/` wrote); the root `disk:/` has no row. Neither is counted in the totals, which cover exactly the
    rows the commit records. Folders are listed with `client.limits.concurrency` concurrent listings; the first
    failed listing stops the walk, is raised, and leaves the manifest unchanged.
    """
    session = manifest.begin_scan(account.uid, root.path)
    if root.resource is not None:
        session.add([root.resource])
    walk = _Walk(client, session, root.path, workers=client.limits.concurrency)
    await walk.run()
    totals = ScanTotals()
    for resource in session.staged():
        if resource.path != root.path:
            totals.count(resource)
    diff = session.commit()
    return ScanResult(root=root.path, totals=totals, vanished=walk.vanished, diff=diff)


def scan_summary(result: ScanResult) -> list[str]:
    """The lines reporting a scan: totals, files without a checksum, vanished folders, and the changes."""
    totals, diff = result.totals, result.diff
    lines = [
        f'Scanned {result.root}: {_plural(totals.files, "file")}, {_plural(totals.folders, "folder")}, '
        f'{format_size(totals.size)}',
        f'Files without a cloud checksum: {totals.without_checksum.count}',
        *_listed('', totals.without_checksum.smallest, totals.without_checksum.count, ''),
    ]
    if result.vanished:
        lines.append(f'Folders that disappeared during the scan: {len(result.vanished)}')
        lines += _listed('', result.vanished, len(result.vanished), '')
    if diff.first_scan:
        lines.append(f'First scan of {result.root}')
    else:
        lines.append(
            f'Changes since the last scan: {len(diff.added)} added, {len(diff.changed)} changed, '
            f'{len(diff.removed)} removed'
        )
        lines += _listed('+ ', diff.added, len(diff.added), ' added')
        lines += _listed('~ ', diff.changed, len(diff.changed), ' changed')
        lines += _listed('- ', diff.removed, len(diff.removed), ' removed')
    return lines


class _Walk:
    """Lists every folder below a root with `workers` concurrent listings, staging rows into a scan session.

    A folder below the root that answers 404 or as a file, on any page, has been deleted or replaced by a file since
    its parent was listed: it and everything under it are left out of the session, including rows of its sub-folders
    that other workers listed before or list after it vanished.
    """

    def __init__(self, client: api.DiskClient, session: ScanSession, root: str, *, workers: int) -> None:
        self._client = client
        self._session = session
        self._root = root
        self._workers = workers
        self._batch: list[api.Resource] = []
        self._vanished: set[str] = set()

    @property
    def vanished(self) -> tuple[str, ...]:
        """The topmost folders that vanished during the walk, sorted."""
        return tuple(sorted(self._vanished))

    async def run(self) -> None:
        """List the root and every folder below it; the first failed listing stops the walk and is raised."""
        folders: asyncio.Queue[str] = asyncio.Queue()
        folders.put_nowait(self._root)
        try:
            async with asyncio.TaskGroup() as group:
                for _ in range(self._workers):
                    group.create_task(self._work(folders))
                await folders.join()
                folders.shutdown()
        except ExceptionGroup as failures:
            raise failures.exceptions[0] from None
        self._stage()

    async def _work(self, folders: asyncio.Queue[str]) -> None:
        while True:
            try:
                folder = await folders.get()
            except asyncio.QueueShutDown:
                return
            try:
                if not self._has_vanished(folder):
                    await self._list(folder, folders)
            finally:
                folders.task_done()

    async def _list(self, folder: str, folders: asyncio.Queue[str]) -> None:
        try:
            async for resource in self._client.list_folder(folder):
                if self._has_vanished(folder):
                    return
                if resource.path in self._vanished:
                    continue
                self._batch.append(resource)
                if resource.kind == 'dir':
                    folders.put_nowait(resource.path)
                if len(self._batch) >= _STAGE_BATCH:
                    self._stage()
        except api.RemotePathNotFoundError, api.NotAFolderError:
            if folder == self._root:
                raise
            self._vanish(folder)

    def _has_vanished(self, path: str) -> bool:
        """Whether `path` or one of its ancestors vanished."""
        below_root = self._root if self._root.endswith('/') else self._root + '/'
        while self._vanished and path.startswith(below_root):
            if path in self._vanished:
                return True
            path = path.rpartition('/')[0]
        return False

    def _vanish(self, folder: str) -> None:
        if self._has_vanished(folder):
            return
        self._vanished = {path for path in self._vanished if not _within(path, folder)} | {folder}
        self._batch = [resource for resource in self._batch if not _within(resource.path, folder)]
        self._session.discard(folder)

    def _stage(self) -> None:
        self._session.add(self._batch)
        self._batch = []


def _within(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder + '/')


def _listed(marker: str, shown: Sequence[str], count: int, what: str) -> list[str]:
    lines = [f'  {marker}{path}' for path in shown[:SHOWN_PATHS]]
    if count > SHOWN_PATHS:
        lines.append(f'  … and {count - SHOWN_PATHS} more{what}')
    return lines


def _plural(count: int, noun: str) -> str:
    return f'{count} {noun}' if count == 1 else f'{count} {noun}s'
