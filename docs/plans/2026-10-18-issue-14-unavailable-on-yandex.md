# Plan: Report files Yandex Disk never sends as "unavailable", and stop paying full price for them

Issue: #14 (umbrella #9; split: ordering is #27, previews are #26)
Branch: feat/unavailable-on-yandex

Builds on the approved plans that land first, in order: #23 → #15 → #16 → #27 → #18. This plan depends on #27:
schema v10 `failures.turns`, `manifest.FailureRecord`, `MirrorEntry.failure: FailureRecord | None`,
`Mirror.record_failure(entry_id, reason, *, turns)`, a failure record written as each turn fails (deferred turns
included), and the pass order "never failed first, fewest failed turns first". The migration number below (**v11**)
is the next after #27. It is subject to rebase if anything else lands a migration first.

## Context

Observed on `main` (0d2b423) against the fake server, in a scratch test that uses the repo's `conftest.py`. Three
files never send a byte:

- `napkin.jpg` stays silent past the stall timeout;
- `http500.jpg` answers HTTP 500;
- `peer.jpg` sends headers, then closes the connection with 0 bytes.

Each run used `--download-timeout 1 --download-passes 2`, three runs in a row:

```
RUN 1 exit 1   /download: each dead file twice (pass 1 and pass 2), healthy files once
Failed: 3
  disk:/Dead/http500.jpg: the download host 127.0.0.1 returned HTTP 500 (after 2 passes)
  disk:/Dead/napkin.jpg: the download stalled: no data for 1 s (after 2 passes)
  disk:/Dead/peer.jpg: the download failed: peer closed connection without sending complete message body (received 0 bytes, expected 5000) (after 2 passes)
RUN 2 exit 1   identical output, each dead file fetched twice again
RUN 3 --json   "failed": [{"path": …, "reason": "… (after 2 passes)"}, …]   no field says "never started"
```

- **Nothing tells a dead file from a transient failure.** The summary, the `--json` report, the failure record and
  `verify` all show the last transient reason. That is the same text as for a file that fails once in a bad hour.
  The maintainer confirmed that one of the 11 files on the live account (`…/christmas-decor-napkin1-2.jpg`) does not
  download in the Yandex web UI either, and its `antivirus_status` is `clean` (probe, 2026-10-18). So no other
  endpoint will help. What is left is a distinct reason that the user can take to Yandex support.
- **Every run pays full price for every dead file.** With the default 7 passes, each dead file costs 7 turns per
  run. Each turn is a new link request plus up to `download_timeout` (15 s) of a held connection. With
  `--download-passes 0` ("until every file is done"), one dead file keeps the run going forever.
- **Silent turns pile up fast within one run.** A later pass holds only the deferred files. It lasts about
  `download_timeout` plus the 5 s pause (`PASS_PAUSE_SECONDS`, mirroring.py:100), and a 503 or a close fails at once.
  `_stream` defers on the first attempt that writes nothing (mirroring.py:1703-1708). So 7 silent turns can be spent
  in 2–3 minutes. A count alone would label a whole folder cluster after a short storage-node blip. The rule must
  also need **time to pass**. The maintainer wants exactly that: give Yandex's infrastructure the chance to recover.
- **#27's `turns` is not enough.** It counts every failed turn, so a local write error, a refused name and a stall
  all count the same. Telling "the Disk sent nothing" apart needs to know, per turn, whether any byte arrived.

## Goal

- A failure record tracks the entry's current **silent streak**: how many of its most recent failed turns in a row
  were silent, and when the first of them happened. A turn is silent when the transfer failed transiently and the
  download host sent no byte of the file. Throttling (429, or any answer with `Retry-After`) is neither silent nor a
  break in the streak.
- A file is **unavailable on Yandex Disk** once its streak has at least `UNAVAILABLE_TURNS` (7) silent turns and has
  lasted at least `UNAVAILABLE_AGE` (24 h), from its first silent turn to now. It is reported under `Failed` with a
  reason that says so and gives the streak's start time. The summary adds one hint line about the web UI and Yandex
  support, and `--json` flags such failures. Every failure record written while the file is unavailable carries
  that reason, so `verify` shows it, even after an interrupted run.
- A plain `download` gives an unavailable file one turn per run, not `--download-passes` turns. A silent turn fails
  it at once. A file Yandex restores is fetched on the next run, at the cost of one turn. `--retry-failed` still
  gives it every pass.

## Non-goals

- Another download endpoint or request shape. The web UI fails too (issue comment, 2026-10-17).
- A shorter stall timeout for unavailable files. The one turn uses `download_timeout` like any other.
- Previews of such files (#26).
- A download-link request (Disk API) that keeps failing for one file. Today that stops the whole run. Filed as #30.
- Making `UNAVAILABLE_TURNS` / `UNAVAILABLE_AGE` options (see D1).

## Decisions for the user

- **D1. When a file counts as unavailable. Recommended: at least 7 silent turns in a row, and at least 24 h since
  the first of them, both constants.**
  - The count alone fails the case in Context. 7 silent turns fit into one 2–3 minute blip of a storage node.
    Requiring 24 h of silence means Yandex had a day to recover. A file is labelled only when it stayed silent on
    every attempt since.
  - With the defaults, a dead file is retried at full cost (7 turns per run) for the first day, as today. The first
    run after 24 h labels it. With `--download-passes 1` (cron style), it takes 7 runs and at least a day.
  - Alternatives:
    - (a) The count alone: no timestamp column, but it mislabels after a blip.
    - (b) At least 2 separate runs instead of a duration: needs a run counter. Two runs a minute apart (a restart)
      are no evidence that time passed.
    - (c) Options for both numbers: more knobs to explain for little gain. The constants are monkeypatchable in
      tests.
- **D2. What a plain run does with an unavailable file. Recommended: B, one turn per run.**
  - **A:** skip it unless `--retry-failed`, as quarantined files are held. This costs nothing per run. But a file
    Yandex restores is never fetched by a plain run, and a mislabelled file waits for the user.
  - **B: one turn per plain run, never deferred after a silent turn.** It stays in pass 1, at the tail by #27's
    order, because it has the most failed turns. The cost is one link request and at most `download_timeout` of one
    connection.
    - The price falls on a file that is flaky but alive, silent with probability p per attempt. Unlabelled, it
      needs about 1/(1−p⁷) runs to finish. Labelled, it needs 1/(1−p): for p = 0.8, 5 runs instead of about 1.3.
    - D1's 24 h rule keeps such files from being labelled. For p = 0.8, the chance of 7 silent attempts in a row
      is 0.21 per run, and the streak would also have to survive every run of a whole day.
  - **C:** a label only, at full cost every run. This is the cost problem in Context, and with
    `--download-passes 0` a run never ends.
- **D3. How it is reported. Recommended: in the `Failed` list with its own reason, plus a hint line and a JSON
  flag.**
  - The head line, the counts, the exit code (`1`) and `--retry-failed` stay as for any failure.
  - Alternative: a separate `Unavailable on Yandex Disk: N` list, counted apart from `failed` in the head line. That
    changes the head line, the `Error: …` line, the counts and the exit-code docs, for what is still a failure.

The plan below is written for D1 = 7 turns and 24 h, constants; D2 = B; D3 = in `Failed`.

## Stages

### Stage 1: Failure records keep the silent streak; 7 silent turns over 24 h make a file unavailable

- **Contract:**
  - **Schema migration 11** (number subject to rebase), appended to `_MIGRATIONS` with an explanatory comment like
    its neighbours. It runs:
    - `ALTER TABLE failures ADD COLUMN silent_turns INTEGER NOT NULL DEFAULT 0 CHECK (silent_turns >= 0)`
    - `ALTER TABLE failures ADD COLUMN silent_since TEXT CHECK ((silent_turns = 0) = (silent_since IS NULL))`

    `silent_since` uses the manifest's fixed-width UTC form (manifest.py:1218). `SCHEMA_VERSION` becomes 11. A v10
    record upgrades with no streak (`0`, `NULL`), because its history is unknown.
  - **`manifest.FailureRecord`** gains `silent_turns: int` and `silent_since: datetime | None`. Both are set
    together: `0 <= silent_turns <= turns`, and `silent_since` is not `None` exactly when `silent_turns > 0`.
  - **`Mirror.record_failure(entry_id, reason, *, turns, silent_turns, silent_since)`** raises `ValueError`, and
    writes nothing, when:
    - the two silent fields break the rule above, or `silent_turns > turns`;
    - #27's `turns < 1`;
    - the entry is outside the mirror.

    Restating a record (#27's end-of-run report rule) passes the same streak.
  - **How a turn reports what happened.** `mirroring.FileDeferred` gains two fields:
    - `received: bool` is true when any chunk of the file was written during the turn, by any attempt of any
      segment. It is counted at the write callback, so a `RangeNotHonoured` restart and writes not yet checkpointed
      still count.
    - `throttled: bool` is true when the deferring error was HTTP 429, or carried `Retry-After`
      (`TransientTransferError.retry_after is not None`). For a segmented file it is true when any segment's
      deferral was throttled, as `retry_after` is already the maximum over the segments (mirroring.py:1660-1664).

    `NoRoomYet` has both false.
  - **A silent turn** is a failed turn, counted by #27, that ended deferred by a transient transfer failure with
    `received` and `throttled` both false. Examples: a stall before the first byte, 408 or 5xx without
    `Retry-After`, a refused or reset connection, headers then a close with 0 bytes, and a refreshed link rejected
    again.
  - **Streak rules.** `MirrorRun` keeps every entry's streak for the run next to #27's failed turns. It starts from
    the record (none means `0`, `None`).
    - A silent turn adds 1. The first one of a streak also sets `silent_since` to the turn's end time.
    - A throttled deferral, `NoRoomYet`, and any turn #27 does not count (a cancelled one) leave the streak
      unchanged.
    - Any other failed turn ends the streak (`0`, `None`): a deferral that received bytes, any `FileFailed`, a
      folder failure, or an entry below a failed folder. So does #27's end-of-run report when it adds a turn,
      because this run counted no failed turn for the file.
  - **`mirroring.UNAVAILABLE_TURNS = 7` and `mirroring.UNAVAILABLE_AGE = timedelta(hours=24)`.** A file is
    **unavailable** when `silent_turns >= UNAVAILABLE_TURNS` and `now - silent_since >= UNAVAILABLE_AGE`, where
    `now` is the run's clock at the moment its record is written. Every record written while that holds, whether a
    deferral's (#27 writes one per deferred turn) or a report's, carries the unavailable reason, not the turn's own.
    The reason is:
    - with no confirmed byte in its download record: `Yandex Disk never started sending this file: no data in its
      last N attempts since <silent_since, YYYY-MM-DD HH:MM UTC>, each with a new download link (the last: <reason
      of the last silent turn, without "(after N passes)">)`
    - with confirmed bytes: `Yandex Disk stopped sending this file after <size of the confirmed bytes>: no data in
      its last N attempts since <…>, each with a new download link (the last: <…>)`

    The prefixes `Yandex Disk never started sending this file` and `Yandex Disk stopped sending this file` are the
    contract. The implementer may tune the rest.
  - **Report and JSON.**
    - `mirroring.Failure` gains `unavailable: bool` (default `False`).
    - `mirror_summary`: when any failure is unavailable, one line right after the `Failed` list: `U of them never
      got data from Yandex Disk for a day or more: open them in the Yandex Disk web interface, and ask Yandex
      support about those that do not open there either.` (singular `1 of them …`).
    - The head line, `Error: …` and the exit code are unchanged.
    - `mirror_document` gains `counts.unavailable` (int) and `failed[].unavailable` (bool). `schema_version` stays
      `1`, because only keys are added.
  - **`verify`** needs no code change. It already lists a file that is not downloaded with its failure record's
    reason (`verifying._not_downloaded_reason`, read as `entry.failure.reason` after #27).
- **Behaviour to lock:**
  - **`tests/test_manifest.py`:**
    - Extend #27's failure-record test: the streak is stored and replaced together with the reason and `turns`.
      Each of these raises `ValueError` and writes nothing: `silent_turns=3` with `turns=2`, `silent_turns=-1`,
      `silent_turns=2` with `silent_since=None`, and `silent_turns=0` with a time.
    - Roll the upgrade test forward to v10 → v11: record a failure, drop both columns, set
      `PRAGMA user_version = {SCHEMA_VERSION - 1}`, run `open_existing`, and assert the record reads back as
      `FailureRecord(<reason>, <turns>, 0, None)`.
    - Bump the `SCHEMA_VERSION` asserts.
  - **`tests/test_download.py`.** `_failure_records()` also returns `silent_turns` and `silent_since`, and
    `PASS_PAUSE_SECONDS` is already 0 there. Add a helper `_age_silence(path, hours)` that moves the file's
    `silent_since` back in the manifest (a real SQLite write between runs, like `_failure_records`' read).
    - `test_seven_silent_turns_in_one_run_are_not_yet_unavailable`: `disk:/Dead/napkin.jpg` answers 503 on every
      `/download`, run with `--download-passes 7`. The reason is the plain `… (after 7 passes)`, the record has
      `silent_turns == 7` and a `silent_since`, and there is no hint line.
    - `test_a_file_silent_for_a_day_fails_as_unavailable_and_the_others_succeed`: after the run above,
      `_age_silence(path, 25)` and rerun with `--retry-failed --download-passes 1`, so the plain-run rule of
      Stage 2 does not apply. Exit 1. The `Failed` line starts with `Yandex Disk never started sending this file:
      no data in its last 8 attempts since ` and ends with `(the last: the download host 127.0.0.1 returned HTTP
      503)`. The next line is the hint. The record has that reason and `(…, 8, 8, <aged time>)`. Every other file
      is verified.
    - `test_any_byte_ends_the_silent_streak`: aged and unavailable as above, then serve `Truncate(4)` on every
      turn with `--retry-failed --download-retries 0 --download-passes 1`. The reason is the plain truncation
      reason, and the record is `(…, 9, 0, None)`.
    - `test_throttling_is_not_silence`: 429 on every `/download`, `--download-passes 7`, and 25 h aged between two
      such runs. The reason is the plain 429 reason, and `silent_turns == 0`. A 503 with `Retry-After: 1` behaves
      the same.
    - `test_a_file_that_stops_sending_midway_is_reported_as_stopped_after_its_confirmed_bytes`: `_leave_partial`
      leaves 300 000 confirmed bytes of `big.bin`. Then come 7 silent turns, aged, and one more. The reason starts
      with `Yandex Disk stopped sending this file after ` and the `format_size(300_000)` text.
    - `test_a_local_error_ends_the_silent_streak`: a file with a streak of 7, aged, whose next turn fails with a
      permission error on its final path (the setup of
      `test_a_copy_out_of_reach_fails_that_file_only`). It ends with `silent_turns == 0`
      and its permission reason.
    - `test_ctrl_c_between_passes_records_the_unavailable_reason_once_it_applies`: aged with a streak of 6, then
      `--retry-failed --download-passes 3`. The 7th silent turn is deferred, and Ctrl+C comes in the pause (the
      setup of #27's `test_ctrl_c_in_the_pause_between_passes_…`). The record holds the unavailable reason, and
      `verify DEST` lists it under not downloaded.
    - Extend `test_json_reports_a_run_with_the_counts_of_its_summary_and_every_failure_and_quarantined_copy`:
      `counts` gains `'unavailable': 0`, and the `failed` item has `unavailable: False`. A `--json` twin of the
      unavailable test has `counts.unavailable == 1` and `failed[].unavailable is True`.
    - Keep green without edits: `test_a_file_stalled_on_every_pass_fails_after_the_last_and_the_others_succeed`.
  - **`tests/test_verify.py`:** extend `test_files_not_downloaded_and_quarantined_are_listed_without_being_read`,
    or add a sibling. After the unavailable run, `verify DEST` lists the file under not downloaded, with the
    `Yandex Disk never started sending this file: …` reason.
- **Deliverable:** schema v11 and the silent streak. Unavailable files are reported distinctly in the summary,
  `--json` and `verify`. Run cost is unchanged in this stage.
- **Validation:**
  - `mise run check`
  - `mise run test -- yandex-disk-rescue/tests/test_manifest.py`
  - `mise run test -- yandex-disk-rescue/tests/test_download.py -k "unavailable or silent or throttling or
    stops_sending or json_reports or stalled or ctrl_c"`
  - `mise run test -- yandex-disk-rescue/tests/test_verify.py`
- **Doc updates:**
  - **Root `README.md`:**
    - § Downloading, "Interruptions, stalls and retries", **Passes** bullet: rewrite as current state.
      - The rule: a file whose download host sent no data on its last 7 attempts in a row, over at least a day, is
        reported as never started, or as stopped after its confirmed bytes. Quote the reason.
      - The hint line.
      - That the count spans runs.
      - That 429 / `Retry-After` answers do not count.
      - That a byte, or a failure for another reason, starts over.
    - § "Retrying failures": the record holds the reason, the failed-turn count (#27) and the silent streak.
    - § Summary example: add one unavailable failure and the hint line.
    - § JSON reports, `download`: add `unavailable` to `counts` and to `failed` items, with one sentence.
    - § Verifying a mirror: check that the not-downloaded text agrees with the record's reason.
  - **`yandex-disk-rescue/README.md`:**
    - § Manifest, failure-record bullet: add the streak columns. A v10 manifest upgrades with none.
    - § Layout, `mirroring.py` line: the silent streak and the unavailable reason.
  - **Docstrings:** the `manifest.py` module docstring and the `failures` migration comment; the
    `mirroring.MirrorRun` docstring (recording rules); `FileDeferred`; the `mirror_summary` / `mirror_document`
    docstrings.
  - **`CHANGELOG.md`** `## [Unreleased]` → `### Added`: a file Yandex Disk sends no data for, on 7 attempts in a
    row over at least a day, is reported as unavailable on Yandex Disk in the summary, `--json` and `verify`.
  - **Stale mentions:** `rg -n "record_failure|FailureRecord|after N passes" README.md yandex-disk-rescue/
    docs/*.md`.

### Stage 2: A plain run gives an unavailable file one turn, and a silent turn ends it for the run

- **Contract:**
  - **Plain runs.** In a plain `download` (not `--retry-failed`), a file gets no further turn in this run once it
    is unavailable. That holds whether it became unavailable in an earlier run or in this one. A run that lasts past
    `UNAVAILABLE_AGE` can label a file in the run itself.
    - After the silent turn that leaves it unavailable, the file is not deferred. It is reported failed at once with
      Stage 1's reason (no `(after N passes)`), and its record is written with that reason.
    - An unavailable file still takes its turn in pass 1 of every plain run, at the tail by #27's order. A turn that
      gets a byte, or that fails for another reason, ends the streak (Stage 1). From then on the file is handled as
      any other.
    - A throttled deferral of an unavailable file is a deferral like any other. The file goes to the next pass,
      because it was not tried.
  - **`--retry-failed`** is unchanged: the usual passes, and Stage 1's reasons.
  - **Passes.** `MirrorReport.passes` stays `None` when no file was deferred. So a run whose only failures are
    unavailable files that failed at once prints no `Passes:` line. `--download-passes 0` ends a plain run once only
    unavailable files are left, or none.
  - **Logging.** `_log_pass` for pass 1 adds `; U unavailable on Yandex Disk get one try` after #27's clause. The
    clause is left out when U is 0. This goes to the log only.
- **Behaviour to lock (`tests/test_download.py`, next to Stage 1's tests):**
  - `test_a_plain_rerun_tries_an_unavailable_file_once_and_fails_it_at_once`: after the aged unavailable run, rerun
    with the defaults (`--download-passes 7`), the 503 still injected.
    - The file's `/download` is requested exactly once.
    - There is no `Passes:` line, and the reason says `last 9 attempts`.
    - The record is `(…, 9, 9, …)`.
    - The log has `1 unavailable on Yandex Disk get one try`.
  - `test_a_file_becoming_unavailable_mid_run_gets_no_more_passes`: one silent run with `--download-passes 1`, then
    `_age_silence(path, 25)`, then `--download-passes 10`. The file is fetched 6 times (silent turns 2 to 7), not
    10. It is reported unavailable, and the summary shows `Passes: 6 of 10`.
  - `test_with_unlimited_passes_a_plain_run_ends_once_only_unavailable_files_are_left`: the same setup with
    `--download-passes 0`. The run ends with exit 1, and the healthy files are placed.
  - `test_retry_failed_still_gives_an_unavailable_file_every_pass`: after the aged unavailable run,
    `--retry-failed --download-passes 3` fetches it 3 times.
  - `test_a_file_yandex_restores_is_fetched_by_the_next_plain_run`: after the aged unavailable run, remove the
    fault. A plain rerun places it, with exit 0 and `_failure_records() == []`.
- **Deliverable:** an unavailable file costs one turn per plain run, and a run with unlimited passes ends.
- **Validation:**
  - `mise run check`
  - `mise run test -- yandex-disk-rescue/tests/test_download.py -k "unavailable or restores or unlimited_passes or
    retry_failed or tried_again or rerun_starts"`
  - `mise run ci`
- **Doc updates:**
  - **Root `README.md`:**
    - § Downloading, **Passes** bullet: a plain run gives an unavailable file one attempt, at the end of pass 1,
      and fails it at once when it is silent again. `--retry-failed` gives it every pass. `--download-passes 0`
      ends once only such files are left.
    - § download, #27's **Order** paragraph: unavailable files are the last of pass 1 and get no later pass in a
      plain run.
    - § Configuration, `download_passes` row: `0` means until every file is done or unavailable. Make
      `settings.DOWNLOAD_PASSES.help` say the same.
  - **`yandex-disk-rescue/README.md`** env-var table, `YANDEX_DISK_RESCUE_DOWNLOAD_PASSES`: the same wording.
  - **Docstrings:** `mirroring.MirrorRun.run` / `_later_passes`.
  - **`CHANGELOG.md`** `### Changed`: a plain `download` tries a file unavailable on Yandex Disk once per run
    instead of on every pass.
  - **Stale mentions:** `rg -n "until every file is done" README.md yandex-disk-rescue/`.

## Discovered issues

- **Fix in this PR:** `--download-passes 0` never ends while a dead file is left (Stage 2).
- **Filed as issues:** #30. A download-link request (Disk API) that keeps failing for one file stops the whole
  run. Reproduced with a persistent 500 on `/v1/disk/resources/download` for one path and `--api-retries 2`: the
  run ends with `Error: Could not download disk:/Dead/napkin.jpg: Yandex Disk API returned HTTP 500: Injected HTTP
  500 (after 3 attempts)`, exit 1, and leaves the file's empty partial. It is out of scope here: the live dead files
  failed at the download host, not at the API.

## Risks / open questions

- **A storage node silent for more than a day.** Its healthy files are labelled after 24 h of silence on every
  attempt. Under D2 = B each then gets one turn per run.
  - A node that comes back fully: its files finish on the next run.
  - A node that comes back flaky: a file silent with probability p needs about 1/(1−p) runs instead of
    1/(1−p⁷), for example 5 runs instead of 1.3 at p = 0.8.
  - A user who sees `Yandex Disk never started sending` on files that do open in the web interface can run
    `--retry-failed`, which gives every pass.
- **Cost of one turn per run at scale.** The cost is one stall timeout per silent file on one connection: 3,500
  files × 15 s ÷ 20 connections ≈ 44 min at the tail of pass 1. It applies only if all of them are silent, and HTTP
  500 or a close fails at once. It is still far below today's 7× cost.
- **Clock.** `silent_since` uses the wall clock. A clock that jumps back delays a label, and one that jumps forward
  hastens it. Both are bounded by the 7-turn count, and neither loses data.
- **Rebase on #27.** This plan uses #27's names (`FailureRecord`, `record_failure(..., turns=)`, `_failure_records`,
  the rollback-upgrade test, the Ctrl+C-between-passes test). If they end up named differently, the implementer
  follows #27. The contract here is the streak, its rules and the reason.
- **Migration number.** v11 assumes that #27 is the only plan landing first that adds a migration. #18's plan says
  it needs none, and #15, #16 and #23 add none. #26 takes v12.
