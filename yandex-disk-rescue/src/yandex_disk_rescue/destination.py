"""The destination folder of a download: its checks, the run lock and work directory, the folder partial files live
in, and every file operation inside them; and the read-only view of a destination that `verify` uses.

Partial files live in the destination's own `.yandex-disk-rescue/partial/`, or, with a partial folder given, in the
destination's own folder below it. In the first layout a finished partial file is itself placed or quarantined; in
the second it is first copied to a staged copy in the destination's `partial/`, which is then placed or quarantined.
The partial folder an earlier run used, either layout, opens to read and delete the partial files left there. The
free space of the partial folder's filesystem and the bytes each partial file allocates there can be looked up.

All operations go through open folder descriptors and `localfs`, so none follows a symbolic link inside the
destination; the destination itself may be a link and is resolved once. Final paths only ever receive a finished
file through the no-replace rename, and whatever stood there is first moved, never deleted, into the run's folder
under `.yandex-disk-rescue/replaced/`. A downloaded file that fails verification is moved, never deleted, into the
run's folder under `.yandex-disk-rescue/quarantine/`.
"""

import errno
import fcntl
import hashlib
import os
import re
import stat
from collections.abc import Collection, Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Literal, Self

from yandex_disk_rescue import localfs
from yandex_disk_rescue.errors import RescueError
from yandex_disk_rescue.local_names import WORK_DIR_NAME, rejected_name_conversion

LOCK_FILE_NAME = 'lock'
PARTIAL_DIR_NAME = 'partial'
REPLACED_DIR_NAME = 'replaced'
QUARANTINE_DIR_NAME = 'quarantine'
_STAMP_FORMAT = '%Y-%m-%dT%H-%M-%SZ'
_READ_SIZE = 1 << 20
_PROBE_NAMES = ('probe', 'probe.renamed')
_COPY_SUFFIX = '.copy'
_COPY_NAME = re.compile(r'[0-9]+\.copy')
_KEY_DIGITS = 16
# Errors that no other file of the run would escape: the run stops instead of failing every file in turn.
FATAL_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS})
# A permission error in the work directory, unlike one on a path of the mirror, would meet every file too.
_WORK_FATAL_ERRNOS = FATAL_ERRNOS | {errno.EACCES, errno.EPERM}
# How a filesystem refuses a name it cannot hold, as exFAT refuses `?`.
NAME_ERRNOS = frozenset({errno.EINVAL, errno.EILSEQ, errno.ENAMETOOLONG})
# A path, or a folder on the way to it, that is missing or not a real folder.
MISSING_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})
# A read failing with one of these means the device is gone, whatever the destination's path still answers.
DEVICE_GONE_ERRNOS = frozenset({errno.ENODEV, errno.ENXIO, errno.ESTALE})


class DestinationUsageError(RescueError):
    """The destination or the partial folder cannot be used: not a folder, a missing parent, or a partial folder inside
    the destination."""

    exit_code = 2


type DiskAction = Literal['read', 'write', 'inspect']


class DestinationError(RescueError):
    """Reading, writing or inspecting the destination failed in a way that stops the run; `action` says which."""

    def __init__(self, action: DiskAction, path: Path | str, reason: str) -> None:
        super().__init__(f'Could not {action} {path}: {reason}.')
        self.action = action
        self.path = path

    @classmethod
    def of(cls, action: DiskAction, path: Path | str, exc: OSError) -> Self:
        return cls(action, path, exc.strerror)


def check_destination(raw: str) -> Path:
    """Return `raw` as an absolute path after checking it can be used, without creating anything.

    It must be an existing folder (or a link to one), or a missing path whose parent folder exists.
    """
    path = Path(os.path.abspath(raw))
    if path.is_dir():
        return path
    if os.path.lexists(path):
        raise DestinationUsageError(f'Destination {raw} exists and is not a folder.')
    if not path.parent.is_dir():
        raise DestinationUsageError(
            f'The parent folder of {raw} does not exist; download creates only the last folder of the destination.'
        )
    return path


def check_partial_dir(raw: str, destination: Path) -> Path | None:
    """The resolved partial folder `raw` names for `destination` (as `check_destination` returns it), or `None` for
    `''`, which keeps partial files in the destination; nothing is created.

    Raises `DestinationUsageError` when `raw` is not an existing folder (or a link to one), or when it is the
    destination or a folder below it.
    """
    if raw == '':
        return None
    path = Path(raw)
    if not path.is_dir():
        raise DestinationUsageError(
            f'The partial folder {raw} is not an existing folder; download creates only its own folder inside it.'
        )
    resolved = path.resolve(strict=True)
    inside = destination.resolve()
    if resolved == inside or inside in resolved.parents:
        raise DestinationUsageError(
            f'The partial folder {raw} is inside the destination {destination}; choose a folder outside it.'
        )
    return resolved


def partial_folder_key(destination: Path) -> str:
    """The name of the folder of the resolved `destination` below a partial folder: the first hex digits of the
    SHA-256 of its path."""
    return hashlib.sha256(os.fsencode(destination)).hexdigest()[:_KEY_DIGITS]


class _Tree:
    """The files below an open destination folder, looked up and opened through folder descriptors opened one
    component at a time without following a link."""

    def __init__(self, path: Path, root_fd: int) -> None:
        self.path = path
        self._root_fd = root_fd

    def lstat(self, local: PurePosixPath) -> os.stat_result | None:
        """The `lstat` of `local`, or `None` when it, or a folder on the way to it, is missing or not a real folder, or
        when the filesystem refuses its name (`NAME_ERRNOS`), so the call creating it meets the refusal."""
        try:
            with self._folder(local.parent) as parent_fd:
                return _lstat_name(local.name, parent_fd)
        except OSError as exc:
            if exc.errno in MISSING_ERRNOS:
                return None
            raise

    @contextmanager
    def existing_file(self, local: PurePosixPath) -> Iterator[ExistingFile]:
        """Open the file at `local` for reading without following a link; the caller checks what it opened."""
        with self._folder(local.parent) as parent_fd:
            fd = localfs.open_file(local.name, dir_fd=parent_fd)
        try:
            yield ExistingFile(fd)
        finally:
            os.close(fd)

    def reachable(self) -> bool:
        """Whether the destination itself can still be read: its path `lstat`ed afresh and its folder listed through a
        new descriptor, so a disk that is gone fails even where open descriptors still answer."""
        try:
            os.lstat(self.path)
            fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                with os.scandir(fd) as found:
                    next(found, None)
            finally:
                os.close(fd)
        except OSError:
            return False
        return True

    @contextmanager
    def _folder(self, local: PurePosixPath) -> Iterator[int]:
        """An open descriptor of the folder `local`, every component opened without following a link."""
        fd = self._root_fd
        try:
            for part in local.parts:
                parent_fd = fd
                fd = localfs.open_dir(part, dir_fd=parent_fd)
                if parent_fd != self._root_fd:
                    os.close(parent_fd)
            yield fd
        finally:
            if fd != self._root_fd:
                os.close(fd)


class ReadOnlyDestination(_Tree):
    """An existing destination opened for reading only; use `ReadOnlyDestination.open` as a context manager.

    It creates, modifies, moves and deletes nothing in the destination; reading a file may update its access time, as
    any read does, unless the destination is mounted `ro` or `noatime`. `path` is the resolved destination.
    """

    def __init__(self, path: Path, root_fd: int, lock_fd: int | None) -> None:
        super().__init__(path, root_fd)
        self._lock_fd = lock_fd

    @classmethod
    def open(cls, path: Path) -> ReadOnlyDestination:
        """Open the existing folder `path` (or a link to one) and, when its work directory holds a lock file, take the
        run lock on that file, opened read-only; with no work directory or no lock file no run can be using it.

        Raises `DestinationUsageError` when `path` is not an existing folder, `RescueError` when another run holds the
        lock, and `DestinationError` when the destination cannot be read.
        """
        if not path.is_dir():
            raise DestinationUsageError(f'Destination {path} is not an existing folder.')
        fds: list[int] = []
        try:
            resolved = path.resolve(strict=True)
            fds.append(os.open(resolved, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
            lock_fd = _read_lock(fds[0], resolved)
        except OSError as exc:
            _close_all(fds)
            raise DestinationError.of('read', path, exc) from None
        except BaseException:
            _close_all(fds)
            raise
        return cls(resolved, fds[0], lock_fd)

    def close(self) -> None:
        _close_all([self._root_fd] if self._lock_fd is None else [self._root_fd, self._lock_fd])

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()


class Destination(_Tree):
    """An open destination holding its run lock; use `Destination.open` as a context manager.

    `path` is the resolved destination. Local paths are relative to it, '/'-separated. Partial files live in
    `partial_folder`, named by entry id: `.yandex-disk-rescue/partial/`, or the destination's folder below the partial
    folder it was opened with (`separate_partials`). With separate partials, a finished partial file is copied to its
    staged copy `.yandex-disk-rescue/partial/<entry id>.copy` (`copy_target`), and that copy is what `place` and
    `quarantine` move. Things moved aside go to `.yandex-disk-rescue/replaced/<run start, UTC>/`, and quarantined
    files to `.yandex-disk-rescue/quarantine/<run start, UTC>/`, each created on its first move under a name no
    earlier run took; `replaced_folder` and `quarantine_folder` are those folders relative to the destination once
    they exist.

    An error in the work directory or the partial folder that every file would meet (a full disk, an I/O error, a
    read-only filesystem, a permission error) raises `DestinationError` with the action that failed: `read`, `write`
    or `inspect` (an `lstat`, `fstat` or `fstatvfs`); other errors are the `OSError` of the call.

    Every method blocks on the disk and may run in a worker thread. Moving aside and quarantining create this run's
    folders on first use, so no two of them may run at once.
    """

    def __init__(
        self,
        path: Path,
        root_fd: int,
        work_fd: int,
        lock_fd: int,
        partial_fd: int,
        *,
        separate: tuple[int, Path] | None,
        started: datetime,
    ) -> None:
        super().__init__(path, root_fd)
        self._work_fd = work_fd
        self._lock_fd = lock_fd
        self._partial_fd = partial_fd
        self._separate = separate
        self._started = started
        self._run_folders: dict[str, tuple[int, PurePosixPath]] = {}
        self._previous_fds: list[int] = []

    @classmethod
    def open(cls, path: Path, *, partial_dir: Path | None) -> Destination:
        """Create `path` if it is missing (its last component only), take the run lock in its work directory and
        check the filesystem supports the no-replace rename; with `partial_dir` (as `check_partial_dir` returns it),
        create the destination's own folder below it if it is missing, named by `partial_folder_key`.

        Raises `RescueError` when another run holds the lock and `DestinationError` when the destination or the
        folder below `partial_dir` cannot be written, or the destination lacks the no-replace rename.
        """
        started = datetime.now(UTC)
        fds: list[int] = []
        separate: tuple[int, Path] | None = None
        try:
            resolved = _create_last_component(path)
            fds.append(os.open(resolved, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
            fds.append(_work_dir(WORK_DIR_NAME, fds[0], resolved / WORK_DIR_NAME))
            fds.append(_lock(fds[1], resolved))
            fds.append(_work_dir(PARTIAL_DIR_NAME, fds[1], resolved / WORK_DIR_NAME / PARTIAL_DIR_NAME))
            _probe_noreplace(fds[3], resolved)
            if partial_dir is not None:
                separate = _separate_folder(partial_dir, partial_folder_key(resolved))
        except OSError as exc:
            _close_all(fds)
            raise DestinationError.of('write', path, exc) from None
        except BaseException:
            _close_all(fds)
            raise
        return cls(resolved, *fds, separate=separate, started=started)

    @property
    def partial_folder(self) -> Path:
        """The folder partial files live in."""
        return self._own_partials if self._separate is None else self._separate[1]

    @property
    def separate_partials(self) -> bool:
        """Whether partial files live below a partial folder of their own, not in the destination."""
        return self._separate is not None

    @property
    def replaced_folder(self) -> PurePosixPath | None:
        return self._run_folder(REPLACED_DIR_NAME)

    @property
    def quarantine_folder(self) -> PurePosixPath | None:
        return self._run_folder(QUARANTINE_DIR_NAME)

    def close(self) -> None:
        run_fds = [fd for fd, _ in self._run_folders.values()]
        separate_fds = [] if self._separate is None else [self._separate[0]]
        _close_all(
            [
                self._partial_fd,
                self._lock_fd,
                self._work_fd,
                self._root_fd,
                *run_fds,
                *separate_fds,
                *self._previous_fds,
            ]
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()

    def make_folder(self, local: PurePosixPath) -> bool:
        """Create the folder `local` unless it exists; `False` when something other than a real folder is there.

        Its parent must be a real folder.
        """
        with self._folder(local.parent) as parent_fd:
            try:
                localfs.make_dir(local.name, dir_fd=parent_fd)
            except FileExistsError:
                existing = localfs.lstat(local.name, dir_fd=parent_fd)
                return stat.S_ISDIR(existing.st_mode)
            os.fsync(parent_fd)
        return True

    @contextmanager
    def partial(self, entry_id: int) -> Iterator[PartialFile]:
        """Open the partial file of `entry_id` for reading and writing, keeping its content; a missing one is created
        empty."""
        with self._open_in_work_dir(str(entry_id), self._files_fd, self.partial_folder, os.O_RDWR) as partial:
            yield partial

    @contextmanager
    def copy_target(self, entry_id: int) -> Iterator[PartialFile]:
        """Open the staged copy of `entry_id`, `.yandex-disk-rescue/partial/<entry id>.copy`, for writing, emptied
        first and created when missing; only with separate partials."""
        if self._separate is None:
            raise ValueError('a staged copy exists only with separate partial files')
        name, folder = _copy_name(entry_id), self._own_partials
        with self._open_in_work_dir(name, self._partial_fd, folder, os.O_RDWR | os.O_TRUNC) as copy:
            yield copy

    def remove_partial(self, entry_id: int) -> None:
        """Delete the partial file of `entry_id`, and with separate partials its staged copy too; a missing one is no
        error. Any other error that is not `DestinationError` is an `OSError` whose `filename` is the full path of the
        file it could not delete."""
        names = [(str(entry_id), self._files_fd, self.partial_folder)]
        if self._separate is not None:
            names.append((_copy_name(entry_id), self._partial_fd, self._own_partials))
        for name, dir_fd, folder in names:
            path = folder / name
            with _work_errors('write', path):
                try:
                    os.unlink(name, dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise OSError(exc.errno, exc.strerror, str(path)) from None

    def sync_partial_folder(self) -> None:
        """Make the files created in and deleted from the partial folder durable."""
        with _work_errors('write', self.partial_folder):
            os.fsync(self._files_fd)

    def partial_free_bytes(self) -> int:
        """The free bytes of the filesystem `partial_folder` is on. Any error raises `DestinationError` naming the
        folder."""
        try:
            return localfs.free_bytes(self._files_fd)
        except OSError as exc:
            raise DestinationError.of('inspect', self.partial_folder, exc) from None

    def partial_allocated(self, entry_id: int) -> int:
        """The bytes the partial file of `entry_id` allocates on its filesystem, which a sparse file keeps below its
        size; 0 when there is no regular file of that name. Any other error raises `DestinationError` naming it."""
        name = str(entry_id)
        try:
            found = localfs.lstat(name, dir_fd=self._files_fd)
        except FileNotFoundError:
            return 0
        except OSError as exc:
            raise DestinationError.of('inspect', self.partial_folder / name, exc) from None
        return found.st_blocks * 512 if stat.S_ISREG(found.st_mode) else 0

    def previous_partials(self, folder: Path) -> PreviousPartials | None:
        """The separate partial folder `folder` an earlier run used (a `partial_folder` of a destination opened with a
        partial folder), opened to read and delete the partial files there; `None` when it is missing. Nothing is
        created, and the folder stays open until the destination is closed. Any other error raises `DestinationError`
        naming `folder`."""
        try:
            fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            if exc.errno in MISSING_ERRNOS:
                return None
            raise DestinationError.of('read', folder, exc) from None
        self._previous_fds.append(fd)
        return PreviousPartials(fd, folder, own=False)

    def own_partials(self) -> PreviousPartials:
        """The destination's own `.yandex-disk-rescue/partial/` as `previous_partials` opens a folder, for the partial
        files an earlier run kept there."""
        fd = os.dup(self._partial_fd)
        self._previous_fds.append(fd)
        return PreviousPartials(fd, self._own_partials, own=True)

    def is_partial_folder(self, previous: PreviousPartials) -> bool:
        """Whether `previous` is the folder partial files live in, opened under the same or another path. Any error
        raises `DestinationError` naming `previous`."""
        try:
            here, there = os.fstat(self._files_fd), previous.stat()
        except OSError as exc:
            raise DestinationError.of('inspect', previous.path, exc) from None
        return (here.st_dev, here.st_ino) == (there.st_dev, there.st_ino)

    def remove_staged_copies(self) -> None:
        """Delete every regular file named `<digits>.copy` in the destination's `.yandex-disk-rescue/partial/`: the
        staged copies a stopped run left there, whichever folder partial files live in now. Any error raises
        `DestinationError` naming the folder."""
        try:
            with os.scandir(self._partial_fd) as found:
                stray = [e.name for e in found if _COPY_NAME.fullmatch(e.name) and e.is_file(follow_symlinks=False)]
            for name in stray:
                os.unlink(name, dir_fd=self._partial_fd)
            os.fsync(self._partial_fd)
        except OSError as exc:
            raise DestinationError.of('write', self._own_partials, exc) from None

    def keep_partials(self, entry_ids: Collection[int]) -> None:
        """Delete every regular file in the partial folder that is not the partial file of one of `entry_ids`; anything
        else there is left alone. Any error raises `DestinationError` naming the folder."""
        keep = {str(entry_id) for entry_id in entry_ids}
        try:
            with os.scandir(self._files_fd) as found:
                stray = [e.name for e in found if e.name not in keep and e.is_file(follow_symlinks=False)]
            for name in stray:
                os.unlink(name, dir_fd=self._files_fd)
        except OSError as exc:
            raise DestinationError.of('write', self.partial_folder, exc) from None

    def place(self, entry_id: int, local: PurePosixPath) -> os.stat_result:
        """Move the finished partial file of `entry_id`, or with separate partials its staged copy, to `local` without
        replacing anything, make the move durable, and return the `lstat` of the placed file.

        Raises `FileExistsError` when anything is at `local`; the file then stays where it was.
        """
        source = self._placed_name(entry_id)
        with self._folder(local.parent) as parent_fd:
            localfs.rename_noreplace(source, local.name, src_dir_fd=self._partial_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
            return localfs.lstat(local.name, dir_fd=parent_fd)

    def move_aside(self, local: PurePosixPath) -> None:
        """Move whatever is at `local` (a file, a folder with everything in it, or a link itself, never its target)
        to the same relative path in this run's replaced folder, without replacing anything, and make the move
        durable."""
        with ExitStack() as stack:
            source_fd = stack.enter_context(self._folder(local.parent))
            with _work_errors('write', self.path / WORK_DIR_NAME / REPLACED_DIR_NAME):
                target_fd = stack.enter_context(_made_folder(self._run_folder_fd(REPLACED_DIR_NAME), local.parent))
            localfs.rename_noreplace(local.name, local.name, src_dir_fd=source_fd, dst_dir_fd=target_fd)
            os.fsync(target_fd)
            os.fsync(source_fd)

    def quarantine(self, entry_id: int, local: PurePosixPath) -> PurePosixPath:
        """Move the partial file of `entry_id`, or with separate partials its staged copy, to the relative path `local`
        in this run's quarantine folder, without replacing anything, make the move durable, and return the copy's path
        relative to the destination.

        When the filesystem refuses the file's name (`NAME_ERRNOS`), the copy gets its `rejected_name_conversion`.
        """
        with (
            _work_errors('write', self.path / WORK_DIR_NAME / QUARANTINE_DIR_NAME),
            _made_folder(self._run_folder_fd(QUARANTINE_DIR_NAME), local.parent) as target_fd,
        ):
            source, name = self._placed_name(entry_id), local.name
            try:
                localfs.rename_noreplace(source, name, src_dir_fd=self._partial_fd, dst_dir_fd=target_fd)
            except OSError as exc:
                if exc.errno not in NAME_ERRNOS:
                    raise
                name = rejected_name_conversion(name, ())
                localfs.rename_noreplace(source, name, src_dir_fd=self._partial_fd, dst_dir_fd=target_fd)
            os.fsync(target_fd)
            os.fsync(self._partial_fd)
        return self._run_folders[QUARANTINE_DIR_NAME][1] / local.parent / name

    @property
    def _own_partials(self) -> Path:
        return self.path / WORK_DIR_NAME / PARTIAL_DIR_NAME

    @property
    def _files_fd(self) -> int:
        """The descriptor of `partial_folder`."""
        return self._partial_fd if self._separate is None else self._separate[0]

    def _placed_name(self, entry_id: int) -> str:
        """The name, in the destination's `partial/`, of the file `place` and `quarantine` move for `entry_id`."""
        return str(entry_id) if self._separate is None else _copy_name(entry_id)

    @contextmanager
    def _open_in_work_dir(self, name: str, dir_fd: int, folder: Path, flags: int) -> Iterator[PartialFile]:
        path = folder / name
        with _work_errors('write', path):
            fd = os.open(name, flags | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o666, dir_fd=dir_fd)
        try:
            yield PartialFile(fd, path)
        finally:
            os.close(fd)

    def _run_folder(self, kind: str) -> PurePosixPath | None:
        return self._run_folders[kind][1] if kind in self._run_folders else None

    def _run_folder_fd(self, kind: str) -> int:
        """The descriptor of this run's folder in the work directory's `kind` folder, created on first use under a
        name no earlier run took."""
        if kind in self._run_folders:
            return self._run_folders[kind][0]
        stamp = self._started.strftime(_STAMP_FORMAT)
        with _made_folder(self._work_fd, PurePosixPath(kind)) as kind_fd:
            name, attempt = stamp, 1
            while True:
                try:
                    localfs.make_dir(name, dir_fd=kind_fd)
                except FileExistsError:
                    attempt += 1
                    name = f'{stamp}-{attempt}'
                else:
                    break
            os.fsync(kind_fd)
            run_fd = localfs.open_dir(name, dir_fd=kind_fd)
        self._run_folders[kind] = (run_fd, PurePosixPath(WORK_DIR_NAME, kind, name))
        return run_fd


class ExistingFile:
    """An open file found at a final path: its status, its content, and setting its modification time."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def stat(self) -> os.stat_result:
        return os.fstat(self._fd)

    def chunks(self) -> Iterator[bytes]:
        """The content in order, in chunks of at most 1 MiB."""
        offset = 0
        while chunk := localfs.read(self._fd, _READ_SIZE, offset):
            yield chunk
            offset += len(chunk)

    def set_mtime(self, mtime_ns: int) -> None:
        """Set the modification time, keeping the access time, and flush it to the disk."""
        localfs.set_times(self._fd, atime_ns=self.stat().st_atime_ns, mtime_ns=mtime_ns)
        os.fsync(self._fd)


class PartialFile:
    """An open partial file or staged copy at `path`: its size, reading back any range of what it holds, cutting or
    extending it (`truncate`), positional writes, fsyncs of the content written so far, then the final mtime and an
    fsync."""

    def __init__(self, fd: int, path: Path) -> None:
        self._fd = fd
        self._path = path

    def size(self) -> int:
        with _work_errors('inspect', self._path):
            return os.fstat(self._fd).st_size

    def chunks(self, start: int, end: int) -> Iterator[bytes]:
        """The bytes `[start, end)` in order, in chunks of at most 1 MiB; the file must hold at least `end` bytes."""
        offset = start
        while offset < end:
            with _work_errors('read', self._path):
                chunk = localfs.read(self._fd, min(_READ_SIZE, end - offset), offset)
            if not chunk:
                raise ValueError(f'the partial file ends at {offset} bytes, before {end}')
            yield chunk
            offset += len(chunk)

    def truncate(self, length: int) -> None:
        with _work_errors('write', self._path):
            os.ftruncate(self._fd, length)

    def write(self, offset: int, chunk: bytes) -> None:
        view = memoryview(chunk)
        with _work_errors('write', self._path):
            while view:
                written = os.pwrite(self._fd, view, offset)
                view = view[written:]
                offset += written

    def sync(self) -> None:
        """Flush the content written so far to the disk."""
        with _work_errors('write', self._path):
            os.fsync(self._fd)

    def finish(self, mtime_ns: int) -> None:
        """Set the modification time and flush the content and metadata to the disk."""
        with _work_errors('write', self._path):
            localfs.set_times(self._fd, atime_ns=mtime_ns, mtime_ns=mtime_ns)
            os.fsync(self._fd)


class PreviousPartials:
    """The partial folder at `path` an earlier run used, opened by `Destination.previous_partials` or
    `Destination.own_partials`: the partial file of an entry, read-only, deleting it, and deleting what is left there.

    Every error raises `DestinationError` naming the file or the folder, except reading an opened partial file, which
    fails as `PartialFile` does.
    """

    def __init__(self, fd: int, path: Path, *, own: bool) -> None:
        self.path = path
        self._fd = fd
        self._own = own

    def stat(self) -> os.stat_result:
        return os.fstat(self._fd)

    def size(self, entry_id: int) -> int | None:
        """The size of the partial file of `entry_id`; `None` when there is no regular file of that name."""
        name = str(entry_id)
        try:
            found = localfs.lstat(name, dir_fd=self._fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise DestinationError.of('inspect', self.path / name, exc) from None
        return found.st_size if stat.S_ISREG(found.st_mode) else None

    @contextmanager
    def partial(self, entry_id: int) -> Iterator[PartialFile]:
        """Open the partial file of `entry_id` for reading."""
        path = self.path / str(entry_id)
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self._fd)
        except OSError as exc:
            raise DestinationError.of('read', path, exc) from None
        try:
            yield PartialFile(fd, path)
        finally:
            os.close(fd)

    def remove(self, entry_id: int) -> None:
        """Delete the partial file of `entry_id` and make that durable; a missing one is no error."""
        name = str(entry_id)
        try:
            try:
                os.unlink(name, dir_fd=self._fd)
            except FileNotFoundError:
                pass
            os.fsync(self._fd)
        except OSError as exc:
            raise DestinationError.of('write', self.path / name, exc) from None

    def remove_leftovers(self) -> None:
        """Delete every regular file in the folder, and then the folder itself, which is left in place when something
        else keeps it from being empty. In the destination's own folder the folder stays, and so do the staged copies,
        which `Destination.remove_staged_copies` handles."""
        try:
            with os.scandir(self._fd) as found:
                files = [e.name for e in found if e.is_file(follow_symlinks=False)]
            for name in files:
                if not (self._own and _COPY_NAME.fullmatch(name)):
                    os.unlink(name, dir_fd=self._fd)
            os.fsync(self._fd)
            if not self._own:
                _remove_empty_folder(self.path)
        except OSError as exc:
            raise DestinationError.of('write', self.path, exc) from None


def _remove_empty_folder(path: Path) -> None:
    """Delete the folder `path` and make that durable, unless it is not empty."""
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        try:
            os.rmdir(path.name, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                return
            raise
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


@contextmanager
def _work_errors(action: DiskAction, path: Path) -> Iterator[None]:
    """Raise `DestinationError` for an error at `path` in the work directory that every other file would meet."""
    try:
        yield
    except OSError as exc:
        if exc.errno in _WORK_FATAL_ERRNOS:
            raise DestinationError.of(action, path, exc) from None
        raise


def _lstat_name(name: str, dir_fd: int) -> os.stat_result | None:
    try:
        return localfs.lstat(name, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in NAME_ERRNOS:
            return None
        raise


@contextmanager
def _made_folder(base_fd: int, local: PurePosixPath) -> Iterator[int]:
    """An open descriptor of the folder `local` below `base_fd`, creating each missing component, never following a
    link."""
    fd = base_fd
    try:
        for part in local.parts:
            parent_fd = fd
            try:
                localfs.make_dir(part, dir_fd=parent_fd)
            except FileExistsError:
                pass
            else:
                os.fsync(parent_fd)
            fd = localfs.open_dir(part, dir_fd=parent_fd)
            if parent_fd != base_fd:
                os.close(parent_fd)
        yield fd
    finally:
        if fd != base_fd:
            os.close(fd)


def _copy_name(entry_id: int) -> str:
    return f'{entry_id}{_COPY_SUFFIX}'


def _separate_folder(partial_dir: Path, key: str) -> tuple[int, Path]:
    """The open descriptor and path of the folder `key` below `partial_dir`, created when missing."""
    folder = partial_dir / key
    try:
        parent_fd = os.open(partial_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            return _work_dir(key, parent_fd, folder), folder
        finally:
            os.close(parent_fd)
    except OSError as exc:
        raise DestinationError.of('write', folder, exc) from None


def _create_last_component(path: Path) -> Path:
    try:
        os.mkdir(path)
    except FileExistsError:
        if not path.is_dir():
            raise DestinationUsageError(f'Destination {path} exists and is not a folder.') from None
    return path.resolve(strict=True)


def _work_dir(name: str, parent_fd: int, shown: Path) -> int:
    try:
        localfs.make_dir(name, dir_fd=parent_fd)
    except FileExistsError:
        pass
    try:
        return localfs.open_dir(name, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise DestinationError('write', shown, 'it is not a folder; move it away and run again') from None
        raise


def _lock(work_fd: int, destination: Path) -> int:
    fd = os.open(LOCK_FILE_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=work_fd)
    _flock(fd, destination)
    return fd


def _read_lock(root_fd: int, destination: Path) -> int | None:
    """The lock file of the work directory below `root_fd`, opened read-only and locked; `None` when the work directory
    or its lock file is missing or not what a run creates (a run could not use either)."""
    try:
        work_fd = localfs.open_dir(WORK_DIR_NAME, dir_fd=root_fd)
    except OSError as exc:
        if exc.errno in MISSING_ERRNOS:
            return None
        raise
    try:
        fd = os.open(LOCK_FILE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=work_fd)
    except OSError as exc:
        if exc.errno in MISSING_ERRNOS:
            return None
        raise
    finally:
        os.close(work_fd)
    _flock(fd, destination)
    return fd


def _flock(fd: int, destination: Path) -> None:
    """Take the run lock on `fd`, closing it when another run holds it."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RescueError(
            f'Another yandex-disk-rescue run is using {destination}; wait for it to finish and try again.'
        ) from None


def _probe_noreplace(partial_fd: int, destination: Path) -> None:
    """Check that a no-replace rename in the partial folder moves a file and refuses an existing target."""
    first, second = _PROBE_NAMES
    for name in _PROBE_NAMES:
        try:
            os.unlink(name, dir_fd=partial_fd)
        except FileNotFoundError:
            pass
    unsupported = DestinationError(
        'write',
        destination,
        'its filesystem does not support renaming without replacing (RENAME_NOREPLACE), which download needs so it '
        'never overwrites a file',
    )
    _touch(first, partial_fd)
    try:
        localfs.rename_noreplace(first, second, src_dir_fd=partial_fd, dst_dir_fd=partial_fd)
    except OSError as exc:
        if exc.errno in (errno.EINVAL, errno.ENOSYS):
            raise unsupported from None
        raise
    _touch(first, partial_fd)
    try:
        localfs.rename_noreplace(first, second, src_dir_fd=partial_fd, dst_dir_fd=partial_fd)
    except FileExistsError:
        pass
    else:
        raise unsupported
    for name in _PROBE_NAMES:
        os.unlink(name, dir_fd=partial_fd)


def _touch(name: str, dir_fd: int) -> None:
    os.close(os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dir_fd))


def _close_all(fds: list[int]) -> None:
    for fd in reversed(fds):
        os.close(fd)
