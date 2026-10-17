# Yandex Disk Rescue

A Linux command-line tool that downloads everything from a Yandex Disk
account to local storage. It is a headless analogue of the Windows app
[Yandex-Disk-Rescue](https://github.com/fefenke/Yandex-Disk-Rescue).

> **Status: early development.** OAuth token handling
> (`yandex-disk-rescue auth`), scanning the remote tree into a local
> manifest (`yandex-disk-rescue scan`) and verified, one-file-at-a-time
> mirroring (`yandex-disk-rescue download`) work. Speed and safety features
> are being built; planned work is tracked in GitHub issues.

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

A folder deleted on the Disk, or replaced by a file of the same name, while
the scan runs does not stop it: the summary lists it under `Folders that
disappeared during the scan: N`, and the manifest records that folder and
everything under it as removed. Only the scanned folder itself disappearing
fails the scan.

The manifest only ever holds completed scans: a scan that fails or is
interrupted (Ctrl+C, a kill, a crash) leaves the previous scan in place, and
the next run scans again from the start. Ctrl+C prints `Interrupted; the
manifest holds the last completed scan.` and exits with 130. Scanning a
folder replaces only that folder's part of the manifest. The manifest also
records when each scan completed, and the entries left out because their
names are not valid Unicode. Only one run can use
the manifest at a time; a second one fails with `Another yandex-disk-rescue
run is using ...`.
Paths outside `disk:/` (such as `trash:/`) are refused.

## Downloading

```bash
yandex-disk-rescue download /mnt/backup              # the whole Disk into /mnt/backup
yandex-disk-rescue download disk:/Photos ~/Photos    # one folder
```

`download [REMOTE_PATH] DEST` first scans REMOTE_PATH (default `disk:/`)
exactly as [`scan`](#scanning-a-folder) does and prints the scan summary,
unless a recent scan covers it (below). It then mirrors that folder into
DEST, with up to `download_connections` files (default 20) downloading at
once, started in path order: the folder structure is kept, each folder is
created before anything inside it, empty folders are created, and every file
gets the modification time it has on the Disk. It takes the `--api-*`
options, `--download-timeout`, `--download-retries`, `--download-passes`,
`--download-connections`, `--download-checkpoint-mib` and `--scan-max-age` (see
[Configuration](#configuration)), and `--no-scan`. A file whose download host
never answers holds one connection until it stalls and delays no other file.

**When it scans.** `download` reuses the last completed scan of the account
covering REMOTE_PATH — a scan of REMOTE_PATH itself or of a folder above it,
by `scan` or by an earlier `download` — when it completed less than
`scan_max_age` minutes ago (default 60). Otherwise it scans: when there is no
such scan, when `scan_max_age` is `0`, when the scan is older, and when its
completion time lies in the future (the clock went back). `--no-scan` reuses
the last covering scan whatever its age; with no such scan it is a usage
error (exit `2`): `disk:/A has never been scanned; run 'yandex-disk-rescue
scan disk:/A' or drop --no-scan.` A reused scan is named in place of the scan
summary:

```
Using the scan of disk:/ completed 2026-10-17 14:05 UTC (23 min ago)
Using the scan of disk:/ completed 2026-10-17 14:05 UTC (--no-scan)
```

A reused scan does not see changes on the Disk since it completed. A file
changed since then fails its download (the bytes no longer match the
recorded size and checksums) and is never kept as verified; rerun with
`--scan-max-age 0` to scan first. `scan` always scans.

**Verified.** A file reaches its final path only after its size and every
checksum the Disk has for it (MD5 and SHA-256) match the downloaded bytes.
Until then it is written to `DEST/.yandex-disk-rescue/partial/`; a file that
fails the check is deleted from there and reported. A file with neither
checksum on the Disk can only be checked by its size; the summary lists such
files. Placing a file never replaces anything at its final path.

**Reruns.** A file verified by an earlier run is skipped without being read
again when its Disk metadata is unchanged and the local file still has the
recorded size, modification time and inode. A file changed on the Disk, or
edited or touched locally, is handled like any file already in DEST (below).

**Interruptions, stalls and retries.** Every download keeps a checkpoint
journal in the manifest: how many bytes of its partial file were flushed to
the disk, and their SHA-256. A checkpoint is taken every
`download_checkpoint_mib` MiB received (default 32), every 15 s while bytes
arrive, and whenever a download attempt ends — a dropped connection, a stall,
Ctrl+C. After a kill, a crash or a power cut, at most one checkpoint interval
is fetched again.

A later pass or a later run asks the Disk for the file again and, when its
size, checksums and modification time are unchanged, re-reads the confirmed
bytes and checks them against their SHA-256. When they match, only the rest
is fetched, with an HTTP `Range` request; bytes past the last checkpoint are
dropped and fetched again, never trusted. Confirmed bytes that changed on
disk, or a partial file deleted or cut short, make the file start again from
byte 0. When the file changed in the cloud, the partial file is deleted and
the file fails with `changed in the cloud since the scan; rerun with
--scan-max-age 0 to fetch the new version` (after a rescan, the new version
is downloaded from the start). A partial file left for an older version is
discarded and the file is downloaded from the start; when the download host
ignores `Range`, the whole file is fetched again, never appended to the bytes
kept.

- **Stalls.** A download fails as stalled (`the download stalled: no data for
  15 s`) when the download host sends nothing for `download_timeout` seconds,
  whether it is slow to answer or pauses mid-file. Only silence counts: a
  slow but steady download never stalls, and time spent writing to DEST does
  not count.
- **Retries.** A download that fails after receiving new data (a cut
  connection, a stall after some data) is resumed at once from where it
  stopped, up to `download_retries` times; past that the file's last reason
  gets `(after N attempts)`. A download that fails before any new data
  arrives (a stall before the first byte, a refused connection, `408`, `429`
  or `5xx`) is not retried at once.
- **Passes.** Files whose download failed that way, or ran out of retries,
  are resumed in another pass over the files still to download, again up to
  `download_connections` at once, each with a new download link, after a 5 s
  pause (longer when the host sent `Retry-After`, up to 300 s). A run makes at most `download_passes` passes
  (`0`: until every file is done or Ctrl+C); a file still failing after the
  last one is reported with its last reason and `(after N passes)`. Once a
  file needed another pass, the summary shows `Passes: <used> of <limit>`. A
  checksum mismatch, a refused download, a file changed in the cloud and a
  local write error fail the file at once; no later pass retries it.
- **Expired links.** A download link the host rejects (`401`, `403` or
  `410`) is replaced by a new one once per pass, and the download continues
  from the bytes received; when the new link is rejected too, the file fails
  with `download link rejected again after a refresh (HTTP <n>)`.

**Summary.**

```
Downloading 120 files (1.2 GiB) to /mnt/backup/Photos
Mirrored disk:/Photos to /mnt/backup/Photos: 120 downloaded (1.2 GiB), 1000 skipped, 1 failed
Passes: 2 of 7
Already on disk and verified: 40
  disk:/Photos/2023/beach.jpg
  …
Verified by size only (no cloud checksum): 1
  disk:/Photos/old.bmp
Renamed for the destination: 1
  disk:/Photos/a.txt -> a~1a2b3c4d.txt
Moved aside to .yandex-disk-rescue/replaced/2026-10-17T09-30-00Z/: 1
  notes.txt
Failed: 1
  disk:/Photos/bad.jpg: MD5 mismatch (expected …, got …)
Manifest: /home/user/.local/state/yandex-disk-rescue/manifest.sqlite3
```

Every list is in path order, whatever the order the files finished in, and
shows at most 20 entries, then `… and N more`. When anything
failed, the run ends with `Error: N failed; see the list above.` and exit
code `1`; a rerun retries exactly what is not verified yet.

**Files already in DEST.** DEST may already hold files, for example an
earlier copy made by hand. A regular file at the path of a Disk file is read
and hashed; when its size and every Disk checksum match, it is *adopted*: it
is not downloaded, it gets the Disk modification time and it counts as
skipped, listed under `Already on disk and verified`. (A file the Disk has no
checksum for is adopted when its size matches.) Anything else at a path the
mirror needs — a file with other content, a folder where a file must go, a
file where a folder must go, or a symbolic link — is *moved aside*. Where a
file must go, that happens only after the new file is downloaded and
verified, so a failed download leaves the old one where it was. It moves,
with its relative path kept, to
`DEST/.yandex-disk-rescue/replaced/<run start, UTC, YYYY-MM-DDTHH-MM-SSZ>/`,
and the summary lists it. Each run that moves something gets its own folder
(a `-2`, `-3`, … suffix when an earlier run started in the same second). A
symbolic link is moved as the link itself; the tool never follows one inside
DEST and never writes to its target. DEST itself may be a link.

**Nothing is deleted.** The tool never deletes or truncates anything outside
`DEST/.yandex-disk-rescue/partial/`; there, at the start of a run, it also
deletes every file that is not the partial file of a download in progress
(folders and links there are left alone). Files and folders that are not on the
Disk are left alone: a path with no Disk counterpart, such as `.DS_Store`,
`.Spotlight-V100/`, `.Trashes/` or your own files inside a mirrored folder,
is never opened, moved or reported, and the tool never lists a folder to find
work. A path that is the same file as another mirrored file (a hard link, or
a name that a case- or normalization-insensitive filesystem treats as the
same) is never adopted, moved or replaced; it is reported as failed. A Disk
entry whose name is not valid Unicode cannot be requested from the API; it is
reported as failed.

**The destination.** DEST may exist; if it does not, only its last component
is created, so a mistyped mount point fails instead of filling another disk.
A DEST that is not a folder or whose parent does not exist, or a DEST
starting with `disk:` (swapped arguments), is a usage error (exit `2`). A
destination mirrors exactly one remote folder of one account; downloading
another folder into it is refused. `DEST/.yandex-disk-rescue/` is the tool's
work directory: it holds partial files, the `replaced/` folder and a lock, so
only one run at a time can use a destination (`Another yandex-disk-rescue run
is using DEST`), even runs with different manifests. The destination's filesystem must support
renaming without replacing (`RENAME_NOREPLACE`; ext4, xfs, btrfs, tmpfs and
HFS+ do); the run stops at the start otherwise. A full disk or quota, an I/O
error or a read-only filesystem stops the run with
`Error: Could not write <path>: <reason>.`, and so does a permission error
inside `DEST/.yandex-disk-rescue/`. A permission error on a path of the
mirror — a file or folder there owned by another user, or without the needed
read, write or search permission — fails only the entries it affects, with
`could not read <path>: <reason>`, `could not write <path>: <reason>` or
`could not set the modification time of <path>: <reason>`, and leaves the path
as it was.

**Failures that stop the run.** A token Yandex rejects, and a Disk API error
that persists past the API retries (an outage), stop the run: the files still
in progress stop too, only the first error is shown, files already verified
stay verified, and a rerun picks up where it stopped. Ctrl+C prints
`Interrupted; rerun to continue the download.` and exits with 130; every file
in progress takes a checkpoint first, and the next run resumes it from there.

### Local file names

Names are kept exactly as the Disk has them, with no Unicode normalization
and no character substitution, with three exceptions:

- **Long names.** A name longer than 255 UTF-8 bytes, or than 255 UTF-16
  units in its decomposed (NFD) form (the HFS+ limit), is shortened: the
  extension is kept, the rest is cut, and `~<8 hex digits>` derived from the
  full name is added.
- **Colliding siblings.** Siblings whose names differ only in case, Unicode
  normalization or invisible formatting characters would land on one file on
  a case-insensitive disk (HFS+, APFS, exFAT, NTFS). The first in code-point
  order (or the one that already has the name in this mirror) keeps it; the
  others get the `~<hex>` suffix. The rule is the same on every filesystem,
  so on ext4 too `a.txt` next to `A.txt` becomes `a~<hex>.txt`, and a mirror
  can later be copied to a case-insensitive disk without loss. The name
  `.yandex-disk-rescue` at the top of DEST is reserved.
- **Names the destination refuses.** When the filesystem refuses to create a
  name (`EINVAL`, `EILSEQ` or `ENAMETOOLONG`; exFAT and NTFS refuse `?`, for
  example), the name is converted and created again: each of
  `\ : * ? " < > |` and each control character becomes `_`, so does every
  character of a trailing run of dots and spaces, the long-name rule applies,
  and `~<8 hex digits>` derived from the Disk name is always added (more
  digits when a sibling already has that name). So `a?b.txt` becomes
  `a_b~<hex>.txt`, and a folder `Q?` becomes `Q_~<hex>` with everything in it
  below. When the converted name is refused too, the file fails with
  `the destination filesystem refuses the name <name> (<reason>), and its
  converted form <converted> too`; for a folder, it and everything below it
  fail.

Once given, a local name is kept on later runs, so a converted name is
created directly and never refused again. The summary lists every
entry whose local name differs from its Disk name.

Paths have no length limit: every folder and file is created and opened
relative to its parent folder's descriptor, one name at a time, so a tree
deeper than the filesystem's `PATH_MAX` is mirrored like any other.

### Tuning

`download_connections` caps the requests to the download host in flight, and
so the files downloading at once (one connection each). Link requests and
look-ups go to the Disk API and are paced separately, by `api_interval_ms`
and `api_concurrency` (see [Rate limiting](#rate-limiting)).

Many files written at once make a hard disk seek between them. On a USB hard
disk, start around 8 connections and raise them while the throughput grows.

A smaller `download_checkpoint_mib` means less fetched again after a crash or
a power cut, at the cost of more flushes to the disk and more manifest
writes.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success; for `download`, every file of the folder is verified at its final path |
| `1` | A failure the tool reports as `Error: ...` on stderr: no token, a malformed, unreadable or insecure token, a token Yandex rejects, a remote path that is missing or not a folder, an API error after the retries, a manifest that is unusable or in use by another run; for `download` also one or more failed files (after the summary), a destination that cannot be written or lacks `RENAME_NOREPLACE`, and a destination in use by another run |
| `2` | Invalid input: a usage error, an invalid option value from any source, an invalid config file, a remote path outside `disk:/`; for `download` also a number of paths other than one or two, a DEST that is not a folder, whose parent does not exist or that starts with `disk:`, a DEST already mirroring another remote folder or account, and `--no-scan` for a remote folder no completed scan covers |
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
| `download_timeout` | `--download-timeout` (`download`) | `YANDEX_DISK_RESCUE_DOWNLOAD_TIMEOUT` | `15` | 1 to 600 (s); a download with no data from the host for this long has stalled |
| `download_retries` | `--download-retries` (`download`) | `YANDEX_DISK_RESCUE_DOWNLOAD_RETRIES` | `5` | 0 to 20; immediate resumes of a download that failed after new data |
| `download_passes` | `--download-passes` (`download`) | `YANDEX_DISK_RESCUE_DOWNLOAD_PASSES` | `7` | 0 to 1000; passes over the files still to download, `0` until all are done |
| `download_connections` | `--download-connections` (`download`) | `YANDEX_DISK_RESCUE_DOWNLOAD_CONNECTIONS` | `20` | 1 to 64; requests to the download host in flight, and so files downloading at once |
| `download_checkpoint_mib` | `--download-checkpoint-mib` (`download`) | `YANDEX_DISK_RESCUE_DOWNLOAD_CHECKPOINT_MIB` | `32` | 1 to 1024 (MiB); bytes received between two checkpoints of a download |
| `scan_max_age` | `--scan-max-age` (`download`) | `YANDEX_DISK_RESCUE_SCAN_MAX_AGE` | `60` | 0 to 10080 (min); a younger covering scan is reused, `0` always scans |
| `client_id` | `--client-id` (`auth login`) | `YANDEX_DISK_RESCUE_CLIENT_ID` | the built-in app | 32 lowercase hex characters |

The `api_*` options apply to every command that talks to the Disk API
(`auth login`, `auth status`, `scan`, `download`); the `download_*` options
and `scan_max_age` apply to `download` and `client_id` to `auth login`.

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
