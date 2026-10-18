"""The filesystem calls a destination makes on names and open files: looking a name up, opening a folder or a file,
creating a folder, the rename that never replaces an existing path, reading an open file and setting its times, and
the free space of a filesystem.

Every call on a name works relative to an open folder descriptor and never follows a symbolic link, so nothing is
ever read or written through a link that appears inside the destination.
"""

import ctypes
import errno
import os

RENAME_NOREPLACE = 1
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

_libc = ctypes.CDLL(None, use_errno=True)


def lstat(name: str, *, dir_fd: int) -> os.stat_result:
    return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)


def open_dir(name: str, *, dir_fd: int) -> int:
    """Open the folder `name` in `dir_fd`; a symbolic link raises `ELOOP`, a file `ENOTDIR`."""
    return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)


def open_file(name: str, *, dir_fd: int) -> int:
    """Open the file `name` in `dir_fd` for reading without blocking on a FIFO; a symbolic link raises `ELOOP`."""
    return os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)


def read(fd: int, size: int, offset: int) -> bytes:
    """Up to `size` bytes of the open file `fd` from `offset`, without moving its file position; empty at the end."""
    return os.pread(fd, size, offset)


def make_dir(name: str, *, dir_fd: int) -> None:
    os.mkdir(name, dir_fd=dir_fd)


def rename_noreplace(src: str, dst: str, *, src_dir_fd: int, dst_dir_fd: int) -> None:
    """Rename `src` to `dst` with `renameat2(RENAME_NOREPLACE)`: an existing `dst` raises `FileExistsError` and is
    left as it was, and a filesystem without the flag raises `EINVAL`."""
    renameat2 = getattr(_libc, 'renameat2', None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, 'the C library has no renameat2', dst)
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    if renameat2(src_dir_fd, os.fsencode(src), dst_dir_fd, os.fsencode(dst), RENAME_NOREPLACE) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), dst)


def set_times(fd: int, *, atime_ns: int, mtime_ns: int) -> None:
    os.utime(fd, ns=(atime_ns, mtime_ns))


def free_bytes(dir_fd: int) -> int:
    """The bytes an unprivileged process can still allocate on the filesystem of the open folder `dir_fd`."""
    found = os.fstatvfs(dir_fd)
    return found.f_bavail * found.f_frsize
