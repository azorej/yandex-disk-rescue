"""Tests for the manifest database: committed scans, scan times, mirrors, migrations and the run lock."""

import os
import signal
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

import pytest
from yandex_disk_rescue.api import Resource
from yandex_disk_rescue.manifest import (
    SCHEMA_VERSION,
    DownloadRecord,
    LocalFingerprint,
    Manifest,
    ManifestError,
    Mirror,
    MirrorMismatchError,
    RemoteIdentity,
    ScanTime,
    Segment,
    UnencodableEntry,
)
from yandex_disk_rescue.paths import state_dir

UID = '1'
MODIFIED = datetime(2024, 5, 1, 12, 30, tzinfo=UTC)


def _file(path: str, size: int = 10, md5: str | None = 'aa', modified: datetime = MODIFIED) -> Resource:
    return Resource(path=path, kind='file', size=size, md5=md5, sha256=None, modified=modified)


def _dir(path: str) -> Resource:
    return Resource(path=path, kind='dir', size=None, md5=None, sha256=None, modified=MODIFIED)


TREE = [
    _dir('disk:/A'),
    _file('disk:/A/keep.txt'),
    _file('disk:/A/edit.txt'),
    _file('disk:/A/gone.txt'),
    Resource(path='disk:/top.bin', kind='file', size=0, md5='bb', sha256='cc', modified=MODIFIED),
]


def _scan(path: Path, resources: list[Resource], root: str = 'disk:/', uid: str = UID):
    with Manifest.open(path) as manifest:
        session = manifest.begin_scan(uid, root)
        session.add(resources)
        return session.commit(unencodable=())


def _unconfirmed(resource: Resource) -> tuple[Segment, ...]:
    """The journal of a download of `resource` that has confirmed nothing yet."""
    return (Segment(0, resource.size, 0, None),) if resource.size else ()


def _entries(path: Path, root: str = 'disk:/', uid: str = UID) -> list[Resource]:
    with Manifest.open(path) as manifest:
        return list(manifest.entries(uid, root))


def test_committed_scan_survives_reopen_and_a_rescan_reports_the_exact_diff(tmp_path):
    db = tmp_path / 'manifest.sqlite3'

    first = _scan(db, TREE)

    assert (first.first_scan, first.added, first.changed, first.removed) == (True, [], [], [])
    assert _entries(db) == sorted(TREE, key=lambda r: r.path)

    rescan = [r for r in TREE if r.path not in {'disk:/A/gone.txt', 'disk:/A/edit.txt'}]
    rescan += [_file('disk:/A/edit.txt', size=11, md5='ab'), _file('disk:/A/new.txt')]
    second = _scan(db, rescan)

    assert second.first_scan is False
    assert second.added == ['disk:/A/new.txt']
    assert second.changed == ['disk:/A/edit.txt']
    assert second.removed == ['disk:/A/gone.txt']
    assert _entries(db) == sorted(rescan, key=lambda r: r.path)


@pytest.mark.parametrize(
    'changed',
    [
        _dir('disk:/A/keep.txt'),
        _file('disk:/A/keep.txt', size=11),
        _file('disk:/A/keep.txt', md5=None),
        Resource(path='disk:/A/keep.txt', kind='file', size=10, md5='aa', sha256='new', modified=MODIFIED),
        _file('disk:/A/keep.txt', modified=MODIFIED + timedelta(microseconds=1)),
    ],
    ids=['kind', 'size', 'md5', 'sha256', 'modified'],
)
def test_any_differing_field_counts_as_changed(tmp_path, changed):
    db = tmp_path / 'manifest.sqlite3'
    _scan(db, TREE)

    diff = _scan(db, [changed if r.path == changed.path else r for r in TREE])

    assert (diff.added, diff.changed, diff.removed) == ([], ['disk:/A/keep.txt'], [])


def test_the_same_instant_in_another_offset_is_not_a_change(tmp_path):
    db = tmp_path / 'manifest.sqlite3'
    _scan(db, TREE)
    shifted = MODIFIED.astimezone(timezone(timedelta(hours=3)))

    diff = _scan(db, [_file('disk:/A/keep.txt', modified=shifted) if r.path == 'disk:/A/keep.txt' else r for r in TREE])

    assert diff.changed == []


def test_an_abandoned_session_leaves_the_committed_scan_and_is_discarded_by_the_next(tmp_path):
    db = tmp_path / 'manifest.sqlite3'
    _scan(db, TREE)

    with Manifest.open(db) as manifest:
        session = manifest.begin_scan(UID, 'disk:/')
        session.add([_file('disk:/staged-only.txt')])
        assert list(manifest.entries(UID, 'disk:/')) == sorted(TREE, key=lambda r: r.path)

    assert _entries(db) == sorted(TREE, key=lambda r: r.path)
    diff = _scan(db, TREE)
    assert (diff.added, diff.changed, diff.removed) == ([], [], [])
    assert 'disk:/staged-only.txt' not in {r.path for r in _entries(db)}


def test_a_session_interrupted_by_an_exception_commits_nothing(tmp_path):
    db = tmp_path / 'manifest.sqlite3'
    _scan(db, TREE)

    with pytest.raises(ValueError, match='outside the scan root'), Manifest.open(db) as manifest:
        session = manifest.begin_scan(UID, 'disk:/A')
        session.add([_file('disk:/A/new.txt')])
        session.add([_file('disk:/A/x.txt'), _file('disk:/B/x.txt')])

    assert _entries(db) == sorted(TREE, key=lambda r: r.path)


def test_scanning_a_folder_leaves_its_siblings_and_other_accounts_alone(tmp_path):
    db = tmp_path / 'manifest.sqlite3'
    siblings = [
        _dir('disk:/A'),
        _file('disk:/A/x'),
        _dir('disk:/AB'),
        _file('disk:/AB/y'),
        _file('disk:/A0'),
        _dir('disk:/B'),
    ]
    _scan(db, siblings)
    _scan(db, [_file('disk:/A/other-account')], uid='2')

    diff = _scan(db, [_dir('disk:/A'), _file('disk:/A/new')], root='disk:/A')

    assert (diff.added, diff.removed, diff.first_scan) == (['disk:/A/new'], ['disk:/A/x'], False)
    assert [r.path for r in _entries(db)] == ['disk:/A', 'disk:/A/new', 'disk:/A0', 'disk:/AB', 'disk:/AB/y', 'disk:/B']
    assert [r.path for r in _entries(db, root='disk:/A')] == ['disk:/A', 'disk:/A/new']
    assert [r.path for r in _entries(db, uid='2')] == ['disk:/A/other-account']


def test_a_discarded_subtree_is_left_out_of_the_commit(tmp_path):
    db = tmp_path / 'manifest.sqlite3'
    _scan(db, [_dir('disk:/A'), _file('disk:/A/x'), _dir('disk:/A/B'), _file('disk:/A/B/y'), _file('disk:/AB')])

    with Manifest.open(db) as manifest:
        session = manifest.begin_scan(UID, 'disk:/')
        session.add([_dir('disk:/A'), _file('disk:/A/x'), _dir('disk:/A/B'), _file('disk:/AB'), _file('disk:/A0')])
        session.add([_file('disk:/A/B/y'), _file('disk:/A/B/z')])
        session.discard('disk:/A/B')
        assert [r.path for r in session.staged()] == ['disk:/A', 'disk:/A/x', 'disk:/A0', 'disk:/AB']
        session.discard('disk:/A')
        assert [r.path for r in session.staged()] == ['disk:/A0', 'disk:/AB']
        diff = session.commit(unencodable=())

    assert (diff.added, diff.removed) == (['disk:/A0'], ['disk:/A', 'disk:/A/B', 'disk:/A/B/y', 'disk:/A/x'])
    assert [r.path for r in _entries(db)] == ['disk:/A0', 'disk:/AB']


def test_discarding_outside_the_scan_root_is_refused(tmp_path):
    with Manifest.open(tmp_path / 'manifest.sqlite3') as manifest:
        session = manifest.begin_scan(UID, 'disk:/A')
        with pytest.raises(ValueError, match='outside the scan root'):
            session.discard('disk:/AB')


def test_first_scan_is_per_subtree(tmp_path):
    db = tmp_path / 'manifest.sqlite3'
    _scan(db, [_file('disk:/A/x')], root='disk:/A')

    assert _scan(db, [_file('disk:/B/y')], root='disk:/B').first_scan is True
    assert _scan(db, [_file('disk:/B/y')], root='disk:/B').first_scan is False


def test_open_creates_a_private_directory_and_database_at_the_current_schema_version():
    db = state_dir() / 'manifest.sqlite3'

    Manifest.open(db).close()

    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    with sqlite3.connect(db) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'


def test_a_newer_schema_version_is_refused(tmp_path):
    db = tmp_path / 'manifest.sqlite3'
    Manifest.open(db).close()
    with sqlite3.connect(db) as connection:
        connection.execute('PRAGMA user_version = 99')

    with pytest.raises(ManifestError, match='schema version 99, newer than') as excinfo:
        Manifest.open(db)

    assert str(db) in str(excinfo.value)
    assert 'Upgrade yandex-disk-rescue' in str(excinfo.value)


def test_a_file_that_is_not_sqlite_is_refused(tmp_path):
    db = tmp_path / 'manifest.sqlite3'
    db.write_bytes(b'this is not a database\n' * 200)

    with pytest.raises(ManifestError, match='is not a yandex-disk-rescue manifest: it is not a SQLite database'):
        Manifest.open(db)

    assert db.read_bytes() == b'this is not a database\n' * 200


def test_another_applications_sqlite_database_is_refused(tmp_path):
    db = tmp_path / 'manifest.sqlite3'
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE notes (body TEXT)')

    with pytest.raises(ManifestError, match='SQLite database of another application'):
        Manifest.open(db)


_OPEN_IN_CHILD = """
import sys
from pathlib import Path
from yandex_disk_rescue.manifest import Manifest, ManifestError
try:
    Manifest.open(Path(sys.argv[1])).close()
except ManifestError as exc:
    print(exc)
    sys.exit(1)
"""


def test_a_second_process_cannot_open_a_manifest_in_use(tmp_path):
    db = tmp_path / 'manifest.sqlite3'
    child = [sys.executable, '-c', _OPEN_IN_CHILD, str(db)]

    with Manifest.open(db):
        locked = subprocess.run(child, capture_output=True, text=True, timeout=60, check=False)
    released = subprocess.run(child, capture_output=True, text=True, timeout=60, check=False)

    assert locked.returncode == 1, locked.stderr
    assert (
        locked.stdout.strip() == f'Another yandex-disk-rescue run is using {db}; wait for it to finish and try again.'
    )
    assert released.returncode == 0, released.stdout + released.stderr


_SCAN_UNTIL_KILLED = """
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from yandex_disk_rescue.api import Resource
from yandex_disk_rescue.manifest import Manifest
manifest = Manifest.open(Path(sys.argv[1]))
session = manifest.begin_scan('1', 'disk:/')
session.add([Resource('disk:/staged.txt', 'file', 1, None, None, datetime.now(UTC))])
print('staged', flush=True)
time.sleep(600)
"""


def _staged_paths(db: Path) -> list[str]:
    with closing(sqlite3.connect(db)) as connection:
        return [path for (path,) in connection.execute('SELECT path FROM staged_entries ORDER BY path')]


def test_rows_staged_before_a_sigkill_stay_on_disk_until_the_next_scan_discards_them(tmp_path):
    db = tmp_path / 'manifest.sqlite3'
    _scan(db, TREE)
    child = subprocess.Popen([sys.executable, '-c', _SCAN_UNTIL_KILLED, str(db)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None
        assert child.stdout.readline() == 'staged\n'
    finally:
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=60)
        child.stdout.close()

    assert _staged_paths(db) == ['disk:/staged.txt']
    with Manifest.open(db) as manifest:
        session = manifest.begin_scan(UID, 'disk:/')
        assert list(session.staged()) == []
    assert _staged_paths(db) == []


V1_FIXTURE = Path(__file__).parent / 'data' / 'manifest-v1.sql'


def _load_v1(db: Path) -> None:
    with closing(sqlite3.connect(db)) as connection:
        connection.executescript(V1_FIXTURE.read_text())


def _ids(db: Path) -> list[tuple[int, str, str]]:
    with closing(sqlite3.connect(db)) as connection:
        return connection.execute('SELECT id, account_uid, path FROM entries ORDER BY id').fetchall()


def test_a_v1_manifest_upgrades_to_the_current_schema_keeping_its_entries_and_their_ids(tmp_path):
    db = tmp_path / 'manifest.sqlite3'
    _load_v1(db)
    v1_ids = _ids(db)

    with Manifest.open(db) as manifest:
        photos = [r.path for r in manifest.entries(UID, 'disk:/')]
        assert manifest.last_scan(UID, 'disk:/') is None
        mirror = manifest.mirror(UID, 'disk:/', tmp_path / 'dest')
        assert [(e.entry_id, e.resource.path, e.local_path, e.download) for e in mirror.entries()] == [
            (1, 'disk:/Photos', None, None),
            (2, 'disk:/Photos/a.jpg', None, None),
            (3, 'disk:/Photos/old.bmp', None, None),
            (4, 'disk:/notes.txt', None, None),
        ]

    assert photos == ['disk:/Photos', 'disk:/Photos/a.jpg', 'disk:/Photos/old.bmp', 'disk:/notes.txt']
    assert _ids(db) == v1_ids
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION == 4
        assert connection.execute('SELECT count(*) FROM scans').fetchone()[0] == 0
        assert connection.execute('SELECT account_uid, remote_root FROM mirrors').fetchall() == [(UID, 'disk:/')]


V3_FIXTURE = Path(__file__).parent / 'data' / 'manifest-v3.sql'


def test_a_v3_manifest_upgrades_with_every_partial_download_restarting_from_byte_0(tmp_path):
    db = tmp_path / 'manifest.sqlite3'
    with closing(sqlite3.connect(db)) as connection:
        connection.executescript(V3_FIXTURE.read_text())

    with Manifest.open(db) as manifest:
        mirror = manifest.mirror(UID, 'disk:/', Path('/dest'))
        records = {e.resource.path: e.download for e in mirror.entries() if e.download is not None}

    assert {path: (record.state, record.segments) for path, record in records.items()} == {
        'disk:/Photos/a.jpg': ('verified', ()),
        'disk:/Photos/big.bin': ('partial', (Segment(0, 5000, 0, None),)),
        'disk:/empty.txt': ('partial', ()),
    }
    assert records['disk:/Photos/a.jpg'].local is not None
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION == 4


def test_a_mirror_is_found_again_by_its_destination(tmp_path):
    db = tmp_path / 'manifest.sqlite3'
    dest = tmp_path / 'dest'
    with Manifest.open(db) as manifest:
        first = manifest.mirror(UID, 'disk:/A', dest)
        assert manifest.mirror(UID, 'disk:/A', dest).id == first.id
        other = manifest.mirror(UID, 'disk:/A', tmp_path / 'elsewhere')

    with Manifest.open(db) as manifest:
        again = manifest.mirror(UID, 'disk:/A', dest)

    assert (again.id, again.destination, again.remote_root) == (first.id, dest, 'disk:/A')
    assert other.id != first.id


@pytest.mark.parametrize(
    ('uid', 'root', 'message'),
    [
        (UID, 'disk:/B', '{dest} already mirrors disk:/A; download disk:/A there or choose another destination.'),
        (UID, 'disk:/', '{dest} already mirrors disk:/A; download disk:/A there or choose another destination.'),
        ('2', 'disk:/A', '{dest} already mirrors disk:/A of another account; choose another destination.'),
    ],
    ids=['other-root', 'ancestor-root', 'other-account'],
)
def test_a_destination_mirrors_one_folder_of_one_account(tmp_path, uid, root, message):
    db = tmp_path / 'manifest.sqlite3'
    dest = tmp_path / 'dest'
    with Manifest.open(db) as manifest:
        manifest.mirror(UID, 'disk:/A', dest)

        with pytest.raises(MirrorMismatchError) as excinfo:
            manifest.mirror(uid, root, dest)

    assert str(excinfo.value) == message.format(dest=dest)
    assert excinfo.value.exit_code == 2
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute('SELECT count(*) FROM mirrors').fetchone()[0] == 1


def _names(paths: dict[str, int], local: dict[str, str]) -> dict[int, PurePosixPath]:
    return {paths[remote]: PurePosixPath(name) for remote, name in local.items()}


def _entry_ids(mirror: Mirror) -> dict[str, int]:
    return {e.resource.path: e.entry_id for e in mirror.entries()}


def _view(mirror: Mirror) -> dict[str, tuple[str | None, DownloadRecord | None]]:
    return {
        e.resource.path: (None if e.local_path is None else str(e.local_path), e.download) for e in mirror.entries()
    }


def _local_paths(mirror: Mirror) -> dict[str, str]:
    return {e.resource.path: str(e.local_path) for e in mirror.entries() if e.local_path is not None}


def test_names_and_download_records_survive_rescans_and_go_with_removed_entries(tmp_path):
    db = tmp_path / 'manifest.sqlite3'
    placed = tmp_path / 'keep.txt'
    placed.write_bytes(b'0123456789')
    _scan(db, TREE)
    with Manifest.open(db) as manifest:
        mirror = manifest.mirror(UID, 'disk:/', Path('/dest'))
        ids = _entry_ids(mirror)
        mirror.assign_names(_names(ids, {r.path: r.path.removeprefix('disk:/') for r in TREE}))
        keep = next(r for r in TREE if r.path == 'disk:/A/keep.txt')
        edit = next(r for r in TREE if r.path == 'disk:/A/edit.txt')
        mirror.record_verified(ids[keep.path], RemoteIdentity.of(keep), 'md5', os.lstat(placed))
        mirror.record_partial(ids[edit.path], RemoteIdentity.of(edit), [Segment(0, 10, 4, 'ab')])
        gone = _file('disk:/A/gone.txt')
        mirror.record_partial(ids[gone.path], RemoteIdentity.of(gone), _unconfirmed(gone))
        before = _view(mirror)

    _scan(db, TREE)
    with Manifest.open(db) as manifest:
        assert _view(manifest.mirror(UID, 'disk:/', Path('/dest'))) == before

    changed = _file('disk:/A/keep.txt', md5='new')
    _scan(db, [changed if r.path == changed.path else r for r in TREE if r.path != 'disk:/A/gone.txt'])
    with Manifest.open(db) as manifest:
        after = {e.resource.path: e for e in manifest.mirror(UID, 'disk:/', Path('/dest')).entries()}

    assert 'disk:/A/gone.txt' not in after
    kept = after['disk:/A/keep.txt']
    assert (kept.entry_id, str(kept.local_path)) == (ids['disk:/A/keep.txt'], 'A/keep.txt')
    assert kept.download is not None
    assert kept.download.identity == RemoteIdentity.of(keep) != RemoteIdentity.of(kept.resource)
    assert after['disk:/A/edit.txt'].download == before['disk:/A/edit.txt'][1]
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute(
            'SELECT count(*) FROM local_names WHERE entry_id = ?', (ids['disk:/A/gone.txt'],)
        ).fetchone() == (0,)
        assert connection.execute(
            'SELECT count(*) FROM downloads WHERE entry_id = ?', (ids['disk:/A/gone.txt'],)
        ).fetchone() == (0,)
        assert connection.execute(
            'SELECT count(*) FROM download_segments WHERE entry_id = ?', (ids['disk:/A/gone.txt'],)
        ).fetchone() == (0,)


def test_mirror_entries_are_the_subtree_below_the_root_with_this_mirrors_names_only(tmp_path):
    db = tmp_path / 'manifest.sqlite3'
    _scan(db, [_dir('disk:/A'), _file('disk:/A/x'), _dir('disk:/AB'), _file('disk:/AB/y')])
    _scan(db, [_dir('disk:/A'), _file('disk:/A/other-account')], uid='2')
    with Manifest.open(db) as manifest:
        whole = manifest.mirror(UID, 'disk:/', tmp_path / 'whole')
        a = manifest.mirror(UID, 'disk:/A', tmp_path / 'a')
        ids = _entry_ids(whole)
        whole.assign_names(_names(ids, {'disk:/A/x': 'A/x'}))
        a.assign_names(_names(ids, {'disk:/A/x': 'x'}))

        entries = a.entries()

    assert isinstance(entries, list)
    assert [(e.resource.path, str(e.local_path)) for e in entries] == [('disk:/A/x', 'x')]


def test_assigning_a_taken_local_path_raises_and_writes_none_of_the_batch(tmp_path):
    db = tmp_path / 'manifest.sqlite3'
    _scan(db, TREE)
    with Manifest.open(db) as manifest:
        mirror = manifest.mirror(UID, 'disk:/', Path('/dest'))
        ids = _entry_ids(mirror)
        mirror.assign_names(_names(ids, {'disk:/A': 'A', 'disk:/top.bin': 'top.bin'}))

        with pytest.raises(ManifestError, match='UNIQUE constraint failed'):
            mirror.assign_names(_names(ids, {'disk:/A/keep.txt': 'A/same', 'disk:/A/edit.txt': 'A/same'}))
        with pytest.raises(ManifestError, match='UNIQUE constraint failed'):
            mirror.assign_names(_names(ids, {'disk:/A/keep.txt': 'A/keep.txt', 'disk:/A/gone.txt': 'top.bin'}))
        assert _local_paths(mirror) == {
            'disk:/A': 'A',
            'disk:/top.bin': 'top.bin',
        }

        mirror.assign_names(_names(ids, {'disk:/A': 'top.bin', 'disk:/top.bin': 'A'}))
        assert _local_paths(mirror) == {
            'disk:/A': 'top.bin',
            'disk:/top.bin': 'A',
        }


@pytest.mark.parametrize('bad', ['/abs', '.', '', 'A/../x'])
def test_a_local_path_must_stay_inside_the_destination(tmp_path, bad):
    db = tmp_path / 'manifest.sqlite3'
    _scan(db, TREE)
    with Manifest.open(db) as manifest:
        mirror = manifest.mirror(UID, 'disk:/', Path('/dest'))
        with pytest.raises(ValueError, match='not a relative path inside the destination'):
            mirror.assign_names({_entry_ids(mirror)['disk:/A']: PurePosixPath(bad)})


def test_records_and_names_are_refused_for_entries_outside_the_mirror(tmp_path):
    db = tmp_path / 'manifest.sqlite3'
    _scan(db, TREE)
    _scan(db, [_file('disk:/A/x')], uid='2')
    with Manifest.open(db) as manifest:
        ids = _entry_ids(manifest.mirror(UID, 'disk:/', Path('/whole')))
        mirror = manifest.mirror(UID, 'disk:/A', Path('/dest'))
        other_account = manifest.mirror('2', 'disk:/', Path('/other')).entries()[0].entry_id

        for outside in (ids['disk:/top.bin'], other_account):
            with pytest.raises(ValueError, match='is not a committed entry below disk:/A'):
                mirror.record_partial(
                    outside, RemoteIdentity.of(_file('disk:/top.bin')), _unconfirmed(_file('disk:/top.bin'))
                )
            with pytest.raises(ValueError, match='is not a committed entry below disk:/A'):
                mirror.assign_names({ids['disk:/A/keep.txt']: PurePosixPath('keep'), outside: PurePosixPath('x')})

    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute('SELECT count(*) FROM downloads').fetchone() == (0,)
        assert connection.execute('SELECT count(*) FROM local_names').fetchone() == (0,)


def test_renaming_a_folder_moves_its_descendants_and_a_taken_name_changes_nothing(tmp_path):
    db = tmp_path / 'manifest.sqlite3'
    tree = [_dir('disk:/A'), _file('disk:/A/x'), _dir('disk:/A/B'), _file('disk:/A/B/y'), _file('disk:/AB')]
    _scan(db, tree)
    with Manifest.open(db) as manifest:
        mirror = manifest.mirror(UID, 'disk:/', Path('/dest'))
        ids = _entry_ids(mirror)
        mirror.assign_names(_names(ids, {r.path: r.path.removeprefix('disk:/') for r in tree}))

        mirror.rename_local(ids['disk:/A'], 'A?')
        renamed = _local_paths(mirror)

        with pytest.raises(ManifestError, match='UNIQUE constraint failed'):
            mirror.rename_local(ids['disk:/A/B'], 'x')
        with pytest.raises(ManifestError, match='UNIQUE constraint failed'):
            mirror.rename_local(ids['disk:/A'], 'AB')
        with pytest.raises(ValueError, match='not a single path component'):
            mirror.rename_local(ids['disk:/A'], 'a/b')
        unchanged = _local_paths(mirror)

    assert renamed == {
        'disk:/A': 'A?',
        'disk:/A/B': 'A?/B',
        'disk:/A/B/y': 'A?/B/y',
        'disk:/A/x': 'A?/x',
        'disk:/AB': 'AB',
    }
    assert unchanged == renamed


def test_a_verified_record_round_trips_the_placed_files_fingerprint_and_names_its_inode_owner(tmp_path):
    db = tmp_path / 'manifest.sqlite3'
    placed = tmp_path / 'keep.txt'
    placed.write_bytes(b'0123456789')
    os.utime(placed, ns=(1_700_000_000_123_456_789, 1_700_000_000_123_456_789))
    local = os.lstat(placed)
    _scan(db, TREE)
    keep = next(r for r in TREE if r.path == 'disk:/A/keep.txt')
    with Manifest.open(db) as manifest:
        mirror = manifest.mirror(UID, 'disk:/', tmp_path / 'dest')
        other = manifest.mirror(UID, 'disk:/', tmp_path / 'other-dest')
        ids = _entry_ids(mirror)
        mirror.record_partial(ids[keep.path], RemoteIdentity.of(keep), _unconfirmed(keep))
        mirror.record_verified(ids[keep.path], RemoteIdentity.of(keep), 'md5+sha256', local)

        (entry,) = [e for e in mirror.entries() if e.download is not None]
        owner = mirror.verified_owner(local.st_ino)
        assert other.verified_owner(local.st_ino) is None
        assert mirror.verified_owner(local.st_ino + 1) is None

        mirror.record_partial(ids[keep.path], RemoteIdentity.of(keep), _unconfirmed(keep))
        assert mirror.verified_owner(local.st_ino) is None
        mirror.forget(ids[keep.path])
        assert all(e.download is None for e in mirror.entries())

    assert entry.entry_id == ids[keep.path]
    assert entry.download is not None
    assert (entry.download.state, entry.download.verified_by) == ('verified', 'md5+sha256')
    assert entry.download.identity == RemoteIdentity.of(keep)
    assert entry.download.local == LocalFingerprint(size=10, mtime_ns=1_700_000_000_123_456_789, ino=local.st_ino)
    assert owner == entry


def _segment_rows(db: Path) -> list[tuple[int, int, int, int, str | None]]:
    with closing(sqlite3.connect(db)) as connection:
        return connection.execute(
            'SELECT entry_id, start, end, confirmed, digest FROM download_segments ORDER BY entry_id, start'
        ).fetchall()


def _journaled(tmp_path: Path):
    """A manifest holding `TREE` scanned, the mirror of disk:/ at /dest, and the entry ids by path."""
    db = tmp_path / 'manifest.sqlite3'
    _scan(db, TREE)
    manifest = Manifest.open(db)
    mirror = manifest.mirror(UID, 'disk:/', Path('/dest'))
    return db, manifest, mirror, _entry_ids(mirror)


def test_a_partial_records_journal_round_trips_and_is_read_again_by_download(tmp_path):
    db, manifest, mirror, ids = _journaled(tmp_path)
    edit = next(r for r in TREE if r.path == 'disk:/A/edit.txt')
    top = next(r for r in TREE if r.path == 'disk:/top.bin')
    journal = (Segment(0, 4, 4, 'aa'), Segment(4, 10, 4, None))
    with manifest:
        assert mirror.download(ids[edit.path]) is None
        mirror.record_partial(ids[edit.path], RemoteIdentity.of(edit), journal)
        mirror.record_partial(ids[top.path], RemoteIdentity.of(top), ())
        listed = {e.resource.path: e.download for e in mirror.entries()}

        mirror.checkpoint(ids[edit.path], [Segment(0, 4, 4, 'aa'), Segment(4, 10, 7, 'bb')])
        read_now = mirror.download(ids[edit.path])

    assert listed[edit.path] == DownloadRecord('partial', RemoteIdentity.of(edit), None, None, journal)
    assert listed[top.path] == DownloadRecord('partial', RemoteIdentity.of(top), None, None, ())
    assert read_now == DownloadRecord(
        'partial', RemoteIdentity.of(edit), None, None, (Segment(0, 4, 4, 'aa'), Segment(4, 10, 7, 'bb'))
    )
    assert _segment_rows(db) == [(ids[edit.path], 0, 4, 4, 'aa'), (ids[edit.path], 4, 10, 7, 'bb')]


@pytest.mark.parametrize(
    'segments',
    [[], [Segment(0, 9, 0, None)], [Segment(0, 5, 0, None), Segment(6, 10, 6, None)], [Segment(5, 10, 5, None)]],
    ids=['none', 'short', 'gap', 'not-from-0'],
)
def test_a_journal_that_does_not_tile_the_file_is_refused(tmp_path, segments):
    db, manifest, mirror, ids = _journaled(tmp_path)
    edit = next(r for r in TREE if r.path == 'disk:/A/edit.txt')
    with manifest, pytest.raises(ValueError, match='do not tile'):
        mirror.record_partial(ids[edit.path], RemoteIdentity.of(edit), segments)

    assert _segment_rows(db) == []


@pytest.mark.parametrize('case', ['other-boundaries', 'fewer-segments', 'verified', 'no-record'])
def test_a_checkpoint_of_other_segments_or_of_no_partial_record_is_refused_and_writes_nothing(tmp_path, case):
    db, manifest, mirror, ids = _journaled(tmp_path)
    keep = next(r for r in TREE if r.path == 'disk:/A/keep.txt')
    placed = tmp_path / 'keep.txt'
    placed.write_bytes(b'0123456789')
    checkpoint = {
        'other-boundaries': [Segment(0, 5, 5, 'aa'), Segment(5, 10, 5, None)],
        'fewer-segments': [Segment(0, 10, 3, 'aa')],
        'verified': [Segment(0, 10, 10, 'aa')],
        'no-record': [Segment(0, 10, 10, 'aa')],
    }[case]
    with manifest:
        if case == 'verified':
            mirror.record_verified(ids[keep.path], RemoteIdentity.of(keep), 'md5', os.lstat(placed))
        elif case != 'no-record':
            mirror.record_partial(
                ids[keep.path], RemoteIdentity.of(keep), [Segment(0, 4, 0, None), Segment(4, 10, 4, None)]
            )
        before = _segment_rows(db)
        with pytest.raises(ValueError):
            mirror.checkpoint(ids[keep.path], checkpoint)

    assert _segment_rows(db) == before


@pytest.mark.parametrize(
    'segment',
    [Segment(0, 10, 11, 'aa'), Segment(0, 10, 3, None), Segment(0, 10, 0, 'aa')],
    ids=['beyond-end', 'no-digest', 'digest-of-nothing'],
)
def test_a_checkpoint_breaking_the_journals_invariant_is_refused(tmp_path, segment):
    db, manifest, mirror, ids = _journaled(tmp_path)
    keep = next(r for r in TREE if r.path == 'disk:/A/keep.txt')
    with manifest:
        mirror.record_partial(ids[keep.path], RemoteIdentity.of(keep), _unconfirmed(keep))
        with pytest.raises(ManifestError, match='CHECK constraint failed'):
            mirror.checkpoint(ids[keep.path], [segment])

    assert _segment_rows(db) == [(ids[keep.path], 0, 10, 0, None)]


def test_verifying_or_forgetting_a_download_deletes_its_journal(tmp_path):
    db, manifest, mirror, ids = _journaled(tmp_path)
    keep = next(r for r in TREE if r.path == 'disk:/A/keep.txt')
    edit = next(r for r in TREE if r.path == 'disk:/A/edit.txt')
    placed = tmp_path / 'keep.txt'
    placed.write_bytes(b'0123456789')
    with manifest:
        for resource in (keep, edit):
            mirror.record_partial(ids[resource.path], RemoteIdentity.of(resource), [Segment(0, 10, 6, 'aa')])
        mirror.record_verified(ids[keep.path], RemoteIdentity.of(keep), 'md5', os.lstat(placed))
        mirror.forget(ids[edit.path])
        verified = mirror.download(ids[keep.path])

    assert verified is not None
    assert (verified.state, verified.segments) == ('verified', ())
    assert _segment_rows(db) == []


def _stat(ino: int, size: int = 1) -> os.stat_result:
    fields = [0] * os.stat_result.n_fields
    fields[1], fields[6], fields[14] = ino, size, 1_700_000_000_000_000_000
    return os.stat_result(fields)


@pytest.mark.parametrize('ino', [1, 2**63 - 1, 2**63, 2**64 - 1])
def test_any_64_bit_inode_number_round_trips(tmp_path, ino):
    db = tmp_path / 'manifest.sqlite3'
    _scan(db, TREE)
    keep = next(r for r in TREE if r.path == 'disk:/A/keep.txt')
    with Manifest.open(db) as manifest:
        mirror = manifest.mirror(UID, 'disk:/', Path('/dest'))
        mirror.record_verified(_entry_ids(mirror)[keep.path], RemoteIdentity.of(keep), 'md5', _stat(ino))

        owner = mirror.verified_owner(ino)

    assert owner is not None
    assert owner.download is not None
    assert owner.download.local == LocalFingerprint(size=1, mtime_ns=1_700_000_000_000_000_000, ino=ino)


def test_verifying_a_file_on_a_reused_inode_drops_the_stale_record_holding_it(tmp_path):
    db = tmp_path / 'manifest.sqlite3'
    _scan(db, TREE)
    keep = next(r for r in TREE if r.path == 'disk:/A/keep.txt')
    edit = next(r for r in TREE if r.path == 'disk:/A/edit.txt')
    with Manifest.open(db) as manifest:
        mirror = manifest.mirror(UID, 'disk:/', Path('/dest'))
        ids = _entry_ids(mirror)
        mirror.record_verified(ids[keep.path], RemoteIdentity.of(keep), 'md5', _stat(7))

        mirror.record_verified(ids[edit.path], RemoteIdentity.of(edit), 'md5', _stat(7))

        owner = mirror.verified_owner(7)
        records = {e.resource.path: e.download for e in mirror.entries() if e.download is not None}

    assert owner is not None
    assert owner.entry_id == ids[edit.path]
    assert list(records) == [edit.path]


def test_remote_identity_is_built_from_files_only():
    with pytest.raises(ValueError, match='is not a file with a size'):
        RemoteIdentity.of(_dir('disk:/A'))


def _scan_time(db: Path, root: str, uid: str = UID) -> ScanTime | None:
    with Manifest.open(db) as manifest:
        return manifest.last_scan(uid, root)


def test_the_last_scan_covering_a_folder_is_the_newest_committed_scan_of_it_or_an_ancestor(tmp_path):
    db = tmp_path / 'manifest.sqlite3'
    with Manifest.open(db) as manifest:
        manifest.begin_scan(UID, 'disk:/')
    assert _scan_time(db, 'disk:/A/B') is None

    started = datetime.now(UTC)
    _scan(db, TREE)
    whole = _scan_time(db, 'disk:/A/B')
    assert whole is not None
    assert whole.root == 'disk:/'
    assert started <= whole.completed_at <= datetime.now(UTC)
    assert whole.completed_at.tzinfo is not None

    _scan(db, [_dir('disk:/A'), _file('disk:/A/x')], root='disk:/A')
    folder = _scan_time(db, 'disk:/A/B')
    assert folder is not None
    assert folder.root == 'disk:/A'
    assert folder.completed_at > whole.completed_at
    assert _scan_time(db, 'disk:/A') == folder
    assert _scan_time(db, 'disk:/AB') == whole
    assert _scan_time(db, 'disk:/') == whole
    assert _scan_time(db, 'disk:/A/B', uid='2') is None

    with Manifest.open(db) as manifest:
        session = manifest.begin_scan(UID, 'disk:/A')
        session.add([_file('disk:/A/y')])
    assert _scan_time(db, 'disk:/A/B') == folder

    _scan(db, TREE)
    again = _scan_time(db, 'disk:/A/B')
    assert again is not None
    assert again.root == 'disk:/'
    assert again.completed_at > folder.completed_at


def test_entries_left_out_for_their_names_are_kept_per_subtree_until_a_scan_covering_them_commits(tmp_path):
    db = tmp_path / 'manifest.sqlite3'
    bad_file = _file('disk:/A/bad\udc80.txt')
    bad_dir = _dir('disk:/B\udc81')
    with Manifest.open(db) as manifest:
        session = manifest.begin_scan(UID, 'disk:/')
        session.add(TREE)
        session.commit(unencodable=[bad_file, bad_dir])
        session = manifest.begin_scan('2', 'disk:/')
        session.add([_dir('disk:/A')])
        session.commit(unencodable=[_file('disk:/A/other\udc82')])
        with pytest.raises(ValueError, match='is not below the scan root'):
            manifest.begin_scan(UID, 'disk:/A').commit(unencodable=[bad_dir])

    with Manifest.open(db) as manifest:
        whole = manifest.mirror(UID, 'disk:/', tmp_path / 'whole')
        assert whole.unencodable() == [
            UnencodableEntry('disk:/A/bad\udc80.txt', 'file'),
            UnencodableEntry('disk:/B\udc81', 'dir'),
        ]
        assert manifest.mirror(UID, 'disk:/A', tmp_path / 'a').unencodable() == [
            UnencodableEntry('disk:/A/bad\udc80.txt', 'file')
        ]
        session = manifest.begin_scan(UID, 'disk:/A')
        session.add([_dir('disk:/A')])
        session.commit(unencodable=())

        assert whole.unencodable() == [UnencodableEntry('disk:/B\udc81', 'dir')]
        assert manifest.mirror('2', 'disk:/', tmp_path / 'other').unencodable() == [
            UnencodableEntry('disk:/A/other\udc82', 'file')
        ]
