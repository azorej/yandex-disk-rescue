# Changelog

All notable changes to yandex-disk-rescue are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- SIGTERM (`kill`, `systemctl stop`, `docker stop`) stops `scan`, `download` and `verify` cleanly, as Ctrl+C does,
  with exit code 143.

### Changed

- `download` fetches files as one stream, without segments, when the partial folder's filesystem has no sparse files
  (HFS+, exFAT, FAT), instead of writing each segmented file's zeros first. The run says so in one line, and its JSON
  report has `sparse_partials`.
- A download that has not kept any confirmed byte is planned again with the current `--download-segments` (and the
  partial folder's sparse files) when it resumes.
- `download` starts the files that failed before only after all the others, fewest failures first, in every pass,
  so a restart reaches new data at once.
- A file whose transfer failed is recorded as failed at once, before its later passes, so a run stopped between
  passes still lists it for `download --retry-failed` and `verify`. The manifest counts each entry's failed turns
  (schema version 10).
- Every HTTP request names the tool in its `User-Agent`:
  `yandex-disk-rescue/<version> (+https://github.com/azorej/yandex-disk-rescue)`.

### Fixed

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
