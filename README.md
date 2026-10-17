# Yandex Disk Rescue

A Linux command-line tool that downloads everything from a Yandex Disk
account to local storage. It is a headless analogue of the Windows app
[Yandex-Disk-Rescue](https://github.com/fefenke/Yandex-Disk-Rescue).

> **Status: early development.** The CLI skeleton exists
> (`yandex-disk-rescue --help`, `--version`) with OAuth token handling under
> `yandex-disk-rescue auth`. Download features are being built. Planned work
> is tracked in GitHub issues.

## Goals

- **Whole-account or one-folder backup.** Mirror `disk:/` or any
  sub-folder, keeping the folder structure.
- **Official API, OAuth token.** Uses the Yandex Disk REST API. No browser
  automation and no Yandex 360 subscription required.
- **Resumable and crash-safe.** Interrupted transfers continue from the
  last confirmed byte. A rerun only fetches what is missing or changed.
- **Fast but polite.** Parallel downloads, segmented transfers for large
  files, and adaptive back-off that honours `429` / `Retry-After`.
- **Verified.** Every file is checked against the cloud size and MD5 /
  SHA-256. Failures are reported, never silently skipped.
- **CLI only.** Scriptable, cron- and systemd-friendly, no GUI.

This is an independent, unofficial tool. It is not affiliated with Yandex.

## Authentication

The tool talks to Yandex Disk with an OAuth token. It looks for the token in
this order:

1. The `YANDEX_DISK_RESCUE_TOKEN` environment variable. If it is set but
   blank, that is an error; the stored file is not used as a fallback.
2. The token file `$XDG_CONFIG_HOME/yandex-disk-rescue/token`
   (`~/.config/yandex-disk-rescue/token` when `XDG_CONFIG_HOME` is unset or
   not an absolute path). The file is mode `0600` inside a `0700`
   directory. A token file readable by group or others is refused, as ssh
   does; fix it with `chmod 600 <file>`.

`yandex-disk-rescue auth status` shows where the token comes from, the Yandex
login it belongs to and the Disk usage, after checking it with one request to
the Disk API:

```
Token: file /home/user/.config/yandex-disk-rescue/token
Account: user.name
Disk usage: 1.5 GiB of 10.0 GiB used (15.0%)
```

It exits `0` when Yandex accepts the token and `1` otherwise (no token, a
token Yandex rejects, an unreachable API or an unexpected response), with an
`Error: ...` line on stderr. Every command reports such failures the same
way; usage errors exit `2`.

`yandex-disk-rescue auth logout` deletes the stored token file. It succeeds
when there is no file, and warns if `YANDEX_DISK_RESCUE_TOKEN` still provides
a token. Deleting the file does not revoke the token at Yandex: revoke the
app's access in Yandex ID at <https://id.yandex.com/personal/data-access>.

## Quick start

**Devcontainer (recommended):** open the repo in VS Code → *Reopen in
Container*, or create a GitHub Codespace. Everything below runs on creation.

**Local machine:** install [mise](https://mise.jdx.dev/getting-started.html)
and Docker (for Dagger), then:

```bash
mise trust && mise run setup        # tools + deps + git hooks
mise run check                      # lint + test
uv run yandex-disk-rescue --help
```

## Everyday commands

| Command | What it does |
|---|---|
| `mise run setup` | Install pinned tools, sync deps, install git hooks (idempotent) |
| `mise run check` | Fast local gate: lint + test |
| `mise run format` | Auto-fix lint issues and format |
| `mise run test -- -k name` | Pytest, with pass-through args |
| `mise run ci` | Full CI pipeline in Dagger — identical to GitHub Actions |
| `mise run ci:selftest` | Tests for the Dagger module itself |
| `mise run lock` | Upgrade every dependency in `uv.lock` |
| `mise tasks` | List all tasks |

Dependencies: `uv add <pkg> --package <member>` (runtime) or
`uv add --dev <pkg>` (tooling). Local secrets go in `.env` (see
[`.env.example`](.env.example)); mise loads it automatically.

## Layout

```
├── yandex-disk-rescue/   # The CLI package — workspace member
├── .dagger/              # Dagger CI module (+ its own tests in .dagger/tests)
├── .devcontainer/        # Devcontainer: mise, Claude Code, Docker-in-Docker
├── .github/              # CI workflow, PR template
├── .agents/skills/       # Agent skills (symlinked into .claude/skills/)
├── .claude/              # Claude Code settings, hooks, subagents
├── docs/                 # Guides (GitHub setup) and implementation plans
├── scripts/              # Standalone helper scripts
├── AGENTS.md             # Rules for humans and agents — start here
├── mise.toml             # Tool versions, env, tasks
└── pyproject.toml        # Workspace root: members, dev deps, ruff, pytest
```

## How it fits together

**Versions.** Each tool is pinned in exactly one place — Python/uv/Dagger in
`mise.toml`, libraries and ruff in `uv.lock`, the Dagger engine in
`dagger.json` — and the devcontainer, CI and your shell all read from there.
Upgrade with `mise outdated` / `mise upgrade --bump` (tools) and `mise run lock` (Python deps).

**CI.** [`.github/workflows/ci.yml`](.github/workflows/ci.yml) is a thin
wrapper around `mise run ci`, which calls the Dagger module in
[`.dagger/`](.dagger/README.md). Pull requests run only the lanes their
diff touches; pushes to `main`, a weekly schedule and manual runs execute
everything. Require the single **`CI required`** check in branch protection
(see [docs/github-setup.md](docs/github-setup.md)).

**AI agents.** [`AGENTS.md`](AGENTS.md) is the one rule file every agent
reads ([`CLAUDE.md`](CLAUDE.md) imports it). With Claude Code:

- `/writing-development-tickets` — turn an idea into well-scoped GitHub issues
- `/executing-development-tickets 42` — architect plans, a critic
  challenges the plan, you approve, implementers build stage by stage, a
  reviewer checks the branch, and a PR is opened
- `/rules-review` — review any branch or PR against `AGENTS.md`

Claude Code in the devcontainer is installed per user (native installer),
so its built-in auto-updates work; its login and settings live on a named
volume and survive container rebuilds.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
