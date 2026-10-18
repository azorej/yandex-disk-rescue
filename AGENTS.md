# AGENTS.md

The single source of rules for humans and AI agents (Claude Code, Codex,
Cursor, Roo, Copilot, …) working in this repository. Tool-specific files
(`CLAUDE.md`) only point here.

> If something should be done → do it. No "temporary" shortcuts. Test what
> you change. Fix what you find. Surface failures explicitly.

## Project Overview

**yandex-disk-rescue** is a Linux command-line tool that downloads the whole
content of a Yandex Disk account (or one folder) to local storage, through the
official Yandex Disk REST API with an OAuth token. It is a headless analogue
of the Windows app [Yandex-Disk-Rescue](https://github.com/fefenke/Yandex-Disk-Rescue):
resumable and crash-safe downloads, parallel and segmented transfers, adaptive
handling of API rate limits, and checksum verification (MD5 / SHA-256) of
every file. There is no GUI; the CLI is the only interface.

The repository is a **uv workspace** (one package today: `yandex-disk-rescue/`),
uses **mise** for tool versions and tasks, **Dagger** for containerized CI
that runs the same way locally and on GitHub, and ships a devcontainer with
**Claude Code** pre-installed.

**Language:** everything in the repository — code, comments, docs, CLI
output, commit messages, issues — is in English. No Russian text, even
when referring to Yandex UI labels or folder names; translate them.

## Core Rules

1. **Surface failures explicitly.** No silent fallbacks, no
   `except Exception: pass`, no returning `None` instead of raising.
   Enforced by ruff `BLE001` outside `tests/`; the only escape is a
   per-line `# noqa: BLE001 — <reason>` on a justified best-effort site.
2. **Quality over shortcuts.** Defer a feature if you must; never defer
   quality.
3. **Fix what you find.** Broken code found = broken code fixed, or an
   issue filed if it is genuinely out of scope.
4. **Contracts over backward compatibility.** Until there are external
   users, refactor freely: no back-compat shims, no `_legacy` aliases, no
   "rename later" comments.
5. **Test behaviour, not code.** No tests that only assert on mocks or
   re-implement the code under test. Prefer real collaborators
   (`CliRunner`, `tmp_path`, subprocess, HTTP). Extend existing tests
   rather than duplicating coverage.
6. **Interfaces over implementation.** Get the public API / protocol right
   first; internals can improve later.
7. **Keep documentation current.** Docs describe the present state. Update
   or delete stale mentions in the same PR — never add "previously…" notes.
8. **Plan → confirm → build → verify.** For any non-trivial change: read
   the relevant docs and code, propose the approach, get alignment,
   implement, then run the verifiers (`mise run check`, `mise run ci`).

## Commands

```bash
mise run setup          # Install tools (mise.toml), sync deps (uv), install git hooks — idempotent
mise run check          # Fast local gate: lint + test
mise run lint           # Ruff lint + format check (no changes)
mise run format         # Auto-fix lint issues and format
mise run test           # Pytest; extra args pass through: mise run test -- -k greet
mise run ci             # Full CI pipeline in Dagger — exactly what GitHub Actions runs
mise run ci:selftest    # Tests for the Dagger module itself
mise run ci:package     # Build, twine-check and install-smoke the wheel (uv tool, pipx) in Dagger
mise run release:dist <tag>   # Rehearse a release up to the upload, into release/ (dist/ + notes.md)
mise run release:verify <tag> # Check that PyPI serves release/dist/ and the version installs by name
mise run lock           # Upgrade all dependencies in uv.lock
mise run dagger:develop # Regenerate the Dagger SDK (after a Dagger version bump)
mise tasks              # List every task
```

**Always** use `mise run` for project tasks and `uv run` for Python
commands — never a raw `python`, `pip`, or `.venv/bin/...`.

## Repository Layout

```
├── yandex-disk-rescue/   # Workspace member: the CLI and its internals (Typer)
├── .dagger/              # Dagger module `repo-ci` (CI pipeline) + tests/ (its own tests)
├── .devcontainer/        # Ubuntu devcontainer: mise, Claude Code, Docker-in-Docker
├── .github/              # Workflows, PR template, Dependabot config
├── .agents/skills/       # Agent skills (canonical); .claude/skills/ symlinks to them
├── .claude/              # Claude Code: shared settings, hooks, subagents
├── docs/                 # Guides; docs/plans/ holds implementation plans
├── scripts/              # Standalone helper scripts (not dev tasks — those are mise tasks)
├── CHANGELOG.md          # Release notes (Keep a Changelog): `[Unreleased]`, then one section per version
├── mise.toml             # Tool versions, env, tasks
├── pyproject.toml        # Workspace root: members, dev deps, ruff/pytest config
└── uv.lock               # Lock file — generated, never edited by hand
```

## Conventions

### Python

- Python 3.14 (`.python-version`, `mise.toml`, `requires-python`).
- Ruff is the only linter/formatter: single quotes, line length 120,
  absolute imports only. Config lives in the root `pyproject.toml`;
  `.dagger/` extends it.
- Typing: annotate public functions; use modern syntax (`X | None`,
  `list[str]`, `typing.Annotated` for Typer/FastAPI parameters).
- Favourite libraries: `typer` (CLIs), `tenacity` (retries), `pytest`.

### uv workspace

- Every package is a workspace member with its own `pyproject.toml`,
  `src/` layout and `tests/`.
- Runtime deps go in the member's `[project] dependencies`. Dev tooling
  goes in the root `[dependency-groups] dev`. No `optional-dependencies`
  for dev tools.
- Members reference each other by name; the root lists each member in
  `[tool.uv.sources]` (`{ workspace = true }`) **and** in the `dev`
  group so `uv sync` installs it in editable mode.
- Add deps with `uv add` (`uv add --package yandex-disk-rescue httpx`). Never
  write `uv.lock` by hand.

**Adding a package:** create `my-package/` with `pyproject.toml`,
`src/my_package/`, `tests/`; add it to `[tool.uv.workspace] members`,
`[tool.uv.sources]`, the `dev` group and `[tool.pytest.ini_options]
testpaths` in the root `pyproject.toml`; run `uv sync`.

### Tasks and scripts

- Developer automation is a mise task. One-liners go inline in
  `mise.toml`; anything longer becomes a file task in `mise-tasks/`
  (executable, `#!/usr/bin/env bash`, `set -euo pipefail`, a `#MISE
  description=...` header).
- `scripts/` is for standalone helpers that are not part of the dev loop;
  follow the template in [`scripts/README.md`](scripts/README.md).

### Versions

Each tool version is pinned in exactly one place. Bump it there (`mise
outdated` lists newer tools; `mise run lock` upgrades Python deps):

| Tool | Pinned in |
|---|---|
| Python, uv, Dagger CLI | `mise.toml` |
| Dagger engine | `dagger.json` + `.dagger/tests/dagger.json` (must equal the CLI) |
| uv/Python image for CI | `UV_IMAGE` in `.dagger/src/repo_ci/main.py` |
| ruff, pytest, pre-commit, libraries | `uv.lock` |
| zizmor, actionlint, shellcheck | `uv.lock` |
| uv_build (build backend) | range in `yandex-disk-rescue/pyproject.toml` and `.dagger/pyproject.toml`, moved together with uv in `mise.toml` |
| pre-commit hook repos | `.pre-commit-config.yaml` |
| GitHub Actions | full commit SHA in `.github/workflows/*.yml` (updated by Dependabot) |
| mise, base OS | `.devcontainer/Dockerfile` |

### CI (Dagger + GitHub Actions)

- Pipeline logic lives in the Dagger module (`.dagger/src/repo_ci/`);
  the workflow (`.github/workflows/ci.yml`) is a thin caller of `mise run`
  tasks. Each lane runs the same mise task locally: `Lint & test` is
  `mise run ci`, `Dagger module tests` is `mise run ci:selftest`,
  `Package & install smoke` is `mise run ci:package`; `Pre-commit hooks`
  runs `pre-commit run --all-files`. Green tasks locally mean a green CI run.
- Pull requests run only the lanes their paths touch; pushes to `main`,
  the weekly schedule and manual dispatch run everything. Branch
  protection requires the single `CI required` check.
- When adding a lane: add a Dagger function, a mise task, a job in
  `ci.yml`, its path filter, and add the job to `required.needs`.
- Workflows are linted by actionlint (with shellcheck) and `zizmor
  --offline` as pre-commit hooks, so in the `hygiene` lane and on every
  local commit. A new `uses:` is pinned to a full commit SHA with its
  release tag as a comment (`@<sha> # vX.Y.Z`), and `actions/checkout`
  sets `persist-credentials: false`. Fix a zizmor finding; only a false
  positive gets an inline `# zizmor: ignore[<audit>]` with the reason.
- Releases: a pushed `v*` tag runs `.github/workflows/release.yml`
  (`dist` → `publish-pypi` → `verify-pypi` → `github-release`). It is the
  one exception to "thin caller of mise tasks": publishing uses
  `pypa/gh-action-pypi-publish` with Trusted Publishing (OIDC, no stored
  secret), in a job that holds `id-token: write` and runs no repository
  code, so nothing local can ever publish. Everything else is Dagger behind
  `mise run release:dist` / `release:verify`.

### Dockerfiles and the devcontainer

- System packages install as root; user tools (mise, uv, Claude Code)
  install as the **non-root user** so their self-update works without sudo.
- Pin base images to a version tag; prefer multi-stage builds with a
  non-root runtime user for application images; `COPY`, not `ADD`.

### Git

- Branch from `main`; conventional commit messages (`feat:`, `fix:`,
  `chore:`, `docs:`, …); squash-merge PRs.
- Never commit secrets. Local secrets go in `.env` (git-ignored; mise
  loads it — see `.env.example`).
- A user-visible change adds a line under `## [Unreleased]` in
  `CHANGELOG.md` in the same PR.
- Releases: see "Releasing" in [`CONTRIBUTING.md`](CONTRIBUTING.md#releasing).

## Agent Workflow

Skills live in [`.agents/skills/`](.agents/skills/) and subagents in
[`.claude/agents/`](.claude/agents/):

| Skill | Use it to |
|---|---|
| `/writing-development-tickets` | Scope work into well-sized GitHub issues |
| `/executing-development-tickets <N>` | Drive an issue to a reviewed PR: architect plans → critic challenges → you approve → stage implementers build → reviewer checks |
| `/rules-review` | Review a branch, PR or path against these rules |

Plans produced by the workflow go to `docs/plans/<YYYY-MM-DD>-issue-<N>-<name>.md`.

MCP servers worth having (per-engineer): **Context7** for library docs
(use it before guessing at an API) and **GitHub** for issues/PRs.

## Gotchas

1. **Never pipe `dagger` output** (`| tee`, `| grep`, `| head`): a pipe
   can mask Dagger's exit status and turn a failure green. Redirect to a
   file instead: `dagger call check > /tmp/ci.log 2>&1`.
2. **Long-running commands:** redirect to a file or `tee` the whole run —
   don't truncate with `head`/`tail` and lose the failure.
3. **Ruff formats Markdown code blocks**, so `mise run lint` can fail on a
   `.md` file. Run `mise run format`.
4. **Dagger needs a git repository**: the `.dagger/tests` module resolves
   its dependency relative to the git root. Run `git init` first in a
   fresh copy.
5. **After a Dagger version bump** run `mise run dagger:develop` so the
   generated SDK matches the engine.
