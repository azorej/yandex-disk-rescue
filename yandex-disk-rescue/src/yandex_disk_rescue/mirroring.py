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
fingerprint still match is skipped without being read, unless the entry has a finding (the copy at its path found
broken): such a file is downloaded again without its copy there being read, and that copy is moved aside once the new
one is verified. Any other file already at a final path is hashed: one matching the cloud is adopted as it is, and
anything else there is moved aside once the new version is verified. A copy that cannot be read while the destination
itself can is recorded as a `corrupt` finding and handled as such, so it is read at most once: no later pass or run
reads it again. Paths with no cloud counterpart are never looked at.

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
the old folder recorded, so the next run moves the files still there. A recorded folder that is gone is reported when
downloads were in progress, and they start again. The recorded folder reached under another path is this run's
folder: nothing is moved, and the new path is recorded.

With separate partials, the partial files take no more than the room of their folder, measured once the move is
done (`PartialRoom`): a file fetched takes a turn of the room for its full size, in the order files start, and waits,
is deferred to the next pass or fails when it does not fit. A pass that changed nothing in the room fails the files it
deferred for room at once, since no later pass could fit them. An error writing the folder all the same stops the run.

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
import errno
import hashlib
import itertools
import logging
import os
import stat
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterable, Iterator, Sequence
from contextlib import ExitStack, asynccontextmanager, contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Self

from yandex_disk_rescue import api
from yandex_disk_rescue.destination import (
    DEVICE_GONE_ERRNOS,
    FATAL_ERRNOS,
    MISSING_ERRNOS,
    NAME_ERRNOS,
    PROCESS_LIMIT_ERRNOS,
    Destination,
    DestinationError,
    DiskAction,
    ExistingFile,
    PartialFile,
    PreviousPartials,
)
from yandex_disk_rescue.disk_threads import DiskThreads
from yandex_disk_rescue.local_names import WORK_DIR_NAME, collision_key, map_local_names, rejected_name_conversion
from yandex_disk_rescue.manifest import (
    Finding,
    LocalFingerprint,
    Mirror,
    MirrorEntry,
    RemoteIdentity,
    Segment,
    UnencodableEntry,
    VerifiedBy,
)
from yandex_disk_rescue.progress import ActiveFile, MirrorProgress, MoveProgress, Outcome
from yandex_disk_rescue.scanning import SHOWN_PATHS, listed
from yandex_disk_rescue.units import format_size

PASS_PAUSE_SECONDS = 5.0
CHECKPOINT_SECONDS = 15.0
# The bytes of a separate partial folder's filesystem the partial files leave free for everything else.
PARTIAL_RESERVE = 2**30
# A write happens only while its fetch holds one of the content client's connections, so each connection needs one
# thread; the spare ones hash, fsync and place.
_SPARE_THREADS = 4
# A filesystem without sparse files (HFS+, exFAT, FAT) writes the zeros of an extension inside the `ftruncate` call.
_PREALLOCATION_STEP = 64 * 2**20
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_CHANGED = 'changed in the cloud since the scan; rerun with --scan-max-age 0 to fetch the new version'
# A copy in DEST whose open or read fails with one of these fails only its entry: the error says the copy is out of
# reach (a permission, a path changed under the run) or the process is out of descriptors or memory, not that the copy
# is unreadable.
_COPY_OUT_OF_REACH_ERRNOS = MISSING_ERRNOS | PROCESS_LIMIT_ERRNOS | {errno.EACCES, errno.EPERM}
_QUARANTINE_HINT = (
    'Download them again with --retry-failed (add --scan-max-age 0 if they changed on the Disk since the scan).'
)
_log = logging.getLogger(__name__)


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


class NoRoomYet(FileDeferred):
    """The file's turn of the partial folder's room does not fit next to the bytes counted there now; `final` is the
    reason it fails with once no turn of the room can end to free any."""

    def __init__(self, reason: str, *, final: str) -> None:
        super().__init__(reason, retry_after=None)
        self.final = final


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
    this run's: `downloads` of them, with `size` confirmed bytes to copy. With `missing`, `source` no longer exists,
    nothing is moved and `downloads` start again from byte 0."""

    source: Path
    target: Path
    downloads: int
    size: int
    missing: bool


@dataclass(frozen=True)
class QuarantinedFile:
    """A file whose quarantined copy at `copy` (relative to the destination) stands for it in this run: quarantined
    for `reason`, and `last_retry`, the failure of the last attempt to fetch it again, if any."""

    path: str
    reason: str
    copy: PurePosixPath
    last_retry: str | None


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
    unreadable: list[Failure] = field(default_factory=list)  # copies in DEST found unreadable, downloaded again
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


class PartialRoom:
    """The room for partial files in a separate partial folder, shared by the turns of a run: `capacity` bytes, the
    free bytes of its filesystem and the bytes its partial files allocate when the room is measured, less
    `PARTIAL_RESERVE`.

    The room counts the bytes each idle partial file (one no turn is running on) allocates, and the reservation of each
    admitted turn: the rest of its file's size beyond what its own partial file allocates, so a running file counts its
    full size. Turns are admitted in the order they ask, each once the counted bytes and its reservation fit the
    capacity; a reservation of 0 is admitted at once.

    A turn that does not fit waits while an admitted turn is running, and is deferred to a later pass once none is; a
    file larger than the capacity on its own fails. When an admitted turn ends, whatever its outcome, its reservation
    is returned, its partial file counts what it allocates then, and the turns waiting are checked again in order, so a
    turn waits only while some admitted turn runs, and every admitted turn ends. An idle partial file deleted outside
    any turn is released the same way.

    `changes` counts the turns admitted and the partial files released: while it stays the same, the counted bytes do
    too, so a deferred turn would be deferred again.
    """

    def __init__(self, destination: Destination, capacity: int, idle: dict[int, int]) -> None:
        self.folder = destination.partial_folder
        self.capacity = capacity
        self._destination = destination
        self._idle = idle
        self._reserved: dict[int, int] = {}
        self._counted = sum(idle.values())
        self._waiting: deque[tuple[int, int, asyncio.Future[None]]] = deque()
        self._changes = 0

    @property
    def changes(self) -> int:
        return self._changes

    @classmethod
    def measure(cls, destination: Destination, in_progress: Iterable[int]) -> Self:
        """The room of the partial folder of `destination` now, holding the partial files of the entries `in_progress`
        idle; it blocks on the disk."""
        idle = {entry_id: destination.partial_allocated(entry_id) for entry_id in in_progress}
        capacity = destination.partial_free_bytes() + sum(idle.values()) - PARTIAL_RESERVE
        return cls(destination, capacity, {entry_id: allocated for entry_id, allocated in idle.items() if allocated})

    @asynccontextmanager
    async def turn(self, entry_id: int, size: int, progress: ActiveFile) -> AsyncIterator[None]:
        """Hold a turn of the room for the `size`-byte file of `entry_id` once it is admitted, until the context is
        left; `progress` shows the file as waiting for room while it waits.

        Raises `FileFailed` when the turn needs a reservation and `size` exceeds the capacity, and `FileDeferred` when
        the turn does not fit and no admitted turn is running.
        """
        await self._admitted(entry_id, size, progress)
        try:
            yield
        finally:
            self._end(entry_id)

    async def _admitted(self, entry_id: int, size: int, progress: ActiveFile) -> None:
        if self._reservation(entry_id, size) == 0:
            self._reserved[entry_id] = 0
            self._changes += 1
            return
        if size > self.capacity:
            raise FileFailed(
                f'larger than the room in the partial folder {self.folder} '
                f'({format_size(size)} needed, {format_size(max(self.capacity, 0))} available)'
            )
        admitted = asyncio.get_running_loop().create_future()
        waiter = (entry_id, size, admitted)
        self._waiting.append(waiter)
        self._admit()
        waits = not admitted.done()
        if waits:
            self._log_wait(progress.path, self._reservation(entry_id, size))
            progress.state('waiting for room')
        try:
            await admitted
        except asyncio.CancelledError:
            if waiter in self._waiting:
                self._waiting.remove(waiter)
                self._admit()
            elif not admitted.cancelled() and admitted.exception() is None:
                self._end(entry_id)
            raise
        if waits:
            progress.state('waiting for a connection')

    def _log_wait(self, path: str, reservation: int) -> None:
        available = max(self.capacity - self._counted, 0)
        if reservation <= available:
            ahead = len(self._waiting) - 1
            _log.debug(
                '%s waits for its turn of the partial folder %s behind %d %s waiting for room',
                path,
                self.folder,
                ahead,
                'file' if ahead == 1 else 'files',
            )
        else:
            _log.debug(
                '%s waits for room in the partial folder %s (%s needed, %s available)',
                path,
                self.folder,
                format_size(reservation),
                format_size(available),
            )

    def _reservation(self, entry_id: int, size: int) -> int:
        return max(0, size - self._idle.get(entry_id, 0))

    def _admit(self) -> None:
        """Admit, defer or skip the turns waiting, in order, up to the first that has to wait."""
        while self._waiting:
            entry_id, size, admitted = self._waiting[0]
            reservation = self._reservation(entry_id, size)
            if admitted.done():
                pass
            elif self._counted + reservation <= self.capacity:
                self._reserved[entry_id] = reservation
                self._counted += reservation
                self._changes += 1
                admitted.set_result(None)
            elif self._reserved:
                return
            else:
                admitted.set_exception(self._no_room_yet(reservation))
            self._waiting.popleft()

    def _no_room_yet(self, reservation: int) -> NoRoomYet:
        sizes = f'({format_size(reservation)} needed, {format_size(max(self.capacity - self._counted, 0))} available)'
        return NoRoomYet(
            f'no room yet in the partial folder {self.folder} {sizes}',
            final=(
                f'no room in the partial folder {self.folder} next to the partial files kept there {sizes}; free '
                'space there, then run again or with --retry-failed'
            ),
        )

    def release(self, entry_id: int) -> None:
        """Stop counting the idle partial file of `entry_id`, deleted outside any turn, and check the turns waiting."""
        if allocated := self._idle.pop(entry_id, 0):
            self._counted -= allocated
            self._changes += 1
            self._admit()

    def _end(self, entry_id: int) -> None:
        """Return the reservation of the turn of `entry_id`, count what its partial file allocates now, and check the
        turns waiting."""
        self._counted -= self._reserved.pop(entry_id) + self._idle.pop(entry_id, 0)
        try:
            allocated = self._destination.partial_allocated(entry_id)
            if allocated:
                self._idle[entry_id] = allocated
                self._counted += allocated
        finally:
            self._admit()


@dataclass(frozen=True)
class _RunTools:
    """What every turn of a run shares: the clients, the worker threads, the placement lock, the copy lock, the room
    of a separate partial folder, the report and the transfer policy."""

    client: api.DiskClient
    content: api.ContentClient
    threads: DiskThreads
    placement: asyncio.Lock
    copying: asyncio.Lock
    room: PartialRoom | None
    report: MirrorReport
    policy: TransferPolicy


class _FileTurns:
    """The turns of files running as tasks, at most `limit` at once, used as an async context manager around the walk
    that starts them; leaving it waits for every turn to end.

    The first exception a turn raises stops the run: the other turns and the walk are cancelled, and leaving the
    context raises that exception itself, with what the cancelled turns raised meanwhile added as notes. An exception
    the walk raises stops the turns the same way. A cancellation of the walk from outside (Ctrl+C, SIGTERM) wins over
    both.
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
    deleted, the verified files already in the destination checked, the files this run will attempt counted, and the
    move of the partial files from the folder the mirror records planned (`partial_move`, `None` when it is this run's
    folder) on construction; `move_partials` then makes that move, and `run` walks the run's entries in path order,
    starting the files with failed turns after the walk.
    The check and `run` report to `progress`: the check as it runs, then every file the run attempts, each settling
    once (deferred files settle in a later pass).

    With `retry_failed`, the run covers only the retry set: the files with a failure record, a quarantine record, a
    finding or a `partial` download record, and the folders with a failure record. Its walk visits them and
    the folders above them, and no other path of the mirror is looked at. Otherwise the run covers every entry, and
    holds every file whose quarantine record is for its current identity: such a file is reported as quarantined
    without being looked at.

    Every failed turn of an entry is counted and recorded when it fails, in the entry's failure record with the
    reason it is reported with and its failed turns since it last succeeded: a file that fails, a folder that cannot
    be made, an entry below such a folder, and a file deferred by a transient failure. A file deferred for room was
    not tried, and counts nothing. A file reported failed once the passes end counts one more failed turn only when
    this run counted none for it; otherwise the report restates its record with the reason it is reported with and
    the same count. A turn cancelled by Ctrl+C, SIGTERM or an error that stops the run counts nothing, and the file
    keeps the record of its last failed turn. An entry verified, adopted or made in place loses its record. A failed
    entry is reported as failed, or as quarantined when its quarantine record is for its current identity, the
    failure then shown as its last retry. Each entry the manifest records as left out of its scan, because its name
    is not valid Unicode, is a failure of the run, and has no record.

    A copy found unreadable gets its `corrupt` finding recorded and a notice when it is found, and is listed under
    `unreadable` whatever then happens to the file; a later pass of the run gives the file that finding, so the copy
    is not read again.
    """

    def __init__(
        self, mirror: Mirror, destination: Destination, *, retry_failed: bool, progress: MirrorProgress
    ) -> None:
        self._mirror = mirror
        self._destination = destination
        self._progress = progress
        entries = _assign_local_names(mirror)
        self._names = LocalNames(mirror, entries)
        self._entries = _retry_walk(entries, self._names) if retry_failed else entries
        self._positions = {entry.entry_id: position for position, entry in enumerate(self._entries)}
        self._held = set() if retry_failed else {e.entry_id for e in self._entries if is_held(e)}
        in_progress = [e for e in entries if e.download is not None and e.download.state == 'partial']
        destination.remove_staged_copies()
        self._in_progress = [e.entry_id for e in in_progress]
        destination.keep_partials(self._in_progress)
        self._current = self._check(destination)
        self._unreadable: dict[int, Finding] = {}
        self._failed_turns = {e.entry_id: e.failure.turns for e in self._entries if e.failure is not None}
        self._counted: set[int] = set()
        self._partial_folder = destination.partial_folder if destination.separate_partials else None
        self._source: PreviousPartials | None = None
        self._moved: list[tuple[MirrorEntry, tuple[Segment, ...], int]] = []
        self._move_done = False
        self.partial_move = self._plan_move(in_progress)
        pending = [
            e
            for e in self._entries
            if e.resource.kind == 'file' and e.entry_id not in self._current and e.entry_id not in self._held
        ]
        self._pending = {e.entry_id for e in pending}
        self._failed_before = sum(1 for e in pending if e.entry_id in self._failed_turns)
        self.pending_files = len(pending)
        self.pending_size = sum(e.resource.size or 0 for e in pending)

    def _plan_move(self, in_progress: list[MirrorEntry]) -> PartialMove | None:
        """The move of the downloads in `in_progress` from the partial folder the mirror records to this run's; `None`
        when it is the same folder, also under another path (a bind mount, a link on the way). Opens the recorded
        folder, when it exists, for `run`."""
        recorded, destination = self._mirror.partial_folder, self._destination
        if recorded == self._partial_folder:
            return None
        if recorded is None:
            source = destination.own_partials()
        elif (found := destination.previous_partials(recorded)) is None:
            return PartialMove(recorded, destination.partial_folder, len(in_progress), 0, missing=True)
        else:
            source = found
        if destination.is_partial_folder(source):
            return None
        for entry in in_progress:
            if entry.download is not None and (held := source.size(entry.entry_id)) is not None:
                self._moved.append((entry, entry.download.segments, held))
        self._source = source
        size = sum(s.confirmed - s.start for _, segments, held in self._moved for s in _movable(segments, held))
        return PartialMove(source.path, destination.partial_folder, len(self._moved), size, missing=False)

    async def move_partials(self, progress: MoveProgress) -> None:
        """Carry out `partial_move`, reporting it to `progress`: each download's partial file in turn, then the deletion
        of what is left in the recorded folder, then the record of this run's folder. A run stopped on the way keeps the
        recorded folder, so the next run moves the partial files still there, writing their copies in this run's folder
        again. This run's folder is recorded also when there is nothing to move. `run` starts only once this is done."""
        move = self.partial_move
        if move is not None and self._source is not None:
            started = time.monotonic()
            if move.downloads:
                progress.start(move.downloads, move.size)
            with DiskThreads(1) as threads:
                for entry, segments, held in self._moved:
                    with _fatal_errors('write', self._destination.partial_folder):
                        await self._move_partial(entry, segments, held, self._source, threads, progress)
                    progress.moved()
                await threads.run(self._source.remove_leftovers)
            if move.downloads:
                _log.info(
                    'Moved the partial files of %d downloads in progress (%s) from %s to %s in %.1f s',
                    move.downloads,
                    format_size(move.size),
                    move.source,
                    move.target,
                    time.monotonic() - started,
                )
        if self._mirror.partial_folder != self._partial_folder:
            self._mirror.record_partial_folder(self._partial_folder)
        self._move_done = True

    async def _move_partial(
        self,
        entry: MirrorEntry,
        segments: tuple[Segment, ...],
        held: int,
        source: PreviousPartials,
        threads: DiskThreads,
        progress: MoveProgress,
    ) -> None:
        """Copy the bytes the journal confirms of the `held`-byte partial file of `entry` in `source` to the same
        offsets of its partial file here, emptied first; a segment whose confirmed bytes are not all held is reset to
        its start in the journal instead. The copy is made durable before the source is deleted."""
        entry_id = entry.entry_id
        movable = _movable(segments, held)
        kept = [s if s in movable else Segment(s.start, s.end, s.start, None) for s in segments]
        if kept != list(segments):
            self._mirror.checkpoint(entry_id, kept)
        with source.partial(entry_id) as old, self._destination.partial(entry_id) as new:

            def write(offset: int, chunk: bytes) -> None:
                new.write(offset, chunk)
                progress.copied(len(chunk))

            await threads.run(new.truncate, 0)
            for segment in movable:
                chunks = _read_errors(old.chunks(segment.start, segment.confirmed), source.path / str(entry_id))
                await _feed(write, chunks, threads, segment.start)
            await threads.run(new.sync)
        await threads.run(self._destination.sync_partial_folder)
        await threads.run(source.remove, entry_id)
        moved = sum(segment.confirmed - segment.start for segment in movable)
        _log.debug(
            '%s: partial file moved from %s to %s (%s confirmed)',
            entry.resource.path,
            source.path,
            self._destination.partial_folder,
            format_size(moved),
        )

    def _check(self, destination: Destination) -> set[int]:
        """The files whose verified copy is still in place, by an `lstat` of each candidate."""
        candidates = [e for e in self._entries if e.entry_id not in self._held and _verified_now(e)]
        if not candidates:
            return set()
        self._progress.start_checking(len(candidates), str(destination.path))
        current = set()
        for entry in candidates:
            if _in_place(entry, destination):
                current.add(entry.entry_id)
            self._progress.checked()
        return current

    async def run(
        self,
        client: api.DiskClient,
        content: api.ContentClient,
        policy: TransferPolicy,
        *,
        passes: int,
        room_measured: Callable[[PartialRoom], None],
    ) -> MirrorReport:
        """With separate partials, measure the room of the partial folder and pass it to `room_measured`; every file
        fetched takes a turn of it. The caller starts the transfer phase of the run's progress first
        (`MirrorProgress.start_transferring`), so whatever shows it opens on that phase. Raises `RuntimeError` when
        `move_partials` has not run.

        Walk the entries in path order, then resume the files deferred by a transient failure in later passes, at
        most `passes` in all (0: until none is left), each after a pause of `PASS_PAUSE_SECONDS` or the longest wait
        the download host asked for. Each file is fetched as `policy` says.

        In every pass up to `content.connections` files are in progress at once, started as slots free up by their
        failed turns so far in this mirror, fewest first, then in path order: the first pass starts each file with no
        failed turn as the walk reaches it, and the others once the walk is done. The segments of a segmented file
        queue for the same connections; a folder is made before any entry below it starts. Each final path is
        checked, cleared, placed and recorded under one placement lock, so files in progress never race for a path or
        an inode. The report lists everything in path order, whatever the order the files ended in.

        The first error that stops the run (`DestinationError`, `TokenRejectedError`, `DiskApiError`) cancels the
        files in progress and is raised as itself once they have ended.

        The disk work goes to worker threads of this run's own, shut down when the run ends, also on cancellation,
        once every file's turn has ended."""
        if not self._move_done:
            raise RuntimeError('the partial files must be moved before the run')
        report = MirrorReport(remote_root=self._mirror.remote_root, destination=str(self._destination.path))
        with DiskThreads(content.connections + _SPARE_THREADS) as threads:
            room = None
            if self._destination.separate_partials:
                room = await threads.run(PartialRoom.measure, self._destination, self._in_progress)
                room_measured(room)
            tools = _RunTools(client, content, threads, asyncio.Lock(), asyncio.Lock(), room, report, policy)
            changes = _changes(room)
            _log_pass(1, passes, self.pending_files, self.pending_size, pause=None, failed_before=self._failed_before)
            deferred = await self._first_pass(tools)
            if deferred:
                await self._later_passes(deferred, tools, passes=passes, changes=changes)
        report.replaced_folder = self._destination.replaced_folder
        for unencodable in self._mirror.unencodable():
            reason = unencodable_reason(unencodable)
            _log.warning('%s failed: %s', unencodable.path, reason)
            report.failed.append(Failure(unencodable.path, reason))
        self._in_path_order(report)
        return report

    async def _first_pass(self, tools: _RunTools) -> list[tuple[MirrorEntry, FileDeferred]]:
        report = tools.report
        blocked: dict[PurePosixPath, str] = {}
        deferred: list[tuple[MirrorEntry, FileDeferred]] = []
        failed_before: list[MirrorEntry] = []
        async with _FileTurns(tools.content.connections) as turns:
            for listed_entry in self._entries:
                local = self._names[listed_entry.entry_id]
                entry = replace(listed_entry, local_path=local)
                if local.name != entry.resource.path.rpartition('/')[2]:
                    _note_renamed(report, entry, local)
                if entry.entry_id in self._held:
                    held = held_copy(entry, None if entry.failure is None else entry.failure.reason)
                    _log.debug('%s held in quarantine: kept at %s', entry.resource.path, held.copy)
                    report.quarantined.append(held)
                elif reason := _blocked_by(local, blocked):
                    self._fail(report, entry, reason)
                elif entry.resource.kind == 'dir':
                    await self._make_folder(entry, local, blocked, tools)
                elif entry.entry_id in self._current:
                    _log.debug('%s verified already: skipped', entry.resource.path)
                    report.skipped += 1
                    _note_verified(report, entry, _recorded_method(entry))
                    self._succeeded(entry)
                elif entry.entry_id in self._failed_turns:
                    failed_before.append(entry)
                else:
                    await turns.start(self._turn(entry, tools, deferred))
            for entry in sorted(failed_before, key=self._start_key):
                await turns.start(self._turn(entry, tools, deferred))
        return self._in_start_order(deferred)

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
                        _note_renamed(report, entry, local)
                    if made:
                        return None, local
                    await threads.run(destination.move_aside, local)
                    _note_moved_aside(report, destination, local)
                    if await make_folder(local):
                        return None, local
            return f'{local} was created again as something other than a folder while it was being moved aside', local
        except FileFailed as failed:
            return failed.reason, local

    async def _later_passes(
        self, deferred: list[tuple[MirrorEntry, FileDeferred]], tools: _RunTools, *, passes: int, changes: int
    ) -> None:
        """Resume the `deferred` files in later passes, at most `passes` in all (0: no limit). When the files a pass
        deferred all wait for room, and the room is as it was when that pass began (at `changes`), no later pass could
        fit them: they fail at once with their final reason."""
        used = 1
        while deferred and not _stuck_for_room(deferred, tools.room, changes) and (passes == 0 or used < passes):
            waits = [failure.retry_after for _, failure in deferred if failure.retry_after is not None]
            pause = max([PASS_PAUSE_SECONDS, *waits])
            used += 1
            _log_pass(used, passes, len(deferred), sum(e.resource.size or 0 for e, _ in deferred), pause=pause)
            self._progress.pausing(used, pause)
            await asyncio.sleep(pause)
            self._progress.resumed()
            changes = _changes(tools.room)
            remaining: list[tuple[MirrorEntry, FileDeferred]] = []
            async with _FileTurns(tools.content.connections) as turns:
                for entry, _ in deferred:
                    await turns.start(self._turn(entry, tools, remaining))
            deferred = self._in_start_order(remaining)
        stuck = _stuck_for_room(deferred, tools.room, changes)
        suffix = f' (after {used} passes)' if used > 1 else ''
        for entry, failure in deferred:
            reason = failure.final if stuck and isinstance(failure, NoRoomYet) else failure.reason + suffix
            self._fail(tools.report, entry, reason, new_turn=entry.entry_id not in self._counted)
        tools.report.passes = (used, passes)

    async def _turn(
        self, entry: MirrorEntry, tools: _RunTools, deferred: list[tuple[MirrorEntry, FileDeferred]]
    ) -> None:
        """One file's turn in a pass; a deferred file is added to `deferred`."""
        if (later := await self._download(entry, tools)) is not None:
            _log.info('%s deferred to a later pass: %s', entry.resource.path, later.reason)
            if not isinstance(later, NoRoomYet):
                self._mirror.record_failure(entry.entry_id, later.reason, turns=self._count_failed_turn(entry))
            self._progress.defer(entry.entry_id)
            deferred.append((entry, later))

    def _fail(self, report: MirrorReport, entry: MirrorEntry, reason: str, *, new_turn: bool = True) -> None:
        """Report `entry` as failed for `reason` and record it; `new_turn` counts one more failed turn, otherwise the
        record keeps the count this run already gave it."""
        path = entry.resource.path
        outcome: Outcome
        if is_held(entry):
            held = held_copy(entry, reason)
            _log.warning('%s failed again: %s; its quarantined copy stays at %s', path, reason, held.copy)
            report.quarantined.append(held)
            outcome = 'quarantined'
        else:
            _log.warning('%s failed: %s', path, reason)
            report.failed.append(Failure(entry.resource.path, reason))
            outcome = 'failed'
        turns = self._count_failed_turn(entry) if new_turn else self._failed_turns[entry.entry_id]
        self._mirror.record_failure(entry.entry_id, reason, turns=turns)
        self._settle(entry, outcome)

    def _count_failed_turn(self, entry: MirrorEntry) -> int:
        """Count one more failed turn of `entry`; its failed turns since it last succeeded."""
        turns = self._failed_turns.get(entry.entry_id, 0) + 1
        self._failed_turns[entry.entry_id] = turns
        self._counted.add(entry.entry_id)
        return turns

    def _settle(self, entry: MirrorEntry, outcome: Outcome) -> None:
        if entry.entry_id in self._pending:
            self._progress.settle(entry.entry_id, entry.resource.size or 0, outcome)

    def _succeeded(self, entry: MirrorEntry) -> None:
        """Drop the failure a folder found in place, or a file skipped as current, had when the run started; placing
        or adopting a file drops it with the verified record."""
        if entry.failure is not None:
            self._mirror.clear_failure(entry.entry_id)

    def _found_unreadable(self, report: MirrorReport, entry: MirrorEntry, finding: Finding) -> None:
        _log.warning('%s %s: %s', entry.resource.path, finding.kind, finding.reason)
        self._mirror.record_finding(entry.entry_id, finding)
        self._unreadable[entry.entry_id] = finding
        report.unreadable.append(Failure(entry.resource.path, finding.reason))

    def _start_key(self, entry: MirrorEntry) -> tuple[int, int]:
        """Where `entry` starts among the files of a pass: by its failed turns so far, then by its place in the walk."""
        return self._failed_turns.get(entry.entry_id, 0), self._positions[entry.entry_id]

    def _in_start_order(
        self, deferred: list[tuple[MirrorEntry, FileDeferred]]
    ) -> list[tuple[MirrorEntry, FileDeferred]]:
        return sorted(deferred, key=lambda item: self._start_key(item[0]))

    def _in_path_order(self, report: MirrorReport) -> None:
        by_path = {entry.resource.path: position for position, entry in enumerate(self._entries)}
        by_local = {self._names[entry.entry_id]: position for position, entry in enumerate(self._entries)}
        report.adopted.sort(key=by_path.__getitem__)
        report.size_only.sort(key=by_path.__getitem__)
        report.moved_aside.sort(key=by_local.__getitem__)
        report.quarantined.sort(key=lambda quarantined: quarantined.path)
        report.failed.sort(key=lambda failure: failure.path)
        report.unreadable.sort(key=lambda unreadable: unreadable.path)
        report.partials_left.sort(key=lambda left: left.path)
        report.renamed.sort()

    async def _download(self, entry: MirrorEntry, tools: _RunTools) -> FileDeferred | None:
        """Mirror one file into the report; the failure when it is deferred to a later pass instead. A file whose copy
        an earlier pass found unreadable takes that finding, so the copy is not read again."""
        report = tools.report
        path = entry.resource.path
        size = entry.resource.size or 0
        if (finding := self._unreadable.get(entry.entry_id)) is not None:
            entry = replace(entry, finding=finding)
        started = time.monotonic()
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
                room=tools.room,
                progress=self._progress.begin(entry.entry_id, path, size),
                unreadable=lambda found: self._found_unreadable(report, entry, found),
            )
        except FileDeferred as deferred:
            return deferred
        except FileFailed as failed:
            self._fail(report, entry, failed.reason)
            return None
        except FileQuarantined as quarantined:
            _log.warning('%s quarantined: %s; kept at %s', path, quarantined.reason, quarantined.copy)
            report.quarantined.append(
                QuarantinedFile(entry.resource.path, quarantined.reason, quarantined.copy, last_retry=None)
            )
            if quarantined.partial_left is not None:
                report.partials_left.append(quarantined.partial_left)
            self._settle(entry, 'quarantined')
            return None
        self._settle(entry, 'downloaded')
        if outcome.partial_left is not None:
            report.partials_left.append(outcome.partial_left)
        if outcome.adopted:
            _log.info('%s adopted: the copy at %s matches (verified by %s)', path, outcome.local, outcome.verified_by)
            report.skipped += 1
            report.adopted.append(entry.resource.path)
        else:
            seconds = time.monotonic() - started
            speed = format_size(round(size / max(seconds, 0.001)))
            _log.info(
                '%s downloaded: %s in %.1f s, %s/s, verified by %s',
                path,
                format_size(size),
                seconds,
                speed,
                outcome.verified_by,
            )
            report.downloaded += 1
            report.downloaded_size += size
        if outcome.local != entry.local_path:
            _note_renamed(report, entry, outcome.local)
        if outcome.moved_aside:
            _note_moved_aside(report, self._destination, outcome.local)
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
    room: PartialRoom | None,
    progress: ActiveFile,
    unreadable: Callable[[Finding], None],
) -> FileOutcome:
    """Make the file `entry` verified at its local path and record it, reporting what it does to `progress`; the only
    state it touches is the mirror's records, the destination and `names`. Its disk work runs in `threads`.

    With `room`, a file that is not adopted takes a turn of the room before its partial file is opened, held until it
    is placed, quarantined or stopped.

    The steps that decide the final path, from re-checking what is there to recording the file adopted or placed,
    hold `placement` in one acquisition, so no other file of the run sees the path or the inode in between. The
    first look at the path, hashing a local copy and fetching run outside it.

    A file already at the local path is adopted when its content matches the cloud, unless the entry has a finding:
    that copy is never read. A copy that cannot be read, while the destination itself can, is passed to `unreadable`
    as a `corrupt` finding (the caller records it) and handled as a file with that finding; a permission error on it
    fails the file instead. Otherwise the file is downloaded and verified first, and only then is whatever stands at
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
    when the destination cannot be read, written or inspected at all (a local copy that cannot be read while the
    device is gone or the destination itself unreadable included), and the API errors that stop the run
    (`TokenRejectedError`, `DiskApiError`).
    """
    local = _local_path(entry)
    identity = RemoteIdentity.of(entry.resource)
    with _local_errors(destination, local, 'write'):
        with _local_errors(destination, local, 'inspect'):
            existing = destination.lstat(local)
        if existing is not None:
            if reason := _alias(entry, local, existing, mirror, destination):
                raise FileFailed(reason)
            if entry.finding is None:
                verified_by = await _adopt(
                    entry, local, identity, existing, mirror, destination, threads, placement, progress, unreadable
                )
                if verified_by is not None:
                    left = await _remove_recorded_partial(entry, destination, threads)
                    if left is None and room is not None:
                        room.release(entry.entry_id)
                    return FileOutcome(local, verified_by, adopted=True, moved_aside=False, partial_left=left)
    async with nullcontext() if room is None else room.turn(entry.entry_id, identity.size, progress):
        with _local_errors(destination, local, 'write'):
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
                progress=progress,
            )
            local, moved_aside = await _place_verified(
                entry, local, identity, verified_by, mirror, destination, names, threads, placement
            )
            separate = destination.separate_partials
            left = await _remove_recorded_partial(entry, destination, threads) if separate else None
    return FileOutcome(local, verified_by, adopted=False, moved_aside=moved_aside, partial_left=left)


async def _place_verified(
    entry: MirrorEntry,
    local: PurePosixPath,
    identity: RemoteIdentity,
    verified_by: VerifiedBy,
    mirror: Mirror,
    destination: Destination,
    names: LocalNames,
    threads: DiskThreads,
    placement: asyncio.Lock,
) -> tuple[PurePosixPath, bool]:
    """Under `placement`, move whatever stands at `local` aside, place the verified file there, converting a name the
    destination refuses, and record it; the path it was placed at, and whether something was moved aside."""

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
    return local, moved_aside


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
    progress: ActiveFile,
    unreadable: Callable[[Finding], None],
) -> VerifiedBy | None:
    """Hash the regular file `existing` at `local` and, when it matches `identity`, give it the cloud mtime and
    record it as verified; `None` when it is not such a file, does not match, or cannot be read.

    The hash runs outside `placement`. Under it, the file at `local` must still be the one hashed (same inode, size
    and mtime) and must not have become another entry's verified copy meanwhile; otherwise it is not adopted, or the
    entry fails as an alias. Only then is its mtime set and the record written.

    A copy that cannot be opened or read is passed to `unreadable` as a `corrupt` finding, as `_unreadable_copy`
    classifies the error; it is never written to.
    """
    if not stat.S_ISREG(existing.st_mode) or existing.st_size != identity.size:
        return None
    digest = Digest()
    with ExitStack() as stack:
        try:
            file = stack.enter_context(destination.existing_file(local))
            opened = file.stat()
            if not stat.S_ISREG(opened.st_mode) or opened.st_ino != existing.st_ino:
                return None
            progress.state('hashing')
            await _feed(digest.update, file.chunks(), threads, 0)
        except OSError as exc:
            unreadable(await _unreadable_copy(local, exc, destination, threads))
            return None
        progress.state('waiting for a connection')
        if digest.mismatch(identity) is not None:
            return None
        return await _record_adopted(entry, local, identity, file, opened, mirror, destination, threads, placement)


async def _record_adopted(
    entry: MirrorEntry,
    local: PurePosixPath,
    identity: RemoteIdentity,
    file: ExistingFile,
    opened: os.stat_result,
    mirror: Mirror,
    destination: Destination,
    threads: DiskThreads,
    placement: asyncio.Lock,
) -> VerifiedBy | None:
    """Under `placement`, give the hashed copy `file` at `local` the cloud mtime and record it as verified, unless the
    file at `local` is no longer the one hashed (`opened`); an alias of another entry's verified copy fails."""
    async with placement:
        with _local_errors(destination, local, 'inspect'):
            now = destination.lstat(local)
        if now is None or LocalFingerprint.of(now) != LocalFingerprint.of(opened):
            return None
        if reason := _alias(entry, local, now, mirror, destination):
            raise FileFailed(reason)
        with _local_errors(destination, local, 'write', failing='set the modification time of'):
            await threads.run(file.set_mtime, _mtime_ns(identity.modified))
        with _local_errors(destination, local, 'inspect'):
            adopted = file.stat()
        verified_by = _method(identity)
        mirror.record_verified(entry.entry_id, identity, verified_by, adopted)
    return verified_by


async def _unreadable_copy(
    local: PurePosixPath, exc: OSError, destination: Destination, threads: DiskThreads
) -> Finding:
    """The finding about the copy at `local` whose read failed with `exc`: corrupt, as it cannot be read.

    Raises `FileFailed` when the copy is out of reach rather than unreadable (a permission error, a path changed under
    the run, the process out of descriptors or memory), and `DestinationError` when the device is gone or the
    destination itself can no longer be read.
    """
    reason = f'could not read {local}: {exc.strerror}'
    if exc.errno in _COPY_OUT_OF_REACH_ERRNOS:
        raise FileFailed(reason) from None
    if exc.errno in DEVICE_GONE_ERRNOS or not await threads.run(destination.reachable):
        raise DestinationError.of('read', destination.path / local, exc) from None
    return Finding('corrupt', reason)


async def _clear_the_way(
    entry: MirrorEntry, local: PurePosixPath, mirror: Mirror, destination: Destination, threads: DiskThreads
) -> bool:
    """Move whatever stands at `local` aside so the verified partial file can be placed; `True` when something was
    moved."""
    with _local_errors(destination, local, 'inspect'):
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
    progress: ActiveFile,
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
            progress=progress,
        )
        if len(streams) > 1:
            progress.state('preallocating')
            await _preallocate(partial, identity.size, threads)
        else:
            await threads.run(partial.truncate, streams[0].written)
        progress.state('waiting for a connection')
        journal = _Journal(
            entry.entry_id,
            entry.resource.path,
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
            progress=progress,
        )
        await fetch.run()
        if destination.separate_partials:
            await journal.take()
            digest = await _copy_to_destination(entry, identity, partial, destination, threads, copying, progress)
        else:
            digest = await _file_digest(journal, partial, threads, identity.size, progress)
            await threads.run(partial.finish, _mtime_ns(identity.modified))
    if (reason := digest.mismatch(identity)) is None:
        return _method(identity)
    async with placement:
        copy = await threads.run(destination.quarantine, entry.entry_id, _local_path(entry))
    mirror.record_quarantined(entry.entry_id, identity, copy, reason)
    separate = destination.separate_partials
    left = await _remove_recorded_partial(entry, destination, threads) if separate else None
    raise FileQuarantined(reason, copy, left)


async def _copy_to_destination(
    entry: MirrorEntry,
    identity: RemoteIdentity,
    partial: PartialFile,
    destination: Destination,
    threads: DiskThreads,
    copying: asyncio.Lock,
    progress: ActiveFile,
) -> Digest:
    """Copy the finished partial file to its staged copy in the destination, in order, one chunk per thread call,
    under `copying`; then give the copy the cloud mtime, durably. The digest of the bytes copied."""
    path = entry.resource.path
    if copying.locked():
        _log.debug('%s waits for the copy of another file to %s', path, destination.path)
        progress.state('waiting for the copy')
    digest = Digest()
    async with copying:
        progress.state('copying')
        started = time.monotonic()
        with destination.copy_target(entry.entry_id) as target:
            await _feed(digest.update, _copied(partial.chunks(0, identity.size), target, progress), threads, 0)
            await threads.run(target.finish, _mtime_ns(identity.modified))
        _log.debug('%s copied to %s in %.1f s', path, destination.path, time.monotonic() - started)
    return digest


def _copied(chunks: Iterator[bytes], target: PartialFile, progress: ActiveFile) -> Iterator[bytes]:
    """`chunks`, the bytes from offset 0 on, each written to `target` at its offset and counted in `progress` before
    it is passed on."""
    offset = 0
    for chunk in chunks:
        target.write(offset, chunk)
        progress.copied(len(chunk))
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
    progress: ActiveFile,
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
    path = entry.resource.path
    for position, segment in enumerate(record.segments):
        if segment.start < segment.confirmed <= held:
            stream = streams[position]
            progress.state('hashing')
            await stream.rehash(partial, segment.confirmed, threads)
            if stream.sha256 != segment.digest:
                _log.debug('%s: segment at byte %d does not match its checkpoint; fetched again', path, segment.start)
                streams[position] = _Stream.of(segment, size)
            else:
                kept = segment.confirmed - segment.start
                _log.debug('%s: segment at byte %d resumed with %d bytes kept', path, segment.start, kept)
                progress.kept(kept)
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


async def _file_digest(
    journal: _Journal, partial: PartialFile, threads: DiskThreads, size: int, progress: ActiveFile
) -> Digest:
    """The digest of the whole fetched file: the one computed as it arrived when it was fetched as one stream;
    otherwise, after a checkpoint, the digest of the partial file read back in full."""
    match journal.streams:
        case [_Stream(hasher=Digest() as digest)]:
            return digest
    await journal.take()
    progress.state('reading back')
    digest = Digest()
    await _feed(digest.update, partial.chunks(0, size), threads, 0)
    return digest


class _Stream:
    """One segment `[start, end)` of a file in one turn.

    Its bytes `[start, written)` are in the partial file, and `sha256` is their SHA-256 (`None` while there are none).
    Both change together on the event loop once a write has returned, also when the task is cancelled while it runs,
    so a checkpoint taken while a write is still in flight never claims its bytes and one taken on cancellation claims
    every byte written. `hasher` goes on hashing the bytes written next: for a segment that is the whole
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
        """Write `chunk` at `offset` and hash it; a write that returned is counted in `written` and `sha256` also when
        the task is cancelled while it runs."""

        def advance(sha256: str) -> None:
            self.written, self.sha256 = offset + len(chunk), sha256

        await threads.run_then(_write_hashed, self.hasher, partial, offset, chunk, then=advance)


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
        path: str,
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
        self._path = path
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
            confirmed = sum(segment.confirmed - segment.start for segment in segments)
            _log.debug('%s: checkpoint, %d of %d bytes confirmed', self._path, confirmed, self._identity.size)
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
            _log.debug('%s: the download host rejected the link; getting a new one', self._path)
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
        progress: ActiveFile,
    ) -> None:
        self._path = path
        self._identity = identity
        self._partial = partial
        self._journal = journal
        self._link = _Link(client, path)
        self._content = content
        self._threads = threads
        self._retries = retries
        self._progress = progress

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
            _log.debug('%s: the download host ignored a range; fetching the whole file as one stream', self._path)
            stream = self._journal.restart()
            await self._threads.run(self._partial.truncate, 0)
            self._progress.cut()
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
            self._progress.wrote(len(chunk))
            await self._journal.written(stream)

        attempt_number = 1
        while True:
            before = stream.written
            _log.debug(
                '%s: fetching bytes %d to %d of %d (attempt %d)', self._path, before, stream.end, size, attempt_number
            )
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
                _log.debug('%s: %s; resuming at once from byte %d', self._path, exc.reason, stream.written)
                attempt_number += 1
            except api.TransferError as exc:
                raise FileFailed(exc.reason) from None
            else:
                return None

    async def _attempt(
        self, link: Callable[[], Awaitable[str]], stream: _Stream, write: Callable[[int, bytes], Awaitable[None]]
    ) -> None:
        """One fetch of the rest of `stream` from the link `link()` returns, counted as a connection of the file from
        when it has its link until it ends; an attempt that ends with a transfer error or a cancellation takes a
        checkpoint first."""
        connected = False

        async def connection() -> str:
            nonlocal connected
            href = await link()
            connected = True
            self._progress.connected()
            return href

        try:
            await self._content.fetch(
                connection, start=stream.written, end=stream.end, size=self._identity.size, write=write
            )
        except api.TransferError, asyncio.CancelledError:
            await self._journal.take()
            raise
        finally:
            if connected:
                self._progress.disconnected()


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


def mirror_summary(report: MirrorReport, *, limit: int | None = SHOWN_PATHS) -> list[str]:
    """The lines reporting a run: the counts, the passes once a transient failure deferred a file, adopted and size-only
    files, renamed entries, the copies found unreadable, what was moved aside, quarantined files with their reasons and
    copies, and failures with their reasons; each list shows its first `limit` members (`None`: all)."""
    lines = [
        f'Mirrored {report.remote_root} to {report.destination}: {report.downloaded} downloaded '
        f'({format_size(report.downloaded_size)}), {report.skipped} skipped, {len(report.quarantined)} quarantined, '
        f'{len(report.failed)} failed'
    ]
    if report.passes is not None:
        used, most = report.passes
        lines.append(f'Passes: {used} of {most or "unlimited"}')
    if report.adopted:
        lines.append(f'Already on disk and verified: {len(report.adopted)}')
        lines += listed('', report.adopted, len(report.adopted), '', limit=limit)
    if report.size_only:
        lines.append(f'Verified by size only (no cloud checksum): {len(report.size_only)}')
        lines += listed('', report.size_only, len(report.size_only), '', limit=limit)
    if report.renamed:
        lines.append(f'Renamed for the destination: {len(report.renamed)}')
        shown = [f'{remote} -> {local}' for remote, local in report.renamed]
        lines += listed('', shown, len(shown), '', limit=limit)
    if report.unreadable:
        lines.append(f'Unreadable in DEST, downloaded again: {len(report.unreadable)}')
        shown = [f'{unreadable.path}: {unreadable.reason}' for unreadable in report.unreadable]
        lines += listed('', shown, len(shown), '', limit=limit)
    if report.moved_aside:
        lines.append(f'Moved aside to {report.replaced_folder}/: {len(report.moved_aside)}')
        shown = [str(local) for local in report.moved_aside]
        lines += listed('', shown, len(shown), '', limit=limit)
    if report.quarantined:
        lines.append(f'Quarantined: {len(report.quarantined)}')
        shown = [quarantined_line(held) for held in report.quarantined]
        lines += listed('', shown, len(shown), '', limit=limit)
        lines.append(_QUARANTINE_HINT)
    if report.failed:
        lines.append(f'Failed: {len(report.failed)}')
        shown = [f'{failure.path}: {failure.reason}' for failure in report.failed]
        lines += listed('', shown, len(shown), '', limit=limit)
    if report.partials_left:
        lines.append(f'Partial files not deleted: {len(report.partials_left)}')
        shown = [f'{left.path}: {left.reason}' for left in report.partials_left]
        lines += listed('', shown, len(shown), '', limit=limit)
    return lines


def mirror_document(report: MirrorReport) -> dict[str, object]:
    """The JSON object of a run: the counts of the summary's head line (`skipped` includes `adopted`), the passes,
    and every list in full."""
    passes = None if report.passes is None else {'used': report.passes[0], 'limit': report.passes[1]}
    return {
        'remote_root': report.remote_root,
        'destination': report.destination,
        'counts': {
            'downloaded': report.downloaded,
            'downloaded_size': report.downloaded_size,
            'skipped': report.skipped,
            'adopted': len(report.adopted),
            'size_only': len(report.size_only),
            'renamed': len(report.renamed),
            'unreadable': len(report.unreadable),
            'moved_aside': len(report.moved_aside),
            'quarantined': len(report.quarantined),
            'failed': len(report.failed),
        },
        'passes': passes,
        'adopted': report.adopted,
        'size_only': report.size_only,
        'renamed': [{'path': remote, 'local': str(local)} for remote, local in report.renamed],
        'unreadable': [{'path': unreadable.path, 'reason': unreadable.reason} for unreadable in report.unreadable],
        'moved_aside': [str(local) for local in report.moved_aside],
        'replaced_folder': None if report.replaced_folder is None else str(report.replaced_folder),
        'quarantined': [quarantined_document(held) for held in report.quarantined],
        'failed': [{'path': failure.path, 'reason': failure.reason} for failure in report.failed],
        'partials_left': [{'path': str(left.path), 'reason': left.reason} for left in report.partials_left],
    }


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
    return QuarantinedFile(entry.resource.path, entry.quarantine.reason, entry.quarantine.copy, last_retry)


def quarantined_line(held: QuarantinedFile) -> str:
    """How a summary lists the quarantined file `held`: its path and reason, its last retry, and its copy."""
    return f'{held.path}: {quarantined_reason(held)}; kept at {held.copy}'


def quarantined_reason(held: QuarantinedFile) -> str:
    """The quarantine reason of `held`, followed by `; last retry: <failure>` when a retry failed."""
    return held.reason if held.last_retry is None else f'{held.reason}; last retry: {held.last_retry}'


def quarantined_document(held: QuarantinedFile) -> dict[str, object]:
    """The JSON object of the quarantined file `held`."""
    return {'path': held.path, 'reason': held.reason, 'last_retry': held.last_retry, 'copy': str(held.copy)}


def _verified_now(entry: MirrorEntry) -> bool:
    """Whether the entry is a file verified for its current identity, with no finding."""
    record = entry.download
    if entry.resource.kind != 'file' or record is None or record.state != 'verified' or entry.finding is not None:
        return False
    return record.identity == RemoteIdentity.of(entry.resource)


def _in_place(entry: MirrorEntry, destination: Destination) -> bool:
    """Whether the placed copy of the verified file `entry` has the recorded `lstat`.

    A placed copy that cannot be looked at is not in place: the file's own turn meets the error and reports it. An
    error no file would escape (`FATAL_ERRNOS`) raises `DestinationError` naming the copy.
    """
    record = entry.download
    if record is None:
        raise ValueError(f'{entry.resource.path} has no download record')
    local = _local_path(entry)
    try:
        placed = destination.lstat(local)
    except OSError as exc:
        if exc.errno in FATAL_ERRNOS:
            raise DestinationError.of('inspect', destination.path / local, exc) from None
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
    with _local_errors(destination, owner.local_path, 'inspect'):
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


async def _remove_recorded_partial(
    entry: MirrorEntry, destination: Destination, threads: DiskThreads
) -> PartialLeft | None:
    """Delete the partial file of a file whose outcome is recorded; the file left behind when an error that does not
    stop the run kept it. The outcome stands either way: the next run deletes a stray partial file that is a regular
    file."""
    try:
        await threads.run(destination.remove_partial, entry.entry_id)
    except OSError as exc:
        left = PartialLeft(Path(exc.filename), exc.strerror)
        _log.warning(
            '%s: partial file %s not deleted: %s; the outcome stands',
            entry.resource.path,
            left.path,
            left.reason,
        )
        return left
    return None


def _discard(entry: MirrorEntry, mirror: Mirror, destination: Destination) -> None:
    destination.remove_partial(entry.entry_id)
    mirror.forget(entry.entry_id)


@contextmanager
def _local_errors(
    destination: Destination, local: PurePosixPath, action: DiskAction, *, failing: str | None = None
) -> Iterator[None]:
    """Turn an `OSError` about `local` into a failure of that entry, `could not <failing or action> <local>:
    <reason>`, or of the run, `DestinationError` with `action`, when no entry could escape it."""
    try:
        yield
    except OSError as exc:
        if exc.errno in FATAL_ERRNOS:
            raise DestinationError.of(action, destination.path / local, exc) from None
        raise FileFailed(f'could not {failing or action} {local}: {exc.strerror}') from None


@contextmanager
def _fatal_errors(action: DiskAction, path: Path) -> Iterator[None]:
    try:
        yield
    except OSError as exc:
        raise DestinationError.of(action, path, exc) from None


def _read_errors(chunks: Iterator[bytes], path: Path) -> Iterator[bytes]:
    """`chunks`, read from the file at `path`; an error reading them raises `DestinationError` naming it."""
    try:
        yield from chunks
    except OSError as exc:
        raise DestinationError.of('read', path, exc) from None


def _changes(room: PartialRoom | None) -> int:
    return 0 if room is None else room.changes


def _stuck_for_room(deferred: list[tuple[MirrorEntry, FileDeferred]], room: PartialRoom | None, changes: int) -> bool:
    """Whether every file of `deferred` waits for room in `room`, unchanged since it counted `changes`."""
    return room is not None and room.changes == changes and all(isinstance(f, NoRoomYet) for _, f in deferred)


def _blocked_by(local: PurePosixPath, blocked: dict[PurePosixPath, str]) -> str | None:
    for parent in local.parents:
        if parent in blocked:
            return blocked[parent]
    return None


def _note_verified(report: MirrorReport, entry: MirrorEntry, verified_by: VerifiedBy) -> None:
    if verified_by == 'size':
        report.size_only.append(entry.resource.path)


def _note_renamed(report: MirrorReport, entry: MirrorEntry, local: PurePosixPath) -> None:
    _log.info('%s renamed for the destination: %s', entry.resource.path, local)
    report.renamed.append((entry.resource.path, local))


def _note_moved_aside(report: MirrorReport, destination: Destination, local: PurePosixPath) -> None:
    _log.info('%s moved aside to %s/', local, destination.replaced_folder)
    report.moved_aside.append(local)


def _log_pass(number: int, passes: int, files: int, size: int, *, pause: float | None, failed_before: int = 0) -> None:
    """Log the start of a pass over `files` files of `size` bytes, `failed_before` of which have a failure record."""
    start = 'starts' if pause is None else f'starts in {pause:g} s'
    if failed_before:
        start += f'; {failed_before} that failed before'
    limit = passes or 'unlimited'
    noun = 'file' if files == 1 else 'files'
    _log.info('Pass %d of %s over %d %s (%s) %s', number, limit, files, noun, format_size(size), start)


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
