# Plan: Partial files on a separate (fast) filesystem

Issue: #20 (related: #18)
Branch: feat/partial-dir (from `main` at a3ffe7b). Merge order: #20 merges first; #6
(`feat/progress-logging-json`) pauses after its Stage 2 and is rebased onto #20 afterwards.

## Context

Observed on `main` (a3ffe7b), with scratch XDG dirs and the fake server only:

- `uv run yandex-disk-rescue download --partial-dir /var/tmp x` gives `No such option: --partial-dir` (exit 2).
  The work directory is fixed. `Destination.open` creates `DEST/.yandex-disk-rescue/partial/` and probes the
  no-replace rename there. `Destination.partial(entry_id)` opens `<that folder>/<entry_id>`, and `Destination.place`
  renames that file to its final path on the same filesystem.
- Every byte of in-flight work goes to DEST: the `pwrite`s of up to `download_connections` files at once, each
  checkpoint's fsync (`_Journal.take` → `PartialFile.sync`), the zero-fill of `_preallocate` on HFS+ (#18), the
  read-back of a segmented file (`_file_digest`), and the resume re-hash (`_Stream.rehash` in `_resume`), which runs
  in parallel, one reader per connection.
- `uv run pytest yandex-disk-rescue/tests/test_download.py -k "partial or full_or_failing or killed or in_use"`:
  20 passed. These tests lock today's behaviour for resume, journals, ENOSPC and the run lock. They read partial
  files from `dest / '.yandex-disk-rescue' / 'partial'` (`_partials`).
- Test environment: `tmp_path` is under `/tmp` (tmpfs, `st_dev` 107 here; overlay in the Dagger container).
  `/dev/shm` is a separate 64 MiB tmpfs (`st_dev` 146). So the tests can use a real second filesystem.
- Measured on the user's machine (from the brief, the issue and the critic's review of the live state): DEST is an
  HFS+ USB HDD. At 16 connections it is 99% busy at 2–3 MB/s, and a restart re-reads the confirmed bytes of the
  partials before it downloads anything. The partials there take about 75 GiB on disk, because HFS+ zero-fills a
  segmented file to its full size, but only about 12.5 GiB of that is confirmed by the journals. The local overlay
  disk does 1.2 GB/s sequential and 539 MB/s with 16 fsyncing writers, and has about 150 GB free. `/tmp` is a 16 GB
  tmpfs. The largest file is about 16.5 GiB; the account holds 133k files, 2.1 TiB.

## Goal

`download --partial-dir PATH` keeps partial files and all of their I/O in a folder of the destination's own below
`PATH`. That I/O is the writes, the checkpoint fsyncs, the preallocation, the read-back of segmented files and the
resume re-hash. The option is also `partial_dir` in the config file and `YANDEX_DISK_RESCUE_PARTIAL_DIR`.

The flow for each file:

1. The file is fetched and checkpointed into the separate folder.
2. A checkpoint makes the journal confirm every byte.
3. The file is copied to `DEST/.yandex-disk-rescue/partial/<id>.copy` as one sequential stream. Only one copy runs
   at a time in the whole run. The size/MD5/SHA-256 check runs over the bytes as they are copied.
4. The copy gets the cloud mtime and is fsynced.
5. The copy is placed with today's no-replace rename, or moved to `quarantine/` when the check fails.
6. The local partial file is deleted only once the outcome is recorded.

A crash at any point leaves the old state or a verified file at its final path, never a truncated one. The partial
files on the separate folder never take more than the room it had when the run started, minus a reserve. A file
that has to wait for room waits, a file that cannot get room in this pass is deferred, and a file that can never fit
fails with a reason. When the folder changes between runs (the option turned on, turned off, or `PATH` changed), the
confirmed bytes of each download in progress move to the new folder at the start of the run. The default (no
`--partial-dir`) is today's behaviour, byte for byte.

## Decisions that go beyond the issue's wording (for the user to approve)

1. **Option form.** A registry setting `partial_dir` (string): flag `--partial-dir` (`download` only), env var
   `YANDEX_DISK_RESCUE_PARTIAL_DIR`, config key `partial_dir`.
   - The default `''` means "in DEST", and an empty value from any source turns the option off, so
     `--partial-dir ''` overrides a config file.
   - The registry validates every source the same way: a value must be `''` or an absolute path. A relative path is
     refused even on the command line, because in `config.toml` it would silently depend on the working directory.
2. **`PATH` must be an existing folder outside DEST.**
   - It is a usage error (exit 2) when `PATH` is not an existing folder, or when it resolves to DEST or a folder
     below DEST (the tool's folder would then be a path of the mirror).
   - The tool creates only its own per-destination folder below `PATH`, so a typo cannot fill the wrong disk. That
     matches how DEST creates only its last component.
   - `PATH` on the same filesystem as DEST is allowed and copies all the same (correct, just wasteful).
   - The README says to use a persistent folder on another, faster disk: not a tmpfs, and not a container layer that
     a rebuild discards. If the folder is lost, the downloads in progress start again.
3. **One folder per destination: `PATH/<key>/`.** `<key>` is the first 16 hex digits of the SHA-256 of the resolved
   DEST path, so one `partial_dir` in `config.toml` serves every destination. Two mirrors of the same account share
   entry ids, but they never collide on `<entry_id>` names. The key follows the DEST path, as the manifest's mirror
   identity already does. The folder holds only partial files.
4. **No lock of its own.** The DEST run lock already allows one run per DEST, and the per-DEST key keeps the folders
   of different DESTs apart. The case left uncovered is two DEST disks mounted at the same path on two machines that
   share one `PATH`. The README says not to share one `PATH` between machines.
5. **The copy on DEST is verified by hashing the bytes as they are copied, not by reading DEST back.**
   - The bytes read from the local partial file are hashed (size, MD5, SHA-256) and the same buffers are written to
     DEST. Then `fsync`, then the rename. This is the guarantee today's one-stream download gives on DEST (hashed in
     memory, written, fsynced, never read back), and it costs no extra HDD I/O.
   - With a separate folder this copy-time digest is the file's only check, for one-stream and segmented files
     alike, so a segmented file is no longer read back separately. `verify` re-hashes DEST later, as today.
6. **Copies to DEST are serialized for the whole run** (one in flight at a time), so DEST sees one sequential
   writer. A finished file waiting for the copy lock keeps its turn slot, so at most `download_connections` files
   are between "admitted" and "placed". That slows fetching when DEST cannot keep up, which also bounds the space
   used on the separate folder.
7. **A quarantined copy goes to DEST's `quarantine/`, never stays local.** It may be the only copy of the user's
   data, and the local disk is scratch space. The copy-time digest decides, and the staged copy is moved with
   today's quarantine rename.
8. **Staged copies have their own name: `DEST/.yandex-disk-rescue/partial/<id>.copy`.**
   - A staged copy is only ever a finished file being copied, or a copy cut short by a crash. It is never resumed.
   - `place` and `quarantine` rename from `<id>.copy` with separate partials and from `<id>` otherwise, so DEST's
     own partial files and staged copies can never be mistaken for each other.
   - At the start of every run, every staged copy is deleted, whatever folder the last run used. With partials in
     DEST, today's `keep_partials` already deletes any file whose name is not an in-progress id.
9. **Space admission on the separate folder.** It applies only with `--partial-dir`; DEST keeps today's behaviour.
   - **Capacity**, measured once when the first pass starts (after any move): free bytes of the folder's
     filesystem (`f_bavail × f_frsize`) + the bytes the partial files there already allocate (`st_blocks × 512`) −
     a fixed reserve of **1 GiB**.
   - **Counted total:** each idle partial file counts its allocated bytes. An idle file is one kept from an earlier
     run, or one whose turn ended with the file kept: deferred, failed, or stopped. An admitted turn adds its
     *reservation* = `max(0, size − allocated)` of its own partial file at admission (block rounding can make a
     small file allocate more than its size), so a running file counts its full
     size.
   - **When a turn ends:** its reservation is returned, and its file counts its allocated bytes (one `fstat`), or
     nothing once the file is deleted.
   - **Admission** is FIFO, including turns that resume a kept partial file. The head of the queue is admitted when
     `counted + reservation ≤ capacity`.
   - **A head that does not fit:**
     - if another admitted turn is still running, it waits;
     - if no admitted turn is running and the file's size alone exceeds the capacity, it fails with `larger than the
       room in the partial folder <folder> (<size> needed, <capacity> available)`;
     - otherwise it is deferred to the next pass with `no room yet in the partial folder <folder> (<reservation>
       needed, <free> available)`.
   - **Wake-up contract:** whenever an admitted turn ends, whatever its outcome, the head is checked again. When the
     head is admitted, deferred or failed, the next waiter is checked at once, and so on. A turn that joins an empty
     queue is checked at once.
   - **Termination:** a waiter waits only while some admitted turn is running, and every admitted turn ends. So no
     waiter holds its turn slot forever.
   - A real `ENOSPC` on the folder anyway (another program filled the disk) stops the run with `Error: Could not
     write <path>: No space left on device.`, as an ENOSPC in DEST's work directory does today. The journals keep the
     progress.
10. **The manifest records the partial folder of each mirror's downloads in progress** (schema v9:
    `mirrors.partial_folder`, `NULL` = DEST's own).
    - At the start of a run whose folder differs from the recorded one, each download in progress has **only its
      confirmed bytes** moved: each journal segment's `[start, confirmed)`, copied into a new file in the new folder.
      Unconfirmed bytes (zero-fill, data past the last checkpoint) are never read.
    - The new file ends at its last confirmed byte. The fetch's preallocation extends it as today, which is
      metadata-only on a sparse-capable filesystem. The critic suggested `ftruncate` to the old size; this choice
      avoids writing zeros when the target is DEST on HFS+.
    - The move goes one file at a time: write the ranges, fsync the file, fsync the target folder, delete the
      source, fsync the source folder. Afterwards the old folder's leftovers are deleted (for a separate folder, the
      folder itself too). Only then is the new folder recorded.
    - Turning the option on, turning it off and changing `PATH` all keep every confirmed byte. For the user's run,
      turning it on reads about 12.5 GiB of confirmed bytes from the HDD (not the ~75 GiB the files take). At HDD
      sequential speed that should take minutes, not the hour or more it takes to fetch them again at 3 MB/s. This
      is an estimate; manual verification measures it.
    - When the recorded folder is missing, nothing is moved, a line says so, and those downloads start again from
      their first byte. That is today's behaviour for a deleted partial file, because their journals no longer match.
11. **Output** (plain `typer.echo` lines, like `main` today):
    - With a separate folder, `Partial files in <folder>` follows the `Downloading …`/`Retrying …` line.
    - From Stage 3 on, that line reads `Partial files in <folder> (<capacity> available)`.
    - A move prints `Moving the confirmed bytes (<size>) of N downloads in progress from <old> to <new>` before it
      starts.
    - A missing old folder prints `The partial folder of the last run, <old>, is missing; its downloads in progress
      start again from their first byte.`

## Non-goals

- Re-planning a journal that confirms no bytes with the current `--download-segments` (#18 comment 1), skipping
  preallocation, or bounding the parallel resume re-hash on DEST without `--partial-dir` (#18 comment 2). With
  `--partial-dir` on a sparse-capable local filesystem, preallocation is a metadata-only `ftruncate` and the re-hash
  reads the local disk.
- Space admission for DEST: DEST's ENOSPC keeps stopping the run as today.
- Reading the copy on DEST back (Decision 5), and `copy_file_range`/`sendfile` (the bytes must pass through the
  hasher).
- Any change to `verify`: it never looks at partial files, takes no `--partial-dir`, and keeps working on a
  read-only DEST.
- Logging and progress states for the new phases (move, copy, waiting for the copy lock, waiting for room). #6
  adds them after its rebase.

## Stages

### Stage 1a: `--partial-dir`: fetch on a separate folder, copy, verify, place or quarantine on DEST

- **Contract:**
  - **Setting.** `settings.PARTIAL_DIR = Setting('partial_dir', '', <help>, <check: '' or an absolute path>)` and
    `settings.PartialDir`. `download` gains `partial_dir: settings.PartialDir`. `''` from any source means none.
  - **Checks.** `destination.check_partial_dir(raw: str, destination: Path) -> Path | None`. It returns `None` for
    `''`, and otherwise the resolved folder. It raises `DestinationUsageError` (exit 2) when `raw` is not an existing
    folder, or when it resolves to the destination or a folder below it. It creates nothing. The command calls it
    right after `check_destination`.
  - **Opening.** `Destination.open(path: Path, *, partial_dir: Path | None) -> Destination`. It does everything it
    does today, then, with `partial_dir`, creates `partial_dir/<key>/` if missing (Decision 3).
    - When `<key>` exists and is not a real folder, it fails with `DestinationError` (`… it is not a folder; move it
      away and run again.`, as `_work_dir`).
    - Other errors creating or opening the folder raise `DestinationError.of(<folder>, exc)`.
    - `Destination.partial_folder: Path` is the folder partial files live in (DEST's `partial/`, or `<folder>`).
      `Destination.separate_partials: bool` says which.
  - **Partial file operations.** `Destination.partial`, `remove_partial` and `keep_partials` act on
    `partial_folder`. With separate partials, `remove_partial(entry_id)` also deletes `DEST/…/partial/<id>.copy`.
  - **Copy target.** `Destination.copy_target(entry_id)` is a context manager, used only with separate partials. It
    opens `DEST/.yandex-disk-rescue/partial/<id>.copy` for writing, truncated and created if missing. It yields an
    object with `write(offset, chunk)` and `finish(mtime_ns)` (mtime, then fsync).
    - Both sides follow `_work_errors`' fatal-errno rule: ENOSPC/EDQUOT/EIO/EROFS/EACCES/EPERM on the separate
      folder or on the copy raise `DestinationError` naming the path.
  - **Placement.** `Destination.place(entry_id, local)` and `Destination.quarantine(entry_id, local)` keep their
    contracts. They rename the staged copy `<id>.copy` with separate partials, and `<id>` otherwise.
  - **Per-file pipeline with separate partials** (`_fetch_verified`):
    1. Fetch and checkpoint into the separate folder, unchanged.
    2. Take a checkpoint, so the journal confirms every byte.
    3. Under a run-wide copy lock (one copy at a time, never held together with `placement`), copy the local partial
       file into `copy_target` in order, chunk by chunk, one bounded thread call per chunk, hashing every chunk into a
       `Digest`. Then `finish(mtime)`.
    4. On a digest match: `place`, `record_verified`, then `remove_partial`.
    5. On a mismatch: `quarantine` under `placement`, `record_quarantined`, then `remove_partial`.
    6. Segmented files skip `_file_digest`'s read-back: the copy is their check.

    Without separate partials the pipeline is unchanged.
  - **Interim, removed in Stage 1b:** a staged copy left by a crash stays until the next copy of that entry
    truncates it.
  - **Interim, replaced by Stage 2's move:** DEST's own `partial/<id>` files are left alone when the option is
    turned on (those downloads start again in the separate folder).
- **Behaviour to lock** (extend `yandex-disk-rescue/tests/test_download.py`; add the `partial_root` fixture to
  `conftest.py`):
  - **Fixture.** `partial_root` is a folder made with `tempfile.mkdtemp(dir='/dev/shm')` and removed after the test.
    It asserts that its `st_dev` differs from `tmp_path`'s, and never skips.
  - **A full mirror** with `--partial-dir <partial_root>`, `--download-segments 4`, the `disk` tree plus `SEGMENTED`
    and `BIG`: every file has its bytes and its cloud mtime, `<partial_root>/<key>/` is empty, and DEST's `partial/`
    is empty.
  - **Sequential writes on DEST.** Wrap `os.pwrite`, `os.write` and `os.ftruncate` to log `(path via
    /proc/self/fd, offset, length)`. Across that run, every write to a file below DEST is a write to a `.copy` file.
    Files are written one at a time, never interleaved, each at contiguous increasing offsets from 0, and no
    `ftruncate` touches a file below DEST.
  - **Resume reads the separate folder only.** Run `_leave_partial` with `--partial-dir`, then rerun with reads
    wrapped the same way. No file below DEST is read before the copy starts, the content request is the tail
    `Range`, and the bytes are right.
  - **Quarantine.** The corrupted-download test is parametrized over `[in-dest, separate]`, one-stream and
    segmented. With `separate`, the copy lands in `DEST/.yandex-disk-rescue/quarantine/<run>/…` with the corrupted
    bytes, the local folder is empty, and `--retry-failed` behaves as today.
  - **Usage errors.** `--partial-dir` that is relative, missing, a file, equal to DEST, or below DEST: exit 2, and
    nothing created in DEST or `PATH`. This extends `test_an_unusable_destination_is_a_usage_error_that_creates_nothing`
    and the option-validation tests. The config key round-trips in `test_config.py`'s registry tests.
  - **`verify`** (`yandex-disk-rescue/tests/test_verify.py`): a mirror made with `--partial-dir` verifies intact
    with no option, and `verify` never opens the separate folder (its mtime is unchanged).
- **Deliverable:** the option works end to end for fresh downloads, resumes and quarantine, and the default is
  unchanged.
- **Validation:**
  - `mise run check`
  - `mise run test -- yandex-disk-rescue/tests/test_download.py -k "partial or quarantin"`
  - `mise run test -- yandex-disk-rescue/tests/test_verify.py`
  - `mise run ci` (confirms `/dev/shm` is a separate filesystem in the Dagger container)
- **Doc updates:**
  - Root `README.md`, rewritten as current state:
    - *Downloading*: add the option to the option list, and add a **Partial files** paragraph covering the
      per-destination folder and its key, the copy and its check, one copy at a time, quarantine to DEST, and a
      persistent folder (Decision 2), not shared between machines.
    - *Verified*: "until then it is written to the partial folder", and the check runs over the bytes copied to DEST.
    - *Nothing is deleted*: name both folders the tool deletes in.
    - *Tuning*: replace "on a USB hard disk, start around 8 connections" with the recommendation to set
      `partial_dir` to a folder on a fast local disk, and say what it removes (interleaved writes, checkpoint fsyncs,
      zero-fill, re-hash on DEST).
    - *Configuration*: add a table row.
  - Package `README.md`: add the env-var table row, and rewrite the layout lines for `destination.py` (the folder
    below `--partial-dir`, the copy target) and `mirroring.py` (the copy pass, one at a time).
  - Rewrite the module docstrings of `destination.py` and `mirroring.py` to describe both layouts.
  - Run `rg -n "partial/" README.md yandex-disk-rescue` and fix every stale sentence.

### Stage 1b: Crash safety of copy → fsync → place

- **Contract:**
  - At the start of every run (`MirrorRun.__init__`), every regular file named `<digits>.copy` in DEST's own
    `partial/` is deleted. This drops Stage 1a's interim. DEST's own `<id>` partial files are not touched by this
    rule.
  - Invariants:
    - (a) Nothing reaches a final path except through `place`, after the copy is complete, fsynced and its digest
      matched.
    - (b) Until `record_verified` or `record_quarantined` commits, the local partial file and its fully confirmed
      journal stay intact, so a rerun fetches nothing again for that file.
    - (c) A rerun after a crash before placement deletes the staged copy and copies again.
    - (d) A crash after placement and before the record leads the rerun to adopt the placed file by hashing it, and
      then to delete the local partial file.
    - (e) A crash after the record leaves a stray local partial file, which the next run's `keep_partials` deletes.
- **Behaviour to lock** (`test_download.py`):
  - **One parametrized test body for the crash points.** It patches the step to raise `KeyboardInterrupt` (the
    `test_a_run_killed_between_quarantining…` pattern). The points are:
    - mid-copy, after the first chunk;
    - after the copy's `finish`, before `place`;
    - after `place`, before `record_verified`;
    - after `record_verified`, before `remove_partial`.

    The test asserts exit 130, and that the final path holds nothing or the verified bytes, never a truncated file.
    It then reruns and asserts: no content request for the file, exit 0, the file verified, no `.copy` file left,
    and the local folder empty.
  - **A real `SIGKILL`.** A new sitecustomize gate variant blocks the fsync of a `.copy` file only; the existing
    fsync/EIO gate is not changed. The child is killed while blocked there, and the rerun holds the same assertions.
  - **Errors.** `test_a_full_or_failing_destination_stops_the_run_and_a_rerun_goes_on` gets a `separate` case. An
    ENOSPC or EIO from a write to the local partial file stops the run with `Could not write <folder>/<id>: …`, and
    the rerun resumes from the journal.
- **Deliverable:** the crash-safety invariants hold and are locked.
- **Validation:**
  - `mise run check`
  - `mise run test -- yandex-disk-rescue/tests/test_download.py -k "partial or killed or full_or_failing"`
- **Doc updates:** root `README.md` *Partial files*: what a crash at each point leaves and what the rerun does, as
  current state.

### Stage 2: Record the partial folder; move confirmed bytes when it changes

- **Contract:**
  - **Manifest.** Schema v9: `ALTER TABLE mirrors ADD COLUMN partial_folder TEXT` (`NULL` = DEST's own
    `partial/`). `Mirror.partial_folder -> Path | None` and `Mirror.record_partial_folder(folder: Path | None) ->
    None`. Existing mirrors upgrade to `NULL`.
  - **Opening the previous folder.** `Destination` gains a way to open a previous partial folder by path, or DEST's
    own for `None`. It never creates it, and reports "missing" when the folder does not exist. It offers:
    - per entry: the partial file's presence, a read of a byte range, and deleting it;
    - deleting the folder's leftovers: for a separate folder, every regular file, then `rmdir`; a folder kept
      non-empty by something else is left in place. For DEST's own folder, every regular file except staged copies,
      which Stage 1b's rule already deletes.

    Errors other than "missing" raise `DestinationError` naming the folder.
  - **Start of a run.** `previous = mirror.partial_folder`; `current = destination.partial_folder` when partials are
    separate, `None` otherwise.
    1. `MirrorRun.__init__`: Stage 1b's staged-copy deletion, then `keep_partials` on the current folder, as today.
    2. `MirrorRun.partial_move` is read by the command before `run`. It is `None` when `previous == current`.
       Otherwise it names the `source` and `target` and gives the number of downloads in progress whose partial file
       is in `source` with their total confirmed bytes, or `missing=True`. The command prints the Decision 11 line.
    3. First thing in `MirrorRun.run`, before any fetch, for each such download in entry order, one at a time:
       1. Truncate `destination.partial(entry_id)` to 0.
       2. Copy each journal segment's `[start, confirmed)` from the source to the same offsets, in chunked thread
          calls. A segment whose confirmed offset lies past the source file's end (a source cut short by a power
          loss on an unjournaled DEST, or by hand) is not copied and is reset to its start in the journal, as
          `_resume` does today, so its bytes are fetched again. The move never fails on a short source.
       3. Fsync the file, then fsync the target folder.
       4. Delete the source, then fsync the source folder.

       Then delete the leftovers of `previous`, then call `mirror.record_partial_folder(current)`.
  - **Crashes during the move.** A crash at any point leaves `previous` recorded. A file still in `previous` wins
    over the same entry's file in `current` on the rerun: the target is truncated and written again. A file already
    moved is no longer in `previous`, so it stays in `current`, and Stage 1b's rule never deletes it, because it is
    named `<id>`, not `<id>.copy`. So the move resumes where it stopped and loses no confirmed byte.
  - **Ctrl+C during the move** waits for the chunk in flight, like any thread call.
- **Behaviour to lock:**
  - `yandex-disk-rescue/tests/test_manifest.py`: the v4 dump upgrades to v9 with `partial_folder is None`, and
    `record_partial_folder` round-trips a path and `None`.
  - `test_download.py`, parametrized over **on** (DEST → `partial_root`), **off** (`partial_root` → DEST) and
    **moved** (`partial_root/a` → `partial_root/b`), with **two** partials left:
    - The first is `BIG`, cut short. The second is `SEGMENTED`, whose segments are cut short at different offsets,
      with unconfirmed bytes past each checkpoint and zero-fill.
    - A rerun with the second setting prints the move line with the confirmed total. Reads are wrapped, and only
      each segment's `[start, confirmed)` of a source file is ever read. The content requests are the tail `Range`
      of each segment, the bytes are right, and the old folder is gone (separate) or holds no partial file (DEST's).
    - **Killed after the first whole file was moved** (patch the second file's first chunk to raise
      `KeyboardInterrupt`), then rerun with the same second setting: both files resume from their confirmed bytes.
      Nothing confirmed is fetched again; for **off**, this checks that the already-moved `DEST/…/partial/<id>`
      survived.
    - **Recorded folder deleted by hand** before the rerun: the missing line, full fetches from byte 0, exit 0.
    - **A source cut short below a confirmed offset** before the rerun: exit 0, the other segments resume from
      their confirmed bytes, and that segment is fetched from its start.
- **Deliverable:** changing `partial_dir` between runs never loses or re-reads more than the confirmed bytes, and a
  lost folder is reported.
- **Validation:**
  - `mise run check`
  - `mise run test -- yandex-disk-rescue/tests/test_manifest.py`
  - `mise run test -- yandex-disk-rescue/tests/test_download.py -k "partial"`
- **Doc updates:**
  - Root `README.md` *Partial files*: what turning the option on or off and changing it does (only confirmed bytes
    are moved), the move line, and the missing-folder line, as current state. Drop the Stage 1a interim wording.
  - Package `README.md` *Manifest*: the mirror's recorded partial folder.

### Stage 3: Space admission on the separate partial folder

- **Contract:**
  - `localfs.free_bytes(dir_fd: int) -> int`: `f_bavail × f_frsize` from `os.fstatvfs`.
  - `PARTIAL_RESERVE = 2**30` in `mirroring.py`.
  - The room on the separate folder is created when the first pass starts, after Stage 2's move, and follows
    Decision 9 exactly: capacity, counted total, reservation per admitted turn, FIFO, wait/defer/fail, and the
    wake-up contract.
  - Admission happens at `_fetch_verified` entry, before the partial file is created or opened, and the turn ends
    when `_fetch_verified` and the steps after it return or raise. Turns that adopt a local copy are never admitted.
    A zero-byte file reserves nothing.
  - A file deferred for room follows the usual pass rules: after the last pass its reason gets `(after N passes)`,
    and it gets a failure record like any failed file.
  - The `Partial files in <folder>` line gains ` (<capacity> available)`. In DEST mode nothing changes.
- **Behaviour to lock** (`test_download.py`). `localfs.free_bytes` is patched to a fixed number: no small real
  filesystem can be made without root, and this is the same seam style as the existing `localfs` fault patches.
  - **One large file at a time.** With room for one 1 MiB file, `BIG` plus a second 1 MiB file plus the small tree
    are all verified, exit 0. In the fake server's request log, the second large file's first content request
    starts only after the first one's turn ended (its placement finished).
  - **Too large.** A file larger than the capacity fails with `larger than the room in the partial folder …`. It
    gets a failure record, no file of it is left in the folder, every other file is verified, and the run exits 1.
    `--retry-failed` with enough room fetches it.
  - **Waiter whose last running peer defers.** File A (1 MiB) is admitted and stalls for the whole pass, so it is
    deferred with its partial kept: idle, counting its allocated bytes. File B (1 MiB) waits behind it. When A's
    turn ends, B is checked again, finds no admitted turn running and not enough room, and is deferred with `no room
    yet …` instead of hanging. A pass with room completes both.
  - **Idle partials count only what they allocate.** A kept sparse partial that allocates little leaves room for
    another file whose full size would not fit next to it at full size.
- **Deliverable:** the separate folder never holds more partial bytes than its room at the start of the run allows,
  and no turn waits forever.
- **Validation:**
  - `mise run check`
  - `mise run test -- yandex-disk-rescue/tests/test_download.py -k "room or partial"`
- **Doc updates:**
  - Root `README.md` *Partial files*: the room rule, the reserve, the two reasons, the capacity in the line, and the
    ENOSPC fallback, as current state.
  - Package `README.md`: the layout lines for `localfs.py` (free space) and `mirroring.py` (admission).

## Manual verification (main runs this with the user; not in CI)

The user's live run uses `/mnt/oblako`, `~/.local/state` and `~/.config`, so nothing here touches them until the user
chooses to. Once the PR is merged and the live run is stopped:

1. `yandex-disk-rescue download --partial-dir /var/tmp/ydr-partials /mnt/oblako`. Check that `/var/tmp` is
   persistent and not a tmpfs. Time the move line: about 12.5 GiB of confirmed bytes, read from the HDD.
2. During the run, `iostat -x` on the HDD should show one sequential writer. Record the files/s placed while small
   files dominate: per-file fsync and rename latency on the HDD is the expected limit there.
3. Ctrl+C and rerun. The resume re-hash should cause no HDD reads.

## Discovered issues

- **Fix in this PR:**
  - README *Tuning*'s HDD advice ("start around 8 connections") is superseded by `partial_dir` (Stage 1a doc
    update).
  - The README's "Nothing is deleted … outside `DEST/.yandex-disk-rescue/partial/`" becomes inaccurate once the tool
    deletes in a second folder (Stage 1a doc update).
- **Filed as issues:** None. The re-planning and bounded re-hash parts of #18 stay in #18 (Non-goals).

## Risks / open questions

- **#6 overlap.** #20 merges first. #6 (`feat/progress-logging-json`) pauses after its Stage 2 and is rebased onto
  #20.
  - The functions both branches touch are `_fetch_verified`, `_resume`, `_Stream.write`, `_Journal`, `MirrorRun.run`,
    `MirrorRun._download`, `MirrorRun._turn`, `mirror_summary`, and `commands/download.py`'s `download`, `_run` and
    `_mirror` (the new option, the new lines).
  - #20 adds no logging infrastructure: its lines are plain `typer.echo`, like `main` today.
  - After the rebase, #6 adds `partial_dir` to its `details` dict, routes #20's lines through `output.line`, and adds
    log records and progress states for #20's phases: moving partials, copying to DEST, waiting for the copy lock,
    and waiting for room.
- **`/dev/shm` in CI.** The `partial_root` fixture needs a second filesystem. The Dagger container (runc) mounts a
  64 MiB tmpfs there by default, and Stage 1a's `mise run ci` confirms it. If that ever changes, the fixture fails
  loudly instead of skipping.
- **Copy throughput.**
  - Large files: one copy at a time at HDD speed (~100 MB/s) is far above the measured 2–3 MB/s total network rate.
  - Small files: each one costs a file fsync, a rename and a folder fsync on the HDD. That is tens of milliseconds
    with seeks, so roughly 20–50 files/s at best (an estimate). Today the same fsyncs happen, but in parallel with
    other writes; serializing them may make runs dominated by small files slower.
  - Manual verification measures files/s. If it is too slow, a follow-up can let small copies through without the
    copy lock. This plan does not, to keep DEST to one writer as the acceptance criterion asks.
  - Turns waiting for the copy hold their slots, which is the intended backpressure.
- **Hashing the copy, not reading DEST back** (Decision 5), trusts DEST's write path after `fsync`, as today's
  one-stream downloads do. Silent corruption on the HDD is `verify`'s job.
- **Capacity is measured once per run** (Decision 9). Another program filling the disk mid-run leads to the explicit
  ENOSPC stop, not to waiting.
- **No partial-folder lock** (Decision 4). One `PATH` shared by two machines whose DEST disks are mounted at the same
  path is unsupported, and the README says so.
- **The key follows the DEST path** (Decision 3). Remounting DEST elsewhere already makes a new mirror in the
  manifest. The old per-destination folder is then orphaned under `PATH`, and the README says it can be deleted.
