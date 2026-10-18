# Plan: An unreadable local copy no longer stops download, and every destination error names its action

Issue: #16
Branch: fix/unreadable-local-copy

## Context

`DestinationError.of(path, exc)` (`destination.py:60`) always renders `Could not write <path>: <reason>.`, whatever
the failed call was: a `pread`, an `lstat`, an `fstat`, a `fstatvfs`, a `scandir`. Separately, `download` treats an
`EIO` while hashing a same-size file already at a final path (`mirroring._adopt`, wrapped by
`_local_errors(..., 'read')`, whose fatal branch is `FATAL_ERRNOS` → `DestinationError.of`) as a failure of the whole
destination, so one bad sector in one old copy on an aging USB disk stops every later run at that file.

Reproduced on `main` (0d2b423) against the fake server, `tmp_path` destinations, reads of the untracked same-content
copy `Docs/readme.txt` refused through `localfs.read` (scratch test, not committed):

```
EIO     → exit 1
          Error: Could not write /tmp/…/dest/Docs/readme.txt: Input/output error.
          tree after the run: .yandex-disk-rescue/lock, .yandex-disk-rescue/partial/3, Docs/readme.txt  (nothing else downloaded)
EBADMSG → exit 1 (after the summary): 5 downloaded, 0 skipped, 0 quarantined, 1 failed
          disk:/Docs/readme.txt failed: could not read Docs/readme.txt: Bad message
          the unreadable copy stays at Docs/readme.txt, so every later run fails it again
lstat of Docs/readme.txt refused with EIO → exit 1
          Error: Could not write /tmp/…/dest/Docs/readme.txt: Input/output error.
```

The existing test `test_an_error_reading_a_partial_file_being_moved_stops_the_run_naming_that_file`
(`tests/test_download.py:2270`) already locks the wrong verb for a read: `Error: Could not write <partial>: Bad
message.`

## Goal

1. `DestinationError` carries the action that failed — `read`, `write` or `inspect` — and every message it renders is
   `Could not <action> <path>: <reason>.`, so a read, an `lstat`/`fstat`/`fstatvfs` and a write each say what failed.
2. **(User decision — see "Decision for the user"; the plan is written for option A, the recommendation.)** An error
   reading the content of one existing local copy, while DEST itself is still reachable, is a finding about that copy,
   not a failure of the run: the copy is recorded `corrupt`, the file is downloaded and verified, and only then is the
   copy moved aside to `replaced/` — never deleted. The copy is not read again (later passes of this run, later runs,
   `--retry-failed`). The run's summary and `--json` report list every copy found unreadable; the exit code stays `0`
   when everything is mirrored. A DEST that is gone stops the run with `Could not read <path>: <reason>.` An `EIO` while
   *writing* stays fatal, unchanged.

## Decision for the user

An `EIO` (or any non-permission read error) while reading one existing local copy:

- **A — recommended: re-download and move the copy aside.** Treat the copy exactly as `verify` treats an unreadable
  file (`verifying._finding`: corrupt unless DEST is unreachable or the errno says the device is gone): record a
  `corrupt` finding, show the notice `disk:/… corrupt: could not read <local>: <reason>`, download the file, and once
  it is verified move the copy to `replaced/<run>/` as for any differing copy. The run goes on and exits `0` when
  everything else succeeds; the summary lists the copy under `Unreadable in DEST, downloaded again`, so the failing
  disk is visible in the report. Why: the tool targets aging disks, where a bad sector in one old file is the common case
  and a pulled cable the rare one; the Disk holds a good copy, so stopping helps nobody. "Never delete user data"
  holds: the copy is renamed, never removed or truncated, and the rename touches only the directory, not the bad
  sectors. Recording the finding (rather than in-memory state) means a failed or interrupted repair does not re-read
  the bad sectors on the next run, and `verify` lists the copy under `Corrupt` until it is repaired — the same
  contract #5 already defines for copies `verify` found.
- **B — keep stopping the run**, only fixing the message to `Could not read`. The user has to find and move the file
  by hand before any later run gets past it. Stage 2 then shrinks to its README paragraph and the test asserts the
  stop.
- **C — fail only that file and leave the copy in place** (today's behaviour for `EBADMSG`). The run goes on, but
  every later run reads the bad sectors again and fails the same file until the user moves it by hand.

Permission errors on a path of the mirror (`EACCES`, `EPERM`) keep failing only that entry and leaving the path as it
was, under every option (README "The destination", locked by
`test_a_copy_out_of_reach_fails_that_file_only`).

## Non-goals

- Unreadable *partial* files (resume re-check, move to a new `--partial-dir`) — they keep stopping the run, with the
  message now saying `read`; filed as #28.
- `verify`'s own `DestinationUnreadableError` (`Could not read DEST: …; verify stopped. …`) — unchanged.
- An `lstat` failure of a final path other than a permission error: it stays fatal (now `Could not inspect …`); a bad
  inode is not the bad-sector case this issue is about. Listed under risks.
- No new field for a stopped run: its JSON document keeps `error.message` (the action is in the message text).
  The only new report content is Stage 2's `Unreadable in DEST` list.

## Stages

### Stage 1: `DestinationError` names the action that failed

- **Contract:**
  - `destination.py`: `DiskAction = Literal['read', 'write', 'inspect']`.
    `class DestinationError(RescueError)` is constructed as `DestinationError(action: DiskAction, path: Path | str,
    reason: str)`, exposes `.action` and `.path`, and its message is exactly `Could not {action} {path}: {reason}.`
    `DestinationError.of(action, path, exc: OSError)` uses `exc.strerror` as the reason. The one-argument
    constructor and the action-less `of(path, exc)` are deleted; every raise site passes an action. The two
    free-text messages become reasons: `it is not a folder; move it away and run again` and `its filesystem does not
    support renaming without replacing (RENAME_NOREPLACE), which download needs so it never overwrites a file`
    (rendered text unchanged). `ReadOnlyDestination.open` raises `DestinationError('read', …)` (text unchanged).
  - Classification rule: **the action is that of the operation a wrapper (or a `destination.py` method) exists to
    perform — its principal call — not of whichever helper call on the way failed.** Principal calls: opening a file
    read-only, `pread`, listing a folder (`scandir`) → `read`; `lstat`, `fstat`, `fstatvfs` → `inspect`; creating,
    opening for writing, `pwrite`, `ftruncate`, `utime`, `fsync`, rename, unlink, rmdir → `write`. So
    `_Tree.lstat(local)` is `inspect` even when an `open_dir` of a folder on the way fails; `Destination.open` as a
    whole is `write` (it exists to make DEST writable); `remove_staged_copies`, `keep_partials`, `remove_leftovers`
    are `write` (they exist to delete; their `scandir` is on the way). Where one wrapper today covers calls with
    different principal actions, it is split so each wrapped block has one:
    - `download_file`'s outer `_local_errors(..., 'write')` (`mirroring.py:1036`, `:1052`) covers the `lstat`s in
      `_clear_the_way` (`:1183`) and `_alias` (`:1916`), and the in-placement `destination.lstat(local)` in `_adopt`
      (`:1166`): each of those `lstat`s gets its own `inspect` wrapper; the rest of the block stays `write`.
    - The rename and `make_folder` in `_folder_failure` stay `write`.
    - `PartialFile.size` → `inspect`, `PartialFile.chunks` → `read`, its other methods → `write`;
      `partial_free_bytes`, `partial_allocated`, `is_partial_folder`, `PreviousPartials.size` → `inspect`;
      `previous_partials`, `PreviousPartials.partial` → `read`; the partial-move read (`_read_errors`) → `read`; the
      `_check` `lstat`s under `_fatal_errors` → `inspect`.
  - File-level failures (`FileFailed` from `_local_errors`) use the same verbs: an `lstat` of a final path (the first
    look, `_alias`, `_clear_the_way`, the in-placement re-check) fails as `could not inspect <local>: <reason>` (was
    `could not read` or `could not write`); `could not read`, `could not write` and `could not set the modification
    time of` stay for the calls they name.
  - Behaviour is otherwise unchanged: which errnos stop the run and which fail one entry is not touched in this stage.
- **Behaviour to lock** (all in `yandex-disk-rescue/tests/test_download.py`, extending existing tests):
  - `test_an_error_reading_a_partial_file_being_moved_stops_the_run_naming_that_file`: the assertion becomes
    `Error: Could not read {big}: Bad message.`
  - `test_a_copy_out_of_reach_fails_that_file_only[untraversable]`: the failure reason becomes
    `could not inspect Photos/2024/{name}: Permission denied`.
  - New parametrization next to `test_a_full_or_failing_destination_stops_the_run_and_a_rerun_goes_on` (or a new test
    beside it using `_refuse(monkeypatch, 'lstat', errno.EIO, lambda name: name == 'readme.txt')` with an untracked
    copy present): exit 1 and stderr exactly `Error: Could not inspect {dest}/Docs/readme.txt: Input/output error.`
  - The `ENOSPC/EDQUOT/EIO/EROFS` write test and the permission-in-work-dir test keep asserting `Could not write`.
- **Deliverable:** `DestinationError` with the action; every raise site classified; tests above green.
- **Validation:** `mise run check`; `mise run test -- yandex-disk-rescue/tests/test_download.py -k "partial_file_being_moved or permission_error or failing_destination or inspect"`;
  `rg -n "DestinationError\(|DestinationError\.of\(" yandex-disk-rescue/src` shows an action at every site.
- **Doc updates:**
  - `README.md` "The destination" paragraph: rewrite as current state — a full disk or quota, an I/O error or a
    read-only filesystem while writing stops the run with `Error: Could not write <path>: <reason>.`; an I/O error
    while reading or inspecting the work directory, the partial folder or a final path stops it with `Could not read
    …` / `Could not inspect …`; the file-level phrasings list gains `could not inspect <path>: <reason>`. Also the
    `--partial-dir` bullet's `Could not write <path>: No space left on device.` stays (it is a write).
  - `README.md` exit-code table row for `1`: "a destination that cannot be read or written".
  - `destination.py` docstrings: `DestinationError` ("Reading, writing or inspecting the destination failed in a way
    that stops the run; `action` says which"), and the `Destination` class paragraph on errors.
  - `CHANGELOG.md` `## [Unreleased]` → `### Fixed`: "A `download` stopped by a destination error says what failed
    (`Could not read`, `Could not write` or `Could not inspect`), no longer `Could not write` for reads."

### Stage 2: An unreadable local copy is downloaded again and moved aside (option A)

- **Contract:**
  - `destination.py`: `reachable() -> bool` moves from `ReadOnlyDestination` to `_Tree`, so `Destination` has it with
    the same semantics (the path `lstat`ed afresh and the folder listed through a new descriptor). The device-gone
    set moves from `verifying._DEVICE_GONE_ERRNOS` to `destination.DEVICE_GONE_ERRNOS` (`ENODEV`, `ENXIO`, `ESTALE`)
    and both `verify` and `download` use it; no copy stays in `verifying.py`.
  - Scope of the new classification: **only** the three calls that read the copy in `_adopt` — opening it
    (`destination.existing_file(local)`), `file.stat()` and `file.chunks()`. The in-placement re-check
    `destination.lstat(local)`, `_alias`, `file.set_mtime` and everything in `_place_verified` keep the Stage 1
    classification (an `EIO` there stops the run with `Could not inspect …` / `Could not write …`). An `OSError` from
    those three calls is classified as:
    - `EACCES`, `EPERM` → unchanged: the entry fails with `could not read <local>: <reason>`, the path is left as it
      was.
    - `_MISSING_ERRNOS` (the path changed under the run) → unchanged: the entry fails with `could not read …`.
    - an errno in `DEVICE_GONE_ERRNOS`, or `destination.reachable()` is `False` (called through `threads.run`, like
      every other disk call of the run — `reachable()` `lstat`s and lists DEST, which can hang on a failing USB disk
      and must not block the event loop) → the run stops with
      `DestinationError('read', DEST/<local>, reason)`: `Error: Could not read <path>: <reason>.`
    - anything else (`EIO`, `EBADMSG`, `EUCLEAN`, …) → the copy is unreadable: `mirror.record_finding(entry_id,
      Finding('corrupt', f'could not read {local}: {reason}'))`, a WARNING notice in `verify`'s format
      `<cloud path> corrupt: could not read <local>: <reason>`, and the file proceeds as a file with a finding:
      fetched, verified, and only then is the copy moved aside by the existing `_clear_the_way` path; placing it
      drops the finding (`record_verified`). Nothing in the copy is written, truncated or deleted.
  - Invariant: a copy found unreadable is read at most once. A later pass of the same run (the file was deferred)
    and every later run treat the entry as having its finding and never open the copy. (Today a deferred entry is
    retried with the `MirrorEntry` snapshot taken at the start of the run; the implementer must make the later turn
    see the finding — how is internal.)
  - Errors while *writing* keep their classification (`FATAL_ERRNOS` stops the run with `Could not write …`).
  - Reporting (recommendation for the exit-code question): a run whose only event was a repaired unreadable copy
    **exits `0`** — the mirror is complete and every file verified, and exit `1` is reserved for files that are not
    mirrored — **but the bad sector is never only a passing notice**: the summary gets a list, placed right before
    `Moved aside to …`, `Unreadable in DEST, downloaded again: N` with one line per file
    `  <cloud path>: could not read <local>: <reason>` (path order, sorted in `_in_path_order` like `failed`, at most 20, then `… and N more`, as every list),
    and the `--json` document gets `counts.unreadable` and `"unreadable": [{"path": …, "reason": …}]`. A copy found
    unreadable whose repair then failed is listed there as well as under `Failed` (it was found unreadable in this
    run either way). Reason: on an aging disk a read error is the warning that the disk is failing; it belongs in the
    report that `> report.txt` keeps, while the exit code keeps meaning "the mirror is not complete".
- **Behaviour to lock** (`yandex-disk-rescue/tests/test_download.py`, next to
  `test_an_untracked_different_file_is_moved_aside_and_the_cloud_file_placed`; reads refused through `localfs.read`
  by inode, as `test_verify._fail_reads` does):
  - `test_an_unreadable_local_copy_is_moved_aside_and_downloaded_again[EIO|EBADMSG]`: an untracked copy of
    `Docs/readme.txt` with the Disk content whose reads fail, next to the healthy files. Exit `0`; `6 downloaded`;
    stderr carries `disk:/Docs/readme.txt corrupt: could not read Docs/readme.txt: <strerror>`; stdout carries
    `Unreadable in DEST, downloaded again: 1\n  disk:/Docs/readme.txt: could not read Docs/readme.txt: <strerror>\n`
    before the `Moved aside to` list; with `--json` (extend
    `test_json_reports_a_run_with_the_counts_of_its_summary_and_every_failure_and_quarantined_copy` or assert in this
    test) `counts.unreadable == 1` and the `unreadable` entry; the copy is at
    `replaced/<run>/Docs/readme.txt` with its original inode and bytes (read with the injection off); `Docs/readme.txt`
    holds the Disk bytes; the copy's reads were refused exactly once.
  - `test_an_unreadable_local_copy_whose_repair_fails_is_not_read_again`: the same copy, with the download of
    `disk:/Docs/readme.txt` failing transiently on every attempt of run 1 (the fake server's `inject` queue must hold
    a transient fault — e.g. a 503 on the file's download host path — for every attempt of run 1: each immediate
    retry (`--download-retries`, set low, e.g. `1`) of each of the two passes; the test asserts the number of
    attempts it saw so a change in the retry budget fails loudly instead of letting run 1 succeed by accident) and
    `--download-passes 2`: run 1 exits `1`, its stdout has the summary line `…, 0 quarantined, 1 failed` and
    `Failed: 1\n  disk:/Docs/readme.txt: …`, the copy is still at its path, and the `Unreadable in DEST` list names
    it; `verify DEST` lists
    it under `Corrupt` with `could not read Docs/readme.txt: Input/output error`; run 2 (faults off) exits `0`, moves
    the copy aside and places the Disk file. Across both runs and both passes the copy's reads were refused once.
  - `test_an_unreadable_local_copy_on_a_disk_that_is_gone_stops_the_run[EIO|ENODEV]`: the read injector, for `EIO`,
    first renames DEST away (so `reachable()` really fails), for `ENODEV` raises it directly. Exit `1`, stderr exactly
    `Error: Could not read {dest}/Docs/readme.txt: <strerror>.`; no finding is recorded and nothing is moved aside.
  - `test_verify.py` keeps passing unchanged (it locks the shared device-gone set and `reachable()`).
- **Deliverable:** the classification above, `_Tree.reachable`, `DEVICE_GONE_ERRNOS` shared, the `Unreadable in DEST` summary list and JSON field, the three tests green.
- **Validation:** `mise run check`;
  `mise run test -- yandex-disk-rescue/tests/test_download.py -k unreadable_local_copy`;
  `mise run test -- yandex-disk-rescue/tests/test_verify.py`; `rg -n "_DEVICE_GONE_ERRNOS" yandex-disk-rescue` is
  empty; the sweep `rg -n "verify\`? (found|finding|last found)|What \`verify\`|verify_findings" README.md
  yandex-disk-rescue/README.md yandex-disk-rescue/src` shows no text that still says only `verify` makes findings
  (the table name `verify_findings` itself stays — see risks).
- **Doc updates:**
  - `README.md` "Files already in DEST": rewrite as current state — a regular file at a Disk file's path is read and
    hashed; one that cannot be read (an I/O error, a bad sector) is treated like a file `verify` found corrupt: it is
    recorded `corrupt` with the read error, shown as a notice, never read again, and moved aside to `replaced/` once
    the Disk file is downloaded and verified; a permission error still fails only that file; a DEST that can no
    longer be read stops the run with `Error: Could not read <path>: <reason>.`
  - `README.md` "Reruns" paragraph: "A file `verify` found missing, modified or corrupt, or whose copy `download`
    could not read, is downloaded again …".
  - `README.md` "The destination" paragraph (as rewritten in Stage 1): an I/O error *reading a file already in DEST*
    is not among the errors that stop the run; point to "Files already in DEST".
  - `README.md` notices list (Output and logs): add "each local copy found unreadable (`<path> corrupt: could not
    read <local>: <reason>`)".
  - `README.md` "Verifying a mirror": the sentence "A file found missing, modified or corrupt is recorded in the
    manifest at once" stays; add that `download` records an unreadable copy the same way, so `verify` lists it under
    `Corrupt` until it is repaired.
  - `README.md` `--retry-failed` paragraph (around line 478, "a [`verify`] finding (missing, modified or
    corrupt)"): the retry set includes files with a finding — one `verify` made, or an unreadable copy `download`
    found.
  - `README.md` "Verifying a mirror" list, **Corrupt** item (around line 688, which describes the recorded file with
    other content or unreadable): rewrite so it also covers a copy `download` could not read, which need not be a
    recorded file.
  - `README.md` "Output and logs" summary description and the `--json` document example/keys: the `Unreadable in
    DEST, downloaded again` list and `counts.unreadable` / `unreadable`.
  - `commands/download.py` `--retry-failed` help (`:74-76`): "… the files verify found missing, modified or corrupt,
    or download found unreadable, …" (the help text is user-visible; `test_cli.py` help snapshots, if any, follow).
  - `manifest.py`: the module docstring (`:13-14`, "what `verify` last found wrong"), the `verify_findings` schema
    comment (`:202`), `Finding` (`:319-321`), the `MirrorEntry.finding` comment (`:345`) and `record_quarantined`
    (`:762`): a finding is what `verify` found, or `download` found unreadable, at the entry's final path.
  - `verifying.py` module docstring (`:11`, "the copy `verify` found is still at its path"): a finding by either
    command.
  - `mirroring.py` module docstring ("Any other file already at a final path is hashed: …", and "unless `verify`
    found the file at its path broken") and the `download_file` docstring: rewrite as current state, including the
    read-at-most-once invariant; `mirror_summary` / `mirror_document` docstrings for the new list.
  - `yandex-disk-rescue/README.md` layout entry for `mirroring.py`: mention that an unreadable local copy becomes a
    finding; for `destination.py`: `reachable`.
  - `CHANGELOG.md` `## [Unreleased]` → `### Fixed`: "`download` no longer stops at a local copy it cannot read (a bad
    sector): the copy is recorded corrupt, the file is downloaded again, and the copy is moved aside to `replaced/`; the summary lists such copies under `Unreadable in DEST, downloaded again`, and `--json` reports them as `unreadable`."

## Discovered issues

- **Fix in this PR:**
  - `test_an_error_reading_a_partial_file_being_moved_stops_the_run_naming_that_file` locks the wrong verb (Stage 1).
  - The file-level `could not read` for a failing `lstat` of a final path (Stage 1, `could not inspect`).
  - `verifying._DEVICE_GONE_ERRNOS` becomes the shared `destination.DEVICE_GONE_ERRNOS` (Stage 2) instead of a second
    copy in `mirroring.py`.
- **Filed as issues:**
  - #28 — An unreadable partial file (resume re-check, move to a new `--partial-dir`) stops every download run, though
    a partial file is scratch data the Disk can supply again.

## Risks / open questions

- **The user's decision** (A / B / C above) changes Stage 2. Under B, Stage 2 becomes: README "Files already in DEST"
  says such a copy stops the run with `Could not read …` and must be moved by hand, plus one test asserting that stop;
  no `reachable`/finding changes. Under C: the entry fails with `could not read <local>: <reason>` for every
  non-permission read error, the copy stays, and the test asserts the healthy files are downloaded.
- **A disk dropping off mid-hash** may answer `EIO` on the read and still pass `reachable()` for a moment (cached
  dentries). Then the copy is recorded `corrupt` and the next write in the run stops it with `Could not write`; the
  finding only means the next run downloads that file again without reading the copy, and moves the copy aside. No
  data is lost, but a healthy copy may be moved aside once. Same trade-off `verify` already makes.
- **Exit code after a repair**: the plan recommends exit `0` plus the `Unreadable in DEST, downloaded again` summary
  list and JSON field (Stage 2 "Reporting"). Alternative for the user: exit `1` with
  `Error: N local copies could not be read; see the list above.` — rejected in the recommendation because a script
  could no longer tell "mirror incomplete" from "mirror complete, disk aging".
- **`verify_findings` table name**: after Stage 2 `download` writes findings too, but the table keeps its name; renaming
  it needs a manifest schema migration for no user-visible gain. Docstrings and comments say what it holds.
- **`lstat` `EIO` on a final path stays fatal** (`Could not inspect …`). If the user wants the same per-file
  treatment for it, `move_aside` (a rename by name) might still work; that would be a follow-up issue, not this PR.
