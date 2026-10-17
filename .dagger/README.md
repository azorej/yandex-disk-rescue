# Dagger CI module (`repo-ci`)

The CI pipeline as code. Every check runs in a container built from the
committed `uv.lock`, so `mise run ci` on your machine and the GitHub
Actions run execute exactly the same steps.

## Layout

```
.dagger/
├── src/repo_ci/main.py    # Pipeline functions
├── pyproject.toml         # Module project (ruff config extends the root one)
├── sdk/                   # Generated Dagger SDK — git-ignored, `mise run dagger:develop`
└── tests/                 # Separate Dagger module that tests repo-ci
    ├── dagger.json        # Depends on the root module
    └── src/tests/main.py
```

`dagger.json` at the repository root points at this directory, so
`dagger call …` works from the root without `-m`.

## Functions

| Function | Does |
|---|---|
| `base` | `UV_IMAGE` (uv + Python) with a persistent uv cache volume |
| `workspace --source DIR` | `base` + the workspace synced with `uv sync --locked` (dependency layer cached separately from sources) |
| `lint --source DIR` | `ruff check` + `ruff format --check` over the whole repo |
| `test --source DIR` | `pytest` over every workspace member |
| `check --source DIR` | `lint` and `test` concurrently — what CI gates on |

`--source` defaults to the repository root (minus `.venv`, caches and
generated SDKs), so the usual call is just:

```bash
mise run ci                     # = dagger call check
dagger call test                # a single function
dagger call workspace terminal  # debug shell inside the CI container
```

Module tests (`.dagger/tests`) assert the toolchain and the workspace sync:

```bash
mise run ci:selftest            # = cd .dagger/tests && dagger call all
```

## Working on the module

- `mise run dagger:develop` generates `sdk/` and syncs `.dagger/.venv` and
  `.dagger/tests/.venv` for IDE completion.
- Keep `UV_IMAGE`'s Python minor version equal to `requires-python` in the
  root `pyproject.toml`.
- Adding a CI lane: add a function here, a mise task, a job in
  `.github/workflows/ci.yml`, its path filter, and the job in
  `required.needs`.
- Upgrading Dagger: bump `dagger` in `mise.toml` and `engineVersion` in both
  `dagger.json` files together, then
  `mise run dagger:develop`.
- Never pipe `dagger` output — redirect to a file
  (`dagger call check > /tmp/ci.log 2>&1`); a pipe can mask a failure.
