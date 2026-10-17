"""The manifest: a SQLite database of the remote tree as of the last completed scan of each folder, and of the local
mirrors made from it.

A scan streams its rows into a staging table through a `ScanSession`; only `ScanSession.commit()` moves them into
`entries`, in one transaction. A scan that stops in any other way (an exception, Ctrl+C, SIGKILL, a power cut)
leaves `entries` as the previous completed scan wrote it. The commit also records when that scan completed, and
the entries it left out because their names are not valid Unicode (`unencodable_entries`), so a run that reuses the
scan still knows about them.

A `Mirror` is one destination directory holding one remote folder of one account. Per mirror, the manifest keeps
the local name of every entry (`local_names`), each file's download state (`downloads`), with the checkpoint journal
of a partial download (`download_segments`), and the reason the last turn of an entry failed (`failures`), all keyed
by `entries.id`.
A rescan never rewrites them: a record keeps the remote identity it was made for, and only an entry's removal from
the manifest deletes its rows.

The database runs in WAL mode with `synchronous=FULL`: a commit is atomic and durable across a crash or power cut,
and each `ScanSession.add` costs one WAL append and fsync instead of the rollback journal's several. WAL needs a
local filesystem, which the XDG state directory is.

The schema version is `PRAGMA user_version`; `_MIGRATIONS[n]` upgrades version `n` to `n + 1` in one transaction.
Schema changes append a migration; released migrations are never edited.
"""

import fcntl
import os
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Literal, Self

from yandex_disk_rescue.api import Resource, normalize_remote_path
from yandex_disk_rescue.errors import RescueError

# The `PRAGMA application_id` of a manifest ('YDRS'), telling it apart from any other SQLite file.
APPLICATION_ID = 0x59445253

_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        f'PRAGMA application_id = {APPLICATION_ID}',
        # `id` is a stable handle for tables that hang per-file state off an entry; a rescan updates rows in
        # place, so `id` and any columns a later migration adds survive while the file's path stays the same.
        """
        CREATE TABLE entries (
            id INTEGER PRIMARY KEY,
            account_uid TEXT NOT NULL,
            path TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('file', 'dir')),
            size INTEGER CHECK (size >= 0),
            md5 TEXT,
            sha256 TEXT,
            modified TEXT NOT NULL,
            UNIQUE (account_uid, path)
        )
        """,
        """
        CREATE TABLE scan_sessions (
            id INTEGER PRIMARY KEY,
            account_uid TEXT NOT NULL,
            root TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE staged_entries (
            session_id INTEGER NOT NULL REFERENCES scan_sessions (id) ON DELETE CASCADE,
            path TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('file', 'dir')),
            size INTEGER CHECK (size >= 0),
            md5 TEXT,
            sha256 TEXT,
            modified TEXT NOT NULL,
            PRIMARY KEY (session_id, path)
        )
        """,
    ),
    (
        # `destination` is the resolved absolute DEST in its OS bytes (`os.fsencode`), so any Linux path fits.
        """
        CREATE TABLE mirrors (
            id INTEGER PRIMARY KEY,
            account_uid TEXT NOT NULL,
            remote_root TEXT NOT NULL,
            destination BLOB NOT NULL UNIQUE
        )
        """,
        """
        CREATE TABLE local_names (
            mirror_id INTEGER NOT NULL REFERENCES mirrors (id) ON DELETE CASCADE,
            entry_id INTEGER NOT NULL REFERENCES entries (id) ON DELETE CASCADE,
            local_path TEXT NOT NULL,
            PRIMARY KEY (mirror_id, entry_id),
            UNIQUE (mirror_id, local_path)
        )
        """,
        # The identity columns are the remote file the record was made for; `local_*` is the `lstat` fingerprint of
        # the verified file at its final path.
        """
        CREATE TABLE downloads (
            mirror_id INTEGER NOT NULL REFERENCES mirrors (id) ON DELETE CASCADE,
            entry_id INTEGER NOT NULL REFERENCES entries (id) ON DELETE CASCADE,
            state TEXT NOT NULL CHECK (state IN ('partial', 'verified')),
            size INTEGER NOT NULL CHECK (size >= 0),
            md5 TEXT,
            sha256 TEXT,
            modified TEXT NOT NULL,
            verified_by TEXT CHECK (verified_by IN ('md5+sha256', 'md5', 'sha256', 'size')),
            local_size INTEGER,
            local_mtime_ns INTEGER,
            local_ino INTEGER,
            PRIMARY KEY (mirror_id, entry_id),
            CHECK (
                (state = 'partial' AND verified_by IS NULL AND local_size IS NULL
                    AND local_mtime_ns IS NULL AND local_ino IS NULL)
                OR (state = 'verified' AND verified_by IS NOT NULL AND local_size IS NOT NULL
                    AND local_mtime_ns IS NOT NULL AND local_ino IS NOT NULL)
            )
        )
        """,
        """
        CREATE UNIQUE INDEX downloads_verified_ino ON downloads (mirror_id, local_ino) WHERE state = 'verified'
        """,
        """
        CREATE TABLE scans (
            account_uid TEXT NOT NULL,
            root TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            PRIMARY KEY (account_uid, root)
        )
        """,
    ),
    (
        # `path` is the UTF-8 encoding with lone surrogates encoded as themselves ('surrogatepass'): such a path is
        # not valid Unicode, so it cannot be TEXT.
        """
        CREATE TABLE unencodable_entries (
            account_uid TEXT NOT NULL,
            path BLOB NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('file', 'dir')),
            PRIMARY KEY (account_uid, path)
        )
        """,
    ),
    (
        # The checkpoint journal of a partial download: bytes [start, confirmed) of each segment were fsynced before
        # the row recording them was committed, and `digest` is their SHA-256 hex.
        """
        CREATE TABLE download_segments (
            mirror_id INTEGER NOT NULL,
            entry_id INTEGER NOT NULL,
            start INTEGER NOT NULL,
            end INTEGER NOT NULL,
            confirmed INTEGER NOT NULL,
            digest TEXT,
            PRIMARY KEY (mirror_id, entry_id, start),
            FOREIGN KEY (mirror_id, entry_id) REFERENCES downloads (mirror_id, entry_id) ON DELETE CASCADE,
            CHECK (0 <= start AND start < end AND start <= confirmed AND confirmed <= end),
            CHECK ((confirmed = start) = (digest IS NULL))
        )
        """,
        # The partial files of earlier versions were never confirmed: each restarts from byte 0.
        """
        INSERT INTO download_segments (mirror_id, entry_id, start, end, confirmed, digest)
        SELECT mirror_id, entry_id, 0, size, 0, NULL FROM downloads WHERE state = 'partial' AND size > 0
        """,
    ),
    (
        # `reason` is the user-facing reason the entry's last turn in this mirror failed.
        """
        CREATE TABLE failures (
            mirror_id INTEGER NOT NULL REFERENCES mirrors (id) ON DELETE CASCADE,
            entry_id INTEGER NOT NULL REFERENCES entries (id) ON DELETE CASCADE,
            reason TEXT NOT NULL,
            PRIMARY KEY (mirror_id, entry_id)
        )
        """,
    ),
)

SCHEMA_VERSION = len(_MIGRATIONS)

_COLUMNS = 'path, kind, size, md5, sha256, modified'
type _Row = tuple[str, Literal['file', 'dir'], int | None, str | None, str | None, str]


class ManifestError(RescueError):
    """The manifest cannot be opened or updated; the message names the file and what to do."""


class MirrorMismatchError(RescueError):
    """The destination already mirrors another remote folder or another account."""

    exit_code = 2


type VerifiedBy = Literal['md5+sha256', 'md5', 'sha256', 'size']


@dataclass(frozen=True)
class RemoteIdentity:
    """The remote content a download record refers to; a record whose identity differs from its entry's is stale."""

    size: int
    md5: str | None
    sha256: str | None
    modified: datetime

    @classmethod
    def of(cls, resource: Resource) -> Self:
        if resource.kind != 'file' or resource.size is None:
            raise ValueError(f'{resource.path!r} is not a file with a size')
        return cls(size=resource.size, md5=resource.md5, sha256=resource.sha256, modified=resource.modified)


@dataclass(frozen=True)
class LocalFingerprint:
    """The `lstat` fields that identify a placed file: equal fingerprints mean the file was not touched."""

    size: int
    mtime_ns: int
    ino: int

    @classmethod
    def of(cls, local: os.stat_result) -> Self:
        return cls(size=local.st_size, mtime_ns=local.st_mtime_ns, ino=local.st_ino)


@dataclass(frozen=True)
class Segment:
    """One byte range `[start, end)` of a partial download and how much of it is confirmed."""

    start: int
    end: int
    confirmed: int  # bytes [start, confirmed) were fsynced before this was recorded
    digest: str | None  # SHA-256 hex of [start, confirmed); None exactly when confirmed == start


@dataclass(frozen=True)
class DownloadRecord:
    """A file's download state in one mirror: `verified_by` and `local` are set exactly when it is verified.

    `segments` is the checkpoint journal: a partial record's segments tile `[0, size)` in order (none for a zero-byte
    file), and a verified record has none.
    """

    state: Literal['partial', 'verified']
    identity: RemoteIdentity
    verified_by: VerifiedBy | None
    local: LocalFingerprint | None
    segments: tuple[Segment, ...]


@dataclass(frozen=True)
class MirrorEntry:
    entry_id: int
    resource: Resource
    local_path: PurePosixPath | None  # relative to the destination; `None` until a name is assigned
    download: DownloadRecord | None
    failure: str | None  # why the entry's last turn in this mirror failed; `None` once it succeeded


@dataclass(frozen=True)
class UnencodableEntry:
    """An entry a scan left out because its path is not valid Unicode (it holds a lone surrogate)."""

    path: str
    kind: Literal['file', 'dir']


@dataclass(frozen=True)
class ScanTime:
    root: str
    completed_at: datetime


@dataclass(frozen=True)
class ScanDiff:
    added: list[str]  # empty on a first scan, where every staged row is new
    changed: list[str]
    removed: list[str]
    first_scan: bool  # the subtree had no committed entries before this scan


class Manifest:
    """An open manifest holding the run lock; use `Manifest.open` as a context manager."""

    def __init__(self, path: Path, connection: sqlite3.Connection, lock_fd: int) -> None:
        self.path = path
        self._connection = connection
        self._lock_fd = lock_fd
        self._active_session: ScanSession | None = None

    @classmethod
    def open(cls, path: Path) -> Manifest:
        """Open (creating if needed) the manifest at `path`, take the run lock and apply pending migrations.

        Missing parent directories are created with mode 0700, and a new database file with mode 0600. Raises
        `ManifestError` when another process holds the lock, the file is not a manifest, or it was written by a
        newer version of the tool.
        """
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            lock_fd = os.open(_lock_path(path), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        except OSError as exc:
            raise ManifestError(f'Could not create the manifest at {path}: {exc.strerror}.') from None
        try:
            _acquire_run_lock(lock_fd, path)
            connection = _connect(path)
        except BaseException:
            os.close(lock_fd)
            raise
        return cls(path, connection, lock_fd)

    def close(self) -> None:
        self._connection.close()
        os.close(self._lock_fd)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()

    def begin_scan(self, account_uid: str, root: str) -> ScanSession:
        """Start a scan of `root` (a normalized `disk:/` path), discarding the rows of any scan never committed."""
        _check_root(root)
        if self._active_session is not None:
            raise RuntimeError('a scan session is already active on this manifest')
        with self._transaction('start a scan'):
            self._connection.execute('DELETE FROM staged_entries')
            self._connection.execute('DELETE FROM scan_sessions')
            (session_id,) = self._connection.execute(
                'INSERT INTO scan_sessions (account_uid, root) VALUES (?, ?) RETURNING id', (account_uid, root)
            ).fetchone()
        self._active_session = ScanSession(self, session_id, account_uid, root)
        return self._active_session

    def mirror(self, account_uid: str, remote_root: str, destination: Path) -> Mirror:
        """Return the mirror of `remote_root` (as the API spells it) at `destination` (resolved, absolute), creating
        it on first use.

        Raises `MirrorMismatchError` when `destination` already mirrors another folder or another account.
        """
        _check_root(remote_root)
        if not destination.is_absolute():
            raise ValueError(f'mirror destination {destination} is not an absolute path')
        with self._transaction('update'):
            row = self._connection.execute(
                'SELECT id, account_uid, remote_root FROM mirrors WHERE destination = ?', (os.fsencode(destination),)
            ).fetchone()
            if row is None:
                (mirror_id,) = self._connection.execute(
                    'INSERT INTO mirrors (account_uid, remote_root, destination) VALUES (?, ?, ?) RETURNING id',
                    (account_uid, remote_root, os.fsencode(destination)),
                ).fetchone()
            else:
                mirror_id, mirrored_uid, mirrored_root = row
                if mirrored_uid != account_uid:
                    raise MirrorMismatchError(
                        f'{destination} already mirrors {mirrored_root} of another account; choose another destination.'
                    )
                if mirrored_root != remote_root:
                    raise MirrorMismatchError(
                        f'{destination} already mirrors {mirrored_root}; download {mirrored_root} there or choose '
                        'another destination.'
                    )
        return Mirror(self, mirror_id, account_uid, remote_root, destination)

    def last_scan(self, account_uid: str, root: str) -> ScanTime | None:
        """The newest completed scan of `account_uid` covering `root`: a scan of `root` itself or of an ancestor."""
        _check_root(root)
        covering = _ancestors(root)
        placeholders = ', '.join('?' * len(covering))
        with self._errors('read'):
            row = self._connection.execute(
                f'SELECT root, completed_at FROM scans WHERE account_uid = ? AND root IN ({placeholders}) '
                'ORDER BY completed_at DESC, length(root) DESC LIMIT 1',
                (account_uid, *covering),
            ).fetchone()
        return None if row is None else ScanTime(root=row[0], completed_at=datetime.fromisoformat(row[1]))

    def entries(self, account_uid: str, root: str) -> Iterator[Resource]:
        """Yield the committed entries of `account_uid` at `root` and below ordered by path, reading as it goes.

        Finish (or close) the iteration before writing to this manifest: the open read sees the database as it was
        when the iteration started.
        """
        _check_root(root)
        with self._errors('read'):
            cursor = self._connection.execute(
                f'SELECT {_COLUMNS} FROM entries e WHERE e.account_uid = :account AND {_SUBTREE} ORDER BY path',
                _subtree_params(account_uid, root),
            )
            for row in cursor:
                yield _resource(row)

    @contextmanager
    def _errors(self, action: str) -> Iterator[None]:
        try:
            yield
        except sqlite3.Error as exc:
            raise ManifestError(f'Could not {action} the manifest at {self.path}: {exc}.') from None

    @contextmanager
    def _transaction(self, action: str) -> Iterator[None]:
        with self._errors(action), _transaction(self._connection):
            yield

    def _end_session(self, session: ScanSession) -> None:
        if self._active_session is session:
            self._active_session = None


class ScanSession:
    """One scan in progress: `add` stages rows durably, `commit` makes them the manifest's view of the subtree."""

    def __init__(self, manifest: Manifest, session_id: int, account_uid: str, root: str) -> None:
        self._manifest = manifest
        self._id = session_id
        self._root = root
        self._params = {'session': session_id, **_subtree_params(account_uid, root)}
        self._committed = False

    def add(self, resources: Iterable[Resource]) -> None:
        """Stage `resources`, all at `root` or below; a path added twice keeps the last record."""
        self._check_open()
        rows = [(self._id, *_row(resource, self._root)) for resource in resources]
        with self._manifest._transaction('update'):
            self._manifest._connection.executemany(
                f'INSERT OR REPLACE INTO staged_entries (session_id, {_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)', rows
            )

    def discard(self, subtree_root: str) -> None:
        """Unstage every row at `subtree_root` or below, a normalized path within the scan root."""
        self._check_open()
        _check_root(subtree_root)
        if not _in_subtree(subtree_root, self._root):
            raise ValueError(f'{subtree_root!r} is outside the scan root {self._root!r}')
        with self._manifest._transaction('update'):
            self._manifest._connection.execute(
                f'DELETE FROM staged_entries AS e WHERE e.session_id = :session AND {_SUBTREE}',
                {'session': self._id, **_subtree_bounds(subtree_root)},
            )

    def staged(self) -> Iterator[Resource]:
        """Yield the staged rows ordered by path, reading them as the iteration goes."""
        self._check_open()
        with self._manifest._errors('read'):
            cursor = self._manifest._connection.execute(
                f'SELECT {_COLUMNS} FROM staged_entries WHERE session_id = ? ORDER BY path', (self._id,)
            )
            for row in cursor:
                yield _resource(row)

    def commit(self, *, unencodable: Iterable[Resource]) -> ScanDiff:
        """Replace the committed subtree of `root` with the staged rows in one transaction and return the diff.

        `unencodable` are the entries below `root` the scan left out because their paths are not valid Unicode; they
        replace the ones the subtree's previous scans recorded.
        """
        self._check_open()
        left_out = [(self._params['account'], *_unencodable_row(resource, self._root)) for resource in unencodable]
        connection = self._manifest._connection
        with self._manifest._transaction('update'):
            (had_entries,) = connection.execute(_HAD_ENTRIES_SQL, self._params).fetchone()
            diff = ScanDiff(
                added=self._paths(_ADDED_SQL) if had_entries else [],
                changed=self._paths(_CHANGED_SQL),
                removed=self._paths(_REMOVED_SQL),
                first_scan=not had_entries,
            )
            connection.execute(_DELETE_REMOVED_SQL, self._params)
            connection.execute(_UPSERT_SQL, self._params)
            connection.execute(_RECORD_SCAN_SQL, {**self._params, 'completed_at': _timestamp(datetime.now(UTC))})
            connection.execute(_DELETE_UNENCODABLE_SQL, {'account': self._params['account'], **_blob_below(self._root)})
            connection.executemany(
                'INSERT INTO unencodable_entries (account_uid, path, kind) VALUES (?, ?, ?)', left_out
            )
            connection.execute('DELETE FROM staged_entries WHERE session_id = ?', (self._id,))
            connection.execute('DELETE FROM scan_sessions WHERE id = ?', (self._id,))
        self._committed = True
        self._manifest._end_session(self)
        return diff

    def _paths(self, sql: str) -> list[str]:
        return [path for (path,) in self._manifest._connection.execute(sql, self._params)]

    def _check_open(self) -> None:
        if self._committed:
            raise RuntimeError('this scan session is already committed')


class Mirror:
    """One destination directory mirroring `remote_root` of one account; obtain it with `Manifest.mirror`.

    Every write is one transaction. Local paths are relative to `destination` and '/'-separated.
    """

    def __init__(
        self, manifest: Manifest, mirror_id: int, account_uid: str, remote_root: str, destination: Path
    ) -> None:
        self._manifest = manifest
        self.id = mirror_id
        self.account_uid = account_uid
        self.remote_root = remote_root
        self.destination = destination

    def entries(self) -> list[MirrorEntry]:
        """The committed entries below `remote_root` (not the root itself) ordered by path, with their local names,
        download records and failures, read in full so no read stays open across later writes."""
        params = {'mirror': self.id, **_subtree_params(self.account_uid, self.remote_root)}
        connection = self._manifest._connection
        with self._manifest._transaction('read'):
            rows = connection.execute(_MIRROR_ENTRIES_SQL, params).fetchall()
            segments: defaultdict[int, list[Segment]] = defaultdict(list)
            for entry_id, *segment in connection.execute(_MIRROR_SEGMENTS_SQL, {'mirror': self.id}):
                segments[entry_id].append(Segment(*segment))
        return [_mirror_entry(row, tuple(segments[row[0]])) for row in rows]

    def download(self, entry_id: int) -> DownloadRecord | None:
        """The entry's download record with its segments as the manifest holds it now; `None` when it has none."""
        connection = self._manifest._connection
        with self._manifest._transaction('read'):
            row = connection.execute(_DOWNLOAD_SQL, {'mirror': self.id, 'entry': entry_id}).fetchone()
            segments = self._segments(entry_id)
        return None if row is None else _download_record(row, segments)

    def unencodable(self) -> list[UnencodableEntry]:
        """The entries below `remote_root` the last scans covering them left out for their names, ordered by path."""
        params = {'account': self.account_uid, **_blob_below(self.remote_root)}
        with self._manifest._errors('read'):
            rows = self._manifest._connection.execute(_UNENCODABLE_SQL, params).fetchall()
        return [UnencodableEntry(path=path.decode('utf-8', 'surrogatepass'), kind=kind) for path, kind in rows]

    def assign_names(self, names: Mapping[int, PurePosixPath]) -> None:
        """Set the local path of each entry id in `names`, replacing any it had; a path already taken by another
        entry of this mirror raises `ManifestError` and leaves every name unchanged."""
        rows = [(self.id, entry_id, _local_path_text(path)) for entry_id, path in names.items()]
        connection = self._manifest._connection
        with self._manifest._transaction('update'):
            # Deleting first lets a batch swap names between its entries.
            connection.executemany(
                'DELETE FROM local_names WHERE mirror_id = ? AND entry_id = ?', [row[:2] for row in rows]
            )
            connection.executemany('INSERT INTO local_names (mirror_id, entry_id, local_path) VALUES (?, ?, ?)', rows)
            outside = connection.execute(
                _NAMED_OUTSIDE_SQL, {'mirror': self.id, **_subtree_params(self.account_uid, self.remote_root)}
            ).fetchone()
            if outside is not None:
                raise ValueError(
                    f'entry {outside[0]} is not a committed entry below {self.remote_root} of this account'
                )

    def rename_local(self, entry_id: int, name: str) -> None:
        """Replace the last component of the entry's local path with `name`, and move the local paths of everything
        below it along; a resulting path already taken raises `ManifestError` and changes nothing."""
        if name in {'', '.', '..'} or '/' in name:
            raise ValueError(f'{name!r} is not a single path component')
        connection = self._manifest._connection
        with self._manifest._transaction('update'):
            row = connection.execute(
                'SELECT local_path FROM local_names WHERE mirror_id = ? AND entry_id = ?', (self.id, entry_id)
            ).fetchone()
            if row is None:
                raise ValueError(f'entry {entry_id} has no local name in mirror {self.id}')
            old = row[0]
            new = str(PurePosixPath(old).with_name(name))
            connection.execute(
                'UPDATE local_names SET local_path = ? WHERE mirror_id = ? AND entry_id = ?', (new, self.id, entry_id)
            )
            connection.execute(_RENAME_DESCENDANTS_SQL, {'mirror': self.id, 'old': old, 'new': new, **_below(old)})

    def record_partial(self, entry_id: int, identity: RemoteIdentity, segments: Sequence[Segment]) -> None:
        """Record that a download of `identity` into the entry's partial file has started with `segments` as its
        journal, replacing any record and its segments; raises `ValueError` unless `segments` tile `[0, size)` in
        order."""
        if not _tiles(segments, identity.size):
            raise ValueError(f'segments {list(segments)} do not tile [0, {identity.size}) in order')
        self._record(entry_id, identity, 'partial', None, None, segments)

    def checkpoint(self, entry_id: int, segments: Sequence[Segment]) -> None:
        """Record the confirmed offset and digest of each of `segments`, matched by `start`, in one transaction.

        Raises `ValueError`, writing nothing, when the entry has no partial record or `segments` are not exactly the
        recorded boundaries.
        """
        connection = self._manifest._connection
        params = [
            {'mirror': self.id, 'entry': entry_id, 'start': s.start, 'confirmed': s.confirmed, 'digest': s.digest}
            for s in segments
        ]
        with self._manifest._transaction('update'):
            row = connection.execute(_DOWNLOAD_SQL, {'mirror': self.id, 'entry': entry_id}).fetchone()
            if row is None or row[0] != 'partial':
                raise ValueError(f'entry {entry_id} has no partial download record in mirror {self.id}')
            recorded = [(s.start, s.end) for s in self._segments(entry_id)]
            if [(s.start, s.end) for s in segments] != recorded:
                raise ValueError(f'segments {list(segments)} are not the recorded segments {recorded}')
            connection.executemany(_CHECKPOINT_SQL, params)

    def record_verified(
        self, entry_id: int, identity: RemoteIdentity, verified_by: VerifiedBy, local: os.stat_result
    ) -> None:
        """Record the entry's file as verified against `identity`, placed at its final path with `lstat` `local`.

        The record's segments and the entry's failure are deleted. Any other verified record of this mirror holding the same inode is deleted:
        the caller has just placed this file there, so that record describes a file that is gone and whose inode
        number was reused.
        """
        self._record(entry_id, identity, 'verified', verified_by, LocalFingerprint.of(local), ())

    def verified_owner(self, ino: int) -> MirrorEntry | None:
        """The entry of this mirror whose verified record holds inode `ino`, if any."""
        with self._manifest._errors('read'):
            row = self._manifest._connection.execute(
                _VERIFIED_OWNER_SQL, {'mirror': self.id, 'ino': _signed_ino(ino)}
            ).fetchone()
        return None if row is None else _mirror_entry(row, ())

    def forget(self, entry_id: int) -> None:
        """Delete the entry's download record and its segments, if any; its failure stays."""
        with self._manifest._transaction('update'):
            self._manifest._connection.execute(
                'DELETE FROM downloads WHERE mirror_id = ? AND entry_id = ?', (self.id, entry_id)
            )

    def record_failure(self, entry_id: int, reason: str) -> None:
        """Record `reason` as why the entry's last turn failed, replacing any earlier reason."""
        with self._manifest._transaction('update'):
            self._check_entry(entry_id)
            self._manifest._connection.execute(_RECORD_FAILURE_SQL, (self.id, entry_id, reason))

    def clear_failure(self, entry_id: int) -> None:
        """Delete the entry's failure, if any."""
        with self._manifest._transaction('update'):
            self._manifest._connection.execute(
                'DELETE FROM failures WHERE mirror_id = ? AND entry_id = ?', (self.id, entry_id)
            )

    def _segments(self, entry_id: int) -> tuple[Segment, ...]:
        rows = self._manifest._connection.execute(
            'SELECT start, end, confirmed, digest FROM download_segments WHERE mirror_id = ? AND entry_id = ? '
            'ORDER BY start',
            (self.id, entry_id),
        )
        return tuple(Segment(*row) for row in rows)

    def _record(
        self,
        entry_id: int,
        identity: RemoteIdentity,
        state: Literal['partial', 'verified'],
        verified_by: VerifiedBy | None,
        local: LocalFingerprint | None,
        segments: Sequence[Segment],
    ) -> None:
        connection = self._manifest._connection
        params = {
            'mirror': self.id,
            'entry': entry_id,
            'state': state,
            'size': identity.size,
            'md5': identity.md5,
            'sha256': identity.sha256,
            'modified': _timestamp(identity.modified),
            'verified_by': verified_by,
            'local_size': None if local is None else local.size,
            'local_mtime_ns': None if local is None else local.mtime_ns,
            'local_ino': None if local is None else _signed_ino(local.ino),
        }
        rows = [
            {
                'mirror': self.id,
                'entry': entry_id,
                'start': s.start,
                'end': s.end,
                'confirmed': s.confirmed,
                'digest': s.digest,
            }
            for s in segments
        ]
        with self._manifest._transaction('update'):
            self._check_entry(entry_id)
            if state == 'verified':
                connection.execute(_RELEASE_INO_SQL, params)
                connection.execute('DELETE FROM failures WHERE mirror_id = ? AND entry_id = ?', (self.id, entry_id))
            # The upsert updates the row in place, so no cascade deletes the old segments.
            connection.execute(
                'DELETE FROM download_segments WHERE mirror_id = ? AND entry_id = ?', (self.id, entry_id)
            )
            connection.execute(_RECORD_DOWNLOAD_SQL, params)
            connection.executemany(_INSERT_SEGMENT_SQL, rows)

    def _check_entry(self, entry_id: int) -> None:
        params = {'entry': entry_id, **_subtree_params(self.account_uid, self.remote_root)}
        (found,) = self._manifest._connection.execute(_ENTRY_IN_MIRROR_SQL, params).fetchone()
        if not found:
            raise ValueError(f'entry {entry_id} is not a committed entry below {self.remote_root} of this account')


_MIRROR_ENTRY_COLUMNS = """
    e.id, e.path, e.kind, e.size, e.md5, e.sha256, e.modified, n.local_path,
    d.state, d.size, d.md5, d.sha256, d.modified, d.verified_by, d.local_size, d.local_mtime_ns, d.local_ino,
    f.reason
"""
_MIRROR_JOINS = """
    FROM entries e
    LEFT JOIN local_names n ON n.mirror_id = :mirror AND n.entry_id = e.id
    LEFT JOIN downloads d ON d.mirror_id = :mirror AND d.entry_id = e.id
    LEFT JOIN failures f ON f.mirror_id = :mirror AND f.entry_id = e.id
"""
_MIRROR_ENTRIES_SQL = f"""
    SELECT {_MIRROR_ENTRY_COLUMNS} {_MIRROR_JOINS}
    WHERE e.account_uid = :account AND e.path >= :low AND e.path < :high
    ORDER BY e.path
"""
_MIRROR_SEGMENTS_SQL = """
    SELECT entry_id, start, end, confirmed, digest FROM download_segments WHERE mirror_id = :mirror
    ORDER BY entry_id, start
"""
_DOWNLOAD_SQL = """
    SELECT state, size, md5, sha256, modified, verified_by, local_size, local_mtime_ns, local_ino
    FROM downloads WHERE mirror_id = :mirror AND entry_id = :entry
"""
_INSERT_SEGMENT_SQL = """
    INSERT INTO download_segments (mirror_id, entry_id, start, end, confirmed, digest)
    VALUES (:mirror, :entry, :start, :end, :confirmed, :digest)
"""
_CHECKPOINT_SQL = """
    UPDATE download_segments SET confirmed = :confirmed, digest = :digest
    WHERE mirror_id = :mirror AND entry_id = :entry AND start = :start
"""
_VERIFIED_OWNER_SQL = f"""
    SELECT {_MIRROR_ENTRY_COLUMNS} {_MIRROR_JOINS}
    WHERE d.state = 'verified' AND d.local_ino = :ino
"""
_ENTRY_IN_MIRROR_SQL = """
    SELECT EXISTS (SELECT 1 FROM entries e WHERE e.id = :entry AND e.account_uid = :account
                   AND e.path >= :low AND e.path < :high)
"""
_NAMED_OUTSIDE_SQL = """
    SELECT n.entry_id FROM local_names n JOIN entries e ON e.id = n.entry_id
    WHERE n.mirror_id = :mirror AND NOT (e.account_uid = :account AND e.path >= :low AND e.path < :high)
    LIMIT 1
"""
_RELEASE_INO_SQL = """
    DELETE FROM downloads
    WHERE mirror_id = :mirror AND state = 'verified' AND local_ino = :local_ino AND entry_id != :entry
"""
_RECORD_DOWNLOAD_SQL = """
    INSERT INTO downloads (
        mirror_id, entry_id, state, size, md5, sha256, modified, verified_by, local_size, local_mtime_ns, local_ino
    ) VALUES (
        :mirror, :entry, :state, :size, :md5, :sha256, :modified, :verified_by, :local_size, :local_mtime_ns, :local_ino
    )
    ON CONFLICT (mirror_id, entry_id) DO UPDATE SET
        state = excluded.state, size = excluded.size, md5 = excluded.md5, sha256 = excluded.sha256,
        modified = excluded.modified, verified_by = excluded.verified_by, local_size = excluded.local_size,
        local_mtime_ns = excluded.local_mtime_ns, local_ino = excluded.local_ino
"""
_RECORD_FAILURE_SQL = """
    INSERT INTO failures (mirror_id, entry_id, reason) VALUES (?, ?, ?)
    ON CONFLICT (mirror_id, entry_id) DO UPDATE SET reason = excluded.reason
"""
_UNENCODABLE_SQL = """
    SELECT path, kind FROM unencodable_entries
    WHERE account_uid = :account AND path >= :low AND path < :high
    ORDER BY path
"""
_DELETE_UNENCODABLE_SQL = """
    DELETE FROM unencodable_entries WHERE account_uid = :account AND path >= :low AND path < :high
"""
_RENAME_DESCENDANTS_SQL = """
    UPDATE local_names SET local_path = :new || substr(local_path, length(:old) + 1)
    WHERE mirror_id = :mirror AND local_path >= :low AND local_path < :high
"""

# `e.path` is `root` itself, or sorts within [low, high): every path starting with `root/` under binary collation.
_SUBTREE = '(e.path = :root OR (e.path >= :low AND e.path < :high))'

_HAD_ENTRIES_SQL = f'SELECT EXISTS (SELECT 1 FROM entries e WHERE e.account_uid = :account AND {_SUBTREE})'

_ADDED_SQL = """
    SELECT s.path FROM staged_entries s
    WHERE s.session_id = :session
      AND NOT EXISTS (SELECT 1 FROM entries e WHERE e.account_uid = :account AND e.path = s.path)
    ORDER BY s.path
"""
_CHANGED_SQL = """
    SELECT s.path FROM staged_entries s JOIN entries e ON e.account_uid = :account AND e.path = s.path
    WHERE s.session_id = :session
      AND (s.kind, s.size, s.md5, s.sha256, s.modified) IS NOT (e.kind, e.size, e.md5, e.sha256, e.modified)
    ORDER BY s.path
"""
_REMOVED_SQL = f"""
    SELECT e.path FROM entries e
    WHERE e.account_uid = :account AND {_SUBTREE}
      AND NOT EXISTS (SELECT 1 FROM staged_entries s WHERE s.session_id = :session AND s.path = e.path)
    ORDER BY e.path
"""
_DELETE_REMOVED_SQL = f"""
    DELETE FROM entries AS e
    WHERE e.account_uid = :account AND {_SUBTREE}
      AND NOT EXISTS (SELECT 1 FROM staged_entries s WHERE s.session_id = :session AND s.path = e.path)
"""
_RECORD_SCAN_SQL = """
    INSERT INTO scans (account_uid, root, completed_at) VALUES (:account, :root, :completed_at)
    ON CONFLICT (account_uid, root) DO UPDATE SET completed_at = excluded.completed_at
"""
# The `WHERE true` resolves SQLite's parsing ambiguity between an upsert clause and a join after `INSERT ... SELECT`.
_UPSERT_SQL = f"""
    INSERT INTO entries (account_uid, {_COLUMNS})
    SELECT :account, {_COLUMNS} FROM staged_entries WHERE session_id = :session AND true
    ON CONFLICT (account_uid, path) DO UPDATE SET
        kind = excluded.kind, size = excluded.size, md5 = excluded.md5,
        sha256 = excluded.sha256, modified = excluded.modified
"""


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + '.lock')


def _acquire_run_lock(lock_fd: int, path: Path) -> None:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise ManifestError(
            f'Another yandex-disk-rescue run is using {path}; wait for it to finish and try again.'
        ) from None


def _connect(path: Path) -> sqlite3.Connection:
    try:
        # Created here so the database (and the WAL files SQLite gives the same mode) are private from the start.
        os.close(os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600))
        connection = sqlite3.connect(path, autocommit=True)
    except (OSError, sqlite3.Error) as exc:
        raise ManifestError(f'Could not open the manifest at {path}: {_reason(exc)}.') from None
    try:
        _configure(connection, path)
        _migrate(connection, path)
    except BaseException:
        connection.close()
        raise
    return connection


def _configure(connection: sqlite3.Connection, path: Path) -> None:
    try:
        journal_mode = connection.execute('PRAGMA journal_mode = WAL').fetchone()[0]
        connection.execute('PRAGMA synchronous = FULL')
        connection.execute('PRAGMA foreign_keys = ON')
    except sqlite3.DatabaseError as exc:
        raise _unreadable(path, exc) from None
    if journal_mode != 'wal':
        raise ManifestError(
            f'Could not open the manifest at {path}: its filesystem does not support SQLite WAL mode '
            f'(journal mode {journal_mode!r}). Set XDG_STATE_HOME to a directory on a local filesystem.'
        )


def _migrate(connection: sqlite3.Connection, path: Path) -> None:
    try:
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        application_id = connection.execute('PRAGMA application_id').fetchone()[0]
        has_tables = connection.execute('SELECT EXISTS (SELECT 1 FROM sqlite_schema)').fetchone()[0]
    except sqlite3.DatabaseError as exc:
        raise _unreadable(path, exc) from None
    if (version == 0 and has_tables) or (version > 0 and application_id != APPLICATION_ID):
        raise _not_a_manifest(path, 'it is a SQLite database of another application')
    if version > SCHEMA_VERSION:
        raise ManifestError(
            f'The manifest at {path} has schema version {version}, newer than the version {SCHEMA_VERSION} this '
            'yandex-disk-rescue supports. Upgrade yandex-disk-rescue, or move the file away to start a new manifest.'
        )
    for target, statements in enumerate(_MIGRATIONS[version:], start=version + 1):
        try:
            with _transaction(connection):
                for statement in statements:
                    connection.execute(statement)
                connection.execute(f'PRAGMA user_version = {target}')
        except sqlite3.Error as exc:
            raise ManifestError(
                f'Could not upgrade the manifest at {path} to schema version {target}: {exc}.'
            ) from None


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute('BEGIN IMMEDIATE')
    try:
        yield
    except BaseException:
        # SQLite rolls back by itself on some errors (SQLITE_FULL, SQLITE_IOERR); a second ROLLBACK would fail.
        if connection.in_transaction:
            connection.execute('ROLLBACK')
        raise
    connection.execute('COMMIT')


def _unreadable(path: Path, exc: sqlite3.DatabaseError) -> ManifestError:
    if exc.sqlite_errorname == 'SQLITE_NOTADB':
        return _not_a_manifest(path, 'it is not a SQLite database')
    return ManifestError(f'Could not open the manifest at {path}: {exc}.')


def _not_a_manifest(path: Path, reason: str) -> ManifestError:
    return ManifestError(
        f'{path} is not a yandex-disk-rescue manifest: {reason}. Move it away so a new manifest can be created.'
    )


def _reason(exc: OSError | sqlite3.Error) -> str:
    return exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)


def _check_root(root: str) -> None:
    if normalize_remote_path(root) != root:
        raise ValueError(f'scan root {root!r} is not a normalized disk:/ path')


def _subtree_params(account_uid: str, root: str) -> dict[str, str]:
    return {'account': account_uid, **_subtree_bounds(root)}


def _subtree_bounds(root: str) -> dict[str, str]:
    return {'root': root, **_below(root)}


def _below(path: str) -> dict[str, str]:
    prefix = _prefix(path)
    # '0' is the character right after '/', so [prefix, high) holds exactly the strings starting with prefix.
    return {'low': prefix, 'high': prefix[:-1] + '0'}


def _blob_below(path: str) -> dict[str, bytes]:
    """`_below` as UTF-8 bounds for the BLOB paths of `unencodable_entries`; UTF-8 byte order is code point order."""
    return {bound: text.encode('utf-8') for bound, text in _below(path).items()}


def _ancestors(root: str) -> list[str]:
    """`root` and every folder above it, up to `disk:/`."""
    parts = root.removeprefix('disk:/').split('/') if root != 'disk:/' else []
    return ['disk:/', *('disk:/' + '/'.join(parts[:depth]) for depth in range(1, len(parts) + 1))]


def _prefix(root: str) -> str:
    return root if root.endswith('/') else root + '/'


def _in_subtree(path: str, root: str) -> bool:
    return path == root or path.startswith(_prefix(root))


def _resource(row: _Row) -> Resource:
    path, kind, size, md5, sha256, modified = row
    return Resource(path=path, kind=kind, size=size, md5=md5, sha256=sha256, modified=datetime.fromisoformat(modified))


def _row(resource: Resource, root: str) -> _Row:
    if not _in_subtree(resource.path, root):
        raise ValueError(f'{resource.path!r} is outside the scan root {root!r}')
    if resource.modified.tzinfo is None:
        raise ValueError(f'{resource.path!r} has a modification time without a UTC offset')
    modified = resource.modified.astimezone(UTC).isoformat()
    return resource.path, resource.kind, resource.size, resource.md5, resource.sha256, modified


def _unencodable_row(resource: Resource, root: str) -> tuple[bytes, Literal['file', 'dir']]:
    if not resource.path.startswith(_prefix(root)):
        raise ValueError(f'{resource.path!r} is not below the scan root {root!r}')
    return resource.path.encode('utf-8', 'surrogatepass'), resource.kind


def _local_path_text(path: PurePosixPath) -> str:
    if path.is_absolute() or path.parts in {(), ('.',)} or '..' in path.parts:
        raise ValueError(f'local path {str(path)!r} is not a relative path inside the destination')
    return str(path)


def _signed_ino(ino: int) -> int:
    # SQLite integers are signed 64-bit; inode numbers are unsigned 64-bit, so the top half is stored negative.
    return ino - 2**64 if ino >= 2**63 else ino


def _timestamp(moment: datetime) -> str:
    # A fixed-width UTC form, so text order is time order.
    return moment.astimezone(UTC).isoformat(timespec='microseconds')


def _mirror_entry(row: tuple, segments: tuple[Segment, ...]) -> MirrorEntry:
    """Build a `MirrorEntry` from a row of `_MIRROR_ENTRY_COLUMNS` and the segments of its download record."""
    entry_id, local_path = row[0], row[7]
    return MirrorEntry(
        entry_id=entry_id,
        resource=_resource(row[1:7]),
        local_path=None if local_path is None else PurePosixPath(local_path),
        download=None if row[8] is None else _download_record(row[8:17], segments),
        failure=row[17],
    )


def _download_record(row: tuple, segments: tuple[Segment, ...]) -> DownloadRecord:
    """Build a `DownloadRecord` from the `downloads` columns state, size, md5, sha256, modified, verified_by,
    local_size, local_mtime_ns and local_ino."""
    state, size, md5, sha256, modified, verified_by, local_size, local_mtime_ns, local_ino = row
    identity = RemoteIdentity(size=size, md5=md5, sha256=sha256, modified=datetime.fromisoformat(modified))
    local = None
    if state == 'verified':
        local = LocalFingerprint(size=local_size, mtime_ns=local_mtime_ns, ino=local_ino % 2**64)
    return DownloadRecord(state=state, identity=identity, verified_by=verified_by, local=local, segments=segments)


def _tiles(segments: Sequence[Segment], size: int) -> bool:
    """Whether `segments` cover `[0, size)` in order, each non-empty and each starting where the last one ended."""
    edge = 0
    for segment in segments:
        if segment.start != edge or segment.end <= segment.start:
            return False
        edge = segment.end
    return edge == size
