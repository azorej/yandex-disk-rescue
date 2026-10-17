"""Tests for `yandex-disk-rescue scan` against the fake Disk API, with the real manifest in the XDG state directory."""

import os
import signal
import subprocess
import time
from datetime import UTC

import pytest
from fake_disk_server import Delay, FakeResponse
from mirror_tree import log_messages, stderr_lines
from typer.testing import CliRunner
from yandex_disk_rescue import scanning
from yandex_disk_rescue.api import Resource
from yandex_disk_rescue.cli import app
from yandex_disk_rescue.manifest import Manifest
from yandex_disk_rescue.paths import log_path, state_dir
from yandex_disk_rescue.units import format_size

runner = CliRunner()

TOKEN = 'y0_AgAAAAB-scan-sentinel-token'
NO_TOKEN_MESSAGE = (
    "Error: No Yandex OAuth token. Run 'yandex-disk-rescue auth login' or set YANDEX_DISK_RESCUE_TOKEN.\n"
)


@pytest.fixture
def disk(fake_disk_api, monkeypatch):
    """The fake API with a small nested tree, short pages, a required token and the fastest allowed pacing."""
    monkeypatch.setenv('YANDEX_DISK_RESCUE_TOKEN', TOKEN)
    monkeypatch.setenv('YANDEX_DISK_RESCUE_API_INTERVAL_MS', '25')
    fake_disk_api.token = TOKEN
    fake_disk_api.page_cap = 2
    fake_disk_api.add_file('disk:/Photos/2024/a.jpg', b'a' * 1000)
    fake_disk_api.add_file('disk:/Photos/2024/b.jpg', b'b' * 2000)
    fake_disk_api.add_file('disk:/Photos/2024/c.jpg', b'c' * 3000)
    fake_disk_api.add_file('disk:/Photos/old.bmp', b'bitmap', md5=None, sha256=None)
    fake_disk_api.add_file('disk:/Docs/readme.txt', b'read me')
    fake_disk_api.add_dir('disk:/Empty')
    fake_disk_api.add_file('disk:/top.txt', b'')
    return fake_disk_api


def _scan(*args: str):
    return runner.invoke(app, ['scan', *args])


def _manifest_path():
    return state_dir() / 'manifest.sqlite3'


def _rows(root: str = 'disk:/') -> list[Resource]:
    with Manifest.open(_manifest_path()) as manifest:
        return list(manifest.entries('1', root))


def _tree_rows(fake, root: str = 'disk:/') -> list[Resource]:
    """What the manifest should hold for `root` of the fake's tree."""
    rows = [
        Resource(
            path='disk:' + key,
            kind=node.kind,
            size=len(node.content) if node.kind == 'file' else None,
            md5=node.md5,
            sha256=node.sha256,
            modified=node.modified.astimezone(UTC),
        )
        for key, node in fake.tree.items()
        if key != '/'
    ]
    prefix = root.rstrip('/') + '/'
    return sorted((r for r in rows if r.path == root or r.path.startswith(prefix)), key=lambda r: r.path)


def test_first_scan_records_the_tree_and_summarizes_it(disk):
    result = _scan()

    assert result.exit_code == 0, result.output
    assert result.stdout == (
        f'Scanned disk:/: 6 files, 4 folders, {format_size(6013)}\n'
        'Files without a cloud checksum: 1\n'
        '  disk:/Photos/old.bmp\n'
        'First scan of disk:/\n'
        f'Manifest: {_manifest_path()}\n'
        f'Log: {log_path()}\n'
    )
    assert _rows() == _tree_rows(disk)
    assert all(r.headers['authorization'] == f'OAuth {TOKEN}' for r in disk.requests)


def test_a_rescan_lists_exactly_what_was_added_changed_and_removed(disk):
    assert _scan().exit_code == 0
    disk.add_file('disk:/Photos/2024/new.jpg', b'new')
    disk.add_file('disk:/Docs/readme.txt', b'read me again')
    disk.remove('disk:/Photos/old.bmp')

    result = _scan('disk:/')

    assert result.exit_code == 0, result.output
    assert result.stdout == (
        f'Scanned disk:/: 6 files, 4 folders, {format_size(6016)}\n'
        'Files without a cloud checksum: 0\n'
        'Changes since the last scan: 1 added, 1 changed, 1 removed\n'
        '  + disk:/Photos/2024/new.jpg\n'
        '  ~ disk:/Docs/readme.txt\n'
        '  - disk:/Photos/old.bmp\n'
        f'Manifest: {_manifest_path()}\n'
        f'Log: {log_path()}\n'
    )
    assert _rows() == _tree_rows(disk)


def test_entries_whose_names_are_not_valid_unicode_are_reported_and_not_recorded(disk):
    disk.add_file('disk:/Docs/bad\udc80.txt', b'x')
    disk.add_file('disk:/Bad\udc81/inside.txt', b'y')

    result = _scan()

    assert result.exit_code == 0, result.output
    assert (
        'Not recorded, the name is not valid Unicode: 2\n  disk:/Bad\\udc81\n  disk:/Docs/bad\\udc80.txt\n'
    ) in result.stdout
    assert result.stdout.startswith(f'Scanned disk:/: 6 files, 4 folders, {format_size(6013)}\n')
    assert sorted(stderr_lines(result.stderr)) == [
        'disk:/Bad\\udc81 left out: the name is not valid Unicode',
        'disk:/Docs/bad\\udc80.txt left out: the name is not valid Unicode',
    ]
    left_out = {'disk:/Docs/bad\udc80.txt', 'disk:/Bad\udc81', 'disk:/Bad\udc81/inside.txt'}
    assert _rows() == [r for r in _tree_rows(disk) if r.path not in left_out]
    assert 'disk:/Bad\udc81' not in [r.params.get('path') for r in disk.requests]


def test_a_folder_scan_records_the_folder_itself_and_leaves_the_rest_alone(disk):
    assert _scan('/Photos/').exit_code == 0
    assert _rows() == _tree_rows(disk, 'disk:/Photos')

    assert _scan().exit_code == 0
    disk.add_file('disk:/Docs/later.txt', b'not rescanned yet')
    result = _scan('Photos')

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == f'Scanned disk:/Photos: 4 files, 1 folder, {format_size(6006)}'
    assert 'Changes since the last scan: 0 added, 0 changed, 0 removed' in result.stdout
    assert 'disk:/Docs/later.txt' not in {r.path for r in _rows()}
    assert 'disk:/Photos' in {r.path for r in _rows()}


def test_long_path_lists_are_cut_after_twenty_entries(disk):
    for index in range(25):
        disk.add_file(f'disk:/Bulk/{index:02}.raw', b'x', md5=None, sha256=None)

    first = _scan('disk:/Bulk')
    disk.remove('disk:/Bulk/00.raw')
    for index in range(1, 25):
        disk.add_file(f'disk:/Bulk/{index:02}.raw', b'changed', md5=None, sha256=None)
    second = _scan('disk:/Bulk')

    assert first.exit_code == 0, first.output
    assert first.stdout == (
        f'Scanned disk:/Bulk: 25 files, 0 folders, {format_size(25)}\n'
        'Files without a cloud checksum: 25\n'
        + ''.join(f'  disk:/Bulk/{index:02}.raw\n' for index in range(20))
        + '  … and 5 more\n'
        'First scan of disk:/Bulk\n'
        f'Manifest: {_manifest_path()}\n'
        f'Log: {log_path()}\n'
    )
    assert second.exit_code == 0, second.output
    assert second.stdout.splitlines()[23:] == [
        'Changes since the last scan: 0 added, 24 changed, 1 removed',
        *(f'  ~ disk:/Bulk/{index:02}.raw' for index in range(1, 21)),
        '  … and 4 more changed',
        '  - disk:/Bulk/00.raw',
        f'Manifest: {_manifest_path()}',
        f'Log: {log_path()}',
    ]
    logged = log_messages()
    assert logged[-1] == 'scan completed (exit 0)'
    changes = logged.index('Changes since the last scan: 0 added, 24 changed, 1 removed')
    assert logged[changes:-1] == [
        'Changes since the last scan: 0 added, 24 changed, 1 removed',
        *(f'  ~ disk:/Bulk/{index:02}.raw' for index in range(1, 25)),
        '  - disk:/Bulk/00.raw',
    ]


@pytest.mark.parametrize(
    ('args', 'exit_code', 'message'),
    [
        (['disk:/missing'], 1, "Error: Remote path 'disk:/missing' does not exist.\n"),
        (['disk:/top.txt'], 1, "Error: Remote path 'disk:/top.txt' is a file, not a folder.\n"),
        (['trash:/'], 2, "Error: Unsupported remote path 'trash:/': only disk:/ paths can be read.\n"),
    ],
    ids=['missing', 'file', 'other-section'],
)
def test_a_path_that_cannot_be_scanned_fails_without_touching_the_manifest(disk, args, exit_code, message):
    result = _scan(*args)

    assert (result.exit_code, result.stderr) == (exit_code, message)
    assert 'Traceback' not in result.output
    assert not _manifest_path().exists()


def test_scan_without_a_token_names_the_ways_to_provide_one(disk, monkeypatch):
    monkeypatch.delenv('YANDEX_DISK_RESCUE_TOKEN')

    result = _scan()

    assert (result.exit_code, result.stderr) == (1, NO_TOKEN_MESSAGE)
    assert disk.requests == []


def test_an_invalid_api_option_is_a_usage_error(disk):
    result = _scan('--api-concurrency', '0')

    assert result.exit_code == 2
    assert '--api-concurrency' in result.stderr
    assert disk.requests == []


def test_a_folder_failing_past_the_retries_names_it_and_keeps_the_previous_scan(disk):
    assert _scan().exit_code == 0
    previous = _rows()
    disk.add_file('disk:/Photos/2024/new.jpg', b'new')
    disk.inject('GET', '/v1/disk/resources', 503, 503, where={'path': 'disk:/Photos/2024'})

    result = _scan('--api-retries', '1')

    assert result.exit_code == 1
    assert stderr_lines(result.stderr) == [
        'Error: Could not list disk:/Photos/2024: Yandex Disk API returned HTTP 503: Injected HTTP 503 '
        '(after 2 attempts)'
    ]
    assert 'Traceback' not in result.output
    assert _rows() == previous


def _wait_for_listing(fake, path: str, child: subprocess.Popen[str], *, after: int) -> None:
    """Wait until the server logs a listing of `path` among the requests from index `after` on."""
    deadline = time.monotonic() + 60
    while not any(r.path == '/v1/disk/resources' and r.params.get('path') == path for r in fake.requests[after:]):
        if child.poll() is not None:
            pytest.fail(f'scan exited with {child.returncode} before listing {path}: {child.communicate()}')
        if time.monotonic() > deadline:
            pytest.fail(f'scan never listed {path}')
        time.sleep(0.01)


@pytest.mark.parametrize('stop', [signal.SIGINT, signal.SIGKILL], ids=['sigint', 'sigkill'])
def test_a_scan_stopped_mid_walk_keeps_the_previous_scan(disk, stop):
    assert _scan().exit_code == 0
    previous = _rows()
    disk.add_file('disk:/Photos/2024/new.jpg', b'new')
    disk.inject('GET', '/v1/disk/resources', Delay(120), where={'path': 'disk:/Photos/2024'})
    earlier_requests = len(disk.requests)

    child = subprocess.Popen(
        ['yandex-disk-rescue', 'scan'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ.copy()
    )
    try:
        _wait_for_listing(disk, 'disk:/Photos/2024', child, after=earlier_requests)
        child.send_signal(stop)
        stdout, stderr = child.communicate(timeout=60)
    finally:
        child.kill()
        child.communicate()

    if stop == signal.SIGINT:
        assert child.returncode == 130, stderr
        assert stderr_lines(stderr) == ['Interrupted; the manifest holds the last completed scan.']
        assert stdout == ''
    else:
        assert child.returncode == -signal.SIGKILL
    assert _rows() == previous

    rescan = _scan()
    assert rescan.exit_code == 0, rescan.output
    assert '  + disk:/Photos/2024/new.jpg\n' in rescan.stdout
    assert _rows() == _tree_rows(disk)


def _now_a_file(path: str) -> FakeResponse:
    """The listing body of `path` after it was replaced by a file of the same name."""
    return FakeResponse.json(
        200,
        {
            'name': path.rsplit('/')[-1],
            'path': path,
            'type': 'file',
            'size': 1,
            'modified': '2024-05-01T12:30:00+00:00',
        },
    )


@pytest.mark.parametrize('answer', [404, _now_a_file('disk:/Photos/2024')], ids=['deleted', 'replaced-by-a-file'])
def test_a_folder_deleted_during_the_scan_is_reported_and_recorded_as_removed(disk, answer):
    assert _scan().exit_code == 0
    disk.inject('GET', '/v1/disk/resources', answer, where={'path': 'disk:/Photos/2024'})

    result = _scan()

    assert result.exit_code == 0, result.output
    assert result.stdout == (
        f'Scanned disk:/: 3 files, 3 folders, {format_size(13)}\n'
        'Files without a cloud checksum: 1\n'
        '  disk:/Photos/old.bmp\n'
        'Folders that disappeared during the scan: 1\n'
        '  disk:/Photos/2024\n'
        'Changes since the last scan: 0 added, 0 changed, 4 removed\n'
        '  - disk:/Photos/2024\n'
        '  - disk:/Photos/2024/a.jpg\n'
        '  - disk:/Photos/2024/b.jpg\n'
        '  - disk:/Photos/2024/c.jpg\n'
        f'Manifest: {_manifest_path()}\n'
        f'Log: {log_path()}\n'
    )
    assert stderr_lines(result.stderr) == ['disk:/Photos/2024 disappeared during the scan; it is recorded as removed']
    disk.remove('disk:/Photos/2024')
    assert _rows() == _tree_rows(disk)


def _add_vanishing_tree(fake) -> None:
    """`disk:/Gone` lists `Sub` and `a.txt` on its first page (page cap 2) and `b.txt`, `c.txt` on the second."""
    fake.add_file('disk:/Gone/Sub/Deep/x.txt', b'x')
    fake.add_file('disk:/Gone/Sub/y.txt', b'y')
    for name in ('a', 'b', 'c'):
        fake.add_file(f'disk:/Gone/{name}.txt', name.encode())


GONE_SECOND_PAGE = {'path': 'disk:/Gone', 'offset': '2'}


def _last_listing(fake, params: dict[str, str]):
    return [r for r in fake.requests if r.path == '/v1/disk/resources' and params.items() <= r.params.items()][-1]


@pytest.mark.parametrize('stage_batch', [1, 1000], ids=['rows-staged', 'rows-buffered'])
@pytest.mark.parametrize('first_scan', [True, False], ids=['first-scan', 'rescan'])
def test_a_folder_vanishing_on_a_later_page_discards_the_rows_of_sub_folders_listed_before(
    disk, monkeypatch, stage_batch, first_scan
):
    monkeypatch.setattr(scanning, '_STAGE_BATCH', stage_batch)
    _add_vanishing_tree(disk)
    if not first_scan:
        assert _scan().exit_code == 0
    disk.inject('GET', '/v1/disk/resources', Delay(1, then=FakeResponse.error(404)), where=GONE_SECOND_PAGE)

    result = _scan()

    assert result.exit_code == 0, result.output
    vanished_at = _last_listing(disk, GONE_SECOND_PAGE).started + 1
    assert _last_listing(disk, {'path': 'disk:/Gone/Sub'}).started < vanished_at
    assert _last_listing(disk, {'path': 'disk:/Gone/Sub/Deep'}).started < vanished_at
    assert 'Folders that disappeared during the scan: 1\n  disk:/Gone\n' in result.stdout
    disk.remove('disk:/Gone')
    assert _rows() == _tree_rows(disk)
    if first_scan:
        assert 'First scan of disk:/\n' in result.stdout
    else:
        assert 'Changes since the last scan: 0 added, 0 changed, 8 removed\n  - disk:/Gone\n' in result.stdout


def test_a_sub_folder_answering_after_its_parent_vanished_adds_no_rows(disk):
    _add_vanishing_tree(disk)
    disk.inject('GET', '/v1/disk/resources', Delay(1), where={'path': 'disk:/Gone/Sub'})
    disk.inject('GET', '/v1/disk/resources', 404, where=GONE_SECOND_PAGE)

    result = _scan()

    assert result.exit_code == 0, result.output
    assert 'Folders that disappeared during the scan: 1\n  disk:/Gone\n' in result.stdout
    assert not any(r.params.get('path') == 'disk:/Gone/Sub/Deep' for r in disk.requests)
    disk.remove('disk:/Gone')
    assert _rows() == _tree_rows(disk)


def test_a_sub_folder_queued_when_its_parent_vanished_is_never_listed(disk):
    _add_vanishing_tree(disk)
    disk.inject('GET', '/v1/disk/resources', 404, where=GONE_SECOND_PAGE)

    result = _scan('--api-concurrency', '1')

    assert result.exit_code == 0, result.output
    assert 'Folders that disappeared during the scan: 1\n  disk:/Gone\n' in result.stdout
    assert not any(r.params.get('path', '').startswith('disk:/Gone/') for r in disk.requests)


@pytest.mark.parametrize('sub_first', [True, False], ids=['sub-folder-first', 'parent-first'])
def test_a_vanished_sub_folder_is_reported_once_under_its_vanished_parent(disk, sub_first):
    _add_vanishing_tree(disk)
    vanish_later = Delay(1, then=FakeResponse.error(404))
    sub, gone_page = (404, vanish_later) if sub_first else (vanish_later, 404)
    disk.inject('GET', '/v1/disk/resources', sub, where={'path': 'disk:/Gone/Sub'})
    disk.inject('GET', '/v1/disk/resources', gone_page, where=GONE_SECOND_PAGE)

    result = _scan()

    assert result.exit_code == 0, result.output
    assert 'Folders that disappeared during the scan: 1\n  disk:/Gone\nFirst scan' in result.stdout
    disk.remove('disk:/Gone')
    assert _rows() == _tree_rows(disk)


@pytest.mark.parametrize(
    ('answer', 'message'),
    [
        (404, "Error: Remote path 'disk:/Photos' does not exist."),
        (_now_a_file('disk:/Photos'), "Error: Remote path 'disk:/Photos' is a file, not a folder."),
    ],
    ids=['deleted', 'replaced-by-a-file'],
)
def test_the_scanned_folder_itself_vanishing_fails_the_scan(disk, answer, message):
    assert _scan().exit_code == 0
    previous = _rows()
    disk.inject('GET', '/v1/disk/resources', answer, where={'path': 'disk:/Photos', 'offset': '0'})

    result = _scan('disk:/Photos')

    assert (result.exit_code, stderr_lines(result.stderr)) == (1, [message])
    assert _rows() == previous
