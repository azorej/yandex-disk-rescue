"""The mirror engine: local names, the walk over a mirror's entries, the per-file download pipeline, and the lines
summarizing a run.

A file is verified before it reaches its final path: the bytes go to a partial file while their MD5 and SHA-256
are computed, the size and every checksum the cloud provides are checked, and only then is the file moved into
place without replacing anything. A verified file whose record and `lstat` fingerprint still match is skipped
without being read. Any other file already at a final path is hashed: one matching the cloud is adopted as it is,
and anything else there is moved aside once the new version is verified. Paths with no cloud counterpart are never
looked at.

Every download keeps a checkpoint journal in the manifest: the offset up to which its partial file was fsynced and
the SHA-256 of those bytes. A checkpoint is taken every `checkpoint_bytes` received, `CHECKPOINT_SECONDS` after the
last one while bytes arrive, and whenever a transfer attempt ends with new bytes, cancellation included. A later
turn of the file (a later pass or a later run) resumes from the confirmed offset with an HTTP range once the cloud
file is confirmed unchanged and the confirmed bytes on disk hash to the recorded digest; bytes past it are dropped.
A transfer that fails after receiving new data is resumed at once, within a retry budget, from the bytes this turn
wrote; one that fails transiently otherwise waits for the next pass over the files still to download.

A folder or file whose name the destination filesystem refuses is created once more under its rejected-name
conversion, which is then recorded as its local name.

Files are in progress concurrently, as many as the content client has connections. Everything that looks at a
final path and changes or records it runs under one placement lock, so two files never act on the same path or
inode at once.

Disk work that scales with a file's size or waits for the disk (hashing, reading back, writing with the digest
updates, fsync, moving aside and placing, creating folders, setting an mtime) runs in the run's worker threads, one
bounded call at a time: at most one chunk, one fsync, one rename. The manifest and the local names stay on the
event loop thread.
"""

import asyncio
import hashlib
import os
import stat
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Self

from yandex_disk_rescue import api
from yandex_disk_rescue.destination import FATAL_ERRNOS, NAME_ERRNOS, Destination, DestinationError, PartialFile
from yandex_disk_rescue.disk_threads import DiskThreads
from yandex_disk_rescue.local_names import WORK_DIR_NAME, collision_key, map_local_names, rejected_name_conversion
from yandex_disk_rescue.manifest import (
    LocalFingerprint,
    Mirror,
    MirrorEntry,
    RemoteIdentity,
    Segment,
    UnencodableEntry,
    VerifiedBy,
)
from yandex_disk_rescue.scanning import display_path, listed
from yandex_disk_rescue.units import format_size

PASS_PAUSE_SECONDS = 5.0
CHECKPOINT_SECONDS = 15.0
# A write happens only while its fetch holds one of the content client's connections, so each connection needs one
# thread; the spare ones hash, fsync and place.
_SPARE_THREADS = 4
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_CHANGED = 'changed in the cloud since the scan; rerun with --scan-max-age 0 to fetch the new version'


class FileFailed(Exception):
    """One file (or folder) could not be mirrored; `reason` is the user-facing text. The run goes on."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class FileDeferred(FileFailed):
    """The file's transfer failed transiently (a stall, a connection error, 408, 429 or 5xx, the retries spent); its
    partial file and record are kept so a later pass resumes it. `retry_after` is the wait the download host asked
    for, if any."""

    def __init__(self, reason: str, *, retry_after: float | None) -> None:
        super().__init__(reason)
        self.retry_after = retry_after


@dataclass(frozen=True)
class Failure:
    path: str
    reason: str


@dataclass
class MirrorReport:
    """What a run did. Lists hold every member in path order; the summary shows the first few."""

    remote_root: str
    destination: str
    downloaded: int = 0
    downloaded_size: int = 0
    skipped: int = 0
    adopted: list[str] = field(default_factory=list)
    size_only: list[str] = field(default_factory=list)
    renamed: list[tuple[str, PurePosixPath]] = field(default_factory=list)
    moved_aside: list[PurePosixPath] = field(default_factory=list)
    replaced_folder: PurePosixPath | None = None
    failed: list[Failure] = field(default_factory=list)
    passes: tuple[int, int] | None = None  # (used, limit; 0 is no limit) once a transient failure deferred a file


@dataclass(frozen=True)
class FileOutcome:
    """How a file came to be verified at its final path `local`: `adopted` when the copy already there matched the
    cloud, `moved_aside` when something else stood there and was moved to the replaced folder."""

    local: PurePosixPath
    verified_by: VerifiedBy
    adopted: bool
    moved_aside: bool


class LocalNames:
    """The local path of each entry of a mirror, kept current as names the destination refuses are converted.

    Siblings are indexed by their parent entry (`None` for the destination root), so a conversion and a rename cost
    the size of one folder and of the renamed subtree, not of the whole mirror.
    """

    def __init__(self, mirror: Mirror, entries: Iterable[MirrorEntry]) -> None:
        self._mirror = mirror
        self._paths = {entry.entry_id: _local_path(entry) for entry in entries}
        ids = {path: entry_id for entry_id, path in self._paths.items()}
        self._parents = {entry_id: ids.get(path.parent) for entry_id, path in self._paths.items()}
        self._children: defaultdict[int | None, list[int]] = defaultdict(list)
        self._keys: defaultdict[int | None, set[str]] = defaultdict(set)
        self._keys[None].add(collision_key(WORK_DIR_NAME))
        for entry_id, parent in self._parents.items():
            self._children[parent].append(entry_id)
            self._keys[parent].add(collision_key(self._paths[entry_id].name))

    def __getitem__(self, entry_id: int) -> PurePosixPath:
        return self._paths[entry_id]

    def conversion(self, entry_id: int) -> PurePosixPath:
        """The entry's local path with its last component converted by `rejected_name_conversion`, unique among its
        siblings."""
        local = self._paths[entry_id]
        taken = self._keys[self._parents[entry_id]]
        own = collision_key(local.name)
        taken.discard(own)
        try:
            return local.with_name(rejected_name_conversion(local.name, taken))
        finally:
            taken.add(own)

    def rename(self, entry_id: int, local: PurePosixPath) -> None:
        """Record `local`, a sibling of the entry's local path, as its local path, moving everything below along."""
        old = self._paths[entry_id]
        self._mirror.rename_local(entry_id, local.name)
        taken = self._keys[self._parents[entry_id]]
        taken.discard(collision_key(old.name))
        taken.add(collision_key(local.name))
        below = [entry_id]
        while below:
            moved = below.pop()
            self._paths[moved] = local / self._paths[moved].relative_to(old)
            below += self._children[moved]


class Digest:
    """The size, MD5 and SHA-256 of bytes written from offset 0 in order."""

    def __init__(self) -> None:
        self.size = 0
        self._md5 = hashlib.md5(usedforsecurity=False)
        self._sha256 = hashlib.sha256()

    def update(self, offset: int, chunk: bytes) -> None:
        if offset != self.size:
            raise ValueError(f'a chunk at offset {offset} does not follow the {self.size} bytes hashed so far')
        self._md5.update(chunk)
        self._sha256.update(chunk)
        self.size += len(chunk)

    def sha256(self) -> str:
        """The SHA-256 hex of the bytes hashed so far, from a copy of the running hash taken now."""
        return self._sha256.copy().hexdigest()

    def mismatch(self, identity: RemoteIdentity) -> str | None:
        """How the bytes differ from `identity`: the size, then the MD5 and the SHA-256 the cloud provides."""
        if self.size != identity.size:
            return f'size mismatch (expected {identity.size} bytes, got {self.size})'
        for name, expected, actual in (
            ('MD5', identity.md5, self._md5.hexdigest()),
            ('SHA-256', identity.sha256, self._sha256.hexdigest()),
        ):
            if expected is not None and expected.lower() != actual:
                return f'{name} mismatch (expected {expected}, got {actual})'
        return None

    def verify(self, identity: RemoteIdentity) -> VerifiedBy:
        """The verification method of `identity`; raises `FileFailed` when the bytes do not match it."""
        if (reason := self.mismatch(identity)) is not None:
            raise FileFailed(reason)
        return _method(identity)


@dataclass(frozen=True)
class _RunTools:
    """What every turn of a run shares: the clients, the worker threads, the placement lock, the report, the
    immediate retries of a file per pass and the bytes between two checkpoints."""

    client: api.DiskClient
    content: api.ContentClient
    threads: DiskThreads
    placement: asyncio.Lock
    report: MirrorReport
    retries: int
    checkpoint_bytes: int


class _FileTurns:
    """The turns of files running as tasks, at most `limit` at once, used as an async context manager around the walk
    that starts them; leaving it waits for every turn to end.

    The first exception a turn raises stops the run: the other turns and the walk are cancelled, and leaving the
    context raises that exception itself, with what the cancelled turns raised meanwhile added as notes. An exception
    the walk raises stops the turns the same way. A cancellation of the walk from outside (Ctrl+C) wins over both.
    """

    def __init__(self, limit: int) -> None:
        self._slots = asyncio.Semaphore(limit)
        self._running: set[asyncio.Task[None]] = set()
        self._error: BaseException | None = None
        self._walk: asyncio.Task[object] | None = None
        self._walk_cancelled = False
        self._leaving = False

    async def __aenter__(self) -> Self:
        self._walk = asyncio.current_task()
        return self

    async def start(self, turn: Coroutine[object, object, None]) -> None:
        """Wait for a free slot, then run `turn` as a task."""
        try:
            await self._slots.acquire()
        except BaseException:
            turn.close()
            raise
        task = asyncio.create_task(turn)
        self._running.add(task)
        task.add_done_callback(self._ended)

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self._leaving = True
        if exc is not None and not isinstance(exc, asyncio.CancelledError):
            self._stop(exc)
        elif exc is not None and not self._walk_cancelled:
            self._cancel_running()
        while self._running:
            try:
                await asyncio.wait(set(self._running))
            except asyncio.CancelledError:
                self._cancel_running()
        walk = self._walk
        if walk is None:
            raise RuntimeError('the file turns were left without being entered')
        if self._walk_cancelled:
            walk.uncancel()
        if walk.cancelling():
            if isinstance(exc, asyncio.CancelledError):
                return
            raise asyncio.CancelledError
        if self._error is not None and self._error is not exc:
            raise self._error from None

    def _ended(self, task: asyncio.Task[None]) -> None:
        self._running.discard(task)
        self._slots.release()
        if not task.cancelled() and (exc := task.exception()) is not None:
            self._stop(exc)

    def _stop(self, exc: BaseException) -> None:
        if self._error is not None:
            if exc is not self._error:
                self._error.add_note(f'Also raised while the run was stopping: {type(exc).__name__}: {exc}')
            return
        self._error = exc
        self._cancel_running()
        if not self._leaving and self._walk is not None:
            self._walk.cancel()
            self._walk_cancelled = True

    def _cancel_running(self) -> None:
        for task in self._running:
            task.cancel()


class MirrorRun:
    """One run over a mirror: names are assigned, stray partial files deleted and the files still to download
    counted on construction; `run` then walks the entries in path order.

    Each entry the manifest records as left out of its scan, because its name is not valid Unicode, is a failure of
    the run.
    """

    def __init__(self, mirror: Mirror, destination: Destination) -> None:
        self._mirror = mirror
        self._destination = destination
        self._entries = _assign_local_names(mirror)
        self._positions = {entry.entry_id: position for position, entry in enumerate(self._entries)}
        self._names = LocalNames(mirror, self._entries)
        with _fatal_errors(destination.path):
            destination.keep_partials(
                [e.entry_id for e in self._entries if e.download is not None and e.download.state == 'partial']
            )
            self._current = {e.entry_id for e in self._entries if _is_current(e, destination)}
        pending = [e for e in self._entries if e.resource.kind == 'file' and e.entry_id not in self._current]
        self.pending_files = len(pending)
        self.pending_size = sum(e.resource.size or 0 for e in pending)

    async def run(
        self, client: api.DiskClient, content: api.ContentClient, *, retries: int, passes: int, checkpoint_bytes: int
    ) -> MirrorReport:
        """Walk the entries in path order, then resume the files deferred by a transient failure in later passes, at
        most `passes` in all (0: until none is left), each after a pause of `PASS_PAUSE_SECONDS` or the longest wait
        the download host asked for. `retries` bounds the immediate retries of each file in each pass, and a
        download takes a checkpoint after every `checkpoint_bytes` received.

        In every pass up to `content.connections` files are in progress at once, started in path order as slots free
        up; a folder is made before any entry below it starts. Each final path is checked, cleared, placed and
        recorded under one placement lock, so files in progress never race for a path or an inode. The report lists
        everything in path order, whatever the order the files ended in.

        The first error that stops the run (`DestinationError`, `TokenRejectedError`, `DiskApiError`) cancels the
        files in progress and is raised as itself once they have ended.

        The disk work goes to worker threads of this run's own, shut down when the run ends, also on cancellation,
        once every file's turn has ended."""
        report = MirrorReport(remote_root=self._mirror.remote_root, destination=str(self._destination.path))
        with DiskThreads(content.connections + _SPARE_THREADS) as threads:
            tools = _RunTools(client, content, threads, asyncio.Lock(), report, retries, checkpoint_bytes)
            deferred = await self._first_pass(tools)
            if deferred:
                await self._later_passes(deferred, tools, passes=passes)
        report.replaced_folder = self._destination.replaced_folder
        report.failed += [Failure(display_path(e.path), _unencodable_reason(e)) for e in self._mirror.unencodable()]
        self._in_path_order(report)
        return report

    async def _first_pass(self, tools: _RunTools) -> list[tuple[MirrorEntry, FileDeferred]]:
        report = tools.report
        blocked: dict[PurePosixPath, str] = {}
        deferred: list[tuple[MirrorEntry, FileDeferred]] = []
        async with _FileTurns(tools.content.connections) as turns:
            for listed_entry in self._entries:
                local = self._names[listed_entry.entry_id]
                entry = replace(listed_entry, local_path=local)
                if local.name != entry.resource.path.rpartition('/')[2]:
                    report.renamed.append((entry.resource.path, local))
                if reason := _blocked_by(local, blocked):
                    report.failed.append(Failure(entry.resource.path, reason))
                elif entry.resource.kind == 'dir':
                    await self._make_folder(entry, local, blocked, tools)
                elif entry.entry_id in self._current:
                    report.skipped += 1
                    _note_verified(report, entry, _recorded_method(entry))
                else:
                    await turns.start(self._turn(entry, tools, deferred))
        return self._sorted(deferred)

    async def _make_folder(
        self, entry: MirrorEntry, local: PurePosixPath, blocked: dict[PurePosixPath, str], tools: _RunTools
    ) -> None:
        destination, report, threads = self._destination, tools.report, tools.threads

        def make_folder(path: PurePosixPath) -> Awaitable[bool]:
            return threads.run(destination.make_folder, path)

        try:
            async with tools.placement:
                with _local_errors(destination, local, 'write'):
                    made, local = await _create(entry, local, make_folder, self._names, destination)
                    if local != entry.local_path:
                        report.renamed.append((entry.resource.path, local))
                    if made:
                        return
                    await threads.run(destination.move_aside, local)
                    report.moved_aside.append(local)
                    if await make_folder(local):
                        return
            reason = f'{local} was created again as something other than a folder while it was being moved aside'
        except FileFailed as failed:
            reason = failed.reason
        blocked[local] = reason
        report.failed.append(Failure(entry.resource.path, reason))

    async def _later_passes(
        self, deferred: list[tuple[MirrorEntry, FileDeferred]], tools: _RunTools, *, passes: int
    ) -> None:
        used = 1
        while deferred and (passes == 0 or used < passes):
            waits = [failure.retry_after for _, failure in deferred if failure.retry_after is not None]
            await asyncio.sleep(max([PASS_PAUSE_SECONDS, *waits]))
            used += 1
            remaining: list[tuple[MirrorEntry, FileDeferred]] = []
            async with _FileTurns(tools.content.connections) as turns:
                for entry, _ in deferred:
                    await turns.start(self._turn(entry, tools, remaining))
            deferred = self._sorted(remaining)
        suffix = f' (after {used} passes)' if used > 1 else ''
        tools.report.failed += [Failure(entry.resource.path, failure.reason + suffix) for entry, failure in deferred]
        tools.report.passes = (used, passes)

    async def _turn(
        self, entry: MirrorEntry, tools: _RunTools, deferred: list[tuple[MirrorEntry, FileDeferred]]
    ) -> None:
        """One file's turn in a pass; a deferred file is added to `deferred`."""
        if (later := await self._download(entry, tools)) is not None:
            deferred.append((entry, later))

    def _sorted(self, deferred: list[tuple[MirrorEntry, FileDeferred]]) -> list[tuple[MirrorEntry, FileDeferred]]:
        return sorted(deferred, key=lambda item: self._positions[item[0].entry_id])

    def _in_path_order(self, report: MirrorReport) -> None:
        by_path = {entry.resource.path: position for position, entry in enumerate(self._entries)}
        by_local = {self._names[entry.entry_id]: position for position, entry in enumerate(self._entries)}
        report.adopted.sort(key=by_path.__getitem__)
        report.size_only.sort(key=by_path.__getitem__)
        report.moved_aside.sort(key=by_local.__getitem__)
        report.failed.sort(key=lambda failure: failure.path)
        report.renamed.sort()

    async def _download(self, entry: MirrorEntry, tools: _RunTools) -> FileDeferred | None:
        """Mirror one file into the report; the failure when it is deferred to a later pass instead."""
        report = tools.report
        try:
            outcome = await download_file(
                entry,
                client=tools.client,
                content=tools.content,
                threads=tools.threads,
                placement=tools.placement,
                mirror=self._mirror,
                destination=self._destination,
                names=self._names,
                retries=tools.retries,
                checkpoint_bytes=tools.checkpoint_bytes,
            )
        except FileDeferred as deferred:
            return deferred
        except FileFailed as failed:
            report.failed.append(Failure(entry.resource.path, failed.reason))
            return None
        if outcome.adopted:
            report.skipped += 1
            report.adopted.append(entry.resource.path)
        else:
            report.downloaded += 1
            report.downloaded_size += entry.resource.size or 0
        if outcome.local != entry.local_path:
            report.renamed.append((entry.resource.path, outcome.local))
        if outcome.moved_aside:
            report.moved_aside.append(outcome.local)
        _note_verified(report, entry, outcome.verified_by)


async def download_file(
    entry: MirrorEntry,
    *,
    client: api.DiskClient,
    content: api.ContentClient,
    threads: DiskThreads,
    placement: asyncio.Lock,
    mirror: Mirror,
    destination: Destination,
    names: LocalNames,
    retries: int,
    checkpoint_bytes: int,
) -> FileOutcome:
    """Make the file `entry` verified at its local path and record it; the only state it touches is the mirror's
    records, the destination and `names`. Its disk work runs in `threads`.

    The steps that decide the final path, from re-checking what is there to recording the file adopted or placed,
    hold `placement` in one acquisition, so no other file of the run sees the path or the inode in between. The
    first look at the path, hashing a local copy and fetching run outside it.

    A file already at the local path is adopted when its content matches the cloud. Otherwise the file is
    downloaded and verified first, and only then is whatever stands at the local path moved aside and the new file
    placed. A path that is the same file as another entry's verified copy is never adopted, moved or replaced. A name
    the destination refuses is converted, and the file placed under the converted name.

    The entry's download record is read from `mirror` when the turn starts. A partial file whose `partial` record
    is for the entry's identity is resumed from its journal's confirmed offset after `client.resource` confirms the
    cloud file still has it and the confirmed bytes hash to the recorded digest; any other partial file is started
    again from byte 0. A checkpoint is taken after every `checkpoint_bytes` received, and as described in the
    module. A transfer that fails after new data is resumed at once, up to `retries` times.

    Raises `FileDeferred` when the transfer failed transiently and a later pass may finish it, `FileFailed` when
    this file cannot be mirrored, `DestinationError` when the destination cannot be written at all, and the API
    errors that stop the run (`TokenRejectedError`, `DiskApiError`).
    """
    local = _local_path(entry)
    identity = RemoteIdentity.of(entry.resource)
    with _local_errors(destination, local, 'write'):
        with _local_errors(destination, local, 'read'):
            existing = destination.lstat(local)
        if existing is not None:
            if reason := _alias(entry, local, existing, mirror, destination):
                raise FileFailed(reason)
            verified_by = await _adopt(entry, local, identity, existing, mirror, destination, threads, placement)
            if verified_by is not None:
                return FileOutcome(local, verified_by, adopted=True, moved_aside=False)
        verified_by = await _fetch_verified(
            entry,
            identity,
            client=client,
            content=content,
            threads=threads,
            mirror=mirror,
            destination=destination,
            retries=retries,
            checkpoint_bytes=checkpoint_bytes,
        )

        async def place(path: PurePosixPath) -> tuple[os.stat_result, bool]:
            moved_aside = await _clear_the_way(entry, path, mirror, destination, threads)
            try:
                return await threads.run(destination.place, entry.entry_id, path), moved_aside
            except FileExistsError:
                _discard(entry, mirror, destination)
                raise FileFailed(f'something else was created at {path} while the file was being placed') from None

        async with placement:
            (placed, moved_aside), local = await _create(entry, local, place, names, destination)
            mirror.record_verified(entry.entry_id, identity, verified_by, placed)
    return FileOutcome(local, verified_by, adopted=False, moved_aside=moved_aside)


async def _create[T](
    entry: MirrorEntry,
    local: PurePosixPath,
    create: Callable[[PurePosixPath], Awaitable[T]],
    names: LocalNames,
    destination: Destination,
) -> tuple[T, PurePosixPath]:
    """Await `create(local)`, the call creating the entry's last component; when the destination refuses that name,
    record the converted name, then await it once more with that name and return it with the result.

    The name is recorded first, so a run killed once the entry is created under it finds it there. Raises
    `FileFailed`, with the original name recorded again, when the converted name is refused too.
    """
    try:
        return await create(local), local
    except OSError as exc:
        if exc.errno not in NAME_ERRNOS:
            raise
        refused = exc.strerror
    converted = names.conversion(entry.entry_id)
    names.rename(entry.entry_id, converted)
    with _local_errors(destination, converted, 'write'):
        try:
            return await create(converted), converted
        except OSError as exc:
            if exc.errno not in NAME_ERRNOS:
                raise
            names.rename(entry.entry_id, local)
            raise FileFailed(
                f'the destination filesystem refuses the name {local.name} ({refused}), and its converted form '
                f'{converted.name} too'
            ) from None


async def _adopt(
    entry: MirrorEntry,
    local: PurePosixPath,
    identity: RemoteIdentity,
    existing: os.stat_result,
    mirror: Mirror,
    destination: Destination,
    threads: DiskThreads,
    placement: asyncio.Lock,
) -> VerifiedBy | None:
    """Hash the regular file `existing` at `local` and, when it matches `identity`, give it the cloud mtime and
    record it as verified; `None` when it is not such a file or does not match.

    The hash runs outside `placement`. Under it, the file at `local` must still be the one hashed (same inode, size
    and mtime) and must not have become another entry's verified copy meanwhile; otherwise it is not adopted, or the
    entry fails as an alias. Only then is its mtime set and the record written.
    """
    if not stat.S_ISREG(existing.st_mode) or existing.st_size != identity.size:
        return None
    digest = Digest()
    with _local_errors(destination, local, 'read'), destination.existing_file(local) as file:
        opened = file.stat()
        if not stat.S_ISREG(opened.st_mode) or opened.st_ino != existing.st_ino:
            return None
        await _hash(digest, file.chunks(), threads)
        if digest.mismatch(identity) is not None:
            return None
        async with placement:
            now = destination.lstat(local)
            if now is None or LocalFingerprint.of(now) != LocalFingerprint.of(opened):
                return None
            if reason := _alias(entry, local, now, mirror, destination):
                raise FileFailed(reason)
            with _local_errors(destination, local, 'set the modification time of'):
                await threads.run(file.set_mtime, _mtime_ns(identity.modified))
            verified_by = _method(identity)
            mirror.record_verified(entry.entry_id, identity, verified_by, file.stat())
            with _local_errors(destination, local, 'write'):
                destination.remove_partial(entry.entry_id)
    return verified_by


async def _clear_the_way(
    entry: MirrorEntry, local: PurePosixPath, mirror: Mirror, destination: Destination, threads: DiskThreads
) -> bool:
    """Move whatever stands at `local` aside so the verified partial file can be placed; `True` when something was
    moved."""
    existing = destination.lstat(local)
    if existing is None:
        return False
    if reason := _alias(entry, local, existing, mirror, destination):
        _discard(entry, mirror, destination)
        raise FileFailed(reason)
    await threads.run(destination.move_aside, local)
    return True


async def _hash(digest: Digest, chunks: Iterator[bytes], threads: DiskThreads) -> None:
    """Add every chunk of `chunks` to `digest` in order, each read and hashed in a thread call of its own."""

    def step() -> bool:
        chunk = next(chunks, None)
        if chunk is None:
            return False
        digest.update(digest.size, chunk)
        return True

    while await threads.run(step):
        pass


async def _fetch_verified(
    entry: MirrorEntry,
    identity: RemoteIdentity,
    *,
    client: api.DiskClient,
    content: api.ContentClient,
    threads: DiskThreads,
    mirror: Mirror,
    destination: Destination,
    retries: int,
    checkpoint_bytes: int,
) -> VerifiedBy:
    """Fetch the file into its partial file while hashing it, keeping the confirmed bytes already there when they can
    be resumed, verify it, and give it the cloud mtime, durably.

    A partial file that fails verification is deleted and its record dropped; one cut short by a transfer error is
    kept with its journal. The partial file is closed only once no thread call on it is running, also when the task
    is cancelled.
    """
    with destination.partial(entry.entry_id) as partial:
        start, digest = await _resume_point(
            entry, identity, partial, client=client, mirror=mirror, destination=destination, threads=threads
        )
        await threads.run(partial.truncate, start)
        if start == 0 or start < identity.size:
            journal = _Journal(
                entry.entry_id,
                identity.size,
                start,
                mirror=mirror,
                partial=partial,
                threads=threads,
                interval=checkpoint_bytes,
            )
            digest = await _transfer(
                entry.resource.path,
                identity,
                partial,
                digest,
                journal,
                client=client,
                content=content,
                threads=threads,
                retries=retries,
            )
        try:
            verified_by = digest.verify(identity)
        except FileFailed:
            _discard(entry, mirror, destination)
            raise
        await threads.run(partial.finish, _mtime_ns(identity.modified))
    return verified_by


async def _resume_point(
    entry: MirrorEntry,
    identity: RemoteIdentity,
    partial: PartialFile,
    *,
    client: api.DiskClient,
    mirror: Mirror,
    destination: Destination,
    threads: DiskThreads,
) -> tuple[int, Digest]:
    """How many bytes of the partial file to keep, with their digest, after making the entry's record a `partial`
    one for `identity`.

    A `partial` record for `identity` in the manifest now is kept once the cloud file is confirmed to still have that
    identity; the bytes its journal confirms are kept when the partial file holds them and they hash to the
    recorded digest, and otherwise the journal is reset to byte 0. Any other record is replaced by a new `partial`
    one starting at byte 0.

    Raises `FileFailed`, with the partial file and its record dropped, when the cloud file changed or is gone. That
    check runs for a journal confirming nothing too: an earlier attempt that received nothing usable may have met
    the changed file.
    """
    record = mirror.download(entry.entry_id)
    if record is None or record.state != 'partial' or record.identity != identity:
        mirror.record_partial(entry.entry_id, identity, _whole_file(identity.size))
        return 0, Digest()
    try:
        live = await client.resource(entry.resource.path)
    except api.RemotePathNotFoundError:
        _discard(entry, mirror, destination)
        raise FileFailed('removed from the Disk after the scan') from None
    if live.kind != 'file' or RemoteIdentity.of(live) != identity:
        _discard(entry, mirror, destination)
        raise FileFailed(_CHANGED)
    if not record.segments:
        return 0, Digest()
    [segment] = record.segments
    if segment.confirmed == segment.start:
        return 0, Digest()
    digest = Digest()
    if partial.size() >= segment.confirmed:
        await _hash(digest, partial.chunks(segment.confirmed), threads)
        if digest.sha256() == segment.digest:
            return segment.confirmed, digest
    mirror.checkpoint(entry.entry_id, [replace(segment, confirmed=segment.start, digest=None)])
    return 0, Digest()


def _whole_file(size: int) -> tuple[Segment, ...]:
    """The journal of a download of a `size`-byte file starting from byte 0: one segment, none for an empty file."""
    return (Segment(0, size, 0, None),) if size else ()


class _Journal:
    """The checkpoint journal of one file's turn: its single segment `[0, size)` and the offset the manifest confirms.

    A checkpoint fsyncs the partial file in a worker thread, then records the bytes a digest covered when the fsync
    began, with their SHA-256. When the fsync or the commit fails, the journal stays at its previous checkpoint.
    """

    def __init__(
        self,
        entry_id: int,
        size: int,
        confirmed: int,
        *,
        mirror: Mirror,
        partial: PartialFile,
        threads: DiskThreads,
        interval: int,
    ) -> None:
        self._entry_id = entry_id
        self._size = size
        self._confirmed = confirmed
        self._mirror = mirror
        self._partial = partial
        self._threads = threads
        self._interval = interval
        self._taken = time.monotonic()

    async def written(self, digest: Digest) -> None:
        """Take a checkpoint once `interval` bytes arrived since the last one, or `CHECKPOINT_SECONDS` passed since
        it and new bytes arrived."""
        new = digest.size - self._confirmed
        if new >= self._interval or (new > 0 and time.monotonic() - self._taken >= CHECKPOINT_SECONDS):
            await self.take(digest)

    async def take(self, digest: Digest) -> None:
        """Make the bytes `digest` covers confirmed; nothing when they are the confirmed ones already.

        No write of the file may be in flight: the bytes `digest` covers must be the ones written.
        """
        confirmed, sha256 = digest.size, digest.sha256()
        if confirmed == self._confirmed:
            return
        await self._threads.run(self._partial.sync)
        self._mirror.checkpoint(self._entry_id, [Segment(0, self._size, confirmed, sha256 if confirmed else None)])
        self._confirmed = confirmed
        self._taken = time.monotonic()

    def reset(self) -> None:
        """Confirm nothing, before the partial file is cut back to byte 0."""
        self._mirror.checkpoint(self._entry_id, [Segment(0, self._size, 0, None)])
        self._confirmed = 0
        self._taken = time.monotonic()


async def _transfer(
    path: str,
    identity: RemoteIdentity,
    partial: PartialFile,
    digest: Digest,
    journal: _Journal,
    *,
    client: api.DiskClient,
    content: api.ContentClient,
    threads: DiskThreads,
    retries: int,
) -> Digest:
    """Fetch the rest of the file after the `digest.size` bytes already in the partial file, taking the checkpoints of
    `journal`, and return the digest of the whole file.

    A failure after new data is resumed at once from where it stopped, up to `retries` times. An expired link is
    refreshed once without spending a retry. A host ignoring the range restarts the file from byte 0. An attempt
    that ends with a transfer error or a cancellation takes a checkpoint first.

    Raises `FileDeferred` for a transient failure that received no new data or spent the retries, and `FileFailed`
    for any other failure.
    """

    # `digest` is looked up at call time, so the fresh digest of a restart from byte 0 takes over.
    async def write(offset: int, chunk: bytes) -> None:
        await threads.run(_write_hashed, digest, partial, offset, chunk)
        await journal.written(digest)

    async def attempt(start: int) -> None:
        try:
            await content.fetch(href, start=start, end=identity.size, size=identity.size, write=write)
        except api.TransferError, asyncio.CancelledError:
            await journal.take(digest)
            raise

    href = await _download_link(client, path)
    refreshed = False
    attempt_number = 1
    while True:
        before = digest.size
        try:
            await attempt(before)
        except api.LinkExpiredError as exc:
            if refreshed:
                raise FileFailed(f'download link rejected again after a refresh (HTTP {exc.status})') from None
            refreshed = True
            href = await _download_link(client, path)
        except api.RangeNotHonouredError as exc:
            if before == 0:
                raise FileFailed(exc.reason) from None
            journal.reset()
            await threads.run(partial.truncate, 0)
            digest = Digest()
        except api.TransientTransferError as exc:
            if digest.size == identity.size:
                return digest
            if digest.size == before or attempt_number > retries:
                reason = exc.reason if attempt_number == 1 else f'{exc.reason} (after {attempt_number} attempts)'
                raise FileDeferred(reason, retry_after=exc.retry_after) from None
            attempt_number += 1
        except api.TransferError as exc:
            raise FileFailed(exc.reason) from None
        else:
            return digest


def _write_hashed(digest: Digest, partial: PartialFile, offset: int, chunk: bytes) -> None:
    """Write `chunk` at `offset`, then add it to `digest`, so the digest never covers a byte the write did not
    return."""
    partial.write(offset, chunk)
    digest.update(offset, chunk)


def mirror_summary(report: MirrorReport) -> list[str]:
    """The lines reporting a run: the counts, the passes once a transient failure deferred a file, adopted and size-only
    files, renamed entries, what was moved aside, and failures with their reasons."""
    lines = [
        f'Mirrored {report.remote_root} to {report.destination}: {report.downloaded} downloaded '
        f'({format_size(report.downloaded_size)}), {report.skipped} skipped, {len(report.failed)} failed'
    ]
    if report.passes is not None:
        used, limit = report.passes
        lines.append(f'Passes: {used} of {limit or "unlimited"}')
    if report.adopted:
        lines.append(f'Already on disk and verified: {len(report.adopted)}')
        lines += listed('', report.adopted, len(report.adopted), '')
    if report.size_only:
        lines.append(f'Verified by size only (no cloud checksum): {len(report.size_only)}')
        lines += listed('', report.size_only, len(report.size_only), '')
    if report.renamed:
        lines.append(f'Renamed for the destination: {len(report.renamed)}')
        shown = [f'{remote} -> {local}' for remote, local in report.renamed]
        lines += listed('', shown, len(shown), '')
    if report.moved_aside:
        lines.append(f'Moved aside to {report.replaced_folder}/: {len(report.moved_aside)}')
        shown = [str(local) for local in report.moved_aside]
        lines += listed('', shown, len(shown), '')
    if report.failed:
        lines.append(f'Failed: {len(report.failed)}')
        shown = [f'{failure.path}: {failure.reason}' for failure in report.failed]
        lines += listed('', shown, len(shown), '')
    return lines


def _assign_local_names(mirror: Mirror) -> list[MirrorEntry]:
    entries = mirror.entries()
    assigned = {e.resource.path: e.local_path for e in entries if e.local_path is not None}
    mapped = map_local_names(mirror.remote_root, (e.resource for e in entries), assigned)
    changed = {e.entry_id: mapped[e.resource.path] for e in entries if mapped[e.resource.path] != e.local_path}
    if changed:
        mirror.assign_names(changed)
    return [replace(e, local_path=mapped[e.resource.path]) for e in entries]


def _is_current(entry: MirrorEntry, destination: Destination) -> bool:
    """Whether the entry is a file verified for its current identity whose placed copy has the recorded `lstat`.

    A placed copy that cannot be looked at is not current: the file's own turn meets the error and reports it.
    """
    record = entry.download
    if entry.resource.kind != 'file' or record is None or record.state != 'verified':
        return False
    if record.identity != RemoteIdentity.of(entry.resource):
        return False
    try:
        placed = destination.lstat(_local_path(entry))
    except OSError as exc:
        if exc.errno in FATAL_ERRNOS:
            raise
        return False
    return placed is not None and stat.S_ISREG(placed.st_mode) and LocalFingerprint.of(placed) == record.local


def _alias(
    entry: MirrorEntry, local: PurePosixPath, existing: os.stat_result, mirror: Mirror, destination: Destination
) -> str | None:
    """The failure reason when `existing` is the verified copy of another entry, still at that entry's own path (a
    hard link, or a name the filesystem folds onto another); `None` otherwise."""
    owner = mirror.verified_owner(existing.st_ino)
    if owner is None or owner.entry_id == entry.entry_id or owner.local_path is None:
        return None
    owner_copy = destination.lstat(owner.local_path)
    if owner_copy is None or owner_copy.st_ino != existing.st_ino:
        return None
    return f'{local} is the same file as {owner.resource.path} on this filesystem; it was left untouched'


async def _download_link(client: api.DiskClient, path: str) -> str:
    try:
        return await client.download_link(path)
    except api.RemotePathNotFoundError:
        raise FileFailed('removed from the Disk after the scan') from None
    except api.DownloadRefusedError as exc:
        raise FileFailed(exc.reason) from None


def _discard(entry: MirrorEntry, mirror: Mirror, destination: Destination) -> None:
    destination.remove_partial(entry.entry_id)
    mirror.forget(entry.entry_id)


@contextmanager
def _local_errors(destination: Destination, local: PurePosixPath, action: str) -> Iterator[None]:
    """Turn an `OSError` about `local` into a failure of that entry, `could not <action> <local>: <reason>`, or of the
    run when no entry could escape it."""
    try:
        yield
    except OSError as exc:
        if exc.errno in FATAL_ERRNOS:
            raise DestinationError.of(destination.path / local, exc) from None
        raise FileFailed(f'could not {action} {local}: {exc.strerror}') from None


@contextmanager
def _fatal_errors(path: Path) -> Iterator[None]:
    try:
        yield
    except OSError as exc:
        raise DestinationError.of(path, exc) from None


def _blocked_by(local: PurePosixPath, blocked: dict[PurePosixPath, str]) -> str | None:
    for parent in local.parents:
        if parent in blocked:
            return blocked[parent]
    return None


def _note_verified(report: MirrorReport, entry: MirrorEntry, verified_by: VerifiedBy) -> None:
    if verified_by == 'size':
        report.size_only.append(entry.resource.path)


def _method(identity: RemoteIdentity) -> VerifiedBy:
    match (identity.md5 is not None, identity.sha256 is not None):
        case (True, True):
            return 'md5+sha256'
        case (True, False):
            return 'md5'
        case (False, True):
            return 'sha256'
    return 'size'


def _recorded_method(entry: MirrorEntry) -> VerifiedBy:
    if entry.download is None or entry.download.verified_by is None:
        raise ValueError(f'{entry.resource.path} has no verified record')
    return entry.download.verified_by


def _unencodable_reason(entry: UnencodableEntry) -> str:
    if entry.kind == 'dir':
        return 'the folder name is not valid Unicode, so the Disk API cannot list it or download anything in it'
    return 'the name is not valid Unicode, so the Disk API cannot download it'


def _local_path(entry: MirrorEntry) -> PurePosixPath:
    if entry.local_path is None:
        raise ValueError(f'{entry.resource.path} has no local name')
    return entry.local_path


def _mtime_ns(modified: datetime) -> int:
    return (modified - _EPOCH) // timedelta(microseconds=1) * 1000
