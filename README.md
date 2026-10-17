# Yandex Disk Rescue

A Linux command-line tool that downloads everything from a Yandex Disk
account to local storage. It is a headless analogue of the Windows app
[Yandex-Disk-Rescue](https://github.com/fefenke/Yandex-Disk-Rescue).

> **Status: early development.** OAuth token handling
> (`yandex-disk-rescue auth`) and scanning the remote tree into a local
> manifest (`yandex-disk-rescue scan`) work. Downloads are being built.
> Planned work is tracked in GitHub issues.

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

The tool talks to Yandex Disk with an OAuth token that grants only Disk read
access (`cloud_api:disk.read`) and Disk info (`cloud_api:disk.info`).

### Getting a token

```bash
yandex-disk-rescue auth login
```

It prints a Yandex ID URL on stderr. Open it in a browser, sign in and allow
access; Yandex then shows a token. Paste it at the hidden `Paste the token`
prompt (the whole address-bar URL containing `access_token=` works too). The
token is checked with a request to the Disk API, paced and retried as
described in [Rate limiting](#rate-limiting), and saved only if Yandex
accepts it:

```
Logged in as user.name. Token saved to /home/user/.config/yandex-disk-rescue/token.
```

A rejected token exits `1` and nothing is saved.

### Using your own OAuth app

By default `auth login` authorizes the tool's built-in Yandex OAuth app (a
public ClientID; no client secret is shipped). To use your own app instead,
register one at <https://oauth.yandex.com/client/new> with:

- the redirect URI `https://oauth.yandex.ru/verification_code`;
- both Disk permissions: read all of Disk (`cloud_api:disk.read`) and
  access information about Disk (`cloud_api:disk.info`).

Then set its ClientID with the `client_id` option (`--client-id ID`, see
[Configuration](#configuration)).

### Automation

For cron, systemd or CI, set `YANDEX_DISK_RESCUE_TOKEN` (for example with a
systemd `EnvironmentFile=`) instead of running `auth login`.

### Where the token comes from

The tool looks for the token in this order:

1. The `YANDEX_DISK_RESCUE_TOKEN` environment variable. If it is set but
   blank, that is an error; the stored file is not used as a fallback.
   `auth login` and `auth logout` warn when it is set, because it overrides
   the stored file.
2. The token file `$XDG_CONFIG_HOME/yandex-disk-rescue/token`
   (`~/.config/yandex-disk-rescue/token` when `XDG_CONFIG_HOME` is unset or
   not an absolute path). `auth login` writes it as mode `0600` and sets
   the `yandex-disk-rescue` directory to `0700`. A token file readable by
   group or others is refused, as ssh does; fix it with `chmod 600 <file>`.
   A path that is not a regular file or a file that is not UTF-8 text is
   refused too.

Surrounding whitespace is stripped from either source. A token that still
contains whitespace or anything but printable ASCII is refused with an error
naming its source.

The token is never accepted as a command-line argument (it would show in
`ps` and shell history), and the tool never prints it.

`yandex-disk-rescue auth status` shows where the token comes from, the Yandex
login it belongs to and the Disk usage, after checking it with a request to
the Disk API (paced and retried as described in [Rate limiting](#rate-limiting)):

```
Token: file /home/user/.config/yandex-disk-rescue/token
Account: user.name
Disk usage: 1.5 GiB of 10.0 GiB used (15.0%)
```

It exits `0` when Yandex accepts the token and `1` otherwise (no token, a
malformed or unreadable token, a token Yandex rejects, an unreachable API or an unexpected response), with an
`Error: ...` line on stderr (see [Exit codes](#exit-codes)).

### Logging out

`yandex-disk-rescue auth logout` deletes the stored token file. It succeeds
when there is no file, and warns if `YANDEX_DISK_RESCUE_TOKEN` still provides
a token. Deleting the file does not revoke the token at Yandex (that needs a
client secret the tool does not have): revoke the app's access in Yandex ID
at <https://id.yandex.com/personal/data-access>.

## Scanning a folder

```bash
yandex-disk-rescue scan              # the whole Disk (disk:/)
yandex-disk-rescue scan disk:/Photos # one folder; /Photos and Photos mean the same
```

`scan` lists the folder and everything below it through the Disk API, up to
`--api-concurrency` folders at a time and paced as described in
[Rate limiting](#rate-limiting) (it takes the same `--api-*` options as
`auth status`). It records every file and folder (path, kind, size, MD5,
SHA-256, modification time) in the manifest,
`$XDG_STATE_HOME/yandex-disk-rescue/manifest.sqlite3`
(`~/.local/state/yandex-disk-rescue/manifest.sqlite3` when `XDG_STATE_HOME`
is unset or not an absolute path), and prints a summary:

```
Scanned disk:/Photos: 1234 files, 56 folders, 12.3 GiB
Files without a cloud checksum: 1
  disk:/Photos/old.bmp
Changes since the last scan: 5 added, 2 changed, 1 removed
  + disk:/Photos/new.jpg
  ~ disk:/Photos/edited.jpg
  - disk:/Photos/gone.jpg
Manifest: /home/user/.local/state/yandex-disk-rescue/manifest.sqlite3
```

The counts and the size cover everything below the scanned folder. Files
without a cloud checksum have neither an MD5 nor a SHA-256 in the API, so
they cannot be verified after download. The first scan of a folder prints
`First scan of disk:/Photos` instead of the changes; a file counts as
changed when its kind, size, checksums or modification time differ. Each
list of paths shows at most 20 entries, then `… and N more`.

A folder deleted on the Disk while the scan runs does not stop it: the
summary lists it under `Folders that disappeared during the scan: N`, and the
manifest records that folder and everything under it as removed. Only the
scanned folder itself disappearing fails the scan.

The manifest only ever holds completed scans: a scan that fails or is
interrupted (Ctrl+C, a kill, a crash) leaves the previous scan in place, and
the next run scans again from the start. Scanning a folder replaces only
that folder's part of the manifest. Only one run can use the manifest at a
time; a second one fails with `Another yandex-disk-rescue run is using ...`.
Paths outside `disk:/` (such as `trash:/`) are refused.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | A failure the tool reports as `Error: ...` on stderr: no token, a malformed, unreadable or insecure token, a token Yandex rejects, a remote path that is missing or not a folder, an API error after the retries, a manifest that is unusable or in use by another run |
| `2` | Invalid input: a usage error, an invalid option value from any source, an invalid config file, a remote path outside `disk:/` |
| `130` | Interrupted with Ctrl+C |

## Rate limiting

Every Disk API request of a command goes through one shared limiter:

- Request starts are at least the current interval apart (base:
  `api_interval_ms`), and at most `api_concurrency` requests are in
  flight.
- A throttling response is `429`, `408` or any `5xx`. Each one doubles the
  current interval, up to 2 s or 8 times the base, whichever is larger.
  Every 10 consecutive successful responses halve it again, never below the
  base.
- When a throttling response carries `Retry-After` (seconds or an HTTP
  date), every request start pauses until then, for at most 300 s.

A request that fails with a transport error (including a timeout,
`api_timeout`) or a throttling response is retried up to `api_retries`
times. Each retry waits
`Retry-After` when the server sent one, otherwise an exponential backoff with
jitter from 0.5 s up to 30 s. Other `4xx` responses are not retried. When
the retries run out, the error names the last failure and ends with
`(after N attempts)`.

The defaults, valid values, flags and environment variables of these
options are in [Configuration](#configuration).

## Configuration

Every option can be set in three places besides its built-in default. The
first one that sets it wins:

1. the command-line flag, for example `--api-interval-ms 100`;
2. the environment variable, for example `YANDEX_DISK_RESCUE_API_INTERVAL_MS=100`;
3. the config file, for example `api_interval_ms = 100`.

| Option | Flag | Environment variable | Default | Valid values |
|---|---|---|---|---|
| `api_interval_ms` | `--api-interval-ms` | `YANDEX_DISK_RESCUE_API_INTERVAL_MS` | `50` | 25 to 1000 (ms) |
| `api_concurrency` | `--api-concurrency` | `YANDEX_DISK_RESCUE_API_CONCURRENCY` | `7` | 1 to 30 |
| `api_timeout` | `--api-timeout` | `YANDEX_DISK_RESCUE_API_TIMEOUT` | `30` | 1 to 300 (s) |
| `api_retries` | `--api-retries` | `YANDEX_DISK_RESCUE_API_RETRIES` | `5` | 0 to 20 |
| `api_url` | `--api-url` (hidden) | `YANDEX_DISK_RESCUE_API_URL` | `https://cloud-api.yandex.net` | an `http://` or `https://` URL |
| `client_id` | `--client-id` (`auth login`) | `YANDEX_DISK_RESCUE_CLIENT_ID` | the built-in app | 32 lowercase hex characters |

The `api_*` options apply to every command that talks to the Disk API
(`auth login`, `auth status`, `scan`); `client_id` applies to `auth login`.

The config file is `$XDG_CONFIG_HOME/yandex-disk-rescue/config.toml`
(`~/.config/yandex-disk-rescue/config.toml` when `XDG_CONFIG_HOME` is unset
or not an absolute path). Another file can be named with the global
`--config PATH` option (`yandex-disk-rescue --config PATH scan`) or the
`YANDEX_DISK_RESCUE_CONFIG` environment variable. It is TOML with flat
top-level keys named after the options:

```toml
api_interval_ms = 100
api_concurrency = 4
client_id = "0123456789abcdef0123456789abcdef"
```

The token is never read from the config file; see
[Where the token comes from](#where-the-token-comes-from).

Every value is validated, whatever its source. A flag or environment
variable value outside its range is a usage error (exit `2`). So is a
problem with the config file, reported as
`Error: config file <path>: <key>: <reason>` before any command runs: a
named file that does not exist, a file that is not valid TOML, an unknown
key, a value of the wrong type or an invalid value. A missing file at the
default location means no values.

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
