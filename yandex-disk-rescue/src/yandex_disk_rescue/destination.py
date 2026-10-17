"""The destination folder of a download: its checks, the run lock and work directory, and every file operation inside
it.

All operations go through open folder descriptors and `localfs`, so none follows a symbolic link inside the
destination; the destination itself may be a link and is resolved once. Final paths only ever receive a finished
file through the no-replace rename, and whatever stood there is first moved, never deleted, into the run's folder
under `.yandex-disk-rescue/replaced/`. A downloaded file that fails verification is moved, never deleted, into the
run's folder under `.yandex-disk-rescue/quarantine/`.
"""

import errno
import fcntl
import os
import stat
from collections.abc import Collection, Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Self

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
# Errors that no other file of the run would escape: the run stops instead of failing every file in turn.
FATAL_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS})
# A permission error in the work directory, unlike one on a path of the mirror, would meet every file too.
_WORK_FATAL_ERRNOS = FATAL_ERRNOS | {errno.EACCES, errno.EPERM}
# How a filesystem refuses a name it cannot hold, as exFAT refuses `?`.
NAME_ERRNOS = frozenset({errno.EINVAL, errno.EILSEQ, errno.ENAMETOOLONG})
_MISSING_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})


class DestinationUsageError(RescueError):
    """The destination argument cannot be used: it is not a folder, or its parent folder does not exist."""

    exit_code = 2


class DestinationError(RescueError):
    """Writing to the destination failed in a way that stops the run."""

    @classmethod
    def of(cls, path: Path | str, exc: OSError) -> Self:
        return cls(f'Could not write {path}: {exc.strerror}.')


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


class Destination:
    """An open destination holding its run lock; use `Destination.open` as a context manager.

    `path` is the resolved destination. Local paths are relative to it, '/'-separated. Partial files live in
    `.yandex-disk-rescue/partial/`, named by entry id. Things moved aside go to
    `.yandex-disk-rescue/replaced/<run start, UTC>/`, and quarantined partial files to
    `.yandex-disk-rescue/quarantine/<run start, UTC>/`, each created on its first move under a name no earlier run
    took; `replaced_folder` and `quarantine_folder` are those folders relative to the destination once they exist.

    An error in the work directory that every file would meet (a full disk, an I/O error, a read-only filesystem, a
    permission error) raises `DestinationError`; other errors are the `OSError` of the call.

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
        started: datetime,
    ) -> None:
        self.path = path
        self._root_fd = root_fd
        self._work_fd = work_fd
        self._lock_fd = lock_fd
        self._partial_fd = partial_fd
        self._started = started
        self._run_folders: dict[str, tuple[int, PurePosixPath]] = {}

    @classmethod
    def open(cls, path: Path) -> Destination:
        """Create `path` if it is missing (its last component only), take the run lock in its work directory and
        check the filesystem supports the no-replace rename.

        Raises `RescueError` when another run holds the lock and `DestinationError` when the destination cannot be
        written or lacks the no-replace rename.
        """
        started = datetime.now(UTC)
        fds: list[int] = []
        try:
            resolved = _create_last_component(path)
            fds.append(os.open(resolved, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
            fds.append(_work_dir(WORK_DIR_NAME, fds[0], resolved / WORK_DIR_NAME))
            fds.append(_lock(fds[1], resolved))
            fds.append(_work_dir(PARTIAL_DIR_NAME, fds[1], resolved / WORK_DIR_NAME / PARTIAL_DIR_NAME))
            _probe_noreplace(fds[3], resolved)
        except OSError as exc:
            _close_all(fds)
            raise DestinationError.of(path, exc) from None
        except BaseException:
            _close_all(fds)
            raise
        return cls(resolved, *fds, started=started)

    @property
    def replaced_folder(self) -> PurePosixPath | None:
        return self._run_folder(REPLACED_DIR_NAME)

    @property
    def quarantine_folder(self) -> PurePosixPath | None:
        return self._run_folder(QUARANTINE_DIR_NAME)

    def close(self) -> None:
        run_fds = [fd for fd, _ in self._run_folders.values()]
        _close_all([self._partial_fd, self._lock_fd, self._work_fd, self._root_fd, *run_fds])

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()

    def lstat(self, local: PurePosixPath) -> os.stat_result | None:
        """The `lstat` of `local`, or `None` when it, or a folder on the way to it, is missing or not a real folder, or
        when the filesystem refuses its name (`NAME_ERRNOS`), so the call creating it meets the refusal."""
        try:
            with self._folder(local.parent) as parent_fd:
                return _lstat_name(local.name, parent_fd)
        except OSError as exc:
            if exc.errno in _MISSING_ERRNOS:
                return None
            raise

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
        path = self._partial_path(entry_id)
        with _work_errors(path):
            fd = os.open(
                str(entry_id),
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o666,
                dir_fd=self._partial_fd,
            )
        try:
            yield PartialFile(fd, path)
        finally:
            os.close(fd)

    def remove_partial(self, entry_id: int) -> None:
        with _work_errors(self._partial_path(entry_id)):
            try:
                os.unlink(str(entry_id), dir_fd=self._partial_fd)
            except FileNotFoundError:
                pass

    def keep_partials(self, entry_ids: Collection[int]) -> None:
        """Delete every regular file in the partial folder that is not the partial file of one of `entry_ids`; anything
        else there is left alone."""
        keep = {str(entry_id) for entry_id in entry_ids}
        with os.scandir(self._partial_fd) as found:
            stray = [entry.name for entry in found if entry.name not in keep and entry.is_file(follow_symlinks=False)]
        for name in stray:
            os.unlink(name, dir_fd=self._partial_fd)

    def place(self, entry_id: int, local: PurePosixPath) -> os.stat_result:
        """Move the finished partial file of `entry_id` to `local` without replacing anything, make the move
        durable, and return the `lstat` of the placed file.

        Raises `FileExistsError` when anything is at `local`; the partial file then stays where it was.
        """
        with self._folder(local.parent) as parent_fd:
            localfs.rename_noreplace(str(entry_id), local.name, src_dir_fd=self._partial_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
            return localfs.lstat(local.name, dir_fd=parent_fd)

    @contextmanager
    def existing_file(self, local: PurePosixPath) -> Iterator[ExistingFile]:
        """Open the file at `local` for reading without following a link; the caller checks what it opened."""
        with self._folder(local.parent) as parent_fd:
            fd = localfs.open_file(local.name, dir_fd=parent_fd)
        try:
            yield ExistingFile(fd)
        finally:
            os.close(fd)

    def move_aside(self, local: PurePosixPath) -> None:
        """Move whatever is at `local` (a file, a folder with everything in it, or a link itself, never its target)
        to the same relative path in this run's replaced folder, without replacing anything, and make the move
        durable."""
        with ExitStack() as stack:
            source_fd = stack.enter_context(self._folder(local.parent))
            with _work_errors(self.path / WORK_DIR_NAME / REPLACED_DIR_NAME):
                target_fd = stack.enter_context(_made_folder(self._run_folder_fd(REPLACED_DIR_NAME), local.parent))
            localfs.rename_noreplace(local.name, local.name, src_dir_fd=source_fd, dst_dir_fd=target_fd)
            os.fsync(target_fd)
            os.fsync(source_fd)

    def quarantine(self, entry_id: int, local: PurePosixPath) -> PurePosixPath:
        """Move the partial file of `entry_id` to the relative path `local` in this run's quarantine folder, without
        replacing anything, make the move durable, and return the copy's path relative to the destination.

        When the filesystem refuses the file's name (`NAME_ERRNOS`), the copy gets its `rejected_name_conversion`.
        """
        with (
            _work_errors(self.path / WORK_DIR_NAME / QUARANTINE_DIR_NAME),
            _made_folder(self._run_folder_fd(QUARANTINE_DIR_NAME), local.parent) as target_fd,
        ):
            name = local.name
            try:
                localfs.rename_noreplace(str(entry_id), name, src_dir_fd=self._partial_fd, dst_dir_fd=target_fd)
            except OSError as exc:
                if exc.errno not in NAME_ERRNOS:
                    raise
                name = rejected_name_conversion(name, ())
                localfs.rename_noreplace(str(entry_id), name, src_dir_fd=self._partial_fd, dst_dir_fd=target_fd)
            os.fsync(target_fd)
            os.fsync(self._partial_fd)
        return self._run_folders[QUARANTINE_DIR_NAME][1] / local.parent / name

    def _partial_path(self, entry_id: int) -> Path:
        return self.path / WORK_DIR_NAME / PARTIAL_DIR_NAME / str(entry_id)

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
    """An open partial file at `path`: its size, reading back any range of what it holds, cutting or extending it
    (`truncate`), positional writes, fsyncs of the content written so far, then the final mtime and an fsync."""

    def __init__(self, fd: int, path: Path) -> None:
        self._fd = fd
        self._path = path

    def size(self) -> int:
        with _work_errors(self._path):
            return os.fstat(self._fd).st_size

    def chunks(self, start: int, end: int) -> Iterator[bytes]:
        """The bytes `[start, end)` in order, in chunks of at most 1 MiB; the file must hold at least `end` bytes."""
        offset = start
        while offset < end:
            with _work_errors(self._path):
                chunk = localfs.read(self._fd, min(_READ_SIZE, end - offset), offset)
            if not chunk:
                raise ValueError(f'the partial file ends at {offset} bytes, before {end}')
            yield chunk
            offset += len(chunk)

    def truncate(self, length: int) -> None:
        with _work_errors(self._path):
            os.ftruncate(self._fd, length)

    def write(self, offset: int, chunk: bytes) -> None:
        view = memoryview(chunk)
        with _work_errors(self._path):
            while view:
                written = os.pwrite(self._fd, view, offset)
                view = view[written:]
                offset += written

    def sync(self) -> None:
        """Flush the content written so far to the disk."""
        with _work_errors(self._path):
            os.fsync(self._fd)

    def finish(self, mtime_ns: int) -> None:
        """Set the modification time and flush the content and metadata to the disk."""
        with _work_errors(self._path):
            localfs.set_times(self._fd, atime_ns=mtime_ns, mtime_ns=mtime_ns)
            os.fsync(self._fd)


@contextmanager
def _work_errors(path: Path) -> Iterator[None]:
    """Raise `DestinationError` for an error at `path` in the work directory that every other file would meet."""
    try:
        yield
    except OSError as exc:
        if exc.errno in _WORK_FATAL_ERRNOS:
            raise DestinationError.of(path, exc) from None
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
            raise DestinationError(
                f'Could not write {shown}: it is not a folder; move it away and run again.'
            ) from None
        raise


def _lock(work_fd: int, destination: Path) -> int:
    fd = os.open(LOCK_FILE_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=work_fd)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RescueError(
            f'Another yandex-disk-rescue run is using {destination}; wait for it to finish and try again.'
        ) from None
    return fd


def _probe_noreplace(partial_fd: int, destination: Path) -> None:
    """Check that a no-replace rename in the partial folder moves a file and refuses an existing target."""
    first, second = _PROBE_NAMES
    for name in _PROBE_NAMES:
        try:
            os.unlink(name, dir_fd=partial_fd)
        except FileNotFoundError:
            pass
    unsupported = DestinationError(
        f'Could not write {destination}: its filesystem does not support renaming without replacing '
        '(RENAME_NOREPLACE), which download needs so it never overwrites a file.'
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
