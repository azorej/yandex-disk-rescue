# Yandex Disk Rescue

A Linux command-line tool that downloads everything from a Yandex Disk
account to local storage. It is a headless analogue of the Windows app
[Yandex-Disk-Rescue](https://github.com/fefenke/Yandex-Disk-Rescue).

> **Status: early development.** OAuth token handling
> (`yandex-disk-rescue auth`), scanning the remote tree into a local
> manifest (`yandex-disk-rescue scan`) and verified, parallel, resumable
> mirroring with segmented downloads of large files
> (`yandex-disk-rescue download`) and an offline, resumable integrity check
> of a mirror (`yandex-disk-rescue verify`) work. Further features are being built;
> planned work is tracked in GitHub issues.

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
`--download-connections`, `--download-checkpoint-mib`, `--download-segments`,
`--download-segment-threshold-mib` and `--scan-max-age` (see
[Configuration](#configuration)), `--no-scan` and `--retry-failed`
(below). A file whose download host never answers holds one connection until
it stalls and delays no other file.

**Segmented files.** A file larger than `download_segment_threshold_mib` MiB
(default 256) is downloaded as `download_segments` segments (default 4): equal
byte ranges fetched in parallel, each with an HTTP `Range` request on a
connection of its own, all into one partial file that is first extended to the
file's full size. Once every segment is in, the whole file is read back and
checked against its size and checksums. A file at or below the threshold, and
every file when `download_segments` is `1`, is fetched as one stream and
checked as it arrives. The segments of a download are fixed when it starts: a
download resumed by a later pass or a later run keeps them, even when the
options changed since.

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
Until then it is written to `DEST/.yandex-disk-rescue/partial/`. A file with
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
A file [`verify`](#verifying-a-mirror) found missing, modified or corrupt is
downloaded again whatever its record says, and its copy in DEST is never read
(it may be unreadable): once the new file is verified, that copy is moved
aside to `replaced/` (below) and the finding is dropped. A repair that fails,
is interrupted or is quarantined keeps the finding, so every later run treats
the file the same way until it is verified. `download --retry-failed`
(below) repairs exactly those files without walking the rest of the mirror.

**Interruptions, stalls and retries.** Every download keeps a checkpoint
journal in the manifest: for each segment (a file fetched as one stream has
one), how many of its bytes were flushed to the disk, and their SHA-256. A
checkpoint is taken every `download_checkpoint_mib` MiB a segment receives
(default 32), every 15 s while bytes arrive, and whenever a download attempt
ends — a dropped connection, a stall, Ctrl+C. After a kill, a crash or a power
cut, at most one checkpoint interval per segment is fetched again.

A later pass or a later run asks the Disk for the file again and, when its
size, checksums and modification time are unchanged, re-reads each segment's
confirmed bytes and checks them against their SHA-256. When they match, only
the rest of the segment is fetched, with an HTTP `Range` request; bytes past
the last checkpoint are fetched again, never trusted. A segment whose confirmed
bytes changed on disk, or that a partial file deleted or cut short no longer
holds, starts again from its first byte; the other segments go on from their
checkpoints. When the file changed in the cloud, the partial file is deleted
and the file fails with `changed in the cloud since the scan; rerun with
--scan-max-age 0 to fetch the new version` (after a rescan, the new version
is downloaded from the start). A partial file left for an older version is
discarded and the file is downloaded from the start; when the download host
ignores `Range` for any segment, the whole file is fetched again as one
stream, never appended to the bytes kept.

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
  are resumed in another pass over the files still to download, again up to
  `download_connections` at once, each with a new download link, after a 5 s
  pause (longer when the host sent `Retry-After`, up to 300 s). The other
  segments of such a file finish first, and the next pass fetches only the
  segments still missing. A run makes at most `download_passes` passes
  (`0`: until every file is done or Ctrl+C); a file still failing after the
  last one is reported with its last reason and `(after N passes)`. Once a
  file needed another pass, the summary shows `Passes: <used> of <limit>`. A
  checksum mismatch quarantines the file at once, and a refused download, a
  file changed in the cloud and a local write error fail it at once, stopping
  its other segments; no later pass retries it.
- **Expired links.** The segments of a file share one download link per pass,
  requested only once a segment has a free connection, so no link ages while
  it waits for one. A link the host rejects (`401`, `403` or `410`) is
  replaced by a new one once per pass for the whole file, and the download
  continues from the bytes received; when the new link is rejected too, the
  file waits for the next pass, which starts with a new link; after the last
  pass its reason is `download link rejected again after a refresh (HTTP <n>)`.

**Retrying failures.** Every file or folder a run reports under `Failed` is
recorded in the manifest for that DEST with the reason shown, as soon as it
fails: a file still failing after the last pass, a refused download, a file
changed or removed in the cloud, a local error, a hard link to another file of
the mirror, a refused name, a folder that could not be made, and every entry
under such a folder. The record is dropped once the entry succeeds: a file
verified or adopted, a folder made or found in place. Files stopped by Ctrl+C
or by an error that stops the run get no failure record; their unfinished
downloads stay recorded as partial.

A quarantined file gets no failure record and is not listed under `Failed`.
When a retry of a quarantined file fails (a stall, say), the failure is
recorded too, but the file stays listed under `Quarantined` only, its line
ending with `; last retry: <reason>`.

`download --retry-failed [REMOTE_PATH] DEST` covers only the *retry set*: the
files with a failure record, a quarantine record, a download that started
and never finished (for example one stopped by Ctrl+C) or a
[`verify`](#verifying-a-mirror) finding (missing, modified or corrupt), and
the folders with a failure record. Files never started are not in it; a plain `download` gets them. The
scan step runs as usual; the retry set is fixed once it is done. The run then
visits only the retry set and the folders above its members, in path order,
with the usual passes and rules, and never looks at any other path of DEST:
those files are neither checked nor counted. It starts with

```
Retrying 3 failed, quarantined, unfinished or broken files (1.5 GiB) in /mnt/backup
```

in place of `Downloading …`, and its summary and exit code are those of a
normal run over the retry set. With nothing to retry it prints `Retrying 0
failed, quarantined, unfinished or broken files (0 B)`, downloads nothing and exits
`0`. Entries
whose names are not valid Unicode have no record; they fail every run,
`--retry-failed` included.

**Summary.**

```
Downloading 120 files (1.2 GiB) to /mnt/backup/Photos
Mirrored disk:/Photos to /mnt/backup/Photos: 120 downloaded (1.2 GiB), 1000 skipped, 1 quarantined, 1 failed
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
Quarantined: 1
  disk:/Photos/bad.jpg: MD5 mismatch (expected …, got …); kept at .yandex-disk-rescue/quarantine/2026-10-17T09-30-00Z/bad.jpg
Download them again with --retry-failed (add --scan-max-age 0 if they changed on the Disk since the scan).
Failed: 1
  disk:/Photos/gone.jpg: removed from the Disk after the scan
Manifest: /home/user/.local/state/yandex-disk-rescue/manifest.sqlite3
```

Every list is in path order, whatever the order the files finished in, and
shows at most 20 entries, then `… and N more`. When anything
failed or is quarantined, the run ends with exit code `1` and
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
(folders and links there are left alone). Moved-aside and quarantined copies
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
work directory: it holds partial files, the `replaced/` and `quarantine/`
folders and a lock, so only one run at a time can use a destination
(`Another yandex-disk-rescue run is using DEST`), even runs with different
manifests. The destination's filesystem must support
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
so the files downloading at once (one connection each, or one per segment of
a segmented file; the segments queue for the same connections). Link requests
and look-ups go to the Disk API and are paced separately, by
`api_interval_ms` and `api_concurrency` (see [Rate limiting](#rate-limiting)).

Many files written at once make a hard disk seek between them. On a USB hard
disk, start around 8 connections and raise them while the throughput grows.

Segments speed up one large file on a fast link whose single connection is the
limit; a folder of small files gains nothing from them, since its files
already download in parallel. They cost disk work: on a filesystem without
sparse files (HFS+, exFAT, FAT), extending the partial file to its full size
writes zeros first, so a segmented file is written twice (zeros, then data)
and read once more for verification. On such a disk, especially a USB hard
disk, prefer `download_segments = 1` or `2`. The defaults (4 segments above
256 MiB) are those of the Windows
[Yandex-Disk-Rescue](https://github.com/fefenke/Yandex-Disk-Rescue).

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
  be read (`could not read <path>: <reason>`).
- **Quarantined:** its last download [failed the check](#downloading) and is
  kept in `quarantine/`; listed with the reason and the copy's path.
- **Not downloaded:** no verified copy of the current Disk version: never
  downloaded, failed (with the reason), unfinished, changed on the Disk since
  it was downloaded, or a name that is not valid Unicode.

Only files with a verified copy of their current Disk version are read; the
others are listed from the manifest. A file found missing, modified or corrupt is recorded
in the manifest at once, and the next `download` (a plain one, `--no-scan`
or `--retry-failed`) downloads it again without reading the bad copy and moves that copy
aside to `replaced/`. A file `verify` finds intact again drops its finding.
Paths of DEST with no Disk counterpart (local-only files, `.DS_Store`, …) are
never opened, listed or reported, and folders are not checked.

A read error usually means that one file is damaged, and the file is reported
as corrupt. When DEST itself cannot be read any more, such as a disk unplugged
mid-check, `verify` stops instead of reporting every remaining file:
`Error: Could not read DEST: <reason>; verify stopped. Rerun it once the disk
is back.` (exit `1`); what it found before then stays recorded.

```
Verifying 1120 files (1.2 GiB) in /mnt/backup/Photos
Verified /mnt/backup/Photos (disk:/Photos): 1115 intact, 1 missing, 1 modified, 1 corrupt, 1 quarantined, 1 not downloaded
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
Not downloaded: 1
  disk:/Photos/new.jpg: not downloaded yet
Manifest: /home/user/.local/state/yandex-disk-rescue/manifest.sqlite3
```

Every list is in path order and shows at most 20 entries, then `… and N
more`. When any file is missing, modified, corrupt, quarantined or not
downloaded, `verify` ends with exit code `1` and `Error: N problems found; see
the lists above.`; on a complete, intact mirror it exits `0`. `verify` uses
the destination's lock like `download`, so the two never run on one DEST at
once. A DEST that is not an existing folder, or that no `download` recorded in
the manifest, is a usage error (exit `2`); `verify` never creates a manifest.

**Resuming.** A check is one *pass* over the mirror, kept in the manifest
until every file is checked. Ctrl+C prints `Interrupted; rerun verify to
continue where it stopped.` and exits with 130; the findings made so far stay
recorded. The next `verify DEST` continues the pass instead of starting over:
it reads only the files after the last one checked, and starts with

```
Continuing the verification started 2026-10-17 09:30 UTC: 412 files (380.5 MiB) left
```

The pass records how far it got at least every 5 s and when it stops, so a
kill or a power cut costs the files checked in the last 5 s and the one being
read; a destination that became unreadable stops the pass the same way. Its
summary always covers the whole pass: a file checked in an earlier part
counts by what was recorded then (intact unless it was found missing,
modified or corrupt), so the report equals that of an uninterrupted
`verify`. A `download` between two parts is harmless: a file it repaired
counts as intact, and the files after the cursor are read as usual.
`verify --restart DEST` drops the pass in progress and starts a new one over
every file.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success; for `download`, every file of the folder is verified at its final path; for `verify`, every file of the mirror is verified and intact |
| `1` | A failure the tool reports as `Error: ...` on stderr: no token, a malformed, unreadable or insecure token, a token Yandex rejects, a remote path that is missing or not a folder, an API error after the retries, a manifest that is unusable or in use by another run; for `download` also one or more failed or quarantined files (after the summary), a destination that cannot be written or lacks `RENAME_NOREPLACE`, and a destination in use by another run; for `verify` also one or more missing, modified, corrupt, quarantined or not downloaded files (after the summary), a destination that became unreadable, and a destination in use by another run |
| `2` | Invalid input: a usage error, an invalid option value from any source, an invalid config file, a remote path outside `disk:/`; for `download` also a number of paths other than one or two, a DEST that is not a folder, whose parent does not exist or that starts with `disk:`, a DEST already mirroring another remote folder or account, and `--no-scan` for a remote folder no completed scan covers; for `verify` a DEST that is not an existing folder or is not a mirror recorded in the manifest |
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
| `download_checkpoint_mib` | `--download-checkpoint-mib` (`download`) | `YANDEX_DISK_RESCUE_DOWNLOAD_CHECKPOINT_MIB` | `32` | 1 to 1024 (MiB); bytes a segment receives between two checkpoints of a download |
| `download_segments` | `--download-segments` (`download`) | `YANDEX_DISK_RESCUE_DOWNLOAD_SEGMENTS` | `4` | 1 to 8; segments of a file above the segment threshold, fetched in parallel; `1` never splits |
| `download_segment_threshold_mib` | `--download-segment-threshold-mib` (`download`) | `YANDEX_DISK_RESCUE_DOWNLOAD_SEGMENT_THRESHOLD_MIB` | `256` | 1 to 65536 (MiB); a larger file is downloaded as segments |
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
