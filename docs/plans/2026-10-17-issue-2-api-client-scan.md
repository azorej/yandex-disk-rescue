# Plan: Yandex Disk API client with adaptive rate limiting and `scan`

Issue: #2 (umbrella #9)
Branch: feat/api-client-scan (from `main` after #1 has merged)

## Context

Today the CLI has no `scan`: `uv run yandex-disk-rescue scan` exits 2 with `No such
command 'scan'`. `mise run check` is green with 3 tests. #1 merges first (main's
decision) and adds the following, which this plan consumes under their #1-plan names
(`docs/plans/2026-10-17-issue-1-oauth-token-management.md`):

- `errors.RescueError` (`exit_code = 1`). A `TyperGroup` subclass on the root app
  renders it as `Error: <message>` on stderr, with no traceback.
- `paths.config_dir()`.
- In `auth`: `get_token()` (raises `RescueError` subclasses), `api_base_url()`,
  `fetch_account(token) -> Account(login, total_space, used_space)` (one
  `GET /v1/disk/` with no retries), `TokenRejectedError` (401 and 403) and
  `DiskApiError` (any other failure, including transport errors and unexpected
  bodies).
- Commands `auth status` and `auth login` that call `fetch_account`.
- In `tests/conftest.py`: an autouse env/XDG isolation fixture and `fake_disk_api`.
  `fake_disk_api` yields a state object with `base_url`, a request log (method, path,
  headers) and a `(method, path)` routes table pre-seeded with `GET /v1/disk/`.

Every later ticket (#3 to #6) needs three things from this one: a rate-limit-safe view
of the remote tree in one crash-safe SQLite database, one options mechanism, and one
fake Yandex Disk server (#9 cross-cutting decisions). This plan builds them by growing
#1's code, not beside it. There is one HTTP client, one `GET /v1/disk/` code path, one
fake server and one way to read the API URL.

Before execution starts, main asks the architect to re-verify these seam names against
the merged #1 code.

## Goal

- `yandex-disk-rescue scan [REMOTE_PATH]` (default `disk:/`) walks the folder
  recursively through `GET /v1/disk/resources`, page by page, with up to
  `api_concurrency` requests in flight. It records every file and folder in
  `state_dir()/manifest.sqlite3`.
- The command prints a summary: files, folders, total size and files without a cloud
  checksum. On a rerun it also prints what was added, changed and removed.
- Every API request from every command (`scan`, `auth status`, `auth login`) goes
  through one `DiskClient`, with one shared adaptive limiter and one bounded retry
  policy.
- The manifest only ever holds the result of a *completed* scan. Ctrl+C, SIGKILL or a
  failure mid-scan leave the previous manifest content unchanged.
- One options mechanism: CLI flag > env var > config file > default. Ranges are
  validated, and a bad value is rejected at startup with exit 2.

## Non-goals

- Downloading content (#3), download concurrency limits (#4), progress, the log file
  and `--json` (#6). `scan` prints only its final summary.
- Trash (`trash:/`) and auto-upload sections. Schemes other than `disk:` are rejected.
- Resuming an interrupted scan. A rerun rescans.
- HTTP/2 (the `h2` extra). Revisit in #4.

## Exit-code contract (extends #1's; reused by #3 to #6)

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | `RescueError`: no token, token rejected, remote path missing or not a folder, API error after retries, manifest unusable or locked |
| 2 | Invalid input: Typer/Click usage errors, an out-of-range option from any source, an invalid config file, an unsupported path scheme (`RescueError` subclasses with `exit_code = 2`) |
| 130 | Interrupted (Ctrl+C). Typer already maps `KeyboardInterrupt` to `Exit(130)` (`typer/core.py:201-202`) |

Error messages name the method and the API path. They never include headers or the
token.

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
  - Ownership moves:
    - `Account`, `TokenRejectedError` and `DiskApiError` move from `auth` into `api/`.
      #1's semantics and message texts are kept: 401 and 403 give
      `TokenRejectedError`; any other non-2xx, a transport error or an unexpected body
      gives `DiskApiError`.
    - `Account` gains `uid` (from `user.uid`), validated like the other fields. The
      manifest keys rows by account. Any #1 test that overrides the account body with
      its own "valid" JSON must add `user.uid`, or it fails as an unexpected response.
    - `auth.fetch_account` is deleted. `auth status` and `auth login` call
      `DiskClient.account()` through `asyncio.run`, with `base_url=auth.api_base_url()`
      (Stage 2 removes `api_base_url`).
    - Sweep: `rg 'fetch_account|from yandex_disk_rescue.auth import .*(Account|TokenRejectedError|DiskApiError)'`
      returns nothing outside `api/`.
  - New errors, all `RescueError` subclasses:
    - `RemotePathNotFoundError` (404 on a resource): `Remote path 'disk:/x' does not exist`.
    - `NotAFolderError`: `list_folder` was called on a file.
    - `UnsupportedPathError` (`exit_code = 2`).
  - `list_folder`:
    - It yields every direct child, paging with `limit` (internal constant 1000) and
      `offset` until `offset >= _embedded.total`. It sends `sort=name` and a `fields=`
      filter limited to `type`, the `Resource` fields and `_embedded.total/limit/offset`.
    - It must cope with pages shorter than `limit`.
    - Malformed bodies give the same unexpected-response `DiskApiError` that #1 defined.
  - `normalize_remote_path`:
    - `disk:/A/` becomes `disk:/A`; `/A` and `A` become `disk:/A`.
    - `disk:`, `disk:/` and `/` become `disk:/`.
    - Any other scheme raises `UnsupportedPathError`.
  - Fake server: `fake_disk_api` keeps its name and its #1 attributes (`base_url`,
    request log, routes table for overrides). The fixture grows in place. If the
    server class moves to a tests helper module, add `yandex-disk-rescue/tests` to
    pytest `pythonpath` in the root `pyproject.toml`. #1's handler code is replaced,
    not kept beside the new one. The additions are:
    - An in-memory tree: `add_dir(path)` and
      `add_file(path, content: bytes, *, modified=..., md5=..., sha256=...)`.
      Checksums default to the real digests of `content`, and `None` omits them.
      Content is stored now so that #3 can serve bytes, Range requests and corruption.
    - `remove(path)`.
    - `page_cap`: the maximum number of items per page, whatever `limit` asks for.
    - `account` fields that feed the pre-seeded `GET /v1/disk/`.
    - `token`: the expected value of `Authorization: OAuth <token>`. Anything else
      gets a 401 JSON error.
    - Built-in `GET /v1/disk/resources`, served from the tree, with Yandex-style JSON
      errors.
    - A fault script, `inject(method, route, *responses, where=None)`. Each response
      is a status (with headers such as `Retry-After` and a JSON body) or a delay in
      seconds before answering normally. The responses are used in order, and then
      the route serves normally again. `where` matches on query parameters, for
      example `path`.
    - Each request log entry gains a monotonic timestamp. The server records the
      peak number of requests in flight.
    - The routes table stays the extension point for #3 (`/v1/disk/resources/download`
      and a download host).
- **Behaviour to lock:**
  - New `yandex-disk-rescue/tests/test_api_client.py`. Async tests use anyio's pytest
    plugin; anyio ships with httpx, and `anyio_backend` is fixed to `'asyncio'`.
    - With `page_cap = 3` and a folder of 10 mixed children, `list_folder` yields all
      10 with correct fields, and the log shows offsets 0, 3, 6, 9.
    - A 404 gives `RemotePathNotFoundError` naming the path. A file path gives
      `NotAFolderError`.
    - `account()` returns `uid` and `login` from the fixture's account fields.
    - `normalize_remote_path` has a table test.
  - Regression lock: #1's `test_auth.py` and `test_cli.py` move to the grown fixture
    in this commit. Any override they make of the old handler becomes a routes or
    `inject` override, and they pass unchanged in what they assert.
- **Deliverable:** the `api/` package. `auth` uses it, `fetch_account` is gone, and
  there is a single grown fake server.
- **Validation:**
  - `mise run check`.
  - `mise run test -- yandex-disk-rescue/tests/test_api_client.py yandex-disk-rescue/tests/test_auth.py yandex-disk-rescue/tests/test_cli.py`
    (all #1 auth tests pass on the new fixture).
  - The `rg` sweep above.
- **Doc updates:** package `README.md`:
  - Rewrite the Layout tree as current state (add `api/`).
  - Rewrite the `conftest.py` / tests paragraph as current state: the fake server's
    tree, `page_cap`, `inject` and the request log.

### Stage 2: Options registry, shared adaptive limiter, bounded retries

- **Contract:**
  - Dependency: `uv add --package yandex-disk-rescue 'tenacity>=9.1.4'`. If the stage
    runs before 2026-10-21, also run `uv lock --upgrade-package tenacity==9.1.4`, so
    that the locked version is at least two weeks old (9.2.1 was published
    2026-10-07).
  - `yandex_disk_rescue/settings.py` is the one registry of options. Each entry
    defines:
    - the option name, which is also the config-file key from Stage 5;
    - the flag;
    - the env var;
    - the default;
    - the validated range.

    A reusable `Annotated` option type per entry lets later tickets add entries
    instead of redefining options. Range validation applies to every source; a
    violation exits 2 and names the option and the range. This stage's entries:

    | Option / flag | Env var | Default | Range |
    |---|---|---|---|
    | `api_interval_ms` / `--api-interval-ms` | `YANDEX_DISK_RESCUE_API_INTERVAL_MS` | 50 | 25–1000 |
    | `api_concurrency` / `--api-concurrency` | `YANDEX_DISK_RESCUE_API_CONCURRENCY` | 7 | 1–30 |
    | `api_timeout` / `--api-timeout` (s) | `YANDEX_DISK_RESCUE_API_TIMEOUT` | 10 | 1–300 |
    | `api_retries` / `--api-retries` | `YANDEX_DISK_RESCUE_API_RETRIES` | 5 | 0–20 |
    | `api_url` / `--api-url` (hidden) | `YANDEX_DISK_RESCUE_API_URL` | `https://cloud-api.yandex.net` | http(s) URL |

  - Every command that talks to the API (`auth status` and `auth login` now, `scan` in
    Stage 4) takes these options and builds its client from them.
  - `auth.api_base_url()` is deleted. `rg api_base_url` returns nothing.
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
    - When the retries run out, the client raises `DiskApiError`. Its message keeps
      #1's wording and adds the attempt count, for example
      `GET /v1/disk/resources?path=disk:/A failed after 6 attempts: HTTP 503 ...` and
      `Could not reach the Yandex Disk API at <base url> after 6 attempts: <reason>`.
- **Behaviour to lock:**
  - Extend `tests/test_api_client.py`:
    - A first listing request answered with 429 and `Retry-After: 1` still lets the
      listing succeed, and the next request arrives at least 1 s later (log
      timestamps).
    - Three 503s followed by a success let the listing succeed. Four 503s with
      `retries=2` give `DiskApiError` containing `after 3 attempts` and `503`.
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
    - Every #1 test that drives a throttling status (408, 429, 5xx) or a transport
      error sets `YANDEX_DISK_RESCUE_API_RETRIES=0` so it stays fast. The closed-port
      test still asserts `Could not reach`.
- **Deliverable:** all API traffic is paced and retried, the options registry exists,
  and `api_base_url` is gone.
- **Validation:** `mise run check`; `mise run test -- -k "rate or api_client or auth"`;
  `rg api_base_url` is empty.
- **Doc updates:**
  - Root `README.md`: add a new "Rate limiting" section written as current state
    (limiter rules, retry rule, 300 s cap). Rewrite the "Authentication" mention of
    `YANDEX_DISK_RESCUE_API_URL` so it points at the options table.
  - Package `README.md`: rewrite the Layout tree and the env var list as current state.
  - `.env.example`: keep #1's commented `YANDEX_DISK_RESCUE_API_URL` entry. Add no
    other entries.

### Stage 3: Manifest database (schema, migrations, atomic scan commit, run lock)

- **Contract:**
  - `paths.state_dir() -> Path`: `$XDG_STATE_HOME/yandex-disk-rescue`, falling back to
    `~/.local/state/yandex-disk-rescue` when the variable is unset, empty or not
    absolute. Same rule as `config_dir()`. It does not create the directory.
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
  - Extend `tests/test_auth.py`, where #1's `config_dir` fallback test lives, or the
    module that holds that test: the unset, empty and relative cases are
    parametrised over both `config_dir` and `state_dir`.
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
  - Rewrite the Layout tree as current state.
  - Add a "Manifest" section: its location, what it stores, the guarantee that it
    only holds committed scans, and "schema changes append a migration".

### Stage 4: `scan` command

- **Contract:**
  - `yandex-disk-rescue scan [REMOTE_PATH]` (default `disk:/`) lives in
    `yandex_disk_rescue/commands/scan.py`. It is registered in `cli.py` with one line
    and takes the Stage 2 API options.
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
    Sizes use #1's binary-unit formatter, which is reused, not duplicated.
  - Errors follow the exit-code contract.
  - Ctrl+C: the command catches `KeyboardInterrupt` only to print
    `Interrupted; the manifest still holds the previous scan.` on stderr, then
    re-raises. Typer turns that into exit 130.
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
      `scan trash:/` exits 2. With no token, it exits 1 with #1's `auth login` hint.
      `--api-concurrency 0` exits 2.
    - A 503 that outlasts the retries on one sub-folder exits 1, names that folder,
      and leaves the previous manifest unchanged.
    - Subprocess `yandex-disk-rescue scan` against a fake whose deep folder is delayed
      (`inject` delay). Once the server logs that request, send SIGINT: exit 130, the
      stderr message, and the previous manifest unchanged. Repeat with SIGKILL: the
      manifest content is unchanged and the next scan succeeds.
  - Extend `tests/test_cli.py`: root `--help` lists `scan`.
- **Deliverable:** a working `scan`.
- **Validation:** `mise run check`;
  `mise run test -- yandex-disk-rescue/tests/test_scan.py yandex-disk-rescue/tests/test_cli.py`;
  `uv run yandex-disk-rescue scan --help`.
- **Doc updates:**
  - Root `README.md`, rewritten as current state:
    - the Status line: `auth` and `scan` exist, downloads are being built;
    - new sections "Scanning a folder" (usage, output, manifest location) and
      "Exit codes" (the table above).
  - Package `README.md`: the Layout tree and usage block.

### Stage 5: Config file layer

- **Contract:**
  - The config file is `config_dir() / 'config.toml'`, or another path given by the
    root option `--config PATH` or the env var `YANDEX_DISK_RESCUE_CONFIG`.
  - It has flat top-level keys named after the registry options, for example
    `api_interval_ms = 100` and `api_url = "..."`.
  - Precedence: CLI flag > env var > config file > default. It applies to every
    command that takes the options, `auth` included.
  - Errors:
    - A missing default file means the defaults are used.
    - These all exit 2 with `Error: config file <path>: <key>: <reason>`:
      - a missing file named by `--config` or the env var;
      - a TOML syntax error;
      - an unknown key (`token` included: the token never lives in this file);
      - a wrong type;
      - an out-of-range value.
  - The registry is the single list of keys. An option added in #4 or #6 is
    available from all three sources without further work.
- **Behaviour to lock:**
  - New `tests/test_config.py`, with real files in `tmp_path`:
    - With `api_interval_ms = 200` in the config, `scan`'s logged start gaps are
      about 190 ms or more. `--api-interval-ms 50` overrides it, and the env var
      overrides the file.
    - With `api_url` set in the config file, and no env var, `auth status` reaches
      the fake server. This shows the auth commands use the one registry.
    - An unknown key, `api_concurrency = 0`, `api_concurrency = "x"`, broken TOML,
      and `--config /nonexistent` each exit 2 with the path and the key in stderr.
- **Deliverable:** config file support for every registered option.
- **Validation:** `mise run check`;
  `mise run test -- yandex-disk-rescue/tests/test_config.py`; `mise run ci`.
- **Doc updates:**
  - Root `README.md`: a new "Configuration" section. It covers the options table, the
    env vars, the file location with an example, precedence and validation. Fold the
    Stage 2 option mentions into it, so each option is described in one place.
  - Package `README.md`: the Layout tree.

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

- **Fix in this PR:** two concurrent runs on one manifest could discard each other's
  staged rows. Stage 3 adds the advisory run lock.
- **Filed as issues:** None. Scan progress output could become an explicit `scan`
  bullet on #6; that is main's call.

## Risks / open questions

- The public field reference does not document `sha256`, although the API returns it
  in practice. Manual step 1 confirms it. Files without it fall back to MD5 in #3.
- The default timeout is 10 s, not the original tool's 3 s. The 3 s value applies to
  download-link requests, while a listing page of 1000 items takes longer. #3 may add
  a separate link timeout.
- Offset pagination over a folder that changes during the scan can skip items. The
  next scan picks them up. Duplicates are harmless because rows are keyed by path.
- Stage 1 changes `auth` internals and test fixtures that #1 just landed. #1's tests
  are the regression lock, and their assertions must not be weakened.
- The seam names here come from the #1 plan. They will be re-verified against the
  merged code before execution.
