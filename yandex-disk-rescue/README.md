# yandex-disk-rescue

The command-line application, a Typer CLI installed as `yandex-disk-rescue`.

```bash
uv run yandex-disk-rescue --help
uv run yandex-disk-rescue --version
```

Tests: `mise run test -- yandex-disk-rescue/tests`.

## Layout

```
yandex-disk-rescue/
├── src/yandex_disk_rescue/
│   ├── __init__.py   # Package version (read from installed metadata)
│   ├── cli.py        # Typer app — the `yandex-disk-rescue` entry point and the `auth` commands
│   ├── errors.py     # RescueError — expected, user-facing failures with an exit code
│   ├── paths.py      # XDG config directory
│   └── auth.py       # OAuth token: resolution (env var, then file) and the 0600 token file
└── tests/
    ├── conftest.py   # Autouse isolation: no YANDEX_DISK_RESCUE_* vars, XDG dirs under tmp_path
    ├── test_auth.py
    └── test_cli.py
```
