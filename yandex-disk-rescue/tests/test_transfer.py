"""Tests for `ContentClient` against the fake download host, writing into real files."""

import asyncio
import os
import random
import time
from pathlib import Path

import httpx
import pytest
from fake_disk_server import Delay, FakeResponse, Truncate
from yandex_disk_rescue import api

pytestmark = pytest.mark.anyio

TOKEN = 'y0_AgAAAAB-sentinel-token-value'
FAST = api.ApiLimits(interval=0.001, concurrency=7, timeout=5, retries=0, backoff_initial=0.001, backoff_max=0.01)
CONTENT = random.Random(7).randbytes(300 * 1024 + 123)
SIZE = len(CONTENT)


class PartialFile:
    """A real file the fetch writes into with `pwrite`, recording the offset of every write."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offsets: list[int] = []
        self._fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)

    async def write(self, offset: int, chunk: bytes) -> None:
        self.offsets.append(offset)
        os.pwrite(self._fd, chunk, offset)

    def close(self) -> None:
        os.close(self._fd)

    def read(self) -> bytes:
        return self.path.read_bytes()


@pytest.fixture
def remote_file(fake_disk_api) -> str:
    fake_disk_api.token = TOKEN
    fake_disk_api.add_file('disk:/a.bin', CONTENT)
    return 'disk:/a.bin'


@pytest.fixture
def partial(tmp_path) -> PartialFile:
    target = PartialFile(tmp_path / 'a.bin.partial')
    yield target
    target.close()


async def _link(fake_disk_api, path: str = 'disk:/a.bin') -> str:
    async with api.DiskClient(TOKEN, base_url=fake_disk_api.base_url, limits=FAST) as client:
        return await client.download_link(path)


async def _fetch(
    fake_disk_api, partial: PartialFile, *, start: int = 0, end: int = SIZE, size: int = SIZE, timeout: float = 5
) -> str:
    href = await _link(fake_disk_api)
    async with api.ContentClient(timeout=timeout) as content:
        await content.fetch(href, start=start, end=end, size=size, write=partial.write)
    return href


def _content_requests(fake_disk_api) -> list:
    return [r for r in fake_disk_api.requests if r.path in ('/download', '/storage')]


def _assert_contiguous(partial: PartialFile, start: int, end: int) -> None:
    assert partial.offsets[0] == start
    assert partial.offsets == sorted(partial.offsets)
    assert len(partial.offsets) > 1
    assert partial.read()[start:end] == CONTENT[start:end]


async def test_a_full_fetch_writes_the_exact_bytes_without_the_token(fake_disk_api, remote_file, partial):
    await _fetch(fake_disk_api, partial)

    assert partial.read() == CONTENT
    _assert_contiguous(partial, 0, SIZE)
    (link_request,) = [r for r in fake_disk_api.requests if r.path == '/v1/disk/resources/download']
    assert link_request.headers['authorization'] == f'OAuth {TOKEN}'
    (request,) = _content_requests(fake_disk_api)
    assert request.path == '/download'
    assert 'authorization' not in request.headers
    assert 'range' not in request.headers


@pytest.mark.parametrize(('start', 'end'), [(100_000, SIZE), (0, 70_000), (65_536, 200_001)])
async def test_a_range_is_requested_and_written_at_its_offsets(fake_disk_api, remote_file, partial, start, end):
    await _fetch(fake_disk_api, partial, start=start, end=end)

    (request,) = _content_requests(fake_disk_api)
    assert request.headers['range'] == f'bytes={start}-{end - 1}'
    assert len(partial.read()) == end
    _assert_contiguous(partial, start, end)


async def test_an_empty_file_is_fetched_without_a_range(fake_disk_api, partial):
    fake_disk_api.add_file('disk:/empty', b'')
    href = await _link(fake_disk_api, 'disk:/empty')

    async with api.ContentClient(timeout=5) as content:
        await content.fetch(href, start=0, end=0, size=0, write=partial.write)

    assert partial.offsets == []
    assert 'range' not in _content_requests(fake_disk_api)[0].headers


async def test_a_redirect_to_the_storage_host_is_followed_with_the_range(fake_disk_api, remote_file, partial):
    fake_disk_api.redirect_downloads = True

    await _fetch(fake_disk_api, partial, start=10, end=SIZE)

    assert partial.read()[10:] == CONTENT[10:]
    requests = _content_requests(fake_disk_api)
    assert [r.path for r in requests] == ['/download', '/storage']
    assert requests[0].query == requests[1].query
    assert {r.headers['range'] for r in requests} == {f'bytes=10-{SIZE - 1}'}
    assert not any('authorization' in r.headers for r in requests)


async def test_a_corrupted_byte_is_written_as_served(fake_disk_api, remote_file, partial):
    fake_disk_api.corrupt('disk:/a.bin', 4321)

    await _fetch(fake_disk_api, partial)

    served = partial.read()
    assert served[4321] == CONTENT[4321] ^ 0xFF
    assert served[:4321] + served[4322:] == CONTENT[:4321] + CONTENT[4322:]
    async with api.DiskClient(TOKEN, base_url=fake_disk_api.base_url, limits=FAST) as client:
        assert (await client.resource('disk:/a.bin')).md5 == fake_disk_api.tree['/a.bin'].md5


async def test_a_full_fetch_from_a_host_ignoring_ranges_succeeds(fake_disk_api, remote_file, partial):
    fake_disk_api.ignore_range = True

    await _fetch(fake_disk_api, partial)

    assert partial.read() == CONTENT


@pytest.mark.parametrize('status', [401, 403, 410])
async def test_an_expired_link_names_its_status_and_a_new_link_works(fake_disk_api, remote_file, partial, status):
    old = await _link(fake_disk_api)
    fake_disk_api.expire_links(status)

    async with api.ContentClient(timeout=5) as content:
        with pytest.raises(api.LinkExpiredError) as excinfo:
            await content.fetch(old, start=0, end=SIZE, size=SIZE, write=partial.write)
        await content.fetch(await _link(fake_disk_api), start=0, end=SIZE, size=SIZE, write=partial.write)

    assert excinfo.value.status == status
    assert excinfo.value.reason == f'the download host 127.0.0.1 rejected the link (HTTP {status})'
    assert partial.read() == CONTENT


async def test_a_truncated_body_keeps_the_bytes_written_before_it(fake_disk_api, remote_file, partial):
    fake_disk_api.inject('GET', '/download', Truncate(after_bytes=200_000))

    with pytest.raises(api.TransientTransferError) as excinfo:
        await _fetch(fake_disk_api, partial)

    assert excinfo.value.reason.startswith('the download failed: ')
    assert partial.read() == CONTENT[:200_000]
    await _fetch(fake_disk_api, partial, start=200_000)
    assert partial.read() == CONTENT


@pytest.mark.parametrize(
    ('fault', 'error', 'reason'),
    [
        (503, api.TransientTransferError, 'the download host 127.0.0.1 returned HTTP 503'),
        (500, api.TransientTransferError, 'the download host 127.0.0.1 returned HTTP 500'),
        (429, api.TransientTransferError, 'the download host 127.0.0.1 returned HTTP 429'),
        (408, api.TransientTransferError, 'the download host 127.0.0.1 returned HTTP 408'),
        (404, api.TransferRefusedError, 'the download host 127.0.0.1 returned HTTP 404'),
        (400, api.TransferRefusedError, 'the download host 127.0.0.1 returned HTTP 400'),
        (
            FakeResponse(206, b'x' * 10, {'Content-Range': 'bytes 0-9/100'}),
            api.RangeNotHonouredError,
            f'the download host 127.0.0.1 sent Content-Range bytes 0-9/100 for bytes 0-{SIZE - 1} of {SIZE}',
        ),
        (
            FakeResponse(206, b'x' * 10, {'Content-Range': 'secret?tknv=1'}),
            api.RangeNotHonouredError,
            f'the download host 127.0.0.1 sent Content-Range a malformed or missing one for bytes 0-{SIZE - 1} '
            f'of {SIZE}',
        ),
        (
            FakeResponse(200, CONTENT + b'!'),
            api.TooManyBytesError,
            f'the download host sent more than the {SIZE} bytes requested',
        ),
        (
            FakeResponse(200, CONTENT[:1000]),
            api.TransientTransferError,
            f'the download ended after 1000 of {SIZE} bytes',
        ),
        (
            FakeResponse(200, CONTENT, {'Content-Encoding': 'gzip'}),
            api.TransferRefusedError,
            'the download host 127.0.0.1 sent Content-Encoding gzip',
        ),
    ],
)
async def test_each_failing_answer_raises_its_own_error(fake_disk_api, remote_file, partial, fault, error, reason):
    fake_disk_api.inject('GET', '/download', fault)

    with pytest.raises(api.TransferError) as excinfo:
        await _fetch(fake_disk_api, partial)

    assert type(excinfo.value) is error
    assert excinfo.value.reason == reason
    assert str(excinfo.value) == reason
    assert 'link=' not in reason


async def test_a_retry_after_is_carried_by_the_transient_error(fake_disk_api, remote_file, partial):
    fake_disk_api.inject('GET', '/download', FakeResponse.error(503, {'Retry-After': '7'}))

    with pytest.raises(api.TransientTransferError) as excinfo:
        await _fetch(fake_disk_api, partial)

    assert excinfo.value.retry_after == 7


async def test_a_host_ignoring_a_range_is_not_appended_to_the_prefix(fake_disk_api, remote_file, partial):
    fake_disk_api.ignore_range = True

    with pytest.raises(api.RangeNotHonouredError) as excinfo:
        await _fetch(fake_disk_api, partial, start=100)

    assert excinfo.value.reason == 'the download host 127.0.0.1 ignored the Range header (HTTP 200)'
    assert partial.offsets == []


async def test_a_resumed_range_past_a_shrunk_file_is_refused(fake_disk_api, remote_file, partial):
    fake_disk_api.add_file('disk:/a.bin', CONTENT[:1000])

    with pytest.raises(api.TransferRefusedError) as excinfo:
        await _fetch(fake_disk_api, partial, start=5000)

    assert excinfo.value.reason == 'the download host 127.0.0.1 returned HTTP 416'
    (request,) = _content_requests(fake_disk_api)
    assert request.headers['range'] == f'bytes=5000-{SIZE - 1}'


async def test_a_response_that_stalls_past_the_timeout_is_transient(fake_disk_api, remote_file, partial):
    fake_disk_api.inject('GET', '/download', Delay(0.6))

    with pytest.raises(api.TransientTransferError) as excinfo:
        await _fetch(fake_disk_api, partial, timeout=0.2)

    assert excinfo.value.reason == 'the download stalled: no data for 0.2 s'


async def test_a_slow_but_steady_body_never_trips_the_stall_timeout(fake_disk_api, partial):
    slow = random.Random(5).randbytes(24 * 64 * 1024)
    fake_disk_api.token = TOKEN
    fake_disk_api.add_file('disk:/slow.bin', slow)
    fake_disk_api.chunk_delay = 0.1
    href = await _link(fake_disk_api, 'disk:/slow.bin')

    async with api.ContentClient(timeout=1.5) as content:
        began = time.monotonic()
        await content.fetch(href, start=0, end=len(slow), size=len(slow), write=partial.write)
        elapsed = time.monotonic() - began

    assert partial.read() == slow
    assert elapsed > 1.5


async def test_time_spent_writing_does_not_count_against_the_stall_timeout(fake_disk_api, remote_file, partial):
    async def slow_write(offset: int, chunk: bytes) -> None:
        await asyncio.sleep(0.3)
        await partial.write(offset, chunk)

    href = await _link(fake_disk_api)
    async with api.ContentClient(timeout=0.2) as content:
        await content.fetch(href, start=0, end=SIZE, size=SIZE, write=slow_write)

    assert partial.read() == CONTENT
    assert len(partial.offsets) > 1


async def test_an_error_from_write_propagates_and_no_further_chunk_is_written(fake_disk_api, remote_file, partial):
    class DiskFull(Exception):
        pass

    async def write_once(offset: int, chunk: bytes) -> None:
        if partial.offsets:
            raise DiskFull
        await partial.write(offset, chunk)

    href = await _link(fake_disk_api)
    async with api.ContentClient(timeout=5) as content:
        with pytest.raises(DiskFull):
            await content.fetch(href, start=0, end=SIZE, size=SIZE, write=write_once)

    assert partial.offsets == [0]
    assert 0 < len(partial.read()) < SIZE
    assert CONTENT.startswith(partial.read())


async def test_an_unreachable_host_is_transient_and_hides_the_link(closed_port_url, partial):
    href = f'{closed_port_url}/disk/s3cr3t-signature?tknv=s3cr3t-query'

    async with api.ContentClient(timeout=5) as content:
        with pytest.raises(api.TransientTransferError) as excinfo:
            await content.fetch(href, start=0, end=10, size=10, write=partial.write)

    assert excinfo.value.reason.startswith('the download failed: ')
    assert 's3cr3t' not in excinfo.value.reason


def test_the_download_host_refuses_a_token(fake_disk_api):
    fake_disk_api.add_file('disk:/a.bin', CONTENT)
    fake_disk_api.redirect_downloads = True

    for path in ('/download', '/storage'):
        response = httpx.get(
            f'{fake_disk_api.base_url}{path}?path=disk%3A%2Fa.bin', headers={'Authorization': 'OAuth x'}
        )
        assert response.status_code == 400
        assert response.json()['error'] == 'AuthorizationNotExpected'


@pytest.mark.parametrize(('start', 'end', 'size'), [(-1, 10, 10), (5, 5, 10), (5, 11, 10), (6, 5, 10), (0, 0, 10)])
async def test_an_invalid_range_is_a_programming_error(fake_disk_api, partial, start, end, size):
    async with api.ContentClient(timeout=5) as content:
        with pytest.raises(ValueError, match='invalid range'):
            await content.fetch(
                f'{fake_disk_api.base_url}/download', start=start, end=end, size=size, write=partial.write
            )

    assert fake_disk_api.requests == []
