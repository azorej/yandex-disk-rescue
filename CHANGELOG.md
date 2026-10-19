# Changelog

All notable changes to yandex-disk-rescue are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- SIGTERM (`kill`, `systemctl stop`, `docker stop`) stops `scan`, `download` and `verify` cleanly, as Ctrl+C does,
  with exit code 143.
- A file Yandex Disk sends no data for, on 7 attempts in a row over at least a day, is reported as unavailable on
  Yandex Disk (`Yandex Disk never started sending this file: …`, or `… stopped sending this file after <size>: …`) in
  the summary, with a hint to check it in the web interface and ask Yandex support, in `--json` (`unavailable`) and
  in `verify`. The manifest keeps each failure's streak of attempts without data (schema version 11).
- `download` saves Yandex's preview (a reduced image, at most 1280 px) of each file it could not download because of
  Yandex's side in `DEST/.yandex-disk-rescue/previews/`, at the end of a run whose transfers ended normally, and
  deletes it once the file is downloaded. The summary lists the files saved only as a preview and the previews not
  saved or deleted, as does `--json` (`preview_only`, `preview_errors`, `counts.previews_pending`); Ctrl+C or SIGTERM during the
  preview step stops only that step. The manifest records each saved preview (schema version 12).
- `--no-previews` (`previews = false`, `YANDEX_DISK_RESCUE_PREVIEWS=false`) turns preview fetching off; previews of
  files now downloaded are still deleted. `verify` names the saved preview of a file not downloaded (`; a preview is
  saved at <path>`, and `preview` in `--json`), and never reads anything in `previews/`.

### Changed

- A plain `download` tries a file unavailable on Yandex Disk once per run, at the end of pass 1, instead of on every
  pass, so `--download-passes 0` ends once only such files are left; `--retry-failed` still gives it every pass.
- `download` fetches files as one stream, without segments, when the partial folder's filesystem has no sparse files
  (HFS+, exFAT, FAT), instead of writing each segmented file's zeros first. The run says so in one line, and its JSON
  report has `sparse_partials`.
- A download that has not kept any confirmed byte is planned again with the current `--download-segments` (and the
  partial folder's sparse files) when it resumes.
- Re-hashing the confirmed bytes of resumed downloads, reading back segmented files and hashing local copies run for
  one file at a time per folder (the partial folder, DEST), so a restart on a hard disk reads sequentially; a
  `--partial-dir` on DEST's own disk still counts as a separate folder. A file waiting for its turn shows
  `waiting to hash`.
- `download` starts the files that failed before only after all the others, fewest failures first, in every pass,
  so a restart reaches new data at once.
- A file whose transfer failed is recorded as failed at once, before its later passes, so a run stopped between
  passes still lists it for `download --retry-failed` and `verify`. The manifest counts each entry's failed turns
  (schema version 10).
- Every HTTP request names the tool in its `User-Agent`:
  `yandex-disk-rescue/<version> (+https://github.com/azorej/yandex-disk-rescue)`.

### Fixed

- A folder whose listing the Disk API keeps failing no longer stops `scan` or `download`: while the API still lists
  other folders, the scan commits everything else, keeps what earlier scans recorded below that folder, lists it
  with the API's reason and exits 1; an outage still stops the scan. The manifest records such folders (schema
  version 13). `download` reports such a folder as failed and `verify` as not downloaded on every run until a scan
  lists it, a reused scan included, and `download` mirrors everything else, the files an earlier scan recorded in it
  included.
- `download` no longer stops, or fails the file, when it cannot read a partial file (a bad sector) while resuming,
  reading back, copying or moving it to a new `--partial-dir`: the partial file is emptied and the file downloaded
  again from its first byte, with a notice; a second read error on it in the same run fails that file. A partial
  folder that is gone still stops the run.
- A Disk API request the API keeps failing for one file (its download link, or the check of an unfinished download
  against the Disk) no longer stops `download`. While the API still gives other files' links, only that file waits
  for the next pass, keeping the bytes it has, fails with the API's status after the last pass, and gets its
  preview; such attempts do not count toward "unavailable on Yandex Disk". An outage, also one of the download-link
  endpoint alone, still stops the run.
- A `download` stopped by a destination error says what failed (`Could not read`, `Could not write` or
  `Could not inspect`), no longer `Could not write` for reads.
- `download` no longer stops at a local copy it cannot read (a bad sector): the copy is recorded corrupt, the file is
  downloaded again, and the copy is moved aside to `replaced/`; the summary lists such copies under
  `Unreadable in DEST, downloaded again`, and `--json` reports them as `unreadable`.
  Running out of file descriptors or memory while reading a copy fails only that file in `download` and stops
  `verify` with `Could not read <path>`; neither records the copy corrupt.
- An I/O error looking up a verified file at the start of `download` names that file, not DEST.

## [1.0.0rc1] - 2026-10-18

### Added

- `auth login`, `auth status` and `auth logout`: an OAuth token that can only read the Disk, checked with Yandex
  before it is saved to a `0600` file, or taken from `YANDEX_DISK_RESCUE_TOKEN`.
- `scan`: the remote tree of `disk:/` or one folder into a local manifest, with the changes since the last scan.
- `download [REMOTE_PATH] DEST`: a verified mirror of a Disk folder, keeping the folder structure, empty folders and
  modification times.
  - Resumable and crash-safe: an interrupted transfer continues from its last confirmed byte, and a rerun fetches
    only what is missing or changed.
  - Every file is checked against its cloud size, MD5 and SHA-256 before it is placed.
  - Parallel downloads, and segmented downloads of large files with checkpoints.
  - `--partial-dir` keeps partial files on a separate, faster folder.
  - A copy that fails the check is kept in quarantine, never deleted.
  - `--retry-failed` retries only the failed, quarantined, unfinished or broken files.
- `verify DEST`: an offline, resumable re-hash of a mirror against the manifest; the next `download` repairs what it
  finds.
- Progress: a live view on a terminal and status lines elsewhere.
- A rotating log file in the XDG state directory.
- `--json` reports of `scan`, `download` and `verify`.
- Adaptive pacing and back-off that honour the API's `429` and `Retry-After`.
- A config file (`config.toml`) and environment variables for every option.
- Documented exit codes: `0`, `1`, `2` and `130`.
