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
| `YANDEX_DISK_RESCUE_API_URL` | Base URL of the Disk REST API (default `https://cloud-api.yandex.net`). |
| `XDG_CONFIG_HOME` | Parent of the config directory holding the token file (default `~/.config`). |

## Layout

```
yandex-disk-rescue/
├── src/yandex_disk_rescue/
│   ├── __init__.py   # Package version (read from installed metadata)
│   ├── cli.py        # Typer app — the entry point, `Error: ...` rendering of RescueError, the `auth` commands
│   ├── errors.py     # RescueError — expected, user-facing failures with an exit code
│   ├── paths.py      # XDG config directory
│   ├── auth.py       # OAuth token: the authorization URL, resolution (env var, then 0600 file), the header-safety check
│   └── api/          # DiskClient — every Disk API request: the account check, paged folder listing,
│                     #   error mapping (401/403, other statuses, transport errors, malformed bodies), `disk:/` paths
└── tests/
    ├── conftest.py         # Autouse env/XDG isolation; the `fake_disk_api` fixture
    ├── fake_disk_server.py # Real-socket fake Disk API: an in-memory tree (`add_dir`, `add_file`, `remove`) served by
    │                       #   built-in `GET /v1/disk/` and `GET /v1/disk/resources`; `page_cap`, `account`, `token`;
    │                       #   `inject`ed faults (statuses, delays); a `routes` override table; the request log
    ├── test_api_client.py
    ├── test_auth.py
    └── test_cli.py
```
