"""The mirror engine: local names, the walk over a mirror's entries, the per-file download pipeline, and the lines
summarizing a run.

A file is verified before it reaches its final path: the bytes go to a partial file while their MD5 and SHA-256
are computed, the size and every checksum the cloud provides are checked, and only then is the file moved into
place without replacing anything. A verified file whose record and `lstat` fingerprint still match is skipped
without being read. Any other file already at a final path is hashed: one matching the cloud is adopted as it is,
and anything else there is moved aside once the new version is verified. Paths with no cloud counterpart are never
looked at.

A partial file left by an interrupted run or an earlier pass is resumed with an HTTP range once the cloud file is
confirmed unchanged. A transfer that fails after receiving new data is resumed at once, within a retry budget; one
that fails transiently otherwise waits for the next pass over the files still to download.

A folder or file whose name the destination filesystem refuses is created once more under its rejected-name
conversion, which is then recorded as its local name.
"""

import asyncio
import hashlib
import os
import stat
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from yandex_disk_rescue import api
from yandex_disk_rescue.destination import FATAL_ERRNOS, NAME_ERRNOS, Destination, DestinationError, PartialFile
from yandex_disk_rescue.local_names import WORK_DIR_NAME, collision_key, map_local_names, rejected_name_conversion
from yandex_disk_rescue.manifest import (
    DownloadRecord,
    LocalFingerprint,
    Mirror,
    MirrorEntry,
    RemoteIdentity,
    UnencodableEntry,
    VerifiedBy,
)
from yandex_disk_rescue.scanning import display_path, listed
from yandex_disk_rescue.units import format_size

PASS_PAUSE_SECONDS = 5.0
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
        self, client: api.DiskClient, content: api.ContentClient, *, retries: int, passes: int
    ) -> MirrorReport:
        """Walk the entries, then resume the files deferred by a transient failure in later passes, at most `passes`
        in all (0: until none is left), each after a pause of `PASS_PAUSE_SECONDS` or the longest wait the download
        host asked for. `retries` bounds the immediate retries of each file in each pass."""
        report = MirrorReport(remote_root=self._mirror.remote_root, destination=str(self._destination.path))
        blocked: dict[PurePosixPath, str] = {}
        deferred: list[tuple[MirrorEntry, FileDeferred]] = []
        for listed_entry in self._entries:
            local = self._names[listed_entry.entry_id]
            entry = replace(listed_entry, local_path=local)
            if local.name != entry.resource.path.rpartition('/')[2]:
                report.renamed.append((entry.resource.path, local))
            if reason := _blocked_by(local, blocked):
                report.failed.append(Failure(entry.resource.path, reason))
            elif entry.resource.kind == 'dir':
                self._make_folder(entry, local, blocked, report)
            elif entry.entry_id in self._current:
                report.skipped += 1
                _note_verified(report, entry, _recorded_method(entry))
            elif (later := await self._download(entry, client, content, report, retries)) is not None:
                deferred.append((entry, later))
        if deferred:
            await self._later_passes(deferred, client, content, report, retries=retries, passes=passes)
        report.replaced_folder = self._destination.replaced_folder
        report.failed += [Failure(display_path(e.path), _unencodable_reason(e)) for e in self._mirror.unencodable()]
        report.failed.sort(key=lambda failure: failure.path)
        report.renamed.sort()
        return report

    def _make_folder(
        self, entry: MirrorEntry, local: PurePosixPath, blocked: dict[PurePosixPath, str], report: MirrorReport
    ) -> None:
        try:
            with _local_errors(self._destination, local, 'write'):
                made, local = _create(entry, local, self._destination.make_folder, self._names, self._destination)
                if local != entry.local_path:
                    report.renamed.append((entry.resource.path, local))
                if made:
                    return
                self._destination.move_aside(local)
                report.moved_aside.append(local)
                if self._destination.make_folder(local):
                    return
            reason = f'{local} was created again as something other than a folder while it was being moved aside'
        except FileFailed as failed:
            reason = failed.reason
        blocked[local] = reason
        report.failed.append(Failure(entry.resource.path, reason))

    async def _later_passes(
        self,
        deferred: list[tuple[MirrorEntry, FileDeferred]],
        client: api.DiskClient,
        content: api.ContentClient,
        report: MirrorReport,
        *,
        retries: int,
        passes: int,
    ) -> None:
        used = 1
        while deferred and (passes == 0 or used < passes):
            waits = [failure.retry_after for _, failure in deferred if failure.retry_after is not None]
            await asyncio.sleep(max([PASS_PAUSE_SECONDS, *waits]))
            used += 1
            remaining = []
            for entry, _ in deferred:
                identity = RemoteIdentity.of(entry.resource)
                resumed = replace(entry, download=DownloadRecord('partial', identity, verified_by=None, local=None))
                if (later := await self._download(resumed, client, content, report, retries)) is not None:
                    remaining.append((entry, later))
            deferred = remaining
        suffix = f' (after {used} passes)' if used > 1 else ''
        report.failed += [Failure(entry.resource.path, failure.reason + suffix) for entry, failure in deferred]
        report.passes = (used, passes)

    async def _download(
        self,
        entry: MirrorEntry,
        client: api.DiskClient,
        content: api.ContentClient,
        report: MirrorReport,
        retries: int,
    ) -> FileDeferred | None:
        """Mirror one file into `report`; the failure when it is deferred to a later pass instead."""
        try:
            outcome = await download_file(
                entry,
                client=client,
                content=content,
                mirror=self._mirror,
                destination=self._destination,
                names=self._names,
                retries=retries,
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
    mirror: Mirror,
    destination: Destination,
    names: LocalNames,
    retries: int,
) -> FileOutcome:
    """Make the file `entry` verified at its local path and record it; the only state it touches is the mirror's
    records, the destination and `names`.

    A file already at the local path is adopted when its content matches the cloud. Otherwise the file is
    downloaded and verified first, and only then is whatever stands at the local path moved aside and the new file
    placed. A path that is the same file as another entry's verified copy is never adopted, moved or replaced. A name
    the destination refuses is converted, and the file placed under the converted name.

    A partial file whose `partial` record is for the entry's identity is resumed after `client.resource` confirms
    the cloud file still has it; any other partial file is discarded. A transfer that fails after new data is
    resumed at once, up to `retries` times.

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
            if (verified_by := _adopt(entry, local, identity, existing, mirror, destination)) is not None:
                return FileOutcome(local, verified_by, adopted=True, moved_aside=False)
        verified_by = await _fetch_verified(
            entry, identity, client=client, content=content, mirror=mirror, destination=destination, retries=retries
        )

        def place(path: PurePosixPath) -> tuple[os.stat_result, bool]:
            moved_aside = _clear_the_way(entry, path, mirror, destination)
            try:
                return destination.place(entry.entry_id, path), moved_aside
            except FileExistsError:
                _discard(entry, mirror, destination)
                raise FileFailed(f'something else was created at {path} while the file was being placed') from None

        (placed, moved_aside), local = _create(entry, local, place, names, destination)
        mirror.record_verified(entry.entry_id, identity, verified_by, placed)
    return FileOutcome(local, verified_by, adopted=False, moved_aside=moved_aside)


def _create[T](
    entry: MirrorEntry,
    local: PurePosixPath,
    create: Callable[[PurePosixPath], T],
    names: LocalNames,
    destination: Destination,
) -> tuple[T, PurePosixPath]:
    """Run `create(local)`, the call creating the entry's last component; when the destination refuses that name,
    record the converted name, then run it once more with that name and return it with the result.

    The name is recorded first, so a run killed once the entry is created under it finds it there. Raises
    `FileFailed`, with the original name recorded again, when the converted name is refused too.
    """
    try:
        return create(local), local
    except OSError as exc:
        if exc.errno not in NAME_ERRNOS:
            raise
        refused = exc.strerror
    converted = names.conversion(entry.entry_id)
    names.rename(entry.entry_id, converted)
    with _local_errors(destination, converted, 'write'):
        try:
            return create(converted), converted
        except OSError as exc:
            if exc.errno not in NAME_ERRNOS:
                raise
            names.rename(entry.entry_id, local)
            raise FileFailed(
                f'the destination filesystem refuses the name {local.name} ({refused}), and its converted form '
                f'{converted.name} too'
            ) from None


def _adopt(
    entry: MirrorEntry,
    local: PurePosixPath,
    identity: RemoteIdentity,
    existing: os.stat_result,
    mirror: Mirror,
    destination: Destination,
) -> VerifiedBy | None:
    """Hash the regular file `existing` at `local` and, when it matches `identity`, give it the cloud mtime and
    record it as verified; `None` when it is not such a file or does not match."""
    if not stat.S_ISREG(existing.st_mode) or existing.st_size != identity.size:
        return None
    digest = Digest()
    with _local_errors(destination, local, 'read'), destination.existing_file(local) as file:
        opened = file.stat()
        if not stat.S_ISREG(opened.st_mode) or opened.st_ino != existing.st_ino:
            return None
        for chunk in file.chunks():
            digest.update(digest.size, chunk)
        if digest.mismatch(identity) is not None:
            return None
        with _local_errors(destination, local, 'set the modification time of'):
            file.set_mtime(_mtime_ns(identity.modified))
        adopted = file.stat()
    verified_by = _method(identity)
    mirror.record_verified(entry.entry_id, identity, verified_by, adopted)
    destination.remove_partial(entry.entry_id)
    return verified_by


def _clear_the_way(entry: MirrorEntry, local: PurePosixPath, mirror: Mirror, destination: Destination) -> bool:
    """Move whatever stands at `local` aside so the verified partial file can be placed; `True` when something was
    moved."""
    existing = destination.lstat(local)
    if existing is None:
        return False
    if reason := _alias(entry, local, existing, mirror, destination):
        _discard(entry, mirror, destination)
        raise FileFailed(reason)
    destination.move_aside(local)
    return True


async def _fetch_verified(
    entry: MirrorEntry,
    identity: RemoteIdentity,
    *,
    client: api.DiskClient,
    content: api.ContentClient,
    mirror: Mirror,
    destination: Destination,
    retries: int,
) -> VerifiedBy:
    """Fetch the file into its partial file while hashing it, keeping the bytes already there when they can be
    resumed, verify it, and give it the cloud mtime, durably.

    A partial file that fails verification is deleted and its record dropped; one cut short by a transfer error is
    kept.
    """
    with destination.partial(entry.entry_id) as partial:
        start = await _resume_point(entry, identity, partial, client=client, mirror=mirror, destination=destination)
        partial.truncate(start)
        mirror.record_partial(entry.entry_id, identity)
        digest = Digest()
        for chunk in partial.chunks(start):
            digest.update(digest.size, chunk)
        if start == 0 or start < identity.size:
            digest = await _transfer(
                entry.resource.path, identity, partial, digest, client=client, content=content, retries=retries
            )
        try:
            verified_by = digest.verify(identity)
        except FileFailed:
            _discard(entry, mirror, destination)
            raise
        partial.finish(_mtime_ns(identity.modified))
    return verified_by


async def _resume_point(
    entry: MirrorEntry,
    identity: RemoteIdentity,
    partial: PartialFile,
    *,
    client: api.DiskClient,
    mirror: Mirror,
    destination: Destination,
) -> int:
    """How many bytes of the partial file to keep: all of them when its record is for `identity`, it holds no more
    than the file, and the cloud file still has that identity; otherwise none.

    Raises `FileFailed`, with the partial file and its record dropped, when the cloud file changed or is gone. That
    check runs for an empty partial file too: an earlier attempt that received nothing usable may have met the
    changed file.
    """
    held = partial.size()
    record = entry.download
    if held > identity.size or record is None or record.state != 'partial' or record.identity != identity:
        return 0
    try:
        live = await client.resource(entry.resource.path)
    except api.RemotePathNotFoundError:
        _discard(entry, mirror, destination)
        raise FileFailed('removed from the Disk after the scan') from None
    if live.kind != 'file' or RemoteIdentity.of(live) != identity:
        _discard(entry, mirror, destination)
        raise FileFailed(_CHANGED)
    return held


async def _transfer(
    path: str,
    identity: RemoteIdentity,
    partial: PartialFile,
    digest: Digest,
    *,
    client: api.DiskClient,
    content: api.ContentClient,
    retries: int,
) -> Digest:
    """Fetch the rest of the file after the `digest.size` bytes already in the partial file, and return the digest
    of the whole file.

    A failure after new data is resumed at once from where it stopped, up to `retries` times. An expired link is
    refreshed once without spending a retry. A host ignoring the range restarts the file from byte 0.

    Raises `FileDeferred` for a transient failure that received no new data or spent the retries, and `FileFailed`
    for any other failure.
    """

    # `digest` is looked up at call time, so the fresh digest of a restart from byte 0 takes over.
    def write(offset: int, chunk: bytes) -> None:
        digest.update(offset, chunk)
        partial.write(offset, chunk)

    href = await _download_link(client, path)
    refreshed = False
    attempt = 1
    while True:
        before = digest.size
        try:
            await content.fetch(href, start=before, end=identity.size, size=identity.size, write=write)
        except api.LinkExpiredError as exc:
            if refreshed:
                raise FileFailed(f'download link rejected again after a refresh (HTTP {exc.status})') from None
            refreshed = True
            href = await _download_link(client, path)
        except api.RangeNotHonouredError as exc:
            if before == 0:
                raise FileFailed(exc.reason) from None
            partial.truncate(0)
            digest = Digest()
        except api.TransientTransferError as exc:
            if digest.size == identity.size:
                return digest
            if digest.size == before or attempt > retries:
                reason = exc.reason if attempt == 1 else f'{exc.reason} (after {attempt} attempts)'
                raise FileDeferred(reason, retry_after=exc.retry_after) from None
            attempt += 1
        except api.TransferError as exc:
            raise FileFailed(exc.reason) from None
        else:
            return digest


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
