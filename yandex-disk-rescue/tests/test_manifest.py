"""Tests for the manifest database: committed scans, abandoned sessions, migrations and the run lock."""

import signal
import sqlite3
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from yandex_disk_rescue.api import Resource
from yandex_disk_rescue.manifest import SCHEMA_VERSION, Manifest, ManifestError
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
        return session.commit()


def _entries(path: Path, root: str = 'disk:/', uid: str = UID) -> list[Resource]:
    with Manifest.open(path) as manifest:
        return list(manifest.entries(uid, root))


def test_committed_scan_survives_reopen_and_a_rescan_reports_the_exact_diff(tmp_path):
    db = tmp_path / 'manifest.sqlite3'

    first = _scan(db, TREE)

    assert first.first_scan is True
    assert first.added == sorted(r.path for r in TREE)
    assert (first.changed, first.removed) == ([], [])
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
        diff = session.commit()

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


def test_sigkill_mid_scan_keeps_the_previous_scan_and_releases_the_lock(tmp_path):
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

    assert _entries(db) == sorted(TREE, key=lambda r: r.path)
    diff = _scan(db, TREE)
    assert (diff.added, diff.changed, diff.removed) == ([], [], [])
