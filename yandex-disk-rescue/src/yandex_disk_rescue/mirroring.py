"""The mirror engine: local names, the walk over a mirror's entries, the per-file download pipeline, and the lines
summarizing a run.

A file is verified before it reaches its final path: the bytes go to a partial file while their MD5 and SHA-256
are computed, the size and every checksum the cloud provides are checked, and only then is the file moved into
place without replacing anything. With the destination's partial files in a separate folder, a finished partial file
is copied to a staged copy in the destination as one sequential stream, one copy of the whole run at a time, and the
digest of the bytes copied is the check; the staged copy is what is placed or quarantined, and the partial file is
deleted once that is recorded. A run stopped before the placing leaves the partial file confirmed in full, so the next
run copies it again without fetching; one stopped after the placing and before the record leaves a file the next run
adopts. A partial file that cannot be deleted once its file's outcome is recorded is reported, and the outcome stands.
A downloaded file that fails the check is moved, never deleted, to the run's quarantine folder and recorded; while
the cloud file keeps that identity, only `--retry-failed` downloads it again. A verified file whose record and `lstat`
fingerprint still match is skipped without being read, unless `verify` found the file at its path broken: such a file
is downloaded again without its copy there being read, and that copy is moved aside once the new one is verified. Any
other file already at a final path is hashed: one matching the cloud is adopted as it is, and anything else there is
moved aside once the new version is verified. Paths with no cloud counterpart are never looked at.

A file larger than the policy's segment threshold is downloaded as several segments, ranged fetches running in
parallel into one partial file, which is preallocated to the file's size first and, with partial files in the
destination, read back in full to be verified once every segment is in; any other file is one stream, there verified
by the digest computed as it arrives. The segments are fixed when the download starts and kept by every later turn of
it.

Every download keeps a checkpoint journal in the manifest: for each segment, the offset up to which its bytes were
fsynced and the SHA-256 of those bytes. A checkpoint is taken every `checkpoint_bytes` a segment receives,
`CHECKPOINT_SECONDS` after the last one while bytes arrive, and whenever a transfer attempt ends with new bytes,
cancellation included. A later turn of the file (a later pass or a later run) resumes each segment from its confirmed
offset with an HTTP range once the cloud file is confirmed unchanged and the segment's confirmed bytes on disk hash
to the recorded digest; other bytes are fetched again, never trusted. A segment that fails after receiving new data
is resumed at once, within a retry budget, from the bytes this turn wrote; one that fails transiently otherwise
waits for the next pass over the files still to download.

The manifest records the folder the mirror's partial files are in. A run whose folder differs first moves each
download in progress there, one at a time: only each segment's confirmed bytes are copied, a segment whose confirmed
bytes the old file no longer holds is reset in the journal, and the copy is made durable before the old file is
deleted. Then what is left in the old folder is deleted and the new folder recorded; a run stopped on the way leaves
the old folder recorded, so the next run moves the files still there. A recorded folder that is gone is reported, and
its downloads start again.

A folder or file whose name the destination filesystem refuses is created once more under its rejected-name
conversion, which is then recorded as its local name.

Files are in progress concurrently, as many as the content client has connections. Everything that looks at a
final path and changes or records it runs under one placement lock, so two files never act on the same path or
inode at once.

Disk work that scales with a file's size or waits for the disk (hashing, reading back, writing with the digest
updates, preallocating, fsync, moving aside and placing, creating folders, setting an mtime) runs in the run's worker
threads, one bounded call at a time: at most one chunk, one `ftruncate` step, one fsync, one rename. The manifest
and the local names stay on the event loop thread.
"""

import asyncio
import hashlib
import itertools
import os
import stat
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Self

from yandex_disk_rescue import api
from yandex_disk_rescue.destination import (
    FATAL_ERRNOS,
    NAME_ERRNOS,
    Destination,
    DestinationError,
    PartialFile,
    PreviousPartials,
)
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
# A filesystem without sparse files (HFS+, exFAT, FAT) writes the zeros of an extension inside the `ftruncate` call.
_PREALLOCATION_STEP = 64 * 2**20
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_CHANGED = 'changed in the cloud since the scan; rerun with --scan-max-age 0 to fetch the new version'
_QUARANTINE_HINT = (
    'Download them again with --retry-failed (add --scan-max-age 0 if they changed on the Disk since the scan).'
)


class FileFailed(Exception):
    """One file (or folder) could not be mirrored; `reason` is the user-facing text. The run goes on."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class FileQuarantined(Exception):
    """The file's download failed verification for `reason`; it was moved to `copy` in the quarantine folder and
    recorded. `partial_left` is its partial file that could not be deleted after that, with the reason. The run goes
    on."""

    def __init__(self, reason: str, copy: PurePosixPath, partial_left: PartialLeft | None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.copy = copy
        self.partial_left = partial_left


class FileDeferred(FileFailed):
    """The file's transfer failed transiently (a stall, a connection error, 408, 429 or 5xx, the retries spent, a
    refreshed link rejected again); its partial file and record are kept so a later pass resumes it. `retry_after` is
    the wait the download host asked for, if any."""

    def __init__(self, reason: str, *, retry_after: float | None) -> None:
        super().__init__(reason)
        self.retry_after = retry_after


@dataclass(frozen=True)
class Failure:
    path: str
    reason: str


@dataclass(frozen=True)
class PartialLeft:
    """A partial file at `path` that could not be deleted once its file's outcome was recorded, for `reason`."""

    path: Path
    reason: str


@dataclass(frozen=True)
class PartialMove:
    """The partial files of downloads in progress moving from `source`, the folder the last run recorded, to `target`,
    this run's: `downloads` of them, with `size` confirmed bytes to copy. With `missing`, `source` no longer exists and
    nothing is moved."""

    source: Path
    target: Path
    downloads: int
    size: int
    missing: bool


@dataclass(frozen=True)
class QuarantinedFile:
    """A file whose quarantined copy at `copy` (relative to the destination) stands for it in this run."""

    path: str
    reason: str
    copy: PurePosixPath


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
    quarantined: list[QuarantinedFile] = field(default_factory=list)
    failed: list[Failure] = field(default_factory=list)
    partials_left: list[PartialLeft] = field(default_factory=list)
    passes: tuple[int, int] | None = None  # (used, limit; 0 is no limit) once a transient failure deferred a file


@dataclass(frozen=True)
class FileOutcome:
    """How a file came to be verified at its final path `local`: `adopted` when the copy already there matched the
    cloud, `moved_aside` when something else stood there and was moved to the replaced folder; `partial_left` is its
    partial file that could not be deleted once the file was recorded."""

    local: PurePosixPath
    verified_by: VerifiedBy
    adopted: bool
    moved_aside: bool
    partial_left: PartialLeft | None


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
            return size_mismatch(identity, self.size)
        for name, expected, actual in (
            ('MD5', identity.md5, self._md5.hexdigest()),
            ('SHA-256', identity.sha256, self._sha256.hexdigest()),
        ):
            if expected is not None and expected.lower() != actual:
                return f'{name} mismatch (expected {expected}, got {actual})'
        return None


def size_mismatch(identity: RemoteIdentity, size: int) -> str:
    """How `size` bytes differ from the size of `identity`."""
    return f'size mismatch (expected {identity.size} bytes, got {size})'


@dataclass(frozen=True)
class TransferPolicy:
    """How files are fetched: up to `retries` immediate retries of each segment after new data in each pass, a
    checkpoint after every `checkpoint_bytes` a segment receives, and a new download of a file larger than
    `segment_threshold` bytes split into `segments` segments."""

    retries: int
    checkpoint_bytes: int
    segments: int
    segment_threshold: int


def segment_plan(size: int, *, segments: int, threshold: int) -> tuple[Segment, ...]:
    """The journal a new download of a `size`-byte file starts with, confirming nothing: when `size` is above
    `threshold` bytes, `segments` segments (fewer for a file of fewer bytes), segment `i` starting at
    `i * size // segments`; otherwise one segment; none for an empty file."""
    if size == 0:
        return ()
    count = min(segments, size) if size > threshold else 1
    starts = [i * size // count for i in range(count)]
    return tuple(Segment(start, end, start, None) for start, end in itertools.pairwise([*starts, size]))


@dataclass(frozen=True)
class _RunTools:
    """What every turn of a run shares: the clients, the worker threads, the placement lock, the copy lock, the report
    and the transfer policy."""

    client: api.DiskClient
    content: api.ContentClient
    threads: DiskThreads
    placement: asyncio.Lock
    copying: asyncio.Lock
    report: MirrorReport
    policy: TransferPolicy


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
    """One run over a mirror: names are assigned, the staged copies and stray partial files a stopped run left
    deleted, the files this run will attempt counted, and the move of the partial files from the folder the mirror
    records planned (`partial_move`, `None` when it is this run's folder) on construction; `run` then makes that move
    and walks the run's entries in path order.

    With `retry_failed`, the run covers only the retry set: the files with a failure record, a quarantine record, a
    `verify` finding or a `partial` download record, and the folders with a failure record. Its walk visits them and
    the folders above them, and no other path of the mirror is looked at. Otherwise the run covers every entry, and
    holds every file whose quarantine record is for its current identity: such a file is reported as quarantined
    without being looked at.

    Every entry that fails gets a failure record with the reason when the failure happens; an entry verified, adopted
    or made in place loses it. A failed entry is reported as failed, or as quarantined when its quarantine record is
    for its current identity, the failure then shown as its last retry. Each entry the manifest records as left out of
    its scan, because its name is not valid Unicode, is a failure of the run, and has no record.
    """

    def __init__(self, mirror: Mirror, destination: Destination, *, retry_failed: bool) -> None:
        self._mirror = mirror
        self._destination = destination
        entries = _assign_local_names(mirror)
        self._names = LocalNames(mirror, entries)
        self._entries = _retry_walk(entries, self._names) if retry_failed else entries
        self._positions = {entry.entry_id: position for position, entry in enumerate(self._entries)}
        self._held = set() if retry_failed else {e.entry_id for e in self._entries if is_held(e)}
        in_progress = [e for e in entries if e.download is not None and e.download.state == 'partial']
        destination.remove_staged_copies()
        with _fatal_errors(destination.path):
            destination.keep_partials([e.entry_id for e in in_progress])
            self._current = {
                e.entry_id for e in self._entries if e.entry_id not in self._held and _is_current(e, destination)
            }
        self._partial_folder = destination.partial_folder if destination.separate_partials else None
        self._source: PreviousPartials | None = None
        self._moved: list[tuple[int, tuple[Segment, ...], int]] = []
        self.partial_move = self._plan_move(in_progress)
        pending = [
            e
            for e in self._entries
            if e.resource.kind == 'file' and e.entry_id not in self._current and e.entry_id not in self._held
        ]
        self.pending_files = len(pending)
        self.pending_size = sum(e.resource.size or 0 for e in pending)

    def _plan_move(self, in_progress: list[MirrorEntry]) -> PartialMove | None:
        """The move of the downloads in `in_progress` from the partial folder the mirror records to this run's; `None`
        when it is the same folder. Opens the recorded folder, when it exists, for `run`."""
        recorded, destination = self._mirror.partial_folder, self._destination
        if recorded == self._partial_folder:
            return None
        if recorded is None:
            source = destination.own_partials()
        elif (found := destination.previous_partials(recorded)) is None:
            return PartialMove(recorded, destination.partial_folder, 0, 0, missing=True)
        else:
            source = found
        for entry in in_progress:
            if entry.download is not None and (held := source.size(entry.entry_id)) is not None:
                self._moved.append((entry.entry_id, entry.download.segments, held))
        self._source = source
        size = sum(s.confirmed - s.start for _, segments, held in self._moved for s in _movable(segments, held))
        return PartialMove(source.path, destination.partial_folder, len(self._moved), size, missing=False)

    async def _move_partials(self, threads: DiskThreads) -> None:
        """Carry out `partial_move`: each download's partial file in turn, then the deletion of what is left in the
        recorded folder, then the record of this run's folder. A run stopped on the way keeps the recorded folder, so
        the next run moves the partial files still there, writing their copies in this run's folder again."""
        if self.partial_move is None:
            return
        if self._source is not None:
            for entry_id, segments, held in self._moved:
                with _fatal_errors(self._destination.partial_folder):
                    await self._move_partial(entry_id, segments, held, self._source, threads)
            await threads.run(self._source.remove_leftovers)
        self._mirror.record_partial_folder(self._partial_folder)

    async def _move_partial(
        self, entry_id: int, segments: tuple[Segment, ...], held: int, source: PreviousPartials, threads: DiskThreads
    ) -> None:
        """Copy the bytes the journal confirms of the `held`-byte partial file of `entry_id` in `source` to the same
        offsets of its partial file here, emptied first; a segment whose confirmed bytes are not all held is reset to
        its start in the journal instead. The copy is made durable before the source is deleted."""
        movable = _movable(segments, held)
        kept = [s if s in movable else Segment(s.start, s.end, s.start, None) for s in segments]
        if kept != list(segments):
            self._mirror.checkpoint(entry_id, kept)
        with source.partial(entry_id) as old, self._destination.partial(entry_id) as new:
            await threads.run(new.truncate, 0)
            for segment in movable:
                await _feed(new.write, old.chunks(segment.start, segment.confirmed), threads, segment.start)
            await threads.run(new.sync)
        await threads.run(self._destination.sync_partial_folder)
        await threads.run(source.remove, entry_id)

    async def run(
        self, client: api.DiskClient, content: api.ContentClient, policy: TransferPolicy, *, passes: int
    ) -> MirrorReport:
        """Move the partial files as `partial_move` says, before any fetch.

        Walk the entries in path order, then resume the files deferred by a transient failure in later passes, at
        most `passes` in all (0: until none is left), each after a pause of `PASS_PAUSE_SECONDS` or the longest wait
        the download host asked for. Each file is fetched as `policy` says.

        In every pass up to `content.connections` files are in progress at once, started in path order as slots free
        up, and the segments of a segmented file queue for the same connections; a folder is made before any entry
        below it starts. Each final path is checked, cleared, placed and recorded under one placement lock, so files
        in progress never race for a path or an inode. The report lists everything in path order, whatever the
        order the files ended in.

        The first error that stops the run (`DestinationError`, `TokenRejectedError`, `DiskApiError`) cancels the
        files in progress and is raised as itself once they have ended.

        The disk work goes to worker threads of this run's own, shut down when the run ends, also on cancellation,
        once every file's turn has ended."""
        report = MirrorReport(remote_root=self._mirror.remote_root, destination=str(self._destination.path))
        with DiskThreads(content.connections + _SPARE_THREADS) as threads:
            await self._move_partials(threads)
            tools = _RunTools(client, content, threads, asyncio.Lock(), asyncio.Lock(), report, policy)
            deferred = await self._first_pass(tools)
            if deferred:
                await self._later_passes(deferred, tools, passes=passes)
        report.replaced_folder = self._destination.replaced_folder
        report.failed += [Failure(display_path(e.path), unencodable_reason(e)) for e in self._mirror.unencodable()]
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
                if entry.entry_id in self._held:
                    report.quarantined.append(held_copy(entry, entry.failure))
                elif reason := _blocked_by(local, blocked):
                    self._fail(report, entry, reason)
                elif entry.resource.kind == 'dir':
                    await self._make_folder(entry, local, blocked, tools)
                elif entry.entry_id in self._current:
                    report.skipped += 1
                    _note_verified(report, entry, _recorded_method(entry))
                    self._succeeded(entry)
                else:
                    await turns.start(self._turn(entry, tools, deferred))
        return self._sorted(deferred)

    async def _make_folder(
        self, entry: MirrorEntry, local: PurePosixPath, blocked: dict[PurePosixPath, str], tools: _RunTools
    ) -> None:
        reason, local = await self._folder_failure(entry, local, tools)
        if reason is None:
            self._succeeded(entry)
            return
        blocked[local] = reason
        self._fail(tools.report, entry, reason)

    async def _folder_failure(
        self, entry: MirrorEntry, local: PurePosixPath, tools: _RunTools
    ) -> tuple[str | None, PurePosixPath]:
        """Make the folder `entry` at `local`, or find it there; why it failed (`None` once it is in place), with the
        local path it ended up under."""
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
                        return None, local
                    await threads.run(destination.move_aside, local)
                    report.moved_aside.append(local)
                    if await make_folder(local):
                        return None, local
            return f'{local} was created again as something other than a folder while it was being moved aside', local
        except FileFailed as failed:
            return failed.reason, local

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
        for entry, failure in deferred:
            self._fail(tools.report, entry, failure.reason + suffix)
        tools.report.passes = (used, passes)

    async def _turn(
        self, entry: MirrorEntry, tools: _RunTools, deferred: list[tuple[MirrorEntry, FileDeferred]]
    ) -> None:
        """One file's turn in a pass; a deferred file is added to `deferred`."""
        if (later := await self._download(entry, tools)) is not None:
            deferred.append((entry, later))

    def _fail(self, report: MirrorReport, entry: MirrorEntry, reason: str) -> None:
        if is_held(entry):
            report.quarantined.append(held_copy(entry, reason))
        else:
            report.failed.append(Failure(entry.resource.path, reason))
        self._mirror.record_failure(entry.entry_id, reason)

    def _succeeded(self, entry: MirrorEntry) -> None:
        """Drop the failure a folder found in place, or a file skipped as current, had when the run started; placing
        or adopting a file drops it with the verified record."""
        if entry.failure is not None:
            self._mirror.clear_failure(entry.entry_id)

    def _sorted(self, deferred: list[tuple[MirrorEntry, FileDeferred]]) -> list[tuple[MirrorEntry, FileDeferred]]:
        return sorted(deferred, key=lambda item: self._positions[item[0].entry_id])

    def _in_path_order(self, report: MirrorReport) -> None:
        by_path = {entry.resource.path: position for position, entry in enumerate(self._entries)}
        by_local = {self._names[entry.entry_id]: position for position, entry in enumerate(self._entries)}
        report.adopted.sort(key=by_path.__getitem__)
        report.size_only.sort(key=by_path.__getitem__)
        report.moved_aside.sort(key=by_local.__getitem__)
        report.quarantined.sort(key=lambda quarantined: quarantined.path)
        report.failed.sort(key=lambda failure: failure.path)
        report.partials_left.sort(key=lambda left: left.path)
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
                copying=tools.copying,
                mirror=self._mirror,
                destination=self._destination,
                names=self._names,
                policy=tools.policy,
            )
        except FileDeferred as deferred:
            return deferred
        except FileFailed as failed:
            self._fail(report, entry, failed.reason)
            return None
        except FileQuarantined as quarantined:
            report.quarantined.append(QuarantinedFile(entry.resource.path, quarantined.reason, quarantined.copy))
            if quarantined.partial_left is not None:
                report.partials_left.append(quarantined.partial_left)
            return None
        if outcome.partial_left is not None:
            report.partials_left.append(outcome.partial_left)
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
    copying: asyncio.Lock,
    mirror: Mirror,
    destination: Destination,
    names: LocalNames,
    policy: TransferPolicy,
) -> FileOutcome:
    """Make the file `entry` verified at its local path and record it; the only state it touches is the mirror's
    records, the destination and `names`. Its disk work runs in `threads`.

    The steps that decide the final path, from re-checking what is there to recording the file adopted or placed,
    hold `placement` in one acquisition, so no other file of the run sees the path or the inode in between. The
    first look at the path, hashing a local copy and fetching run outside it.

    A file already at the local path is adopted when its content matches the cloud, unless the entry has a finding:
    that copy is never read. Otherwise the file is downloaded and verified first, and only then is whatever stands at
    the local path moved aside and the new file placed; placing it drops the finding. A path that is the same file as
    another entry's verified copy is never adopted, moved or replaced. A name the destination refuses is converted,
    and the file placed under the converted name.

    The entry's download record is read from `mirror` when the turn starts. A partial file whose `partial` record
    is for the entry's identity is resumed, segment by segment, from its journal's confirmed offsets after
    `client.resource` confirms the cloud file still has it and each segment's confirmed bytes hash to the recorded
    digest; any other partial file is started again from byte 0 with the segments of `policy`. Checkpoints and
    immediate retries follow `policy`, as described in the module.

    Raises `FileDeferred` when the transfer failed transiently and a later pass may finish it, `FileQuarantined`
    when the downloaded file failed verification, `FileFailed` when this file cannot be mirrored, `DestinationError`
    when the destination cannot be written at all, and the API errors that stop the run (`TokenRejectedError`,
    `DiskApiError`).
    """
    local = _local_path(entry)
    identity = RemoteIdentity.of(entry.resource)
    with _local_errors(destination, local, 'write'):
        with _local_errors(destination, local, 'read'):
            existing = destination.lstat(local)
        if existing is not None:
            if reason := _alias(entry, local, existing, mirror, destination):
                raise FileFailed(reason)
            if entry.finding is None:
                verified_by = await _adopt(entry, local, identity, existing, mirror, destination, threads, placement)
                if verified_by is not None:
                    left = await _remove_recorded_partial(entry.entry_id, destination, threads)
                    return FileOutcome(local, verified_by, adopted=True, moved_aside=False, partial_left=left)
        verified_by = await _fetch_verified(
            entry,
            identity,
            client=client,
            content=content,
            threads=threads,
            placement=placement,
            copying=copying,
            mirror=mirror,
            destination=destination,
            policy=policy,
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
        separate = destination.separate_partials
        left = await _remove_recorded_partial(entry.entry_id, destination, threads) if separate else None
    return FileOutcome(local, verified_by, adopted=False, moved_aside=moved_aside, partial_left=left)


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
        await _feed(digest.update, file.chunks(), threads, 0)
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


class _RangeDigest:
    """The SHA-256 of bytes written in order from offset `start`."""

    def __init__(self, start: int) -> None:
        self._end = start
        self._sha256 = hashlib.sha256()

    def update(self, offset: int, chunk: bytes) -> None:
        if offset != self._end:
            raise ValueError(f'a chunk at offset {offset} does not follow the bytes hashed up to {self._end}')
        self._sha256.update(chunk)
        self._end += len(chunk)

    def sha256(self) -> str:
        """The SHA-256 hex of the bytes hashed so far, from a copy of the running hash taken now."""
        return self._sha256.copy().hexdigest()


async def _feed(
    consume: Callable[[int, bytes], None], chunks: Iterator[bytes], threads: DiskThreads, start: int
) -> int:
    """Pass every chunk of `chunks`, the bytes from offset `start` on, to `consume(offset, chunk)` in order, each read
    and consumed in a thread call of its own; the offset after the last chunk."""

    def step(offset: int) -> int | None:
        chunk = next(chunks, None)
        if chunk is None:
            return None
        consume(offset, chunk)
        return offset + len(chunk)

    offset = start
    while (after := await threads.run(step, offset)) is not None:
        offset = after
    return offset


async def _fetch_verified(
    entry: MirrorEntry,
    identity: RemoteIdentity,
    *,
    client: api.DiskClient,
    content: api.ContentClient,
    threads: DiskThreads,
    placement: asyncio.Lock,
    copying: asyncio.Lock,
    mirror: Mirror,
    destination: Destination,
    policy: TransferPolicy,
) -> VerifiedBy:
    """Fetch the file into its partial file, keeping the confirmed bytes already there when they can be resumed, give
    it the cloud mtime, durably, and verify it.

    A file fetched as one stream is cut to its confirmed bytes first, a segmented file preallocated to its size. With
    partial files in the destination, a file fetched as one stream is verified by the digest computed as it arrives,
    and a segmented file is read back in full once every segment is in. With separate partials, a checkpoint then
    confirms every byte, and the file is copied to its staged copy in the destination under `copying`, one copy of the
    run at a time, and verified by the digest of the bytes copied; the cloud mtime goes to the copy.

    A file that fails verification is moved to the run's quarantine folder under `placement`, then recorded as
    quarantined, which drops its download record, and `FileQuarantined` is raised; a separate partial file is deleted
    after the record. One cut short by a transfer error is kept with its journal. The partial file is closed only once
    no thread call on it is running, also when the task is cancelled.
    """
    with destination.partial(entry.entry_id) as partial:
        streams = await _resume(
            entry,
            identity,
            partial,
            client=client,
            mirror=mirror,
            destination=destination,
            threads=threads,
            policy=policy,
        )
        if len(streams) > 1:
            await _preallocate(partial, identity.size, threads)
        else:
            await threads.run(partial.truncate, streams[0].written)
        journal = _Journal(
            entry.entry_id,
            identity,
            streams,
            mirror=mirror,
            partial=partial,
            threads=threads,
            interval=policy.checkpoint_bytes,
        )
        fetch = _FileFetch(
            entry.resource.path,
            identity,
            partial,
            journal,
            client=client,
            content=content,
            threads=threads,
            retries=policy.retries,
        )
        await fetch.run()
        if destination.separate_partials:
            await journal.take()
            digest = await _copy_to_destination(entry.entry_id, identity, partial, destination, threads, copying)
        else:
            digest = await _file_digest(journal, partial, threads, identity.size)
            await threads.run(partial.finish, _mtime_ns(identity.modified))
    if (reason := digest.mismatch(identity)) is None:
        return _method(identity)
    async with placement:
        copy = await threads.run(destination.quarantine, entry.entry_id, _local_path(entry))
    mirror.record_quarantined(entry.entry_id, identity, copy, reason)
    separate = destination.separate_partials
    left = await _remove_recorded_partial(entry.entry_id, destination, threads) if separate else None
    raise FileQuarantined(reason, copy, left)


async def _copy_to_destination(
    entry_id: int,
    identity: RemoteIdentity,
    partial: PartialFile,
    destination: Destination,
    threads: DiskThreads,
    copying: asyncio.Lock,
) -> Digest:
    """Copy the finished partial file to its staged copy in the destination, in order, one chunk per thread call,
    under `copying`; then give the copy the cloud mtime, durably. The digest of the bytes copied."""
    digest = Digest()
    async with copying:
        with destination.copy_target(entry_id) as target:
            await _feed(digest.update, _copied(partial.chunks(0, identity.size), target), threads, 0)
            await threads.run(target.finish, _mtime_ns(identity.modified))
    return digest


def _copied(chunks: Iterator[bytes], target: PartialFile) -> Iterator[bytes]:
    """`chunks`, the bytes from offset 0 on, each written to `target` at its offset before it is passed on."""
    offset = 0
    for chunk in chunks:
        target.write(offset, chunk)
        yield chunk
        offset += len(chunk)


async def _resume(
    entry: MirrorEntry,
    identity: RemoteIdentity,
    partial: PartialFile,
    *,
    client: api.DiskClient,
    mirror: Mirror,
    destination: Destination,
    threads: DiskThreads,
    policy: TransferPolicy,
) -> list[_Stream]:
    """The streams of this turn, one per segment of the journal, each starting after the bytes of its segment kept,
    once the entry's record is a `partial` one for `identity`.

    A `partial` record for `identity` in the manifest now is kept once the cloud file is confirmed to still have that
    identity, with the segments it was started with. Each segment keeps the bytes its journal confirms when the
    partial file holds them and they hash to the recorded digest; otherwise that segment is reset to its start in
    the journal. Any other record is replaced by a new `partial` one with the segments of `policy`'s plan.

    Raises `FileFailed`, with the partial file and its record dropped, when the cloud file changed or is gone. That
    check runs for a journal confirming nothing too: an earlier attempt that received nothing usable may have met
    the changed file.
    """
    size = identity.size
    record = mirror.download(entry.entry_id)
    if record is None or record.state != 'partial' or record.identity != identity:
        segments = segment_plan(size, segments=policy.segments, threshold=policy.segment_threshold)
        mirror.record_partial(entry.entry_id, identity, segments)
        return _streams(segments, size)
    try:
        live = await client.resource(entry.resource.path)
    except api.RemotePathNotFoundError:
        _discard(entry, mirror, destination)
        raise FileFailed('removed from the Disk after the scan') from None
    if live.kind != 'file' or RemoteIdentity.of(live) != identity:
        _discard(entry, mirror, destination)
        raise FileFailed(_CHANGED)
    if not record.segments:
        return _streams((), size)
    held = partial.size()
    streams = _streams(record.segments, size)
    for position, segment in enumerate(record.segments):
        if segment.start < segment.confirmed <= held:
            stream = streams[position]
            await stream.rehash(partial, segment.confirmed, threads)
            if stream.sha256 != segment.digest:
                streams[position] = _Stream.of(segment, size)
    if [stream.written for stream in streams] != [segment.confirmed for segment in record.segments]:
        mirror.checkpoint(entry.entry_id, [stream.segment() for stream in streams])
    return streams


def _movable(segments: Sequence[Segment], held: int) -> list[Segment]:
    """The segments of a journal that confirm bytes, all of them within the first `held` bytes."""
    return [s for s in segments if s.start < s.confirmed <= held]


def _whole_file(size: int) -> tuple[Segment, ...]:
    """The journal of a download of a `size`-byte file as one stream from byte 0: one segment, none for an empty
    file."""
    return (Segment(0, size, 0, None),) if size else ()


def _streams(segments: Sequence[Segment], size: int) -> list[_Stream]:
    """A stream for each of `segments`, starting at its start; one empty stream for an empty file, which has none."""
    return [_Stream.of(segment, size) for segment in segments] or [_Stream.of(Segment(0, 0, 0, None), 0)]


async def _preallocate(partial: PartialFile, size: int, threads: DiskThreads) -> None:
    """Make the partial file `size` bytes long, extending it one `_PREALLOCATION_STEP` at most per thread call, so a
    cancellation waits for one step only."""
    length = partial.size()
    if length > size:
        await threads.run(partial.truncate, size)
    while length < size:
        length = min(length + _PREALLOCATION_STEP, size)
        await threads.run(partial.truncate, length)


async def _file_digest(journal: _Journal, partial: PartialFile, threads: DiskThreads, size: int) -> Digest:
    """The digest of the whole fetched file: the one computed as it arrived when it was fetched as one stream;
    otherwise, after a checkpoint, the digest of the partial file read back in full."""
    match journal.streams:
        case [_Stream(hasher=Digest() as digest)]:
            return digest
    await journal.take()
    digest = Digest()
    await _feed(digest.update, partial.chunks(0, size), threads, 0)
    return digest


class _Stream:
    """One segment `[start, end)` of a file in one turn.

    Its bytes `[start, written)` are in the partial file, and `sha256` is their SHA-256 (`None` while there are none).
    Both change together on the event loop once a write has returned, so a checkpoint taken while a write is still in
    flight never claims its bytes. `hasher` goes on hashing the bytes written next: for a segment that is the whole
    file, the file's `Digest`.
    """

    def __init__(self, start: int, end: int, hasher: Digest | _RangeDigest) -> None:
        self.start = start
        self.end = end
        self.hasher = hasher
        self.written = start
        self.sha256: str | None = None

    @classmethod
    def of(cls, segment: Segment, size: int) -> Self:
        """A stream of `segment` of a `size`-byte file, starting at the segment's start."""
        whole = segment.start == 0 and segment.end == size
        return cls(segment.start, segment.end, Digest() if whole else _RangeDigest(segment.start))

    @property
    def pending(self) -> bool:
        """Whether the turn still fetches the stream: some of its bytes are missing, or it is an empty file."""
        return self.written < self.end or self.end == 0

    def segment(self) -> Segment:
        return Segment(self.start, self.end, self.written, self.sha256)

    async def rehash(self, partial: PartialFile, end: int, threads: DiskThreads) -> None:
        """Hash the bytes `[start, end)` the partial file holds, the bytes kept from an earlier turn."""
        self.written = await _feed(self.hasher.update, partial.chunks(self.start, end), threads, self.start)
        self.sha256 = self.hasher.sha256()

    async def write(self, partial: PartialFile, threads: DiskThreads, offset: int, chunk: bytes) -> None:
        sha256 = await threads.run(_write_hashed, self.hasher, partial, offset, chunk)
        self.written, self.sha256 = offset + len(chunk), sha256


def _write_hashed(digest: Digest | _RangeDigest, partial: PartialFile, offset: int, chunk: bytes) -> str:
    """Write `chunk` at `offset`, then add it to `digest`, so the digest never covers a byte the write did not
    return; the SHA-256 of the bytes hashed so far."""
    partial.write(offset, chunk)
    digest.update(offset, chunk)
    return digest.sha256()


class _Journal:
    """The checkpoint journal of one file's turn: `streams`, which tile the file, and the offsets the manifest
    confirms.

    A checkpoint fsyncs the partial file in a worker thread, then records, in one manifest write, every segment's
    bytes whose write had returned when the fsync began, with their SHA-256. One checkpoint runs at a time. When the
    fsync or the commit fails, the journal stays at its previous checkpoint.
    """

    def __init__(
        self,
        entry_id: int,
        identity: RemoteIdentity,
        streams: list[_Stream],
        *,
        mirror: Mirror,
        partial: PartialFile,
        threads: DiskThreads,
        interval: int,
    ) -> None:
        self.streams = streams
        self._entry_id = entry_id
        self._identity = identity
        self._mirror = mirror
        self._partial = partial
        self._threads = threads
        self._interval = interval
        self._confirmed = {stream.start: stream.written for stream in streams}
        self._taking = asyncio.Lock()
        self._taken = time.monotonic()

    async def written(self, stream: _Stream) -> None:
        """Take a checkpoint once `stream` received `interval` bytes since the last one, or `CHECKPOINT_SECONDS`
        passed since it and new bytes arrived in any stream."""
        if stream.written - self._confirmed[stream.start] >= self._interval or (
            time.monotonic() - self._taken >= CHECKPOINT_SECONDS
            and any(s.written != self._confirmed[s.start] for s in self.streams)
        ):
            await self.take()

    async def take(self) -> None:
        """Make the bytes every stream has written confirmed; nothing when they are the confirmed ones already."""
        async with self._taking:
            segments = [stream.segment() for stream in self.streams if stream.end > stream.start]
            if all(segment.confirmed == self._confirmed[segment.start] for segment in segments):
                return
            await self._threads.run(self._partial.sync)
            self._mirror.checkpoint(self._entry_id, segments)
            self._confirmed = {segment.start: segment.confirmed for segment in segments}
            self._taken = time.monotonic()

    def restart(self) -> _Stream:
        """Replace the journal by one segment holding the whole file and confirming nothing, before the partial file
        is cut back to byte 0; the stream that fetches it."""
        size = self._identity.size
        self._mirror.record_partial(self._entry_id, self._identity, _whole_file(size))
        [stream] = self.streams = _streams(_whole_file(size), size)
        self._confirmed = {0: 0}
        self._taken = time.monotonic()
        return stream


class _Link:
    """The download link the segments of one file share in one turn: requested on first use, which comes once a
    segment holds a connection, and replaced once when the download host rejects it."""

    def __init__(self, client: api.DiskClient, path: str) -> None:
        self._client = client
        self._path = path
        self._href: str | None = None
        self._generation = 0
        self._lock = asyncio.Lock()

    async def current(self) -> tuple[str, int]:
        """The link and its generation (1 for the first link, 2 for its replacement)."""
        async with self._lock:
            if self._href is None:
                self._href = await _download_link(self._client, self._path)
                self._generation = 1
            return self._href, self._generation

    async def rejected(self, generation: int) -> bool:
        """Replace the link of `generation`, which the host rejected, unless another segment has replaced it already;
        `False` when it was the replacement, which this turn does not replace."""
        async with self._lock:
            if generation != self._generation:
                return True
            if generation > 1:
                return False
            self._href = await _download_link(self._client, self._path)
            self._generation += 1
            return True


class _FileFetch:
    """The fetch of one file's unfinished segments in one turn, each a ranged fetch of the rest of its segment, all
    started at once; the content client's connection cap queues them, and each takes the file's link only once it
    holds a connection."""

    def __init__(
        self,
        path: str,
        identity: RemoteIdentity,
        partial: PartialFile,
        journal: _Journal,
        *,
        client: api.DiskClient,
        content: api.ContentClient,
        threads: DiskThreads,
        retries: int,
    ) -> None:
        self._identity = identity
        self._partial = partial
        self._journal = journal
        self._link = _Link(client, path)
        self._content = content
        self._threads = threads
        self._retries = retries

    async def run(self) -> None:
        """Fetch every pending stream of the journal.

        The turn ends when every stream is complete or has stopped. A stream that fails cancels the others, which
        take their checkpoints, and the file fails with its error. Streams deferred by a transient failure let the
        others run on; then the file is deferred with the first one's reason and the longest wait the host asked
        for. When the host ignores a segment's range, the journal is replaced by one segment holding the whole file,
        the partial file is cut back to byte 0, and the file is fetched again as one stream.

        Raises `FileDeferred`, `FileFailed`, or an error that stops the run.
        """
        try:
            await self._fetch(self._journal.streams)
        except api.RangeNotHonouredError:
            # The task group reports the error instead of a cancellation that came while it was stopping the others.
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise asyncio.CancelledError from None
            stream = self._journal.restart()
            await self._threads.run(self._partial.truncate, 0)
            await self._fetch([stream])

    async def _fetch(self, streams: list[_Stream]) -> None:
        results = await _concurrently([self._stream(stream) for stream in streams if stream.pending])
        deferred = [result for result in results if result is not None]
        if deferred:
            waits = [failure.retry_after for failure in deferred if failure.retry_after is not None]
            raise FileDeferred(deferred[0].reason, retry_after=max(waits, default=None))

    async def _stream(self, stream: _Stream) -> FileDeferred | None:
        """Fetch the rest of `stream`; the failure when it is deferred to a later pass.

        A failure after new data is resumed at once from where it stopped, up to `retries` times. An expired link is
        replaced once for the whole file without spending a retry; when the replacement is rejected too, the stream
        is deferred, and the next pass gets a new link. Raises `FileFailed` for a failure that is not transient, and
        `RangeNotHonouredError` when the host ignored the range of a fetch of less than the whole file.
        """
        size = self._identity.size
        generation = 0

        async def link() -> str:
            nonlocal generation
            href, generation = await self._link.current()
            return href

        async def write(offset: int, chunk: bytes) -> None:
            await stream.write(self._partial, self._threads, offset, chunk)
            await self._journal.written(stream)

        attempt_number = 1
        while True:
            before = stream.written
            try:
                await self._attempt(link, stream, write)
            except api.LinkExpiredError as exc:
                if not await self._link.rejected(generation):
                    reason = f'download link rejected again after a refresh (HTTP {exc.status})'
                    return FileDeferred(reason, retry_after=None)
            except api.RangeNotHonouredError as exc:
                if before == 0 and stream.end == size:
                    raise FileFailed(exc.reason) from None
                raise
            except api.TransientTransferError as exc:
                if stream.written == stream.end:
                    return None
                if stream.written == before or attempt_number > self._retries:
                    reason = exc.reason if attempt_number == 1 else f'{exc.reason} (after {attempt_number} attempts)'
                    return FileDeferred(reason, retry_after=exc.retry_after)
                attempt_number += 1
            except api.TransferError as exc:
                raise FileFailed(exc.reason) from None
            else:
                return None

    async def _attempt(
        self, link: Callable[[], Awaitable[str]], stream: _Stream, write: Callable[[int, bytes], Awaitable[None]]
    ) -> None:
        """One fetch of the rest of `stream` from the link `link()` returns; an attempt that ends with a transfer error
        or a cancellation takes a checkpoint first."""
        try:
            await self._content.fetch(link, start=stream.written, end=stream.end, size=self._identity.size, write=write)
        except api.TransferError, asyncio.CancelledError:
            await self._journal.take()
            raise


async def _concurrently[T](calls: list[Coroutine[object, object, T]]) -> list[T]:
    """The results of `calls`, run at once. The first exception one of them raises cancels the others and, once they
    have all ended, is raised itself, with what the others raised meanwhile added as notes."""
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(call) for call in calls]
    except BaseExceptionGroup as raised:
        first, *others = raised.exceptions
        for other in others:
            first.add_note(f'Also raised while the file was stopping: {type(other).__name__}: {other}')
        raise first from None
    return [task.result() for task in tasks]


def mirror_summary(report: MirrorReport) -> list[str]:
    """The lines reporting a run: the counts, the passes once a transient failure deferred a file, adopted and size-only
    files, renamed entries, what was moved aside, quarantined files with their reasons and copies, and failures with
    their reasons."""
    lines = [
        f'Mirrored {report.remote_root} to {report.destination}: {report.downloaded} downloaded '
        f'({format_size(report.downloaded_size)}), {report.skipped} skipped, {len(report.quarantined)} quarantined, '
        f'{len(report.failed)} failed'
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
    if report.quarantined:
        lines.append(f'Quarantined: {len(report.quarantined)}')
        shown = [f'{held.path}: {held.reason}; kept at {held.copy}' for held in report.quarantined]
        lines += listed('', shown, len(shown), '')
        lines.append(_QUARANTINE_HINT)
    if report.failed:
        lines.append(f'Failed: {len(report.failed)}')
        shown = [f'{failure.path}: {failure.reason}' for failure in report.failed]
        lines += listed('', shown, len(shown), '')
    if report.partials_left:
        lines.append(f'Partial files not deleted: {len(report.partials_left)}')
        shown = [f'{left.path}: {left.reason}' for left in report.partials_left]
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


def _retry_walk(entries: list[MirrorEntry], names: LocalNames) -> list[MirrorEntry]:
    """The entries a `--retry-failed` run walks, in path order: the retry set and the folders above its members."""
    members = [
        e
        for e in entries
        if e.failure is not None
        or e.quarantine is not None
        or e.finding is not None
        or (e.resource.kind == 'file' and e.download is not None and e.download.state == 'partial')
    ]
    walked = {names[e.entry_id] for e in members}
    walked.update(parent for e in members for parent in names[e.entry_id].parents)
    return [e for e in entries if names[e.entry_id] in walked]


def is_held(entry: MirrorEntry) -> bool:
    """Whether the entry's quarantine record is for its current identity, so a plain run holds the file back."""
    return entry.quarantine is not None and entry.quarantine.identity == RemoteIdentity.of(entry.resource)


def held_copy(entry: MirrorEntry, last_retry: str | None) -> QuarantinedFile:
    """The report of a file held by its quarantine record, with the failure of its last retry, if any."""
    if entry.quarantine is None:
        raise ValueError(f'{entry.resource.path} has no quarantine record')
    reason = entry.quarantine.reason if last_retry is None else f'{entry.quarantine.reason}; last retry: {last_retry}'
    return QuarantinedFile(entry.resource.path, reason, entry.quarantine.copy)


def _is_current(entry: MirrorEntry, destination: Destination) -> bool:
    """Whether the entry is a file verified for its current identity, with no finding, whose placed copy has the
    recorded `lstat`.

    A placed copy that cannot be looked at is not current: the file's own turn meets the error and reports it.
    """
    record = entry.download
    if entry.resource.kind != 'file' or record is None or record.state != 'verified' or entry.finding is not None:
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


async def _remove_recorded_partial(entry_id: int, destination: Destination, threads: DiskThreads) -> PartialLeft | None:
    """Delete the partial file of a file whose outcome is recorded; the file left behind when an error that does not
    stop the run kept it. The outcome stands either way: the next run deletes a stray partial file that is a regular
    file."""
    try:
        await threads.run(destination.remove_partial, entry_id)
    except OSError as exc:
        return PartialLeft(Path(exc.filename), exc.strerror)
    return None


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


def unencodable_reason(entry: UnencodableEntry) -> str:
    if entry.kind == 'dir':
        return 'the folder name is not valid Unicode, so the Disk API cannot list it or download anything in it'
    return 'the name is not valid Unicode, so the Disk API cannot download it'


def _local_path(entry: MirrorEntry) -> PurePosixPath:
    if entry.local_path is None:
        raise ValueError(f'{entry.resource.path} has no local name')
    return entry.local_path


def _mtime_ns(modified: datetime) -> int:
    return (modified - _EPOCH) // timedelta(microseconds=1) * 1000
