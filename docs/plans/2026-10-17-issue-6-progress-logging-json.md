# Plan: Progress display, a log file and a JSON summary

Issue: #6 (umbrella #9)
Branch: feat/progress-logging-json (from `main` at a3ffe7b)

## Context

Observed on `main` (a3ffe7b), against the fake server with scratch XDG dirs only. The script started the real
`yandex-disk-rescue` console script in a child process, with stdout and stderr captured.

- **A run says nothing while it works.** Six files are downloaded, one of them stalling forever on the host
  (`Delay(30)` on its `/download`). The flags are `--download-timeout 2 --download-passes 3`. The run took 16.4 s, and
  after `Downloading 6 files (1.4 MiB) to …/dest` it printed nothing at all until the summary:
  ```
  --- download exit 1 16.4s
  STDOUT: 'Scanned disk:/A: 6 files, 0 folders, 1.4 MiB\n…\nDownloading 6 files (1.4 MiB) to …/dest\n
           Mirrored disk:/A to …/dest: 5 downloaded (1.4 MiB), 0 skipped, 0 quarantined, 1 failed\nPasses: 3 of 3\n
           Failed: 1\n  disk:/A/stuck.bin: the download stalled: no data for 2 s (after 3 passes)\nManifest: …\n'
  STDERR: 'Error: 1 failed; see the list above.\n'
  ```
  The live full mirror (133,521 files, 2.1 TiB, about 4 MB/s, so close to a week) runs under `nohup` with stdout and
  stderr going to files. For days the files show only the scan summary and the `Downloading …` line. Other phases
  are just as silent: `scan` (minutes of listing), the `lstat` check of every file already in DEST before the walk
  (minutes on a USB HDD), pauses between passes (up to 300 s), and `verify` (4–6 h on a USB HDD).
- **Nothing is logged.** The state directory holds only `manifest.sqlite3` and its lock. There is no `logging` call
  anywhere in the package. API throttling, retries, deferrals and every per-file outcome leave no trace beyond the
  20 paths per list that the summary shows (`… and N more`).
- **No machine-readable output.** `download --json` gives `No such option: --json` (exit 2).
- **Output plumbing.** Commands print with `typer.echo`, which flushes every line, so `> file 2>&1` keeps the order.
  Summaries live next to each engine (`scan_summary`, `mirror_summary`, `verify_summary`) and truncate through
  `scanning.listed`. Quarantine and repair details are baked into reason strings: `held_copy` builds
  `…; last retry: …`, and `VerifyReport.add` builds `…; repair quarantined: …; kept at …`.
- **Rich is already installed** (15.0.0, pulled in by typer) but is not a direct dependency. Its
  `Console.is_terminal` returns True whenever `FORCE_COLOR` or `TTY_COMPATIBLE` is set
  (`rich/console.py:955–964`), even when the output is a file.
- **Tests.** About 30 tests assert `result.stderr == 'Error: …\n'` or `'Interrupted; …\n'` exactly
  (`test_download.py`, `test_verify.py`, `test_scan.py`). Any new stderr output changes them.

## Goal

- **Live progress on a terminal** for `scan`, `download` and `verify`: overall files and bytes, speed, ETA, and the
  active transfers with their state. A transfer receiving no data is visible as such.
- **Line-oriented output elsewhere.** When stderr is not a terminal, a timestamped status line goes out every
  `progress_interval` seconds (default 60), so a multi-day `nohup` log stays readable and small. The tool's own
  output never sends control characters to a file or a pipe.
- **A rotating log file** under the XDG state directory. It records every run's start and end, every per-file
  outcome, transient failures, API throttling, and the full untruncated summary. `-v` and `-q` set the verbosity.
- **`--json`** prints the run's final report as one JSON document on stdout. It carries the same counts as the human
  summary, every list in full (quarantined copies, every kind of `verify` problem), and the pass of a resumed
  `verify`.
- **README: running unattended,** with `nohup` and a systemd timer.

## Decisions that go beyond the issue's wording (for the user to approve)

1. **Two channels.** stdout carries the *report*: the start lines, the scan summary, the final summary, the
   `Manifest:` and `Log:` lines, or with `--json` the JSON document. stderr carries progress, notices and errors.
   The live view is chosen by **stderr** being a terminal: `sys.stderr.isatty()`, the stream the view draws on,
   and `TERM` not `dumb` or `unknown`. The issue words it as stdout. `download … > out.txt` on a terminal therefore
   still shows the live view, and `out.txt` stays clean. The tool's own output ignores `FORCE_COLOR`,
   `TTY_COMPATIBLE` and the like, so an inherited environment cannot put escape codes into a status line, a notice
   or the report.
   - **Out of scope.** Typer's own rich panels for usage errors and `--help` (exit 2, before the command runs)
     honour `FORCE_COLOR`. They are left as Typer renders them: they are not progress and never appear in a
     running command's output.
2. **Three progress modes.** (a) *Live* on a terminal. (b) *Status lines* when stderr is not a terminal: one line
   every `progress_interval` s, plus a final line per phase with the elapsed time and the average speed.
   (c) *None* with `--no-progress`: no live view and no status lines. Notices and the report stay.
   - **Why (c).** The issue says `--no-progress` gives "plain, line-oriented output". Here plain output (b) is
     already what a non-terminal gets, so `--no-progress` means what it says. Use it under cron, where periodic
     lines are noise.
3. **Verbosity.** stderr shows by default progress, plus a notice (WARNING) as it happens for each file that fails
   or is quarantined in this run, and for each `verify` finding (missing, modified or corrupt). `verify` gives no
   notice for files it lists from the manifest: not downloaded, or held in quarantine. The summary lists those. `-v`/`--verbose` adds the INFO records: each file downloaded or adopted,
   each pass, each deferral, scan milestones, API throttling. It also raises the log file from INFO to DEBUG.
   `-q`/`--quiet` leaves only the final `Error: …` / `Interrupted; …` line on stderr, with no progress and no notices.
   stdout is never affected by `-v` or `-q`. `-v` with `-q` is a usage error (exit 2).
4. **A new registry option, `progress_interval`.** Seconds between two status lines (1 to 3600, default 60). It
   is also the interval of the status records in the log file. It has a flag, an env var and a config key, like
   every option, and applies to `scan`, `download` and `verify`.
5. **One rotating log file:** `$XDG_STATE_HOME/yandex-disk-rescue/yandex-disk-rescue.log`. It rotates by size,
   10 MiB, keeping 5 older files (`.1` … `.5`). Files are mode 0600 in the 0700 state directory, UTF-8 with
   `backslashreplace`. Each line starts with a UTC timestamp, the level, the command and the pid. Multi-line
   content (the summary, a traceback) is written as one prefixed line per line, so `grep` on the log always shows
   when and which run.
   - **Who writes it.** Only `scan`, `download` and `verify` log; the `auth` commands write nothing.
   - **The log ignores the console flags.** It gets INFO by default (DEBUG with `-v`), whatever `-q`,
     `--no-progress` or `--json` say.
   - **Every failure stays in the log.** The full untruncated summary is logged at the end of each run. So "the log
     names every failed file and the reason" holds even when the per-file records of a week-long run have rotated
     out.
6. **The log never holds the token or a download link.** Handlers attach to the `yandex_disk_rescue` logger only,
   never the root logger. httpx's own INFO records carry full request URLs, signed download links included.
7. **`--json` on `scan`, `download` and `verify`,** with a versioned schema (`schema_version: 1`).
   - **stdout.** Under `--json`, stdout holds exactly one JSON object, and the human report moves to stderr (so a
     `nohup` log still has it, and `-q` drops it).
   - **One document per command.** A command that produced its report writes that report, and only that. This
     includes a download with failed or quarantined files (`FilesNotMirroredError`) and a verify with problems
     (`ProblemsFoundError`). The report carries `exit_code` and, when it is not 0, `error: {message}`.
   - **Error-only document.** Written only when the command ends with no report: the common keys plus
     `error: {message}` and `exit_code`. The cases are a token Yandex rejects, a `DestinationError` or API outage
     mid-run, `NeverScannedError`, `NotAMirrorError`, a `LogFileError`, and Ctrl+C (130).
   - **No JSON for usage errors.** Click and Typer usage errors print no JSON (exit 2). That covers errors before
     the command runs and the argument checks each command makes before `command_output` (Stage 1), whatever their
     exception type (`UnsupportedPathError` and `DestinationUsageError` included).
   - **Lists.** Every list is complete. The one exception is the scan's files without a checksum, which the engine
     samples (20 smallest paths plus the count); the key is named `sample` accordingly.
8. **Structured quarantine details.** `QuarantinedFile` gains `last_retry: str | None` and `verify`'s `Problem`
   gains `repair: QuarantinedFile | None`. The reason strings no longer have these appended. The human lines are
   byte-for-byte unchanged: the summary composes them. JSON gets `last_retry` and `repair_quarantined` fields
   instead of parsing text.
9. **Progress covers every slow phase:**
   - `scan`'s listing, and the scan inside `download`;
   - `download`'s check of the files already in DEST;
   - each pass, and the pause before it;
   - each active transfer's state: waiting for a connection, downloading, *no data for N s* (shown from 5 s on),
     preallocating, hashing a local copy, reading back a segmented file;
   - `verify`, including the bytes of the file being read, and a continued pass.

   Preallocation is a visible state, so the zero-fill of #18 shows up in the view and the log.
10. **Speed and ETA.** Speed is the rate over the last 60 s. The ETA is the remaining bytes divided by the rate over
    the last 15 min, so one stall does not swing it. Sizes are in binary units, like every other line of the tool.
    Durations read `45 s`, `3 min 10 s`, `5 h 2 min`, `6 d 7 h`. The ETA reads `ETA unknown` while the rate is 0.
11. **The live view is transient.** It is cleared when its phase ends. The notices printed above it and the report
    stay.
12. **`Log: <path>`** follows the `Manifest: <path>` line at the end of every human report.
13. **Log failures.**
    - **Opening.** A log file that cannot be opened stops the command before it does anything:
      `Error: Could not open the log file <path>: <reason>.` (exit 1). Under `--json` this produces an error-only
      document.
    - **Mid-run.** A write that fails mid-run (the state disk full, an I/O error) never stops the rescue. stderr gets
      one notice, `Warning: could not write the log file <path>: <reason>; logging to it is off for this run.`, even
      under `-q`, and the file handler is detached. The notice goes through the same path as every other notice
      (printed above the live view, timestamped in plain mode). Detaching happens once under a lock, so threads that
      fail together print a single notice. The run goes on.
    - **The report says so.** The human report then ends with `Log: <path> (incomplete: <reason>)`, and the JSON
      carries `log_incomplete: "<reason>"` (otherwise `null`). Python's default `handleError`, a traceback per
      record, is never used.

## Non-goals

- SIGTERM (#15). The log's end record and the live view's cleanup run on every exception path, so #15 only needs
  to route SIGTERM into the Ctrl+C path. Until it lands, the README's systemd unit uses `KillSignal=SIGINT`.
- Zero-fill on HFS+ (#18). Preallocation becomes visible (Decision 9), and nothing about it changes.
- Files that never start sending (#14). They show as *no data for N s*, then deferred, then failed as today.
- A log file per run, a `--log-file` option, syslog/journald output, colour themes.
- Progress for `auth` commands.
- Manifest schema changes. None are needed: the manifest stays at v8.

## Output contract (all stages build towards this)

| | Terminal stderr | stderr not a terminal | `--no-progress` | `-q` |
|---|---|---|---|---|
| stdout | report (or JSON) | report (or JSON) | report (or JSON) | report (or JSON) |
| stderr progress | live view, refreshed twice a second, cleared at phase end | `<UTC ts> <status line>` every `progress_interval` s + a final line per phase | none | none |
| stderr notices | WARNING (INFO with `-v`), printed above the view, no timestamp | `<UTC ts> <message>` | as without | none |
| stderr result | `Error: …` / `Interrupted; …` once, untimestamped | same | same | same |
| log file | INFO (DEBUG with `-v`), status record every `progress_interval` s | same | same | same |

Under `--json`, the human report goes to stderr, and `-q` suppresses it there. Every line written to either stream is
flushed as it is written. stdout is byte-identical whatever stderr is connected to.

Status line formats (the log record has the same text):

```
Scanning disk:/: 45120 files, 3200 folders (812.3 GiB) listed, 1024 folders to list
Scanned disk:/: 133521 files, 9800 folders (2.1 TiB) listed in 14 min 5 s
Checking 133521 files already in /mnt/backup: 40210 checked
Checked 133521 files already in /mnt/backup in 6 min 12 s
Downloading: 1234 of 133521 files, 12.3 GiB of 2.1 TiB (0.6%), 3.9 MiB/s, ETA 6 d 7 h; 20 active, 3 without data; 5 to retry, 3 failed, 0 quarantined; pass 1 of 7
Downloading: …; pass 2 of 7 starts in 4 s
Downloaded: 133521 of 133521 files, 2.1 TiB in 6 d 9 h, average 4.0 MiB/s; 0 to retry, 3 failed, 0 quarantined; 2 of 7 passes
Verifying: 1234 of 98000 files, 120.5 GiB of 2.0 TiB (5.9%), 110.2 MiB/s, ETA 5 h 2 min; 2 problems; reading disk:/big.mkv (1.2 GiB of 4.0 GiB)
Verified: 98000 of 98000 files, 2.0 TiB in 5 h 10 min, average 112.4 MiB/s; 2 problems
```

The scan and check lines exist in `scan` and `download`. Each phase's last line (`Scanned`, `Checked`,
`Downloaded`, `Verified`) is the final line written when the phase ends; it has the elapsed time and the average
speed in place of the ETA.

"Files" and "bytes" count the files the phase attempts: `Downloading N files (size)` and `Verifying N files`
(`… left` for a continued pass). A file counts as done once it settles (verified, adopted, failed or quarantined).
A file still in progress or waiting for a later pass counts the bytes its partial file holds, resumed bytes
included. A file cut back to byte 0 (the host ignored a range) loses them again.

The download totals cover the *pending set*: the files `MirrorRun` counts in `pending_files` (neither current nor
held). Some report entries are outside it and never appear in the progress counts:
- failed folders;
- quarantined files held without being looked at;
- entries left out for a name that is not valid Unicode.

The invariant is that at the end of the phase, the settled outcomes equal the report's counts for files in the
pending set. That is downloaded plus adopted, failed (files only, excluding the unencodable entries), and
quarantined in this run (excluding the held files).

## Stages

### Stage 1: Command output, the log file and verbosity

- **Contract:**
  - **`paths.log_path() -> Path`:** `state_dir() / 'yandex-disk-rescue.log'`.
  - **New module `console.py`.** It is the command's output and holds no rich yet.
    - `OutputOptions` (frozen), in Stage 1: `verbosity: Literal['quiet', 'normal', 'verbose']`. Stage 2 adds
      `progress: bool` and `progress_interval: int`, and Stage 4 adds `json: bool`, each with its flag.
    - **Flags.** The Typer annotations `Verbose` (`-v/--verbose`) and `Quiet` (`-q/--quiet`) are plain flags, not
      registry settings. `-v` together with `-q` raises `typer.BadParameter`.
    - **Usage errors come first.** Every argument check that needs no network and no state runs before
      `command_output`, whatever its exception type: the `-v`/`-q` conflict, `download`'s `_split_locations`,
      `normalize_remote_path` (`UnsupportedPathError`) and `check_destination` (`DestinationUsageError`). Such a check
      prints no JSON and leaves no log record. A usage error therefore never opens the log. Inside `command_output`, a `click.ClickException` or `typer.Exit` that still arises is logged as
      its exit code and message, with no traceback.
    - `command_output(command: str, options: OutputOptions, *, details: Mapping[str, object]) -> ContextManager[Output]`.
      - **On entry.** It opens the log (Decision 5), attaches the console and file handlers to the
        `yandex_disk_rescue` logger, and logs the start record: command, version, pid, `details` (the arguments and
        effective option values; never the token).
      - **On exit.** It logs the end record: `completed`, or the `RescueError` message and exit code, `interrupted`
        (130), or an unexpected exception at ERROR with its traceback. Then it detaches and closes the handlers, so
        `CliRunner` tests never accumulate them. This runs for every exception, `BaseException` included.
      - **Failure.** If the log cannot be opened, it raises `LogFileError(RescueError)`.
    - `Output.line(text)` writes one report line (stdout). `Output.log_path` is the log's path.
  - **New module `logs.py`.** It holds the file handler: rotation 10 MiB × 5, files created 0600 (rotated files
    included), the line format `2026-10-17T19:20:01.123Z INFO download[12345] <message>`, `backslashreplace`. It also
    holds the stderr notice handler: `<UTC ts> <message>` with no level name, WARNING by default, INFO with `-v`, off
    with `-q`. Multi-line messages become one prefixed line each. A failed file write follows Decision 13: one stderr
    notice, then the file handler is detached, and `Output` records the reason for the `Log:` line. The end record of a run that ends in error goes to the file only, so stderr shows the error once
    (as the `Error:` line `RescueErrorGroup` prints).
  - **`scan`, `download` and `verify`** take `-v` and `-q`. Each command wraps its whole body in `command_output` and prints every
    report line through `Output.line`. The report ends with `Log: <path>` after `Manifest: <path>`.
  - **Summaries take a limit.** `scan_summary`, `mirror_summary` and `verify_summary` gain
    `limit: int | None = SHOWN_PATHS`, threaded into `listed` (`None` lists everything). The command logs its summary
    with `limit=None` at INFO before printing it.
  - **Engine records.** Each engine logs through `logging.getLogger(__name__)`, with paths through `display_path`.

    | Level | Records |
    |---|---|
    | WARNING | a file or folder failed in this run (path, reason); a file quarantined in this run (path, reason, copy); a `verify` finding made in this run, missing, modified or corrupt (path, kind, reason); a folder vanished during a scan; an entry left out for its name |
    | INFO | a file downloaded (path, size, seconds, average speed, verified by); adopted; moved aside; renamed; deferred to a later pass (path, reason); a pass starting (number, limit, files, size, pause); scan started and committed (totals); a reused scan; a verify pass started or continued; a Disk API throttling response (status, the new interval, `Retry-After`); a Disk API retry (reason, attempt) |
    | DEBUG | a skipped current file; a held quarantined file; a file `verify` lists as not downloaded or quarantined; a file found intact by `verify`; each transfer attempt and immediate retry; a link refresh; a resumed segment (bytes kept); the fallback to one stream; each checkpoint; each folder listed |

- **Behaviour to lock** (new `tests/test_output.py` for the cross-command contract; per-command details in the
  existing files):
  - **Acceptance 2.** 25 files whose download link answers 404 (refused): the run exits 1, stdout lists 20 of them
    and `… and 5 more`, and the log file names all 25 with their reasons. A rerun with `--retry-failed` appends to
    the same log, which then holds both runs' start and end records with their exit codes.
  - **No secrets.** `download -v` of the `disk` tree with an expired-link fault. The log never contains `TOKEN`,
    `link=` (the fake's signed-link query) or `/storage?`. It does hold the DEBUG link-refresh record. `/download?`
    is not asserted on, since the harmless API path `/v1/disk/resources/download?` matches it too.
  - **File mode and rotation.**
    - The log and the state directory are 0600 / 0700.
    - Rotation is exercised by monkeypatching `logs.MAX_BYTES` small in-process. After several runs, `.1` exists,
      every file is 0600, and the newest file holds the latest run's end record.
  - **`-q` and `-v`.**
    - `-q` on a run with a failure: stderr is exactly `Error: 1 failed; see the list above.\n`.
    - Without `-q`: stderr also has the timestamped WARNING line for that file before it.
    - `-v`: stderr has a `downloaded` line per file.
    - `-v -q`: exit 2.
  - **An unwritable state directory** (a file where the directory should be): exit 1 with the `Could not open the log
    file` message, and nothing downloaded.
  - **A log write failing mid-run.** In-process, monkeypatch the file handler's stream write to raise `ENOSPC` after
    the start record. The download completes with exit 0 and every file verified. stderr has the one `Warning: could
    not write the log file` line, and stdout ends with `Log: <path> (incomplete: …)`.
  - **Usage errors open no log.** `download a b c` and `-v -q` exit 2, and no log file exists afterwards.
  - **Existing tests.**
    - Update the exact-stderr assertions to the new contract: the result line is the last line, and failure notices
      precede it.
    - The about 10 exact `stdout ==` assertions gain the `Log:` line: `test_scan.py:79/99/153/288`,
      `test_verify.py:153/180/310` and `test_download.py:197/699`, plus any other the implementer finds with
      `rg 'stdout =='`.
    - `test_scan.py`'s long-list test gains the untruncated log check.
- **Deliverable:** every run of `scan`/`download`/`verify` leaves a complete, private, rotating log, and failures
  show on stderr as they happen.
- **Validation:** `mise run check`; `mise run test -- yandex-disk-rescue/tests/test_output.py`; `mise run ci`.
- **Doc updates:**
  - **Root `README.md`:**
    - A new "Output and logs" section (after "Verifying a mirror"): the two channels, the notices, `-v`/`-q`, the
      log file (path, rotation, mode, levels, what is never logged).
    - `Log:` in the download and verify summary examples.
    - The `XDG_STATE_HOME` row: the manifest and the log.
    - Exit codes: the log-file error.
  - **Package `README.md`:** Layout (`console.py`, `logs.py`), the `XDG_STATE_HOME` row.

### Stage 2: Progress state and status lines

- **Contract:**
  - **New module `progress.py`.** It has no rich, no typer and no I/O. It holds thread-safe progress states the
    engines update and renderers sample.
    - **`ScanProgress`.** Listed files, folders and bytes; folders still to list.
    - **`MirrorProgress`.** Phase `checking` (checked of total), `transferring` or `pausing` (until when); totals;
      settled counts by outcome; per-file active entries with path, size, held bytes, state, start time and
      last-data time; the pass number and limit; a rate history bounded to the last 15 minutes (one sample a
      second at most, so at most 900 samples).
    - **`VerifyProgress`.** Totals of this part, files and bytes read, problems so far, the current file and its
      bytes read, whether the pass is continued.
    - Each state has `snapshot()`, which returns an immutable snapshot, and `status_line(snapshot, *, final: bool)`,
      which returns the text in the Output contract. `units.format_duration(seconds: float) -> str` formats
      durations (Decision 10).
    - **Invariants:**
      - Held and done bytes never exceed the total, and settled files never exceed the total.
      - At the end of a download phase, settled files equal the `Downloading N files` count. The settled outcomes
        equal the report's counts for files in the pending set, as defined under the Output contract (failed
        folders, held quarantined files and unencodable entries excluded).
  - **Engine wiring.**
    - **Signatures.** `scan_tree(…, progress: ScanProgress)`, `MirrorRun(mirror, destination, *, retry_failed,
      progress: MirrorProgress)`, `VerifyRun(mirror, destination, verify_pass, progress: VerifyProgress)`.
    - **Download events.** `MirrorRun` reports the `lstat` check as it runs. Every file counted in its pending total
      ends exactly once per pass it takes part in. A file deferred to a later pass ends as "to retry" and starts
      again in that pass. A file under a failed folder ends failed without starting. A stream is "waiting for a
      connection" until it calls for its link, and "without data" once 5 s pass with no bytes.
    - **Verify events.** `VerifyRun` counts bytes as chunks are hashed.
  - **`console.Output.progress(state) -> ContextManager[None]`.** It runs the renderer for one phase.
    - **Plain mode.** A daemon thread emits a status record every `progress_interval` s to the log (always) and to
      stderr (unless `--no-progress` or `-q`), then the final record when the context exits. No line is written after
      it exits.
    - **The flag.** `--no-progress` is added to `scan`, `download` and `verify` here, with
      `OutputOptions.progress` (Decision 2).
  - **New registry option `progress_interval`** in `settings.py`: 1 to 3600 s, default 60, flag
    `--progress-interval`, env `YANDEX_DISK_RESCUE_PROGRESS_INTERVAL`. It is taken by `scan`, `download` and
    `verify`.
- **Behaviour to lock** (`tests/test_output.py`, subprocess with real pipes):
  - **Acceptance 1.**
    - `download --progress-interval 1` of a tree served with `chunk_delay` so it takes about 3 s, run with
      `FORCE_COLOR=1` and `TTY_COMPATIBLE=1` in its environment: neither stdout nor stderr contains `\x1b` or `\r`.
    - stderr has at least two timestamped `Downloading: … of 6 files` lines, with the done bytes never decreasing,
      then one final `Downloaded: 6 of 6 files` line.
    - stdout equals that of the same run with `--no-progress`, modulo paths.
  - **Order in one file.** With stderr merged into stdout (`2>&1`), the `Downloading N files (…)` report line comes
    before the first `Downloading:` status line, and the summary after the final `Downloaded:` line.
  - **Counts match the report.** The run also has a held quarantined file and a failed folder, which are outside the
    pending set. The final `Downloaded:` line's failed and quarantined counts equal the summary's minus those
    entries.
  - **Final lines of the other phases.** A `scan` with `--progress-interval 1` ends with one `Scanned disk:/…` line.
    A rerun of `download` ends its check phase with one `Checked N files already in …` line.
  - **A stalled file is visible.** One file is `Delay`ed beyond `--download-timeout 6`, with `--progress-interval 1`:
    some status line shows `1 without data`, and after the deferral `1 to retry` and `pass 2 of 2 starts in`.
  - **Resumed `verify`.** With `--progress-interval 1` and a slow read, `verify` shows `Verifying: … of N files`
    lines. After a SIGINT and a rerun, its lines count only the files left, matching the `Continuing … N files left`
    line.
  - **Silence.** `--no-progress` and `-q` write no status line to stderr, and the log still has the status records.
  - **The option.** `progress_interval` from config, env and flag, and out-of-range values: extend `test_config.py`'s
    registry checks.
- **Deliverable:** a `nohup` run shows its progress once a minute, with no flood and no escape codes.
- **Validation:** `mise run check`; `mise run test -- yandex-disk-rescue/tests/test_output.py
  yandex-disk-rescue/tests/test_config.py`; `mise run ci`.
- **Doc updates:**
  - **Root `README.md` "Output and logs":** rewrite it as the current state, with the status lines (format,
    interval, final line, what counts) and `--no-progress`. The Configuration table gets `progress_interval`.
    The sentence on which options apply to which commands (README.md:672) now says that `progress_interval`
    applies to `scan`, `download` and `verify`.
  - **Package `README.md`:** the env-var table row, Layout (`progress.py`).

### Stage 3: The live view on a terminal

- **Contract:**
  - **Dependency.** `uv add --package yandex-disk-rescue rich` (rich is then a direct dependency; the lock keeps
    15.0.0).
  - **Live mode.** When stderr is a terminal (Decision 1) and neither `--no-progress` nor `-q` is given,
    `Output.progress` renders the phase's snapshot with rich `Live` on a `Console(file=sys.stderr,
    force_terminal=True)`, twice a second.
    - **The view.** The status text; a bar for bytes (or a spinner for a scan); up to 10 active transfers, each with
      its path (shortened from the left), held of size, percent, and its speed or state (`waiting for a connection`,
      `no data for 9 s`, `preallocating`, `hashing`, `reading back`); then `… and N more`. `verify` shows the file
      being read.
    - **`redirect_stdout` and `redirect_stderr` are off.** Report lines are never printed while a view is live, and
      notices go through the live console, above the view, with markup and highlighting off. A name such as
      `[red]x[/red].txt` prints literally.
    - **The view is transient** and is stopped on every exit path (cursor restored).
  - **Plain mode is unchanged.** The log keeps its status records in live mode too.
- **Behaviour to lock** (`tests/test_output.py`, the child's stderr on a pseudo-terminal from `pty.openpty()`, with
  `TERM=xterm-256color` and `COLUMNS=100`; stdout a pipe):
  - **Reading the pty.** A thread drains the master fd while the child runs. `EIO` from reading the master once
    the child has exited is the expected end of output, not an error.
  - **The view.** stderr contains escape sequences and a transfer row for a slow file.
  - **The view was cleared.** After the last erase-line sequence rich writes when it stops the transient view, no
    bar characters and no transfer-row text remain; only notices, the result line or nothing follow. This is
    checked on the raw bytes, with no terminal emulator and no new dependency.
  - stdout is byte-identical to a run with stderr piped.
  - A failing file named `[red]x[/red].txt` appears literally in the notice on the terminal.
  - `TERM=dumb` on the pty gives status lines, not the live view.
  - Ctrl+C (SIGINT to the child) mid-run: exit 130, `Interrupted; rerun to continue the download.` is on the
    terminal, and the log's end record says `interrupted`.
- **Deliverable:** an interactive run shows what is happening at a glance, including stuck transfers and disk-bound
  states.
- **Validation:**
  - Run `mise run ci` early in the stage, with the first pty test in place, to confirm the Dagger container gives
    the tests a working pty before the rest is built on it.
  - Then `mise run check`, `mise run test -- yandex-disk-rescue/tests/test_output.py` and `mise run ci`.
- **Doc updates:**
  - **Root `README.md` "Output and logs":** rewrite it as the current state, with the live view (what it shows, the
    transfer states, when it is used, `TERM=dumb`). The status paragraph at the top lists progress, the log file
    and (from Stage 4) `--json` among the features that work.
  - **Package `README.md`:** the `rich` dependency is mentioned where the layout describes `console.py`.

### Stage 4: `--json` reports

- **Contract:**
  - **The flag.** `--json` is added to `scan`, `download` and `verify` here, with `OutputOptions.json`.
  - **Report types (Decision 8):**
    - `QuarantinedFile(path, reason, copy, last_retry: str | None)`; `held_copy(entry, last_retry)` fills
      `last_retry` instead of appending it.
    - `verifying.Problem(path, reason, repair: QuarantinedFile | None = None)`; `VerifyReport.add` sets `repair`.
    - `mirror_summary` and `verify_summary` compose the existing lines (`; last retry: …`,
      `; repair quarantined: …; kept at …`). The human output is unchanged.
  - **Document builders**, next to each summary. Each returns a JSON-ready `dict` of the report; the command adds
    the common keys.
    - `scan_document(result: ScanResult) -> dict`
    - `mirror_document(report: MirrorReport) -> dict`
    - `verify_document(report: VerifyReport) -> dict`
  - **Common keys:** `schema_version: 1`, `command`, `version`, `started_at`, `finished_at` (ISO 8601 UTC),
    `manifest`, `log`, `log_incomplete`, `exit_code`. When `exit_code` is not 0, there is also `error: {message}`.
  - **`scan`:**
    - `root`, `files`, `folders`, `size`;
    - `without_checksum: {count, sample}`;
    - `vanished`, `unencodable`, `first_scan`, `added`, `changed`, `removed`.
  - **`download`:**
    - `remote_root`, `destination`, `retry_failed`;
    - `scan`: the scan object without the common keys plus `reused: false`, or
      `{reused: true, root, completed_at}`;
    - `to_download: {files, size}`;
    - `counts: {downloaded, downloaded_size, skipped, adopted, size_only, renamed, moved_aside, quarantined, failed}`,
      where `skipped` includes `adopted`, as in the head line;
    - `passes: {used, limit} | null`;
    - `adopted`, `size_only`, `renamed: [{path, local}]`, `moved_aside`, `replaced_folder`;
    - `quarantined: [{path, reason, last_retry, copy}]`, `failed: [{path, reason}]`.
  - **`verify`:**
    - `destination`, `remote_root`;
    - `pass: {started_at, continued, files_read, size_read}`;
    - `counts: {intact, size_only, missing, modified, corrupt, quarantined, not_downloaded, problems}`;
    - `size_only`;
    - `missing` / `modified` / `corrupt`: `[{path, reason, repair_quarantined: {reason, copy} | null}]`;
    - `quarantined: [{path, reason, last_retry, copy}]`, `not_downloaded: [{path, reason}]`.
  - **One document (Decision 7).** When the command has its report, `command_output` writes the report document,
    whatever the exit code. `FilesNotMirroredError` and `ProblemsFoundError` set `exit_code: 1` and
    `error.message`. Only when the command ends with no report does it write the error-only document: the common
    keys that are known plus `error: {message}`. The cases are a `RescueError` before or during the run (a rejected
    token, `DestinationError`, `NeverScannedError`, `NotAMirrorError`, `LogFileError`) and Ctrl+C (130). Never two
    documents.
  - **Encoding.** Sizes are integers in bytes, and paths go through `display_path`. The document is written by
    `json.dumps(…, ensure_ascii=False, indent=2)` with a trailing newline.
  - **Streams.** Under `--json`, `Output.line` writes to stderr (or nowhere with `-q`). The summary's `Error: …` line
    still goes to stderr, and the exit codes are unchanged.
- **Behaviour to lock:**
  - **`test_download.py` (Acceptance 3).**
    - The quarantine-and-failure run (the existing `1 failed and 1 quarantined` setup), with `--json`: stdout parses,
      with nothing else on it.
    - `counts` equal the numbers in the human head line of the same run without `--json`.
    - `quarantined[0].copy` exists in DEST and `failed` has the reason. `exit_code` is 1, as is the process, and
      `error.message` is `1 failed and 1 quarantined; see the lists above.`.
    - stdout holds exactly one JSON value: `json.loads(stdout)` succeeds, and there is no second document after it.
    - A held quarantined file with a failed retry has `last_retry` set.
  - **`test_verify.py`.**
    - The each-kind-of-damage mirror plus the repair-quarantined case: every list is complete, and the repair case
      has `repair_quarantined` with the copy.
    - A continued pass after SIGINT has `pass.continued: true`, and its `counts` equal an uninterrupted run's.
    - A verify with problems writes one report document with `exit_code: 1` and `error.message`, not an error-only
      document.
  - **`test_scan.py`:** a rescan's `added`/`changed`/`removed` hold every path (more than 20).
  - **`test_output.py`.**
    - `--json` with a token Yandex rejects: one error-only document with `exit_code: 1` and no report keys, and the
      process exits 1.
    - `--json` with an unopenable log file: one error-only document, exit 1.
    - `--json` and SIGINT: one error document with `exit_code: 130`.
    - `--json -q` on a clean run: stderr is empty.
- **Deliverable:** scripts get a complete, stable, parseable record of every run.
- **Validation:** `mise run check`; `mise run test -- yandex-disk-rescue/tests`; `mise run ci`.
- **Doc updates:**
  - **Root `README.md` "Output and logs":** rewrite it as the current state, with `--json`: when it is printed, the
    streams, the schema with one example per command, the error document, `schema_version`.
  - **Package `README.md`:** Layout entries for the document builders.

### Stage 5: Running unattended

- **Contract:** documentation only. Every command and unit shown is checked against the CLI as built.
- **Deliverable:** a root `README.md` section, "Running unattended" (after "Output and logs"), as the current state:
  - `nohup yandex-disk-rescue download disk:/ /mnt/backup > download.out 2> download.err &`: what each file holds,
    `tail -f` on the log, and how big each grows (a status line a minute).
  - **A systemd user service and timer.**
    - The service: `Type=oneshot`; `ExecStart=… download -q --no-progress --json disk:/ /mnt/backup`, with the JSON
      to a file through `StandardOutput=append:` or `file:`; `KillSignal=SIGINT` (a clean stop with checkpoints,
      until #15); `TimeoutStopSec`.
    - `TimeoutStartSec=infinity`, since a oneshot's default start timeout would kill a multi-day run.
    - `loginctl enable-linger <user>`, so a user service runs without a login session.
    - The timer: `OnCalendar=daily`, `Persistent=true`. A oneshot still running is not started twice.
    - The journal gets the notices and the result line.
  - **Cron:** `-q`, the exit code as the signal, and `--json` into a file.
  - **Checking a run afterwards:** the summary, the log, and `jq` on the JSON.
- **Behaviour to lock:** none new. The commands shown are exercised by Stage 1–4 tests. The implementer runs the
  `ExecStart` command line against the fake server once and pastes the result in the stage report.
  `systemd-analyze verify` runs on the unit files if it is available in the devcontainer.
- **Validation:** `mise run lint` (Markdown code blocks); `mise run check`.
- **Doc updates:**
  - The section itself.
  - The "Goals" bullet "CLI only. Scriptable, cron- and systemd-friendly" links to it.
  - The "Authentication → Automation" paragraph (README.md:70–73) links to it for the rest of the setup.
  - The Configuration sentence on which options apply to which commands (README.md:672) gets a check against the
    built CLI. It also says that `verify` takes no `api_*` option.

## Manual verification (main runs this with the user; not in CI)

The live full mirror holds the manifest lock in `~/.local/state/yandex-disk-rescue` and writes to `/mnt/oblako`.
Every step uses scratch `XDG_STATE_HOME`, `XDG_CONFIG_HOME` and a scratch DEST. No migration is added, but the
running build's lock would refuse a second run anyway.

1. In a real terminal, download a small real folder with this branch's build: watch the live view, resize the
   terminal, press Ctrl+C, rerun.
2. The same folder under `nohup … > out 2> err &` with `--progress-interval 10`: `err` holds timestamped status
   lines only, and `out` holds the report.
3. `verify` of that DEST in a terminal and under `nohup`.

## Discovered issues

- **Fix in this PR:**
  - Silent phases: the `lstat` check before the walk, the pause between passes, scan listing, verify (Stages 2–3).
  - Throttling, API retries and deferrals left no trace (Stage 1 logs them).
  - Quarantine and repair details were only available inside reason strings (Stage 4, Decision 8).
- **Filed as issues:** none. #15 (SIGTERM) and #18 (zero-fill) already cover what this study touched. This plan
  keeps both compatible (see Non-goals).

## Risks / open questions

- **Test churn.** Notices and final status lines change the exact `result.stderr` of about 30 tests. The `Log:`
  line changes about 10 exact `stdout ==` assertions (listed in Stage 1). Stages 1 and 2 update them to assert the
  result line plus the notices that should be there. They must not silence the output with `-q` to keep old
  assertions.
- **Stage 1 size.** It carries the output plumbing and every engine's log records. If the implementer finds it
  too large for one reviewable commit, the engine records (the level table) may be split into a Stage 1b right
  after it, with the acceptance-2 test moving there. No other stage depends on the split.
- **Two processes rotating one log.** Only one run holds the manifest lock, but a second run that fails to get it
  still logs its start and end. If that second run rotates the file, the running process keeps writing into `.1`
  until its own size check rotates again. No record is lost, but records can sit in a different backup than
  expected. Accepted rather than adding a lock to logging.
- **Log volume.** INFO is about one line per file. For 133,521 files that is about 30 MB, so 10 MiB × 5 holds about
  one full run plus the final summary. A run with tens of thousands of failures could rotate part of its own final
  summary into `.1`. The JSON report is the complete record in that case.
- **Notice flood.** A folder that fails makes every entry under it fail, and each is a WARNING on stderr. Bounded
  by the tree, so accepted. If it proves noisy, a follow-up can fold "under failed folder X" notices.
- **Status thread versus the event loop.** Snapshots are taken under a lock in the renderer thread. Updates are
  O(1) per chunk; at 100 MB/s with 64 KiB chunks that is about 1,500 updates/s, which is negligible. If profiling
  shows contention, the state can batch, and the contract does not change.
- **pty tests in CI.** The live-view tests need `/dev/ptmx`, which the devcontainer and the Dagger container
  provide. They fail rather than skip when it is missing, so CI never goes green without them.
- **`progress_interval` as a registry option** (Decision 4) adds a config key. The alternative, a hard-coded 60 s, is
  simpler but cannot be tuned for a slow scan or a short cron run.
