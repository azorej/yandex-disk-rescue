# Plan: OAuth token management (`auth login` / `status` / `logout`)

Issue: #1 (epic #9)
Branch: feat/auth-token-management

## Context

The CLI is a bare Typer skeleton: `uv run yandex-disk-rescue auth --help` exits 2 with
`No such command 'auth'`, and `mise run check` passes with 3 tests (`--version`, `--help`, console
script). Every later feature needs a Yandex OAuth token, so this ticket adds token storage,
resolution and verification. It also adds the project-wide error/exit contract and the XDG
config-dir helper that #2 consumes. Order: #1 is implemented and merged first, and #2 branches
on top of it. #2 consumes `errors.py`, `paths.py`, `auth.py` and the test fixtures directly, with
no stubs.

## Goal

Public contracts this PR establishes. #2 and later tickets build on them.

- `yandex_disk_rescue.errors.RescueError(Exception)`: an expected, user-facing failure with a
  class attribute `exit_code: int = 1`. When any `RescueError` escapes a command, the app writes
  `Error: <message>` to stderr and exits with `exit_code`, with no traceback. This holds under
  `CliRunner` and under the installed console script alike. Usage errors keep Typer's exit
  code 2.
- `yandex_disk_rescue.paths.config_dir() -> Path`: `$XDG_CONFIG_HOME/yandex-disk-rescue`, or
  `~/.config/yandex-disk-rescue` when `XDG_CONFIG_HOME` is unset, empty or not absolute (XDG
  spec). It does not create the directory. #2 adds `state_dir()` next to it with the same rule.
- `yandex_disk_rescue.auth`:
  - `TOKEN_ENV_VAR = 'YANDEX_DISK_RESCUE_TOKEN'`
  - `get_token() -> str`: returns the token or raises a `RescueError` subclass (`NoTokenError`,
    `InsecureTokenFileError`). This is the interface #2 consumes. Consumers code against
    `RescueError`, not the individual subclasses.
  - `resolve_token() -> Token`, where `Token` is a frozen dataclass with `value: str`
    (`repr=False`) and `source: TokenSource`. `TokenSource` is an enum: `ENV` or `FILE`.
    Precedence: env var, then file. An env var that is set but empty or whitespace-only raises
    `NoTokenError` naming the variable. It does not silently fall back to the file.
  - `token_path() -> Path` = `config_dir() / 'token'`
  - `save_token(value: str) -> Path`, `delete_token() -> bool`
  - `api_base_url() -> str`: `YANDEX_DISK_RESCUE_API_URL` or `https://cloud-api.yandex.net`.
    #2 may fold this into its config mechanism.
  - `fetch_account(token: str) -> Account`, where `Account` has `login`, `total_space` and
    `used_space` (bytes).
- CLI: `yandex-disk-rescue auth login [--client-id ID]`, `auth status`, `auth logout`.
- Security invariants: the token file is mode 0600 inside a 0700 directory. The token never
  appears in stdout, stderr, `--help`, exception messages or `repr`.
  `pretty_exceptions_show_locals=False` is passed explicitly. Typer 0.27.3 already defaults to
  False, but pinning it means a future default change can't print the token in a traceback.

## Non-goals

- Device-code flow, refresh tokens, multiple accounts or profiles, keyring storage.
- Revoking the token server-side: `/revoke_token` needs the client secret, which is not shipped.
- The shared `DiskClient`, rate limiting and the config file (#2).
- Logging (#6). This plan only guarantees nothing secret is reachable through `repr` or messages.

## Stages

### Stage 1: Error contract, config dir, token store and `auth logout`

- **Contract:**
  - `errors.py`: the `RescueError` class as described in Goal. The `cli.app` Typer instance is
    built with an explicit `pretty_exceptions_show_locals=False` (see Goal). Rendering
    `RescueError` in the CLI is Stage 2's job (see below).
  - `paths.py`: `config_dir()` as described in Goal.
  - `auth.py` (no network in this stage): `TOKEN_ENV_VAR`, `Token`, `TokenSource`,
    `NoTokenError`, `token_path()`, `resolve_token()`, `get_token()`, `save_token()`,
    `delete_token()`.
    - `save_token(value)` strips surrounding whitespace. An empty value or one with internal
      whitespace raises `InvalidTokenError(RescueError)`, whose message does not echo the
      value. It creates `config_dir()` with mode 0700 if missing. It writes atomically: the
      temp file in the same directory is created with mode 0600 from the start (never chmod
      after writing), then fsync and `os.replace`. It returns the path.
    - The file is read by `resolve_token()`. If the file has any group or other permission bit,
      it raises `InsecureTokenFileError(RescueError)`:
      `Token file <path> is accessible by other users; run: chmod 600 <path>`. This is the
      same policy as ssh. An empty file raises `NoTokenError`. A missing file with no env var
      raises `NoTokenError`: `No Yandex OAuth token. Run 'yandex-disk-rescue auth login' or
      set YANDEX_DISK_RESCUE_TOKEN.`
    - `delete_token()` returns `False` when there is no file and does not raise.
  - CLI: an `auth` Typer sub-app (`help='Manage the Yandex OAuth token.'`) with `logout`:
    - It deletes the stored token and prints `Removed the stored token <path>.`, or
      `No stored token at <path>.` when there is none. Both exit 0, so logout is idempotent.
    - It always prints the revoke hint: `Also revoke this app's access in Yandex ID:
      https://id.yandex.com/personal/data-access`.
    - If `YANDEX_DISK_RESCUE_TOKEN` is set, it also warns on stderr that the variable still
      provides a token. The warning names the variable, never the value.
- **Behaviour to lock:**
  - New `yandex-disk-rescue/tests/conftest.py` with an autouse fixture. It deletes every
    `YANDEX_DISK_RESCUE_*` variable and points `XDG_CONFIG_HOME` and `XDG_STATE_HOME` at
    `tmp_path` subdirectories. Without it, mise's `.env` loading leaks into tests.
  - New `yandex-disk-rescue/tests/test_auth.py`:
    - `save_token` then `resolve_token` round-trips with `source == FILE`. The file is at
      `$XDG_CONFIG_HOME/yandex-disk-rescue/token`, its `stat` mode is `0o600` and the
      directory's is `0o700`.
    - With unset, empty and relative `XDG_CONFIG_HOME`, `config_dir()` falls back under
      `Path.home()` (set `HOME` to `tmp_path`).
    - When both the env var and the file are present, the env var wins. An env var set to
      `''` raises `NoTokenError` naming the variable.
    - A token file chmodded to 0644 raises `InsecureTokenFileError`.
    - `repr(resolve_token())` does not contain the token value.
    - `auth logout` via `CliRunner`: the file is gone, exit 0 and the revoke URL is printed. A
      second run prints `No stored token` and exits 0.
  - Extend `yandex-disk-rescue/tests/test_cli.py`: `auth --help` lists `logout`. The
    console-script test of the `RescueError` contract lands in Stage 2. That is the first
    command that resolves a token, and no debug command is added to test it earlier. So the
    CLI-side rendering of `RescueError` is implemented in Stage 2 together with its first
    caller. Stage 1 ships only the `errors.py` class and the pinned `show_locals=False`.
- **Deliverable:** `errors.py`, `paths.py`, `auth.py`, the `auth` sub-app with `logout`, and
  the tests.
- **Validation:** `mise run check`;
  `mise run test -- yandex-disk-rescue/tests/test_auth.py yandex-disk-rescue/tests/test_cli.py`;
  `uv run yandex-disk-rescue auth logout`.
- **Doc updates:**
  - `yandex-disk-rescue/README.md`: rewrite the Layout tree to the current modules
    (`errors.py`, `paths.py`, `auth.py`, `tests/conftest.py`, `tests/test_auth.py`).
  - Root `README.md`: add an "Authentication" section that describes the current state:
    precedence (env var over file), file location and permissions, `auth logout` and the
    revoke link. Update the Status note to mention `auth`.
  - `.env.example`: replace the template placeholder `# EXAMPLE_API_KEY=changeme` with a
    commented entry `# YANDEX_DISK_RESCUE_TOKEN=`. Note there that if the variable is set but
    blank, it is an error, not a fallback to the stored file.

### Stage 2: `auth status` against the Disk API

- **Contract:**
  - CLI rendering of `RescueError` (Goal contract), with a pinned mechanism:
    - Define a `typer.core.TyperGroup` subclass whose `invoke(ctx)` catches `RescueError`,
      writes `Error: <message>` to stderr (`typer.echo(..., err=True)`), and raises
      `typer.Exit(err.exit_code)`.
    - Pass it as `typer.Typer(cls=<subclass>)` for the root app. Nested sub-app commands
      (`auth status`) run inside the root group's `invoke`, so one subclass covers everything.
    - Rejected alternatives, verified by the critic's probe:
      - Overriding `Typer.__call__`: `CliRunner.invoke` bypasses it.
      - A `ClickException` adapter: Typer renders it as a rich boxed panel, not `Error: ...`.
      - Importing `typer._click` (private).
    - `errors.py` imports neither click nor typer.
    - The contract must hold under `CliRunner.invoke(app, ...)` and under the console script.
  - Runtime dependency: `uv add --package yandex-disk-rescue httpx`.
  - `auth.fetch_account(token) -> Account` sends one `GET {api_base_url()}/v1/disk/` with
    `Authorization: OAuth <token>`, a finite timeout and no retries (that is #2's client job).
    It maps outcomes to `RescueError` subclasses whose messages never contain the token:
    - 401: `TokenRejectedError`: `Yandex rejected the token (invalid, expired or revoked).
      Run 'yandex-disk-rescue auth login' to store a new one.`
    - 403: the same class, with a message about missing Disk permissions for the app.
    - Any other non-2xx: `DiskApiError`: `Yandex Disk API returned HTTP <code>: <description>`.
      The description is the JSON `description` field when present.
    - `httpx.TransportError`: `DiskApiError`: `Could not reach the Yandex Disk API at <base
      url>: <reason>`.
    - A 200 whose body is not JSON, is not a JSON object, lacks `user.login`, `total_space` or
      `used_space`, or has them with the wrong types (login must be `str`, sizes non-negative
      `int`): `DiskApiError` saying the response was unexpected. Do not use a default value.
  - `auth status` prints, on stdout, exit 0:
    ```
    Token: environment variable YANDEX_DISK_RESCUE_TOKEN   (or: file <path>)
    Account: <login>
    Disk usage: <used> of <total> used (<pct>%)
    ```
    Sizes are in binary units, one decimal (`1.5 GiB`). Failures follow the `RescueError`
    contract with exit 1.
- **Behaviour to lock:**
  - `conftest.py` (owned by #1) gains a `fake_disk_api` fixture: a stdlib `ThreadingHTTPServer`
    on `127.0.0.1:0` in a background thread, real sockets, not `MockTransport`. The fixture:
    - yields a state object with `base_url`, a request log (method, path, headers) and a routes
      table mapping `(method, path)` to a response (status, headers, raw body bytes).
    - is pre-seeded with `GET /v1/disk/` returning a valid account. Tests override that route
      for 401, 500, non-JSON, etc.
    - sets `YANDEX_DISK_RESCUE_API_URL` via monkeypatch.

    A routes/state object, rather than hard-coded handlers, is what #2 needs to grow this same
    fixture into the single shared fake Yandex Disk server (pagination, 429/`Retry-After`,
    scripted faults). There is no second server.
  - `test_auth.py`:
    - With a stored token and a 200 body, `auth status` exits 0 and shows the login and
      `Disk usage:`. The server saw `OAuth <token>`.
    - With the env var set, the source line names the variable.
    - A 401 gives exit 1, a `rejected` message, no `Traceback` and no token value in the output.
    - With an API URL pointing at a closed port, it exits 1 with `Could not reach`.
    - A 200 with a non-JSON body (`b'<html>'`) gives exit 1 with the unexpected-response message.
      A 200 with `used_space: "x"` gives the same.
    - With no token at all, it exits 1 with the `auth login` hint.
  - `test_cli.py`: the console-script subprocess runs `auth status` with a garbage token and the
    API URL pointing at the fake server returning 401. Assert exit 1, stderr starts with
    `Error:`, no `Traceback` and the token sentinel is absent. This locks the
    entry-point-level contract.
- **Deliverable:** `fetch_account`, `Account`, `auth status`, the fake-server fixture and the tests.
- **Validation:** `mise run check`; `mise run test -- -k status`. Manual check, optional, needs
  a real token: `YANDEX_DISK_RESCUE_TOKEN=... uv run yandex-disk-rescue auth status`.
- **Doc updates:**
  - Root `README.md` "Authentication": rewrite to include `auth status` and its exit codes.
  - Package README: Layout (`conftest.py` purpose) and the env vars, including
    `YANDEX_DISK_RESCUE_API_URL` as a supported override.
  - `.env.example`: add a commented `# YANDEX_DISK_RESCUE_API_URL=https://cloud-api.yandex.net`.

### Stage 3: `auth login` with the built-in client ID

- **Contract:**
  - `auth.DEFAULT_CLIENT_ID = '9e2a0382c430429c9d57913b61d7d48f'`: the maintainer's registered
    app. It is public, used with the implicit flow, and no secret is shipped.
  - `auth.REQUESTED_SCOPES = ('cloud_api:disk.read', 'cloud_api:disk.info')`.
  - `auth.authorize_url(client_id: str) -> str` returns
    `https://oauth.yandex.ru/authorize?response_type=token&client_id=<id>&scope=<scopes>`.
    - The scopes are joined with a single space and URL-encoded with
      `urllib.parse.urlencode(..., quote_via=urllib.parse.quote)`, so the space becomes `%20`.
      The maintainer verified in a browser on 2026-10-17: the comma-separated form returns
      `invalid_scope`, while the `%20`-separated form and a single scope both work.
    - Asking for only the two Disk rights stops the consent screen from offering the other rights
      the built-in app is registered with: WebDAV, the app folder, and the Yandex 360 Security
      API.
    - Yandex ID docs ("Authorization code request", "Debug token"):
      - `scope` must be a subset of the app's registered rights.
      - Leaving it out grants every registered right.
      - Rights requested through `scope` are treated as optional by the consent screen.
    - Users who pass their own `--client-id` must have registered both rights in that app.
  - `auth login --client-id ID` takes its value from the option, then
    `YANDEX_DISK_RESCUE_CLIENT_ID` (Typer `envvar=`), then `DEFAULT_CLIENT_ID`. A value that is
    not 32 lowercase hex characters is a usage error (exit 2). The product code does not read
    `DISK_APP_*` names.
  - Flow:
    1. Print the URL and short instructions on stderr, so stdout stays clean.
    2. Prompt `Paste the token` with hidden input (`typer.prompt(..., hide_input=True)`).
    3. If the pasted text contains `access_token=`, extract the parameter (users may paste the
       whole redirect URL `...#access_token=...&token_type=bearer&...`).
    4. Verify with `fetch_account`. On success, `save_token` and print
       `Logged in as <login>. Token saved to <path>.`, exit 0. On failure, nothing is written
       and the `RescueError` contract applies (exit 1).
    5. If `YANDEX_DISK_RESCUE_TOKEN` is set, warn on stderr that it overrides the saved file.
  - There is no `--token` option. The token never travels as an argv value: it would show in
    `ps` and shell history, and in `--help` through Typer's show-default or envvar display.
- **Behaviour to lock:** `test_auth.py`:
  - `auth login` with `input='<token>\n'` and the fake API returning 200 exits 0. Then
    `auth status` exits 0 with the login (the issue's acceptance criterion end to end). The file
    mode is 0600.
  - The printed URL, parsed with `urllib.parse`, has host `oauth.yandex.ru`, path `/authorize`,
    `response_type=token`, `client_id=9e2a0382c430429c9d57913b61d7d48f` and
    `scope` equal to `cloud_api:disk.read cloud_api:disk.info` after decoding, and the raw URL
    contains `cloud_api%3Adisk.read%20cloud_api%3Adisk.info` or the same with unescaped colons
    (`%20`, never `,` or `+`). With `--client-id` or the env var it has the
    override.
  - Pasting a full redirect URL stores only the token.
  - A 401 during login gives exit 1, and no token file is created.
  - The pasted token is not echoed in the output.
  - `--client-id nothex` exits 2.
  - A `--help` sweep: with the env var set to a sentinel, `--help` for the root app, `auth`,
    `login`, `status` and `logout` never contains the sentinel.
- **Deliverable:** `auth login`, `authorize_url`, `DEFAULT_CLIENT_ID` and the tests.
- **Validation:** `mise run check`; `mise run test -- -k login`.
  - Manual merge gate, run by the maintainer because it needs a browser and a real account:
    `uv run yandex-disk-rescue auth login`, open the printed URL, and confirm that:
    - the `verification_code` page shows a token;
    - pasting it passes `auth status`.
- **Doc updates:** Root `README.md` "Authentication" gets its final current-state rewrite:
  - Getting a token: run `auth login`, open the URL, approve, paste.
  - Using your own registered app via `--client-id` / `YANDEX_DISK_RESCUE_CLIENT_ID`, with the
    redirect URI `https://oauth.yandex.ru/verification_code` and Disk read permissions.
  - Automation via `YANDEX_DISK_RESCUE_TOKEN` (cron/systemd).
  - The security model: 0600 file under the XDG config dir, env precedence, the token never
    on argv or in output, no server-side revoke so use the Yandex ID page.
  - Run `rg 'YANDEX_DISK_RESCUE_|auth '` across the docs to keep them consistent.
  - `.env.example`: add a commented `# YANDEX_DISK_RESCUE_CLIENT_ID=` (override of the built-in
    app's client ID).

## Discovered issues

- **Fix in this PR:**
  - `.env.example` still holds the template placeholder `EXAMPLE_API_KEY`. Replaced
    stage by stage with the real `YANDEX_DISK_RESCUE_*` entries.
  - mise loads `.env` into every `mise run test`, so tests must isolate the environment
    (autouse fixture, Stage 1).
- **Filed as issues:** None. Nothing out of scope was found in this small skeleton.

## Risks / open questions

- **`scope` separator:** settled by the maintainer's browser test (space, sent as `%20`). The
  maintainer will also trim the built-in app's registered rights to Disk read and Disk info.
- **Seams agreed with the #2 architect (confirmed):**
  - Names: `YANDEX_DISK_RESCUE_TOKEN`; `auth.get_token()` / `NoTokenError`.
  - Ownership: `paths.py` (#2 adds `state_dir()` with the same XDG rule) and `errors.py`
    (`RescueError.exit_code = 1`, caught inside the Typer app, `show_locals=False`).
  - Fixtures: `fake_disk_api` (stdlib `ThreadingHTTPServer`) and the autouse env/XDG isolation
    fixture, owned by #1. #2 grows the same fixture and may move the server class into a tests
    helper module.
  - Exit codes: 2 for usage/config errors, 130 for Ctrl+C (#2).
  - Merge order (main's decision): #1 merges first, and #2 branches on top and consumes
    `errors.py`, `paths.py`, `auth.py` and `tests/conftest.py` directly.
  - API URL: `YANDEX_DISK_RESCUE_API_URL` becomes #2's settings option `api_url` (same env name,
    same default). #2 makes `auth.api_base_url()` read from the settings registry, with no
    duplicate reader kept.
- Hidden-input prompts under a non-TTY fall back to stdin, and Python's getpass may print a
  warning. Scripted use is meant to go through the env var, not piped `login`.
