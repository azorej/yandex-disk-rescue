"""The manifest: a SQLite database of the remote tree as of the last completed scan of each folder.

A scan streams its rows into a staging table through a `ScanSession`; only `ScanSession.commit()` moves them into
`entries`, in one transaction. A scan that stops in any other way (an exception, Ctrl+C, SIGKILL, a power cut)
leaves `entries` as the previous completed scan wrote it.

The database runs in WAL mode with `synchronous=FULL`: a commit is atomic and durable across a crash or power cut,
and each `ScanSession.add` costs one WAL append and fsync instead of the rollback journal's several. WAL needs a
local filesystem, which the XDG state directory is.

The schema version is `PRAGMA user_version`; `_MIGRATIONS[n]` upgrades version `n` to `n + 1` in one transaction.
Schema changes append a migration; released migrations are never edited.
"""

import fcntl
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
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
)

SCHEMA_VERSION = len(_MIGRATIONS)

_COLUMNS = 'path, kind, size, md5, sha256, modified'
type _Row = tuple[str, Literal['file', 'dir'], int | None, str | None, str | None, str]


class ManifestError(RescueError):
    """The manifest cannot be opened or updated; the message names the file and what to do."""


@dataclass(frozen=True)
class ScanDiff:
    added: list[str]
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

    def entries(self, account_uid: str, root: str) -> Iterator[Resource]:
        """Return the committed entries of `account_uid` at `root` and below, ordered by path."""
        _check_root(root)
        with self._errors('read'):
            rows = self._connection.execute(
                f'SELECT {_COLUMNS} FROM entries e WHERE e.account_uid = :account AND {_SUBTREE} ORDER BY path',
                _subtree_params(account_uid, root),
            ).fetchall()
        return iter(_resource(row) for row in rows)

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

    def commit(self) -> ScanDiff:
        """Replace the committed subtree of `root` with the staged rows in one transaction and return the diff."""
        self._check_open()
        connection = self._manifest._connection
        with self._manifest._transaction('update'):
            (had_entries,) = connection.execute(_HAD_ENTRIES_SQL, self._params).fetchone()
            diff = ScanDiff(
                added=self._paths(_ADDED_SQL),
                changed=self._paths(_CHANGED_SQL),
                removed=self._paths(_REMOVED_SQL),
                first_scan=not had_entries,
            )
            connection.execute(_DELETE_REMOVED_SQL, self._params)
            connection.execute(_UPSERT_SQL, self._params)
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
    prefix = _prefix(root)
    # '0' is the character right after '/', so [prefix, high) holds exactly the strings starting with prefix.
    return {'root': root, 'low': prefix, 'high': prefix[:-1] + '0'}


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
