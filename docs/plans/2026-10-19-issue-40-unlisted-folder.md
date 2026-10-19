# Plan: A folder listing failing for one folder no longer stops the scan

Issue: #40
Branch: fix/unlisted-folder

## Context

The scan (`scanning._Walk`) lists every folder below the root with `client.limits.concurrency` workers. The first
listing that raises anything other than `RemotePathNotFoundError` / `NotAFolderError` (a vanished folder) stops the
walk, and `scan_tree` never commits. `download` scans first unless a recent scan covers the folder, so one folder whose
listing keeps failing blocks the whole mirror on every run.

Reproduced on `main` (a819103) against `tests/fake_disk_server.py`, with a scratch script that is not committed
(300 files in `disk:/Dead`, `disk:/Dead/Sub/inner.txt`, a 500 on every `GET /v1/disk/resources?path=disk:/Dead/Sub`):

```
$ download --api-retries 2 --scan-max-age 0 disk:/Dead DEST          (first run)
exit 1
--stdout--
--stderr--
… Scanned disk:/Dead: 300 files, 1 folder (2.9 KiB) listed in 2 s
Error: Could not list disk:/Dead/Sub: Yandex Disk API returned HTTP 500: Injected HTTP 500 (after 3 attempts)
files under dest: 1          (only the tool's own folder; nothing downloaded)

$ download disk:/Dead DEST                                             (healthy, then the fault is injected)
exit 0 … 301 downloaded
$ scan --api-retries 2 disk:/Dead
exit 1
Error: Could not list disk:/Dead/Sub: Yandex Disk API returned HTTP 500: Injected HTTP 500 (after 3 attempts)
$ download --api-retries 2 --scan-max-age 0 disk:/Dead DEST
exit 1   (same error; nothing checked or downloaded)

$ scan --api-retries 1 disk:/Dead                                      (every resources request fails)
exit 1
Error: Could not read disk:/Dead: Yandex Disk API returned HTTP 500: Injected HTTP 500 (after 2 attempts)
```

The current behaviour is locked by `test_scan.py::test_a_folder_failing_past_the_retries_names_it_and_keeps_the_previous_scan`
(passes on `main`), which Stage 1 rewrites. #30 (merged, `mirroring.ApiProbe`) settled the same question for a
file's download link: after a per-item request runs out of retries, ask the same endpoint about something known to
answer; any answer means the failure is the item's own, a failure means an outage.

## Goal

**The rule (scan phase).** When the listing of a folder other than the scanned folder itself fails with
`DiskApiError` past the API retries (any page), the scan asks the Disk API to list another folder: the **probe**, one
request for the first page of the **reference folder**, with the usual retries.

- **The reference folder** is the most recent folder, other than the failing one, whose listing page the API gave in
  this scan (by the order the pages arrived, whichever worker got them). One always exists: the failing folder's parent gave the page that named it. So, unlike #30, there is no
  account fallback and no bound.
- **Any answer counts:** a page, a 404 or a "now a file" answer.
- **The probe answers:** only this folder failed. It is recorded as **not listed** with the failure's `reason` (the
  `DiskApiError.reason`, without `Could not list <path>: `), a WARNING notice goes to stderr and the log
  (`<path> could not be listed: <reason>`), and the walk goes on.
- **The probe fails with `DiskApiError`:** an outage. The folder's own error is raised and stops the scan (exit 1,
  `Error: Could not list <path>: …`), and the manifest keeps the previous scan, as today. The probe's error goes to
  the log file only.
- **The probe raises `TokenRejectedError`:** it propagates and stops the scan, as today.
- **A folder at or below a folder that already vanished** in this scan: its `DiskApiError` is ignored, with no probe
  and no record, as its rows already are.
- **The scanned folder itself** failing to list (any page) stops the scan, as its vanishing does today: there is
  nothing to commit a view of.

**What the commit records** for a scan with folders not listed:
- The scan commits and is recorded as completed (`scans.completed_at`), so `download` reuses it under
  `--scan-max-age` / `--no-scan` as any scan.
- Everything the scan staged is recorded as usual, including rows listed below a folder not listed (its earlier
  pages, its sub-folders listed before or after it failed).
- **Kept rows.** A committed row this scan did not stage is **kept** (not removed) exactly when its parent is a
  folder not listed, or is itself a kept row; and it is not at or below a folder that vanished during this scan.
  Every other committed row the scan did not stage is removed, as today. So, below a folder not listed:
  - the rows its listing could not settle stay (an earlier page's sub-folders and files never named again, and
    everything below them);
  - below a sub-folder this scan staged and listed to its last page, a row it did not list is removed (the listing
    settled it). Within a not-listed subtree, every staged folder is either listed to its last page, not listed itself,
    or vanished, because the walk lists every folder it stages;
  - below a path this scan staged as a **file** (a folder replaced by a file), every committed row is removed, so no
    file ever has children;
  - the kept rows always hang under a recorded folder, so the tree stays consistent for `local_names`.
- A kept row is exactly as the earlier scan recorded it, and is not in the diff's `removed`. The not-listed folder's
  own row comes from its parent's listing, as for any folder.
- So a rescan keeps every file an earlier scan recorded below the folder that this scan could not settle, with its
  download, failure and preview records (they cascade only on a row's removal). A first scan records nothing below it
  beyond what it listed.
- The manifest records each folder not listed with its reason and the **root of the scan that recorded it** (new
  table, schema version 13). A commit replaces the records **at or below** its root (the root included, so a scan of `disk:/A` that lists it clears the record of
  `disk:/A` a scan of `disk:/` wrote).
- A not-listed record at or below a folder that vanished in the same scan is dropped, as `_vanish` drops unencodable
  entries.

**What the commands report:**
- **`scan`:** the summary lists `Folders that could not be listed: N`, then `  <path>: <reason>` per folder in path
  order (cut at 20 like every list). JSON gains `unlisted: [{"path", "reason"}]`, complete. After the summary and the
  footer the command exits 1 with `Error: 1 folder could not be listed; see the list above.` (`N folders …`).
- **`download`:** each folder not listed that the mirror's scan recorded fails in the report, with the reason
  `the scan could not list this folder, so files in it may be missing or out of date: <reason>`. It is reported
  every run until a scan lists it, from the manifest, as unencodable entries are today, so a reused scan reports it too.
  It counts in `failed`, so the run exits 1 through `FilesNotMirroredError`. Every other file is mirrored as usual,
  and so are the files kept below the folder from an earlier scan.
- **`verify`:** lists the same folders under "Not downloaded" with the same reason, so it exits 1.
- For a mirror of `R`, the folders reported are the records at or below `R`, and the records at an ancestor of `R`
  whose scan root is the root of `Manifest.last_scan(account, R)`, the scan the mirror's entries come from. A scan
  of `disk:/` that could not list `disk:/A` leaves a mirror of `disk:/A/B` as stale as a mirror of `disk:/A`; once a
  newer scan of `disk:/A/B` (or of `disk:/A`) completes, that record no longer concerns the mirror. Records at or
  below `R` need no such check: every later scan covering them replaces them.

## Decisions for the user

The plan is written for the recommendation of each.

- **D1: earlier-scan entries below a folder not listed.**
  - **Recommended: the rows the scan could not settle are kept as they were; whatever was listed is still recorded,
    and whatever a completed listing settled (or a folder now a file) is removed** (the "Kept rows" rule). `download`
    then keeps verified files verified, downloads the recorded ones still missing, and a file deleted on the Disk
    since then fails as `removed from the Disk after the scan` (a 404 on its link). Nothing is deleted or forgotten,
    and the folder is reported every run until a scan lists it.
  - Considered: drop everything below the folder for this scan (as a vanished folder). Rejected: it deletes the
    mirror's records of files that probably still exist, which the issue forbids.
  - Considered: keep the old rows but discard everything listed below the folder in this scan. Rejected: it throws
    away fresh data (a later page's failure would drop pages already read and sub-folders fully listed) for no
    safety gain, since recording a listed row never loses anything.
- **D2: exit codes.**
  - **Recommended: `scan` and `download` exit 1** after their summaries when a folder could not be listed. Exit 0
    claims "every file of the folder is verified", which nobody can know here, and an unattended run (cron,
    systemd) must see it. The acceptance criterion "`scan` and `download` finish" holds: both run to the end, print
    their summaries and commit or download everything else.
  - Considered: exit 0 for `scan` (the scan "completed"). Rejected: it hides a stale part of the manifest.
- **D3: the scanned folder itself failing to list.**
  - **Recommended: stops the scan**, as today and as its vanishing does. Probing it would make the outcome depend on
    timing (on a later page, sub-folders may or may not have been listed yet), and with nothing listed there is no
    view to commit; `--no-scan` reuses an earlier scan.
- **D4: shared code with `ApiProbe`.**
  - **Recommended: same rule, separate small probe in `scanning.py`.** The reference source (folders listed in this
    scan versus files linked or verified), the endpoint and the lack of a bound all differ; what is shared is the
    rule, and the README states it once for both phases. A shared abstraction would be a callback wrapper around a
    ten-line try/except.

## Non-goals

- The API retry and back-off policy (issue's Out of Scope).
- The scan's last status line saying `Scanned …` for a scan that stopped with an error: filed as #44.
- Re-listing a folder not listed within the same run: the next scan lists it again.

## Stages

### Stage 1: A folder that cannot be listed is recorded as not listed, and the scan commits the rest

- **Contract:**
  - **`scanning._Walk`:** a `DiskApiError` from listing a folder other than the root, and not at or below a vanished
    folder, goes through the probe of the Goal; one at or below a vanished folder is ignored (no probe, no record). It
    tracks the most recent folder whose page the API gave (and the one before, for when that is the failing folder
    itself). The probe is exactly one listing request (first page) of the reference folder, with the usual retries.
    `RemotePathNotFoundError` and `NotAFolderError` from the probe count as answers. The INFO log lines name the probe
    target, mirroring `ApiProbe`'s: `<path>: the Disk API still answers for the listing of <ref>, so only this folder
    is not listed` and `<path>: the Disk API failed for the listing of <ref> too, an outage: <error>`.
  - **The walk's invariants:** a folder not listed and then found at or below a vanished folder is dropped from the
    not-listed set. A not-listed folder's sub-folders already queued are still listed.
  - **`manifest.UnlistedFolder`** (frozen dataclass): `path: str`, `reason: str`.
  - **`ScanSession.commit(*, unencodable, unlisted: Iterable[UnlistedFolder], …)`:** the "Kept rows" rule of the
    Goal, in the commit's one transaction. Which extra arguments the commit takes to apply the vanished-folder
    exception is the implementer's call (the walk's vanished folders, or another way); the rule needs nothing else,
    since staged rows, their kinds and the not-listed folders decide it. The `ScanDiff` returned excludes the kept rows
    from `removed`.
  - **Migration to schema version 13:** a table of not-listed folders per account (`account_uid`, `path` TEXT,
    `reason` TEXT, `scan_root` TEXT, primary key on the first two), replaced at commit at or below the scan root.
  - **`ScanResult.unlisted: tuple[UnlistedFolder, ...]`**, sorted by path.
  - **`scan_summary`:** after the vanished list and before the unencodable one, `Folders that could not be listed: N`
    and `  <path>: <reason>` lines (limit applies). **`scan_document`:** `unlisted: [{"path", "reason"}]`.
  - **`scan` command:** after `output.footer`, raises a `RescueError` (exit 1) with `1 folder could not be listed;
    see the list above.` / `N folders could not be listed; see the list above.` when `result.unlisted` is not empty.
  - **`download`'s scan phase:** `scan_tree` returns normally, so `download` prints the scan summary (with the new
    list) and goes on. Until Stage 2, `download` does not yet fail the folder (it can exit 0); Stage 2 closes that.
    The Stage 1 commit message says so (`download does not report the folder until the next commit`); the branch is
    squash-merged, so `main` never has that state.
  - **Docstrings as current state:** `scan_tree` ("committed when every folder was listed, had vanished or could not
    be listed while the API answered for another folder; an outage raises the listing's error and leaves the manifest
    unchanged"), `_Walk`, `_Walk.run`, `ScanSession.commit`, the `manifest.py` module docstring (the new table).
- **Behaviour to lock** (`yandex-disk-rescue/tests/test_scan.py`, using its `disk` fixture, `page_cap = 2`):
  - **Rewrite `test_a_folder_failing_past_the_retries_names_it_and_keeps_the_previous_scan`** as
    `test_a_folder_failing_past_the_retries_is_not_listed_and_the_rest_is_committed`, parametrized `first_scan` /
    `rescan`.
    - Rescan setup: a first `_scan()` (exit 0). Then `add_file('disk:/Photos/2024/new.jpg')`,
      `add_file('disk:/Docs/more.txt')`, `remove('disk:/top.txt')`, and
      `inject('GET', '/v1/disk/resources', 503, 503, where={'path': 'disk:/Photos/2024'})` (exactly the two attempts,
      so the fault is used up afterwards). Run `_scan('--api-retries', '1')`.
    - Exit 1. Stdout has `Folders that could not be listed: 1\n  disk:/Photos/2024: Yandex Disk API returned HTTP
      503: Injected HTTP 503 (after 2 attempts)\n`.
    - Rescan: `Changes since the last scan: 1 added, 0 changed, 1 removed\n  + disk:/Docs/more.txt\n` and
      `  - disk:/top.txt\n`. `_rows()` equals the fake's tree rows without `disk:/Photos/2024/new.jpg` (so the old
      `a.jpg`, `b.jpg`, `c.jpg` rows are kept as they were).
    - First scan: `First scan of disk:/`, and `_rows()` is the tree rows minus everything strictly below
      `disk:/Photos/2024` (its own row is there).
    - `stderr_lines(result.stderr) == ['disk:/Photos/2024 could not be listed: Yandex Disk API returned HTTP 503:
      Injected HTTP 503 (after 2 attempts)', 'Error: 1 folder could not be listed; see the list above.']`.
    - The log has the probe's "still answers" INFO line for `disk:/Photos/2024` (`log_messages`).
    - `--json` variant (same run, one more parametrization or a second run on a fresh fault): `unlisted ==
      [{'path': 'disk:/Photos/2024', 'reason': '…(after 2 attempts)'}]`.
    - Then, with the faults used up, `_scan()` exits 0, has no "could not be listed", and `_rows() ==
      _tree_rows(disk)` (rescan: `+ disk:/Photos/2024/new.jpg`).
  - **`test_a_disk_api_outage_during_the_walk_stops_the_scan_and_keeps_the_previous_scan`.**
    - Setup: a first `_scan()`. Then `inject('GET', '/v1/disk/resources', Delay(0), Delay(0), *[503] * 20,
      where={'path': 'disk:/Photos'})` (the root resolution and its single listing page pass) and `*[503] * 20` with
      `where={'path': 'disk:/Photos/2024'}`. Check the `Photos` listing is one page under `page_cap = 2`
      (`2024/`, `old.bmp`); if not, add `Delay(0)` per page. Run `_scan('disk:/Photos', '--api-retries', '1')`.
    - Exit 1. `stderr_lines(result.stderr)[-1] == 'Error: Could not list disk:/Photos/2024: Yandex Disk API returned
      HTTP 503: Injected HTTP 503 (after 2 attempts)'`, no summary in stdout, `_rows() == previous`.
    - The log has the probe's "failed … too, an outage" INFO line.
  - **`test_the_scanned_folder_failing_to_list_stops_the_scan`**, next to
    `test_the_scanned_folder_itself_vanishing_fails_the_scan`: inject `*[503] * 20` with `where={'path':
    'disk:/Photos', 'offset': '0'}`, `_scan('disk:/Photos', '--api-retries', '1')`: exit 1, `Error: Could not list
    disk:/Photos: …(after 2 attempts)`, `_rows() == previous`.
  - **`test_a_folder_not_listed_keeps_only_the_rows_its_listing_could_not_settle`**, next to the vanishing tests,
    with `_add_vanishing_tree` (`disk:/Gone` lists `Sub/`, `a.txt` on page 1 and `b.txt`, `c.txt` on page 2). A full
    first scan; then `inject('GET', '/v1/disk/resources', 503, where=GONE_SECOND_PAGE)` and `_scan('--api-retries',
    '0')`. Every case exits 1 with `disk:/Gone` under `Folders that could not be listed: 1`. Parametrized on what
    changed on the Disk before the rescan:
    - **nothing:** `_rows()` equals the first scan's rows (`b.txt`, `c.txt` kept).
    - **`Sub/y.txt` deleted** (`disk.remove('disk:/Gone/Sub/y.txt')`): `y.txt` is removed (`Sub` was listed to its
      last page) and is the diff's only `- ` line; `b.txt`, `c.txt` kept.
    - **`Sub` replaced by a file** (`disk.remove('disk:/Gone/Sub')`, `disk.add_file('disk:/Gone/Sub', b's')`):
      `disk:/Gone/Sub` is a `file` row, no row is below it, and `Sub/Deep`, `Sub/Deep/x.txt`, `Sub/y.txt` are in the
      diff's `removed`; `b.txt`, `c.txt` kept.
    - **`Sub` vanishing during the scan** (`inject(..., 404, where={'path': 'disk:/Gone/Sub'})`): `disk:/Gone/Sub` is
      under `Folders that disappeared during the scan: 1`, it and everything below it are gone from `_rows()`;
      `b.txt`, `c.txt` kept.
  - **`test_a_folder_failing_to_list_below_a_vanished_folder_is_not_reported`**, parametrized on order:
    - parent vanishes after the child failed: `disk:/Gone/Sub` answers `503` (`--api-retries 0`) and
      `GONE_SECOND_PAGE` answers `Delay(1, then=FakeResponse.error(404))`;
    - child fails after the parent vanished: `GONE_SECOND_PAGE` answers `404` and `disk:/Gone/Sub` answers
      `Delay(1, then=FakeResponse.error(503))`.
    - Both: exit 0, `Folders that disappeared during the scan: 1\n  disk:/Gone\n`, no "could not be listed", and in
      the second case no probe line in the log, and a `disk:/Gone/Sub` listing request was made (so the ignore path
      ran, rather than `Sub` never being taken off the queue).
  - **`test_manifest.py`:** the three `SCHEMA_VERSION == 12` assertions become `13`; the upgrade tests pass unchanged
    otherwise.
- **Deliverable:** `scan` and `download`'s scan phase survive one broken folder and commit everything else, keeping
  the earlier-scan rows below it; an outage still stops the scan with the manifest unchanged.
- **Validation:**
  - `mise run check`
  - `mise run test -- yandex-disk-rescue/tests/test_scan.py yandex-disk-rescue/tests/test_manifest.py`
  - `mise run ci`
- **Doc updates** (rewrite as current state):
  - **Root `README.md` § Scanning a folder:** a new paragraph after the vanished-folder one: a folder whose listing
    keeps failing past the API retries makes the tool list another folder of this scan, the last one listed. When
    that answers, only this folder is not listed: the summary lists it under `Folders that could not be listed: N`
    with the API's reason, the manifest records what this scan listed and keeps what earlier scans recorded below it that
    this scan could not settle (an entry below a sub-folder listed in full, or below a folder now a file, is removed), and `scan` exits 1 after the summary. When it does not answer, the scan stops as
    an outage and the manifest keeps the previous scan. "Only the scanned folder itself disappearing fails the
    scan" becomes "Only the scanned folder itself disappearing or failing to list fails the scan". The "manifest
    also records …" sentence adds the folders not listed.
  - **§ Downloading, "Failures that stop the run":** add one sentence that the scan phase follows the same rule for
    a folder listing (link to § Scanning a folder).
  - **§ Output and logs, notices list:** add "each folder a scan could not list".
  - **§ JSON reports, `scan` example and key list:** `"unlisted": []`.
  - **§ Exit codes, row `1`:** add "for `scan` also one or more folders that could not be listed (after the summary)".
  - **`yandex-disk-rescue/README.md`:** the Manifest section names the new table and when a commit replaces it; the
    `scanning.py` layout line adds "folders that could not be listed, told apart from an outage by a probe listing";
    the `manifest.py` line adds "the folders a scan could not list".
  - **`CHANGELOG.md` `### Fixed`:** a folder whose listing the Disk API keeps failing no longer stops `scan` or
    `download`: while the API still lists other folders, the scan commits everything else, keeps what earlier scans
    recorded below that folder, lists it with the API's reason and exits 1; an outage still stops the scan. The
    manifest records such folders (schema version 13).
  - **Stale mentions:** `rg -n "first failed listing|failed listing stops|Only the scanned folder itself" README.md
    yandex-disk-rescue/ CHANGELOG.md`.

### Stage 2: `download` and `verify` report the folders the scan could not list

- **Contract:**
  - **`Mirror.unlisted() -> list[UnlistedFolder]`:** the not-listed records of the mirror's account at or below its
    `remote_root`, and those at an ancestor of it whose `scan_root` is the root of `Manifest.last_scan(account,
    remote_root)`, ordered by path.
  - **`mirroring.unlisted_reason(folder: UnlistedFolder) -> str`**, next to `unencodable_reason`: `the scan could not
    list this folder, so files in it may be missing or out of date: <reason>`.
  - **`MirrorRun.run`:** after the passes, next to the unencodable entries, each not-listed folder is logged
    (`<path> failed: <reason>`, WARNING, so a stderr notice) and appended to `report.failed` as `Failure(path,
    reason)`; the report's path ordering applies. No preview is fetched for it. The same in a `--retry-failed` run
    and with a reused scan (`--no-scan` or within `--scan-max-age`).
  - **`verify`:** `report.not_downloaded` gets a `Problem(path, unlisted_reason(folder))` per folder, as for
    unencodable entries.
  - Docstrings of `Mirror`, `MirrorRun.run` and the verify engine rewritten as current state.
- **Behaviour to lock:**
  - **`yandex-disk-rescue/tests/test_download.py`**, next to `test_a_name_that_is_not_valid_unicode_fails_that_entry_only`:
    - **`test_a_folder_that_cannot_be_listed_fails_only_itself_and_everything_else_is_downloaded`** (the issue's
      acceptance criterion). `disk` fixture, `inject('GET', '/v1/disk/resources', 500, 500, where={'path':
      'disk:/Photos/2024'})` (exactly the two attempts), `run_download('--api-retries', '1', str(dest))`.
      - Exit 1. `Failed: 1\n  disk:/Photos/2024: the scan could not list this folder, so files in it may be missing
        or out of date: Yandex Disk API returned HTTP 500: Injected HTTP 500 (after 2 attempts)\n` in stdout.
      - `stderr_lines(result.stderr)[-1] == 'Error: 1 failed; see the list above.'`.
      - Every file of `FILES` outside `disk:/Photos/2024/` is at its local path with its content; nothing exists
        below `local_file(dest, 'disk:/Photos/2024')`.
      - No `/preview` request.
      - `run_download('--no-scan', str(dest))` reports the same failed line, exit 1, and lists nothing.
      - With the faults used up, `run_download('--scan-max-age', '0', str(dest))` exits 0 and every file of `FILES`
        is there.
    - **`test_a_folder_that_cannot_be_listed_keeps_the_files_an_earlier_scan_recorded_in_it`.**
      - A full first run (exit 0). Then `local_file(dest, 'disk:/Photos/2024/a.jpg').unlink()`,
        `disk.add_file('disk:/Photos/2024/new.jpg', …)`, `500, 500` on `disk:/Photos/2024`'s listing, and
        `run_download('--scan-max-age', '0', '--api-retries', '1', str(dest))`.
      - Exit 1, with the folder in `Failed: 1`. `a.jpg` is downloaded again (`1 downloaded`), `b.jpg` stays (it is in
        `skipped`), `new.jpg` is not there.
    - **`test_a_folder_an_ancestor_scan_could_not_list_fails_a_mirror_below_it`.**
      - `runner.invoke(app, ['scan', '--api-retries', '1'])` with `500, 500` on `disk:/Photos`'s listing
        (exit 1, first scan). Then `run_download('--no-scan', 'disk:/Photos/2024', str(dest))`.
      - Exit 1, `Failed: 1\n  disk:/Photos: the scan could not list this folder, …`.
      - Then `run_download('--scan-max-age', '0', 'disk:/Photos/2024', str(dest))`, with the faults used up: the
        scan of `disk:/Photos/2024` is the newest covering the mirror, so exit 0, no `Failed:` line, every file below
        `disk:/Photos/2024` downloaded. The record of `disk:/Photos` is still in the manifest (a `--no-scan` download
        of `disk:/Photos` into another destination still fails with it).
      - Then `scan disk:/Photos` (exit 0) clears that record: the `--no-scan` download of `disk:/Photos` exits 0.
  - **`yandex-disk-rescue/tests/test_verify.py`:** **`test_a_folder_the_scan_could_not_list_is_not_downloaded`**: a
    full download (exit 0), then `scan` with `disk:/Photos/2024` failing (exit 1), then `verify DEST`: exit 1, the
    folder under `Not downloaded` with the `unlisted_reason` text, and in `--json`'s not-downloaded list.
- **Deliverable:** a folder the scan could not list fails `download` and `verify` on every run until a scan lists it;
  the issue's acceptance criteria hold end to end.
- **Validation:**
  - `mise run check`
  - `mise run test -- yandex-disk-rescue/tests/test_download.py -k "listed or unicode or no_scan" yandex-disk-rescue/tests/test_verify.py`
  - `mise run ci`
- **Doc updates** (rewrite as current state):
  - **Root `README.md` § Downloading:** where the README says an entry whose name is not valid Unicode is reported as
    failed, add that a folder the scan could not list is reported as failed with `the scan could not list this
    folder, so files in it may be missing or out of date: <reason>`, in every run until a scan lists it (a reused
    scan included), while the files an earlier scan recorded in it are mirrored as usual. The "Retrying failures"
    paragraph ("Entries whose names are not valid Unicode have no record; they fail every run") names these folders
    too.
  - **§ Verifying a mirror, "Not downloaded":** add "a folder the last scan could not list".
  - **§ Exit codes, row `1`:** the `download` and `verify` parts already cover it through failed / not downloaded
    files; check, and name folders if the wording says "files" only.
  - **`yandex-disk-rescue/README.md`:** the Manifest section names `Mirror.unlisted` (records at or below the
    mirror's root, and above it when written by the scan the mirror's entries come from); the `mirroring.py` and `verifying.py` layout lines name the folders not listed.
  - **`CHANGELOG.md`:** widen Stage 1's `### Fixed` line: `download` and `verify` report such a folder as failed /
    not downloaded on every run until a scan lists it, and mirror everything else, the files an earlier scan
    recorded in it included.
  - **Stale mentions:** `rg -n "unencodable\(\)|not valid Unicode have no record" README.md yandex-disk-rescue/` to
    check every place listing what `download`/`verify` report from the manifest names both.

## Discovered issues

- **Fix in this PR:**
  - `test_a_folder_failing_past_the_retries_names_it_and_keeps_the_previous_scan` locks the bug; Stage 1 rewrites it.
  - A mirror below a folder an ancestor scan could not list would otherwise look complete; Stage 2 reports records
    at an ancestor of the mirror's root too.
- **Filed as issues:**
  - #44: a scan stopped by an error still ends with a `Scanned …` status line (seen in the reproduction above).

## Risks / open questions

- **Pacing cost.** Each broken folder costs `api_retries + 1` throttling answers plus a probe, which slows the shared
  limiter to its 2 s floor for about 40 s (same cost as #30's per-file failures). A Disk with many broken folders
  scans slowly; the policy is out of scope.
- **Cost of an outage before it stops:** the folder's retries plus the probe's, about 30–45 s at the defaults,
  against about 15 s today.
- **Stale rows below a folder not listed.** Files the failed listing could not settle that were deleted on the Disk
  since the earlier scan are kept and fail in
  `download` as `removed from the Disk after the scan` (one link request each, a 404). Files added are missing until
  a scan lists the folder. Both are covered by the folder's failed line saying "may be missing or out of date".
- **The probe holds a scan worker.** While a folder probes, one of the `api_concurrency` workers waits; harmless.
- **A reference folder that broke in the meantime** turns a per-folder failure into an outage stop. It answered a
  moment ago in this scan, so this needs it to break in between.
- **Schema version 13 conflicts** with any other branch adding a migration; none is in flight (`git log`, open
  issues).
- **`download` exit code with a reused scan:** a run within `--scan-max-age` of a scan that could not list a folder
  exits 1 without retrying the listing. Intended (D2): `--scan-max-age 0` lists again.
