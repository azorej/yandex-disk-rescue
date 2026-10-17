"""A fake Yandex Disk REST API on a real socket: an in-memory tree, a routes table and a fault script.

A request is answered by the first of: an `inject`ed fault, the `token` check, a `routes` entry, a
built-in handler (`GET /v1/disk/`, `GET /v1/disk/resources`), then a Yandex-style 404.
"""

import enum
import hashlib
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Literal
from urllib.parse import parse_qs, urlsplit

DEFAULT_MODIFIED = datetime(2024, 5, 17, 12, 30, 45, tzinfo=timezone(timedelta(hours=3)))
_DEFAULT_LIMIT = 20


class _Checksum(enum.Enum):
    OF_CONTENT = enum.auto()


@dataclass(frozen=True)
class FakeResponse:
    status: int
    body: bytes = b''
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, status: int, payload: object, headers: dict[str, str] | None = None) -> FakeResponse:
        return cls(status, json.dumps(payload).encode(), {'Content-Type': 'application/json', **(headers or {})})

    @classmethod
    def error(cls, status: int, headers: dict[str, str] | None = None) -> FakeResponse:
        return cls.json(status, {'error': f'Http{status}Error', 'description': f'Injected HTTP {status}'}, headers)


@dataclass(frozen=True)
class Delay:
    """A fault that waits `seconds` and then lets the request be answered normally."""

    seconds: float


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    query: str
    headers: dict[str, str]
    started: float

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
    responses: deque[FakeResponse | Delay]


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
    """

    base_url: str
    routes: dict[tuple[str, str], FakeResponse] = field(default_factory=dict)
    requests: list[RecordedRequest] = field(default_factory=list)
    account: dict[str, object] = field(default_factory=_default_account)
    page_cap: int | None = None
    token: str | None = None
    peak_in_flight: int = 0
    tree: dict[str, FakeNode] = field(default_factory=lambda: {'/': FakeNode('dir', DEFAULT_MODIFIED)})
    _faults: dict[tuple[str, str], list[_Fault]] = field(default_factory=dict)
    _in_flight: int = 0
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

    def inject(
        self, method: str, route: str, *responses: FakeResponse | Delay | int, where: dict[str, str] | None = None
    ) -> None:
        """Answer the next matching requests with `responses` in order, then serve the route normally again.

        An int is a status with a Yandex-style JSON error body. `where` matches query parameters exactly.
        """
        queue = deque(FakeResponse.error(r) if isinstance(r, int) else r for r in responses)
        self._faults.setdefault((method, route), []).append(_Fault(where or {}, queue))

    def _next_fault(self, method: str, path: str, params: dict[str, str]) -> FakeResponse | Delay | None:
        with self._lock:
            for fault in self._faults.get((method, path), []):
                if fault.responses and all(params.get(k) == v for k, v in fault.where.items()):
                    return fault.responses.popleft()
        return None

    def _enter(self, request: RecordedRequest) -> None:
        with self._lock:
            self.requests.append(request)
            self._in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self._in_flight)

    def _leave(self) -> None:
        with self._lock:
            self._in_flight -= 1

    def respond(self, request: RecordedRequest) -> FakeResponse:
        params = request.params
        fault = self._next_fault(request.method, request.path, params)
        if isinstance(fault, FakeResponse):
            return fault
        if isinstance(fault, Delay):
            time.sleep(fault.seconds)
        if self.token is not None and request.headers.get('authorization') != f'OAuth {self.token}':
            return FakeResponse.json(401, {'error': 'UnauthorizedError', 'description': 'Unauthorized'})
        if routed := self.routes.get((request.method, request.path)):
            return routed
        if (request.method, request.path) == ('GET', '/v1/disk/'):
            return FakeResponse.json(200, self.account)
        if (request.method, request.path) == ('GET', '/v1/disk/resources'):
            return self._resources(params)
        return FakeResponse.json(
            404, {'error': 'NotFound', 'description': f'No fake route for {request.method} {request.path}'}
        )

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
            api._enter(request)
            try:
                response = api.respond(request)
                self.send_response(response.status)
                for name, value in response.headers.items():
                    self.send_header(name, value)
                self.send_header('Content-Length', str(len(response.body)))
                self.end_headers()
                self.wfile.write(response.body)
            finally:
                api._leave()

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
