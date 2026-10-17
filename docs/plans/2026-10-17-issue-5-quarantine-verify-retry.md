# Plan: Quarantine, `--retry-failed` and `verify`

Issue: #5 (umbrella #9)
Branch: feat/quarantine-verify-retry (from `main` after the #4 PR, `feat/parallel-segmented-downloads`, has merged)

## Context

#3 delivered #5's first deliverable: a changed or differing local copy is moved to
`DEST/.yandex-disk-rescue/replaced/<stamp>/` before the new file is placed. Its acceptance criterion ("change a file in
the cloud, rerun: old copy in replaced, new copy verified") is locked by
`test_a_file_changed_in_the_cloud_replaces_the_old_copy_only_once_verified[None]`. This plan covers the other three
deliverables.

Observed on `feat/parallel-segmented-downloads` (dd88182), against the fake server with scratch XDG dirs only.
`test_download.py -k "corrupted_download or corrupt_file_is_not_retried or changed_in_the_cloud_replaces"`: 5 passed.

- **A copy that fails verification is deleted and downloaded again by every run.** This is #3 Decision 4, `_discard`
  in `mirroring._fetch_verified`. Scratch script: `bad.txt` is corrupt on the server, `good.txt` is healthy.
  ```
  --- download () exit 1
  Mirrored disk:/A to …/dest: 1 downloaded (400 B), 0 skipped, 1 failed
    disk:/A/bad.txt: MD5 mismatch (expected be59e6bd…, got 3242536e…)
  --- download ('--scan-max-age', '0') exit 1
  Downloading 1 file (400 B) to …/dest
  content requests on rerun for bad.txt: ['/v1/disk/resources/download', '/download']
  work dir: ['lock', 'partial'] []        <- the bad bytes are gone
  ```
  When the Disk's checksum is wrong and its bytes are right, the bytes thrown away are the only copy of the user's
  file.
- **Bit rot in a verified file goes unnoticed.** Changing one byte of a verified `good.txt` while keeping its size and
  mtime gives a rerun that skips it: `exit 0`, `1 skipped`, and the file still reads `b'Xoodgood'`. Skipping by the
  `lstat` fingerprint is right for `download`. Nothing re-hashes the data, though, and nothing would send the file
  to download again.
- **No retry mode and no verify command.** `uv run yandex-disk-rescue verify /tmp/x` gives `No such command 'verify'`,
  and `download --retry-failed` gives `No such option: --retry-failed`. Both exit 2.
- **Failures leave no trace.** A failed file is only a line in that run's summary. The manifest (schema v4) has
  `downloads(state IN ('partial', 'verified'))` plus the segment journal, and nothing records a failure.
- **Scale of `verify`.** The target is 133,696 files and 2.1 TiB on a USB HFS+ disk, which reads at roughly
  100–150 MB/s. A full re-hash takes about 4–6 hours. MD5+SHA-256 in one pass is much faster than that (#3 measured
  880/1935 MB/s), so one sequential reader is limited by the disk, not the CPU.
- **Found while studying.** An `EIO` while reading a local copy for adoption stops the run with
  `Error: Could not write <path>: Input/output error.`. Filed as #16 (see Discovered issues).

## Goal

- **Quarantine.** A downloaded copy that fails verification (size, MD5 or SHA-256, one-stream or segmented) is
  moved, never deleted, to `DEST/.yandex-disk-rescue/quarantine/<run start, UTC>/<local path>`, and the manifest
  records it. A plain `download` does not fetch that file again while the Disk still has the same version. It lists
  the file as quarantined and exits 1.
- **`download --retry-failed`.** The run covers only the files whose last attempt in this mirror did not end
  verified: failed, quarantined, or partially downloaded. Every other file is left alone and not counted.
- **`verify DEST`.** It re-hashes every verified file of the mirror against the manifest, with no network and no
  token. It reports missing, modified and corrupt files, and also files that are quarantined or not downloaded. It
  exits non-zero when anything is reported. It creates, modifies, moves and deletes nothing in DEST, so it works
  on a read-only mount; reads may update access times. It stops, instead of reporting everything as corrupt,
  when DEST itself becomes unreadable (a disconnected disk). It never opens or reports local-only content. The next `download` fetches again what `verify` found broken. An interrupted
  `verify` continues where it stopped.

## Decisions that go beyond the issue's wording (for the user to approve)

1. **What counts as "failed" for `--retry-failed`.** Every file whose last turn ended in a failure is recorded in the
   manifest with its reason: a transient failure after the last pass, a refused download, a file changed or removed
   in the cloud, a local error, an alias, a refused name, or an entry under a failed folder. A failed folder is
   recorded too. `--retry-failed` covers those files and the quarantined ones (the issue's wording). It also covers
   files with a `partial` download record, meaning downloads that started and never finished, such as one stopped
   by Ctrl+C. That matches the issue's parenthetical "the original tool's 'download everything unfinished'": after
   a Ctrl+C, `--retry-failed` resumes the interrupted files. Files never started are not covered; a plain `download`
   gets them.
2. **A plain `download` still retries every non-quarantined failure.** That is today's behaviour ("a rerun retries
   exactly what is not verified yet"). Only quarantine holds a file back. `--retry-failed` exists so the user can
   retry quarantined files, and retry the failures of a large mirror without walking or starting the rest.
3. **A held quarantined file makes the run exit 1.** A plain run lists it under `Quarantined`, counts it in the head
   line, and ends with `Error: …`. The mirror is incomplete, and cron or systemd must see that. A Disk copy that is
   permanently corrupt therefore makes every plain run exit 1 until the user deals with it. The same holds for #14's
   files that never start.
4. **Quarantine is per Disk version.** A quarantine record holds back only the version (size, checksums, mtime) it
   was made for. Once a rescan shows the file changed on the Disk, a plain `download` fetches the new version as
   usual. The record is dropped when the file verifies, and replaced when the new version is quarantined too.
5. **Every quarantined copy is kept, with no de-duplication.** A `--retry-failed` that fails verification again
   adds another copy under that run's quarantine folder. Copies pile up only through explicit retries. The README
   says the user deletes them after checking. Weighed against this: removing a fresh copy whose SHA-256 equals an
   earlier copy that is still in place. It saves space for a Disk file that is deterministically corrupt, but it is
   a deletion rule with an "is the old copy still intact" check. It is listed under Risks and not built.
6. **`verify` checks local integrity against the manifest only.** It needs no network or token, and it compares
   each verified file with the version it was verified against. Cloud changes since the last scan are `download`'s
   job: `verify` reports a file whose verified version is older than the manifest's entry as not downloaded, without
   reading it.
7. **`verify` reports files that are not downloaded.** It lists entries with no current verified copy (never
   downloaded, failed, partial, an older version, or a name that is not valid Unicode) and quarantined entries, and
   they make the exit code 1. #9's "done when" is `verify /mnt/backup  # exit 0` on a complete mirror. Exiting 0
   while files are missing would make that check meaningless.
8. **Findings send the file back to `download`.** `verify` records each missing, modified or corrupt file in the
   manifest. `download` treats a file with a finding as not current. It does not hash the copy there, since that
   copy is known to be bad and may be unreadable (#16). It downloads the file, moves the bad copy aside to
   `replaced/` once the new copy is verified, and clears the finding. A finding belongs to the entry, not to its
   download record. A repair that fails, is deferred or is interrupted therefore keeps it, so no later turn reads
   the bad copy. `verify` never repairs anything itself.
9. **`verify` is sequential.** It reads one file at a time, in path order, on the calling thread. That suits a USB
   HDD, where parallel reads only add seeks.
10. **`verify` resumes.** An interrupted pass (Ctrl+C or a kill) continues on the next `verify`: the manifest keeps
    the pass's start and the path of the last file checked. `--restart` starts a new pass instead. The final report
    covers the whole pass, because findings are stored per file.

## Non-goals

- #14 (files whose download never starts). See Risks for the recommendation: such files are already ordinary
  transient failures, so this plan records them and retries them with `--retry-failed`. The distinct "never started
  sending" reason, the "unavailable on Yandex Disk" wording and the preview fallback stay in #14.
- SIGTERM handling (#15). `verify` handles Ctrl+C the way `download` does. #15 should cover `verify` too.
- Live progress, the log file, and `--json` (#6). `verify` prints a start line and the summary. The new report
  fields (quarantined, verify findings) are what #6's JSON will carry.
- Deleting or pruning anything: quarantined copies, replaced copies, or local files removed from the cloud.
- Checking folders or empty folders in `verify` (the issue says "local files").
- Parallel hashing in `verify`, and a `verify` of one sub-folder.
- An `EIO` while reading a copy `verify` has not marked (#16).

## Exit-code contract (extends #3/#4)

| Code | `download` | `verify` |
|---|---|---|
| 0 | Every file of the run's scope is verified at its final path; nothing failed and nothing is quarantined | Every file of the mirror is verified and intact |
| 1 | As today, plus one or more files quarantined (newly or held). The summary is printed first, then `Error: F failed; see the list above.`, `Error: Q quarantined; see the list above.` or `Error: F failed and Q quarantined; see the lists above.` | The summary is printed first, then `Error: N problems found; see the lists above.` (N = missing + modified + corrupt + quarantined + not downloaded). Also a manifest that is unusable or in use. |
| 2 | As today | DEST is not an existing folder; no mirror at DEST is recorded in the manifest |
| 130 | Ctrl+C | Ctrl+C: `Interrupted; rerun verify to continue where it stopped.` |

## Stages

Each stage appends one manifest migration: 5, 6, 7, 8. Migrations already written are never edited.

### Stage 1: Failure records and `download --retry-failed`

- **Contract:**
  - **Migration 5:**
    ```sql
    CREATE TABLE failures (
        mirror_id INTEGER NOT NULL REFERENCES mirrors (id) ON DELETE CASCADE,
        entry_id INTEGER NOT NULL REFERENCES entries (id) ON DELETE CASCADE,
        reason TEXT NOT NULL,      -- the user-facing reason the entry's last turn in this mirror failed
        PRIMARY KEY (mirror_id, entry_id)
    )
    ```
  - **Manifest API:**
    - `MirrorEntry.failure: str | None` is read by `Mirror.entries()`.
    - `Mirror.record_failure(entry_id: int, reason: str) -> None` replaces any earlier reason.
    - `Mirror.clear_failure(entry_id: int) -> None` is a no-op when there is none.
    - `record_verified` deletes the entry's failure in its own transaction. `forget` does not touch it.
  - **When failures are recorded.** Every entry the run reports under `Failed` gets a record with the reported
    reason. That includes a deferred file after the last pass (with `(after N passes)`), a folder that could not be
    made or moved aside, and each entry under such a folder. Records are written as the failures happen, so a run
    stopped later keeps them.
    - **Not recorded:** unencodable entries, which have no entry id. They are reported in every run, retries
      included. Files cancelled by Ctrl+C or by a run-stopping error are not recorded either.
    - **Cleared when the entry succeeds:** a file verified or adopted (through `record_verified`), and a folder
      made or found in place (`clear_failure`).
  - **`download --retry-failed`** (a mode flag like `--no-scan`, not a registry option).
    - **Scan.** The scan policy is unchanged.
    - **Retry set.** It is fixed once the scan step is done: the file entries of the mirror with a failure record or
      a `partial` download record, plus the folder entries with a failure record.
    - **The walk.** It visits only the retry set and the folders above its members, in path order, with the usual
      rules (folders made first, things in the way moved aside, names converted, blocked subtrees). It never looks
      at any other path. That includes `MirrorRun.__init__`: in retry mode, the `_is_current` pass that `lstat`s
      every entry (`mirroring.py:352`) covers only retry-set members.
    - **Passes.** Retry set members go through the usual turns and passes.
    - **Start line:** `Retrying <n> failed or unfinished file(s) (<size>) in <DEST>`, in place of
      `Downloading …`.
    - **Summary.** The summary and the exit code are those of a normal run, over the retry set only.
    - **Empty retry set.** The start line reads `Retrying 0 …`, no file request is made, and the run exits 0 unless
      unencodable entries fail it.
  - **`MirrorRun(mirror, destination, *, retry_failed: bool)`.** `pending_files` and `pending_size` describe the
    files this run will attempt.
- **Behaviour to lock** (extend `tests/test_download.py` and `tests/test_manifest.py`):
  - **Retry covers only failures.**
    - Setup: `readme.txt` gets `503` on every `/download` with `--download-passes 1`, and the run exits 1. Then a
      new cloud file `disk:/New/n.txt` is added.
    - `download --retry-failed --scan-max-age 0`, with the fault removed, exits 0.
    - Link and content requests go to `readme.txt` only. `New/n.txt` does not exist locally.
    - The summary says `1 downloaded`.
  - **A second `--retry-failed`** prints `Retrying 0 failed or unfinished files (0 B)`, makes no `/download`
    request, and exits 0.
  - **Interrupted files are retried.** After the SIGINT case of
    `test_ctrl_c_mid_file_checkpoints_every_byte_written_and_the_rerun_resumes_there`, `--retry-failed` resumes
    the big file from its journal (`range: bytes=<held>-…`) and fetches nothing else.
  - **A failed folder is retried with its files.** Use `_refuse_names` so that a folder's name and its conversion
    are both refused: the folder and its files fail, and both are recorded. Then remove the patch:
    `--retry-failed` creates the folder and its files, exits 0, and a further `--retry-failed` covers nothing.
  - **Unencodable entries** still fail a `--retry-failed` run (extend
    `test_a_name_that_is_not_valid_unicode_fails_that_entry_only`).
  - **`test_manifest.py`:**
    - A failure round-trips through `entries()`.
    - `record_failure` replaces the reason. `record_verified` clears it, `forget` keeps it, and removing the entry
      in a rescan deletes it.
    - **Upgrade.** A new fixture `tests/data/manifest-v4.sql`, a `.dump` made with the code before this stage
      holding one partial and one verified record, upgrades to version 5 with no failures. The v1 and v3 upgrade
      tests now end at 5.
- **Deliverable:** failures persist per mirror, and `--retry-failed` reruns exactly the failed and unfinished files.
- **Validation:** `mise run check`;
  `mise run test -- yandex-disk-rescue/tests/test_download.py yandex-disk-rescue/tests/test_manifest.py`.
- **Doc updates:**
  - **Root `README.md` "Downloading":**
    - Add `--retry-failed` to the option sentence.
    - New paragraph **Retrying failures** as current state: what is recorded, what the retry set is, that the
      other files are neither looked at nor counted, and the start line.
    - Rewrite the Summary paragraph's "a rerun retries exactly what is not verified yet" so it names both modes.
  - **Package `README.md` "Manifest":** a bullet on failure records. Update the `mirroring.py` layout line (the
    retry set).

### Stage 2: Quarantine of copies that fail verification

The first test of this stage is the reproduction: the rewritten corrupt-download test below fails on the current
code, which deletes the bytes (`work dir: ['lock', 'partial'] []`) and fetches the file again on the rerun.

- **Contract:**
  - **Migration 6:**
    ```sql
    CREATE TABLE quarantined (
        mirror_id INTEGER NOT NULL REFERENCES mirrors (id) ON DELETE CASCADE,
        entry_id INTEGER NOT NULL REFERENCES entries (id) ON DELETE CASCADE,
        size INTEGER NOT NULL CHECK (size >= 0),  -- size, md5, sha256, modified: the Disk version the copy was for
        md5 TEXT,
        sha256 TEXT,
        modified TEXT NOT NULL,
        copy_path TEXT NOT NULL,                  -- the quarantined copy, relative to the destination
        reason TEXT NOT NULL,                     -- the verification failure, e.g. "MD5 mismatch (expected …, got …)"
        PRIMARY KEY (mirror_id, entry_id)
    )
    ```
  - **Manifest API:**
    - `@dataclass(frozen=True) class Quarantine: identity: RemoteIdentity; copy: PurePosixPath; reason: str`.
    - `MirrorEntry.quarantine: Quarantine | None`.
    - `Mirror.record_quarantined(entry_id, identity, copy, reason) -> None`, in one transaction: it upserts the
      quarantine record and deletes the entry's download record (with its segments) and its failure record.
    - `record_verified` also deletes the quarantine record.
  - **`Destination.quarantine(entry_id: int, local: PurePosixPath) -> PurePosixPath`.**
    - It moves the entry's partial file to `.yandex-disk-rescue/quarantine/<run start>/<local>` with
      `RENAME_NOREPLACE`, creating the folders on the way, and makes the move durable. It returns the copy's path
      relative to DEST.
    - The run folder is created on first use, with the same stamp format and `-2`, `-3`… suffix rule as `replaced/`.
      `Destination.quarantine_folder` is set then.
    - When the filesystem refuses the file's name (`NAME_ERRNOS`), the copy gets the `rejected_name_conversion` of
      that name.
    - It runs under the placement lock, like a move aside.
  - **When a copy is quarantined.** It happens on any verification mismatch, of a one-stream digest or of a
    segmented read-back. It replaces `_discard`. The steps run in this order:
    1. The partial file gets the Disk mtime and is fsynced (`PartialFile.finish`).
    2. It is moved to quarantine.
    3. `record_quarantined` runs.

    A crash between steps 2 and 3 leaves an unrecorded copy in `quarantine/`, and the next run downloads the file
    again. Bytes are never lost. A crash before step 2 leaves the partial file and its journal, as today. Nothing at
    the final path is touched, and an old local copy there stays.

    Other failures keep their current handling: the resume-time re-hash mismatch (a segment reset), changed or
    removed in the cloud, and a range not honoured.
  - **Plain `download`.**
    - **Held.** A file whose quarantine record's identity equals the entry's current identity is held. It is not
      looked at (no `lstat`, no request) and not counted in `Downloading N files`. It is reported as quarantined.
    - **Changed on the Disk.** A record for another identity does not hold the file: it is downloaded as usual.
  - **`--retry-failed`.** The retry set also includes every file with a quarantine record.
  - **Report and summary.**
    - **Report.** `MirrorReport` gains `quarantined: list[QuarantinedFile]` (remote path, reason, copy path), in
      path order, holding both this run's new copies and the held ones.
    - **Head line:** `…: D downloaded (S), K skipped, Q quarantined, F failed`.
    - **List.** `Quarantined: Q` lists, per file, the remote path, the reason and the copy path (its stamp tells
      which run made it), then the line `Download them again with --retry-failed.`
    - **Exit.** As in the exit-code table.
  - Quarantined files are not failures: they get no failure record and do not appear under `Failed`.
  - **Precedence.** A file can hold both records, for example when a `--retry-failed` of a quarantined file failed
    transiently. Quarantine then wins everywhere. A plain `download` holds the file (identity rule above) and lists
    it only under `Quarantined`, and `verify` lists it only under `Quarantined`. The line shows the quarantine
    reason followed by `; last retry: <failure reason>`. This rule is about a failure record versus a quarantine
    record only. A `verify` finding (Stage 3) describes the copy at the final path and wins in `verify`'s lists:
    such a file is listed under its finding as `<finding>; repair quarantined: <quarantine reason>; kept at <copy>`, while a plain
    `download` still holds it and lists it under `Quarantined`.
- **Behaviour to lock** (`tests/test_download.py`, `tests/test_manifest.py`):
  - **Rewrite `test_a_corrupted_download_fails_that_file_and_a_rerun_fetches_it[one-stream|segmented]`** as
    `test_a_corrupted_download_is_quarantined_and_fetched_again_only_with_retry_failed`:
    - **First run:** exit 1. The summary has `1 quarantined, 0 failed` and the `Quarantined: 1` list.
      `quarantine/<stamp>/Photos/2024/a.jpg` holds exactly the corrupted bytes, with the Disk mtime. The final path
      is absent, `partial/` is empty, and the journal is empty.
    - **Plain rerun with `--scan-max-age 0`:** exit 1, no link or content request for the file, and the file is
      listed as quarantined.
    - **After `repair`, `--retry-failed`:** exit 0. The file is verified at its final path, the quarantined copy is
      unchanged, and a further plain run exits 0 with no `Quarantined` line.
  - **A retry that fails again** adds a second copy under a new run folder; the first copy is untouched.
  - **A new Disk version is not held.** After a quarantine, change the file on the server and rerun with
    `--scan-max-age 0`: it is downloaded, verified and exits 0.
  - **`test_a_file_changed_in_the_cloud_replaces_the_old_copy_only_once_verified[corrupt]`:** the old copy stays at
    its final path, untouched. The bad new copy is in `quarantine/` and `replaced/` does not exist.
  - **`test_a_corrupt_file_is_not_retried_in_a_later_pass`:** the file is quarantined, its link is requested once,
    and the deferred file still verifies on pass 2.
  - **Crash between the move and the record.** `Mirror.record_quarantined` is patched to raise `KeyboardInterrupt`,
    which stands in for a kill at that point. The copy is in `quarantine/`. The next run (after `repair`)
    downloads and verifies the file, and the copy is still there.
  - **`test_manifest.py`:** a quarantine round-trips through `entries()`. `record_quarantined` drops the download
    record, its segments and the failure. `record_verified` drops the quarantine. The v4 upgrade test ends at 6.
- **Deliverable:** a copy that fails verification is kept in quarantine and fetched again only by `--retry-failed`
  or a new Disk version.
- **Validation:** `mise run check`;
  `mise run test -- yandex-disk-rescue/tests/test_download.py yandex-disk-rescue/tests/test_manifest.py`.
- **Doc updates:**
  - **Root `README.md` "Downloading":**
    - Rewrite **Verified.**: a copy that fails the check is quarantined, not deleted. Give the layout, that it may
      be the only copy of the data (the Disk's checksum can be the wrong part), that it is not downloaded again
      until `--retry-failed` or a new Disk version, and that the run exits 1.
    - Rewrite the **Passes** bullet's "A checksum mismatch … fail the file at once": a mismatch is quarantined.
    - Update the **Summary** example (head line counts, the `Quarantined` list).
    - Rewrite **Nothing is deleted.** and **The destination** so the work directory holds `quarantine/` too, and
      quarantined copies are only ever removed by the user.
    - Rewrite the **Retrying failures** paragraph to add quarantined files.
    - **Copies no record points to.** A copy whose file later verified, or whose record a new Disk version
      replaced, is no longer listed anywhere. It stays in `quarantine/<stamp>/` until the user removes it, and the
      user finds such copies by browsing those folders.
    - `rg -n 'deleted from there|fails the check|fail the file at once' README.md` must find no stale sentence.
  - **Root `README.md` "Exit codes":** the `1` row adds quarantined files.
  - **Package `README.md`:** a "Manifest" bullet on quarantine records. Update the layout lines of `destination.py`
    (quarantine) and `mirroring.py` (quarantine instead of discard).

### Stage 3: `verify DEST`, and `download` repairs what it finds

- **Contract:**
  - **Migration 7:**
    ```sql
    CREATE TABLE verify_findings (
        mirror_id INTEGER NOT NULL REFERENCES mirrors (id) ON DELETE CASCADE,
        entry_id INTEGER NOT NULL REFERENCES entries (id) ON DELETE CASCADE,
        finding TEXT NOT NULL CHECK (finding IN ('missing', 'modified', 'corrupt')),
        reason TEXT NOT NULL,
        PRIMARY KEY (mirror_id, entry_id)
    )
    ```
    A finding is about *what stands at the entry's final path*, not about a download record, so it is keyed to the
    entry and survives every change of the download record: `record_partial`, `checkpoint`, `forget`, and
    `record_quarantined` (a quarantined repair leaves the bad copy at the final path). Exactly these clear it:
    - `record_verified`: a new copy was placed or adopted there;
    - `verify` finding the copy intact;
    - removing the entry (cascade).
  - **Manifest API:**
    - `Manifest.mirror_at(destination: Path) -> Mirror | None` looks a mirror up by its resolved destination,
      without creating one.
    - `MirrorEntry.finding: Finding | None`, with `Finding(kind: Literal['missing', 'modified', 'corrupt'],
      reason: str)`.
    - `Mirror.record_finding(entry_id, finding)` and `Mirror.clear_finding(entry_id)`, for any committed entry of
      the mirror, whatever its download record.
  - **Read-only destination.** `Destination.open_read_only(path) -> ReadOnlyDestination` offers `lstat`,
    `existing_file` and `reachable()` through the same no-follow descriptor walk. It creates, modifies, moves and
    deletes nothing in DEST. Reading a file may update its access time, as any read does, unless DEST is mounted
    `ro` or `noatime`. `O_NOATIME` is not used: it fails with `EPERM` on files the user does not own.
    - When `DEST/.yandex-disk-rescue/lock` exists, it is opened read-only and `flock`ed exclusively for the whole
      verify. The busy message is the same as `download`'s.
    - A missing work directory is not an error, since no run can be using DEST.
    - A DEST that is not an existing folder raises `DestinationUsageError` (exit 2).
  - **CLI:** `yandex-disk-rescue verify DEST`. It takes no token and no API option, and makes no network request.
    1. It resolves DEST and opens the manifest, which takes the run lock and applies migrations. It finds the
       mirror with `mirror_at`. When there is none, it exits 2 with
       `<DEST> is not a mirror recorded in the manifest <path>; nothing to verify.` Then it opens DEST read-only.
    2. It prints `Verifying <n> file(s) (<size>) in <DEST>`.
    3. It walks the mirror's file entries in path order, one at a time, on the calling thread. Folders are not
       checked.
  - **What each file is.**
    - **Checked.** A file with a verified record for the entry's current identity has its final path `lstat`ed.
    - **Missing:** nothing is there, or a folder on the way is missing or is a link.
    - **Modified:** the path holds something other than a regular file (`<local> is no longer a regular file`),
      or the fingerprint differs from the record and the content does not match.
    - **Otherwise hashed.** The size, then every checksum the record has. On a match the file is *intact* and any
      earlier finding is cleared; the fingerprint is not rewritten.
    - **Corrupt:** the fingerprint equals the record and the content does not match (the mismatch text), or the
      file could not be read (`could not read <local>: <strerror>`).
    - **A read error when the disk may be gone.** Before a read error is recorded as corrupt, `verify` calls
      `reachable()`, which re-checks the destination itself: a fresh `lstat` of DEST by its path and a listing of
      the root folder through a new descriptor.
      - **When that fails too, or the errno is `ENODEV`, `ENXIO` or `ESTALE`,** `verify` stops with
        `Error: Could not read <DEST>: <reason>; verify stopped. Rerun it once the disk is back.` and exits 1.
        It records nothing for that file and does not advance the Stage 4 cursor past the previous file. Findings
        committed before then stay. So a USB disconnect never turns the rest of the mirror into "corrupt" findings
        that `download` would then re-fetch.
      - **Otherwise** the file is recorded as corrupt, and the walk goes on.
    - **Not looked at.** Any other file entry is listed without being read:
      - an entry with a finding is listed under that finding, since the bad copy is still at its path (for
        example, a repair that has not finished);
      - else an entry with a current quarantine record is *quarantined*, with its reason and copy path;
      - else it is *not downloaded*: no record, a partial record, a failure (its reason is shown), or a verified
        record for an older identity (`changed on the Disk since it was downloaded`).
      - Unencodable entries are *not downloaded*, with their usual reason.
    - **Never touched.** Paths with no manifest entry are never opened, listed or reported.
  - **Findings.** Each finding is committed as soon as it is found.
  - **Summary:**
    - **Head line:** `Verified <DEST> (<remote root>): I intact, M missing, D modified, C corrupt, Q quarantined,
      N not downloaded`.
    - **Size-only files** are listed under `Checked by size only (no cloud checksum): k`.
    - **Then** the lists `Missing`, `Modified`, `Corrupt`, `Quarantined` and `Not downloaded`, each with reasons,
      in path order, at most 20 shown (`listed`).
    - **Then** `Manifest: <path>`, and the exit as in the table.
  - **`download` repair.**
    - A file with a finding is never current, whatever its download record says.
    - **Every turn** of such a file, in any pass and any run until the finding is cleared, does the `_alias` check
      and skips adoption: the copy at the path is never hashed. It downloads (resuming a journal as usual), moves
      whatever stands at the path aside once the new copy is verified, and places it.
    - `record_verified` clears the finding. A repair that fails, is deferred, is interrupted or is quarantined
      keeps it.
- **Behaviour to lock:** a new `tests/test_verify.py`, since no existing file covers this command. The `disk` and
  `dest` fixtures and the `_download` and `_local` helpers it needs move from `test_download.py` to `conftest.py`
  (or a shared test-helper module) rather than being copied.
  - **No network.** After a full `download`, with `YANDEX_DISK_RESCUE_TOKEN` unset and `YANDEX_DISK_RESCUE_API_URL`
    pointing at `closed_port_url`, `verify` exits 0 with `6 intact` (`SIZE_ONLY` is listed by size only).
  - **Each finding.** One byte flipped with size and mtime kept gives *corrupt* with the MD5 mismatch text. An
    edited file gives *modified*. A deleted file gives *missing*. A file replaced by a symlink to an outside file
    gives *modified* with `… is no longer a regular file`, and the link and its target are unchanged. Exit 1, and
    stderr is `Error: 4 problems found; see the lists above.`
  - **Read error.** `localfs.read` is patched to raise `EIO` for one file. That file is *corrupt*
    (`could not read …: Input/output error`), and the other files are still checked.
  - **Disk gone mid-verify.** `localfs.read` raises `EIO` for every file from the third one on, and `reachable()` is
    patched to fail once the first `EIO` is seen. `verify` exits 1 with the `verify stopped` message, and the
    manifest holds no finding at all. After the patches are removed, a rerun exits 0 with every file intact. Stage 4
    adds that it continued from the cursor.
  - **Not downloaded and quarantined.** The setup holds a failed file (503 with `--download-passes 1`), a
    quarantined one, and a cloud file added after the download (rescan with `scan`). Each appears in its list with
    its reason, and the exit is 1.
  - **Local-only content and no writes.** The `local_only` tree of `test_local_only_content_is_never_touched_or_reported`
    is never mentioned. A snapshot of DEST is identical before and after: every path, its kind, size, mode,
    inode, `st_mtime_ns`, `st_ctime_ns` and content, but not `st_atime`. The ctime catches any metadata write. That
    snapshot is the "no writes" lock. A `chmod -R a-w` test is left out, because CI runs pytest as root, where it
    would prove nothing.
  - **Repair.** After `verify` finds a corrupt, a modified and a missing file, a `download --no-scan` downloads
    exactly those three. The corrupt and modified copies end up byte-identical under `replaced/<stamp>/`. A second
    `verify` exits 0.
  - **Unreadable copy is not re-read by `download`.** `localfs.read` raises `EIO` for that file throughout. After
    `verify` marks it, `download --no-scan` exits 0 with the file moved aside and replaced, and no read of the old
    copy is attempted.
  - **The finding survives a failed repair.** The setup is the same unreadable copy, marked by `verify`.
    1. `download --no-scan --download-passes 1` with `503` on every `/download` of that file: exit 1. The finding
       is still in the manifest, and the old copy was never read.
    2. Rerun with the fault removed: exit 0, still no read of the old copy, and the copy is under `replaced/`.
    3. A Ctrl+C variant of step 1 behaves the same: the finding survives the interrupted turn.
  - **Usage errors.** A DEST that does not exist, or is a file, exits 2. An existing folder with no recorded mirror
    exits 2 with the message above.
  - **Busy destination.** `verify` holds the destination lock: a `download` into the same DEST during it fails with
    `Another yandex-disk-rescue run is using …`. Use an in-process `flock` probe, as
    `test_a_destination_in_use_by_another_run_is_refused_whatever_the_manifest` does.
  - **`test_manifest.py`:**
    - `mirror_at` finds a recorded mirror and returns `None` otherwise.
    - A finding round-trips through `entries()`.
    - `record_partial`, `checkpoint`, `forget` and `record_quarantined` keep the finding. `record_verified`,
      `clear_finding` and removing the entry drop it.
    - The upgrade test ends at 7.
- **Deliverable:** `verify DEST` reports every missing, modified, corrupt, quarantined and not-downloaded file with no
  network, and `download` repairs what it found.
- **Validation:** `mise run check`;
  `mise run test -- yandex-disk-rescue/tests/test_verify.py yandex-disk-rescue/tests/test_download.py yandex-disk-rescue/tests/test_manifest.py`.
- **Doc updates:**
  - **Root `README.md`:**
    - A new section **Verifying a mirror** after "Downloading", written as current state: what it reads, the
      categories, that it needs no token and no network and creates, modifies, moves and deletes nothing in DEST
      (reads may update access times), the read-only mount, that it stops when DEST itself becomes unreadable,
      local-only content, the summary example, that `download` fixes what it found, and the time it takes (it reads every
      byte, so 2 TiB on a USB disk takes hours).
    - Update the Status blurb and the Exit-code rows for `verify`.
    - The "Reruns" paragraph of "Downloading" says a file `verify` found broken is downloaded again.
  - **Package `README.md`:** the usage line `uv run yandex-disk-rescue verify /tmp/photos`, the Manifest bullet
    (findings, `mirror_at`), and layout lines for `commands/verify.py` and the verify engine module (for example
    `verifying.py`), plus the `destination.py` line (read-only open).

### Stage 4: Resumable `verify`

- **Contract:**
  - **Migration 8:**
    ```sql
    CREATE TABLE verify_passes (
        mirror_id INTEGER PRIMARY KEY REFERENCES mirrors (id) ON DELETE CASCADE,
        started_at TEXT NOT NULL,
        checked_through TEXT        -- the Disk path of the last file whose check is committed; NULL before the first
    )
    ```
  - **Manifest API:**
    - `Mirror.verify_pass() -> VerifyPass | None`.
    - `Mirror.start_verify_pass() -> VerifyPass` replaces any existing pass.
    - `Mirror.checked_through(path: str)`.
    - `Mirror.end_verify_pass()`.
  - **Behaviour.**
    - **Starting or continuing.** `verify DEST` with no pass row starts one. With a pass row, it continues it: it
      checks only files whose path sorts after `checked_through` and prints
      `Continuing the verification started <YYYY-MM-DD HH:MM> UTC: <n> file(s) (<size>) left`.
    - **`verify --restart`** starts a new pass.
    - **Progress commits.** `checked_through` is committed at least every 5 s while files are checked, and on
      Ctrl+C. A finding is committed at once, as in Stage 3.
    - **Ctrl+C** prints `Interrupted; rerun verify to continue where it stopped.` and exits 130.
    - **A kill** re-hashes at most the files checked in the last 5 s, plus the one in flight.
    - **Completion.** The pass row is deleted only once every file has been checked.
    - **Report.** It always covers the whole pass. A file at or before `checked_through` counts by its stored state:
      intact unless it has a finding, and current-verified now. So the report of a resumed pass equals that of an
      uninterrupted one over the same files.
    - **`download` between two parts** of a pass is harmless. A file it placed or repaired at or before the cursor
      counts as intact, since it was just verified. One after the cursor is hashed.
- **Behaviour to lock** (`tests/test_verify.py`; subprocess, as the download SIGINT test does):
  - **Interrupt and resume.**
    - Setup: 40 files of 1 MiB, with reads slowed for the child by a small chunk size. Alternatively, use enough
      files that hashing takes more than 1 s.
    - SIGINT once the manifest's `checked_through` (read with a separate read-only `sqlite3` connection) is set:
      exit 130 and the stderr message.
    - Rerun: the `Continuing …` line names fewer files than the total, and the exit code and every summary line
      equal those of an uninterrupted `verify` of an identical mirror. The test includes one corrupt file before the
      interruption point and one after.
  - **`--restart`** after an interruption announces `Verifying 40 files` again.
  - **A completed pass** leaves no `verify_passes` row, and the next `verify` starts a new pass and re-hashes
    everything.
  - **`test_manifest.py`:** the pass round-trips, `start_verify_pass` replaces it, deleting the mirror cascades, and
    the upgrade test ends at 8.
- **Deliverable:** a multi-hour `verify` survives Ctrl+C and kills, losing seconds of work.
- **Validation:** `mise run check`; `mise run test -- yandex-disk-rescue/tests`; `mise run ci`.
- **Doc updates:**
  - **Root `README.md` "Verifying a mirror":** resuming, `--restart`, what a kill costs, and that the summary covers
    the whole pass.
  - **Package `README.md` "Manifest":** the verify pass row.

## Manual verification (main runs this with the user; not in CI)

The live full-account run (built from `main`) uses `~/.local/state/yandex-disk-rescue` and `/mnt/oblako`. This
branch adds migrations 5–8, and opening the live manifest with it would upgrade it in place and break the running
build. Every step therefore uses a scratch `XDG_STATE_HOME`: a copy of the state directory taken while no run writes
it, or a fresh one. The steps also use a scratch `XDG_CONFIG_HOME` and a scratch destination, never `/mnt/oblako`,
until the live run has finished and this PR has merged.

1. Download a small real folder into a scratch DEST, then run `verify` (exit 0). Flip a byte with `dd conv=notrunc`
   and restore the mtime with `touch -r`. `verify` reports it as corrupt, and `download --no-scan` repairs it.
2. After the live run ends: run `verify /mnt/oblako` against a copy of the live manifest. Interrupt it after about
   10 minutes and resume it. Record the throughput (MB/s) in the PR description.
3. The #14 trial folder: run `download --retry-failed` and check that only the dead files are retried. They still
   fail stalled, so that is #14's job.

## Discovered issues

- **Fix in this PR:**
  - A verification failure destroys bytes that may be the only copy of the data (Stage 2).
  - Silent bit rot of a verified file was undetectable and unrepairable (Stage 3).
- **Filed as issues:**
  - #16: an `EIO` while reading an existing local copy stops `download` with the misleading
    `Could not write <path>: Input/output error.`, and one bad sector blocks every later run. This plan's `verify`
    plus repair route avoids reading a copy it marked. The unmarked case and the wording are #16's.
- **For main to relay (issue edits, not new issues):**
  - #6: the JSON summary gains the quarantined counts and list, and `verify`'s report. Live progress should cover
    `verify` too, which runs for hours.
  - #15: SIGTERM handling should cover `verify` as well (the same message and checkpoint semantics as its Ctrl+C).
  - #14: its "report these files distinctly" can refine the failure reason this plan records. `--retry-failed`
    already retries them cheaply.

## Risks / open questions

- **#14 recommendation.** Files whose download never starts fit this ticket's model unchanged. They are transient
  failures recorded with `the download stalled: no data for 15 s (after 7 passes)`, retried by every plain run and
  by `--retry-failed`, and reported by `verify` as not downloaded with that reason.
  - **Why not here.** A distinct "the Disk never started sending this file" reason is not a trivial fit. It needs
    "no byte in any pass" tracked per file across passes, and probably across runs, plus the wording and README
    decisions #14 owns (and its optional preview fallback). So it is left to #14, which can build on the failure
    record (for example, a column counting runs without a byte).
- **Quarantine growth.** Decision 5 keeps every copy. A 16 GiB file that is deterministically corrupt costs 16 GiB
  per explicit `--retry-failed`. The alternative, de-duplicating by SHA-256 against a copy still in place, is a
  deletion rule. It is left for the user to decide.
- **Exit 1 forever.** Decisions 3 and 7 make plain `download` and `verify` exit 1 while any file is quarantined or
  undownloadable. That is honest, but a cron user must read the list. An "accept this loss" command is a possible
  follow-up and is not designed here.
- **Clock and cursor.** The resume cursor is a path, not a time, so a clock change cannot skip files. `started_at`
  is only shown to the user.
- **`verify` holds both locks for hours.** No `download` with the same manifest or into the same DEST can run
  meanwhile. That is intended, since both would compete for the disk, and the README says so.
- **Migrations on the live manifest.** Opening `~/.local/state` with this branch upgrades it to v8, and older builds
  then refuse it. Manual steps use copies (see above).
- **A disconnected disk mid-verify.** Without the `reachable()` check, a USB disconnect would mark every remaining
  file corrupt, and the repair rule would then re-download TiBs and move good copies aside. The check stops `verify`
  instead. What is left is a disk that fails one file at a time while the root stays readable (bad sectors spread
  over many files). Each such file is then reported corrupt, which is accurate, and its repair is a real re-download.
- **Access times.** On a `relatime`/`strictatime` mount, `verify` updates the atime of the files it reads. This is
  stated in the README rather than avoided with `O_NOATIME`, which fails with `EPERM` on files the user does not own
  (copies made by hand, other uids).
- **Fingerprint not refreshed by `verify`.** A touched-but-identical file stays "not current" for `download`, which
  hashes and adopts it once. It is cheap and keeps `verify` from writing trust it did not establish via `download`'s
  rules (the mtime is not reset).
- **Timing-based subprocess test** (Stage 4). If CI flakes, raise the file count or slow the child's reads. Do not
  switch to mocks.
