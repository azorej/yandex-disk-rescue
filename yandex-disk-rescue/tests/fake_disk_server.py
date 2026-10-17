"""A fake Yandex Disk REST API and download host on a real socket: an in-memory tree, a routes table, faults.

A request is answered by the first of: the download host's refusal of an `Authorization` header (`/download`
and `/storage` only), an `inject`ed fault, the `token` check (every other path), a `routes` entry, a built-in
handler (`GET /v1/disk/`, `GET /v1/disk/resources`, `GET /v1/disk/resources/download`, `GET /download`,
`GET /storage`), then a Yandex-style 404. Bodies are sent in 64 KiB chunks, `chunk_delay` seconds apart.
"""

import enum
import hashlib
import json
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Literal
from urllib.parse import parse_qs, urlencode, urlsplit

DEFAULT_MODIFIED = datetime(2024, 5, 17, 12, 30, 45, tzinfo=timezone(timedelta(hours=3)))
_DEFAULT_LIMIT = 20
_CHUNK = 64 * 1024
_CONTENT_PATHS = ('/download', '/storage')
_RANGE = re.compile(r'bytes=(\d+)-(\d*)')


class _Checksum(enum.Enum):
    OF_CONTENT = enum.auto()


@dataclass(frozen=True)
class FakeResponse:
    status: int
    body: bytes = b''
    headers: dict[str, str] = field(default_factory=dict)
    truncate_after: int | None = None

    @classmethod
    def json(cls, status: int, payload: object, headers: dict[str, str] | None = None) -> FakeResponse:
        return cls(status, json.dumps(payload).encode(), {'Content-Type': 'application/json', **(headers or {})})

    @classmethod
    def error(cls, status: int, headers: dict[str, str] | None = None) -> FakeResponse:
        return cls.json(status, {'error': f'Http{status}Error', 'description': f'Injected HTTP {status}'}, headers)


@dataclass(frozen=True)
class Delay:
    """A fault that waits `seconds`, then answers with `then`, or normally when `then` is None.

    The normal answer is computed after the wait, so it reflects tree changes made meanwhile.
    """

    seconds: float
    then: FakeResponse | None = None


@dataclass(frozen=True)
class Truncate:
    """A fault that sends the normal answer's headers (the full `Content-Length`), then closes after `after_bytes`."""

    after_bytes: int


@dataclass
class RecordedRequest:
    """One request as received; `finished` is when its response ended (its last byte written or its connection
    broken), `None` while it is being answered, and `sent` the body bytes written to the socket so far."""

    method: str
    path: str
    query: str
    headers: dict[str, str]
    started: float
    finished: float | None = None
    sent: int = 0

    @property
    def params(self) -> dict[str, str]:
        return {name: values[-1] for name, values in parse_qs(self.query).items()}


@dataclass
class FakeNode:
    kind: Literal['file', 'dir']
    modified: datetime
    content: bytes = b''
    md5: str | None = None
    sha256: str | None = None


@dataclass
class _Fault:
    where: dict[str, str]
    responses: deque[FakeResponse | Delay | Truncate]


def _default_account() -> dict[str, object]:
    return {
        'user': {'login': 'rescue.tester', 'display_name': 'Rescue Tester', 'uid': '1'},
        'total_space': 10 * 1024**3,
        'used_space': 3 * 1024**3 // 2,
        'trash_size': 0,
    }


def _tree_key(path: str) -> str:
    return '/' + path.removeprefix('disk:').strip('/')


def _parent(key: str) -> str:
    return key.rsplit('/', 1)[0] or '/'


@dataclass
class FakeDiskApi:
    """State of the fake API. Tests build the tree, set `routes`, `page_cap`, `account`, `token` and faults.

    Recorded header names are lower-cased. `routes` maps `(method, path)` (path without the query string)
    to the response served for it and overrides the built-in handlers. `token=None` accepts any token.

    A download link is `<base_url>/download?path=<disk:/ path>&link=<n>`, numbered from 1 in issue order.
    The download host serves the file's current content, honouring one `bytes=a-b` or `bytes=a-` range.
    Its faults: `corrupt` (undone by `repair`), `expire_links`, `ignore_range` (200 with the full body for any range),
    `redirect_downloads` (`/download` answers 302 to `/storage` with the same query, which serves the bytes,
    as the real host redirects to a storage host), `chunk_delay`, and the `Truncate` fault for `inject`.

    `peak_in_flight` is the most requests ever answered at once; `peak_content_in_flight` the most content requests
    (`/download`, `/storage`), each counted until just before its last byte is sent. A request still sleeping in a
    `Delay` counts, also after its client gave up.
    """

    base_url: str
    routes: dict[tuple[str, str], FakeResponse] = field(default_factory=dict)
    requests: list[RecordedRequest] = field(default_factory=list)
    account: dict[str, object] = field(default_factory=_default_account)
    page_cap: int | None = None
    token: str | None = None
    peak_in_flight: int = 0
    peak_content_in_flight: int = 0
    ignore_range: bool = False
    redirect_downloads: bool = False
    chunk_delay: float = 0.0
    tree: dict[str, FakeNode] = field(default_factory=lambda: {'/': FakeNode('dir', DEFAULT_MODIFIED)})
    _faults: dict[tuple[str, str], list[_Fault]] = field(default_factory=dict)
    _in_flight: int = 0
    _content_in_flight: int = 0
    _links_issued: int = 0
    _expired_links: dict[int, int] = field(default_factory=dict)
    _corruptions: dict[str, set[int]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def route(
        self, method: str, path: str, status: int, body: bytes = b'', headers: dict[str, str] | None = None
    ) -> None:
        self.routes[(method, path)] = FakeResponse(status, body, headers or {})

    def route_json(self, method: str, path: str, status: int, payload: object) -> None:
        self.routes[(method, path)] = FakeResponse.json(status, payload)

    def add_dir(self, path: str, *, modified: datetime = DEFAULT_MODIFIED) -> None:
        """Add a folder (and any missing parents) at a `disk:/a/b`, `/a/b` or `a/b` path."""
        key = _tree_key(path)
        if key != '/':
            self.add_dir(_parent(key))
        node = self.tree.setdefault(key, FakeNode('dir', modified))
        if node.kind != 'dir':
            raise ValueError(f'{path} is a file')

    def add_file(
        self,
        path: str,
        content: bytes,
        *,
        modified: datetime = DEFAULT_MODIFIED,
        md5: str | None | _Checksum = _Checksum.OF_CONTENT,
        sha256: str | None | _Checksum = _Checksum.OF_CONTENT,
    ) -> None:
        """Add a file (and any missing parent folders); checksums default to the digests of `content`, None omits one."""
        key = _tree_key(path)
        self.add_dir(_parent(key))
        if self.tree.get(key, FakeNode('file', modified)).kind != 'file':
            raise ValueError(f'{path} is a folder')
        self.tree[key] = FakeNode(
            'file',
            modified,
            content,
            md5=hashlib.md5(content).hexdigest() if md5 is _Checksum.OF_CONTENT else md5,
            sha256=hashlib.sha256(content).hexdigest() if sha256 is _Checksum.OF_CONTENT else sha256,
        )

    def remove(self, path: str) -> None:
        """Remove a file or a folder with everything below it; a missing path raises `KeyError`."""
        key = _tree_key(path)
        if key == '/' or key not in self.tree:
            raise KeyError(path)
        for other in [k for k in self.tree if k == key or k.startswith(key + '/')]:
            del self.tree[other]

    def corrupt(self, path: str, offset: int) -> None:
        """Flip every bit of the served byte at `offset` of a file; its metadata keeps the true checksums."""
        self._corruptions.setdefault(_tree_key(path), set()).add(offset)

    def repair(self, path: str) -> None:
        """Serve the true bytes of a file `corrupt` changed again."""
        self._corruptions.pop(_tree_key(path), None)

    def expire_links(self, status: int = 410) -> None:
        """Make every download link issued so far answer `status`; links issued later work."""
        with self._lock:
            for link in range(1, self._links_issued + 1):
                self._expired_links.setdefault(link, status)

    def inject(
        self,
        method: str,
        route: str,
        *responses: FakeResponse | Delay | Truncate | int,
        where: dict[str, str] | None = None,
    ) -> None:
        """Answer the next matching requests with `responses` in order, then serve the route normally again.

        An int is a status with a Yandex-style JSON error body. `where` matches query parameters exactly.
        """
        queue = deque(FakeResponse.error(r) if isinstance(r, int) else r for r in responses)
        self._faults.setdefault((method, route), []).append(_Fault(where or {}, queue))

    def _next_fault(self, method: str, path: str, params: dict[str, str]) -> FakeResponse | Delay | Truncate | None:
        with self._lock:
            for fault in self._faults.get((method, path), []):
                if fault.responses and all(params.get(k) == v for k, v in fault.where.items()):
                    return fault.responses.popleft()
        return None

    def _enter(self, request: RecordedRequest) -> bool:
        """Count `request` in flight; `True` when it is a content request, counted until `_content_ends`."""
        content = request.path in _CONTENT_PATHS
        with self._lock:
            self.requests.append(request)
            self._in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self._in_flight)
            if content:
                self._content_in_flight += 1
                self.peak_content_in_flight = max(self.peak_content_in_flight, self._content_in_flight)
        return content

    def _content_ends(self) -> None:
        with self._lock:
            self._content_in_flight -= 1

    def _leave(self) -> None:
        with self._lock:
            self._in_flight -= 1

    def respond(self, request: RecordedRequest) -> FakeResponse:
        if request.path in _CONTENT_PATHS and 'authorization' in request.headers:
            return FakeResponse.json(
                400, {'error': 'AuthorizationNotExpected', 'description': 'The download host takes no token.'}
            )
        fault = self._next_fault(request.method, request.path, request.params)
        if isinstance(fault, FakeResponse):
            return fault
        if isinstance(fault, Delay):
            time.sleep(fault.seconds)
            if fault.then is not None:
                return fault.then
        normal = self._normal(request)
        if isinstance(fault, Truncate):
            return replace(normal, truncate_after=fault.after_bytes)
        return normal

    def _normal(self, request: RecordedRequest) -> FakeResponse:
        params = request.params
        if (
            request.path not in _CONTENT_PATHS
            and self.token is not None
            and request.headers.get('authorization') != f'OAuth {self.token}'
        ):
            return FakeResponse.json(401, {'error': 'UnauthorizedError', 'description': 'Unauthorized'})
        if routed := self.routes.get((request.method, request.path)):
            return routed
        match (request.method, request.path):
            case ('GET', '/v1/disk/'):
                return FakeResponse.json(200, self.account)
            case ('GET', '/v1/disk/resources'):
                return self._resources(params)
            case ('GET', '/v1/disk/resources/download'):
                return self._download_link(params)
            case ('GET', '/download') if self.redirect_downloads:
                return FakeResponse(302, headers={'Location': f'{self.base_url}/storage?{request.query}'})
            case ('GET', '/download' | '/storage'):
                return self._content(params, request.headers.get('range'))
        return FakeResponse.json(
            404, {'error': 'NotFound', 'description': f'No fake route for {request.method} {request.path}'}
        )

    def _download_link(self, params: dict[str, str]) -> FakeResponse:
        key = _tree_key(params.get('path', ''))
        node = self.tree.get(key)
        if node is None:
            return FakeResponse.json(404, {'error': 'DiskNotFoundError', 'description': 'Resource not found.'})
        if node.kind == 'dir':
            return FakeResponse.json(400, {'error': 'FieldValidationError', 'description': 'Resource is a folder.'})
        with self._lock:
            self._links_issued += 1
            link = self._links_issued
        query = urlencode({'path': 'disk:' + key, 'link': link})
        return FakeResponse.json(
            200, {'href': f'{self.base_url}/download?{query}', 'method': 'GET', 'templated': False}
        )

    def _content(self, params: dict[str, str], range_header: str | None) -> FakeResponse:
        with self._lock:
            expired = self._expired_links.get(int(params.get('link', '0')))
        if expired is not None:
            return FakeResponse.json(expired, {'error': 'LinkExpired', 'description': 'The link has expired.'})
        key = _tree_key(params.get('path', ''))
        node = self.tree.get(key)
        if node is None or node.kind != 'file':
            return FakeResponse.json(404, {'error': 'NotFound', 'description': 'No such file.'})
        content = bytearray(node.content)
        for offset in self._corruptions.get(key, set()):
            if offset < len(content):
                content[offset] ^= 0xFF
        headers = {'Content-Type': 'application/octet-stream', 'Accept-Ranges': 'bytes'}
        if range_header is None or self.ignore_range:
            return FakeResponse(200, bytes(content), headers)
        match = _RANGE.fullmatch(range_header)
        first = int(match[1]) if match else len(content)
        last = min(int(match[2]), len(content) - 1) if match and match[2] else len(content) - 1
        if first > last:
            return FakeResponse(416, headers={'Content-Range': f'bytes */{len(content)}'})
        headers['Content-Range'] = f'bytes {first}-{last}/{len(content)}'
        return FakeResponse(206, bytes(content[first : last + 1]), headers)

    def _resources(self, params: dict[str, str]) -> FakeResponse:
        if 'path' not in params:
            return FakeResponse.json(400, {'error': 'FieldValidationError', 'description': 'path is required'})
        key = _tree_key(params['path'])
        node = self.tree.get(key)
        if node is None:
            return FakeResponse.json(404, {'error': 'DiskNotFoundError', 'description': 'Resource not found.'})
        body = self._resource_json(key, node)
        if node.kind == 'dir':
            limit = int(params.get('limit', _DEFAULT_LIMIT))
            offset = int(params.get('offset', 0))
            served = min(limit, self.page_cap) if self.page_cap is not None else limit
            children = sorted((k for k in self.tree if k != key and _parent(k) == key), key=lambda k: k.rsplit('/')[-1])
            body['_embedded'] = {
                'path': body['path'],
                'items': [self._resource_json(k, self.tree[k]) for k in children[offset : offset + served]],
                'limit': limit,
                'offset': offset,
                'total': len(children),
                'sort': params.get('sort', ''),
            }
        return FakeResponse.json(200, body)

    @staticmethod
    def _resource_json(key: str, node: FakeNode) -> dict[str, object]:
        body: dict[str, object] = {
            'name': key.rsplit('/')[-1] or 'disk',
            'path': 'disk:' + key,
            'type': node.kind,
            'created': node.modified.isoformat(),
            'modified': node.modified.isoformat(),
        }
        if node.kind == 'file':
            body['size'] = len(node.content)
            body['mime_type'] = 'application/octet-stream'
            body.update({name: value for name, value in (('md5', node.md5), ('sha256', node.sha256)) if value})
        return body


def _handler_for(api: FakeDiskApi) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            target = urlsplit(self.path)
            request = RecordedRequest(
                method=self.command,
                path=target.path,
                query=target.query,
                headers={name.lower(): value for name, value in self.headers.items()},
                started=time.monotonic(),
            )
            counted = api._enter(request)

            # A content request stops counting before its last byte goes out, so a client that has read a whole
            # response, and starts its next request, never finds the old one still counted.
            def content_ends() -> None:
                nonlocal counted
                if counted:
                    counted = False
                    api._content_ends()

            try:
                response = api.respond(request)
                self.send_response(response.status)
                for name, value in response.headers.items():
                    self.send_header(name, value)
                self.send_header('Content-Length', str(len(response.body)))
                body = response.body[: response.truncate_after]
                if not body:
                    content_ends()
                self.end_headers()
                self._send_body(request, response, body, before_last=content_ends)
            finally:
                content_ends()
                request.finished = time.monotonic()
                api._leave()

        def _send_body(
            self, request: RecordedRequest, response: FakeResponse, body: bytes, *, before_last: Callable[[], None]
        ) -> None:
            try:
                for start in range(0, len(body), _CHUNK):
                    if start:
                        time.sleep(api.chunk_delay)
                    if start + _CHUNK >= len(body):
                        before_last()
                    chunk = body[start : start + _CHUNK]
                    self.wfile.write(chunk)
                    request.sent += len(chunk)
            except BrokenPipeError, ConnectionResetError:
                self.close_connection = True
            if response.truncate_after is not None:
                self.close_connection = True

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _serve

        def log_message(self, *args: object) -> None:
            pass

    return Handler


def start_fake_disk_server() -> tuple[FakeDiskApi, ThreadingHTTPServer, threading.Thread]:
    api = FakeDiskApi(base_url='')
    server = ThreadingHTTPServer(('127.0.0.1', 0), _handler_for(api))
    server.daemon_threads = True
    host, port = server.server_address[:2]
    api.base_url = f'http://{host}:{port}'
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    return api, server, thread
