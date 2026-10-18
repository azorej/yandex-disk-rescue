# yandex-disk-rescue

The command-line application, a Typer CLI installed as `yandex-disk-rescue`.

```bash
uv run yandex-disk-rescue --help
uv run yandex-disk-rescue --version
uv run yandex-disk-rescue auth status
uv run yandex-disk-rescue scan disk:/Photos
uv run yandex-disk-rescue download disk:/Photos /tmp/photos
uv run yandex-disk-rescue download --no-scan disk:/Photos /tmp/photos
uv run yandex-disk-rescue download --retry-failed disk:/Photos /tmp/photos
uv run yandex-disk-rescue verify /tmp/photos
uv run yandex-disk-rescue verify --restart /tmp/photos
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
| `YANDEX_DISK_RESCUE_DOWNLOAD_PASSES` | Passes over the files still to download, resuming those that failed transiently, 0 to 1000 (default 7; 0 until every file is done or unavailable on Yandex Disk). Same as `--download-passes`. |
| `YANDEX_DISK_RESCUE_DOWNLOAD_CONNECTIONS` | Requests to the download host in flight, and so files downloading at once, 1 to 64 (default 20). Same as `--download-connections`. |
| `YANDEX_DISK_RESCUE_DOWNLOAD_CHECKPOINT_MIB` | MiB a segment of a download receives between two checkpoints, 1 to 1024 (default 32). Same as `--download-checkpoint-mib`. |
| `YANDEX_DISK_RESCUE_DOWNLOAD_SEGMENTS` | Segments a file above the segment threshold is downloaded as, in parallel, 1 to 8 (default 4; 1 never splits). Same as `--download-segments`. |
| `YANDEX_DISK_RESCUE_DOWNLOAD_SEGMENT_THRESHOLD_MIB` | Size above which a file is downloaded as segments, 1 to 65536 MiB (default 256). Same as `--download-segment-threshold-mib`. |
| `YANDEX_DISK_RESCUE_PARTIAL_DIR` | Existing folder, an absolute path outside DEST, below which `download` keeps DEST's partial files (in `<folder>/<key>/`) and copies each finished one to DEST; empty counts as unset (default: partial files in DEST). Same as `--partial-dir`. |
| `YANDEX_DISK_RESCUE_SCAN_MAX_AGE` | `download` reuses a covering scan younger than this, 0 to 10080 min (default 60; 0 always scans). Same as `--scan-max-age`. |
| `YANDEX_DISK_RESCUE_PROGRESS_INTERVAL` | Seconds between two status lines of `scan`, `download` and `verify`, on stderr and in the log file, 1 to 3600 (default 60). Same as `--progress-interval`. |
| `XDG_CONFIG_HOME` | Parent of the config directory holding the token file and `config.toml` (default `~/.config`). |
| `XDG_STATE_HOME` | Parent of the state directory holding the manifest and the log file (default `~/.local/state`). |

## Manifest

The manifest is a SQLite database at `$XDG_STATE_HOME/yandex-disk-rescue/manifest.sqlite3` (a 0600 file in a 0700
directory). For each account (keyed by the Disk user uid) it stores every file and folder of each scanned remote
folder: path, kind, size, MD5, SHA-256 and modification time. A scanned folder other than `disk:/` is a row too,
as the API names it, so rescanning `disk:/A` keeps the `disk:/A` row a scan of `disk:/` wrote; `disk:/` has no row.
It also records when the last scan of each folder completed (`Manifest.last_scan` finds the newest one covering a
folder: a scan of the folder itself or of an ancestor), and the entries each scan left out because their paths are
not valid Unicode (`unencodable_entries`, the path stored as a BLOB; `Mirror.unencodable` lists those below a
mirror's root), so a run reusing a scan still reports them.

For each destination directory ("mirror") it records the local name of every entry, each file's download state,
and the folder the partial files of its downloads in progress are in (`mirrors.partial_folder`, the path in OS bytes;
`Mirror.partial_folder` reads it, `None` for the destination's own `.yandex-disk-rescue/partial/`, and
`Mirror.record_partial_folder` sets it; a mirror from schema version 8 or older gets `None` on the upgrade).
A destination mirrors exactly one remote folder of one account; `Manifest.mirror` refuses a destination already
recorded for another folder or account (`MirrorMismatchError`, exit code 2). `Manifest.mirror_at` looks a recorded
mirror up by its destination without creating one.

- Local names (`local_names`) are paths relative to the destination, unique within the mirror.
  `Mirror.rename_local` renames one entry and moves everything below it along.
- A download record (`downloads`) is `partial` or `verified`. It keeps the remote identity it was made for (size,
  MD5, SHA-256, modification time); a record whose identity differs from the entry's current one is stale, and the
  caller decides what to do with it. A verified record also stores how the file was verified and the `lstat`
  fingerprint of the placed file (size, mtime in ns, inode number; no device number, which changes when a disk is
  re-plugged). `Mirror.verified_owner` finds the entry whose verified copy is a given inode.
- A partial record carries the checkpoint journal of its download (`download_segments`): segments tiling
  `[0, size)` in order (none for a zero-byte file), each with the offset up to which its bytes were fsynced before
  the row was committed and the SHA-256 of those bytes. `Mirror.record_partial` starts a journal,
  `Mirror.checkpoint` moves its confirmed offsets, `Mirror.download` reads the record as it is now; verifying or
  forgetting the download deletes its journal. A partial record from schema version 3 or older gets one segment
  confirming nothing on the upgrade, so its file is downloaded again from byte 0.
- A failure record (`failures`) holds, for files and folders alike, the reason the entry's last failed turn in the
  mirror was reported with, the count of its failed turns there since it last succeeded (at least 1), and its silent
  streak: how many of the last of those turns in a row got no byte from the download host (`silent_turns`, at most
  the count) and when the first of them did (`silent_since`, set exactly when `silent_turns` is above 0).
  `Mirror.record_failure(entry_id, reason, turns=…, silent_turns=…, silent_since=…)` replaces any earlier record (the
  caller owns the counts; a streak breaking those rules raises `ValueError` and writes nothing) and
  `Mirror.clear_failure` drops it; verifying the entry's file (`record_verified`) drops it too, while `forget` keeps
  it. `MirrorEntry.failure` reads it as a `FailureRecord`. A failure record from schema version 9 or older counts 1
  failed turn on the upgrade, and one from version 10 or older has no silent streak.
- A quarantine record (`quarantined`) holds the copy of the entry's last download that failed verification: its
  path relative to the destination, the reason, and the remote identity the download was for.
  `Mirror.record_quarantined` replaces any earlier record and deletes the entry's download record (with its journal)
  and its failure; verifying the file (`record_verified`) deletes it, while `forget` keeps it.
  `MirrorEntry.quarantine` reads it.
- A finding (`verify_findings`) is what was found wrong with the file at the entry's final path, with the reason:
  by `verify` (`missing`, `modified` or `corrupt`), or by `download` (`corrupt`, a copy it could not read). It
  belongs to the entry, not to its download record, so `record_partial`, `checkpoint`, `forget` and
  `record_quarantined` keep it; `record_verified` (a new copy placed or adopted there), `Mirror.clear_finding` and
  removing the entry drop it. `Mirror.record_finding` replaces any earlier one, and
  `MirrorEntry.finding` reads it.
- A preview record (`previews`) holds the preview image saved for a file not downloaded: its path relative to the
  destination, its byte size, its content type, the preview size it was asked for, and the remote identity it was
  taken for. `Mirror.record_preview` replaces any earlier one and `Mirror.forget_preview` drops it; every other
  record (`record_partial`, `checkpoint`, `record_verified`, `forget`, `record_failure`, `record_quarantined`,
  `record_finding` and the `clear_*` calls) keeps it. `MirrorEntry.preview` reads it as a `PreviewRecord`.
- A rescan never touches names, records, failures, quarantine records, findings or preview records; removing an
  entry from the manifest deletes them all.
- A verify pass (`verify_passes`, one row per mirror) is the `verify` in progress on the mirror: when it started and
  the Disk path of the last file whose check is committed (`checked_through`, `None` before the first).
  `Mirror.verify_pass` reads it, `Mirror.start_verify_pass` replaces it with a new one, `Mirror.checked_through`
  moves its cursor (refused when no pass is in progress) and `Mirror.end_verify_pass` deletes it.

- It only ever holds the result of completed scans. A scan stages its rows durably and moves them into the manifest
  in one transaction when it finishes; a scan stopped by an error, Ctrl+C, SIGTERM or SIGKILL leaves the previous
  content unchanged, and the next scan discards its staged rows. Rescanning a folder replaces only that folder's
  subtree. A scan can unstage a subtree before it commits (`ScanSession.discard`); `scan` does so for a folder deleted on
  the Disk or replaced by a file mid-scan, which the commit then records as removed.
- `Manifest.entries` and `ScanSession.staged` stream rows as the caller iterates; finish the iteration before
  writing to the same manifest. `Mirror.entries` returns a fully read list, so the caller can write records while
  walking it.
- One run at a time: opening the manifest takes an exclusive lock on `manifest.sqlite3.lock`, and a second run fails
  with `Another yandex-disk-rescue run is using ...`.
- `Manifest.open` creates the manifest when there is none; `Manifest.open_existing`, which `verify` uses, creates
  nothing and raises `ManifestMissingError` instead. Both take the lock and apply pending migrations.
- The database runs in WAL mode with `synchronous=FULL`, so it must live on a local filesystem.
- The schema version is `PRAGMA user_version`. Schema changes append a migration to `manifest.py`; opening the
  manifest applies pending ones, and a manifest from a newer version of the tool is refused.

## Layout

```
yandex-disk-rescue/
├── pyproject.toml    # Package metadata, dependencies, the `yandex-disk-rescue` console script, the uv_build backend
├── PYPI.md           # The PyPI page (the long description): absolute https:// links only
├── LICENSE           # A byte-identical copy of the repository LICENSE, shipped in the sdist and the wheel
├── src/yandex_disk_rescue/
│   ├── __init__.py   # Package version (read from installed metadata)
│   ├── cli.py        # Typer app — the entry point, `Error: ...` rendering of RescueError, exit code 143 of a run
│   │                 #   stopped by SIGTERM, the global `--config`
│   │                 #   (config-file values as Click's default_map), the `auth` commands, registration of
│   │                 #   the commands in `commands/`
│   ├── commands/
│   │   ├── scan.py   # The `scan` command: token, client, manifest, the scan engine, its summary, its JSON object and
│   │   │             #   interrupt message
│   │   ├── download.py # The `download` command: arguments, whether to scan (`--scan-max-age`, `--no-scan`),
│   │   │             #   destination and partial folder (`--partial-dir`), mirror run of every entry or of the
│   │   │             #   retry set (`--retry-failed`), its start line, the move line and the move's status
│   │   │             #   lines, the partial-folder line with its room once measured, summary, JSON object
│   │   │             #   (the report's, with the scan, the files to download, the move and the room), exit code
│   │   │             #   and interrupt message
│   │   └── verify.py # The `verify` command: the read-only destination, the existing manifest and its recorded
│   │                 #   mirror, starting or continuing a pass (`--restart`), its start line, summary, JSON
│   │                 #   object with the pass, exit code and interrupt message; no token, no network
│   ├── scanning.py   # The scan engine any command runs: resolving the root folder, the concurrent walk into a
│   │                 #   scan session (folders that vanish mid-walk, names that are not valid Unicode), the
│   │                 #   commit, the summary lines and `scan_document`, the scan's JSON object
│   ├── mirroring.py  # The mirror engine: local names, the walk over a mirror's entries or, with --retry-failed,
│   │                 #   over the retry set (failed, quarantined or partial files, files with a finding, failed
│   │                 #   folders) and the folders above it (skip, hold quarantined files, fetch again files with a
│   │                 #   finding without reading their copy, folders, things in the way, hard-link aliases) with up
│   │                 #   to `download_connections` files in progress at once (files with failure records started
│   │                 #   last, fewest failed turns first), after moving the confirmed bytes of the downloads in
│   │                 #   progress when the partial folder changed since the last run and, with separate partial
│   │                 #   files, admission to the partial folder's room (measured once, idle partial files counting
│   │                 #   what they allocate, each fetched file its full size, in the order files start: wait, defer
│   │                 #   to the next pass, or fail a file larger than the room, and every file deferred for room at
│   │                 #   once when a pass changed nothing in it),
│   │                 #   one placement lock over each check-clear-place-record of a final path, one hash of
│   │                 #   bytes on disk at a time per folder (re-hashing confirmed bytes, a read-back, a local
│   │                 #   copy), the first run-stopping error raised as itself once the other files are
│   │                 #   cancelled, the per-file pipeline (adopt a matching local copy, record one that cannot
│   │                 #   be read as a `corrupt` finding and fetch the file again, or resume a partial file
│   │                 #   segment by segment from its checkpoint journal once the cloud file is confirmed
│   │                 #   unchanged and each segment's confirmed bytes re-hashed, a journal keeping no byte
│   │                 #   planned again as a new download; the segment plan of a new download, one segment
│   │                 #   in a partial folder without sparse files; one link per file shared by its segments
│   │                 #   and taken once one holds a connection, preallocation of a segmented file, parallel
│   │                 #   ranged fetches of its segments while hashing with checkpoints, immediate retries
│   │                 #   after new data and one link refresh per pass, the fallback to one stream when the host
│   │                 #   ignores a range;
│   │                 #   verify, by reading a segmented file back, or with separate partial files by
│   │                 #   copying the finished file to its staged copy in DEST, one copy of the run at a time,
│   │                 #   hashing the bytes copied; quarantining a file that fails; move aside
│   │                 #   what is in the way, place, record), the rejected-name conversion of a folder or
│   │                 #   file name the destination refuses, later passes over transiently failed files,
│   │                 #   failure records written and counted as each turn fails (also one deferred to a
│   │                 #   later pass) and dropped as entries succeed, with the silent streak of turns that got
│   │                 #   no byte (throttling neither counts nor breaks it) and the unavailable-on-Yandex-Disk
│   │                 #   reason once it spans `UNAVAILABLE_TURNS` turns over `UNAVAILABLE_AGE`, the summary
│   │                 #   lines (with the partial files that could not be deleted once their file was
│   │                 #   recorded); disk work (hashing, writes, preallocation, fsync, placement) runs in the
│   │                 #   run's worker threads, one bounded call at a time, while the manifest and local names
│   │                 #   stay on the event loop; `mirror_document`, the run's JSON object
│   ├── verifying.py  # The verify engine: each file of a mirror in path order on the calling thread, re-hashed
│   │                 #   when verified for its current identity (intact, missing, modified, corrupt, each
│   │                 #   finding recorded at once) or listed from the manifest (quarantined, not downloaded;
│   │                 #   a finding with a quarantined repair stays under its finding, with the repair);
│   │                 #   stopping when the destination itself becomes unreadable; the pass's cursor, committed
│   │                 #   every `CURSOR_SECONDS` and when it stops, so a later part reads only the files after
│   │                 #   it while the report covers the whole pass; the summary lines and
│   │                 #   `verify_document`, the check's JSON object
│   ├── disk_threads.py # DiskThreads — the run's dedicated worker threads; a cancelled caller waits for its call
│   │                 #   in flight, so no descriptor is closed under a running call
│   ├── destination.py # The destination folder: argument checks (DEST and `--partial-dir`), the run lock and
│   │                 #   work directory, the RENAME_NOREPLACE probe, partial files in DEST's `partial/` or in
│   │                 #   DEST's own folder below `--partial-dir` (`<key>/`, created on open), the staged copy
│   │                 #   `partial/<id>.copy` a separate partial file is copied to (each left by a stopped run
│   │                 #   deleted at the start of the next), the partial folder an earlier run used (reading,
│   │                 #   deleting its partial files and its leftovers, whether it is this run's folder under
│   │                 #   another path), the free space of the partial folder,
│   │                 #   the bytes each partial file allocates there and whether its filesystem keeps sparse
│   │                 #   files (probed on open), reading existing files, whether the
│   │                 #   destination itself is still readable (`reachable`),
│   │                 #   folder creation, moving aside into the run's `replaced/` folder, moving a partial file
│   │                 #   or staged copy into the run's `quarantine/` folder, placement, and the `previews/`
│   │                 #   folder (saving a preview atomically, replacing an earlier one, removing it with the
│   │                 #   folders it emptied), never through a link; which errors stop the run;
│   │                 #   the read-only destination `verify` opens (lookups, reading files, the run lock when a
│   │                 #   lock file exists, whether the destination itself is still readable)
│   ├── localfs.py    # Destination syscalls relative to folder descriptors, never following links: lstat, open a
│   │                 #   folder or a file, mkdir, renameat2(RENAME_NOREPLACE) through ctypes; reading a chunk of
│   │                 #   an open file and setting its times; the free space of a folder's filesystem
│   ├── console.py    # The output of scan, download and verify: `-v`/`-q`/`--no-progress`/`--json`, the report
│   │                 #   lines on stdout (on stderr with `--json`), the run wrapped in `command_output` (log file
│   │                 #   and notice handlers attached for the run, its start and end records, the
│   │                 #   `Interrupted; …` line on Ctrl+C or SIGTERM, SIGTERM as a clean stop for the run,
│   │                 #   `LogFileError`, and with `--json` the one JSON document: the common keys, then the
│   │                 #   report object `Output.report` was given, or `error` alone), the start lines and the
│   │                 #   full summary logged, the `Manifest:` and `Log:` lines, and `Output.progress`, which reports one phase:
│   │                 #   its status-line thread (to the log file always, to stderr off a terminal unless
│   │                 #   `--no-progress` or `-q`) and on a terminal its live view; the one logged warning once
│   │                 #   stderr is off; whether stderr is a terminal
│   │                 #   (a tty, `TERM` neither dumb nor unknown); the engines and the API client log their own
│   │                 #   records (failures, findings, per-file outcomes, passes, throttling) through
│   │                 #   `logging.getLogger`
│   ├── view.py       # The live view of one phase, drawn with rich `Live` on stderr: the status line, a bar (a
│   │                 #   spinner for a scan), up to 10 files in progress with their speed or state, the file
│   │                 #   `verify` reads; notices and report lines printed above it; cleared when the phase ends
│   ├── progress.py   # Thread-safe progress states the engines update (ScanProgress, MoveProgress for the move of
│   │                 #   partial files to a new partial folder, MirrorProgress with one ActiveFile per file in
│   │                 #   progress, its state from waiting for room to copying to DEST, its speed over the last
│   │                 #   10 s and how far its copy to DEST got, VerifyProgress), their snapshots and status lines;
│   │                 #   speed over the last minute, ETA over the last 15 minutes; no I/O
│   ├── logs.py       # The handlers of the `yandex_disk_rescue` logger: the rotating 0600 log file (turned off with
│   │                 #   one notice when a write fails) and the notices on stderr (timestamped off a terminal),
│   │                 #   sent through the live view while one is shown; a write to stderr that fails turns
│   │                 #   stderr off for the run (`sys.stderr` then discards everything) instead of raising
│   ├── units.py      # format_size and format_duration — binary-unit byte counts and durations for command output
│   ├── display.py    # display_line and display_text — text with every control character and lone surrogate
│   │                 #   escaped (`\x1b`, `\x0a`, `\udcxx`), applied where the report, stderr, the live view and
│   │                 #   the log file are written
│   ├── errors.py     # RescueError — expected, user-facing failures with an exit code
│   ├── interrupts.py # SIGTERM as a clean stop: `Terminated`, the run-scoped handler (`sigterm_stops`) and the
│   │                 #   asyncio runner of the commands (`run`)
│   ├── paths.py      # XDG config and state directories, the manifest and log file paths
│   ├── manifest.py   # The manifest database: schema migrations, the run lock, atomic commit of a scan and its
│   │                 #   completion time, mirrors (local names, download records and their checkpoint journals,
│   │                 #   failure and quarantine records, findings, preview records, the verify pass and the
│   │                 #   folder of the partial files per destination)
│   ├── local_names.py # Local name mapping: long-name shortening, collision keys, the rejected-name conversion
│   ├── settings.py   # The options registry: name, flag, env var, default and validation of every option
│   ├── config.py     # The config file: reading and validating `config.toml` against the registry
│   ├── auth.py       # OAuth token: the authorization URL, resolution (env var, then 0600 file), the header-safety check
│   └── api/
│       ├── client.py # DiskClient — every Disk API request: the account check, one resource, paged folder listing,
│       │             #   download links, a file's preview link and the preview itself (the token only to the
│       │             #   API's origin and `downloader.disk.yandex.ru`, no redirects); error mapping (401/403,
│       │             #   other statuses, transport errors, malformed bodies) and bounded retries
│       ├── limiter.py # The AdaptiveLimiter every DiskClient request shares: pacing, concurrency cap, Retry-After
│       ├── models.py # Parsing of account, resource, folder-page, preview-link and download-link bodies
│       ├── remote_paths.py # `disk:/` path normalization
│       ├── errors.py # The RescueError subclasses of failed API requests
│       ├── user_agent.py # USER_AGENT — the User-Agent every request sends: tool name, version, project URL
│       └── transfer.py # ContentClient — fetches byte ranges from the download host into an async write
│                       #   callback, awaited before the next chunk is read, at most `connections` fetches in
│                       #   flight (waiting for one never counts as a stall), each asking for its link only once
│                       #   it has a connection, with no token, following redirects,
│                       #   with a stall (silence) timeout; its errors (expired link, transient, range not
│                       #   honoured, refused, too many bytes) never quote the link
└── tests/
    ├── data/manifest-v1.sql # A schema-v1 manifest dump, the input of a migration test
    ├── data/manifest-v3.sql # A schema-v3 manifest dump with download records, the input of a migration test
    ├── data/manifest-v4.sql # A schema-v4 manifest dump with a verified and a partial record, the input of a
    │                        #   migration test
    ├── installed/test_installed_cli.py # End-to-end tests of the `yandex-disk-rescue` on `PATH` in child processes
    │                       #   with an environment of their own, against the fake API: `--version`, `--help`,
    │                       #   `auth status`, a download with segmented files, a rerun, `verify`; stdlib, pytest
    │                       #   and `fake_disk_server` only, run against the dev install and against the built wheel
    ├── conftest.py         # Autouse env/XDG isolation; the `fake_disk_api` and `closed_port_url` fixtures; `disk`
    │                       #   (the fake serving `mirror_tree.FILES`), `dest`, and `partial_root` (a folder on
    │                       #   `/dev/shm`, another filesystem than `dest`'s)
    ├── mirror_tree.py      # The tree the download and verify tests mirror; running `download`, running a command
    │                       #   in a child process and waiting on it, the local path of a Disk file, the replaced
    │                       #   and quarantine run folders, a corrupted copy of bytes, the messages of the log file,
    │                       #   the lines of stderr without the notice timestamps
    ├── fake_disk_server.py # Real-socket fake Disk API and download host (stdlib only), started by `serve()` for
    │                       #   the length of a `with` block: an in-memory tree (`add_dir`, `add_file`,
    │                       #   `remove`) served by built-in `GET /v1/disk/`, `GET /v1/disk/resources` and the
    │                       #   download-link endpoint; `page_cap`, `account`, `token`; previews (`/preview`,
    │                       #   which needs the token; `redirect_previews`); the download host
    │                       #   (`/download`, `/storage`: Range, 400 to any Authorization header) and its faults
    │                       #   (`corrupt` and `repair`, `expire_links`, `ignore_range`, `redirect_downloads`, `chunk_delay`);
    │                       #   `inject`ed faults (statuses, delays, `Truncate`), matched by query parameters and
    │                       #   request headers such as `Range`; a `routes` override table; the
    │                       #   request log (start and finish times, body bytes sent); peaks of requests and of
    │                       #   content requests in flight
    ├── test_api_client.py
    ├── test_auth.py
    ├── test_cli.py
    ├── test_config.py
    ├── test_download.py
    ├── test_interrupts.py
    ├── test_local_names.py
    ├── test_manifest.py
    ├── test_output.py
    ├── test_packaging.py   # A real `uv build`: the sdist and the wheel, their metadata, the PyPI page and its links,
    │                       #   the license (byte-identical to the repository LICENSE), the console script, the
    │                       #   wheel's contents
    ├── test_rate_limiter.py
    ├── test_scan.py
    ├── test_transfer.py
    └── test_verify.py
```
