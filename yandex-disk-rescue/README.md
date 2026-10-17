# yandex-disk-rescue

The command-line application, a Typer CLI installed as `yandex-disk-rescue`.

```bash
uv run yandex-disk-rescue --help
uv run yandex-disk-rescue --version
```

Tests: `mise run test -- yandex-disk-rescue/tests`.

## Environment variables

| Variable | Meaning |
|---|---|
| `YANDEX_DISK_RESCUE_TOKEN` | Yandex OAuth token; takes precedence over the stored token file. Blank is an error. |
| `YANDEX_DISK_RESCUE_CLIENT_ID` | ClientID of the OAuth app `auth login` authorizes (default: the built-in app). Same as `--client-id`. |
| `YANDEX_DISK_RESCUE_API_URL` | Base URL of the Disk REST API (default `https://cloud-api.yandex.net`). Same as the hidden `--api-url`. |
| `YANDEX_DISK_RESCUE_API_INTERVAL_MS` | Base gap between Disk API request starts, 25 to 1000 ms (default 50). Same as `--api-interval-ms`. |
| `YANDEX_DISK_RESCUE_API_CONCURRENCY` | Maximum Disk API requests in flight, 1 to 30 (default 7). Same as `--api-concurrency`. |
| `YANDEX_DISK_RESCUE_API_TIMEOUT` | Timeout of one Disk API request, 1 to 300 s (default 30). Same as `--api-timeout`. |
| `YANDEX_DISK_RESCUE_API_RETRIES` | Retries after a transport error or a throttling status, 0 to 20 (default 5). Same as `--api-retries`. |
| `XDG_CONFIG_HOME` | Parent of the config directory holding the token file (default `~/.config`). |
| `XDG_STATE_HOME` | Parent of the state directory holding the manifest (default `~/.local/state`). |

## Manifest

The manifest is a SQLite database at `$XDG_STATE_HOME/yandex-disk-rescue/manifest.sqlite3` (a 0600 file in a 0700
directory). For each account (keyed by the Disk user uid) it stores every file and folder of each scanned remote
folder: path, kind, size, MD5, SHA-256 and modification time.

- It only ever holds the result of completed scans. A scan stages its rows durably and moves them into the manifest
  in one transaction when it finishes; a scan stopped by an error, Ctrl+C or SIGKILL leaves the previous content
  unchanged, and the next scan discards its staged rows. Rescanning a folder replaces only that folder's subtree.
- One run at a time: opening the manifest takes an exclusive lock on `manifest.sqlite3.lock`, and a second run fails
  with `Another yandex-disk-rescue run is using ...`.
- The database runs in WAL mode with `synchronous=FULL`, so it must live on a local filesystem.
- The schema version is `PRAGMA user_version`. Schema changes append a migration to `manifest.py`; opening the
  manifest applies pending ones, and a manifest from a newer version of the tool is refused.

## Layout

```
yandex-disk-rescue/
├── src/yandex_disk_rescue/
│   ├── __init__.py   # Package version (read from installed metadata)
│   ├── cli.py        # Typer app — the entry point, `Error: ...` rendering of RescueError, the `auth` commands
│   ├── errors.py     # RescueError — expected, user-facing failures with an exit code
│   ├── paths.py      # XDG config and state directories
│   ├── manifest.py   # The manifest database: schema migrations, the run lock, atomic commit of a scan
│   ├── settings.py   # The options registry: name, flag, env var, default and validation of every option
│   ├── auth.py       # OAuth token: the authorization URL, resolution (env var, then 0600 file), the header-safety check
│   └── api/          # DiskClient — every Disk API request: the account check, paged folder listing,
│                     #   error mapping (401/403, other statuses, transport errors, malformed bodies), `disk:/` paths;
│                     #   the shared AdaptiveLimiter (pacing, concurrency cap, Retry-After) and bounded retries
└── tests/
    ├── conftest.py         # Autouse env/XDG isolation; the `fake_disk_api` and `closed_port_url` fixtures
    ├── fake_disk_server.py # Real-socket fake Disk API: an in-memory tree (`add_dir`, `add_file`, `remove`) served by
    │                       #   built-in `GET /v1/disk/` and `GET /v1/disk/resources`; `page_cap`, `account`, `token`;
    │                       #   `inject`ed faults (statuses, delays); a `routes` override table; the request log
    ├── test_api_client.py
    ├── test_auth.py
    ├── test_cli.py
    ├── test_manifest.py
    └── test_rate_limiter.py
```
