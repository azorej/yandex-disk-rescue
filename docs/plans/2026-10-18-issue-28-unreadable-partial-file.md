# Plan: An unreadable partial file is downloaded again instead of stopping the run

Issue: #28
Branch: fix/unreadable-partial-file (from `main` at 2d1add9)

## Context

Reproduced on `main` (2d1add9) against the fake server, with `tmp_path` destinations and scratch XDG dirs. A scratch
test (not committed) left two partial files with `_leave_two_partials` (`BIG`, 300 000 bytes confirmed; `SEGMENTED`,
4 segments confirming `_SEGMENT_CUTS`), then refused every `os.pread` of `BIG`'s partial file and ran `download` again:

```
resume, EIO      exit 1   Error: Could not read DEST/.yandex-disk-rescue/partial/2: Input/output error.
                          nothing fetched; both journals kept, so every later run stops at the same file
resume, EBADMSG  exit 1   Failed: 1
                            disk:/Big/big.bin: could not write Big/big.bin: Bad message
                          (wrong verb, wrong path: the final path, not the partial file; the journal is kept, so
                          every later run reads the bad sector again and fails the file again)
move,   EIO      exit 1   Error: Could not read DEST/.yandex-disk-rescue/partial/2: Input/output error.
move,   EBADMSG  exit 1   Error: Could not read DEST/.yandex-disk-rescue/partial/2: Bad message.
                          (move from DEST's own partial/ to a new --partial-dir; nothing downloaded)
```

The reason is in two places. `PartialFile.chunks` wraps its `pread` in `_work_errors('read', …)`, which turns
`WORK_FATAL_ERRNOS` (`EIO` among them) into a `DestinationError`; any other errno reaches `download_file`'s outer
`_local_errors(..., 'write')` and fails the file as a *write* of its final path. The move wraps the same reads in
`_read_errors`, which turns every `OSError` into a `DestinationError`. The same `PartialFile.chunks` also reads the
partial file when a segmented file is read back (`_file_digest`) and when a file is copied to DEST from a
`--partial-dir` (`_copy_to_destination`), so an `EIO` there stops the run as well. A partial file holds no user data:
its confirmed bytes come from the Disk, which still has them.

## Goal

One rule for every read of a partial file's bytes: the resume re-hash, the read-back of a segmented file, the copy to
DEST from a separate partial folder, and the move to a new partial folder.

- A read that fails while the partial file's folder is still reachable **drops that download's confirmed bytes**.
  The journal is replaced by one confirming nothing before anything else is written. The partial file is cut to 0
  bytes, or is not carried over by a move. The file is then downloaded again from its first byte, with the plan a new
  download gets now (#18's rule for a journal that keeps no byte). The event is a WARNING notice and a log line:
  `<cloud path>: could not read its partial file <path>: <reason>; downloading it again from its first byte`.
  The other downloads go on from their checkpoints, and the run goes on.
- A read that fails for a reason every other file would meet too **stops the run** with
  `Error: Could not read <path>: <reason>.`, as today. Nothing is dropped then. That covers three cases:
  - the folder is gone: an errno in `DEVICE_GONE_ERRNOS`, or the partial file's folder no longer passes a fresh
    `lstat` and listing;
  - the process cannot read anything right now: an errno in `PROCESS_LIMIT_ERRNOS`;
  - a permission error (`EACCES`, `EPERM`), which is fatal in the work directory today (`WORK_FATAL_ERRNOS`) and
    stays so.
- **The drop is bounded: once per download per run.** A second read error on the same download's partial file in the
  same run, from any reader (the move included), fails that file at once instead. The reason is
  `could not read its partial file <path>: <reason> (a second time in this run)`, its bytes are dropped again so the
  next run starts clean, and it is not a failure from Yandex's side.
- A file whose drop ends its turn and that then runs out of passes fails as a local failure (`on_yandex` false), like
  `NoRoomYet`, so it gets no Yandex preview and is not reported as Yandex's fault.

Writes, `fstat`s and opens of partial files keep their classification (see Non-goals).

## Decision for the user

**What happens to the unreadable partial file?**

- **A (recommended): empty it in place, and let the move delete it as it deletes every moved file.** In a turn, the
  partial file is cut to 0 bytes (`ftruncate`), which frees its blocks, and the download goes on in the same file. In
  the move, the old file is deleted once its download's empty journal is recorded, exactly as every old file is
  deleted once it is moved. Why:
  - A partial file is the tool's scratch data. Its confirmed bytes are bytes the Disk served, and the Disk still has
    them: the resume checks the file's identity before it trusts any byte, and a file changed in the cloud has its
    partial file deleted today (`_discard`).
  - The tool already deletes partial files on its own: stray ones (`keep_partials`), ones whose file changed, and
    every moved one.
  - "Never delete user data" covers final paths in DEST. `replaced/` keeps a user's earlier copy, and `quarantine/`
    keeps a download that failed verification, which may be the only copy left. Neither applies to unconfirmed
    scratch bytes.
  - Freeing the blocks has two effects. The filesystem may hand the same bad sectors back to the rewrite. Usually
    that is harmless: on a hard disk, writing to a pending sector makes the drive remap it, so the rewrite tends to
    heal the area, and on flash the write lands on another physical page. Sometimes it is not: a sector that still
    reads badly after the rewrite fails the read-back or copy again. Then the bound above fails the file after its
    second drop, not on every pass. So A is safe *because of* the bound. B and C avoid reuse, but they keep the bad
    area allocated, and C only delays that by one run.
- **B: move it aside** to `DEST/.yandex-disk-rescue/replaced/<run>/partial-<id>`. This keeps bad sectors allocated
  and grows the work directory, and there is no `replaced/` beside a separate `--partial-dir`. Nobody can use the
  bytes: they are confirmed only by a journal that is being dropped.
- **C: rename it in its folder to a leftover name.** The next run's `keep_partials` deletes it anyway, so this is A
  one run later.

**How much is dropped: the whole download, not the segment.** A read error on one segment drops every segment of that
file, including the ones already re-hashed. It is simpler: one rule for all four readers, and the read-back and the
copy read the whole file anyway. It also suits a bad area on an aging disk, which often spans more than one segment.
#18's re-plan then gives the download the current segments. The cost is re-fetching the other segments' confirmed
bytes of that one file. The issue allows either.

## Non-goals

- **Opening, `fstat`, extending or writing a partial file.** An `EIO` there keeps stopping the run (`Could not write
  …` / `Could not inspect …`, #16's classification). Opening a partial file for a turn is a read-write open in the
  partial folder, and an `fstat` failure is a bad inode, not the bad-sector case the issue is about. The move's
  read-only open of an old file (`PreviousPartials.partial`) also keeps stopping the run. See Risks.
- **No summary list or JSON field** for dropped partial files. The issue asks for a notice and a log line. The bytes
  were scratch, and the file is downloaded and verified in the same run, so the report does not change. See Risks.
- Reads of files already in DEST (`_adopt`): #16, unchanged.
- `verify`: it never reads partial files.

## Stages

### Stage 1: A partial file that cannot be read in a turn is emptied and downloaded again

- **Contract:**
  - `destination.py`:
    - `class PartialReadError(Exception)` (not an `OSError`), with `path: Path`, `errno: int` and `reason: str`
      (the `strerror`). `PartialFile.chunks(start, end)` raises it for any `OSError` of a read. It no longer raises
      `DestinationError` itself. The `ValueError` for a file that ends before `end` stays.
    - `Destination.partial_folder_reachable() -> bool`: `partial_folder` `lstat`ed afresh and listed through a new
      descriptor, with the same semantics as `reachable()` (which stays as it is for DEST). The probe is one shared
      helper, and `_Tree.reachable` uses it too, so there is no second copy. (`PreviousPartials.reachable()` arrives
      in Stage 2 with its caller.)
  - `mirroring.py`: one classification, used by every reader of a partial file in a turn (`_rehash_kept`/
    `_Stream.rehash`, `_file_digest`, `_copy_to_destination`). For a `PartialReadError`:
    - the run stops, `raise DestinationError('read', exc.path, exc.reason)`, when any of these holds:
      - its errno is in `DEVICE_GONE_ERRNOS`, `PROCESS_LIMIT_ERRNOS` or `{EACCES, EPERM}`;
      - `await threads.run(destination.partial_folder_reachable)` is `False`. The probe goes through `threads`
        because it can hang on a failing USB disk.
    - otherwise the download's bytes are **dropped**:
      1. A WARNING is logged:
         `<cloud path>: could not read its partial file <path>: <reason>; downloading it again from its first byte`.
      2. `mirror.record_partial(entry_id, identity, _new_plan(...))` records a journal confirming nothing.
      3. The partial file is cut to 0 bytes. This is a write, so its errors keep `_work_errors`' classification.
      4. `progress.cut()` is called, so bytes shown as kept are no longer counted.

      The journal is written before the cut, so a crash in between leaves a journal that trusts nothing.
    - **Bound:** the run (`MirrorRun`) keeps the set of entry ids dropped in this run. Stage 2's move adds to the
      same set. A read error on an entry already in the set drops its bytes the same way (journal, cut), logs nothing
      extra, and raises `FileFailed('could not read its partial file <path>: <reason> (a second time in this run)')`.
      That `FileFailed` has `on_yandex` false: the file fails at once, with no later pass.
  - What follows a first drop depends on the reader:
    - **Resume re-hash:** the turn goes on at once with the new plan, as a new download. The other segments of the
      file are not re-hashed further. A segmented plan is preallocated from 0, and one stream appends from 0.
    - **Read-back of a segmented file** (partial files in DEST) **and copy to DEST** (separate partials): the turn
      ends with a new `UnreadablePartial(FileDeferred)`:
      - `reason` is `could not read its partial file <path>: <reason>`, with `retry_after=None`, `throttled=False`
        and `received=True`;
      - `received=True` is accurate, since the whole file arrived in this turn, and it keeps the turn from counting
        as silent towards "unavailable on Yandex Disk" (#14);
      - a later pass downloads the file from byte 0;
      - a staged copy left in DEST is truncated by the next `copy_target`, or deleted at the next run's start, as
        today.
    - `_later_passes`: a deferral whose cause is local (`NoRoomYet` or `UnreadablePartial`) fails with `on_yandex`
      false when the passes run out. Today it is `on_yandex=not no_room` (`mirroring.py:1043`), and
      `previews.preview_candidates` takes every `on_yandex` failure.
  - `download_file`'s outer `_local_errors(..., 'write')` no longer sees partial-file read errors. The
    `could not write <final path>: Bad message` failure is gone.
  - **Interim:** the move (`_move_partial`) maps a `PartialReadError` to `DestinationError('read', exc.path,
    exc.reason)`, so its behaviour is unchanged in this stage. Stage 2 removes this mapping, along with
    `_read_errors`.
- **Behaviour to lock** (all in `yandex-disk-rescue/tests/test_download.py`, next to
  `test_an_error_reading_a_partial_file_being_moved_stops_the_run_naming_that_file`, reusing `_leave_two_partials`;
  reads are refused by patching `os.pread` for one `/proc/self/fd` path, as that test does, counting the refusals):
  - `test_an_unreadable_partial_file_is_emptied_and_downloaded_again_from_its_first_byte[layout][code][file]`,
    parametrized `layout` ∈ {`in-dest`, `separate`} (`partial_root` passed to both runs), `code` ∈ {`EIO`,
    `EBADMSG`}, `file` ∈ {`one stream`, `segmented`} (the partial file of `BIG` or of `SEGMENTED` is refused). The
    rerun passes `SEGMENTED_ARGS`. Assertions:
    - exit 0;
    - stderr has exactly one notice
      `disk:/…: could not read its partial file <partial>: <strerror>; downloading it again from its first byte`,
      and `log_messages()` has the same record;
    - the refused file is fetched from byte 0: `[None]` for `BIG`, `sorted(SEGMENT_RANGES)` for `SEGMENTED`;
    - the other file resumes from its checkpoints: `[_tail(300_000)]`, or the four tails after `_SEGMENT_CUTS`;
    - both files are intact at their final paths, `_journals() == {}`, and no partial file is left;
    - its reads were refused exactly once;
    - for `segmented`, an `_io_log(monkeypatch, ('ftruncate', 'pwrite'))` of the rerun shows that the first call on
      that partial file is `ftruncate` to 0. The bad bytes are not kept under the new plan.
  - `test_a_partial_file_unreadable_once_fetched_is_downloaded_again_in_a_later_pass[read back|copy]`:
    - `read back`: `SEGMENTED` with partial files in DEST, plus `SEGMENTED_ARGS`. The first `pread` of its partial
      file is refused with `EIO`. That read happens only in the read-back.
    - `copy`: `BIG` with `--partial-dir`. The first `pread` of its partial file below `partial_root` is refused. That
      read happens only in the copy.
    - Assertions for both: exit 0; the notice once; the summary has `0 failed`; the file is fetched twice in full
      (`SEGMENT_RANGES` twice, or `[None, None]`); the file is intact; the log has
      `… deferred to a later pass: could not read its partial file <partial>: Input/output error`.
  - `test_a_partial_file_unreadable_once_fetched_with_no_pass_left_is_a_local_failure_with_no_preview`: the
    `read back` setup with `--download-passes 1 --json`. Assertions:
    - exit 1, and `failed == [{'path': SEGMENTED_PATH, 'reason': 'could not read its partial file <partial>:
      Input/output error', …}]`, the failure not from Yandex's side (whatever field `--json` exposes for it, if any);
    - `_preview_requests(disk) == []` and `document['preview_only'] == []`.
  - `test_a_partial_file_unreadable_twice_in_one_run_fails_that_file[read back|copy]`: every `pread` of that partial
    file is refused, from the first fetch on, with `--download-passes 0` (no limit). Assertions:
    - exit 1; the file is fetched exactly twice; one notice;
    - `Failed: 1\n  <cloud path>: could not read its partial file <partial>: Input/output error (a second time in
      this run)`;
    - `_preview_requests(disk) == []`;
    - the journal confirms nothing, and the other files are downloaded.
  - `test_an_unreadable_partial_file_stops_the_run_when_every_file_would_fail[EIO|ENODEV]`, partial files in DEST:
    - For `EIO`, the injector first renames DEST away (as
      `test_an_unreadable_local_copy_on_a_disk_that_is_gone_stops_the_run` does), so `partial_folder_reachable()`
      really fails. For `ENODEV` it raises directly.
    - Exit 1; stderr exactly `Error: Could not read <partial>: <strerror>.`; with DEST renamed back, `_journals()`
      is unchanged (nothing dropped) and no notice was logged.
    - Add `EMFILE` (process limit) and `EACCES` (permission) cases, with DEST in place: the same stop. `EACCES`
      locks that a permission error on a partial read stays fatal.
  - `test_an_error_reading_a_partial_file_being_moved_stops_the_run_naming_that_file` keeps passing unchanged in
    this stage.
- **Deliverable:** `PartialReadError`, the shared folder probe and `partial_folder_reachable`, the classification and
  the once-per-run bound at the three in-turn readers, `UnreadablePartial` failing locally, and the tests above
  green.
- **Validation:** `mise run check`; `mise run test -- yandex-disk-rescue/tests/test_download.py -k "unreadable_partial
  or unreadable_once_fetched or partial_file_being_moved or resum or segment"`;
  `rg -n "could not read its partial file" yandex-disk-rescue/src README.md` shows the one message source and its
  docs.
- **Doc updates:**
  - Root `README.md` "Interruptions, stalls and retries", the resume paragraph ("A later pass or a later run asks the
    Disk …"): rewrite as current state. A partial file whose bytes cannot be read (an I/O error, a bad sector) while
    its folder can is emptied and downloaded again from its first byte, with a notice. The same applies to a
    segmented file read back, and to the copy to DEST with `--partial-dir`, where the file is fetched again in a
    later pass. This happens at most once per file per run; a second read error fails the file (`… (a second time
    in this run)`), as a local failure that gets no preview. A partial folder that is gone, or a permission error,
    stops the run with `Error: Could not read <path>: <reason>.`
  - Root `README.md` "The destination" paragraph: "An I/O error while reading the work directory or the partial
    folder stops it with `Could not read …`" becomes current state. An I/O error reading a partial file does not stop
    the run while its folder is reachable (point to "Interruptions, stalls and retries"). One reading the work
    directory, or a partial folder that is gone, does.
  - Root `README.md` "Output and logs" notices list: add each partial file that could not be read
    (`<path>: could not read its partial file <file>: <reason>; downloading it again from its first byte`).
  - `mirroring.py` module docstring and the `download_file`, `_resume`, `_fetch_verified`, `_file_digest` and
    `_copy_to_destination` docstrings: state the drop and the stop as current state. `download_file`'s `Raises`
    paragraph gets "a partial file that cannot be read while its folder is gone".
  - `destination.py`: the `PartialFile` docstring (reads raise `PartialReadError`), and the module docstring if it
    describes error mapping for partial reads.
  - `yandex-disk-rescue/README.md` layout entries for `mirroring.py` (an unreadable partial file is downloaded again)
    and `destination.py` (the partial folder's reachability).
  - `CHANGELOG.md` `## [Unreleased]` → `### Fixed`: "`download` no longer stops, or fails the file, when it cannot
    read a partial file (a bad sector) while resuming, reading back or copying it: the partial file is emptied and
    the file downloaded again from its first byte, with a notice; a partial folder that is gone still stops the run."

### Stage 2: A partial file that cannot be read during a move is dropped, and the move goes on

- **Contract:**
  - `PreviousPartials.reachable() -> bool`: its `path` probed with Stage 1's shared helper.
  - `MirrorRun.move_partials` / `_move_partial`: a `PartialReadError` reading an old partial file is classified like
    Stage 1, with `source.reachable()` (through `threads`) in place of `partial_folder_reachable()`:
    - gone, process limit, permission error, or old folder unreachable → the run stops:
      `Error: Could not read <old file>: <reason>.` The recorded folder stays, so the next run moves again.
    - otherwise → that download is dropped:
      - the same WARNING is logged, with the old file's path;
      - `mirror.checkpoint(entry_id, …)` records every segment of its journal at its start (confirming nothing);
      - its new file in this run's partial folder is cut to 0 bytes and synced;
      - the partial folder is synced and the old file is deleted, as for every moved file;
      - `progress.moved()` counts the file. The bytes it no longer copies are simply not counted, so the final
        `Moved:` line gives the bytes actually copied;
      - the entry joins the run's set of drops (Stage 1's bound), so one more read error on it in this run fails the
        file.
      The move goes on with the next file. When the transfers start, the download keeps no byte, so #18's re-plan
      gives it the current plan from byte 0.
  - **What the move reports after a drop:**
    - `PartialMove` (the `Moving the confirmed bytes (<size>) of N downloads …` headline, and `partial_move` in
      `--json`) is announced before the move, so it keeps the plan: `downloads` and `size` are what the move set out
      to move.
    - The INFO record at the end reports what was done: `Moved the partial files of <N> downloads in progress
      (<bytes actually copied>) from … to … in … s`, with `, <k> dropped as unreadable` appended when `k > 0`. N
      still counts every file handled.
    - The README JSON section says `partial_move.size` is the confirmed bytes the move set out to copy.
    - The order "journal recorded, then new file cut, then old file deleted" means a crash anywhere leaves a journal
      that trusts nothing.
  - `_read_errors` and Stage 1's interim mapping in `_move_partial` are deleted. `PreviousPartials`' docstring
    ("except reading an opened partial file, which fails as `PartialFile` does") states that a read raises
    `PartialReadError`.
- **Behaviour to lock** (`yandex-disk-rescue/tests/test_download.py`):
  - Replace `test_an_error_reading_a_partial_file_being_moved_stops_the_run_naming_that_file` with
    `test_an_unreadable_partial_file_being_moved_is_dropped_and_downloaded_again[EIO|EBADMSG-first|later]`. The setup
    is `_leave_two_partials(disk, dest, None)`, with the first read of `BIG`'s old partial file refused (`first`), or
    the reads of `SEGMENTED`'s past its first segment, once that segment is copied (`later`). The rerun passes
    `--partial-dir partial_root` and `SEGMENTED_ARGS`. Assertions:
    - exit 0, and the notice naming the old path once;
    - the dropped file is fetched from its first byte, and the other one fetches only the tails after its confirmed
      bytes (they were moved);
    - both files intact; the old folder holds no partial file, and `_partials(dest, partial_root) == []`;
    - with `--json`, `partial_move` reports `downloads: 2` and `size: 300_000 + sum(_SEGMENT_CUTS)` (the plan);
    - `log_messages()` has `Moved the partial files of 2 downloads in progress (<format_size(sum(_SEGMENT_CUTS))>)
      from … to … in … s, 1 dropped as unreadable` (the other file's confirmed bytes), and the final `Moved:` status
      line gives the same figure: a dropped file's bytes copied before the failure are taken back from the move's
      progress (`MoveProgress.dropped`).
  - `test_a_partial_file_dropped_in_the_move_and_unreadable_again_fails_that_file`: the same setup, with every
    `pread` of `BIG`'s partial file refused, both the old one and the new one in `partial_root`. Since `BIG` is one
    stream, its only later read is its copy to DEST. Exit 1; `Failed: 1\n  disk:/Big/big.bin: could not read its
    partial file <new partial>: Input/output error (a second time in this run)`; `SEGMENTED` is downloaded.
  - `test_an_unreadable_partial_file_being_moved_from_a_folder_that_is_gone_stops_the_run[EIO|ENODEV]`:
    - Setup: `_leave_two_partials(disk, dest, partial_root)`, then a rerun **without** `--partial-dir` (the move
      back into DEST).
    - For `EIO`, the injector renames `_separate_folder(dest, partial_root)` away before raising. For `ENODEV` it
      raises directly.
    - Exit 1; stderr exactly `Error: Could not read <old big>: <strerror>.`; `_journals()` unchanged (`BIG` still
      confirms 300 000 bytes).
  - `test_a_move_of_partial_files_stopped_after_the_first_file_goes_on_in_the_rerun_losing_no_confirmed_byte`,
    `test_a_move_of_partial_files_shows_its_status_lines_then_its_last_one` and
    `test_a_moved_partial_file_cut_short_below_a_confirmed_offset_fetches_that_segment_again` keep passing unchanged.
- **Deliverable:** the move never stops at one unreadable partial file while its folder is reachable; `_read_errors`
  is gone.
- **Validation:** `mise run check`; `mise run test -- yandex-disk-rescue/tests/test_download.py -k "moved or move or
  partial_dir or unreadable_partial"`; `rg -n "_read_errors" yandex-disk-rescue` is empty; `mise run ci`.
- **Doc updates:**
  - Root `README.md` `--partial-dir` list, the move bullet ("The manifest records which folder …"): rewrite as
    current state. A partial file that cannot be read during the move is not carried over: its download starts again
    from its first byte, with the same notice. A folder that is gone stops the run with `Error: Could not read
    <path>: <reason>.`
  - Root `README.md` "JSON reports": `partial_move` — `size` is the confirmed bytes the move set out to copy (rewrite
    the sentence at the `partial_move` key as current state).
  - `mirroring.py` module docstring (the move paragraph, "The manifest records the folder …") and the
    `move_partials` / `_move_partial` / `PartialMove` docstrings.
  - `CHANGELOG.md`: extend Stage 1's `### Fixed` line to read "… while resuming, reading back, copying or moving it
    to a new `--partial-dir` …", so there is one entry, not two.

## Discovered issues

- **Fix in this PR:**
  - An `EBADMSG` (any errno outside `WORK_FATAL_ERRNOS`) during the resume re-hash fails the file as `could not write
    <final path>: Bad message` (the wrong verb and the wrong path), and the file fails the same way on every later run.
    Stage 1 fixes this.
  - The read-back of a segmented file and the copy to DEST stop the run on an `EIO` in the partial file. They are the
    same class as the issue's two readers, share `PartialFile.chunks`, and get the same rule in Stage 1.
  - The README "The destination" paragraph says an I/O error reading the partial folder stops the run. Stage 1
    rewrites it.
- **Filed as issues:** none. Everything seen in the neighbourhood is either fixed above or a deliberate non-goal
  (Risks).

## Risks / open questions

- **The user's decision** on what happens to the unreadable file (A: empty in place, recommended; B: move aside;
  C: rename to a leftover) and on the scope of the drop (the whole download, recommended, or only the failing
  segment). Under B, the drop renames the file into `replaced/<run>/` instead of cutting it, and the README says so.
  Under segment-only, `_rehash_kept` resets just that segment, and the cut to 0 applies only when no segment keeps a
  byte (#18).
- **Errors other than reads of a partial file still stop the run:** opening it, `fstat`, extending or writing it, and
  the move's read-only open of an old file (a bad inode, a failing write). These are #16's write/inspect
  classification, and a failing write is the disk refusing new data, which a re-download cannot fix. If a live run
  shows an `EIO` on opening an old partial file during a move, the same drop could be applied to that open as a
  follow-up.
- **A disk dropping off mid-read** may answer `EIO` and still pass the fresh `lstat` and listing for a moment
  (cached dentries). Then one download's confirmed bytes are dropped, and the next write stops the run with `Could
  not write`. Only scratch bytes are lost, which the Disk serves again. This is the trade-off #16 and `verify` make.
- **A partial file that cannot be read on every attempt** (read-back or copy on a disk that keeps failing in the same
  place) is downloaded at most twice per run: the bound fails it at the second drop, also with `--download-passes
  0`. Each later run tries it again. Under #27 a failed file goes last, so a bad area costs at most two downloads of
  that file per run.
- **No report entry**: the notice and the log line are the only trace. On an aging disk a read error warns that the
  disk is failing. If the user wants it in the summary and `--json`, like #16's `Unreadable in DEST` list, that is an
  `Unreadable partial files` list, a small addition to Stage 1.
- **Coordination:** #30 (a download-link failure stops the run) is planned in parallel and touches
  `mirroring.download_file` / `_download_link`, not the readers changed here. The stage implementers rebase on
  whichever lands first. No manifest schema change.
