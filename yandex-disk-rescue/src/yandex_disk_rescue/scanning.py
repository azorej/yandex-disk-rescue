"""The scan engine: walk a remote folder through the Disk API into the manifest, and the lines summarizing it."""

import asyncio
import bisect
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from yandex_disk_rescue import api
from yandex_disk_rescue.manifest import Manifest, ScanDiff, ScanSession
from yandex_disk_rescue.progress import ScanProgress
from yandex_disk_rescue.units import format_size

SHOWN_PATHS = 20
# Rows are staged in batches: each `ScanSession.add` is one transaction with an fsync, run on the event loop.
_STAGE_BATCH = 1000
_log = logging.getLogger(__name__)


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
    vanished during the walk (sorted), the entries left out because their names are not valid Unicode (sorted by
    path), and the changes against the previous scan."""

    root: str
    totals: ScanTotals
    vanished: tuple[str, ...]
    unencodable: tuple[api.Resource, ...]
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


async def scan_tree(
    client: api.DiskClient, manifest: Manifest, account: api.Account, root: ScanRoot, progress: ScanProgress
) -> ScanResult:
    """Scan `root` into the manifest, reporting the walk to `progress`; the scan is committed only when every folder
    was listed or had vanished.

    A folder root is itself a manifest row, as the API names it (so a scan of `disk:/A` keeps the `disk:/A` row a
    scan of `disk:/` wrote); the root `disk:/` has no row. Neither is counted in the totals, which cover exactly the
    rows the commit records. An entry whose path is not valid Unicode (the API can send a lone UTF-16 surrogate in a
    JSON escape) cannot be stored as an entry or requested again: it is left out of the session, a folder is not
    listed, and the result and the commit name it. Folders are listed with `client.limits.concurrency` concurrent listings; the first
    failed listing stops the walk, is raised, and leaves the manifest unchanged.
    """
    _log.info('Scanning %s', display_path(root.path))
    session = manifest.begin_scan(account.uid, root.path)
    if root.resource is not None:
        session.add([root.resource])
    progress.start(display_path(root.path))
    walk = _Walk(client, session, root.path, progress, workers=client.limits.concurrency)
    await walk.run()
    totals = ScanTotals()
    for resource in session.staged():
        if resource.path != root.path:
            totals.count(resource)
    diff = session.commit(unencodable=walk.unencodable)
    changes = (
        'first scan'
        if diff.first_scan
        else f'{len(diff.added)} added, {len(diff.changed)} changed, {len(diff.removed)} removed'
    )
    _log.info(
        'Scan of %s committed: %s, %s, %s; %s',
        display_path(root.path),
        _plural(totals.files, 'file'),
        _plural(totals.folders, 'folder'),
        format_size(totals.size),
        changes,
    )
    return ScanResult(root=root.path, totals=totals, vanished=walk.vanished, unencodable=walk.unencodable, diff=diff)


def scan_summary(result: ScanResult, *, limit: int | None = SHOWN_PATHS) -> list[str]:
    """The lines reporting a scan: totals, files without a checksum, vanished folders, entries left out for their
    names, and the changes; each list shows its first `limit` members (`None`: all)."""
    totals, diff = result.totals, result.diff
    lines = [
        f'Scanned {result.root}: {_plural(totals.files, "file")}, {_plural(totals.folders, "folder")}, '
        f'{format_size(totals.size)}',
        f'Files without a cloud checksum: {totals.without_checksum.count}',
        *listed('', totals.without_checksum.smallest, totals.without_checksum.count, '', limit=limit),
    ]
    if result.vanished:
        lines.append(f'Folders that disappeared during the scan: {len(result.vanished)}')
        lines += listed('', result.vanished, len(result.vanished), '', limit=limit)
    if result.unencodable:
        lines.append(f'Not recorded, the name is not valid Unicode: {len(result.unencodable)}')
        shown = [display_path(resource.path) for resource in result.unencodable]
        lines += listed('', shown, len(shown), '', limit=limit)
    if diff.first_scan:
        lines.append(f'First scan of {result.root}')
    else:
        lines.append(
            f'Changes since the last scan: {len(diff.added)} added, {len(diff.changed)} changed, '
            f'{len(diff.removed)} removed'
        )
        lines += listed('+ ', diff.added, len(diff.added), ' added', limit=limit)
        lines += listed('~ ', diff.changed, len(diff.changed), ' changed', limit=limit)
        lines += listed('- ', diff.removed, len(diff.removed), ' removed', limit=limit)
    return lines


def display_path(path: str) -> str:
    """`path` safe to print: a lone surrogate, which no output encoding takes, is shown as `\\udcxx`."""
    return path.encode('utf-8', 'backslashreplace').decode('utf-8')


def listed(marker: str, shown: Sequence[str], count: int, what: str, *, limit: int | None) -> list[str]:
    """Indented lines for the first `limit` of `shown` (all of them for `None`), then `… and N more<what>` when
    `count` exceeds the lines shown."""
    lines = [f'  {marker}{path}' for path in (shown if limit is None else shown[:limit])]
    if count > len(lines):
        lines.append(f'  … and {count - len(lines)} more{what}')
    return lines


class _Walk:
    """Lists every folder below a root with `workers` concurrent listings, staging rows into a scan session.

    A folder below the root that answers 404 or as a file, on any page, has been deleted or replaced by a file since
    its parent was listed: it and everything under it are left out of the session, including rows of its sub-folders
    that other workers listed before or list after it vanished.
    """

    def __init__(
        self, client: api.DiskClient, session: ScanSession, root: str, progress: ScanProgress, *, workers: int
    ) -> None:
        self._client = client
        self._session = session
        self._root = root
        self._progress = progress
        self._workers = workers
        self._batch: list[api.Resource] = []
        self._vanished: set[str] = set()
        self._unencodable: dict[str, api.Resource] = {}

    @property
    def unencodable(self) -> tuple[api.Resource, ...]:
        """The entries left out because their paths are not valid Unicode, sorted by path."""
        return tuple(self._unencodable[path] for path in sorted(self._unencodable))

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
                self._progress.folder_done()

    async def _list(self, folder: str, folders: asyncio.Queue[str]) -> None:
        try:
            async for resource in self._client.list_folder(folder):
                if self._has_vanished(folder):
                    return
                if resource.path in self._vanished:
                    continue
                if not _is_unicode(resource.path):
                    _log.warning('%s left out: the name is not valid Unicode', display_path(resource.path))
                    self._unencodable[resource.path] = resource
                    continue
                self._batch.append(resource)
                if resource.kind == 'dir':
                    self._progress.listed_folder()
                    folders.put_nowait(resource.path)
                else:
                    self._progress.listed_file(resource.size or 0)
                if len(self._batch) >= _STAGE_BATCH:
                    self._stage()
            _log.debug('Listed %s', display_path(folder))
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
        _log.warning('%s disappeared during the scan; it is recorded as removed', display_path(folder))
        self._vanished = {path for path in self._vanished if not _within(path, folder)} | {folder}
        self._batch = [resource for resource in self._batch if not _within(resource.path, folder)]
        self._unencodable = {path: r for path, r in self._unencodable.items() if not _within(path, folder)}
        self._session.discard(folder)

    def _stage(self) -> None:
        self._session.add(self._batch)
        self._batch = []


def _is_unicode(path: str) -> bool:
    try:
        path.encode('utf-8')
    except UnicodeEncodeError:
        return False
    return True


def _within(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder + '/')


def _plural(count: int, noun: str) -> str:
    return f'{count} {noun}' if count == 1 else f'{count} {noun}s'
