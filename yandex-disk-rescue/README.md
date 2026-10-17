# yandex-disk-rescue

The command-line application, a Typer CLI installed as `yandex-disk-rescue`.

```bash
uv run yandex-disk-rescue --help
uv run yandex-disk-rescue --version
uv run yandex-disk-rescue auth status
uv run yandex-disk-rescue scan disk:/Photos
uv run yandex-disk-rescue download disk:/Photos /tmp/photos
uv run yandex-disk-rescue download --no-scan disk:/Photos /tmp/photos
```

Tests: `mise run test -- yandex-disk-rescue/tests`.

## Environment variables

| Variable | Meaning |
|---|---|
| `YANDEX_DISK_RESCUE_TOKEN` | Yandex OAuth token; takes precedence over the stored token file. Blank is an error. |
| `YANDEX_DISK_RESCUE_CONFIG` | Config file with option values (default `$XDG_CONFIG_HOME/yandex-disk-rescue/config.toml`). Same as the global `--config`. |
| `YANDEX_DISK_RESCUE_CLIENT_ID` | ClientID of the OAuth app `auth login` authorizes (default: the built-in app). Same as `--client-id`. |
| `YANDEX_DISK_RESCUE_API_URL` | Base URL of the Disk REST API (default `https://cloud-api.yandex.net`). Same as the hidden `--api-url`. |
| `YANDEX_DISK_RESCUE_API_INTERVAL_MS` | Base gap between Disk API request starts, 25 to 1000 ms (default 50). Same as `--api-interval-ms`. |
| `YANDEX_DISK_RESCUE_API_CONCURRENCY` | Maximum Disk API requests in flight, 1 to 30 (default 7). Same as `--api-concurrency`. |
| `YANDEX_DISK_RESCUE_API_TIMEOUT` | Timeout of one Disk API request, 1 to 300 s (default 30). Same as `--api-timeout`. |
| `YANDEX_DISK_RESCUE_API_RETRIES` | Retries after a transport error or a throttling status, 0 to 20 (default 5). Same as `--api-retries`. |
| `YANDEX_DISK_RESCUE_DOWNLOAD_TIMEOUT` | Stall timeout: a download with no data from the download host for this long fails as stalled, 1 to 600 s (default 15). Same as `--download-timeout`. |
| `YANDEX_DISK_RESCUE_DOWNLOAD_RETRIES` | Immediate resumes of a download that failed after receiving new data, 0 to 20 (default 5). Same as `--download-retries`. |
| `YANDEX_DISK_RESCUE_DOWNLOAD_PASSES` | Passes over the files still to download, resuming those that failed transiently, 0 to 1000 (default 7; 0 until every file is done). Same as `--download-passes`. |
| `YANDEX_DISK_RESCUE_DOWNLOAD_CONNECTIONS` | Requests to the download host in flight, and so files downloading at once, 1 to 64 (default 20). Same as `--download-connections`. |
| `YANDEX_DISK_RESCUE_SCAN_MAX_AGE` | `download` reuses a covering scan younger than this, 0 to 10080 min (default 60; 0 always scans). Same as `--scan-max-age`. |
| `XDG_CONFIG_HOME` | Parent of the config directory holding the token file and `config.toml` (default `~/.config`). |
| `XDG_STATE_HOME` | Parent of the state directory holding the manifest (default `~/.local/state`). |

## Manifest

The manifest is a SQLite database at `$XDG_STATE_HOME/yandex-disk-rescue/manifest.sqlite3` (a 0600 file in a 0700
directory). For each account (keyed by the Disk user uid) it stores every file and folder of each scanned remote
folder: path, kind, size, MD5, SHA-256 and modification time. A scanned folder other than `disk:/` is a row too,
as the API names it, so rescanning `disk:/A` keeps the `disk:/A` row a scan of `disk:/` wrote; `disk:/` has no row.
It also records when the last scan of each folder completed (`Manifest.last_scan` finds the newest one covering a
folder: a scan of the folder itself or of an ancestor), and the entries each scan left out because their paths are
not valid Unicode (`unencodable_entries`, the path stored as a BLOB; `Mirror.unencodable` lists those below a
mirror's root), so a run reusing a scan still reports them.

For each destination directory ("mirror") it records the local name of every entry and each file's download state.
A destination mirrors exactly one remote folder of one account; `Manifest.mirror` refuses a destination already
recorded for another folder or account (`MirrorMismatchError`, exit code 2).

- Local names (`local_names`) are paths relative to the destination, unique within the mirror.
  `Mirror.rename_local` renames one entry and moves everything below it along.
- A download record (`downloads`) is `partial` or `verified`. It keeps the remote identity it was made for (size,
  MD5, SHA-256, modification time); a record whose identity differs from the entry's current one is stale, and the
  caller decides what to do with it. A verified record also stores how the file was verified and the `lstat`
  fingerprint of the placed file (size, mtime in ns, inode number; no device number, which changes when a disk is
  re-plugged). `Mirror.verified_owner` finds the entry whose verified copy is a given inode.
- A rescan never touches names or records; removing an entry from the manifest deletes its name and record.

- It only ever holds the result of completed scans. A scan stages its rows durably and moves them into the manifest
  in one transaction when it finishes; a scan stopped by an error, Ctrl+C or SIGKILL leaves the previous content
  unchanged, and the next scan discards its staged rows. Rescanning a folder replaces only that folder's subtree.
  A scan can unstage a subtree before it commits (`ScanSession.discard`); `scan` does so for a folder deleted on
  the Disk or replaced by a file mid-scan, which the commit then records as removed.
- `Manifest.entries` and `ScanSession.staged` stream rows as the caller iterates; finish the iteration before
  writing to the same manifest. `Mirror.entries` returns a fully read list, so the caller can write records while
  walking it.
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
│   ├── cli.py        # Typer app — the entry point, `Error: ...` rendering of RescueError, the global `--config`
│   │                 #   (config-file values as Click's default_map), the `auth` commands, registration of
│   │                 #   the commands in `commands/`
│   ├── commands/
│   │   ├── scan.py   # The `scan` command: token, client, manifest, the scan engine, its summary and Ctrl+C message
│   │   └── download.py # The `download` command: arguments, whether to scan (`--scan-max-age`, `--no-scan`),
│   │                 #   destination, mirror run, summary, exit code
│   ├── scanning.py   # The scan engine any command runs: resolving the root folder, the concurrent walk into a
│   │                 #   scan session (folders that vanish mid-walk, names that are not valid Unicode), the
│   │                 #   commit, the summary lines
│   ├── mirroring.py  # The mirror engine: local names, the walk over a mirror's entries (skip, folders, things in
│   │                 #   the way, hard-link aliases) with up to `download_connections` files in progress at once
│   │                 #   and one placement lock over each check-clear-place-record of a final path, the first
│   │                 #   run-stopping error raised as itself once the other files are cancelled, the per-file
│   │                 #   pipeline (adopt a matching local copy, or resume a partial file once the cloud file is
│   │                 #   confirmed unchanged, link, fetch while hashing with immediate retries after new data and
│   │                 #   one link refresh, verify, move aside what is in the way, place, record), the
│   │                 #   rejected-name conversion of a folder or file
│   │                 #   name the destination refuses, later passes over transiently failed files, the summary
│   │                 #   lines; disk work (hashing, writes, fsync, placement) runs in the run's worker threads,
│   │                 #   one bounded call at a time, while the manifest and local names stay on the event loop
│   ├── disk_threads.py # DiskThreads — the run's dedicated worker threads; a cancelled caller waits for its call
│   │                 #   in flight, so no descriptor is closed under a running call
│   ├── destination.py # The destination folder: argument checks, the run lock and work directory, the
│   │                 #   RENAME_NOREPLACE probe, partial files, reading existing files, folder creation, moving
│   │                 #   aside into the run's `replaced/` folder and placement, never through a link; which
│   │                 #   errors stop the run
│   ├── localfs.py    # Destination syscalls relative to folder descriptors, never following links: lstat, open a
│   │                 #   folder or a file, mkdir, renameat2(RENAME_NOREPLACE) through ctypes; reading a chunk of
│   │                 #   an open file and setting its times
│   ├── units.py      # format_size — binary-unit byte counts for command output
│   ├── errors.py     # RescueError — expected, user-facing failures with an exit code
│   ├── paths.py      # XDG config and state directories, the manifest path
│   ├── manifest.py   # The manifest database: schema migrations, the run lock, atomic commit of a scan and its
│   │                 #   completion time, mirrors (local names and download records per destination)
│   ├── local_names.py # Local name mapping: long-name shortening, collision keys, the rejected-name conversion
│   ├── settings.py   # The options registry: name, flag, env var, default and validation of every option
│   ├── config.py     # The config file: reading and validating `config.toml` against the registry
│   ├── auth.py       # OAuth token: the authorization URL, resolution (env var, then 0600 file), the header-safety check
│   └── api/
│       ├── client.py # DiskClient — every Disk API request: the account check, one resource, paged folder listing,
│       │             #   download links; error mapping (401/403, other statuses, transport errors, malformed
│       │             #   bodies) and bounded retries
│       ├── limiter.py # The AdaptiveLimiter every DiskClient request shares: pacing, concurrency cap, Retry-After
│       ├── models.py # Parsing of account, resource, folder-page and download-link bodies
│       ├── remote_paths.py # `disk:/` path normalization
│       ├── errors.py # The RescueError subclasses of failed API requests
│       └── transfer.py # ContentClient — fetches byte ranges from the download host into an async write
│                       #   callback, awaited before the next chunk is read, at most `connections` fetches in
│                       #   flight (waiting for one never counts as a stall), with no token, following redirects,
│                       #   with a stall (silence) timeout; its errors (expired link, transient, range not
│                       #   honoured, refused, too many bytes) never quote the link
└── tests/
    ├── data/manifest-v1.sql # A schema-v1 manifest dump, the input of the migration test
    ├── conftest.py         # Autouse env/XDG isolation; the `fake_disk_api` and `closed_port_url` fixtures
    ├── fake_disk_server.py # Real-socket fake Disk API and download host: an in-memory tree (`add_dir`, `add_file`,
    │                       #   `remove`) served by built-in `GET /v1/disk/`, `GET /v1/disk/resources` and the
    │                       #   download-link endpoint; `page_cap`, `account`, `token`; the download host
    │                       #   (`/download`, `/storage`: Range, 400 to any Authorization header) and its faults
    │                       #   (`corrupt` and `repair`, `expire_links`, `ignore_range`, `redirect_downloads`, `chunk_delay`);
    │                       #   `inject`ed faults (statuses, delays, `Truncate`); a `routes` override table; the
    │                       #   request log (start and finish times); peaks of requests and of content requests
    │                       #   in flight
    ├── test_api_client.py
    ├── test_auth.py
    ├── test_cli.py
    ├── test_config.py
    ├── test_download.py
    ├── test_local_names.py
    ├── test_manifest.py
    ├── test_rate_limiter.py
    ├── test_scan.py
    └── test_transfer.py
```
