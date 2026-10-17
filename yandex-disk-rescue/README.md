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
│   └── cli.py        # Typer app — the `yandex-disk-rescue` entry point
└── tests/
    └── test_cli.py
```
