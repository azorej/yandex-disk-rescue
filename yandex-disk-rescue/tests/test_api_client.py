"""Tests for `DiskClient` against the fake Disk API server."""

import socket
from datetime import UTC, datetime, timedelta, timezone

import anyio
import pytest
from fake_disk_server import Delay
from yandex_disk_rescue import api, auth

pytestmark = pytest.mark.anyio

TOKEN = 'y0_AgAAAAB-sentinel-token-value'


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


def _client(fake_disk_api, token: str = TOKEN) -> api.DiskClient:
    return api.DiskClient(token, base_url=fake_disk_api.base_url, timeout=auth.API_TIMEOUT_SECONDS)


async def _list(fake_disk_api, path: str) -> list[api.Resource]:
    async with _client(fake_disk_api) as client:
        return [resource async for resource in client.list_folder(path)]


@pytest.fixture
def closed_port_url() -> str:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    return f'http://127.0.0.1:{port}'


def _listing_requests(fake_disk_api) -> list[dict[str, str]]:
    return [r.params for r in fake_disk_api.requests if r.path == '/v1/disk/resources']


async def test_list_folder_pages_through_every_child(fake_disk_api):
    moscow = timezone(timedelta(hours=3))
    fake_disk_api.page_cap = 3
    for index in range(4):
        fake_disk_api.add_dir(f'disk:/Photos/dir{index}', modified=datetime(2023, 1, index + 1, 9, tzinfo=moscow))
    for index in range(5):
        fake_disk_api.add_file(
            f'/Photos/file{index}.jpg', b'x' * index, modified=datetime(2024, 2, 3, 4, 5, 6, tzinfo=moscow)
        )
    fake_disk_api.add_file('Photos/no-checksums.bin', b'abc', md5=None, sha256=None)
    fake_disk_api.add_file('Photos/nested/deeper.txt', b'not a direct child')

    resources = await _list(fake_disk_api, 'disk:/Photos/')

    dirs = [
        api.Resource(f'disk:/Photos/dir{i}', 'dir', None, None, None, datetime(2023, 1, i + 1, 6, tzinfo=UTC))
        for i in range(4)
    ]
    files = [
        api.Resource(
            f'disk:/Photos/file{i}.jpg',
            'file',
            i,
            fake_disk_api.tree[f'/Photos/file{i}.jpg'].md5,
            fake_disk_api.tree[f'/Photos/file{i}.jpg'].sha256,
            datetime(2024, 2, 3, 1, 5, 6, tzinfo=UTC),
        )
        for i in range(5)
    ]
    nested = api.Resource('disk:/Photos/nested', 'dir', None, None, None, datetime(2024, 5, 17, 9, 30, 45, tzinfo=UTC))
    no_checksums = api.Resource('disk:/Photos/no-checksums.bin', 'file', 3, None, None, nested.modified)
    assert resources == [*dirs, *files, nested, no_checksums]
    assert (files[0].md5, files[0].sha256) == (
        'd41d8cd98f00b204e9800998ecf8427e',
        'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
    )
    pages = _listing_requests(fake_disk_api)
    assert [page['offset'] for page in pages] == ['0', '3', '6', '9']
    assert {(page['path'], page['limit'], page['sort']) for page in pages} == {('disk:/Photos', '1000', 'name')}
    assert set(pages[0]['fields'].split(',')) == {
        'type',
        '_embedded.items.path',
        '_embedded.items.type',
        '_embedded.items.size',
        '_embedded.items.md5',
        '_embedded.items.sha256',
        '_embedded.items.modified',
        '_embedded.total',
        '_embedded.limit',
        '_embedded.offset',
    }
    assert {r.headers['authorization'] for r in fake_disk_api.requests} == {f'OAuth {TOKEN}'}


async def test_list_folder_of_an_empty_root_makes_one_request(fake_disk_api):
    assert await _list(fake_disk_api, '/') == []
    assert [page['path'] for page in _listing_requests(fake_disk_api)] == ['disk:/']


async def test_list_folder_of_a_missing_path_names_it(fake_disk_api):
    with pytest.raises(api.RemotePathNotFoundError) as excinfo:
        await _list(fake_disk_api, 'disk:/missing/')

    assert str(excinfo.value) == "Remote path 'disk:/missing' does not exist."
    assert excinfo.value.exit_code == 1


async def test_list_folder_of_a_file_is_not_a_folder(fake_disk_api):
    fake_disk_api.add_file('disk:/notes.txt', b'hello')

    with pytest.raises(api.NotAFolderError) as excinfo:
        await _list(fake_disk_api, 'notes.txt')

    assert str(excinfo.value) == "Remote path 'disk:/notes.txt' is a file, not a folder."


async def test_list_folder_reports_a_status_error_with_the_path(fake_disk_api):
    fake_disk_api.add_dir('disk:/A')
    fake_disk_api.add_dir('disk:/B')
    fake_disk_api.inject('GET', '/v1/disk/resources', 503, where={'path': 'disk:/A'})

    assert await _list(fake_disk_api, 'disk:/B') == []
    with pytest.raises(api.DiskApiError) as excinfo:
        await _list(fake_disk_api, 'disk:/A')
    assert str(excinfo.value) == 'Could not list disk:/A: Yandex Disk API returned HTTP 503: Injected HTTP 503'
    assert await _list(fake_disk_api, 'disk:/A') == []


@pytest.mark.parametrize(
    ('body', 'reason'),
    [
        (b'<html>', 'the body is not JSON'),
        (b'{"type": "link"}', "type is missing or not 'file' or 'dir'"),
        (b'{"type": "dir"}', '_embedded is missing or not an object'),
        (b'{"type": "dir", "_embedded": {"items": [], "total": -1}}', '_embedded.total is missing'),
        (b'{"type": "dir", "_embedded": {"items": {}, "total": 1}}', '_embedded.items is missing or not a list'),
        (
            b'{"type": "dir", "_embedded": {"items": [{"path": "disk:/A/x", "type": "file", '
            b'"modified": "2024-01-01T00:00:00+00:00"}], "total": 1}}',
            '_embedded.items[0].size is missing',
        ),
        (
            b'{"type": "dir", "_embedded": {"items": [{"path": "disk:/A/x", "type": "dir", '
            b'"modified": "2024-01-01T00:00:00"}], "total": 1}}',
            '_embedded.items[0].modified is missing or not an ISO 8601 timestamp with a UTC offset',
        ),
        (
            b'{"type": "dir", "_embedded": {"items": [{"path": "trash:/x", "type": "dir", '
            b'"modified": "2024-01-01T00:00:00Z"}], "total": 1}}',
            '_embedded.items[0].path is not a disk:/ path below the root',
        ),
        (b'{"type": "dir", "_embedded": {"items": [], "total": 5}}', 'an empty page at offset 0 of 5 items'),
    ],
)
async def test_list_folder_rejects_an_unexpected_body(fake_disk_api, body, reason):
    fake_disk_api.route('GET', '/v1/disk/resources', 200, body, {'Content-Type': 'application/json'})

    with pytest.raises(api.DiskApiError) as excinfo:
        await _list(fake_disk_api, 'disk:/A')

    assert str(excinfo.value).startswith(
        f'Could not list disk:/A: Yandex Disk API returned an unexpected response to GET /v1/disk/resources: {reason}'
    )


async def test_list_folder_reports_an_unreachable_api_with_the_path(closed_port_url):
    client = api.DiskClient(TOKEN, base_url=closed_port_url, timeout=auth.API_TIMEOUT_SECONDS)

    with pytest.raises(api.DiskApiError) as excinfo:
        async with client:
            [resource async for resource in client.list_folder('disk:/A')]

    assert str(excinfo.value).startswith(
        f'Could not list disk:/A: Could not reach the Yandex Disk API at {closed_port_url}: '
    )
    assert TOKEN not in str(excinfo.value)


async def test_account_returns_the_uid_and_login(fake_disk_api):
    fake_disk_api.account = {**fake_disk_api.account, 'user': {'login': 'someone.else', 'uid': '42'}}

    async with _client(fake_disk_api) as client:
        account = await client.account()

    assert account == api.Account(uid='42', login='someone.else', total_space=10 * 1024**3, used_space=3 * 1024**3 // 2)


@pytest.mark.parametrize('request_kind', ['account', 'listing'])
async def test_a_token_the_server_does_not_know_is_rejected(fake_disk_api, request_kind):
    fake_disk_api.token = 'the-right-token'

    with pytest.raises(api.TokenRejectedError, match='Yandex rejected the token'):
        async with _client(fake_disk_api, 'a-wrong-token') as client:
            if request_kind == 'account':
                await client.account()
            else:
                [resource async for resource in client.list_folder('disk:/')]

    async with _client(fake_disk_api, 'the-right-token') as client:
        assert (await client.account()).login == 'rescue.tester'


async def test_injected_delays_overlap_and_the_server_records_the_peak(fake_disk_api):
    fake_disk_api.inject('GET', '/v1/disk/', Delay(0.3), Delay(0.3))

    async with _client(fake_disk_api) as client, anyio.create_task_group() as group:
        group.start_soon(client.account)
        group.start_soon(client.account)

    assert fake_disk_api.peak_in_flight == 2
    assert len(fake_disk_api.requests) == 2


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('disk:/A/', 'disk:/A'),
        ('disk:/A/b c', 'disk:/A/b c'),
        ('/A', 'disk:/A'),
        ('A', 'disk:/A'),
        ('A/B/', 'disk:/A/B'),
        ('notes:2024', 'disk:/notes:2024'),
        ('disk:', 'disk:/'),
        ('disk:/', 'disk:/'),
        ('/', 'disk:/'),
        ('', 'disk:/'),
    ],
)
def test_normalize_remote_path(raw, expected):
    assert api.normalize_remote_path(raw) == expected


@pytest.mark.parametrize('raw', ['trash:/', 'trash:', 'app:/backup', 'photounlim:/x'])
def test_normalize_remote_path_rejects_other_sections_as_invalid_input(raw):
    with pytest.raises(api.UnsupportedPathError) as excinfo:
        api.normalize_remote_path(raw)

    assert str(excinfo.value) == f"Unsupported remote path '{raw}': only disk:/ paths can be read."
    assert excinfo.value.exit_code == 2
