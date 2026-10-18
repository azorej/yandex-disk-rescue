# Plan: Identify the tool in the User-Agent of its HTTP requests

Issue: #23
Branch: feat/user-agent

## Context

Every HTTP request the tool makes goes out with httpx's default `User-Agent: python-httpx/<version>`. Observed by
running `download disk:/ DEST` through `CliRunner` against the `disk` fake-server fixture and counting
`(method, path, user-agent)` over `FakeDiskApi.requests`:

```
('GET', '/v1/disk/resources/download', 'python-httpx/0.28.1'): 6
('GET', '/download',                   'python-httpx/0.28.1'): 6
('GET', '/v1/disk/resources',          'python-httpx/0.28.1'): 5
('GET', '/v1/disk/',                   'python-httpx/0.28.1'): 1
```

The tool has exactly two HTTP clients: `DiskClient` (`api/client.py`, every Disk API request, including the token
check of `auth login` / `auth status` in `cli.py:_account_of`) and `ContentClient` (`api/transfer.py`, the download
host, following redirects to the storage host). OAuth itself makes no HTTP request from the tool: `auth.py` only
builds the `https://oauth.yandex.ru/authorize` URL the user opens in a browser and parses the pasted token (it uses
`urllib.parse` only). `verify` makes no network request. So covering the two clients covers every request.

## Goal

- `yandex_disk_rescue.api.USER_AGENT: str` equals
  `yandex-disk-rescue/<version> (+https://github.com/azorej/yandex-disk-rescue)`, `<version>` being
  `yandex_disk_rescue.__version__` (installed package metadata).
- Every request `DiskClient` and `ContentClient` send carries `User-Agent: <USER_AGENT>`, including requests that
  follow a redirect (download host to storage host) and retries (true by construction: the header is a client default).
- No option, env var or config key changes it.

## Non-goals

- Making the User-Agent configurable (issue's Out of Scope).
- Any other header change (`Accept-Encoding: identity` and `Authorization` stay as they are).

## Stages

### Stage 1: Send the tool's User-Agent from both clients

- **Contract:**
  - New public constant `USER_AGENT` exported from `yandex_disk_rescue.api` (in `__all__`). It lives in a leaf
    module of `api/` (for example `api/user_agent.py`) so `client.py` and `transfer.py` import it without a cycle
    through `api/__init__.py`. Its value is built from `yandex_disk_rescue.__version__`; the project URL is the
    literal `https://github.com/azorej/yandex-disk-rescue` (the `Homepage` in `yandex-disk-rescue/pyproject.toml`).
  - `DiskClient` and `ContentClient` set it as a default header of their `httpx.AsyncClient`, so it applies to
    every request, redirect and retry. Constructor signatures do not change.
- **Behaviour to lock:**
  - `yandex-disk-rescue/tests/test_download.py::test_a_fresh_mirror_holds_every_file_with_its_bytes_and_cloud_mtime`
    (already parametrized over `redirect`, so it runs once with `/download` serving bytes and once with
    `/download` 302-ing to `/storage`): extend it to assert
    `{r.headers['user-agent'] for r in disk.requests} == {f'yandex-disk-rescue/{version("yandex-disk-rescue")} (+https://github.com/azorej/yandex-disk-rescue)'}`
    — the expected string is spelled out literally in the test (with the version read via
    `importlib.metadata.version`), not taken from `api.USER_AGENT`, so the test checks the wire value rather than
    restating the constant. Also assert the request log covers every request kind the run makes:
    `{r.path for r in disk.requests} >= {'/v1/disk/', '/v1/disk/resources', '/v1/disk/resources/download', '/download'}`,
    plus `'/storage'` when `redirect` is true. This is the issue's acceptance criterion (the fake server's request
    log shows the tool's User-Agent on every request kind, API and download host, redirect included) through the
    real CLI, real HTTP and a real fake server.
  - `yandex-disk-rescue/tests/test_auth.py::test_status_with_a_stored_token_shows_the_account`: its existing
    assertion on `(r.method, r.path, r.headers['authorization'])` over `fake_disk_api.requests` gains
    `r.headers['user-agent']` with the same literal expected value, locking the token-check request of the `auth`
    commands.
- **Deliverable:** both clients identify the tool; the tests above pass; the docs and changelog below describe it.
- **Validation:**
  - `mise run test -- yandex-disk-rescue/tests/test_download.py -k fresh_mirror`
  - `mise run test -- yandex-disk-rescue/tests/test_auth.py -k stored_token_shows_the_account`
  - `mise run check`
  - `rg -n 'python-httpx|User-Agent' --glob '!uv.lock' --glob '!docs/plans/**'` shows only the new code, tests and
    doc lines.
- **Doc updates:**
  - `README.md` (root): one short paragraph at the end of the "Rate limiting" section (the section about how the
    tool talks to Yandex's servers), stated as current fact: every request the tool makes, to the Disk API
    and to the download host, carries `User-Agent: yandex-disk-rescue/<version> (+https://github.com/azorej/yandex-disk-rescue)`.
    One or two sentences; no "previously python-httpx" wording.
  - `yandex-disk-rescue/README.md` "Layout": add the new `api/` module line (for example
    `user_agent.py # USER_AGENT — the User-Agent every request sends: tool name, version, project URL`), and
    mention in the `client.py` and `transfer.py` lines that every request sends `USER_AGENT` only if it fits the
    existing descriptions without bloating them.
  - `CHANGELOG.md` under `## [Unreleased]`: a `### Changed` line: "Every HTTP request names the
    tool in its `User-Agent`: `yandex-disk-rescue/<version> (+https://github.com/azorej/yandex-disk-rescue)`."

## Discovered issues

- **Fix in this PR:** None.
- **Filed as issues:** None. Nothing out of scope was found in the touched code (two clients, one construction site
  each; the OAuth flow makes no tool-side HTTP request).

## Risks / open questions

- `CHANGELOG.md`, the root `README.md` and `yandex-disk-rescue/README.md` are also likely touched by the parallel
  plans for #15, #16 and #27; whichever lands second resolves a trivial merge conflict in those files.
- `__version__` in a source checkout is the editable install's metadata version (`1.0.0rc1` today), which is the
  intended value; the test reads the same metadata, so it does not hard-code a version.
