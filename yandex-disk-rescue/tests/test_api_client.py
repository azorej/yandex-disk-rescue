"""Tests for `DiskClient` against the fake Disk API server."""

import dataclasses
from datetime import UTC, datetime, timedelta, timezone

import anyio
import httpx
import pytest
from fake_disk_server import Delay, FakeResponse, serve
from yandex_disk_rescue import api

pytestmark = pytest.mark.anyio

TOKEN = 'y0_AgAAAAB-sentinel-token-value'


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


async def test_resource_returns_a_file_or_folder_as_the_api_names_it(fake_disk_api):
    fake_disk_api.add_dir('disk:/Photos', modified=datetime(2023, 6, 7, 8, 9, 10, tzinfo=UTC))
    fake_disk_api.add_file(
        'disk:/Photos/a.jpg', b'abc', sha256=None, modified=datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    )

    async with _client(fake_disk_api) as client:
        folder = await client.resource('Photos/')
        file = await client.resource('/Photos/a.jpg')
        with pytest.raises(api.RemotePathNotFoundError, match=r"^Remote path 'disk:/Photos/b\.jpg' does not exist\.$"):
            await client.resource('disk:/Photos/b.jpg')

    assert folder == api.Resource('disk:/Photos', 'dir', None, None, None, datetime(2023, 6, 7, 8, 9, 10, tzinfo=UTC))
    assert file == api.Resource(
        'disk:/Photos/a.jpg',
        'file',
        3,
        '900150983cd24fb0d6963f7d28e17f72',
        None,
        datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
    )
    assert {r.params['fields'] for r in fake_disk_api.requests} == {'path,type,size,md5,sha256,modified'}


async def test_resource_reports_a_malformed_body_with_the_path(fake_disk_api):
    fake_disk_api.route_json('GET', '/v1/disk/resources', 200, {'path': 'disk:/A', 'type': 'dir'})

    async with _client(fake_disk_api) as client:
        with pytest.raises(api.DiskApiError) as excinfo:
            await client.resource('disk:/A')

    assert str(excinfo.value) == (
        'Could not read disk:/A: Yandex Disk API returned an unexpected response to GET /v1/disk/resources: '
        'modified is missing or not an ISO 8601 timestamp with a UTC offset.'
    )


async def test_download_link_returns_the_href_of_a_file(fake_disk_api):
    fake_disk_api.token = TOKEN
    fake_disk_api.add_file('disk:/Photos/a.jpg', b'abc')

    async with _client(fake_disk_api) as client:
        first = await client.download_link('/Photos/a.jpg')
        second = await client.download_link('disk:/Photos/a.jpg')

    assert first == f'{fake_disk_api.base_url}/download?path=disk%3A%2FPhotos%2Fa.jpg&link=1'
    assert second.endswith('&link=2')
    link_requests = [r for r in fake_disk_api.requests if r.path == '/v1/disk/resources/download']
    assert [r.params for r in link_requests] == [{'path': 'disk:/Photos/a.jpg', 'fields': 'href,method,templated'}] * 2


@pytest.mark.parametrize(
    ('status', 'error', 'message'),
    [
        (404, api.RemotePathNotFoundError, "Remote path 'disk:/a.bin' does not exist."),
        (
            403,
            api.DownloadRefusedError,
            'Could not download disk:/a.bin: Yandex Disk API returned HTTP 403: Injected HTTP 403',
        ),
        (
            409,
            api.DownloadRefusedError,
            'Could not download disk:/a.bin: Yandex Disk API returned HTTP 409: Injected HTTP 409',
        ),
        (401, api.TokenRejectedError, 'Yandex rejected the token'),
    ],
)
async def test_download_link_maps_client_errors_without_retrying(fake_disk_api, status, error, message):
    fake_disk_api.add_file('disk:/a.bin', b'abc')
    fake_disk_api.inject('GET', '/v1/disk/resources/download', status)

    async with _client(fake_disk_api) as client:
        with pytest.raises(error) as excinfo:
            await client.download_link('disk:/a.bin')

    assert str(excinfo.value).startswith(message)
    assert excinfo.value.exit_code == 1
    assert len(fake_disk_api.requests) == 1


async def test_download_link_of_a_missing_path_or_a_folder_is_refused(fake_disk_api):
    fake_disk_api.add_dir('disk:/Photos')

    async with _client(fake_disk_api) as client:
        with pytest.raises(api.RemotePathNotFoundError, match=r"^Remote path 'disk:/gone\.bin' does not exist\.$"):
            await client.download_link('disk:/gone.bin')
        with pytest.raises(api.DownloadRefusedError) as excinfo:
            await client.download_link('disk:/Photos')

    assert str(excinfo.value) == (
        'Could not download disk:/Photos: Yandex Disk API returned HTTP 400: Resource is a folder.'
    )


async def test_download_link_retries_throttling(fake_disk_api):
    fake_disk_api.add_file('disk:/a.bin', b'abc')
    fake_disk_api.inject('GET', '/v1/disk/resources/download', 503, 429)

    async with _client(fake_disk_api) as client:
        assert (await client.download_link('disk:/a.bin')).endswith('&link=1')
    assert len(fake_disk_api.requests) == 3


_SIGNED = 'https://downloader.example/disk/s3cr3t-signature?tknv=s3cr3t-query'


@pytest.mark.parametrize(
    ('body', 'reason'),
    [
        ({'href': _SIGNED, 'method': 'GET', 'templated': True}, 'templated is missing or not false'),
        ({'href': _SIGNED, 'method': 'GET'}, 'templated is missing or not false'),
        ({'href': _SIGNED, 'method': 'POST', 'templated': False}, "method is missing or not 'GET'"),
        ({'method': 'GET', 'templated': False}, 'href is missing or not a non-empty string'),
        (
            {'href': 'ftp://downloader.example/s3cr3t-signature', 'method': 'GET', 'templated': False},
            'href is not an http(s) URL',
        ),
    ],
)
async def test_download_link_rejects_an_unexpected_body_without_quoting_the_href(fake_disk_api, body, reason):
    fake_disk_api.route_json('GET', '/v1/disk/resources/download', 200, body)

    async with _client(fake_disk_api) as client:
        with pytest.raises(api.DiskApiError) as excinfo:
            await client.download_link('disk:/a.bin')

    assert str(excinfo.value) == (
        'Could not download disk:/a.bin: Yandex Disk API returned an unexpected response to '
        f'GET /v1/disk/resources/download: {reason}.'
    )
    assert 's3cr3t' not in str(excinfo.value)


_JPEG = b'\xff\xd8\xff\xe0 a reduced photo'


async def test_preview_of_a_photo_is_fetched_with_the_token_and_a_file_without_one_has_none(fake_disk_api):
    fake_disk_api.token = TOKEN
    modified = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    fake_disk_api.add_file('disk:/Photos/a.jpg', b'the original', modified=modified, preview=_JPEG)
    fake_disk_api.add_file('disk:/Photos/b.zip', b'an archive')

    async with _client(fake_disk_api) as client:
        photo = await client.preview('/Photos/a.jpg')
        archive = await client.preview('disk:/Photos/b.zip')
        assert photo is not None
        preview = await client.fetch_preview(photo.link)
        with pytest.raises(api.RemotePathNotFoundError, match=r"^Remote path 'disk:/gone\.jpg' does not exist\.$"):
            await client.preview('disk:/gone.jpg')

    assert archive is None
    node = fake_disk_api.tree['/Photos/a.jpg']
    assert photo.resource == api.Resource('disk:/Photos/a.jpg', 'file', 12, node.md5, node.sha256, modified)
    assert photo.link.size_name == 'XXXL'
    assert photo.link.url not in repr(photo)
    assert preview == api.Preview(content=_JPEG, content_type='image/jpeg')
    resource_requests = [r.params for r in fake_disk_api.requests if r.path == '/v1/disk/resources']
    assert resource_requests[0] == {
        'path': 'disk:/Photos/a.jpg',
        'fields': 'path,type,size,md5,sha256,modified,preview',
        'preview_size': 'XXXL',
    }
    (fetch,) = [r for r in fake_disk_api.requests if r.path == '/preview']
    assert fetch.headers['authorization'] == f'OAuth {TOKEN}'
    async with httpx.AsyncClient() as plain:
        without_token = await plain.get(photo.link.url)
    assert (without_token.status_code, without_token.headers['content-type']) == (403, 'text/html')


@pytest.mark.parametrize(
    ('url', 'allowed'),
    [
        ('https://downloader.disk.yandex.ru/preview/s3cr3t?size=XXXL', True),
        ('https://downloader.disk.yandex.ru:443/preview/s3cr3t', True),
        ('API_ORIGIN/preview?path=disk%3A%2Fa.jpg&size=XXXL', True),
        ('http://downloader.disk.yandex.ru/preview/s3cr3t', False),
        ('https://downloader.disk.yandex.ru:8443/preview/s3cr3t', False),
        ('https://s1klg.storage.yandex.net/preview/s3cr3t', False),
        ('https://yandex.ru/preview/s3cr3t', False),
        ('https://evil-downloader.disk.yandex.ru.example/preview/s3cr3t', False),
        ('https://example.com/preview/s3cr3t', False),
        ('OTHER_ORIGIN/preview?path=disk%3A%2Fa.jpg&size=XXXL', False),
    ],
)
async def test_the_token_goes_only_to_the_preview_host_and_the_api_origin(fake_disk_api, url, allowed):
    fake_disk_api.add_file('disk:/a.jpg', b'the original', preview=_JPEG)

    with serve() as other:
        other.add_file('disk:/a.jpg', b'the original', preview=_JPEG)
        url = url.replace('API_ORIGIN', fake_disk_api.base_url).replace('OTHER_ORIGIN', other.base_url)
        async with _client(fake_disk_api) as client:
            assert client.may_send_token_to(url) is allowed
            if not allowed:
                with pytest.raises(api.PreviewRefusedError) as excinfo:
                    await client.fetch_preview(api.PreviewLink(url=url, size_name='XXXL'))
            elif url.startswith(fake_disk_api.base_url):
                assert (await client.fetch_preview(api.PreviewLink(url=url, size_name='XXXL'))).content == _JPEG

    assert other.requests == []
    if not allowed:
        origin = str(httpx.URL(url).copy_with(path='/', query=None)).removesuffix('/')
        assert str(excinfo.value) == (
            f'the preview link points to {origin}, not to the Yandex Disk preview host '
            'https://downloader.disk.yandex.ru; the token is not sent there'
        )
        assert 's3cr3t' not in str(excinfo.value)
        assert fake_disk_api.requests == []


async def test_a_redirecting_preview_host_is_not_followed_and_the_token_never_leaves(fake_disk_api):
    fake_disk_api.add_file('disk:/a.jpg', b'the original', preview=_JPEG)

    with serve() as other:
        other.add_file('disk:/a.jpg', b'the original', preview=_JPEG)
        fake_disk_api.redirect_previews = other.base_url
        async with _client(fake_disk_api) as client:
            found = await client.preview('disk:/a.jpg')
            assert found is not None
            with pytest.raises(api.PreviewRefusedError) as excinfo:
                await client.fetch_preview(found.link)

    assert str(excinfo.value) == 'the preview host redirected (HTTP 302); not followed'
    assert other.requests == []
    assert [r.path for r in fake_disk_api.requests] == ['/v1/disk/resources', '/preview']


@pytest.mark.parametrize(
    ('preview', 'preview_type', 'reason'),
    [
        (_JPEG, 'text/html', 'not an image (text/html)'),
        (_JPEG, 'image/png; charset=binary', None),
        (b'p' * 5000, 'image/jpeg', 'larger than 1000 B'),
        (b'p' * 1000, 'image/jpeg', None),
        (None, 'image/jpeg', 'the preview host returned HTTP 404'),
    ],
)
async def test_a_preview_that_is_not_an_image_or_too_large_is_refused(
    fake_disk_api, monkeypatch, preview, preview_type, reason
):
    monkeypatch.setattr('yandex_disk_rescue.api.client.PREVIEW_MAX_BYTES', 1000)
    fake_disk_api.add_file('disk:/a.jpg', b'the original', preview=preview, preview_type=preview_type)
    link = api.PreviewLink(url=f'{fake_disk_api.base_url}/preview?path=disk%3A%2Fa.jpg&size=XXXL', size_name='XXXL')

    async with _client(fake_disk_api) as client:
        if reason is None:
            assert await client.fetch_preview(link) == api.Preview(preview, preview_type.partition(';')[0])
        else:
            with pytest.raises(api.PreviewRefusedError) as excinfo:
                await client.fetch_preview(link)
            assert str(excinfo.value) == reason

    assert len(fake_disk_api.requests) == 1


async def test_a_failing_preview_request_is_retried_like_an_api_request(fake_disk_api):
    fake_disk_api.add_file('disk:/a.jpg', b'the original', preview=_JPEG)
    fake_disk_api.inject('GET', '/preview', 503)
    link = api.PreviewLink(url=f'{fake_disk_api.base_url}/preview?path=disk%3A%2Fa.jpg&size=XXXL', size_name='XXXL')

    async with _client(fake_disk_api, retries=2, timeout=0.2) as client:
        assert (await client.fetch_preview(link)).content == _JPEG
        fake_disk_api.inject('GET', '/preview', 503, 503, 503)
        with pytest.raises(api.DiskApiError) as throttled:
            await client.fetch_preview(link)
        fake_disk_api.inject('GET', '/preview', Delay(0.6), Delay(0.6), Delay(0.6))
        with pytest.raises(api.DiskApiError) as timed_out:
            await client.fetch_preview(link)

    assert str(throttled.value) == 'Could not fetch the preview: the preview host returned HTTP 503 (after 3 attempts)'
    assert str(timed_out.value) == (
        f'Could not fetch the preview: Could not reach the Yandex Disk preview host {fake_disk_api.base_url}: '
        'the request timed out after 0.2 s (ReadTimeout) (after 3 attempts)'
    )
    assert TOKEN not in str(timed_out.value)
    assert len(fake_disk_api.requests) == 8


async def test_a_preview_link_that_is_not_an_http_url_is_an_unexpected_response(fake_disk_api):
    fake_disk_api.route_json(
        'GET',
        '/v1/disk/resources',
        200,
        {
            'path': 'disk:/a.jpg',
            'type': 'file',
            'size': 3,
            'modified': '2024-01-02T03:04:05+00:00',
            'preview': 'ftp://downloader.example/s3cr3t',
        },
    )

    async with _client(fake_disk_api) as client:
        with pytest.raises(api.DiskApiError) as excinfo:
            await client.preview('disk:/a.jpg')

    assert str(excinfo.value) == (
        'Could not read the preview of disk:/a.jpg: Yandex Disk API returned an unexpected response to '
        'GET /v1/disk/resources: preview is not an http(s) URL.'
    )


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
    assert second - first >= 0.9


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


@pytest.mark.parametrize(
    ('status', 'error'),
    [
        (400, api.DiskApiError),
        (401, api.TokenRejectedError),
        (403, api.TokenRejectedError),
        (404, api.RemotePathNotFoundError),
        (409, api.DiskApiError),
    ],
)
async def test_other_client_errors_are_not_retried(fake_disk_api, status, error):
    fake_disk_api.add_dir('disk:/A')
    fake_disk_api.inject('GET', '/v1/disk/resources', status)

    with pytest.raises(error) as excinfo:
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
    assert starts[-1] - starts[0] >= 0.9 * 0.05 * (len(starts) - 1)
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
