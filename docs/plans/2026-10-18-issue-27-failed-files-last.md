# Plan: Try failed files last

Issue: #27 (umbrella #9; split out of #14)
Branch: feat/failed-files-last

## Context

Observed on `main` (0d2b423) against the fake server only, with scratch XDG dirs (a pytest file in the scratchpad
using the repo's `conftest.py`). Two failing files `a-bad{0,1}.bin` (every `/download` answers 503) and three healthy
`b-good{0..2}.bin` in `disk:/M`, `--download-connections 1`:

```
RUN1 (--download-passes 2) exit 1  /download order:
  a-bad0, a-bad1, b-good0, b-good1, b-good2, a-bad0, a-bad1
# add b-new{0..2}.bin, rerun with --scan-max-age 0 --download-passes 1
RUN2 exit 1  /download order:
  a-bad0, a-bad1, b-new0, b-new1, b-new2
```

- **Within a run the order is already right.** Pass 1 walks every file once; only files deferred by a transient
  failure go to pass 2, after pass 1 has ended (`MirrorRun._first_pass` / `_later_passes`, mirroring.py:780-880).
  Nothing in pass 1 is retried. This only needs a test that locks it.
- **On a restart, known-bad files come first.** Pass 1 walks `self._entries` in path order
  (mirroring.py:796-815), and failure records play no part. This is the 0 B/s start seen on the live mirror.
- **A run stopped between passes records nothing.** A deferred file gets its failure record only when the last pass
  gives up (`_fail` from `_later_passes`, mirroring.py:877-879). The README says so: "Files stopped by Ctrl+C … get no
  failure record". On a 2.1 TiB mirror, which a user stops and restarts long before pass 1 ends, the files that
  failed in pass 1 leave no trace, so ordering by manifest records alone would not help that case.
- **A failure record has no count.** `failures(mirror_id, entry_id, reason)` (schema v9, manifest.py:176-185). The
  issue's "fewer failures before more" rule needs one number per record. That number is the one new piece of state.
  The issue's "no new state" decision is about a separate queue or timer, and this plan adds neither.

## Goal

- Every failed turn of a file is recorded when it fails, and counted. A turn deferred by a transient transfer failure
  counts too, so a run stopped between passes still leaves the record.
- Every pass starts its files in one order. First, files with no failure record, in path order. Then files with one,
  fewest failed turns first, ties in path order. Pass 1 of a run therefore reaches untried and healthy files first,
  and later passes still hold only the files the pass before deferred.
- `--download-passes` still limits the turns per file per run. Reports, JSON, exit codes and the status line stay
  as they are.

## Non-goals

- Back-off timers, per-folder or per-host health tracking, preview fallback (#26), and the "unavailable on Yandex
  Disk" reason (#14).
- Showing the failed-turn count in the summary, the JSON report or `verify`.
- Reordering folders. Folders are still made in the path-order walk, before anything below them.
- Changing the `--retry-failed` selection rule (`_retry_walk`, mirroring.py:1839-1851). It does not change, but
  more files now carry a failure record. A file interrupted after a deferral now has one, for example after a stall
  before its first byte. Such a file is in the retry set, and `verify` shows its deferral reason (Stage 1).

## Stages

### Stage 1: Failure records count failed turns and are written when each turn fails

- **Contract:**
  - Schema migration 10, appended to `_MIGRATIONS`, with an explanatory comment like its neighbours:
    `ALTER TABLE failures ADD COLUMN turns INTEGER NOT NULL DEFAULT 1 CHECK (turns >= 1)`. `SCHEMA_VERSION` becomes 10.
    The failed turns of an existing record are unknown, so it upgrades with `turns = 1`.
  - `manifest.FailureRecord`, a frozen dataclass: `reason: str` (the user-facing reason the entry's last failed turn
    was reported with) and `turns: int` (the entry's failed turns in this mirror since it last succeeded, at least 1).
  - `MirrorEntry.failure: FailureRecord | None`, replacing `str | None`. Every consumer reads `.reason`: mirroring's
    `held_copy` calls and `_retry_walk`, verifying's `held_copy` call and `_not_downloaded_reason`.
  - `Mirror.record_failure(entry_id: int, reason: str, *, turns: int) -> None` replaces the record's reason and count.
    It raises `ValueError` when `turns < 1`, and keeps today's `ValueError` for an entry outside the mirror. The caller
    owns the count. Restating a reason (the `(after N passes)` suffix at the end of the passes) passes the same
    `turns`, so it is never counted twice. `clear_failure`, `record_verified`, `record_quarantined` and `forget` keep
    their current effect on the record.
  - `MirrorRun` keeps every entry's failed turns for the run. It starts from `entry.failure.turns` (0 when there is no
    record) and adds 1 for each turn of this run that fails. Recording rules:
    - A turn that raises `FileFailed`, a folder that cannot be made, and an entry below such a folder each count one
      failed turn and record it at once (as today, now with the count).
    - A turn deferred by a transient failure (`FileDeferred` that is not `NoRoomYet`) counts one failed turn and
      records its reason **when it is deferred**.
    - A turn deferred for room (`NoRoomYet`) did not try the file. It counts and records nothing.
    - **The end-of-run report rule.** A file is reported failed after the passes (stuck for room, or out of passes).
      That report adds one failed turn only when this run counted no failed turn for the file. Otherwise it
      restates the record with the reported reason (`… (after N passes)`, or the final no-room reason) and the
      same count. A mixed history therefore works out as follows. A file with a transient failure in pass 1 and
      only `NoRoomYet` in later passes counts 1 failed turn in this run, and is reported with the final reason. A
      file deferred only for room counts 1 when it is finally reported.
    - A turn cancelled by Ctrl+C, or by an error that stops the run, counts and records nothing. The file keeps the
      record of its last failed turn, if it has one.
  - Ordering and the walk are unchanged in this stage.
- **Behaviour to lock:**
  - `tests/test_manifest.py`:
    - Extend `test_a_failure_is_replaced_kept_by_forget_and_dropped_by_verifying_or_clearing` so `_failures` returns
      `FailureRecord`s. Assert that `turns` is stored as given and replaced along with the reason, and that
      `record_failure(..., turns=0)` raises `ValueError` and writes nothing.
    - `test_opening_an_existing_manifest_creates_nothing_and_still_takes_the_lock_and_upgrades` (test_manifest.py:
      214-240) rolls the last migration back by hand. Today it drops `mirrors.partial_folder`, and with v10 that
      fails with `duplicate column name`. Rewrite it to roll back migration 10. Before the rollback, open the mirror
      and record one failure. Then run `ALTER TABLE failures DROP COLUMN turns` and set
      `PRAGMA user_version = {SCHEMA_VERSION - 1}`. After `open_existing`, assert the row reads back as
      `FailureRecord(<reason>, 1)`. This test is the v9 → v10 upgrade test, so no `manifest-v9.sql` fixture is added.
      If a fixture is ever needed, it is `Connection.iterdump()` via `uv run python` with the header pragmas first,
      like `tests/data/manifest-v4.sql`. The devcontainer has no `sqlite3` CLI.
    - Bump the existing `SCHEMA_VERSION == 9` asserts to 10.
  - `tests/test_download.py` (`_failure_records()` also returns `turns`):
    - `test_a_file_stalled_on_every_pass_fails_after_the_last_and_the_others_succeed`: the record is
      `(BIG_PATH, '… (after 2 passes)', 2)`. A second identical run fetches the file twice more and leaves `turns`
      at 4.
    - `test_ctrl_c_in_the_pause_between_passes_stops_the_run_and_keeps_the_partial_file`: after the interrupt, the
      record is `(BIG_PATH, 'the download stalled: no data for 1 s', 1)`. Today there is none. Extend the test:
      `verify DEST` then lists `big.bin` under not downloaded with that reason. No test asserts the old text,
      "its download has not finished", for this case. A following `download --retry-failed disk:/Big DEST` then
      covers it (`Retrying 1 failed, …`) and places it.
    - `test_a_file_stalled_in_one_pass_resumes_in_the_next_with_a_fresh_link`: `_failure_records() == []` once the
      file is placed. The record written at the deferral is dropped by the verified record.
    - `test_a_file_waiting_for_room_is_deferred_when_the_last_running_file_is_deferred_and_a_pass_with_room_ends_both`
      (or the room test at line 2485 that asserts the `waited` reason): a file deferred only for room has no record
      until it fails, and then `turns == 1`. Add the mixed case in the same file's room tests: a file with a
      transient failure in pass 1 and only `NoRoomYet` afterwards ends with `turns == 1`, not 2, and its record
      holds the reason it is reported with.
- **Deliverable:** schema v10 with counted failure records, written when each turn fails. Nothing else in the run's
  behaviour or output changes.
- **Validation:** `mise run check`; `mise run test -- yandex-disk-rescue/tests/test_manifest.py`;
  `mise run test -- yandex-disk-rescue/tests/test_download.py -k "stalled or ctrl_c_in_the_pause or room"`;
  `mise run test -- yandex-disk-rescue/tests/test_verify.py`.
- **Doc updates:**
  - `yandex-disk-rescue/README.md` § Manifest, failure-record bullet. Rewrite as current state: the reason of the
    entry's last failed turn and the count of failed turns since it last succeeded, written by
    `Mirror.record_failure(entry_id, reason, turns=…)`, read as `MirrorEntry.failure` (a `FailureRecord`). Add that
    a manifest from schema version 9 or older upgrades each record to 1 turn.
  - `yandex-disk-rescue/README.md` § Layout: the `mirroring.py` line "failure records written as entries fail" now
    covers turns deferred to a later pass.
  - Root `README.md` § "Retrying failures": rewrite the first paragraph as current state. A record is written as soon
    as a turn fails, including a transient failure waiting for a later pass. It holds the reason last shown and the
    count of failed turns. A turn stopped by Ctrl+C or a run-stopping error records nothing, and the file keeps any
    earlier record and its partial download. Delete the sentence "Files stopped by Ctrl+C … get no failure record".
  - `manifest.py` module docstring and the `failures` comment: "the reason the last turn failed" becomes "the reason
    and count of the entry's failed turns".
  - `mirroring.MirrorRun` docstring, paragraph "Every entry that fails gets a failure record …": rewrite to the
    recording rules above.
  - `CHANGELOG.md` `## [Unreleased]` → `### Changed`: a file whose transfer failed is recorded as failed at once, so
    a run stopped between passes still lists it for `--retry-failed` and `verify`.
  - `rg "failure: str|\.failure\b|record_failure|get no failure record"` over `yandex-disk-rescue/`, `README.md` and
    `docs/` (not `docs/plans/`): no stale mention.

### Stage 2: Each pass starts files with no failure record first, then the failed ones by fewest failed turns

- **Contract:**
  - The order of a pass is the sort key `(failed turns so far in this mirror, position in the walk)`, where the
    position is the entry's index in path order (`self._positions`). Pass 1 still walks every entry in path order:
    folders are made and held, current and blocked entries are handled there. During that walk it starts every file
    with no failed turns. After the walk it starts the files with failed turns, in key order. So a known-failed file
    starts only after every untried file of the run has started.
  - A file started after the walk checks `_blocked_by` at that point, against the folders failed so far. A file below
    a folder that failed in this run fails with that folder's reason, wherever it is in the order.
  - Later passes start their deferred files in the same key order, with this run's failed turns included. Today they
    start in path order (`_sorted`). The report lists stay in path order (`_in_path_order`).
  - `--retry-failed` uses the same order over its walk. Partial downloads, verify findings and quarantined files
    without a failure record start before files with one.
  - The order has no effect on whether a partial download is resumed. A file with confirmed bytes resumes from its
    journal whenever its turn comes. Room turns (`PartialRoom`) are taken in the order files start, not in path order.
  - `--download-passes`, `MirrorReport.passes`, the summary, the JSON object, the status line and the exit codes are
    unchanged. The tail of pass 1 is still pass 1, so each file still gets at most `passes` turns in a run.
  - `_log_pass` for pass 1 also logs how many files start last because they failed before, for example
    `Pass 1 of 7 over 120 files (3.1 GiB) starts; 12 that failed before start last`. Omit the clause when there are
    none. Log file only; stdout is unchanged.
- **Behaviour to lock (`tests/test_download.py`, next to `test_dead_files_do_not_hold_up_the_healthy_ones`):**
  - `test_files_failing_in_a_pass_are_tried_again_only_after_every_healthy_file_finished`: 3 failing (503 on every
    `/download`) and 8 healthy files, `--download-connections 4 --download-passes 2`. Every healthy file's `/download`
    request has `finished` before the start of any failing file's second `/download` request, and every failing file
    is fetched exactly twice. This is the issue's acceptance test. It already passes on `main`, and the test locks it.
  - `test_a_rerun_starts_files_never_failed_first_then_the_failed_ones_fewest_failures_first`:
    `--download-connections 1 --download-passes 1 --scan-max-age 0`, in `disk:/M`. Run 1: `a-x` fails. Run 2: add
    `c-y` (fails), and `a-x` fails again. Run 3: add `d-z` (healthy). The `/download` order of run 3 is
    `[d-z, c-y, a-x]`. The `turns` of `_failure_records()` are `a-x: 3`, `c-y: 2`. This fails on `main`, where the
    order is `[a-x, c-y, d-z]`. The log file of run 3 (`log_messages`) contains
    `Pass 1 of 1 over 3 files (…) starts; 2 that failed before start last`. Runs without earlier failures, such as
    run 1, have no such clause.
  - `test_with_a_partial_folder_an_untried_file_waits_for_the_room_a_failed_files_kept_partial_frees`
    (with `partial_root`, next to the room tests). Known-failed `F` (path first) has an idle kept partial. Untried
    `U` fits only once `F`'s partial is gone. In pass 1, `U` starts first and is deferred with `no room yet …`,
    with no failed turn counted and no failure record. `F` then resumes and is placed, freeing its room. Pass 2
    places `U`. The summary has `Passes: 2 of 7` and no failure.
  - `test_a_failed_partial_download_resumes_from_its_journal_after_the_untried_files`: `_leave_partial(big, dest)`
    leaves `big.bin` with 300 000 confirmed bytes and a failure record. Then add `disk:/Big/c-new.bin`. A rerun of
    `disk:/Big` with `--download-connections 1 --scan-max-age 0` fetches `c-new.bin` first, then `big.bin` with
    `Range: _tail(300_000)` only, and places both. Without `--scan-max-age 0` the rerun reuses the old scan and
    never sees `c-new.bin`.
  - `test_retry_failed_starts_the_files_with_no_failure_record_first`: `disk:/Docs/readme.txt` fails (passes 1).
    Delete `Photos/2024/a.jpg` from DEST and run `verify` (a `missing` finding, no failure record). Then run
    `--retry-failed --download-connections 1`. The `/download` order is `[disk:/Photos/2024/a.jpg,
    disk:/Docs/readme.txt]`, the reverse of path order.
  - Keep green without edits: `test_dead_files_do_not_hold_up_the_healthy_ones`, the room tests (2407-2610) and the
    `--retry-failed` tests.
- **Deliverable:** restarts and every pass reach untried files first. Known-bad files go to the end, fewest failures
  first.
- **Validation:** `mise run check`; `mise run test -- yandex-disk-rescue/tests/test_download.py -k "tried_again or
  rerun_starts or failed_partial or retry_failed or room or untried_file_waits or dead_files"`; `mise run ci`.
- **Doc updates:**
  - Root `README.md` § download, first paragraph: "with up to `download_connections` files … downloading at once,
    started in path order" becomes the current rule. Add a short paragraph **Order** right after it: files with no
    failure record start in path order; files that failed before start after all of them, fewest failed turns
    first, ties in path order; each later pass follows the same rule over the files still to download; a partial
    download keeps its confirmed bytes whenever its turn comes; folders are still made before anything inside them.
    With `--partial-dir`, a file that fits only once an earlier failed file's kept partial is gone may meet
    `no room yet` in pass 1 and get the room in the next pass. With `--download-passes 1` it fails with that reason,
    and the next run or `--retry-failed` fetches it.
  - Root `README.md` § Partial files: "Files take their room in path order" becomes "in the order they start".
  - Root `README.md` § "Retrying failures": "The run then visits only the retry set … in path order" becomes "in the
    order above".
  - `yandex-disk-rescue/README.md` § Layout, `mirroring.py` line: "in path order: wait, defer …" becomes "in the
    order files start"; add "files with failure records started last, fewest failed turns first".
  - `mirroring.py`: module docstring (line 43, "in path order"), the `MirrorRun` docstring ("`run` walks the run's
    entries in path order") and `MirrorRun.run` ("started in path order as slots free up") rewritten to the rule.
  - `CHANGELOG.md` `## [Unreleased]` → `### Changed`: `download` starts files that failed before only after the
    others, fewest failures first, so a restart reaches new data at once.
  - `rg -n "path order" README.md yandex-disk-rescue/` and fix each mention that is about download order. The report
    lists and `verify` stay in path order.

## Discovered issues

- **Fix in this PR:** the README promise "Files stopped by Ctrl+C … get no failure record" also hid that a file
  failing in pass 1 of an interrupted run leaves no trace (Stage 1).
- **Filed as issues:** none. Nothing out of scope came up.

## Risks / open questions

- **Count semantics.** `turns` counts failed turns, so a file that fails 7 passes in one run gets 7, and a file that
  fails once in a run stopped early gets 1. The issue says "failed fewer times", and this is the most direct reading.
  Counting failed runs instead would rank a file stalled in all 7 passes the same as one that failed once.
- **Issue wording vs. new state.** The issue's design decision says "no new state is needed". The `turns` column is
  new, and it is the minimum the "fewer failures before more" deliverable needs. Dropping that rule would remove
  the migration, but restarts would then order known-bad files only by path.
- **`verify` wording shifts.** For a file stopped between passes, `verify` used to list it under "not downloaded"
  with "its download has not finished". It now lists the last failed turn's reason, for example
  `the download stalled: no data for 15 s`. That is more accurate, but it changes the visible text. Stage 1 runs
  `test_verify.py` to catch any assertion on the old text.
- **Schema number collision.** The parallel plans (#15, #16, #23) do not touch the schema now. If one lands a
  migration first, this one is renumbered and the fixture regenerated.
- **Partial-folder room with kept partials at the tail.** Large kept partial files of failed downloads still count
  their allocated bytes while untried files run first. With `--partial-dir`, an untried file that fits only after a
  failed file's partial is gone now meets `no room yet` in pass 1 and is placed in pass 2. With
  `--download-passes 1` it fails for that run. Stage 2 locks this with a test and documents it in the README order
  paragraph.
- **Decision for the user: the `turns` column.** The plan keeps it. The alternative needs no migration: order only
  by "has a failure record" (failed files last, in path order). That alternative drops the issue's "fewer failures
  before more" rule.
