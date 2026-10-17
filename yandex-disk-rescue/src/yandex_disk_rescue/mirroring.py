"""The mirror engine: local names, the walk over a mirror's entries, the per-file download pipeline, and the lines
summarizing a run.

A file is verified before it reaches its final path: the bytes go to a partial file while their MD5 and SHA-256
are computed, the size and every checksum the cloud provides are checked, and only then is the file moved into
place without replacing anything. A verified file whose record and `lstat` fingerprint still match is skipped
without being read.
"""

import errno
import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from yandex_disk_rescue import api
from yandex_disk_rescue.destination import FATAL_ERRNOS, Destination, DestinationError
from yandex_disk_rescue.local_names import map_local_names
from yandex_disk_rescue.manifest import LocalFingerprint, Mirror, MirrorEntry, RemoteIdentity, VerifiedBy
from yandex_disk_rescue.scanning import display_path, listed
from yandex_disk_rescue.units import format_size

_NAME_ERRNOS = frozenset({errno.EINVAL, errno.EILSEQ, errno.ENAMETOOLONG})
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class FileFailed(Exception):
    """One file (or folder) could not be mirrored; `reason` is the user-facing text. The run goes on."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
    size_only: list[str] = field(default_factory=list)
    renamed: list[tuple[str, PurePosixPath]] = field(default_factory=list)
    failed: list[Failure] = field(default_factory=list)


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

    def verify(self, identity: RemoteIdentity) -> VerifiedBy:
        """Check the size, then the MD5 and the SHA-256 the cloud provides; raises `FileFailed` on a mismatch."""
        if self.size != identity.size:
            raise FileFailed(f'size mismatch (expected {identity.size} bytes, got {self.size})')
        for name, expected, actual in (
            ('MD5', identity.md5, self._md5.hexdigest()),
            ('SHA-256', identity.sha256, self._sha256.hexdigest()),
        ):
            if expected is not None and expected.lower() != actual:
                raise FileFailed(f'{name} mismatch (expected {expected}, got {actual})')
        match (identity.md5 is not None, identity.sha256 is not None):
            case (True, True):
                return 'md5+sha256'
            case (True, False):
                return 'md5'
            case (False, True):
                return 'sha256'
        return 'size'


class MirrorRun:
    """One run over a mirror: names are assigned, stray partial files deleted and the files still to download
    counted on construction; `run` then walks the entries in path order.

    `unencodable` are the entries the scan left out because their names are not valid Unicode; each one is a
    failure of the run.
    """

    def __init__(self, mirror: Mirror, destination: Destination, *, unencodable: tuple[api.Resource, ...]) -> None:
        self._mirror = mirror
        self._destination = destination
        self._unencodable = unencodable
        self._entries = _assign_local_names(mirror)
        with _fatal_errors(destination.path):
            destination.keep_partials(
                [e.entry_id for e in self._entries if e.download is not None and e.download.state == 'partial']
            )
            self._current = {e.entry_id for e in self._entries if _is_current(e, destination)}
        pending = [e for e in self._entries if e.resource.kind == 'file' and e.entry_id not in self._current]
        self.pending_files = len(pending)
        self.pending_size = sum(e.resource.size or 0 for e in pending)

    async def run(self, client: api.DiskClient, content: api.ContentClient) -> MirrorReport:
        report = MirrorReport(remote_root=self._mirror.remote_root, destination=str(self._destination.path))
        blocked: dict[PurePosixPath, str] = {}
        for entry in self._entries:
            local = _local_path(entry)
            if local.name != entry.resource.path.rpartition('/')[2]:
                report.renamed.append((entry.resource.path, local))
            if reason := _blocked_by(local, blocked):
                report.failed.append(Failure(entry.resource.path, reason))
            elif entry.resource.kind == 'dir':
                self._make_folder(entry, local, blocked, report)
            elif entry.entry_id in self._current:
                report.skipped += 1
                _note_verified(report, entry, _recorded_method(entry))
            else:
                await self._download(entry, client, content, report)
        report.failed += [Failure(display_path(r.path), _unencodable_reason(r)) for r in self._unencodable]
        report.failed.sort(key=lambda failure: failure.path)
        return report

    def _make_folder(
        self, entry: MirrorEntry, local: PurePosixPath, blocked: dict[PurePosixPath, str], report: MirrorReport
    ) -> None:
        try:
            with _local_errors(self._destination, local):
                if self._destination.make_folder(local):
                    return
            reason = f'{local} already exists and is not a folder written by this mirror; it was left untouched'
        except FileFailed as failed:
            reason = failed.reason
        blocked[local] = reason
        report.failed.append(Failure(entry.resource.path, reason))

    async def _download(
        self, entry: MirrorEntry, client: api.DiskClient, content: api.ContentClient, report: MirrorReport
    ) -> None:
        try:
            verified_by = await download_file(
                entry, client=client, content=content, mirror=self._mirror, destination=self._destination
            )
        except FileFailed as failed:
            report.failed.append(Failure(entry.resource.path, failed.reason))
            return
        report.downloaded += 1
        report.downloaded_size += entry.resource.size or 0
        _note_verified(report, entry, verified_by)


async def download_file(
    entry: MirrorEntry,
    *,
    client: api.DiskClient,
    content: api.ContentClient,
    mirror: Mirror,
    destination: Destination,
) -> VerifiedBy:
    """Download the file `entry` to its local path and record it as verified; the only state it touches is the
    mirror's records and the destination.

    Raises `FileFailed` when this file cannot be mirrored, `DestinationError` when the destination cannot be
    written at all, and the API errors that stop the run (`TokenRejectedError`, `DiskApiError`).
    """
    local = _local_path(entry)
    identity = RemoteIdentity.of(entry.resource)
    with _local_errors(destination, local):
        existing = destination.lstat(local)
        if existing is not None:
            raise FileFailed(_in_the_way(entry, local, existing, mirror, destination))
        mirror.record_partial(entry.entry_id, identity)
        href = await _download_link(client, entry.resource.path)
        verified_by = await _fetch_verified(
            entry, identity, href, content=content, mirror=mirror, destination=destination
        )
        try:
            placed = destination.place(entry.entry_id, local)
        except FileExistsError:
            existing = destination.lstat(local)
            _discard(entry, mirror, destination)
            if existing is None:
                raise
            raise FileFailed(_in_the_way(entry, local, existing, mirror, destination)) from None
        mirror.record_verified(entry.entry_id, identity, verified_by, placed)
    return verified_by


async def _fetch_verified(
    entry: MirrorEntry,
    identity: RemoteIdentity,
    href: str,
    *,
    content: api.ContentClient,
    mirror: Mirror,
    destination: Destination,
) -> VerifiedBy:
    """Fetch the file into its partial file while hashing it, verify it, and give it the cloud mtime, durably.

    A partial file that fails verification is deleted and its record dropped; one cut short by a transfer error is
    kept.
    """
    digest = Digest()
    with destination.partial(entry.entry_id) as partial:

        def write(offset: int, chunk: bytes) -> None:
            digest.update(offset, chunk)
            partial.write(offset, chunk)

        try:
            await content.fetch(href, start=0, end=identity.size, size=identity.size, write=write)
        except api.TransferError as exc:
            raise FileFailed(exc.reason) from None
        try:
            verified_by = digest.verify(identity)
        except FileFailed:
            _discard(entry, mirror, destination)
            raise
        partial.finish(_mtime_ns(identity.modified))
    return verified_by


def mirror_summary(report: MirrorReport) -> list[str]:
    """The lines reporting a run: the counts, size-only files, renamed entries and failures with their reasons."""
    lines = [
        f'Mirrored {report.remote_root} to {report.destination}: {report.downloaded} downloaded '
        f'({format_size(report.downloaded_size)}), {report.skipped} skipped, {len(report.failed)} failed'
    ]
    if report.size_only:
        lines.append(f'Verified by size only (no cloud checksum): {len(report.size_only)}')
        lines += listed('', report.size_only, len(report.size_only), '')
    if report.renamed:
        lines.append(f'Renamed for the destination: {len(report.renamed)}')
        shown = [f'{remote} -> {local}' for remote, local in report.renamed]
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
    """Whether the entry is a file verified for its current identity whose placed copy has the recorded `lstat`."""
    record = entry.download
    if entry.resource.kind != 'file' or record is None or record.state != 'verified':
        return False
    if record.identity != RemoteIdentity.of(entry.resource):
        return False
    placed = destination.lstat(_local_path(entry))
    return placed is not None and stat.S_ISREG(placed.st_mode) and LocalFingerprint.of(placed) == record.local


def _in_the_way(
    entry: MirrorEntry, local: PurePosixPath, existing: os.stat_result, mirror: Mirror, destination: Destination
) -> str:
    ino = existing.st_ino
    record = entry.download
    if record is not None and record.local is not None and record.local.ino == ino:
        return f'{local} is a copy verified earlier that no longer matches the Disk; it was left untouched'
    owner = mirror.verified_owner(ino)
    if owner is not None and owner.entry_id != entry.entry_id and owner.local_path is not None:
        owner_copy = destination.lstat(owner.local_path)
        if owner_copy is not None and owner_copy.st_ino == ino:
            return f'{local} is the same file as {owner.resource.path} on this filesystem; it was left untouched'
    return f'{local} already exists and was not written by this mirror; it was left untouched'


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
def _local_errors(destination: Destination, local: PurePosixPath) -> Iterator[None]:
    """Turn an `OSError` about `local` into a failure of that entry, or of the run when no entry could escape it."""
    try:
        yield
    except OSError as exc:
        if exc.errno in FATAL_ERRNOS:
            raise DestinationError.of(destination.path / local, exc) from None
        if exc.errno in _NAME_ERRNOS:
            raise FileFailed(f'the destination filesystem refuses the name {local.name} ({exc.strerror})') from None
        raise FileFailed(f'could not write {local}: {exc.strerror}') from None


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


def _recorded_method(entry: MirrorEntry) -> VerifiedBy:
    if entry.download is None or entry.download.verified_by is None:
        raise ValueError(f'{entry.resource.path} has no verified record')
    return entry.download.verified_by


def _unencodable_reason(resource: api.Resource) -> str:
    if resource.kind == 'dir':
        return 'the folder name is not valid Unicode, so the Disk API cannot list it or download anything in it'
    return 'the name is not valid Unicode, so the Disk API cannot download it'


def _local_path(entry: MirrorEntry) -> PurePosixPath:
    if entry.local_path is None:
        raise ValueError(f'{entry.resource.path} has no local name')
    return entry.local_path


def _mtime_ns(modified: datetime) -> int:
    return (modified - _EPOCH) // timedelta(microseconds=1) * 1000
