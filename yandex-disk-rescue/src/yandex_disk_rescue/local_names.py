"""Local names of mirrored entries: exact remote names, shortened when too long, made unique among siblings.

Siblings are compared by `collision_key`, the same on every filesystem, so a mirror can be copied to a case- or
normalization-insensitive disk without two entries landing on one name.
"""

import hashlib
import re
import unicodedata
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping
from pathlib import PurePosixPath

from yandex_disk_rescue.api import ROOT, Resource

MAX_NAME_BYTES = 255  # UTF-8 bytes (ext4, xfs, btrfs)
MAX_NAME_UTF16_UNITS = 255  # UTF-16 units of the NFD form (HFS+)
WORK_DIR_NAME = '.yandex-disk-rescue'

_HASH_DIGITS = (8, 16, 64)
_MAX_EXTENSION_BYTES = 16
_IGNORABLE = re.compile('[‌-‏‪-‮⁪-⁯﻿]')
_REJECTED_CHARS = re.compile(r'[\\:*?"<>|\x00-\x1f\x7f]')
_TRAILING_DOTS_AND_SPACES = re.compile(r'[. ]+$')


def collision_key(name: str) -> str:
    """Return the key under which two sibling names count as the same name: `NFD(casefold(NFD(name)))` with the
    code points HFS+ ignores (U+200C-U+200F, U+202A-U+202E, U+206A-U+206F, U+FEFF) removed."""
    stripped = _IGNORABLE.sub('', name)
    return unicodedata.normalize('NFD', unicodedata.normalize('NFD', stripped).casefold())


def map_local_names(
    remote_root: str, entries: Iterable[Resource], assigned: Mapping[str, PurePosixPath]
) -> dict[str, PurePosixPath]:
    """Map the remote path of every entry below `remote_root` to its local path relative to the destination.

    `assigned` holds the local paths recorded earlier, by remote path. An assigned entry keeps its path while its
    parent's local path is unchanged, the name is short enough and no sibling assigned earlier in code-point order
    holds its collision key. Every other entry gets its remote name, shortened when too long; among siblings whose
    names still collide, the first in code-point order keeps the name and the others get a `~<hex>` suffix. At the
    destination root the collision key of `WORK_DIR_NAME` is taken.

    Raises `ValueError` for an entry that is not strictly below `remote_root` or whose parent folder is missing
    from `entries`.
    """
    children = _children_by_parent(remote_root, entries)
    result: dict[str, PurePosixPath] = {}
    pending = [(remote_root, PurePosixPath())]
    while pending:
        parent_remote, parent_local = pending.pop()
        reserved = {collision_key(WORK_DIR_NAME)} if parent_remote == remote_root else set()
        names = _sibling_names(sorted(children.pop(parent_remote, [])), parent_local, assigned, reserved)
        for remote_path, name in names.items():
            result[remote_path] = parent_local / name
            pending.append((remote_path, parent_local / name))
    if children:
        orphan = min(path for siblings in children.values() for _, path in siblings)
        raise ValueError(f'the parent folder of {orphan} is not among the entries')
    return result


def rejected_name_conversion(name: str, taken: Collection[str]) -> str:
    """Convert a name the destination filesystem refuses into one it accepts, unique among the sibling collision
    keys in `taken`.

    Each of ``\\ : * ? " < > |``, U+0000-U+001F and U+007F becomes `_`, as does every character of a trailing run of
    dots and spaces; the result is shortened when too long and always gets the `~<hex>` suffix of the original
    name, extended when it collides.
    """
    replaced = _REJECTED_CHARS.sub('_', name)
    replaced = _TRAILING_DOTS_AND_SPACES.sub(lambda match: '_' * len(match.group()), replaced)
    for digits in _HASH_DIGITS:
        candidate = _with_suffix(replaced, _hex(name, digits))
        if collision_key(candidate) not in taken:
            return candidate
    raise ValueError(f'no converted name for {name!r} is free among its siblings')


def with_ending(name: str, ending: str) -> str:
    """`name` followed by `ending`; when that is too long, `name` is shortened as a too-long remote name is, its
    `~<hex>` suffix included, until the result with `ending` fits."""
    if _fits(name + ending):
        return name + ending
    return _with_suffix(name, _hex(name, _HASH_DIGITS[0]), ending)


def _fits(name: str) -> bool:
    return (
        len(name.encode()) <= MAX_NAME_BYTES
        and len(unicodedata.normalize('NFD', name).encode('utf-16-le')) // 2 <= MAX_NAME_UTF16_UNITS
    )


def _children_by_parent(remote_root: str, entries: Iterable[Resource]) -> dict[str, list[tuple[str, str]]]:
    prefix = remote_root if remote_root == ROOT else remote_root + '/'
    children: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for entry in entries:
        if not entry.path.startswith(prefix) or entry.path == prefix:
            raise ValueError(f'{entry.path} is not below {remote_root}')
        parent, _, name = entry.path.rpartition('/')
        children[ROOT if parent == 'disk:' else parent].append((name, entry.path))
    return children


def _sibling_names(
    siblings: list[tuple[str, str]], parent_local: PurePosixPath, assigned: Mapping[str, PurePosixPath], taken: set[str]
) -> dict[str, str]:
    """Name the `(name, remote_path)` siblings, sorted by name: assigned names first, then free remote names, then
    suffixed names for the rest."""
    names: dict[str, str] = {}
    unnamed: list[tuple[str, str]] = []
    for name, remote_path in siblings:
        local = assigned.get(remote_path)
        if local is not None and local.parent == parent_local and _claim(local.name, taken):
            names[remote_path] = local.name
        else:
            unnamed.append((name, remote_path))
    collided: list[tuple[str, str]] = []
    for name, remote_path in unnamed:
        plain = name if _fits(name) else _with_suffix(name, _hex(name, _HASH_DIGITS[0]))
        if _claim(plain, taken):
            names[remote_path] = plain
        else:
            collided.append((name, remote_path))
    for name, remote_path in collided:
        names[remote_path] = _suffixed_name(name, remote_path, taken)
    return names


def _suffixed_name(name: str, remote_path: str, taken: set[str]) -> str:
    for digits in _HASH_DIGITS:
        candidate = _with_suffix(name, _hex(name, digits))
        if _claim(candidate, taken):
            return candidate
    raise ValueError(f'no local name for {remote_path} is free among its siblings')


def _claim(name: str, taken: set[str]) -> bool:
    key = collision_key(name)
    if not _fits(name) or key in taken:
        return False
    taken.add(key)
    return True


def _hex(name: str, digits: int) -> str:
    return hashlib.sha256(name.encode()).hexdigest()[:digits]


def _with_suffix(name: str, hex_digits: str, ending: str = '') -> str:
    """Insert `~<hex_digits>` before the extension of `name` (or at its end) and append `ending`, cutting the stem at a
    character boundary until the result fits."""
    stem, extension = _split_extension(name)
    tail = f'~{hex_digits}{extension}{ending}'
    low, high = 0, len(stem)
    while low < high:
        middle = (low + high + 1) // 2
        if _fits(stem[:middle] + tail):
            low = middle
        else:
            high = middle - 1
    # A cut before a combining mark would leave the mark at the start of the suffix's character.
    while 0 < low < len(stem) and unicodedata.combining(stem[low]):
        low -= 1
    return stem[:low] + tail


def _split_extension(name: str) -> tuple[str, str]:
    stem, dot, extension = name.rpartition('.')
    if dot and stem and 1 <= len(extension.encode()) <= _MAX_EXTENSION_BYTES:
        return stem, dot + extension
    return name, ''
