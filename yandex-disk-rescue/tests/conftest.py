"""Shared fixtures for the yandex-disk-rescue tests."""

import json
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Keep tests away from the developer's real token, config and the `.env` that mise loads."""
    for name in list(os.environ):
        if name.startswith('YANDEX_DISK_RESCUE_'):
            monkeypatch.delenv(name)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg-config'))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'xdg-state'))


@dataclass(frozen=True)
class FakeResponse:
    status: int
    body: bytes = b''
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, status: int, payload: object) -> FakeResponse:
        return cls(status, json.dumps(payload).encode(), {'Content-Type': 'application/json'})


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    query: str
    headers: dict[str, str]


@dataclass
class FakeDiskApi:
    """State of the fake Yandex Disk API: tests edit `routes` and inspect `requests`.

    Recorded header names are lower-cased. `routes` maps `(method, path)` (path without the query string) to the response served for it;
    an unrouted request gets a 404 with a Yandex-style JSON error body.
    """

    base_url: str
    routes: dict[tuple[str, str], FakeResponse] = field(default_factory=dict)
    requests: list[RecordedRequest] = field(default_factory=list)

    def route(
        self, method: str, path: str, status: int, body: bytes = b'', headers: dict[str, str] | None = None
    ) -> None:
        self.routes[(method, path)] = FakeResponse(status, body, headers or {})

    def route_json(self, method: str, path: str, status: int, payload: object) -> None:
        self.routes[(method, path)] = FakeResponse.json(status, payload)


def _handler_for(api: FakeDiskApi) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            target = urlsplit(self.path)
            api.requests.append(
                RecordedRequest(
                    method=self.command,
                    path=target.path,
                    query=target.query,
                    headers={name.lower(): value for name, value in self.headers.items()},
                )
            )
            response = api.routes.get((self.command, target.path)) or FakeResponse.json(
                404, {'error': 'NotFound', 'description': f'No fake route for {self.command} {target.path}'}
            )
            self.send_response(response.status)
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.send_header('Content-Length', str(len(response.body)))
            self.end_headers()
            self.wfile.write(response.body)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _serve

        def log_message(self, *args: object) -> None:
            pass

    return Handler


@pytest.fixture
def fake_disk_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiskApi]:
    """A real HTTP server on 127.0.0.1 standing in for the Yandex Disk API, wired in via YANDEX_DISK_RESCUE_API_URL."""
    api = FakeDiskApi(base_url='')
    server = ThreadingHTTPServer(('127.0.0.1', 0), _handler_for(api))
    server.daemon_threads = True
    host, port = server.server_address[:2]
    api.base_url = f'http://{host}:{port}'
    api.route_json(
        'GET',
        '/v1/disk/',
        200,
        {
            'user': {'login': 'rescue.tester', 'display_name': 'Rescue Tester', 'uid': '1'},
            'total_space': 10 * 1024**3,
            'used_space': 3 * 1024**3 // 2,
            'trash_size': 0,
        },
    )
    monkeypatch.setenv('YANDEX_DISK_RESCUE_API_URL', api.base_url)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield api
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
