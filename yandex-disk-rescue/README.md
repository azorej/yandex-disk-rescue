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
│   └── auth.py       # OAuth token: the authorization URL, resolution (env var, then 0600 file), the account check against the Disk API
└── tests/
    ├── conftest.py   # Autouse env/XDG isolation; `fake_disk_api`: a real-socket fake Disk API driven by a routes table
    ├── test_auth.py
    └── test_cli.py
```
