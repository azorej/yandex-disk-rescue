# Yandex Disk Rescue

A Linux command-line tool that downloads everything from a Yandex Disk
account to local storage. It is a headless analogue of the Windows app
[Yandex-Disk-Rescue](https://github.com/fefenke/Yandex-Disk-Rescue).

> **Status: 1.0, release candidate `1.0.0rc1`.** Authentication
> (`yandex-disk-rescue auth`), scanning the remote tree into a local
> manifest (`yandex-disk-rescue scan`),
> verified, parallel, resumable mirroring with segmented downloads of large
> files (`yandex-disk-rescue download`) and an offline, resumable integrity
> check of a mirror (`yandex-disk-rescue verify`) are complete, with
> progress, a log file, `--json` reports and documented exit codes. Changes
> are listed in the [changelog](CHANGELOG.md).

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
  SHA-256. Failures are reported, never silently skipped, and a copy that
  fails the check is kept in quarantine, never deleted. `verify` re-hashes a
  mirror later, offline, and the next `download` repairs what it finds.
- **CLI only.** Scriptable, [cron- and systemd-friendly](#running-unattended),
  no GUI.

This is an independent, unofficial tool. It is not affiliated with Yandex.

## Installation

The tool is on PyPI as
[`yandex-disk-rescue`](https://pypi.org/project/yandex-disk-rescue/). It
needs Linux and Python 3.14 or newer. Install it with
[uv](https://docs.astral.sh/uv/), which fetches Python 3.14 when the system
has none:

```bash
uv tool install yandex-disk-rescue
```

or with [pipx](https://pipx.pypa.io/), which needs a Python 3.14
interpreter:

```bash
pipx install yandex-disk-rescue
pipx install --python python3.14 yandex-disk-rescue   # when the default Python is older
```

While the only release on PyPI is the release candidate `1.0.0rc1`, both
install it by default.

Both put the command at `~/.local/bin/yandex-disk-rescue`, the path the
[systemd and cron examples](#running-unattended) use; make sure
`~/.local/bin` is on your `PATH` (`uv tool update-shell` or
`pipx ensurepath` adds it). Check the install:

```bash
yandex-disk-rescue --version
```

Upgrade with `uv tool upgrade yandex-disk-rescue` or
`pipx upgrade yandex-disk-rescue`. Uninstall with
`uv tool uninstall yandex-disk-rescue` or
`pipx uninstall yandex-disk-rescue`; that removes only the program. The
token and the config file (`~/.config/yandex-disk-rescue/`) and the
manifest and the log file (`~/.local/state/yandex-disk-rescue/`), or their
`$XDG_CONFIG_HOME` and `$XDG_STATE_HOME` equivalents, stay until you delete
them; `yandex-disk-rescue auth logout` deletes the token first.

## Quick start

```bash
uv tool install yandex-disk-rescue                # or: pipx install yandex-disk-rescue
yandex-disk-rescue auth login                     # open the printed URL, allow access, paste the token
yandex-disk-rescue download disk:/ /mnt/backup    # the whole Disk into /mnt/backup
yandex-disk-rescue download disk:/ /mnt/backup    # after an interruption (Ctrl+C, a crash): continues where it stopped
yandex-disk-rescue verify /mnt/backup             # re-hash the mirror offline
```

[Authentication](#authentication), [Downloading](#downloading) and
[Verifying a mirror](#verifying-a-mirror) describe each step in full.

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
systemd `EnvironmentFile=`) instead of running `auth login`. The rest of the
setup is in [Running unattended](#running-unattended).

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
Log: /home/user/.local/state/yandex-disk-rescue/yandex-disk-rescue.log
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
interrupted (Ctrl+C, SIGTERM, SIGKILL, a crash) leaves the previous scan in
place, and the next run scans again from the start. Ctrl+C or SIGTERM
(`kill`, `systemctl stop`, `docker stop`) prints `Interrupted; the manifest
holds the last completed scan.` and exits with 130 (Ctrl+C) or 143
(SIGTERM). Scanning a folder replaces only that folder's part of the
manifest. The manifest also records when each scan completed, and the
entries left out because their names are not valid Unicode. Only one run
can use the manifest at a time; a second one fails with `Another
yandex-disk-rescue run is using ...`.
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
once, started in the order below: the folder structure is kept, each folder
is created before anything inside it, empty folders are created, and every
file gets the modification time it has on the Disk. It takes the `--api-*`
options, `--download-timeout`, `--download-retries`, `--download-passes`,
`--download-connections`, `--download-checkpoint-mib`, `--download-segments`,
`--download-segment-threshold-mib`, `--partial-dir`, `--scan-max-age` and
`--previews/--no-previews` (see [Configuration](#configuration)), `--no-scan`
and `--retry-failed` (below). A file whose download host never answers holds one connection until
it stalls and delays no other file.

**Order.** Files with no failure record start first, in path order. Files that
failed before start after all of them, fewest failed turns first, ties in path
order, so a restart reaches new and healthy data at once. A file
[unavailable on Yandex Disk](#downloading) has failed more turns than most, so
it comes near the end of pass 1, and a plain run gives it no later pass. Each
later pass follows the same rule over the files still to download. A partial
download keeps its confirmed bytes whenever its turn comes, and folders are still made
before anything inside them. With `--partial-dir`, a file that fits only once
the kept partial file of an earlier failed download is gone may meet `no room
yet` in pass 1 and get the room in the next pass; with `--download-passes 1`
it fails with that reason, and the next run or `--retry-failed` fetches it.

**Segmented files.** A file larger than `download_segment_threshold_mib` MiB
(default 256) is downloaded as `download_segments` segments (default 4): equal
byte ranges fetched in parallel, each with an HTTP `Range` request on a
connection of its own, all into one partial file that is first extended to the
file's full size. Once every segment is in, the whole file is read back and
checked against its size and checksums (with `--partial-dir`, the copy to
DEST is the check; below). A file at or below the threshold, and every file
when `download_segments` is `1`, is fetched as one stream and checked as it
arrives. Segments need a partial folder whose filesystem keeps sparse files,
which `download` checks when it starts (the log file says `Partial folder …
keeps sparse files: yes` or `no`). On a filesystem without them (HFS+,
exFAT, FAT), extending a file writes its zeros first, so there every new
download is fetched as one stream, whatever `download_segments` says, and
the run says so:

```
Partial files in /mnt/usb/.yandex-disk-rescue/partial cannot be sparse: new downloads are fetched as one stream each, not as segments.
```

A download resumed by a later pass or a later run keeps its segments while
any of them keeps confirmed bytes, even when the options changed since. A
download that has not kept any confirmed byte is planned again, as a new one
is, with the current `download_segments` and partial folder.

**Partial files.** A file being downloaded lives in a partial file, named by
the file's id in the manifest. By default partial files are in
`DEST/.yandex-disk-rescue/partial/`, and a verified partial file is itself
moved to its final path. With `--partial-dir PATH` (`partial_dir` in the
config file) they are kept below `PATH` instead, so all of their disk work —
the writes of many files at once, the flush of every checkpoint, extending a
segmented file to its full size (whether files are segmented then depends
on `PATH`'s filesystem, not DEST's), and re-reading the confirmed bytes on a
resume — happens on that disk, not on DEST:

- `PATH` must be an existing folder outside DEST, given as an absolute path;
  anything else is a usage error (exit `2`). The tool creates only its own
  folder of this DEST below it, `PATH/<key>/`, where `<key>` is the first 16
  hex digits of the SHA-256 of DEST's resolved path. So one `partial_dir` in
  the config file serves every destination. That folder holds only partial
  files.
- Once every byte of a file is in, it is copied to
  `DEST/.yandex-disk-rescue/partial/<id>.copy` as one sequential stream, one
  file at a time for the whole run, so DEST sees a single writer. The size
  and checksums are checked over the bytes as they are copied, so DEST is
  never read back. The copy gets the Disk modification time, is flushed to
  the disk, and is then placed like any verified file, or quarantined in
  DEST's `quarantine/` (below) when the check fails. The partial file below
  `PATH` is deleted once that is recorded.
- A run stopped anywhere on the way (Ctrl+C, SIGTERM, SIGKILL, a crash, a
  power cut) never leaves a cut-short file at a final path, and fetches
  nothing of that file again. Until the outcome is recorded, the partial
  file below `PATH` and its journal stay as they are. Stopped before the
  copy is placed, it leaves a staged copy, which the next run deletes at its
  start and then copies the file again. Stopped once the copy is placed but
  before it is recorded, it leaves the file at its final path, which the
  next run hashes, adopts as matching the Disk, and then deletes the partial
  file. Stopped after the record, it leaves only the partial file, which the
  next run deletes at its start.
- A file waiting for its copy keeps its place among the
  `download_connections` files in progress, so when DEST is slower than the
  network, fetching slows down instead of filling `PATH`.
- The partial files never take more than the room `PATH` had when the run
  started: the free space of its filesystem, plus what the partial files
  already there take, minus a reserve of 1 GiB left for everything else. Once
  any move (below) is done, the run prints `Partial files in PATH/<key>
  (<size> available)`. A file being downloaded counts its full size; a partial
  file kept for a later pass or run counts the disk space it actually takes,
  which for a segmented file is only what was written so far, until it is
  deleted (also when its file is found already in DEST and adopted). Files
  take their room in the order they start, a file that does not fit yet waits
  for one in progress to end, and when none is left in progress it is deferred
  to the next pass with `no room yet in the partial folder <folder> (<size>
  needed, <size> available)`. When a pass starts no download and frees no
  room, and every file it defers waits for room, the partial files kept there
  can never make room for each other: those files fail at once, whatever
  `--download-passes` says, with `no room in the partial folder <folder> next
  to the partial files kept there (<size> needed, <size> available); free
  space there, then run again or with --retry-failed`. A file larger than the
  whole room fails with `larger than the room in the partial folder <folder>
  (<size> needed, <size> available)`; `--retry-failed` fetches it once there
  is room. When another program fills that disk anyway, the run stops with
  `Error: Could not write <path>: No space left on device.` (below) and a
  rerun resumes from the journals.
- Use a persistent folder on a fast local disk: not a tmpfs, and not a
  container layer that a rebuild discards. When the folder is lost, the
  downloads in progress start again from their first byte. Do not share one
  `PATH` between machines.
- The manifest records which folder the partial files of each DEST are in.
  When a run's folder differs from it — the option turned on or off, or
  `PATH` changed — the run first moves the downloads in progress there,
  printing `Moving the confirmed bytes (<size>) of N downloads in progress
  from <old> to <new>` and `Moving:` status lines (see
  [Output and logs](#output-and-logs)). Only the bytes the journals confirm are copied, one
  file at a time; the rest (bytes past the last checkpoint, a segmented
  file's zero-fill) is never read and is fetched again. Each copy is flushed
  to the disk before its old file is deleted, and the old folder is deleted
  at the end (for DEST's own `partial/`, only the files in it). A run stopped
  during the move goes on with it on the next run, losing no confirmed byte.
  A confirmed range missing from an old file cut short (by hand, or by a
  power cut on a disk that lost the end of a write) is fetched again. When
  the recorded folder is gone and downloads were in progress, the run prints
  `The partial folder of the last run, <old>, is missing; its downloads in
  progress start again from their first byte.`, and they do. The recorded
  folder reached under another path (a bind mount, a link on the way) is
  the same folder: nothing is moved, and the new path is recorded.
- Remounting DEST at another path makes it a new mirror with a new `<key>`;
  the old `PATH/<key>/` can then be deleted.

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
changed since then fails its download, or is quarantined when its size did
not change (the bytes no longer match the recorded size and checksums), and
is never kept as verified; rerun with `--scan-max-age 0` to scan first.
A file quarantined that way is quarantined again by a `--retry-failed` that
reuses the same scan, so add `--scan-max-age 0` to that retry. `scan` always
scans.

**Verified.** A file reaches its final path only after its size and every
checksum the Disk has for it (MD5 and SHA-256) match the downloaded bytes.
Until then it is written to the partial folder (`DEST/.yandex-disk-rescue/partial/`,
or the one below `--partial-dir`, where the check runs over the bytes copied
to DEST). A file with
neither checksum on the Disk can only be checked by its size; the summary
lists such files. Placing a file never replaces anything at its final path.

**Quarantine.** A downloaded file that fails the check, whether fetched as
one stream or as segments, is never deleted: it gets the Disk modification
time and moves, with its relative path kept, to
`DEST/.yandex-disk-rescue/quarantine/<run start, UTC, YYYY-MM-DDTHH-MM-SSZ>/`
(one folder per run that quarantines something, with the same `-2`, `-3`, …
suffix rule as `replaced/`; a name the filesystem refuses gets its
[converted form](#local-file-names)). It may be the only copy of your data:
when the Disk's checksum is the wrong part, its bytes are right. Nothing at
the file's final path is touched, so an older local copy there stays. The
manifest records the copy for that version of the Disk file (size,
checksums, modification time), and a plain `download` does not fetch the
file again while the Disk still has that version: it is not looked at, not
counted in `Downloading N files`, and listed under `Quarantined` with its
reason and the copy's path, so the run exits `1`. `download --retry-failed`
downloads it again (below); a rescan that finds a new version on the Disk
lets a plain `download` fetch that version as usual. Once the file is
verified the record is dropped; a retry that fails the check again adds
another copy under that run's folder.

**Reruns.** A file verified by an earlier run is skipped without being read
again when its Disk metadata is unchanged and the local file still has the
recorded size, modification time and inode. A file changed on the Disk, or
edited or touched locally, is handled like any file already in DEST (below).
A file [`verify`](#verifying-a-mirror) found missing, modified or corrupt, or
whose copy `download` could not read (below), is downloaded again whatever
its record says, and its copy in DEST is never read again (it may be
unreadable): once the new file is verified, that copy is moved
aside to `replaced/` (below) and the finding is dropped. A repair that fails,
is interrupted or is quarantined keeps the finding, so every later run treats
the file the same way until it is verified. `download --retry-failed`
(below) repairs exactly those files without walking the rest of the mirror.

**Interruptions, stalls and retries.** Every download keeps a checkpoint
journal in the manifest: for each segment (a file fetched as one stream has
one), how many of its bytes were flushed to the disk, and their SHA-256. A
checkpoint is taken every `download_checkpoint_mib` MiB a segment receives
(default 32), every 15 s while bytes arrive, and whenever a download attempt
ends — a dropped connection, a stall, Ctrl+C or SIGTERM. After SIGKILL, a
crash or a power cut, at most one checkpoint interval per segment is fetched
again.

A later pass or a later run asks the Disk for the file again and, when its
size, checksums and modification time are unchanged, re-reads each segment's
confirmed bytes and checks them against their SHA-256, one file of the run at
a time per folder (the partial folder, DEST), so a restart on a hard disk
reads sequentially; a partial folder on DEST's own disk still counts as a
separate folder. When they match, only the rest of the segment is fetched,
with an HTTP `Range` request; bytes past the last checkpoint are fetched
again, never trusted. A segment whose confirmed bytes changed on disk, or that a partial file deleted or cut short no longer
holds, starts again from its first byte; the other segments go on from their
checkpoints. When the file changed in the cloud, the partial file is deleted
and the file fails with `changed in the cloud since the scan; rerun with
--scan-max-age 0 to fetch the new version` (after a rescan, the new version
is downloaded from the start). A partial file left for an older version is
discarded and the file is downloaded from the start; when the download host
ignores `Range` for any segment, the whole file is fetched again as one
stream, never appended to the bytes kept.

A partial file whose bytes cannot be read (an I/O error, a bad sector) while
its folder still can is emptied, and the file is downloaded again from its
first byte, with a notice (`<path>: could not read its partial file <file>:
<reason>; downloading it again from its first byte`); the other downloads go
on from their checkpoints. This covers re-reading confirmed bytes, reading
back a segmented file, and copying a finished file to DEST with
`--partial-dir`; after the last two the file waits for the next pass. It
happens at most once per file per run: a second read error on the same
partial file fails the file with `… (a second time in this run)`, a local
failure that gets no preview, and so does a file whose partial file could not
be read once it arrived when no pass is left. A partial folder that is gone,
a permission error, or running out of file descriptors or memory stops the
run with `Error: Could not read <path>: <reason>.`

- **Stalls.** A download fails as stalled (`the download stalled: no data for
  15 s`) when the download host sends nothing for `download_timeout` seconds,
  whether it is slow to answer or pauses mid-file. Only silence counts: a
  slow but steady download never stalls, and time spent writing to DEST or
  waiting for a free connection does not count.
- **Retries.** A segment whose download fails after receiving new data (a cut
  connection, a stall after some data) is resumed at once from where it
  stopped, up to `download_retries` times; past that its last reason gets
  `(after N attempts)`. A download that fails before any new data arrives (a
  stall before the first byte, a refused connection, `408`, `429` or `5xx`)
  is not retried at once.
- **Passes.** Files whose download failed that way, or ran out of retries,
  and files whose download link or resume check the Disk API kept failing to
  answer (see "Failures that stop the run"), are resumed in another pass over the files
  still to download, again up to `download_connections` at once, each with a
  new download link, after a 5 s pause (longer when the host sent
  `Retry-After`, up to 300 s). The other
  segments of such a file finish first, and the next pass fetches only the
  segments still missing. A run makes at most `download_passes` passes
  (`0`: until every file is done or unavailable on Yandex Disk, or Ctrl+C); a
  file still failing after the last one is reported with its last reason and
  `(after N passes)`, or as unavailable on Yandex Disk (below). Once a file
  needed another pass, the summary shows `Passes: <used> of <limit>`. A
  checksum mismatch quarantines the file at once, and a refused download, a
  file changed in the cloud and a local write error fail it at once, stopping
  its other segments; no later pass retries it.
- **Unavailable on Yandex Disk.** A file whose download host sent no byte of
  it on its last 7 attempts in a row, the first of them at least a day ago, is
  reported under `Failed` as `Yandex Disk never started sending this file: no
  data in its last N attempts since <YYYY-MM-DD HH:MM> UTC, each with a new
  download link (the last: <reason>)`, or, when part of it is confirmed on
  disk, as `Yandex Disk stopped sending this file after <size>: …`. The
  attempts count across passes and runs. A `429` or any answer with
  `Retry-After` is throttling, which neither counts nor starts over; an
  attempt whose download link or resume check the Disk API failed to answer,
  like throttling, neither counts nor starts over; a byte
  received, or a failure for any other reason (a refused download, a local
  error), starts the count over. The summary then adds, after the `Failed`
  list, `N of them never got data from Yandex Disk for a day or more: open
  them in the Yandex Disk web interface, and ask Yandex support about those
  that do not open there either.` A plain `download` gives such a file one
  attempt per run, in pass 1, and fails it at once when the host sends
  nothing again, without `(after N passes)`; an attempt the host throttles
  waits for the next pass as usual. A byte received makes it an ordinary file
  again, so a file Yandex restores is fetched by the next run.
  `--retry-failed` gives it every pass.
- **Expired links.** The segments of a file share one download link per pass,
  requested only once a segment has a free connection, so no link ages while
  it waits for one. A link the host rejects (`401`, `403` or `410`) is
  replaced by a new one once per pass for the whole file, and the download
  continues from the bytes received; when the new link is rejected too, the
  file waits for the next pass, which starts with a new link; after the last
  pass its reason is `download link rejected again after a refresh (HTTP <n>)`.

**Retrying failures.** Every file or folder a run reports under `Failed` is
recorded in the manifest for that DEST, as soon as a turn of it fails: a
refused download, a file changed or removed in the cloud, a local error, a
hard link to another file of the mirror, a refused name, a folder that could
not be made, every entry under such a folder, a transient transfer failure
that waits for a later pass, and a download link or resume check the Disk API
kept failing to answer. The record holds the reason last shown, the
count of failed turns since the entry last succeeded, and how many of the
last of them in a row got no byte from the download host, since when (what
makes a file [unavailable on Yandex Disk](#downloading)); a file still failing
after the last pass keeps the counts and gets the reason it is reported with.
The record is dropped once the entry succeeds: a file verified or adopted, a
folder made or found in place. A turn stopped by Ctrl+C, SIGTERM or an error
that stops the run records nothing: the file keeps any earlier record, and its
unfinished download stays recorded as partial.

A quarantined file gets no failure record and is not listed under `Failed`.
When a retry of a quarantined file fails (a stall, say), the failure is
recorded too, but the file stays listed under `Quarantined` only, its line
ending with `; last retry: <reason>`.

`download --retry-failed [REMOTE_PATH] DEST` covers only the *retry set*: the
files with a failure record, a quarantine record, a download that started
and never finished (for example one stopped by Ctrl+C) or a finding — one
[`verify`](#verifying-a-mirror) made (missing, modified or corrupt), or a
copy in DEST that `download` could not read — and the folders with a failure
record. Files never started are not in it; a
plain `download` gets them. The scan step runs as usual; the retry set is
fixed once it is done. The run then visits only the retry set and the
folders above its members, in the [order](#downloading) of every run, with
the usual passes and rules,
and never looks at any other path of DEST: those files are neither checked
nor counted. It starts with

```
Retrying 3 failed, quarantined, unfinished or broken files (1.5 GiB) in /mnt/backup
```

in place of `Downloading …`, and its summary and exit code are those of a
normal run over the retry set. With nothing to retry it prints `Retrying 0
failed, quarantined, unfinished or broken files (0 B)`, downloads nothing
and exits `0`. Entries whose names are not valid Unicode have no record;
they fail every run, `--retry-failed` included.

**Previews of files that cannot be downloaded.** For a photo, a video or a
PDF that Yandex Disk will not send, Yandex often still serves a reduced
image, and a rescue is better with it than with nothing. At the end of every
run whose transfers ended normally, plain and `--retry-failed` alike, each
file that failed on Yandex's side gets the largest preview Yandex offers of
it: a file out of passes after transient transfer failures, a file
[unavailable on Yandex Disk](#downloading), a download the Disk refuses, and
a file the download host refuses or sends wrong. Files that failed for a
local cause (a permission error, a refused name, a folder that could not be
made and everything below it, no room in the partial folder, a hard link to
another file, a file changed or removed on the Disk) and quarantined files,
which have a full-size copy in `quarantine/`, get none. The run prints
`Saving the previews of N failed files to DEST/.yandex-disk-rescue/previews`
first. `--no-previews` (`previews = false`, `YANDEX_DISK_RESCUE_PREVIEWS=false`)
turns fetching off: no preview request is made and the headline is not
printed, while the previews already saved for a file's current version are
still listed, and those of files now verified are still deleted.

- The preview is saved at the file's local path below
  `DEST/.yandex-disk-rescue/previews/`, named `<local name>.preview<ext>`
  (`napkin.jpg` gives `napkin.jpg.preview.jpg`), shortened to fit like a
  long name; the extension comes from the image's type (`.jpg`, `.png`,
  `.webp`, `.gif`, `.heic`, else `.img`). It is never at a mirror path.
- It is a reduced image, Yandex's `XXXL` size (at most 1280 px on its longer
  side), not the original, and never verified: Yandex publishes no checksum
  of it. The manifest records its path, size, type, the size asked for and
  the version of the file it was taken for. Images, videos (a JPEG frame)
  and PDFs (a PNG of the first page) have one; archives and most other
  files do not. A file with no preview is asked again on every run.
- A preview already saved for the file's current version is not fetched
  again; the summary lists it all the same. When a newer version of the
  file fails too, its preview replaces the older version's.
- Once the original is downloaded and verified, its preview and its record
  are deleted, so `previews/` holds exactly the files you have only as a
  preview. A preview a crash left half-written (`*.preview-saving`) is
  deleted at the start of the next run's preview step. A preview of a file later removed from the Disk stays in
  `previews/`, untracked, until you delete it.
- A preview that cannot be fetched, saved or deleted is listed under
  `Previews not saved or deleted` with the reason. A token Yandex rejects,
  or a full disk, an I/O error, a read-only filesystem or a permission error
  in `previews/` (`could not use previews in <folder>: <reason>`), ends the
  preview step, and every file not handled yet is listed with that reason. A
  preview of a verified file that cannot be deleted keeps its record, so the
  next run deletes it. Previews never change the run's outcome or exit code,
  and never stop the run, except when the manifest itself cannot be written,
  which stops it as in any other phase.
- Ctrl+C or SIGTERM during the preview step stops only that step: the
  fetches in flight are dropped, a preview being written is finished and
  recorded first, the summary is printed with `Previews interrupted: P of N
  files not handled; the next run saves them.`, and the exit code is the
  transfers' (`1` when files failed), not 130 or 143. A second Ctrl+C stops
  the tool at once. An interrupt or an error that stops the transfers
  fetches no preview at all.
- The preview host requires the OAuth token. The tool sends it there only
  over https to `downloader.disk.yandex.ru` (and to the API itself), and
  follows no redirect from it; a preview link anywhere else is refused
  without a request, as a preview error naming the host.

**Summary.**

```
Downloading 120 files (1.2 GiB) to /mnt/backup/Photos
Saving the previews of 2 failed files to /mnt/backup/Photos/.yandex-disk-rescue/previews
Mirrored disk:/Photos to /mnt/backup/Photos: 120 downloaded (1.2 GiB), 1000 skipped, 1 quarantined, 3 failed
Passes: 2 of 7
Already on disk and verified: 40
  disk:/Photos/2023/beach.jpg
  …
Verified by size only (no cloud checksum): 1
  disk:/Photos/old.bmp
Renamed for the destination: 1
  disk:/Photos/a.txt -> a~1a2b3c4d.txt
Unreadable in DEST, downloaded again: 1
  disk:/Photos/old.jpg: could not read old.jpg: Input/output error
Moved aside to .yandex-disk-rescue/replaced/2026-10-17T09-30-00Z/: 2
  notes.txt
  old.jpg
Quarantined: 1
  disk:/Photos/bad.jpg: MD5 mismatch (expected …, got …); kept at .yandex-disk-rescue/quarantine/2026-10-17T09-30-00Z/bad.jpg
Download them again with --retry-failed (add --scan-max-age 0 if they changed on the Disk since the scan).
Failed: 3
  disk:/Photos/gone.jpg: removed from the Disk after the scan
  disk:/Photos/napkin.jpg: Yandex Disk never started sending this file: no data in its last 8 attempts since 2026-10-16 08:12 UTC, each with a new download link (the last: the download stalled: no data for 15 s)
  disk:/Photos/scan.pdf: the download host s123klg.storage.yandex.net returned HTTP 500 (after 7 passes)
1 of them never got data from Yandex Disk for a day or more: open them in the Yandex Disk web interface, and ask Yandex support about those that do not open there either.
Saved only as a preview (a reduced image, not verified): 1
  disk:/Photos/napkin.jpg -> .yandex-disk-rescue/previews/napkin.jpg.preview.jpg
Previews not saved or deleted: 1
  disk:/Photos/scan.pdf: Could not fetch the preview: the preview host returned HTTP 500 (after 6 attempts)
Manifest: /home/user/.local/state/yandex-disk-rescue/manifest.sqlite3
Log: /home/user/.local/state/yandex-disk-rescue/yandex-disk-rescue.log
```

Every list is in path order, whatever the order the files finished in, and
shows at most 20 entries, then `… and N more`. `Unreadable in DEST,
downloaded again` lists each copy already in DEST that could not be read
(see "Files already in DEST"); it does not change the exit code. `Saved only
as a preview` lists the failed files whose preview is saved, and `Previews not
saved or deleted` those whose preview could not be saved or deleted (see
"Previews of files that cannot be downloaded"); neither changes the exit
code. When anything failed or is quarantined, the run ends with exit code `1` and
`Error: F failed; see the list above.`, `Error: Q quarantined; see the list
above.` or `Error: F failed and Q quarantined; see the lists above.` A plain
rerun retries every file that is not verified yet except the quarantined
ones, and `download --retry-failed` only the failed, quarantined, unfinished
and broken ones.

**Files already in DEST.** DEST may already hold files, for example an
earlier copy made by hand. A regular file at the path of a Disk file is read
and hashed; when its size and every Disk checksum match, it is *adopted*: it
is not downloaded, it gets the Disk modification time and it counts as
skipped, listed under `Already on disk and verified`. (A file the Disk has no
checksum for is adopted when its size matches.) A file that cannot be read
(an I/O error, a bad sector) is treated like a file `verify` found corrupt:
it is recorded `corrupt` in the manifest with the read error, shown as a
notice (`<path> corrupt: could not read <local>: <reason>`), never read again
— not by a later pass, a later run or `--retry-failed` — and moved aside once
the Disk file is downloaded and verified; nothing in it is written. The
summary lists it under `Unreadable in DEST, downloaded again`, as a sign that
the disk may be failing, and the run still exits `0` when everything is
mirrored. A repair that fails keeps the finding, so `verify` lists the copy
under `Corrupt` until a later run repairs it. A permission error on the file
fails only that file (see "The destination"), and so does the tool running
out of file descriptors or memory while opening or reading it (`could not
read <local>: <reason>`, such as `Too many open files`): neither says anything
about the copy, so nothing is recorded and the next run reads it again. A DEST
that can no longer be read at all (a disk unplugged, a device gone) stops the
run with
`Error: Could not read <path>: <reason>.` Anything else at a path the
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
`DEST/.yandex-disk-rescue/partial/`, `DEST/.yandex-disk-rescue/previews/` and,
with `--partial-dir PATH`, DEST's folder `PATH/<key>/`, or the `<key>/` folder
below the `PATH` an earlier run used. In `previews/` it deletes a file's
preview once the file is verified, with the folders that left empty (never
`previews/` itself), and replaces it with the preview of a newer version of
the same file that failed too, which may have been the last trace of the
older version. At the start of every preview step it deletes the regular
`*.preview-saving` files below `previews/`, the previews a crash left
half-written (folders and links of that name are left alone). At the start
of every run it deletes the staged copies (`<id>.copy` files) in
`DEST/.yandex-disk-rescue/partial/`, and in the partial folder of the run every file that is not the partial file of a download in
progress (folders and links there are left alone). After moving the
downloads in progress out of an earlier run's partial folder, it deletes the
files left in it, then the folder itself when it is empty (DEST's own
`partial/` stays). A partial file that cannot
be deleted once its file's outcome is recorded (a folder made there under its
name, say) is listed under `Partial files not deleted` with the reason; the
file still counts as verified or quarantined. Moved-aside and quarantined copies
stay in `replaced/` and `quarantine/` until you remove them, after checking
them. A quarantined copy whose file later verified, or whose record a new
Disk version replaced, is no longer listed anywhere; browse the
`quarantine/<run start>/` folders to find it. Files and folders that are not on the
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
work directory: it holds partial files (or, with `--partial-dir`, the copies
of finished ones), the `replaced/`, `quarantine/` and `previews/`
folders and a lock, so only one run at a time can use a destination
(`Another yandex-disk-rescue run is using DEST`), even runs with different
manifests. The destination's filesystem must support
renaming without replacing (`RENAME_NOREPLACE`; ext4, xfs, btrfs, tmpfs and
HFS+ do); the run stops at the start otherwise. A full disk or quota, an I/O
error or a read-only filesystem while writing stops the run with
`Error: Could not write <path>: <reason>.`, and so does a permission error
while writing inside `DEST/.yandex-disk-rescue/` or the partial folder below
`--partial-dir`. An I/O error while reading the work directory, or a partial
file whose folder is gone, stops it with `Error: Could not read <path>:
<reason>.`, and one while looking up a path there or in DEST (its size, type
or free space) with `Error: Could not inspect <path>: <reason>.` An I/O error
while reading a partial file whose folder is still readable does not stop the
run; see "Interruptions, stalls and retries". Nor does one while reading a file
already in DEST; see "Files already in DEST". A permission error on
a path of the mirror — a file or folder there owned by another user, or
without the needed read, write or search permission — fails only the entries
it affects, with `could not read <path>: <reason>`, `could not inspect <path>:
<reason>`, `could not write <path>: <reason>` or `could not set the
modification time of <path>: <reason>`, and leaves the path as it was.

**Failures that stop the run.** A token Yandex rejects, and a Disk API error
that persists past the API retries (an outage), stop the run: the files still
in progress stop too, only the first error is shown, files already verified
stay verified, and a rerun picks up where it stopped. A Disk API request for
one file (its download link, or the check of an unfinished download against
the Disk) that keeps failing is told apart from an outage first: the
tool asks the API for the download link of another file of the mirror that got
one, the last one in this run, or else a verified one (of this mirror, or else
of another mirror of the same account). When there is no such file (no other
file got a link in this run, and no file of the account is verified), the tool
asks for the account (`GET /v1/disk/`) instead. When that
answers, only the file's turn fails: it waits for the next pass with the API's
status as its reason, an unfinished download keeping the bytes it has, and the
run goes on. When it does not answer, the run
stops as an outage, with the file's error. The run also stops at the 11th such
file of a run with no such file to ask about, its error ending with
`(no file of this mirror got a download link: 10 failed the same way)`.
Ctrl+C or SIGTERM
(`kill`, `systemctl stop`, `docker stop`) prints `Interrupted; rerun to
continue the download.` and exits with 130 (Ctrl+C) or 143 (SIGTERM); every
file in progress takes a checkpoint first, and the next run resumes it from
there. During the preview step at the end of a run, Ctrl+C or SIGTERM stops
only that step (see "Previews of files that cannot be downloaded").

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
so the files downloading at once (one connection each, or one per segment of
a segmented file; the segments queue for the same connections). Link requests
and look-ups go to the Disk API and are paced separately, by
`api_interval_ms` and `api_concurrency` (see [Rate limiting](#rate-limiting)).

Many files written at once make a hard disk seek between them. When DEST is a
hard disk (a USB one especially), set `partial_dir` to a folder on a fast
local disk with free space for at least the largest file plus 1 GiB
([Partial files](#downloading)): the interleaved writes of the
files in progress, the checkpoint flushes, extending segmented files, and the
re-reading of confirmed bytes on a resume all move there, and DEST only
receives one sequential copy of each finished file. Bytes already on disk are
hashed one file at a time per folder (the partial folder, DEST): the
confirmed bytes of a resumed download, a segmented file read back, and a
local copy before it is adopted, so the hashing reads sequentially. A partial
folder on DEST's own disk still counts as a separate folder.

Segments speed up one large file on a fast link whose single connection is the
limit; a folder of small files gains nothing from them, since its files
already download in parallel. They need a partial folder with sparse files:
on HFS+, exFAT or FAT, `download` fetches each new download as one stream by
itself, so its partial file is only appended to. To get segments with such a
DEST, set `partial_dir` to a folder on a filesystem with sparse files, such as
ext4, XFS or Btrfs. The defaults (4 segments above 256 MiB) are those
of the Windows [Yandex-Disk-Rescue](https://github.com/fefenke/Yandex-Disk-Rescue).

A smaller `download_checkpoint_mib` means less fetched again after a crash or
a power cut, at the cost of more flushes to the disk and more manifest
writes.

## Verifying a mirror

```bash
yandex-disk-rescue verify /mnt/backup
```

`verify DEST` checks that the files `download` verified in DEST are still
intact. It reads every such file in full and compares its size, then each
checksum the Disk had for it (MD5 and SHA-256), with the manifest. It needs no
token and makes no network request, and it creates, modifies, moves and
deletes nothing in DEST, so it works on a read-only mount; reading a file may
update its access time, as any read does, unless DEST is mounted `ro` or
`noatime`. It checks the files in path order, one at a time, which suits a USB
hard disk. Since it reads every byte, 2 TiB on a USB hard disk takes hours.

Each file of the mirror, as of the last scan, is one of:

- **Intact:** the size and every checksum match.
- **Missing:** nothing is at its path, or a folder on the way is missing or is
  a link.
- **Modified:** something other than a regular file is at its path (`… is no
  longer a regular file`; a link is never followed), or the file was changed
  since it was downloaded (another size, modification time or inode) and its
  content no longer matches.
- **Corrupt:** the file has the recorded size, modification time and inode,
  but its content does not match (silent damage such as bit rot), or it cannot
  be read (`could not read <path>: <reason>`); or a copy `download` found at
  its path could not be read (verified or not), and no repair has replaced it
  yet.
- **Quarantined:** its last download [failed the check](#downloading) and is
  kept in `quarantine/`; listed with the reason and the copy's path.
- **Not downloaded:** no verified copy of the current Disk version: never
  downloaded, failed (with the reason its last failed turn was reported with,
  such as `Yandex Disk never started sending this file: …`), unfinished,
  changed on the Disk since it was downloaded, or a name that is not valid
  Unicode. When a [preview](#downloading) of the current version is saved,
  the line ends with `; a preview is saved at <path>`, the path the manifest
  records.

A file with a finding (missing, modified or corrupt) is listed under its
finding even when a later download of it was quarantined: the finding
describes the copy at its path, and the line ends with `; repair
quarantined: <quarantine reason>; kept at <copy>`.

Only files with a verified copy of their current Disk version are read; the
others are listed from the manifest. A file found missing, modified or
corrupt is recorded in the manifest at once, and the next `download` (a
plain one, `--no-scan` or `--retry-failed`) downloads it again without
reading the bad copy and moves that copy aside to `replaced/`. `download`
records a copy it could not read [the same way](#downloading), so `verify`
lists it under `Corrupt` until it is repaired. A file `verify` finds intact
again drops its finding. Paths of DEST with no Disk
counterpart (local-only files, `.DS_Store`, …) are never opened, listed or
reported, and folders are not checked. Nothing below
`.yandex-disk-rescue/previews/` is opened, checked or reported either: a
preview is named from the manifest alone.

A read error usually means that one file is damaged, and the file is reported
as corrupt. When DEST itself cannot be read any more, such as a disk unplugged
mid-check, `verify` stops instead of reporting every remaining file:
`Error: Could not read DEST: <reason>; verify stopped. Rerun it once the disk
is back.` (exit `1`); what it found before then stays recorded. When the tool
runs out of file descriptors or memory while opening or reading a file, which
says nothing about the file, `verify` stops with `Error: Could not read
<path>: <reason>.` (exit `1`) and records nothing for that file; a rerun
continues from it.

```
Verifying 1120 files (1.2 GiB) in /mnt/backup/Photos
Verified /mnt/backup/Photos (disk:/Photos): 1115 intact, 1 missing, 1 modified, 1 corrupt, 1 quarantined, 2 not downloaded
Checked by size only (no cloud checksum): 1
  disk:/Photos/old.bmp
Missing: 1
  disk:/Photos/2023/gone.jpg: 2023/gone.jpg is missing
Modified: 1
  disk:/Photos/notes.txt: size mismatch (expected 120 bytes, got 98)
Corrupt: 1
  disk:/Photos/2024/beach.jpg: MD5 mismatch (expected …, got …)
Quarantined: 1
  disk:/Photos/bad.jpg: MD5 mismatch (expected …, got …); kept at .yandex-disk-rescue/quarantine/2026-10-17T09-30-00Z/bad.jpg
Not downloaded: 2
  disk:/Photos/napkin.jpg: the download host s123klg.storage.yandex.net returned HTTP 500; a preview is saved at .yandex-disk-rescue/previews/napkin.jpg.preview.jpg
  disk:/Photos/new.jpg: not downloaded yet
Manifest: /home/user/.local/state/yandex-disk-rescue/manifest.sqlite3
Log: /home/user/.local/state/yandex-disk-rescue/yandex-disk-rescue.log
```

Every list is in path order and shows at most 20 entries, then `… and N
more`. When any file is missing, modified, corrupt, quarantined or not
downloaded, `verify` ends with exit code `1` and `Error: N problems found; see
the lists above.`; on a complete, intact mirror it exits `0`. `verify` uses
the destination's lock like `download`, so the two never run on one DEST at
once. A DEST that is not an existing folder, or that no `download` recorded in
the manifest, is a usage error (exit `2`); `verify` never creates a manifest.

**Resuming.** A check is one *pass* over the mirror, kept in the manifest
until every file is checked. Ctrl+C or SIGTERM (`kill`, `systemctl stop`,
`docker stop`) prints `Interrupted; rerun verify to continue where it
stopped.` and exits with 130 (Ctrl+C) or 143 (SIGTERM); the findings made so
far stay recorded. The next `verify DEST` continues the pass instead of
starting over: it reads only the files after the last one checked, and
starts with

```
Continuing the verification started 2026-10-17 09:30 UTC: 412 files (380.5 MiB) left
```

The pass records how far it got at least every 5 s and when it stops, so
SIGKILL or a power cut costs the files checked in the last 5 s and the one being
read; a destination that became unreadable stops the pass the same way. Its
summary always covers the whole pass: a file checked in an earlier part
counts by what was recorded then (intact unless it was found missing,
modified or corrupt), so the report equals that of an uninterrupted
`verify`. A `download` between two parts is harmless: a file it repaired
counts as intact, and the files after the cursor are read as usual.
`verify --restart DEST` drops the pass in progress and starts a new one over
every file.

## Output and logs

`scan`, `download` and `verify` write to two channels:

- **stdout** carries the report: the start lines, the scan summary, the
  final summary, then the `Manifest:` and `Log:` lines. Nothing else is ever
  written there, so `> report.txt` keeps exactly the report. With `--json` it
  carries one JSON document instead (see [JSON reports](#json-reports)).
- **stderr** carries the progress and the notices as they happen, then
  the `Error: …` or `Interrupted; …` line a run ends with.

The progress is a live view when stderr is a terminal, and status lines
anywhere else. stderr counts as a terminal when it is a tty and `TERM` is set
and neither `dumb` nor `unknown`; `TERM=dumb` thus gives status lines on a
terminal too. Where stdout goes plays no part: `download … > report.txt` on a
terminal shows the live view, and the file gets the report alone.

**The live view.** On a terminal each slow phase is drawn in place and
redrawn twice a second:

```
Downloading: 1234 of 133521 files, 12.3 GiB of 2.1 TiB (0.6%), 3.9 MiB/s, ETA 6 d 7 h; 20 active, 3 without
data; 5 to retry, 3 failed, 0 quarantined; pass 1 of 7
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  disk:/Photos/2024/IMG_2041.jpg               2.1 MiB of 4.0 MiB (52.5%)  612.0 KiB/s
  …/Videos/2019/Summer/holiday-full.mkv        1.1 GiB of 16.5 GiB (6.7%)  no data for 9 s
  disk:/Docs/archive.zip                       320.0 MiB of 320.0 MiB (100.0%)  copying 41.2%
  disk:/Music/live.flac                        0 B of 512.3 MiB (0.0%)  waiting for a connection
  … and 10 more
```

- The first lines are the phase's status line (described below); then comes a
  bar of its bytes (of the files checked, in `download`'s check; of the files
  handled, in its preview step), or a spinner in front of the status line
  while a scan lists folders.
- During the passes of `download`, up to 10 files in progress follow, each
  with its path (shortened from the left to fit), the bytes its partial file
  holds of its size, and its speed over the last 10 s (`…` during its first
  second of data) or its state:
  `waiting for room` (in the partial folder), `waiting for a connection`
  (none of its connections is open: a segment waits for a free one, or the
  file for a new download link), `no data for N s` (a connection open but no
  data for 5 s or more),
  `preallocating`, `waiting to hash` (another file's bytes in the same folder
  are being hashed), `hashing` (a local copy, or the confirmed bytes of a
  resumed download), `reading back` (a segmented file), `waiting for the copy`
  (of another file to DEST) and `copying N%` (to DEST, with
  `--partial-dir`). `… and N more` counts the rest.
- In `verify`, the file being read follows the bar, with the bytes read of
  its size.
- Notices and report lines written while a phase runs are printed above the
  view, literally: a name such as `[red]x[/red].txt` shows as it is.
- The view is cleared when its phase ends, also when the run stops with an
  error, Ctrl+C or SIGTERM, and the cursor is shown again. What stays on the
  terminal is the report, the notices and the result line.

**Status lines.** Off a terminal, each slow phase writes one status line every
`progress_interval` seconds (default 60; see
[Configuration](#configuration)), then a last line when it ends, also when
the run stops with an error, Ctrl+C or SIGTERM. A multi-day run under
`nohup` thus writes one line a minute:

```
2026-10-17T09:30:14.002Z Scanning disk:/: 45120 files, 3200 folders (812.3 GiB) listed, 1024 folders to list
2026-10-17T09:44:05.310Z Scanned disk:/: 133521 files, 9800 folders (2.1 TiB) listed in 14 min 5 s
2026-10-17T09:45:05.311Z Checking 133521 files already in /mnt/backup: 40210 checked
2026-10-17T09:50:17.020Z Checked 133521 of 133521 files already in /mnt/backup in 6 min 12 s
2026-10-17T09:51:17.021Z Moving: 1 of 3 files, 1.2 GiB of 3.5 GiB (34.3%), 210.0 MiB/s, ETA 11 s
2026-10-17T09:51:28.402Z Moved: 3 of 3 files, 3.5 GiB in 17 s, average 210.8 MiB/s
2026-10-17T10:50:17.021Z Downloading: 1234 of 133521 files, 12.3 GiB of 2.1 TiB (0.6%), 3.9 MiB/s, ETA 6 d 7 h; 20 active, 3 without data; 5 to retry, 3 failed, 0 quarantined; pass 1 of 7
2026-10-23T18:01:40.512Z Downloaded: 133521 of 133521 files, 2.1 TiB in 6 d 9 h, average 4.0 MiB/s; 0 to retry, 3 failed, 0 quarantined; 2 of 7 passes
2026-10-23T18:01:41.020Z Previews: 3 of 3 files, 2 saved
2026-10-24T09:00:01.733Z Verifying: 1234 of 98000 files, 120.5 GiB of 2.0 TiB (5.9%), 110.2 MiB/s, ETA 5 h 2 min; 2 problems; reading disk:/big.mkv (1.2 GiB of 4.0 GiB)
2026-10-24T14:10:03.004Z Verified: 98000 of 98000 files, 2.0 TiB in 5 h 10 min, average 112.4 MiB/s; 2 problems
```

- The phases are the scan's listing (in `scan`, and in `download` when it
  scans), `download`'s check of the files already verified in DEST (only
  when there are any), its move of the downloads in progress to a new
  partial folder (only when the folder changed and some hold confirmed
  bytes; `Moving` counts those files and their confirmed bytes), the passes
  of `download`, its preview step (only when files failed on Yandex's side;
  `Previews: <done> of <n> files, <k> saved`, after the start line `Saving
  the previews of N failed files to …`), and `verify`.
- `Downloading` and `Verifying` count the files of the start line
  (`Downloading N files`, `Verifying N files`, or the files `left` of a
  continued pass). A file is done once it is downloaded or adopted, failed or
  quarantined; until then it counts the bytes its partial file holds,
  resumed bytes included. Failed folders, files held in quarantine and names
  that are not valid Unicode are in the summary but not in these counts.
- The speed is the rate over the last minute, and the ETA divides the bytes
  left by the rate over the last 15 minutes (`ETA unknown` while it is 0).
  Both count only the bytes this run moved. During the first second of a
  phase there is no speed yet: the line leaves it out and says `ETA unknown`.
- `N active, M without data`: the files in progress, and those of them with
  a connection open but no data for 5 s or more. `to retry` counts the
  files deferred to a later pass; between passes the line ends with
  `pass 2 of 7 starts in 4 s`.
- The last line of a phase gives how many of its files it got through
  (fewer than all when an error stopped it), the time it took and the
  average speed.

Every status line also goes to the log file, on a terminal too.

`--no-progress` shows no live view and writes no status lines to stderr; the
notices stay. Use it under cron, where periodic lines are noise. Off a
terminal, nothing the tool writes contains escape codes or carriage returns,
whatever `FORCE_COLOR` or `TTY_COMPATIBLE` say.

A name on the Disk can hold any character. In the report, the notices, the
status lines, the live view, the error line and the log file, each control
character of a name (an escape, a carriage return, a newline, any other C0 or
C1 control, U+2028 or U+2029) is shown as its escape, such as `\x1b` or
`\x0a`, and a lone surrogate (a name that is not valid Unicode) as `\udcxx`,
whether the name appears as a path or inside a reason that quotes the local
file: a hostile name cannot move the cursor, recolour the terminal or forge a
line of the report or the log.

A notice is one line, `<UTC time> <message>` (on a terminal, the message
alone):

```
2026-10-17T09:31:02.417Z disk:/Photos/gone.jpg failed: removed from the Disk after the scan
2026-10-17T09:35:40.002Z disk:/Photos/bad.jpg quarantined: MD5 mismatch (expected …, got …); kept at .yandex-disk-rescue/quarantine/2026-10-17T09-30-00Z/bad.jpg
```

By default stderr shows a notice for each file or folder that fails or is
quarantined in this run, each `verify` finding made in this run (`missing`,
`modified` or `corrupt`), each copy in DEST that `download` found unreadable
(`<path> corrupt: could not read <local>: <reason>`), each folder that
disappeared during a scan, each
entry left out because its name is not valid Unicode, each partial file
that could not be deleted once its file's outcome was recorded (`<path>:
partial file <file> not deleted: <reason>; the outcome stands`), each partial
file that could not be read (`<path>: could not read its partial file <file>:
<reason>; downloading it again from its first byte`), and each
preview not saved (`<path>: preview not saved: <reason>`). Files `verify` lists
from the manifest (not downloaded, or held in quarantine) get no notice; the
summary lists them.

- `-v` / `--verbose` also shows each file downloaded (size, time, average
  speed, how it was verified), adopted, renamed or moved aside, each
  deferral to a later pass (also for room in the partial folder), each pass
  starting, the end of a move of partial files to a new partial folder, the
  start and commit of a scan, and each Disk API throttling response and
  retry. It also logs DEBUG records to the log file.
- `-q` / `--quiet` leaves only the final `Error: …` or `Interrupted; …` line
  on stderr (and the log-file warning described below): no live view, no
  status lines and no notices.
- Neither changes stdout, and giving both is a usage error (exit `2`).

**The log file.** Every run of `scan`, `download` and `verify` appends to
`$XDG_STATE_HOME/yandex-disk-rescue/yandex-disk-rescue.log`
(`~/.local/state/yandex-disk-rescue/yandex-disk-rescue.log` when
`XDG_STATE_HOME` is unset or not an absolute path), whatever `-q` says:

```
2026-10-17T09:30:00.123Z INFO download[12345] download started: version 1.0.0rc1, pid 12345; remote_path=disk:/Photos, dest=/mnt/backup, …
2026-10-17T09:41:12.456Z INFO download[12345] Summary:
2026-10-17T09:41:12.456Z INFO download[12345] Mirrored disk:/Photos to /mnt/backup: 1234 downloaded (12.3 GiB), …
2026-10-17T09:41:12.457Z INFO download[12345] download completed (exit 0)
```

- Each line starts with the UTC time, the level, the command and its pid;
  every line of a summary or a traceback gets that prefix too, so `grep`
  always shows when and which run wrote a line.
- A run logs its start (the version and every argument and option value,
  `partial_dir` included), its start lines (`Downloading N files …`,
  `Moving the confirmed bytes …`, `The partial folder of the last run … is
  missing …`, `Partial files in …`), its full summary with every list complete (no `… and N more`), and its
  end: `completed (exit 0)`, `failed (exit N): <error>`,
  `interrupted (exit 130)` (Ctrl+C), `interrupted by SIGTERM (exit 143)`, or
  an unexpected error with its traceback.
- In between it logs every notice, every `-v` record and every status line,
  whatever the console flags (`-q` and `--no-progress` included). With `-v`
  it also logs DEBUG records: each file skipped as verified already or held
  in quarantine, each file `verify` found intact or listed from the
  manifest, each transfer attempt and immediate retry, each link refresh,
  each resumed segment with the bytes kept, the fallback to one stream, each
  checkpoint, each folder listed, and with `--partial-dir` each partial file
  moved to the new partial folder, each file waiting for room there (or for
  its turn behind files that wait for room), each file waiting for the copy
  of another to DEST, and each copy to DEST with its time.
- The file is rotated at 10 MiB, keeping five older files (`.1` is the most
  recent, `.5` the oldest). Every file is mode 0600 in the 0700 state
  directory.
- The log never holds the OAuth token or a download link.
- A usage error (exit `2` before the command starts, such as a wrong number
  of paths or `-v` with `-q`) writes nothing to the log.

A log file that cannot be opened stops the command before it does anything:
`Error: Could not open the log file <path>: <reason>.` (exit `1`). A write
that fails later (the state disk full, an I/O error) never stops the run:
stderr gets one `Warning: could not write the log file <path>: <reason>;
logging to it is off for this run.`, even with `-q`, and the report ends with
`Log: <path> (incomplete: <reason>)`. Likewise a write to stderr that fails
(a `2> file` on a full disk, a terminal that went away) never stops the run
and never changes its exit code: stderr is off for the rest of the run (no
live view, status lines, notices or `Error: …` line), the log gets one warning
`Could not write to stderr: <reason>; stderr is off for this run.`, and keeps
getting every record and status line.

### JSON reports

`--json` on `scan`, `download` or `verify` prints the run's report as one JSON
document on stdout, written once the command ends and every other line is
out. The human report then goes to stderr, where the notices and status
lines are (with `-q`, nowhere); the `Error: …` or `Interrupted; …` line and
the exit codes stay as they are. stdout holds that document and nothing else,
whatever the exit code:

```bash
yandex-disk-rescue download -q --json disk:/Photos /mnt/backup > run.json
jq '.counts, [.failed[].path]' run.json
```

Every document starts with the same keys:

| Key | Value |
|---|---|
| `schema_version` | `1`; it changes only when a key changes meaning or goes away |
| `command` | `scan`, `download` or `verify` |
| `version` | The tool's version |
| `started_at`, `finished_at` | UTC times, `2026-10-17T09:30:00.123Z` |
| `manifest`, `log` | The paths of the manifest and the log file |
| `log_incomplete` | Why the log file missed records (the `Log: … (incomplete: …)` reason), else `null` |
| `exit_code` | The process's exit code |
| `error` | `{"message": "…"}`, the text of the `Error:` (or `Interrupted;`) line; only when `exit_code` is not `0` |

A command that got as far as its report adds the report's keys, also when it
then exits `1` because files failed, were quarantined or have problems. A
command that ended with no report (a token Yandex rejects, a log file that
cannot be opened, an unreadable DEST, `--no-scan` with no scan, a DEST that is
not a mirror, Ctrl+C or SIGTERM) prints the common keys alone, with `error`. A
usage error (exit `2` before the command starts) prints no JSON.

Every list is complete, sizes are integers in bytes, and paths are the Disk's
own, control characters included (JSON escapes them); a name that is not
valid Unicode keeps its lone surrogate as a `\udcxx` escape. The one sample
is `scan`'s `without_checksum.sample`, the 20 smallest paths, with the full
`count`.

`scan`:

```json
{
  "schema_version": 1, "command": "scan", "…": "…",
  "root": "disk:/Photos", "files": 1203, "folders": 41, "size": 8573190144,
  "without_checksum": {"count": 2, "sample": ["disk:/Photos/a.heic", "disk:/Photos/b.heic"]},
  "vanished": [], "unencodable": [], "first_scan": false,
  "added": ["disk:/Photos/new.jpg"], "changed": [], "removed": ["disk:/Photos/old.jpg"]
}
```

`download` (`skipped` includes `adopted`, as in the summary's first line;
`passes` is `null` until a transient failure defers a file; `unreadable`
lists the copies in DEST found unreadable and downloaded again, each with
`path` and `reason`, also when that download then failed; each `failed`
item has `unavailable`, `true` for a file [unavailable on Yandex
Disk](#downloading), and `counts.unavailable` counts those; `preview_only`
lists the failed files saved only as a preview, each with `path`, `preview`
(relative to DEST) and `bytes`, `preview_errors` the previews not saved or
deleted, each with `path` and `reason`, and `counts.previews_pending` the failed files an
interrupted preview step did not get to; `scan` is the
scan object above with `reused: false`, or `{"reused": true, "root",
"completed_at"}`; `partial_dir` is the `--partial-dir` value, `partial_move`
the move of the downloads in progress to a new partial folder, `null` when
there is none, `partial_room` the room measured in a separate partial
folder, `sparse_partials` whether the partial folder's filesystem keeps
sparse files, without which new downloads are not segmented, and `previews`
the `--previews/--no-previews` value):

```json
{
  "schema_version": 1, "command": "download", "…": "…",
  "exit_code": 1, "error": {"message": "1 failed and 1 quarantined; see the lists above."},
  "remote_root": "disk:/Photos", "destination": "/mnt/backup", "retry_failed": false,
  "scan": {"reused": true, "root": "disk:/Photos", "completed_at": "2026-10-17T09:20:11.004Z"},
  "to_download": {"files": 12, "size": 104857600},
  "counts": {"downloaded": 10, "downloaded_size": 94371840, "skipped": 1191, "adopted": 0, "size_only": 0,
             "renamed": 0, "unreadable": 0, "moved_aside": 0, "quarantined": 1, "failed": 1, "unavailable": 0,
             "preview_only": 0, "preview_errors": 0, "previews_pending": 0},
  "passes": {"used": 2, "limit": 7},
  "adopted": [], "size_only": [], "renamed": [{"path": "disk:/Photos/a?b.jpg", "local": "a_b~1f2e3d4c.jpg"}],
  "unreadable": [], "moved_aside": [], "replaced_folder": null,
  "quarantined": [{"path": "disk:/Photos/bad.jpg", "reason": "MD5 mismatch (expected …, got …)",
                   "last_retry": null, "copy": ".yandex-disk-rescue/quarantine/2026-10-17T09-30-00Z/bad.jpg"}],
  "failed": [{"path": "disk:/Photos/gone.jpg", "reason": "removed from the Disk after the scan", "unavailable": false}],
  "preview_only": [], "preview_errors": [],
  "partials_left": [],
  "partial_dir": "/mnt/fast",
  "previews": true,
  "partial_move": {"source": "/mnt/old/0a1b2c3d4e5f6a7b", "target": "/mnt/fast/0a1b2c3d4e5f6a7b",
                   "downloads": 2, "size": 750000, "missing": false},
  "partial_room": {"folder": "/mnt/fast/0a1b2c3d4e5f6a7b", "available": 53687091200},
  "sparse_partials": true
}
```

`last_retry` is the failure of the last attempt to fetch a quarantined file
again (the summary's `; last retry: …`), `null` when none failed.

`verify` (`pass` is the pass the run belongs to: when it started, whether
this run `continued` an interrupted one, and the files and bytes this run
read; `counts` cover the whole pass):

```json
{
  "schema_version": 1, "command": "verify", "…": "…",
  "destination": "/mnt/backup", "remote_root": "disk:/Photos",
  "counts": {"intact": 1199, "size_only": 2, "missing": 1, "modified": 0, "corrupt": 1, "quarantined": 1,
             "not_downloaded": 1, "problems": 4},
  "size_only": ["disk:/Photos/a.heic", "disk:/Photos/b.heic"],
  "missing": [{"path": "disk:/Photos/x.jpg", "reason": "x.jpg is missing", "repair_quarantined": null}],
  "modified": [],
  "corrupt": [{"path": "disk:/Photos/y.jpg", "reason": "MD5 mismatch (expected …, got …)",
               "repair_quarantined": {"reason": "MD5 mismatch (expected …, got …)",
                                      "copy": ".yandex-disk-rescue/quarantine/2026-10-17T09-30-00Z/y.jpg"}}],
  "quarantined": [{"path": "disk:/Photos/bad.jpg", "reason": "MD5 mismatch (expected …, got …)",
                   "last_retry": null, "copy": ".yandex-disk-rescue/quarantine/2026-10-17T09-30-00Z/bad.jpg"}],
  "not_downloaded": [{"path": "disk:/Photos/napkin.jpg", "reason": "the download host … returned HTTP 500",
                      "preview": ".yandex-disk-rescue/previews/napkin.jpg.preview.jpg"}],
  "pass": {"started_at": "2026-10-24T09:00:00.512Z", "continued": true, "files_read": 400, "size_read": 2147483648}
}
```

`repair_quarantined` is set when the download meant to repair a finding was
quarantined (the summary's `; repair quarantined: …; kept at …`). Each
`not_downloaded` item has `path`, `reason` and `preview`, the path of the
preview saved for the file's current version (relative to DEST), or `null`.

## Running unattended

A long mirror runs for days, so it usually runs without a terminal. Off a
terminal, stderr gets a timestamped status line every `progress_interval`
seconds (default 60) instead of the live view, and every run appends to the
[log file](#output-and-logs) whatever the flags. The examples run as an
ordinary user who has run `yandex-disk-rescue auth login` once (or who sets
`YANDEX_DISK_RESCUE_TOKEN`; see [Automation](#automation)). systemd and cron
need the command's absolute path, which `command -v yandex-disk-rescue`
prints; the examples assume `~/.local/bin/yandex-disk-rescue`.

When DEST is a slow disk (a USB hard disk, a network share), add
`--partial-dir` with a folder on a fast local disk, or set `partial_dir` in
the [config file](#configuration) so every way of running uses it (see
[Partial files](#downloading)). Pick a persistent folder, not a `tmpfs` such
as `/tmp` on many systems: the partial files must survive a reboot for the
next run to resume them.

### In the background with nohup

```bash
nohup yandex-disk-rescue download disk:/ /mnt/backup < /dev/null > download.out 2> download.err &
tail -f download.err
tail -f ~/.local/state/yandex-disk-rescue/yandex-disk-rescue.log
```

- `download.out` gets the report as the run goes: the start lines, the scan
  summary, then the final summary and the `Manifest:` and `Log:` lines. It
  stays small.
- `download.err` gets the status lines, the notices and the final `Error: …`
  or `Interrupted; …` line, each with its UTC time. A status line is about 200
  bytes, so each phase adds about 300 KB a day at the default interval;
  `--progress-interval 600` writes one every 10 minutes instead.
- The log file gets the same notices and status lines and the full summary.
  It is rotated at 10 MiB and keeps five older files, so it never takes more
  than 60 MiB.
- `< /dev/null` keeps `nohup` from writing `nohup: ignoring input` into
  `download.err`.

`verify` runs the same way. `kill <pid>` (SIGTERM) stops the run as Ctrl+C
does: it takes its last checkpoints, prints `Interrupted; …` and exits with
143, and the next run continues from there. This holds also for a background
job that a script starts, whose SIGINT is ignored; `kill -INT <pid>` stops
the run only when it was started from an interactive shell. A SIGTERM sent
while the run is already stopping, on Ctrl+C or an earlier SIGTERM, is
ignored, so the last checkpoints are never cut short. A Ctrl+C during a
SIGTERM stop takes over and exits with 130: it cuts the stop short, so some
last checkpoints may be skipped, and a second Ctrl+C ends the run at once.
SIGKILL ends the run at once.

### As a systemd user service

`~/.config/systemd/user/yandex-disk-rescue.service`:

```ini
[Unit]
Description=Mirror Yandex Disk to /mnt/backup

[Service]
Type=oneshot
ExecStart=%h/.local/bin/yandex-disk-rescue download -q --json disk:/ /mnt/backup
StandardOutput=append:%h/yandex-disk-rescue.json
StandardError=journal
TimeoutStartSec=infinity
TimeoutStopSec=5min
SuccessExitStatus=143
```

`~/.config/systemd/user/yandex-disk-rescue.timer`:

```ini
[Unit]
Description=Mirror Yandex Disk to /mnt/backup every day

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now yandex-disk-rescue.timer
systemctl --user start --no-block yandex-disk-rescue.service   # a run now
journalctl --user -u yandex-disk-rescue.service
systemctl --user stop yandex-disk-rescue.service
```

- `ExecStart` takes the command's absolute path (`%h` is your home folder).
  To pass the token by environment instead of the token file, add
  `EnvironmentFile=%h/.config/yandex-disk-rescue/env` with a line
  `YANDEX_DISK_RESCUE_TOKEN=…` in that file (mode `0600`).
- `Type=oneshot` makes the unit active for as long as the run lasts.
  `TimeoutStartSec=infinity` is needed because a oneshot's start timeout
  would otherwise kill a run that lasts days. `systemctl start` waits for a
  oneshot to finish, hence `--no-block`.
- `systemctl stop` (and a shutdown) sends SIGTERM, which stops the run as
  Ctrl+C does, with its last checkpoints. `TimeoutStopSec` is how long
  systemd waits for them before it sends SIGKILL, which costs at most the
  last checkpoint interval of each file in progress.
  `SuccessExitStatus=143` counts a stopped run as a clean stop, not a
  failure.
- `loginctl enable-linger` starts your user's systemd instance at boot and
  keeps it after you log out, so the timer fires and the run goes on without
  a login session.
- The timer starts the service once a day, and `Persistent=true` catches up
  a start missed while the machine was off. A run still going when the timer
  fires again is not started a second time.
- stdout, the JSON document, is appended to `~/yandex-disk-rescue.json`, one
  document per run; with `--json` the human report goes to stderr, and `-q`
  leaves nothing there but the final `Error: …` or `Interrupted; …` line (and
  the log-file warning, if a log write fails). So the journal holds just
  that. Without `-q` the journal also gets the notices and the human report;
  add `--no-progress` then to keep the status line a minute out of it (the
  log file still gets them).
- Stopped with `systemctl stop`, the run still writes its JSON document,
  with `exit_code` 143.
- A run that exits `1` (files failed or were quarantined, or an error)
  leaves the unit failed: `systemctl --user --failed` lists it, and
  `OnFailure=` in `[Unit]` can start a unit that tells you.

### From cron

```bash
# crontab -e: every day at 03:30
30 3 * * * $HOME/.local/bin/yandex-disk-rescue download -q --json disk:/ /mnt/backup >> $HOME/yandex-disk-rescue.json
```

- The exit code is the signal: `0` when every file is verified, `1` when
  files failed or were quarantined or the run hit an error (see
  [Exit codes](#exit-codes)). cron does not look at it, but it mails what a
  command writes, and with `-q` and stdout in a file a run writes nothing
  but its `Error: …` line (and the log-file warning, if a log write fails).
  So a mail arrives only for a run that did not exit `0`.
- A run that starts while the last one still goes on exits `1` at once with
  `Error: Another yandex-disk-rescue run is using …; wait for it to finish
  and try again.`; the running one is not disturbed.
- Each run appends one JSON document to the file.

### Checking a run afterwards

- **The summary.** The log file holds every run's full summary, every list
  complete, and the end of each run, `completed (exit 0)` or `failed (exit N):
  <error>`. `grep -E ' (WARNING|ERROR) ' <log>` lists every notice (each
  file that failed or was quarantined, each `verify` finding, each copy in
  DEST found unreadable) and the end of
  every run that failed or was interrupted. Under `nohup`, the summary is
  also in `download.out`.
- **The JSON documents.** `jq` reads a file of appended documents as a stream:

  ```bash
  jq -c '{finished_at, exit_code, error: .error.message}' ~/yandex-disk-rescue.json   # one line per run
  jq -s 'last | {counts, failed: [.failed[]?.path]}' ~/yandex-disk-rescue.json        # the latest run
  ```

  `jq` shows a name that is not valid Unicode with U+FFFD (`�`) in place of
  each `\udcxx` escape. The file keeps the escape, and Python's `json`
  module, for one, reads it back as the exact name.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success; for `download`, every file of the folder is verified at its final path; for `verify`, every file of the mirror is verified and intact |
| `1` | A failure the tool reports as `Error: ...` on stderr: no token, a malformed, unreadable or insecure token, a token Yandex rejects, a remote path that is missing or not a folder, an API error after the retries, a manifest that is unusable or in use by another run, a log file that cannot be opened; for `download` also one or more failed or quarantined files (after the summary), a destination that cannot be read or written or lacks `RENAME_NOREPLACE`, and a destination in use by another run; for `verify` also one or more missing, modified, corrupt, quarantined or not downloaded files (after the summary), a destination that became unreadable, and a destination in use by another run |
| `2` | Invalid input: a usage error, an invalid option value from any source, an invalid config file, a remote path outside `disk:/`; for `download` also a number of paths other than one or two, a DEST that is not a folder, whose parent does not exist or that starts with `disk:`, a DEST already mirroring another remote folder or account, and `--no-scan` for a remote folder no completed scan covers; for `verify` a DEST that is not an existing folder or is not a mirror recorded in the manifest |
| `130` | Interrupted with Ctrl+C |
| `143` | Stopped with SIGTERM (`kill`, `systemctl stop`, `docker stop`): the same clean stop as Ctrl+C |

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

Every request the tool makes, to the Disk API and to the download host,
names the tool in its `User-Agent`:
`yandex-disk-rescue/<version> (+https://github.com/azorej/yandex-disk-rescue)`.

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
| `download_passes` | `--download-passes` (`download`) | `YANDEX_DISK_RESCUE_DOWNLOAD_PASSES` | `7` | 0 to 1000; passes over the files still to download, `0` until every file is done or unavailable on Yandex Disk |
| `download_connections` | `--download-connections` (`download`) | `YANDEX_DISK_RESCUE_DOWNLOAD_CONNECTIONS` | `20` | 1 to 64; requests to the download host in flight, and so files downloading at once |
| `download_checkpoint_mib` | `--download-checkpoint-mib` (`download`) | `YANDEX_DISK_RESCUE_DOWNLOAD_CHECKPOINT_MIB` | `32` | 1 to 1024 (MiB); bytes a segment receives between two checkpoints of a download |
| `download_segments` | `--download-segments` (`download`) | `YANDEX_DISK_RESCUE_DOWNLOAD_SEGMENTS` | `4` | 1 to 8; segments of a file above the segment threshold, fetched in parallel; `1` never splits |
| `download_segment_threshold_mib` | `--download-segment-threshold-mib` (`download`) | `YANDEX_DISK_RESCUE_DOWNLOAD_SEGMENT_THRESHOLD_MIB` | `256` | 1 to 65536 (MiB); a larger file is downloaded as segments |
| `partial_dir` | `--partial-dir` (`download`) | `YANDEX_DISK_RESCUE_PARTIAL_DIR` | empty (partial files in DEST) | an absolute path of an existing folder outside DEST, or empty; see [Partial files](#downloading) |
| `scan_max_age` | `--scan-max-age` (`download`) | `YANDEX_DISK_RESCUE_SCAN_MAX_AGE` | `60` | 0 to 10080 (min); a younger covering scan is reused, `0` always scans |
| `progress_interval` | `--progress-interval` (`scan`, `download`, `verify`) | `YANDEX_DISK_RESCUE_PROGRESS_INTERVAL` | `60` | 1 to 3600 (s); seconds between two status lines on stderr and in the log file |
| `previews` | `--previews/--no-previews` (`download`) | `YANDEX_DISK_RESCUE_PREVIEWS` | `true` | `true` or `false` (the environment variable also takes `1`/`0`, `yes`/`no`); fetch the preview of each file that failed on Yandex's side, see [Previews](#downloading) |
| `client_id` | `--client-id` (`auth login`) | `YANDEX_DISK_RESCUE_CLIENT_ID` | the built-in app | 32 lowercase hex characters |

The `api_*` options apply to every command that talks to the Disk API
(`auth login`, `auth status`, `scan`, `download`); the `download_*` options,
`partial_dir`, `scan_max_age` and `previews` apply to `download`, `progress_interval` to
`scan`, `download` and `verify`, and `client_id` to `auth login`. `verify`
never talks to the Disk API and takes no `api_*` option, and `auth logout`
takes no option.
`--partial-dir ''` keeps partial files in DEST whatever the config file says;
an empty environment variable counts as unset, as for every option.

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

## Development

The repository is a uv workspace with mise tasks and a Dagger CI pipeline;
[`AGENTS.md`](AGENTS.md) holds its rules.

### Setup

**Devcontainer (recommended):** open the repo in VS Code → *Reopen in
Container*, or create a GitHub Codespace. Everything below runs on creation.

**Local machine:** install [mise](https://mise.jdx.dev/getting-started.html)
and Docker (for Dagger), then:

```bash
mise trust && mise run setup        # tools + deps + git hooks
mise run check                      # lint + test
uv run yandex-disk-rescue --help
```

### Everyday commands

| Command | What it does |
|---|---|
| `mise run setup` | Install pinned tools, sync deps, install git hooks (idempotent) |
| `mise run check` | Fast local gate: lint + test |
| `mise run format` | Auto-fix lint issues and format |
| `mise run test -- -k name` | Pytest, with pass-through args |
| `mise run ci` | Full CI pipeline in Dagger — identical to GitHub Actions |
| `mise run ci:selftest` | Tests for the Dagger module itself |
| `mise run ci:package` | Build and `twine check` the sdist and wheel, then install the wheel with `uv tool` and `pipx` in clean containers and run the installed-CLI tests |
| `mise run release:dist vX.Y.Z` | Rehearse a release up to the upload: the tag and CHANGELOG checks, the build and the install smoke, into `release/` |
| `mise run release:verify vX.Y.Z` | Check that PyPI lists exactly the files of `release/dist/`, with their SHA-256s, and that `uv tool install yandex-disk-rescue==X.Y.Z` works |
| `mise run lock` | Upgrade every dependency in `uv.lock` |
| `mise tasks` | List all tasks |

Dependencies: `uv add <pkg> --package <member>` (runtime) or
`uv add --dev <pkg>` (tooling). Local secrets go in `.env` (see
[`.env.example`](.env.example)); mise loads it automatically.

### Layout

```
├── yandex-disk-rescue/   # The CLI package — workspace member
├── .dagger/              # Dagger CI module (+ its own tests in .dagger/tests)
├── .devcontainer/        # Devcontainer: mise, Claude Code, Docker-in-Docker
├── .github/              # Workflows, PR template, Dependabot config
├── .agents/skills/       # Agent skills (symlinked into .claude/skills/)
├── .claude/              # Claude Code settings, hooks, subagents
├── docs/                 # Guides (GitHub setup) and implementation plans
├── scripts/              # Standalone helper scripts
├── AGENTS.md             # Rules for humans and agents — start here
├── CHANGELOG.md          # Release notes, one section per version
├── mise.toml             # Tool versions, env, tasks
└── pyproject.toml        # Workspace root: members, dev deps, ruff, pytest
```

### How it fits together

**Versions.** Each tool is pinned in exactly one place — Python/uv/Dagger in
`mise.toml`, libraries and ruff in `uv.lock`, the Dagger engine in
`dagger.json` — and the devcontainer, CI and your shell all read from there.
Upgrade with `mise outdated` / `mise upgrade --bump` (tools) and `mise run lock` (Python deps).

**CI.** [`.github/workflows/ci.yml`](.github/workflows/ci.yml) is a thin
wrapper around `mise run` tasks (`ci`, `ci:selftest`, `ci:package`), which call the Dagger module in
[`.dagger/`](.dagger/README.md). Pull requests run only the lanes their
diff touches; pushes to `main`, a weekly schedule and manual runs execute
everything. Require the single **`CI required`** check in branch protection
(see [docs/github-setup.md](docs/github-setup.md)). The workflows themselves
are linted by actionlint and zizmor (pre-commit hooks), and every action is
pinned to a commit SHA that Dependabot keeps current. A pushed `vX.Y.Z` tag runs
[`.github/workflows/release.yml`](.github/workflows/release.yml): build and check, publish to PyPI after a
manual approval, verify the install, create the GitHub Release (see
[CONTRIBUTING.md](CONTRIBUTING.md#releasing)).

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
