# Plan: Yandex Disk API client with adaptive rate limiting and `scan`

Issue: #2 (umbrella #9)
Branch: feat/api-client-scan (from `main` at e817461, where #1 has merged)

## Context

Today the CLI has no `scan`: `uv run yandex-disk-rescue scan` exits 2 with `No such
command 'scan'`. `mise run check` is green with 84 tests. #1 (PR #10, e817461) landed
these seams, which this plan grows rather than duplicates (re-verified against the
merged code):

- `errors.RescueError` (`exit_code = 1`). `cli.RescueErrorGroup`, the root app's
  `TyperGroup`, renders it as `Error: <message>` on stderr and exits with
  `err.exit_code`, with no traceback.
- `paths.config_dir()` (XDG rule: unset, empty or relative `XDG_CONFIG_HOME` falls back
  to `~/.config`) and `paths.APP_DIR_NAME`.
- In `auth`:
  - Token: `resolve_token() -> Token(value, source)`, `get_token() -> str`,
    `token_from_paste`, `save_token`, `delete_token`, `token_path`; the private
    `_validated_token` / `_token_defect` header-safety check.
  - Errors: `NoTokenError`, `InvalidTokenError`, `InsecureTokenFileError`,
    `TokenFileError`, `TokenRejectedError` (401 and 403, two different texts) and
    `DiskApiError` (everything else).
  - API: `API_URL_ENV_VAR`, `DEFAULT_API_URL`, `API_TIMEOUT_SECONDS = 30.0`,
    `api_base_url()`, `Account(login, total_space, used_space)` and
    `fetch_account(token)`. `fetch_account` validates the token before any request
    (`InvalidTokenError`), sends one sync `httpx.get` with no retries, and builds its
    messages with `_transport_failure` (keeps the text only for `NetworkError`,
    `TimeoutException`, `RemoteProtocolError`, `UnsupportedProtocol`; any other
    `TransportError` is reported by class name, because its text can echo the request
    headers and the token), `_error_description` (JSON `description`, else the reason
    phrase) and `_unexpected_response`.
  - OAuth app: `CLIENT_ID_ENV_VAR`, `DEFAULT_CLIENT_ID`; `cli.CLIENT_ID_PATTERN` and
    `cli._validate_client_id` back an ad hoc `--client-id` option on `auth login`.
- In `cli`: `auth login`, `auth logout`, `auth status` (inline in `cli.py`),
  `REVOKE_URL` and the binary-unit size formatter `_format_size`.
- `tests/conftest.py`:
  - The autouse `isolated_environment` fixture deletes every `YANDEX_DISK_RESCUE_*`
    variable and points `XDG_CONFIG_HOME` and `XDG_STATE_HOME` into `tmp_path`.
  - `fake_disk_api` yields `FakeDiskApi`: `base_url`; `routes:
    dict[(method, path), FakeResponse]` (path without query); `requests:
    list[RecordedRequest(method, path, query, headers)]` with lower-cased header names;
    `route(method, path, status, body=b'', headers=None)` and
    `route_json(method, path, status, payload)`. It is a `ThreadingHTTPServer` on
    127.0.0.1, sets `YANDEX_DISK_RESCUE_API_URL` via `monkeypatch`, pre-seeds
    `GET /v1/disk/` (with `user.uid = '1'`) as a routes entry, accepts any token, and
    answers unrouted requests with a Yandex-style 404 JSON body.

Every later ticket (#3 to #6) needs three things from this one: a rate-limit-safe view
of the remote tree in one crash-safe SQLite database, one options mechanism, and one
fake Yandex Disk server (#9 cross-cutting decisions). This plan builds them by growing
#1's code, not beside it. There is one HTTP client, one `GET /v1/disk/` code path, one
fake server and one way to read every option.

## Goal

- `yandex-disk-rescue scan [REMOTE_PATH]` (default `disk:/`) walks the folder
  recursively through `GET /v1/disk/resources`, page by page, with up to
  `api_concurrency` requests in flight. It records every file and folder in
  `state_dir()/manifest.sqlite3`.
- The command prints a summary: files, folders, total size and files without a cloud
  checksum. On a rerun it also prints what was added, changed and removed.
- Every API request from every command (`scan`, `auth status`, `auth login`) goes
  through one `DiskClient`, with one shared adaptive limiter and one bounded retry
  policy, and with #1's token-leak protection on every request.
- The manifest only ever holds the result of a *completed* scan. Ctrl+C, SIGKILL or a
  failure mid-scan leave the previous manifest content unchanged.
- One options mechanism, used by every option including #1's `--client-id`: CLI flag >
  env var > config file > default. Values are validated, and a bad value is rejected
  at startup with exit 2.

## Non-goals

- Downloading content (#3), download concurrency limits (#4), progress, the log file
  and `--json` (#6). `scan` prints only its final summary.
- Trash (`trash:/`) and auto-upload sections. Schemes other than `disk:` are rejected.
- Resuming an interrupted scan. A rerun rescans.
- HTTP/2 (the `h2` extra). Revisit in #4.
- Moving the `auth` commands out of `cli.py` (see Risks).

## Exit-code contract (extends #1's; reused by #3 to #6)

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | `RescueError`: no token, a malformed, unreadable or insecure token, token rejected, remote path missing or not a folder, API error after retries, manifest unusable or locked |
| 2 | Invalid input: Typer/Click usage errors, an invalid option value from any source, an invalid config file, an unsupported path scheme (`RescueError` subclasses with `exit_code = 2`, rendered by `RescueErrorGroup`) |
| 130 | Interrupted (Ctrl+C). Typer already maps `KeyboardInterrupt` to `Exit(130)` (`typer/core.py:201-202`, verified) |

Error message rule (keeps #1's texts byte for byte):

- Account-check failures keep #1's exact messages, for example
  `Yandex Disk API returned HTTP 502: Bad Gateway` and
  `Could not reach the Yandex Disk API at <base url>: <reason>`.
- Failures of a request about a remote path prefix that message with the path:
  `Could not list disk:/A: Yandex Disk API returned HTTP 503: <description>`.
- When more than one attempt was made, the message ends with
  ` (after N attempts)`. A single attempt (`api_retries = 0`, or a non-retried status)
  adds no suffix, so #1's exact-equality assertions still hold.
- Transport reasons always go through #1's `_transport_failure` rule. Messages never
  include request headers or the token.

## Stages

### Stage 1: One `DiskClient` and one fake server (listing, account, error mapping)

- **Contract:**
  - New package `yandex_disk_rescue/api/`:
    ```python
    @dataclass(frozen=True)
    class Account:
        uid: str
        login: str
        total_space: int
        used_space: int


    @dataclass(frozen=True)
    class Resource:
        path: str  # normalised 'disk:/a/b'
        kind: Literal['file', 'dir']
        size: int | None  # None for folders
        md5: str | None
        sha256: str | None
        modified: datetime  # timezone-aware UTC


    class DiskClient:  # async context manager owning one httpx.AsyncClient
        def __init__(self, token: str, *, base_url: str, timeout: float): ...
        async def account(self) -> Account: ...  # GET /v1/disk/
        def list_folder(self, path: str) -> AsyncIterator[Resource]: ...


    def normalize_remote_path(raw: str) -> str: ...
    ```
  - Token safety:
    - `DiskClient.__init__` validates the token with #1's header-safety rule and
      raises `auth.InvalidTokenError` (#1's `The token ...` texts) before any request.
      `auth._validated_token` becomes the public `auth.validate_token(value) -> str`
      so `api/` does not reach into a private name. `api/` may import from `auth`;
      `auth` never imports `api/`.
    - #1's `_transport_failure` rule moves into `api/` and covers every request.
  - Ownership moves:
    - `Account`, `TokenRejectedError` and `DiskApiError` move from `auth` into `api/`,
      with #1's message texts unchanged (see the error message rule): 401 and 403 give
      `TokenRejectedError` with #1's two texts; any other non-2xx, a transport error
      or an unexpected body gives `DiskApiError`. `_error_description` and the
      unexpected-response message (`... unexpected response to <METHOD> <api path>:
      <reason>.`) move with them.
    - `Account` gains `uid` (from `user.uid`, a non-empty string). Field checks run in
      the order login, total_space, used_space, uid, so every existing
      `test_status_rejects_an_unexpected_success_body` case keeps its reason; add a
      case for a body without `user.uid` (`user.uid is missing ...`).
    - `auth.fetch_account` is deleted. `auth status` and `auth login` call
      `DiskClient.account()` through `asyncio.run`, with
      `base_url=auth.api_base_url()` and `timeout=auth.API_TIMEOUT_SECONDS` (Stage 2
      deletes both).
    - Sweep: `rg 'fetch_account|_validated_token|from yandex_disk_rescue.auth import .*(Account|TokenRejectedError|DiskApiError)|auth\.(Account|TokenRejectedError|DiskApiError)'`
      returns nothing outside `docs/plans/`.
  - New errors, all `RescueError` subclasses in `api/`:
    - `RemotePathNotFoundError` (404 on a resource request only; a 404 on
      `GET /v1/disk/` stays #1's `DiskApiError`): `Remote path 'disk:/x' does not exist`.
    - `NotAFolderError`: `list_folder` was called on a file.
    - `UnsupportedPathError` (`exit_code = 2`).
  - `list_folder`:
    - It yields every direct child, paging with `limit` (internal constant 1000) and
      `offset` until `offset >= _embedded.total`. It sends `sort=name` and a `fields=`
      filter limited to `type`, the `Resource` fields and `_embedded.total/limit/offset`.
    - It must cope with pages shorter than `limit`.
    - Malformed bodies give the unexpected-response `DiskApiError` naming
      `GET /v1/disk/resources`, prefixed with the remote path.
  - `normalize_remote_path`:
    - `disk:/A/` becomes `disk:/A`; `/A` and `A` become `disk:/A`.
    - `disk:`, `disk:/` and `/` become `disk:/`.
    - Any other scheme raises `UnsupportedPathError`.
  - Fake server: `fake_disk_api` keeps its name and every #1 attribute and helper
    (`base_url`, `routes`, `requests`, `route()`, `route_json()`, `FakeResponse`,
    `RecordedRequest`, the `YANDEX_DISK_RESCUE_API_URL` wiring, the 404 for unknown
    paths). The fixture grows in place. If the server class moves to a tests helper
    module, add `yandex-disk-rescue/tests` to pytest `pythonpath` in the root
    `pyproject.toml` (pytest runs with `--import-mode=importlib`). #1's handler code
    is replaced, not kept beside the new one. Lookup order for a request: an
    `inject`ed fault, then a `routes` entry, then the built-in handlers, then the 404.
    The additions are:
    - An in-memory tree: `add_dir(path)` and
      `add_file(path, content: bytes, *, modified=..., md5=..., sha256=...)`.
      Checksums default to the real digests of `content`, and `None` omits them.
      Content is stored now so that #3 can serve bytes, Range requests and corruption.
    - `remove(path)`.
    - `page_cap`: the maximum number of items per page, whatever `limit` asks for.
    - `account`: the fields served by a built-in `GET /v1/disk/` (replacing #1's
      pre-seeded routes entry; same body, `user.uid = '1'`). A `route_json('GET',
      '/v1/disk/', ...)` override still wins, as #1's tests rely on.
    - `token: str | None = None`: when set, any other `Authorization: OAuth <token>`
      gets a 401 JSON error. `None` accepts any token, as #1's fake does.
    - Built-in `GET /v1/disk/resources`, served from the tree, with Yandex-style JSON
      errors.
    - A fault script, `inject(method, route, *responses, where=None)`. Each response
      is a status (with headers such as `Retry-After` and a JSON body) or a delay in
      seconds before answering normally. The responses are used in order, and then
      the route serves normally again. `where` matches on query parameters, for
      example `path`.
    - `RecordedRequest` gains a monotonic `started` timestamp; `method`, `path`,
      `query` and lower-cased `headers` stay. The server records the peak number of
      requests in flight.
    - The routes table stays the extension point for #3 (`/v1/disk/resources/download`
      and a download host).
  - Dev dependency: `uv add --dev anyio`. The tests use anyio's pytest plugin
    directly, so it is declared rather than relied on as httpx's transitive dependency.
- **Behaviour to lock:**
  - New `yandex-disk-rescue/tests/test_api_client.py`. Async tests use anyio's pytest
    plugin with `anyio_backend` fixed to `'asyncio'`.
    - With `page_cap = 3` and a folder of 10 mixed children, `list_folder` yields all
      10 with correct fields, and the log shows offsets 0, 3, 6, 9.
    - A 404 gives `RemotePathNotFoundError` naming the path. A file path gives
      `NotAFolderError`.
    - `account()` returns `uid` and `login` from the fixture's account fields.
    - With `fake_disk_api.token` set, a different token gets `TokenRejectedError`.
    - `normalize_remote_path` has a table test.
  - Regression lock: #1's `test_auth.py` and `test_cli.py` run on the grown fixture in
    this commit, with their assertions unchanged. The only permitted edits:
    - `test_fetch_account_refuses_a_token_unfit_for_a_header_before_any_request`
      constructs `DiskClient` instead of calling `fetch_account`; it still asserts
      `InvalidTokenError`, no `SECRET` in the message and an empty request log.
    - `test_status_formats_sizes_in_binary_units` adds `'uid': '1'` to its `user`
      body.
    - The new `user.uid` case in `test_status_rejects_an_unexpected_success_body`.
- **Deliverable:** the `api/` package. `auth` commands use it, `fetch_account` is gone,
  and there is a single grown fake server.
- **Validation:**
  - `mise run check`.
  - `mise run test -- yandex-disk-rescue/tests/test_api_client.py yandex-disk-rescue/tests/test_auth.py yandex-disk-rescue/tests/test_cli.py`
    (all #1 auth tests pass on the new fixture).
  - The `rg` sweep above.
- **Doc updates:** package `README.md`:
  - Rewrite the Layout tree as current state: add `api/` (client, account check,
    listing, error mapping) and rewrite the `auth.py` line, which no longer does the
    account check.
  - Rewrite the `conftest.py` line as current state: the fake server's tree,
    `page_cap`, `token`, `inject`, the routes override and the request log.

### Stage 2: Options registry, shared adaptive limiter, bounded retries

- **Contract:**
  - Dependency: `uv add --package yandex-disk-rescue 'tenacity>=9.1.4'`. Today is
    2026-10-17 and 9.2.1 was published 2026-10-07, so if the stage runs before
    2026-10-21 also run `uv lock --upgrade-package tenacity==9.1.4` (published
    2026-02-07), so that the locked version is at least two weeks old.
  - `yandex_disk_rescue/settings.py` is the one registry of options. Each entry
    defines:
    - the option name, which is also the config-file key from Stage 5;
    - the flag;
    - the env var;
    - the default;
    - the validation (a range, or a check with its message).

    A reusable `Annotated` option type per entry lets later tickets add entries
    instead of redefining options. Validation applies to every source; a violation
    exits 2 and names the option and the accepted values. This stage's entries:

    | Option / flag | Env var | Default | Valid values |
    |---|---|---|---|
    | `api_interval_ms` / `--api-interval-ms` | `YANDEX_DISK_RESCUE_API_INTERVAL_MS` | 50 | 25–1000 |
    | `api_concurrency` / `--api-concurrency` | `YANDEX_DISK_RESCUE_API_CONCURRENCY` | 7 | 1–30 |
    | `api_timeout` / `--api-timeout` (s) | `YANDEX_DISK_RESCUE_API_TIMEOUT` | 30 | 1–300 |
    | `api_retries` / `--api-retries` | `YANDEX_DISK_RESCUE_API_RETRIES` | 5 | 0–20 |
    | `api_url` / `--api-url` (hidden) | `YANDEX_DISK_RESCUE_API_URL` | `https://cloud-api.yandex.net` | http(s) URL |
    | `client_id` / `--client-id` (`auth login` only) | `YANDEX_DISK_RESCUE_CLIENT_ID` | `9e2a0382c430429c9d57913b61d7d48f` | 32 lowercase hex characters; message keeps #1's `must be 32 lowercase hexadecimal characters (the ClientID of a Yandex OAuth app).` |

    The `api_timeout` default is #1's 30 s, not 10 s: a 1000-item listing page can be
    slow, and a hung request is bounded by the retries anyway.
  - Every command that talks to the API (`auth status` and `auth login` now, `scan` in
    Stage 4) takes the `api_*` options and builds its client from them.
  - Deleted, with the registry as their only replacement: `auth.api_base_url`,
    `auth.API_URL_ENV_VAR`, `auth.DEFAULT_API_URL`, `auth.API_TIMEOUT_SECONDS`,
    `auth.CLIENT_ID_ENV_VAR`, `auth.DEFAULT_CLIENT_ID`, `cli.CLIENT_ID_PATTERN` and
    `cli._validate_client_id`. `rg 'api_base_url|API_URL_ENV_VAR|DEFAULT_API_URL|API_TIMEOUT_SECONDS|CLIENT_ID_ENV_VAR|DEFAULT_CLIENT_ID|CLIENT_ID_PATTERN|_validate_client_id'`
    returns nothing outside `docs/plans/`.
  - The `ApiLimits` dataclass and `DiskClient(token, *, base_url, limits: ApiLimits)`
    replace the bare `timeout` argument. `ApiLimits` fields: `interval` (s),
    `concurrency`, `timeout`, `retries`, `backoff_initial = 0.5` and
    `backoff_max = 30.0`. The two backoff fields are not options; tests pass small
    values.
  - The limiter, one per `DiskClient`, is shared by every request including retries.
    Its invariants can be observed at the fake server:
    1. Request starts are at least the *current* interval apart across all
       concurrent requests, and at most `concurrency` requests are in flight.
    2. A throttling response is a 429, a 408 or any 5xx. If it carries `Retry-After`
       (delta-seconds or an HTTP date), **all** request starts pause until that
       moment. The pause is capped at 300 s.
    3. Each throttling response doubles the current interval, up to
       `max(2 s, 8 × base)`. Every 10 consecutive successes halve it, but never below
       the base interval.
  - Retries (tenacity):
    - Transport errors, including timeouts, and throttling responses are retried up
      to `retries` times.
    - The wait is `Retry-After` when the server sent one. Otherwise it is exponential
      with jitter, from `backoff_initial` up to `backoff_max`.
    - Other 4xx responses are not retried. That includes 401, 403 and 404, which keep
      their Stage 1 errors.
    - When the retries run out, the client raises `DiskApiError` per the error message
      rule: #1's text for the last failure plus ` (after N attempts)`, for example
      `Yandex Disk API returned HTTP 503: Service unavailable (after 6 attempts)` and
      `Could not reach the Yandex Disk API at <base url>: <reason> (after 6 attempts)`.
      The message is built by the client, never from tenacity's `RetryError` text, and
      no retry hook logs the exception or the request.
- **Behaviour to lock:**
  - Extend `tests/test_api_client.py`:
    - A first listing request answered with 429 and `Retry-After: 1` still lets the
      listing succeed, and the next request arrives at least 1 s later (log
      timestamps).
    - Three 503s followed by a success let the listing succeed. Four 503s with
      `retries=2` give `DiskApiError` ending in `(after 3 attempts)` and containing
      `HTTP 503`.
    - A response delayed past `timeout=0.2` once is retried and succeeds. A delay on
      every attempt gives `DiskApiError` that names the timeout.
    - With `interval=0.05` and `concurrency=7`, and 20 folders listed concurrently,
      every logged start gap is at least about 45 ms, and the server's in-flight peak
      is 7 or less.
  - New `tests/test_rate_limiter.py`, driven through a controllable clock passed to
    the limiter: the interval doubles per throttle up to the cap, then returns to the
    base after enough successes.
  - Extend `tests/test_auth.py`:
    - `auth status --api-interval-ms 10` and
      `YANDEX_DISK_RESCUE_API_CONCURRENCY=99 auth status` exit 2 naming the range.
    - `auth status` against a fake that returns 503 then 200 exits 0, so retries
      apply to auth.
    - `auth status --api-url <fake base_url>` with no env var reaches the fake; this
      replaces `test_api_base_url_defaults_to_yandex`, which is deleted with
      `api_base_url`.
    - #1 tests that drive a throttling status or a transport error set
      `YANDEX_DISK_RESCUE_API_RETRIES=0` so they stay fast and their exact messages
      hold: `test_status_reports_api_errors_without_a_traceback_or_the_token` (the 500
      case), `test_status_error_without_a_json_description_uses_the_reason_phrase`
      (502) and `test_status_reports_an_unreachable_api` (closed port; it still
      asserts `Could not reach` and uses the literal env var name instead of
      `auth.API_URL_ENV_VAR`).
    - The `--client-id` tests (`test_login_prints_the_implicit_flow_url_on_stderr`,
      `test_login_rejects_a_malformed_client_id_as_a_usage_error`) use the literal
      env var name instead of `auth.CLIENT_ID_ENV_VAR`; their assertions are unchanged.
- **Deliverable:** all API traffic is paced and retried, the options registry exists
  and owns every option, and the ad hoc constants are gone.
- **Validation:** `mise run check`; `mise run test -- -k "rate or api_client or auth"`;
  the `rg` sweep above is empty.
- **Doc updates:**
  - Root `README.md`:
    - Add a "Rate limiting" section written as current state (limiter rules, retry
      rule, 300 s cap).
    - Rewrite the two "checked with one request to the Disk API" sentences (`auth
      login` and `auth status`) as current state: the check goes through the paced,
      retried client.
  - Package `README.md`: rewrite the Layout tree (add `settings.py`) and the
    environment variable table as current state (the new `api_*` variables; the
    `YANDEX_DISK_RESCUE_API_URL` and `YANDEX_DISK_RESCUE_CLIENT_ID` rows stay).
  - `.env.example`: keep #1's commented `YANDEX_DISK_RESCUE_API_URL` and
    `YANDEX_DISK_RESCUE_CLIENT_ID` entries. Add no other entries.

### Stage 3: Manifest database (schema, migrations, atomic scan commit, run lock)

- **Contract:**
  - `paths.state_dir() -> Path`: `$XDG_STATE_HOME/yandex-disk-rescue`, falling back to
    `~/.local/state/yandex-disk-rescue` when the variable is unset, empty or not
    absolute. Same rule as `config_dir()`, same `APP_DIR_NAME`. It does not create the
    directory. (The autouse fixture already points `XDG_STATE_HOME` into `tmp_path`.)
  - `yandex_disk_rescue/manifest.py` (or a package):
    ```python
    class Manifest:  # context manager
        @classmethod
        def open(cls, path: Path) -> Manifest: ...
        def begin_scan(self, account_uid: str, root: str) -> ScanSession: ...
        def entries(self, account_uid: str, root: str) -> Iterator[Resource]: ...


    class ScanSession:
        def add(self, resources: Iterable[Resource]) -> None: ...
        def commit(self) -> ScanDiff: ...


    @dataclass(frozen=True)
    class ScanDiff:
        added: list[str]
        changed: list[str]
        removed: list[str]
        first_scan: bool  # the subtree had no entries before
    ```
  - Opening, versions and errors:
    - `open` creates the parent directories with mode 0700 and applies pending
      migrations.
    - The schema version is `PRAGMA user_version`. Migrations are an ordered list,
      one transaction each. This ticket ships version 1, and #3 to #5 append to the
      list.
    - `ManifestError` (`RescueError`, exit 1) names the path and what to do. It is
      raised for a DB newer than the code, and for a file that is not SQLite.
  - Run lock: `open` takes an exclusive `fcntl.flock` on `<db>.lock` and holds it
    until close. A second process gets `ManifestError`:
    `another yandex-disk-rescue run is using <path>`.
  - Scan sessions:
    - Rows added through a `ScanSession` are durable but invisible to `entries()`
      until `commit()`.
    - `commit()` replaces the subtree of `root` for that account in one transaction,
      then returns the diff. The subtree is `path == root` plus everything under
      `root + '/'`; for `disk:/` it is everything.
    - A path counts as `changed` when any of kind, size, md5, sha256 or modified
      differs.
    - A session that is never committed (exception, Ctrl+C, SIGKILL) leaves
      `entries()` unchanged. The next `begin_scan` discards its staged rows.
    - Rows are keyed by `(account_uid, path)`. Scanning `disk:/A` touches only
      `disk:/A/...`, and two accounts never mix.
- **Behaviour to lock:**
  - Extend `tests/test_auth.py`: parametrise #1's
    `test_config_dir_falls_back_under_home` (cases unset, empty, relative) over both
    `config_dir` / `XDG_CONFIG_HOME` / `.config` and `state_dir` / `XDG_STATE_HOME` /
    `.local/state`.
  - New `tests/test_manifest.py`, using real SQLite in `tmp_path`:
    - After commit and reopen, `entries()` returns the same resources. A second
      session with one file changed, one removed and one added returns exactly that
      diff.
    - An abandoned session (closed without commit) leaves `entries()` identical, and
      the next scan does not see its rows.
    - Scanning `disk:/A` leaves `disk:/AB` and `disk:/B` alone.
    - `user_version = 99` gives `ManifestError` mentioning a newer version. A garbage
      file gives `ManifestError`.
    - While one `open` holds the DB, a subprocess opening the same path gets
      `ManifestError` (lock).
- **Deliverable:** `state_dir()`, the manifest module and its tests. No CLI change.
- **Validation:** `mise run check`;
  `mise run test -- yandex-disk-rescue/tests/test_manifest.py yandex-disk-rescue/tests/test_auth.py`.
- **Doc updates:** package `README.md`:
  - Rewrite the Layout tree as current state (`paths.py`: XDG config and state
    directories; add `manifest.py`).
  - Add a "Manifest" section: its location, what it stores, the guarantee that it
    only holds committed scans, and "schema changes append a migration".
  - Add an `XDG_STATE_HOME` row to the environment variable table.

### Stage 4: `scan` command

- **Contract:**
  - `yandex-disk-rescue scan [REMOTE_PATH]` (default `disk:/`) lives in
    `yandex_disk_rescue/commands/scan.py`. It is registered in `cli.py` with one line
    and takes the Stage 2 `api_*` options.
  - `cli._format_size` moves to a module both `cli.py` and `commands/scan.py` import
    (for example `yandex_disk_rescue/units.py`, public `format_size(size: int) -> str`),
    because `commands/scan.py` cannot import from `cli.py`, which imports it. `auth
    status` output is unchanged (`test_status_formats_sizes_in_binary_units` is the
    lock). `rg _format_size` returns nothing.
  - Flow:
    1. `auth.get_token()`.
    2. `DiskClient.account()` for the uid.
    3. `Manifest.open(state_dir() / 'manifest.sqlite3')`.
    4. `begin_scan`.
    5. Walk the tree: a queue of folders, `concurrency` workers, rows streamed into
       the session.
    6. `commit`, then print the summary.
  - Stdout elements. The wording is the implementer's call; these elements are the
    contract:
    ```
    Scanned disk:/Photos: 1234 files, 56 folders, 12.3 GiB
    Files without a cloud checksum: 3
      disk:/Photos/old.bmp
    Changes since the last scan: 5 added, 2 changed, 1 removed
      + disk:/Photos/new.jpg
      ~ disk:/Photos/edited.jpg
      - disk:/Photos/gone.jpg
    Manifest: /home/u/.local/state/yandex-disk-rescue/manifest.sqlite3
    ```
    The first scan of a subtree prints `First scan of disk:/Photos` instead of the
    change block. Each path list shows at most 20 entries, then `… and N more`.
    Sizes use the shared `format_size`.
  - Errors follow the exit-code contract and the error message rule.
  - Ctrl+C: the command catches `KeyboardInterrupt` only to print
    `Interrupted; the manifest still holds the previous scan.` on stderr, then
    re-raises. Typer turns that into exit 130 (`RescueErrorGroup` catches only
    `RescueError`, so it does not interfere).
    - There is no custom SIGINT handler.
    - `commit()` runs only on the success path, never in `finally` or other cleanup.
      Cancellation can therefore never commit a partial scan.
- **Behaviour to lock:**
  - New `tests/test_scan.py`, using `CliRunner` and `fake_disk_api`:
    - A nested tree with one file that has no checksums exits 0. Stdout has the
      counts, the size and that file's path, and the manifest rows match the tree.
    - A rerun after `add_file`, changed content and `remove` lists exactly those
      `+`, `~` and `-` paths.
    - `scan disk:/missing` exits 1 with `does not exist` and no `Traceback`.
      `scan trash:/` exits 2. With no token, it exits 1 with #1's exact
      `No Yandex OAuth token. Run 'yandex-disk-rescue auth login' or set YANDEX_DISK_RESCUE_TOKEN.`
      message. `--api-concurrency 0` exits 2.
    - A 503 that outlasts the retries on one sub-folder exits 1, names that folder,
      ends in `(after N attempts)`, and leaves the previous manifest unchanged.
    - Subprocess `yandex-disk-rescue scan` against a fake whose deep folder is delayed
      (`inject` delay). Once the server logs that request, send SIGINT: exit 130, the
      stderr message, and the previous manifest unchanged. Repeat with SIGKILL: the
      manifest content is unchanged and the next scan succeeds.
  - Extend `tests/test_cli.py`: root `--help` lists `scan`.
- **Deliverable:** a working `scan`.
- **Validation:** `mise run check`;
  `mise run test -- yandex-disk-rescue/tests/test_scan.py yandex-disk-rescue/tests/test_cli.py yandex-disk-rescue/tests/test_auth.py`;
  `uv run yandex-disk-rescue scan --help`; `rg _format_size` is empty.
- **Doc updates:**
  - Root `README.md`, rewritten as current state:
    - the Status line (today: "The CLI skeleton exists ... with OAuth token handling
      under `yandex-disk-rescue auth`. Download features are being built."): `auth`
      and `scan` exist, downloads are being built;
    - new sections "Scanning a folder" (usage, output, manifest location) and
      "Exit codes" (the table above). Fold the exit-code sentences at the end of the
      `auth status` paragraph ("Every command reports such failures the same way;
      usage errors exit `2`.") into the new section so the rule lives in one place.
  - Package `README.md`: the Layout tree (`commands/scan.py`, the size-formatter
    module) and the usage block.

### Stage 5: Config file layer

- **Contract:**
  - The config file is `config_dir() / 'config.toml'`, or another path given by the
    root option `--config PATH` or the env var `YANDEX_DISK_RESCUE_CONFIG`.
  - It has flat top-level keys named after the registry options, for example
    `api_interval_ms = 100`, `api_url = "..."` and `client_id = "..."`.
  - Precedence: CLI flag > env var > config file > default. It applies to every
    command that takes the options, `auth` included.
  - Errors (a `RescueError` subclass with `exit_code = 2`, rendered by
    `RescueErrorGroup`):
    - A missing default file means the defaults are used.
    - These all exit 2 with `Error: config file <path>: <key>: <reason>`:
      - a missing file named by `--config` or the env var;
      - a TOML syntax error;
      - an unknown key (`token` included: the token never lives in this file);
      - a wrong type;
      - an invalid value.
  - The registry is the single list of keys. An option added in #4 or #6 is
    available from all three sources without further work.
- **Behaviour to lock:**
  - New `tests/test_config.py`, with real files in `tmp_path`:
    - With `api_interval_ms = 200` in the config, `scan`'s logged start gaps are
      about 190 ms or more. `--api-interval-ms 50` overrides it, and the env var
      overrides the file.
    - With `api_url` set in the config file, and no env var, `auth status` reaches
      the fake server. This shows the auth commands use the one registry.
    - With `client_id` set in the config file, `auth login` prints an authorize URL
      carrying that ClientID.
    - An unknown key, `api_concurrency = 0`, `api_concurrency = "x"`, broken TOML,
      and `--config /nonexistent` each exit 2 with the path and the key in stderr.
- **Deliverable:** config file support for every registered option.
- **Validation:** `mise run check`;
  `mise run test -- yandex-disk-rescue/tests/test_config.py`; `mise run ci`.
- **Doc updates:**
  - Root `README.md`: a new "Configuration" section. It covers the options table, the
    env vars, the file location with an example, precedence and validation. Fold the
    Stage 2 option mentions and the "Using your own OAuth app" precedence sentence
    ("the option wins") into it, so each option is described in one place.
  - Package `README.md`: the Layout tree and a `YANDEX_DISK_RESCUE_CONFIG` row in the
    environment variable table.
  - `.env.example`: add a commented `YANDEX_DISK_RESCUE_CONFIG` entry.

## Manual verification (main runs this with the user; not in CI)

With a real token (`auth login`, or `YANDEX_DISK_RESCUE_TOKEN` from the user's `.env`):

1. Run `uv run yandex-disk-rescue scan` on an account with thousands of files. It
   should exit 0 with no 429 failure in the output, and the totals should be
   plausible against the web UI. Check that `sha256` values are present in the
   manifest.
2. Rerun. It should report 0 added, 0 changed and 0 removed.
3. Upload, rename and delete one file each in the web UI, then rerun. It should
   report exactly those changes.
4. Press Ctrl+C mid-scan. It should exit 130, and the manifest row count should be
   unchanged (`sqlite3 <manifest> 'select count(*) from <entries table>'`).
5. Run `scan disk:/does-not-exist`. It should exit 1 with a clear message.
6. Run `auth status`. It should still work through `DiskClient`.

## Discovered issues

- **Fix in this PR:**
  - Two concurrent runs on one manifest could discard each other's staged rows.
    Stage 3 adds the advisory run lock.
  - #1's `--client-id` is an ad hoc Typer option with its own env var constant and
    validator, a second options mechanism. Stage 2 moves it into the registry.
  - The tests use anyio's pytest plugin, which today is only a transitive dependency
    of httpx. Stage 1 declares it in the root dev group.
- **Filed as issues:** None. Scan progress output could become an explicit `scan`
  bullet on #6; that is main's call.

## Risks / open questions

- The public field reference does not document `sha256`, although the API returns it
  in practice. Manual step 1 confirms it. Files without it fall back to MD5 in #3.
- The default timeout is 30 s (#1's value), not the original tool's 3 s. The 3 s value
  applies to download-link requests, while a listing page of 1000 items takes longer.
  #3 may add a separate link timeout.
- Offset pagination over a folder that changes during the scan can skip items. The
  next scan picks them up. Duplicates are harmless because rows are keyed by path.
- Stage 1 changes `auth` internals and test fixtures that #1 just landed. #1's tests
  are the regression lock; the edits allowed to them are listed per stage, and their
  assertions must not be weakened.
- `auth status` and `auth login` pay the limiter's first-request cost and, on a
  throttling status, up to `api_retries` backoffs before failing. That is intended
  (retries apply everywhere); it is why the affected #1 tests pin
  `YANDEX_DISK_RESCUE_API_RETRIES=0`.
- `scan` lives in `commands/scan.py` while the `auth` commands stay inline in
  `cli.py`. Moving `auth` to `commands/auth.py` would make the layout uniform; it is
  left out to keep the diff on #2's scope, and main may ask for it as a cheap extra
  commit.
