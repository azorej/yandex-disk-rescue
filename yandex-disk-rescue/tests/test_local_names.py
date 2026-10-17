"""Tests for the local name mapping: exact names, long-name shortening, sibling collisions and rejected names."""

import os
import unicodedata
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest
from yandex_disk_rescue.api import Resource
from yandex_disk_rescue.local_names import (
    MAX_NAME_BYTES,
    MAX_NAME_UTF16_UNITS,
    collision_key,
    map_local_names,
    rejected_name_conversion,
)

MODIFIED = datetime(2024, 5, 1, 12, 30, tzinfo=UTC)
NFC_SHORT_I = 'й'
NFD_SHORT_I = 'й'
ZWJ = '‍'
LONG_CYRILLIC = 'я' * 146 + '.txt'
LONG_GREEK = 'ΐ' * 127


def _file(path: str) -> Resource:
    return Resource(path=path, kind='file', size=1, md5=None, sha256=None, modified=MODIFIED)


def _dir(path: str) -> Resource:
    return Resource(path=path, kind='dir', size=None, md5=None, sha256=None, modified=MODIFIED)


def _siblings(names: list[str]) -> list[Resource]:
    return [_file(f'disk:/A/{name}') for name in names]


@pytest.mark.parametrize(
    ('remote_names', 'local_names'),
    [
        pytest.param(['Документы.pdf'], ['Документы.pdf'], id='cyrillic'),
        pytest.param(['a  b .txt'], ['a  b .txt'], id='spaces'),
        pytest.param(['a:?*b'], ['a:?*b'], id='windows-reserved'),
        pytest.param([NFD_SHORT_I + '.txt'], [NFD_SHORT_I + '.txt'], id='nfd-kept'),
        pytest.param([LONG_CYRILLIC], ['я' * 121 + '~e7f9df41.txt'], id='long-with-extension'),
        pytest.param([LONG_GREEK], ['ΐ' * 82 + '~4b4b68e6'], id='long-in-nfd-only'),
        pytest.param(['x' * 300], ['x' * 246 + '~0d4e2ca9'], id='long-without-extension'),
        pytest.param(['x' * 250 + '.' + 'e' * 17], ['x' * 246 + '~3194f104'], id='long-extension'),
        pytest.param(['x' + 'é' * 130], ['x' + 'é' * 81 + '~0cdf23db'], id='long-cut-before-mark'),
        pytest.param(['A.txt', 'a.txt'], ['A.txt', 'a~18b7cb09.txt'], id='case'),
        pytest.param([NFC_SHORT_I, NFD_SHORT_I], [NFC_SHORT_I + '~15f2ccab', NFD_SHORT_I], id='nfc-nfd'),
        pytest.param(['a.txt', f'a{ZWJ}.txt'], ['a.txt', f'a{ZWJ}~3ad2f25a.txt'], id='ignorable'),
        pytest.param(['.Bashrc', '.bashrc'], ['.Bashrc', '.bashrc~b7cf3e96'], id='dotfile'),
        pytest.param(['A', 'a', 'a~ca978112'], ['A', 'a~ca978112ca1bbdca', 'a~ca978112'], id='real-name-wins'),
    ],
)
def test_sibling_names(remote_names: list[str], local_names: list[str]) -> None:
    mapped = map_local_names('disk:/A', _siblings(remote_names), {})

    assert mapped == {
        f'disk:/A/{remote}': PurePosixPath(local) for remote, local in zip(remote_names, local_names, strict=True)
    }


def test_work_dir_name_is_taken_at_the_root_only() -> None:
    entries = [_dir('disk:/.Yandex-Disk-Rescue'), _dir('disk:/B'), _file('disk:/B/.yandex-disk-rescue')]

    mapped = map_local_names('disk:/', entries, {'disk:/.Yandex-Disk-Rescue': PurePosixPath('.Yandex-Disk-Rescue')})

    assert mapped == {
        'disk:/.Yandex-Disk-Rescue': PurePosixPath('.Yandex-Disk-Rescue~a7c9d139'),
        'disk:/B': PurePosixPath('B'),
        'disk:/B/.yandex-disk-rescue': PurePosixPath('B/.yandex-disk-rescue'),
    }


def test_renamed_folder_carries_its_children() -> None:
    entries = [_dir('disk:/A/D'), _dir('disk:/A/d'), _dir('disk:/A/d/sub'), _file('disk:/A/d/sub/x.txt')]

    mapped = map_local_names('disk:/A', entries, {})

    assert mapped == {
        'disk:/A/D': PurePosixPath('D'),
        'disk:/A/d': PurePosixPath('d~18ac3e73'),
        'disk:/A/d/sub': PurePosixPath('d~18ac3e73/sub'),
        'disk:/A/d/sub/x.txt': PurePosixPath('d~18ac3e73/sub/x.txt'),
    }


def test_assigned_name_keeps_its_place() -> None:
    assigned = {'disk:/A/photo.jpg': PurePosixPath('photo.jpg')}

    mapped = map_local_names('disk:/A', _siblings(['PHOTO.jpg', 'photo.jpg']), assigned)

    assert mapped == {
        'disk:/A/PHOTO.jpg': PurePosixPath('PHOTO~e73db093.jpg'),
        'disk:/A/photo.jpg': PurePosixPath('photo.jpg'),
    }


def test_assigned_converted_name_is_kept() -> None:
    assigned = {'disk:/A/a?b.txt': PurePosixPath('a_b~f8e3cd91.txt')}

    assert map_local_names('disk:/A', _siblings(['a?b.txt']), assigned) == {
        'disk:/A/a?b.txt': PurePosixPath('a_b~f8e3cd91.txt')
    }


@pytest.mark.parametrize(
    ('assigned', 'expected'),
    [
        pytest.param(
            {'disk:/A/d/x.txt': PurePosixPath('elsewhere/x.txt')}, PurePosixPath('d/x.txt'), id='parent-moved'
        ),
        pytest.param({'disk:/A/d/x.txt': PurePosixPath('d/' + 'y' * 256)}, PurePosixPath('d/x.txt'), id='too-long'),
    ],
)
def test_assigned_name_is_dropped_when_no_longer_valid(
    assigned: dict[str, PurePosixPath], expected: PurePosixPath
) -> None:
    mapped = map_local_names('disk:/A', [_dir('disk:/A/d'), _file('disk:/A/d/x.txt')], assigned)

    assert mapped['disk:/A/d/x.txt'] == expected


def test_two_assigned_siblings_on_one_key_keep_the_first() -> None:
    assigned = {'disk:/A/B': PurePosixPath('x'), 'disk:/A/a': PurePosixPath('X')}

    mapped = map_local_names('disk:/A', _siblings(['B', 'a']), assigned)

    assert mapped == {'disk:/A/B': PurePosixPath('x'), 'disk:/A/a': PurePosixPath('a')}


@pytest.mark.parametrize(
    'entries',
    [
        pytest.param([_file('disk:/A')], id='the-root-itself'),
        pytest.param([_file('disk:/AB/x')], id='outside'),
        pytest.param([_file('disk:/A/d/x')], id='missing-parent'),
    ],
)
def test_entries_outside_the_tree_raise(entries: list[Resource]) -> None:
    with pytest.raises(ValueError, match='disk:/A'):
        map_local_names('disk:/A', entries, {})


def test_mapping_is_deterministic() -> None:
    names = ['b', 'B', 'a.txt', 'A.txt', NFC_SHORT_I, NFD_SHORT_I, LONG_GREEK]

    assert map_local_names('disk:/A', _siblings(names), {}) == map_local_names(
        'disk:/A', _siblings(list(reversed(names))), {}
    )


def test_mapped_paths_can_be_created_and_read_back(tmp_path: Path) -> None:
    entries = [
        _dir('disk:/Папка'),
        _dir('disk:/папка'),
        _file(f'disk:/Папка/{LONG_CYRILLIC}'),
        _file(f'disk:/Папка/{LONG_GREEK}'),
        _file(f'disk:/папка/{NFC_SHORT_I}'),
        _file(f'disk:/папка/{NFD_SHORT_I}'),
        _file(f'disk:/папка/a{ZWJ}.txt'),
        _file('disk:/папка/a.txt'),
        _file('disk:/.yandex-disk-rescue'),
    ]

    mapped = map_local_names('disk:/', entries, {})

    for entry in entries:
        target = tmp_path / mapped[entry.path]
        if entry.kind == 'dir':
            target.mkdir()
        else:
            target.write_bytes(b'')
    created = {
        PurePosixPath(os.path.relpath(Path(root) / name, tmp_path))
        for root, dirs, files in os.walk(tmp_path)
        for name in dirs + files
    }
    assert created == set(mapped.values())
    for path in mapped.values():
        assert all(len(part.encode()) <= MAX_NAME_BYTES for part in path.parts)
        assert all(
            len(unicodedata.normalize('NFD', part).encode('utf-16-le')) // 2 <= MAX_NAME_UTF16_UNITS
            for part in path.parts
        )
    assert len({(path.parent, collision_key(path.name)) for path in mapped.values()}) == len(mapped)


def test_collision_key_folds_case_normalization_and_ignorables() -> None:
    assert collision_key('A' + ZWJ + NFC_SHORT_I + '﻿') == collision_key('a' + NFD_SHORT_I)


@pytest.mark.parametrize(
    ('name', 'taken', 'converted'),
    [
        pytest.param('a?b.txt', set(), 'a_b~f8e3cd91.txt', id='reserved-character'),
        pytest.param('report. ', set(), 'report__~ae1512f2', id='trailing-dot-and-space'),
        pytest.param('x\x07y', set(), 'x_y~b429fa83', id='control-character'),
        pytest.param('a\\b:c*d"e<f>g|h\x7f.md', set(), 'a_b_c_d_e_f_g_h_~5b43e8a4.md', id='every-reserved-character'),
        pytest.param('safe.txt', set(), 'safe~5cfcfacc.txt', id='already-safe'),
        pytest.param('a?b.txt', {collision_key('A_B~F8E3CD91.txt')}, 'a_b~f8e3cd9103f98886.txt', id='taken'),
        pytest.param('?' * 300, set(), '_' * 246 + '~5cb5e403', id='long'),
    ],
)
def test_rejected_name_conversion(name: str, taken: set[str], converted: str) -> None:
    assert rejected_name_conversion(name, taken) == converted
