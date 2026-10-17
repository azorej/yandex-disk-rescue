"""Tests for `DiskClient` against the fake Disk API server."""

import dataclasses
import itertools
from datetime import UTC, datetime, timedelta, timezone

import anyio
import pytest
from fake_disk_server import Delay, FakeResponse
from yandex_disk_rescue import api

pytestmark = pytest.mark.anyio

TOKEN = 'y0_AgAAAAB-sentinel-token-value'


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


# The production policy with intervals and backoff shrunk so retries cost milliseconds.
FAST = api.ApiLimits(interval=0.001, concurrency=7, timeout=5, retries=5, backoff_initial=0.001, backoff_max=0.01)


def _client(fake_disk_api, token: str = TOKEN, **limits: float) -> api.DiskClient:
    return api.DiskClient(token, base_url=fake_disk_api.base_url, limits=dataclasses.replace(FAST, **limits))


async def _list(fake_disk_api, path: str, **limits: float) -> list[api.Resource]:
    async with _client(fake_disk_api, **limits) as client:
        return [resource async for resource in client.list_folder(path)]


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

    assert await _list(fake_disk_api, 'disk:/B', retries=0) == []
    with pytest.raises(api.DiskApiError) as excinfo:
        await _list(fake_disk_api, 'disk:/A', retries=0)
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


@pytest.mark.parametrize(('retries', 'suffix'), [(0, ''), (2, ' (after 3 attempts)')])
async def test_list_folder_reports_an_unreachable_api_with_the_path(closed_port_url, retries, suffix):
    client = api.DiskClient(TOKEN, base_url=closed_port_url, limits=dataclasses.replace(FAST, retries=retries))

    with pytest.raises(api.DiskApiError) as excinfo:
        async with client:
            [resource async for resource in client.list_folder('disk:/A')]

    message = str(excinfo.value)
    assert message.startswith(f'Could not list disk:/A: Could not reach the Yandex Disk API at {closed_port_url}: ')
    assert message.endswith(suffix)
    assert ('attempts)' in message) == bool(suffix)
    assert TOKEN not in message


def _starts(fake_disk_api) -> list[float]:
    return [r.started for r in fake_disk_api.requests]


async def test_a_retry_after_pauses_the_next_request_and_the_listing_succeeds(fake_disk_api):
    fake_disk_api.add_file('disk:/A/x.txt', b'x')
    fake_disk_api.inject('GET', '/v1/disk/resources', FakeResponse.error(429, {'Retry-After': '1'}))

    resources = await _list(fake_disk_api, 'disk:/A')

    assert [r.path for r in resources] == ['disk:/A/x.txt']
    first, second = _starts(fake_disk_api)
    assert second - first >= 0.95


async def test_throttling_statuses_are_retried_until_the_listing_succeeds(fake_disk_api):
    fake_disk_api.add_file('disk:/A/x.txt', b'x')
    fake_disk_api.inject('GET', '/v1/disk/resources', 503, 503, 503)

    assert [r.path for r in await _list(fake_disk_api, 'disk:/A', retries=3)] == ['disk:/A/x.txt']
    assert len(fake_disk_api.requests) == 4


@pytest.mark.parametrize('status', [503, 500, 429, 408])
async def test_retries_run_out_with_the_last_failure_and_the_attempt_count(fake_disk_api, status):
    fake_disk_api.add_dir('disk:/A')
    fake_disk_api.inject('GET', '/v1/disk/resources', status, status, status, status)

    with pytest.raises(api.DiskApiError) as excinfo:
        await _list(fake_disk_api, 'disk:/A', retries=2)

    assert str(excinfo.value) == (
        f'Could not list disk:/A: Yandex Disk API returned HTTP {status}: Injected HTTP {status} (after 3 attempts)'
    )
    assert len(fake_disk_api.requests) == 3


@pytest.mark.parametrize('status', [400, 401, 403, 404, 409])
async def test_other_client_errors_are_not_retried(fake_disk_api, status):
    fake_disk_api.add_dir('disk:/A')
    fake_disk_api.inject('GET', '/v1/disk/resources', status)

    with pytest.raises((api.DiskApiError, api.TokenRejectedError, api.RemotePathNotFoundError)) as excinfo:
        await _list(fake_disk_api, 'disk:/A')

    assert 'attempts' not in str(excinfo.value)
    assert len(fake_disk_api.requests) == 1


async def test_a_timed_out_request_is_retried(fake_disk_api):
    fake_disk_api.add_file('disk:/A/x.txt', b'x')
    fake_disk_api.inject('GET', '/v1/disk/resources', Delay(0.6))

    assert [r.path for r in await _list(fake_disk_api, 'disk:/A', timeout=0.2)] == ['disk:/A/x.txt']
    assert len(fake_disk_api.requests) == 2


async def test_a_request_that_always_times_out_names_the_timeout(fake_disk_api):
    fake_disk_api.add_dir('disk:/A')
    fake_disk_api.inject('GET', '/v1/disk/resources', Delay(0.6), Delay(0.6), Delay(0.6))

    with pytest.raises(api.DiskApiError) as excinfo:
        await _list(fake_disk_api, 'disk:/A', timeout=0.2, retries=2)

    assert str(excinfo.value) == (
        f'Could not list disk:/A: Could not reach the Yandex Disk API at {fake_disk_api.base_url}: '
        'the request timed out after 0.2 s (ReadTimeout) (after 3 attempts)'
    )


async def test_concurrent_requests_share_one_pace_and_one_concurrency_cap(fake_disk_api):
    folders = [f'disk:/F{index:02}' for index in range(20)]
    for folder in folders:
        fake_disk_api.add_dir(folder)
        fake_disk_api.inject('GET', '/v1/disk/resources', Delay(0.5), where={'path': folder})

    async with _client(fake_disk_api, interval=0.05, concurrency=7) as client, anyio.create_task_group() as group:
        for folder in folders:
            group.start_soon(_drain, client, folder)

    starts = sorted(_starts(fake_disk_api))
    assert len(starts) == 20
    assert min(later - earlier for earlier, later in itertools.pairwise(starts)) >= 0.04
    assert fake_disk_api.peak_in_flight == 7


async def _drain(client: api.DiskClient, folder: str) -> None:
    assert [resource async for resource in client.list_folder(folder)] == []


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
