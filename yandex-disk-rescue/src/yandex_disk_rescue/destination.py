"""The destination folder of a download: its checks, the run lock and work directory, and every file operation inside
it.

All operations go through open folder descriptors and `localfs`, so none follows a symbolic link inside the
destination; the destination itself may be a link and is resolved once. Final paths only ever receive a finished
file through the no-replace rename.
"""

import errno
import fcntl
import os
import stat
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Self

from yandex_disk_rescue import localfs
from yandex_disk_rescue.errors import RescueError
from yandex_disk_rescue.local_names import WORK_DIR_NAME

LOCK_FILE_NAME = 'lock'
PARTIAL_DIR_NAME = 'partial'
_PROBE_NAMES = ('probe', 'probe.renamed')
# Errors that no other file of the run would escape: the run stops instead of failing every file in turn.
FATAL_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS, errno.EACCES, errno.EPERM})
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
    `.yandex-disk-rescue/partial/`, named by entry id.
    """

    def __init__(self, path: Path, root_fd: int, work_fd: int, lock_fd: int, partial_fd: int) -> None:
        self.path = path
        self._root_fd = root_fd
        self._work_fd = work_fd
        self._lock_fd = lock_fd
        self._partial_fd = partial_fd

    @classmethod
    def open(cls, path: Path) -> Destination:
        """Create `path` if it is missing (its last component only), take the run lock in its work directory and
        check the filesystem supports the no-replace rename.

        Raises `RescueError` when another run holds the lock and `DestinationError` when the destination cannot be
        written or lacks the no-replace rename.
        """
        fds: list[int] = []
        try:
            resolved = _create_last_component(path)
            fds.append(os.open(resolved, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
            fds.append(_work_dir(WORK_DIR_NAME, fds[0], resolved / WORK_DIR_NAME))
            fds.append(_lock(fds[1], resolved))
            fds.append(_work_dir(PARTIAL_DIR_NAME, fds[1], resolved / WORK_DIR_NAME / PARTIAL_DIR_NAME))
            _probe_noreplace(fds[1], resolved)
        except OSError as exc:
            _close_all(fds)
            raise DestinationError.of(path, exc) from None
        except BaseException:
            _close_all(fds)
            raise
        return cls(resolved, *fds)

    def close(self) -> None:
        _close_all([self._partial_fd, self._lock_fd, self._work_fd, self._root_fd])

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()

    def lstat(self, local: PurePosixPath) -> os.stat_result | None:
        """The `lstat` of `local`, or `None` when it, or a folder on the way to it, is missing or not a real folder."""
        try:
            with self._folder(local.parent) as parent_fd:
                return os.stat(local.name, dir_fd=parent_fd, follow_symlinks=False)
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
                existing = os.stat(local.name, dir_fd=parent_fd, follow_symlinks=False)
                return stat.S_ISDIR(existing.st_mode)
            os.fsync(parent_fd)
        return True

    @contextmanager
    def partial(self, entry_id: int) -> Iterator[PartialFile]:
        """Open the partial file of `entry_id`, emptied, for writing."""
        fd = os.open(
            str(entry_id),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o666,
            dir_fd=self._partial_fd,
        )
        try:
            yield PartialFile(fd)
        finally:
            os.close(fd)

    def remove_partial(self, entry_id: int) -> None:
        try:
            os.unlink(str(entry_id), dir_fd=self._partial_fd)
        except FileNotFoundError:
            pass

    def keep_partials(self, entry_ids: Collection[int]) -> None:
        """Delete every file in the partial folder that is not the partial file of one of `entry_ids`."""
        keep = {str(entry_id) for entry_id in entry_ids}
        for name in os.listdir(self._partial_fd):
            if name not in keep:
                os.unlink(name, dir_fd=self._partial_fd)

    def place(self, entry_id: int, local: PurePosixPath) -> os.stat_result:
        """Move the finished partial file of `entry_id` to `local` without replacing anything, make the move
        durable, and return the `lstat` of the placed file.

        Raises `FileExistsError` when anything is at `local`; the partial file then stays where it was.
        """
        with self._folder(local.parent) as parent_fd:
            localfs.rename_noreplace(str(entry_id), local.name, src_dir_fd=self._partial_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
            return os.stat(local.name, dir_fd=parent_fd, follow_symlinks=False)

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


class PartialFile:
    """An open partial file: positional writes, then the final mtime and an fsync."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def write(self, offset: int, chunk: bytes) -> None:
        view = memoryview(chunk)
        while view:
            written = os.pwrite(self._fd, view, offset)
            view = view[written:]
            offset += written

    def finish(self, mtime_ns: int) -> None:
        """Set the modification time and flush the content and metadata to the disk."""
        os.utime(self._fd, ns=(mtime_ns, mtime_ns))
        os.fsync(self._fd)


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


def _probe_noreplace(work_fd: int, destination: Path) -> None:
    """Check that a no-replace rename in the work directory moves a file and refuses an existing target."""
    first, second = _PROBE_NAMES
    for name in _PROBE_NAMES:
        try:
            os.unlink(name, dir_fd=work_fd)
        except FileNotFoundError:
            pass
    unsupported = DestinationError(
        f'Could not write {destination}: its filesystem does not support renaming without replacing '
        '(RENAME_NOREPLACE), which download needs so it never overwrites a file.'
    )
    _touch(first, work_fd)
    try:
        localfs.rename_noreplace(first, second, src_dir_fd=work_fd, dst_dir_fd=work_fd)
    except OSError as exc:
        if exc.errno in (errno.EINVAL, errno.ENOSYS):
            raise unsupported from None
        raise
    _touch(first, work_fd)
    try:
        localfs.rename_noreplace(first, second, src_dir_fd=work_fd, dst_dir_fd=work_fd)
    except FileExistsError:
        pass
    else:
        raise unsupported
    for name in _PROBE_NAMES:
        os.unlink(name, dir_fd=work_fd)


def _touch(name: str, dir_fd: int) -> None:
    os.close(os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dir_fd))


def _close_all(fds: list[int]) -> None:
    for fd in reversed(fds):
        os.close(fd)
