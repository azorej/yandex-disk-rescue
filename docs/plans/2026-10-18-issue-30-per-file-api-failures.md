# Plan: A Disk API request failing for one file fails only that file

Issue: #30
Branch: fix/per-file-api-failures

## Context

Each turn of a file in `download` makes Disk API requests for that file:
- the download link (`DiskClient.download_link`, through `mirroring._Link.current`);
- the link again, when the download host rejects it (`_Link.rejected`);
- for a resumed download, the identity check first (`client.resource` in `mirroring._resume`).

When one of these requests keeps failing past `--api-retries`, the client raises `DiskApiError`. The run treats that
as an outage: `_FileTurns` cancels everything and the error stops the run. No summary is printed and no failure is
recorded, so the next run stops at the same file. Two cases already behave correctly: a 4xx on the link request fails
only its file (`DownloadRefusedError` → `FileFailed`), and `TokenRejectedError` stops the run.

Reproduced on `main` (2d1add9) against `tests/fake_disk_server.py`, with a scratch script that is not committed:
- 300 files `disk:/Dead/z000.txt` … `z299.txt` plus `disk:/Dead/napkin.jpg`;
- a 500 on every `GET /v1/disk/resources/download?path=disk:/Dead/napkin.jpg`;
- `--api-retries 2`.

```
run 1: exit 1
  Downloading 301 files (2.9 KiB) to …/dest
  Downloaded: 61 of 301 files, 610 B in 4 s, average 141 B/s; 0 to retry, 0 failed, 0 quarantined; 1 of 7 passes
  Error: Could not download disk:/Dead/napkin.jpg: Yandex Disk API returned HTTP 500: Injected HTTP 500 (after 3 attempts)
  files in dest: 61
run 2: exit 1
  Downloading 240 files (2.3 KiB) to …/dest
  Downloaded: 54 of 240 files, 540 B in 5 s, average 108 B/s; 0 to retry, 0 failed, 0 quarantined; 1 of 7 passes
  Error: Could not download disk:/Dead/napkin.jpg: Yandex Disk API returned HTTP 500: Injected HTTP 500 (after 3 attempts)
  files in dest: 115
```

- Each run downloads only the files that finish while `napkin.jpg` spends its retries, then stops.
- No failure record is written, so #27 never moves the file to the tail, and `--retry-failed` does not cover it.
- The resume check takes the same `_FileTurns` stop, but that was found by reading the code, not reproduced. Stage 2
  reproduces it first.
- A full outage today stops at the very first request, the account check:
  `Error: Yandex Disk API returned HTTP 500: Injected HTTP 500 (after 3 attempts)`.

Pacing facts this plan relies on (`api/limiter.py`, `api/client.py:356-376`):
- Every 5xx, 408 and 429 counts as throttling. Each one doubles the shared request interval, up to `max(2 s, 8 ×
  api_interval_ms)`, which is 2 s at the default 50 ms.
- Every 10 consecutive successes halve the interval again.
- So the 6 failed attempts of one request (`api_retries = 5`) take the whole client to one request start per 2 s.
  Getting back to 50 ms takes about 60 successes, roughly 40 s.

## Goal

**The rule.** It applies to every per-file Disk API request of a turn: the link request, the second link request
after a rejection, and the resume's identity check.

When the request fails with `DiskApiError` past the API retries, the tool checks whether the Disk API still answers
for other files. This is the **probe**, a single request with the usual retries:

1. **A reference file exists.** That is a file other than this one whose download link the API gave in this run, or,
   failing that, any other file the manifest records as verified for this mirror, or failing that, for any mirror of
   the same account. The probe asks for the reference
   file's download link, on the same endpoint. Any answer counts: a link, a 404 or a per-file refusal.
2. **No reference file exists.** This happens only when nothing in this mirror ever got a link: a fresh mirror whose
   first link requests fail. The probe asks for the account (`GET /v1/disk/`).
   - **Bound:** in one run, at most `UNREFERENCED_FAILURES = 10` files may fail this way under this branch. The next
     one stops the run as an outage of the link endpoint. The count is taken when the failure occurs, before its probe,
     so concurrent failures never send more than 10 probes. Accepted corner: a first run of an account whose first 11+
     files all fail before any file gets a link stops as an outage (named in Risks).

What happens next:
- **The probe answers.** The failure belongs to this file.
  - The turn is deferred as a transient failure, and the run goes on.
  - Its reason is the API failure without the `Could not … <path>:` prefix, for example `Yandex Disk API returned
    HTTP 500: Injected HTTP 500 (after 3 attempts)`.
- **The probe fails with `DiskApiError`, or the bound is reached.** That is an outage. The run stops with the file's
  own error and exits 1.
  - On a probe failure the text is unchanged: `Error: Could not download <path>: …`.
  - On the bound, ` (no file of this mirror got a download link: 10 failed the same way)` is appended.
- **The probe raises `TokenRejectedError`.** The run stops as today.

**How such a turn counts:**
- **Failed turns and records:** it counts one failed turn and writes the failure record at once, so `--retry-failed`
  covers it. The file gets the next pass. After the last pass its reason ends with `(after N passes)`, and the next
  run starts it last (#27).
- **Silent streak (#14): neutral.** The download host was never asked for the bytes. Like a throttled turn, the turn
  neither lengthens nor ends the file's silent streak, and its reason never becomes the streak's last.
  - So the README rule "a 429 or any answer with Retry-After is throttling, which neither counts nor starts over"
    stays true whatever status the API returned.
  - It also follows that a file already unavailable on Yandex Disk whose turn fails this way is deferred to the next
    pass. It is not failed at once.
- **Preview (#26):** the file fails on Yandex's side (`on_yandex`), so the end-of-run step fetches its preview.

**Unchanged:** a per-file 4xx (`DownloadRefusedError`), a 404 and a rejected token.

## Decisions for the user

The plan is written for the recommendation of each decision.

- **D1: what evidence separates "this file" from "an outage".** The issue requires evidence from the same run.
  - **Recommended: the reference-file probe described in the Goal.** It asks the same endpoint, for a file known to
    get a link, so it sees an outage of the link endpoint alone.
    - It needs no other file to be in flight. That covers the tail of a pass, where #27 starts the files that failed
      before (often all broken files together), and a `--retry-failed` run of broken files.
    - A mirror that is done except for its broken files always has verified files to probe with.
    - The account-probe branch and its bound exist only for a mirror where nothing ever got a link.
  - **Considered: an account probe only.** It is simpler, but unbounded when only the link endpoint fails. Each file
    then spends about 6 attempts plus a probe, about 14 s at the 2 s throttled pace, and the files queue behind one
    another on the shared limiter. A 127k-file pass would take about 20 days.
  - **Considered: concurrent evidence only.** The file fails alone if another API request got an answer meanwhile;
    otherwise the run stops. Rejected: the tail cluster of broken files sees no other answer, so every run would end
    with an `Error:`, no summary and no record.
  - **Considered: a breaker of "N files in a row".** Rejected for the same reason: a mirror with N broken files trips
    it at the tail of every run.
- **D2: defer or fail at once.**
  - **Recommended: defer as transient** (`FileDeferred`). A 5xx or transport error past the retries is the API-side
    twin of a download-host 5xx, which is deferred today, and the API may recover within the run.
  - The alternative, failing at once, treats a transient status as final.
- **D3: the silent streak.**
  - **Neutral, as in the Goal, per main's direction.** This also makes the question of the unavailable reason's
    wording moot. "No data in its last N attempts …, each with a new download link" stays literally true, because only
    turns that got a link and reached the host count.
- **D4: preview.**
  - **Recommended: yes.** It comes with no new code: `_later_passes` already flags deferrals out of passes `on_yandex`.

## Non-goals

- The API retry and back-off policy, and the limiter (out of scope per the issue). The pacing cost below is the
  current policy applied to a run that now goes on.
- The scan phase. A folder listing that keeps failing for one folder stops the scan; filed as #40.
- The preview step, which already fails only the file on `DiskApiError` (`previews.py:160`).

## Stages

### Stage 1: A link request failing for one file defers only that file; an outage still stops the run

- **Contract:**
  - **`api.DiskApiError` gains `reason: str`.**
    - It is the failure without the request's context: what follows `Could not <verb> <path>: ` in the message, or
      the whole message when the request has no context.
    - `str(exc)` is unchanged everywhere.
    - Every raise site in `client.py` sets it: `_retrying`, `_raise_for_status`, `_unexpected_response`.
  - **`mirroring.FileDeferred` gains `api_failed: bool`** (default `False`): the turn was deferred because a Disk API
    request for the file failed, and the download host was not asked for its bytes.
    - When the streams of one turn defer for different causes, the turn's `api_failed` is set only if every deferred
      stream's was. Otherwise the turn's reason is that of the first stream deferred for another cause.
  - **The streak rule (`_after_deferral`).**
    - A turn that received a byte ends the streak, as today.
    - Otherwise an `api_failed` turn returns the streak unchanged, `last` included.
  - **`_turn`'s fail-at-once rule.** An unavailable file is failed at once only after a turn that is neither throttled
    nor `api_failed`.
  - **The probe, for the link request (`_Link.current`) and the link refresh (`_Link.rejected`).** The run applies
    the Goal's rule.
    - It knows its reference file: the last file whose link the API gave in this run, else a verified file of the
      mirror other than the failing one. Which verified file is the implementer's call, but it must be deterministic.
    - It counts the failures under the account branch for the bound.
    - When the probe answers, the stream that needed the link returns a `FileDeferred` with `exc.reason`,
      `retry_after=None`, `throttled=False`, `api_failed=True`, and `received` set from the turn. As with any
      transient deferral, the file's other streams run on.
    - When the probe raises `DiskApiError`, the original link error is raised and stops the run. The probe's error
      goes to the log file only.
    - When the bound is reached, a `DiskApiError` is raised with the file's message plus the bound suffix from the
      Goal.
    - A `TokenRejectedError` from the probe propagates.
  - **Invariant: one link failure per turn.** Within one turn of a file, once a link request (first or refresh) has
    failed this way, every later need for a link in that turn gets the same deferral, with no new request and no
    probe.
  - **Log file.** The deferral is logged as every deferral is (`<path> deferred to a later pass: <reason>`). The
    probe's outcome gets its own INFO line naming the probe target (the reference path or the account). Nothing new
    on stderr.
  - **Docstrings rewritten as current state:**
    - `FileDeferred`: its causes, and `api_failed`.
    - `MirrorRun`: the streak paragraph adds "a deferral for a failed Disk API request leaves it as it is".
    - `MirrorRun.run` and `download_file`: `DiskApiError` stops the run only as an outage.
    - `_FileFetch._stream` and `_Link`.
- **Behaviour to lock** (`yandex-disk-rescue/tests/test_download.py`, next to
  `test_a_link_refused_for_one_file_fails_only_that_file` unless noted):
  - **`test_a_link_request_failing_for_one_file_defers_only_that_file_and_retry_failed_covers_it`.**
    - Setup: the `disk` tree, `disk.inject('GET', '/v1/disk/resources/download', *[500] * 4, where={'path':
      'disk:/Docs/readme.txt'})`, and a run with `--api-retries 1 --download-passes 2`.
    - Exit 1, `5 downloaded`, `Passes: 2 of 2`.
    - `Failed: 1\n  disk:/Docs/readme.txt: Yandex Disk API returned HTTP 500: Injected HTTP 500 (after 2 attempts)
      (after 2 passes)\n`.
    - `_failure_records() == [('disk:/Docs/readme.txt', <that reason>, 2)]`.
    - Each failed turn was followed by one link request for a file other than `readme.txt` (the reference probe), and
      `GET /v1/disk/` was requested only once, at the start.
    - Then `run_download('--retry-failed', str(dest))`, with the faults used up: exit 0, `1 downloaded`, and no
      failure record.
  - **`test_a_link_endpoint_outage_stops_a_run_over_a_mirror_with_verified_files`** (the bounded endpoint-only case).
    - Setup: a full first run (exit 0). Then delete two local copies (`local_file(dest, …).unlink()`), inject
      `*[500] * 50` on `/v1/disk/resources/download` for every path while `GET /v1/disk/` stays healthy, and run with
      `--api-retries 1`.
    - Exit 1.
    - The last stderr line starts with `Error: Could not download disk:/` and ends with `(after 2 attempts)`.
    - `'Mirrored' not in result.stdout`.
    - No new failure record.
  - **`test_a_disk_api_outage_during_the_first_run_stops_it`** (fresh mirror, account branch).
    - Setup: the `disk` tree, `inject('GET', '/v1/disk/resources/download', *[500] * 50)` and `inject('GET',
      '/v1/disk/', Delay(0), *[500] * 50)`. `Delay(0)` lets the start's account check through. Run with
      `--api-retries 1`.
    - Exit 1, and the same `Error: Could not download …` last line.
    - `'Mirrored' not in result.stdout`, and `_failure_records() == []`.
  - **`test_a_fresh_mirror_whose_links_all_fail_stops_after_ten_files`** (the bound).
    - Setup: a folder of 12 files, links failing for all, the account healthy, and a run with `--api-retries 0` on
      that folder.
    - Exit 1, and the last stderr line ends with `(no file of this mirror got a download link: 10 failed the same
      way)`.
    - `'Mirrored' not in result.stdout`.
  - **`test_a_segmented_file_whose_link_refresh_keeps_failing_waits_for_the_next_pass`** (`segmented` fixture).
    - The host answers 410 to each segment's first request. Inject per segment with `where={'path':
      SEGMENTED_PATH}` and `where_headers={'range': f'bytes={start}-{end}'}`, from the plan's segment ranges.
    - The link endpoint for `SEGMENTED_PATH` answers `Delay(0)` (the first link, normal), then `500, 500` for the
      refresh. Run with `--api-retries 1`.
    - Exit 0, `Passes: 2 of 7`, and the file verifies.
    - The link requests for `SEGMENTED_PATH` made before the first pass-2 fetch of it number exactly 3: the first
      link and 2 refresh attempts. Index the request log: everything before the first `/download` request for that
      path after the pass pause, that is after the first 410 batch. This locks the once-per-turn invariant.
  - **`test_a_link_failure_of_an_unavailable_file_defers_it_and_leaves_its_streak`**, next to
    `test_a_throttled_turn_of_an_unavailable_file_defers_it_to_the_next_pass` and mirroring it.
    - Setup: `_unavailable_run(dead, dest)`, then inject `500, 500` on `DEAD_PATH`'s link request (`--api-retries 1`)
      and one host `503` for the next fetch. Plain run with `--download-passes 7`.
    - Pass 1 defers the file rather than failing it at once. Pass 2 is silent at the host and fails it as
      unavailable: `Passes: 2 of 7`.
    - Reason `f'{NEVER_STARTED} 9 attempts {_since(aged)} (the last: {SERVICE_UNAVAILABLE})'`.
    - `failure_record(DEAD_PATH) == (reason, 10, 9, aged)`: two more failed turns, but only one more silent turn.
  - **`test_a_file_whose_link_request_keeps_failing_gets_its_preview`** (`photo` fixture).
    - Setup: a persistent 500 on `DEAD_PATH`'s link request, `--api-retries 1 --download-passes 1`.
    - `Saved only as a preview … : 1` lists `DEAD_PATH`.
  - **Rewrite `test_an_interrupted_or_stopped_transfer_phase_fetches_no_previews`.** Its "stopped" case uses the #30
    reproduction, which no longer stops the run. Replace it with the fresh-mirror outage injection above. Keep its
    assertions: exit 1, `Error: Could not download …`, no `/preview` request.
  - **`yandex-disk-rescue/tests/test_api_client.py`:** extend `test_retries_run_out_with_the_last_failure_and_the_attempt_count`
    and `test_download_link_rejects_an_unexpected_body_without_quoting_the_href` with `excinfo.value.reason`. It is
    the message without `Could not … : `, and equals the message for a request without context.
- **Deliverable:** link failures of one file no longer stop `download`. An outage still does, including one of the
  link endpoint alone. All the tests above pass, and every existing #14 streak test passes unchanged.
- **Validation:**
  - `mise run check`
  - `mise run test -- yandex-disk-rescue/tests/test_download.py -k "link or outage or stopped_transfer or unavailable or silent or preview"`
  - `mise run test -- yandex-disk-rescue/tests/test_api_client.py`
  - `mise run ci`
- **Doc updates:** all rewritten as current state, with no "previously".
  - **Root `README.md` § Downloading:**
    - **Failures that stop the run.** Rewrite: a token Yandex rejects, and a Disk API outage, stop the run. State the
      rule as its own sentences, the "the README states the rule" deliverable:
      - A Disk API request for one file that keeps failing past the API retries makes the tool ask the API for the
        download link of another file of the mirror that got one: the last one in this run, or a verified one.
      - When nothing in the mirror ever got a link, the tool asks for the account (`GET /v1/disk/`) instead.
      - When that answers, only the file's turn fails: it waits for the next pass with the API's status as its
        reason, and the run goes on.
      - When it does not answer, the run stops as an outage. It also stops at the 11th such file of a run in a mirror
        where nothing ever got a link.
    - **Passes.** bullet: the files resumed in another pass include those whose download link the Disk API kept
      failing to give.
    - **Unavailable on Yandex Disk.** bullet, the sentence listing what counts: "a turn whose download link or
      resume check the Disk API failed to give, like throttling, neither counts nor starts over".
    - **Retrying failures.** list: add "a download link the Disk API kept failing to give".
  - **§ Exit codes:** keep the general wording "an API error after the retries". It still covers the outage and the
    scan's failing listing (#40), and per-file failures exit 1 through the failed list, which the row already names.
  - **`yandex-disk-rescue/README.md` layout:**
    - the `mirroring.py` line gains "a per-file Disk API request failing past the retries deferring only its file
      while the API still gives another file's link (or answers the account request, at most 10 files, when nothing
      in the mirror got one), such turns leaving the silent streak as it is";
    - the `api/errors.py` line notes `DiskApiError.reason`.
  - **`CHANGELOG.md` `### Fixed`:** a download link the Disk API keeps failing to give for one file no longer stops
    `download`. While the API still gives other files' links, only that file waits for the next pass, failing with
    the API's status after the last pass, and gets its preview. Such attempts do not count toward "unavailable on
    Yandex Disk". An outage, also one of the download-link endpoint alone, still stops the run.
  - **Stale mentions:** `rg -n "persists past the API retries|whose download host sent no byte" README.md
    yandex-disk-rescue/ CHANGELOG.md`.

### Stage 2: The resume check of an unfinished download follows the same rule

- **Reproduce first.** Before changing code, the implementer reproduces the stop on `main` with the stage's test
  setup (scratch test or the new test run against `main`'s code). The run should exit 1 with `Error: Could not read
  disk:/Big/big.bin: Yandex Disk API returned HTTP 500: … (after 2 attempts)` and no summary. The captured output
  goes into the stage report. If the run does not stop that way, the implementer stops and reports back: the stage's
  premise is then wrong.
- **Contract:**
  - In `mirroring._resume`, a `DiskApiError` from `client.resource(entry.resource.path)` goes through the same probe
    and bound.
  - When the probe answers, the turn is deferred with `exc.reason`, `api_failed=True` and `received=False`. The
    partial file and its `partial` record are kept untouched, so the next pass resumes from the confirmed bytes.
  - When the probe fails, the original error stops the run (`Error: Could not read <path>: …`).
  - It is the one probe-and-decide step from Stage 1, shared, with no second copy.
  - `_resume`'s and `download_file`'s docstrings are rewritten as current state.
- **Behaviour to lock** (`test_download.py`, next to the resume tests around `big`):
  - **`test_a_resume_check_failing_for_one_file_waits_for_the_next_pass_and_keeps_its_bytes`.**
    - Leave a partial download of `BIG_PATH` with confirmed bytes, the way neighbouring resume tests do (a `Truncate`
      after some chunks with `--download-passes 1`), and record `_confirmed()`.
    - Inject `500, 500` on `GET /v1/disk/resources` with `where={'path': BIG_PATH}`, and run with `--api-retries 1`.
    - Exit 0, `1 downloaded` (only `BIG_PATH` is left after the first run), `Passes: 2 of 7`.
    - The pass-2 fetch of `BIG_PATH` starts at the confirmed offset: `_fetches(...)` shows `_tail(confirmed)`.
    - No failure record afterwards.
  - The outage half is locked by Stage 1's outage tests through the shared step.
- **Deliverable:** no per-file Disk API request of a turn can stop the run unless the API is out.
- **Validation:** `mise run check`; `mise run test -- yandex-disk-rescue/tests/test_download.py -k "resume"`;
  `mise run ci`.
- **Doc updates:**
  - Root `README.md`: the Stage 1 rule names both requests: "a Disk API request for one file (its download link, or
    the check of an unfinished download against the Disk)". In the unavailable bullet, "download link or resume
    check" is already there.
  - `CHANGELOG.md`: the Stage 1 `### Fixed` line widened the same way.
  - Package README `mirroring.py` line: already "per-file Disk API request". Check it.

## Discovered issues

- **Fix in this PR:**
  - The resume identity check (`_resume` → `client.resource`) stops the run the same way. It goes in Stage 2, with
    the same mechanism, reproduced first.
  - `test_an_interrupted_or_stopped_transfer_phase_fetches_no_previews` uses the #30 bug as its "stopped run". It is
    rewritten to a real outage in Stage 1.
- **Filed as issues:**
  - #40: a folder listing that keeps failing for one folder stops the scan, so `download` never runs. Reproduced
    against the fake with a 500 on `GET /v1/disk/resources?path=disk:/Dead/Sub` and `--api-retries 2`: `Error: Could
    not list disk:/Dead/Sub: Yandex Disk API returned HTTP 500: Injected HTTP 500 (after 3 attempts)`, exit 1, nothing
    downloaded. Its plan should reuse this evidence rule; the issue body points at an account probe, which this plan
    refines.

## Risks / open questions

- **A broken file now slows the whole run's API pacing.** This is a new cost, because the run now goes on.
  - Each failed turn makes `api_retries + 1` throttling answers, so the shared interval reaches 2 s. It takes about
    60 successful requests, roughly 40 s, to return to 50 ms.
  - Meanwhile every file's link request is paced at that rate, so file starts slow down.
  - Bounded in practice by #27: files with failure records start at the tail of pass 1, together, and later passes
    hold only deferred files. So a run pays this mostly once, at its end, not once per broken file spread through the
    run.
  - The policy itself is out of scope (issue's Out of Scope).
- **Cost of an outage before it stops.**
  - With a reference file: the file's retries plus the probe's retries, at a pace already throttled to 2 s. That is
    about 30–45 s at the defaults, against about 15 s today.
  - Under the bound: up to 10 files' retries plus probes, on the shared 2 s pace, about 2–3 minutes.
- **The resources endpoint alone failing.** A resume check probe goes to the link endpoint, so an outage of
  `GET /v1/disk/resources` alone defers the resumed files instead of stopping. The cost is bounded by the number of
  unfinished downloads, and their bytes are kept.
- **A reference file that is itself broken.** If the reference file's link fails too, the run stops as an outage. In
  this run that file's link already succeeded, or it was verified, so this needs it to break in the meantime.
- **Conflict with #28.** It is planned in parallel and also touches `mirroring._resume` and `download_file` (an
  unreadable partial file). Stage 2 edits the same function, and whichever lands second rebases. The changes are
  independent in behaviour.
- **Malformed link bodies.** An unexpected response body is a `DiskApiError` too, so under this rule it also defers
  only its file. That is intended.
