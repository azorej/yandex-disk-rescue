# Plan: No zero-fill on filesystems without sparse files, re-planned empty journals, one re-hash at a time

Issue: #18 (body and both comments)
Branch: perf/non-sparse-segments (from `main` once #23, #15, #16 and #27 have merged)

## Context

Observed on `main` (0d2b423) with the fake server, `tmp_path` destinations and scratch XDG dirs only. There is no
real HFS+/exFAT/FAT filesystem here: `mkfs.vfat`/`mkfs.exfat`/FUSE are absent and `losetup` needs root. So a scratch
test emulated HFS+ at the syscall level, as Linux `hfsplus` behaves (`cont_write_begin`, see the #4 plan): an
`os.ftruncate` that extends a file, or an `os.pwrite` past EOF, first writes the zeros of the gap.

- **Zero-fill.** A segmented download of the 4 MiB `SEGMENTED` file (`--download-segment-threshold-mib 1`) wrote
  **4,194,304 zero bytes before any data**. That is the whole file, and it happens in `_preallocate`
  (`mirroring.py:1428`). It then fetched the 4 ranges `bytes=0-1048575 … bytes=3145728-4194303`. So each segmented
  file is written twice (zeros, then data) and, with partial files in DEST, read once more (`_file_digest`).
  Skipping the preallocation would not help: a `pwrite` at a segment's start past EOF zero-fills the gap all the
  same. On such a filesystem, segments in one file cost the zero-fill.
- **An empty journal keeps its plan.** A first run whose 4 segments all got `503` (`--download-passes 1`) left the
  journal `[(0,1048576,0,None), (1048576,2097152,1048576,None), …]`: 4 segments, no confirmed byte, and a
  4 MiB partial file. A rerun with `--download-segments 1` still fetched the same 4 ranges, because `_resume` keeps
  any `partial` record for the identity (`mirroring.py:1377-1410`). This is the live-run case from comment 1:
  18 partials of 4–5 GiB with nothing confirmed kept zero-filling after the restart.
- **Re-hash in parallel.** Six 1 MiB files cut short after 600,000 bytes were resumed by a rerun. Up to **3
  `_Stream.rehash` calls ran at once** (a counter around it). Nothing bounds them except `download_connections`.
  Comment 2 saw 19 threads in D state on the USB HDD for tens of minutes. `_adopt`'s hash of a local copy and
  `_file_digest`'s read-back are unbounded in the same way.
- `Destination.open` already probes the partial folder (`_probe_noreplace`, `destination.py:828`), and
  `Destination.partial_allocated` reads `st_blocks`. On tmpfs, overlay and `/dev/shm` here, a file `ftruncate`d to
  1 MiB allocates 0 bytes, and under the emulation it allocates 1 MiB.

**What `--partial-dir` (#20) already solves, and what is left.** With `--partial-dir` on a fast local disk with sparse
files, all three costs leave DEST: the extension is metadata-only, the re-hash reads the local disk, and the copy to
DEST is one sequential stream that also serves as the check (no read-back). But `--partial-dir` is opt-in and needs a
second disk with room for the largest file plus 1 GiB. The default layout (partial files in DEST) on an HFS+/exFAT
USB disk is the case the issue reports, and a `--partial-dir` on an exFAT stick has the same problem. So the fix
looks at **the partial folder's filesystem, whichever layout**, not at DEST. The re-plan (comment 1) helps both
layouts. The hash bound (comment 2) matters without `--partial-dir`, or with one on a rotating disk; on an SSD it
costs a few seconds per restart. All three changes are small and local, so they are still worth doing.

## Goal

- `Destination.sparse_partials: bool` tells whether the partial folder's filesystem keeps sparse files. It is
  probed once when the destination opens.
- In a partial folder without sparse files, every **new** download plan has one segment, whatever
  `download_segments` says. The file is then only appended to, and no zeros are written. `download` says so in one
  line, and in its JSON report (`sparse_partials`).
- A resumed download whose journal keeps no byte (none confirmed, or none surviving the re-hash) starts again with
  the plan the current options and partial folder give, as a new download does. A journal that keeps bytes keeps
  its segments.
- In a run, bytes already on disk are hashed for one file at a time per folder read (the partial folder, DEST; one
  hold when they are the same disk): the resume re-hash, the read-back of a
  segmented file, and the hash of a local copy before adoption. A file waiting for its turn shows
  `waiting to hash`.

## Non-goals

- **Segments as separate files concatenated at the end** (the issue's "preallocate only what the segments need"). This
  would avoid the zeros while keeping segments. But it means another partial-file model: N files per download, a
  journal per file, the move, the room accounting, and a concatenation pass that writes the file a second time. One
  stream on a non-sparse folder is 1 write pass; that design is 2 writes and 1 read. Users who want segments get them
  with `--partial-dir` on a sparse filesystem at no cost.
- **Re-planning journals that keep bytes** (for example, merging a segmented journal whose only confirmed bytes are in
  segment 0 into one stream). A segmented partial with confirmed bytes is already extended. Its zeros are written,
  and dropping the plan would drop confirmed bytes or need a merge rule. Nothing is gained for the live run.
- **Honouring an explicit `--download-segments N` on a non-sparse folder.** The settings registry cannot tell an
  explicit value from the default (`settings.py`), and the exFAT-on-SSD case that might want it has `--partial-dir`.
  See Risks.
- **Skipping `_preallocate` on sparse filesystems**, or changing `_PREALLOCATION_STEP`. It still bounds a
  cancellation while a kept segmented journal on a non-sparse folder finishes its extension.
- **Keeping digests in memory across passes of one run**, so a file deferred in pass N is not re-hashed in pass N+1.
  Filed as #29.
- **Read errors while re-hashing a partial file** (#28): this plan does not change what an `EIO` does there.
- Detecting rotating disks (`/sys/block/*/queue/rotational`): the hash bound applies everywhere.

## Stages

### Stage 1: A partial folder without sparse files gets single-stream downloads

- **Contract:**
  - `destination.Destination.sparse_partials -> bool` (property): `True` when the filesystem of `partial_folder` (the
    own `.yandex-disk-rescue/partial/` or the separate folder below `--partial-dir`) keeps sparse files.
    `Destination.open` probes it once, after the partial folder is open and after the no-replace probe. The
    definition, which is also the probe: *a file extended with `ftruncate` to 1 MiB allocates
    (`st_blocks * 512`) less than 1 MiB.* The probe must go through `os.ftruncate` and `os.fstat` (via `localfs` or
    `PartialFile`), so a syscall-level emulation sees it.
    - The probe file has a name no partial file or staged copy can have (not `<digits>`, not `<digits>.copy`). It is
      created exclusively after any leftover of that name is unlinked, and unlinked before `open` returns.
    - A probe error raises `DestinationError` with action `write` (per #16) **naming `partial_folder`**: the separate
      folder below `--partial-dir` when there is one, not DEST. Today `Destination.open` maps every `OSError` in its
      `try` block to an error naming DEST (`destination.py:270-281`), so the probe of the separate folder needs its
      own mapping, as `_separate_folder` (`destination.py:753-763`) already has. Nothing is downloaded.
    - After the probe, the run logs one file-only INFO record, always:
      `Partial folder <partial_folder> keeps sparse files: yes|no`. This record cannot go on the `download started:`
      line, because that line is written by `console.command_output` from the command's arguments
      (`commands/download.py:146`, `console.py:288`) before `_run` opens the destination.
  - `mirroring`: a new download plan uses `segment_plan(size, segments=<policy.segments when
    destination.sparse_partials, else 1>, threshold=…)`. The engine owns this rule, so `MirrorRun` callers
    (including the tests' `_mirror_run`) get it without composing the policy themselves. `TransferPolicy` and
    `segment_plan` keep their signatures. A kept journal with segments (with confirmed bytes, see Stage 2) is resumed
    with its segments as today, preallocation included.
  - `download` command output:
    - When `download_segments > 1` and `not destination.sparse_partials`, one headline after the
      `Downloading …`/`Retrying …` line and the move lines, before the room line:
      `Partial files in <partial folder> cannot be sparse: new downloads are fetched as one stream each, not as
      segments.` Like every headline it is logged to the log file. It is not printed when `download_segments` is 1.
    - The JSON report gains a top-level `"sparse_partials": true|false`, next to `partial_dir`, `partial_move` and
      `partial_room`.
- **Behaviour to lock** (all in `yandex-disk-rescue/tests/test_download.py`):
  - New test helper, next to `_io_log`: a fixture `non_sparse` that returns `emulate(folder) -> ZeroFill`. Once
    called, it patches `os.ftruncate` and `os.pwrite` so that for files whose `/proc/self/fd` path is below `folder`,
    an extension first `pwrite`s the zeros of the gap, as hfsplus does. `ZeroFill.bytes` counts those zeros. Its
    docstring says why it exists: there is no non-sparse filesystem an unprivileged test can mount. Everything else
    is real: files, `st_blocks`, the fake server, the CLI.
  - `test_a_partial_folder_without_sparse_files_gets_each_file_as_one_stream_without_zero_fill(segmented, dest,
    non_sparse)`: `emulate(dest)`, then `run_download(*SEGMENTED_ARGS, 'disk:/Seg', str(dest))`. Exit 0,
    `_fetches(segmented, SEGMENTED_PATH) == [None]`, `zero_fill.bytes == 0`, `(dest / 'large.bin').read_bytes() ==
    SEGMENTED`, the headline line in `result.stdout`, and `_journal() == []`.
    - Parametrized `partials=['in-dest', 'separate']`. For `separate`, DEST is emulated non-sparse but
      `--partial-dir partial_root` (`/dev/shm`) is sparse. Then the fetches are `SEGMENT_RANGES`, no zero is written
      below DEST (the copy only appends), and the headline is absent.
  - `test_json_reports_a_run_with_the_counts_of_its_summary_and_every_failure_and_quarantined_copy` (asserts `partial_dir`/`partial_move`/`partial_room`):
    extend it with `document['sparse_partials'] is True`. Add one JSON assertion in the new test above:
    `sparse_partials` is `False`.
  - `test_the_partial_folder_probe_reports_sparse_files_and_leaves_nothing_behind(dest, non_sparse)`:
    `Destination.open(dest, partial_dir=None)` on `tmp_path` reports `sparse_partials is True`. After
    `emulate(dest)`, a second open reports `False`. The CLI test above also asserts the `keeps sparse files: no`
    log record (`log_messages()`), and the in-dest run of
    `test_json_reports_a_run_with_the_counts_of_its_summary_and_every_failure_and_quarantined_copy` asserts `yes`.
  - `test_a_probe_error_in_the_separate_partial_folder_names_that_folder(disk, dest, partial_root, monkeypatch)`:
    patch `os.ftruncate` so that a call on a file below `_separate_folder(dest, partial_root)` raises `ENOSPC`. Then
    `run_download('--partial-dir', str(partial_root), str(dest))` exits with `DestinationError`'s code, and `stderr`
    is `Error: Could not write <separate folder>: No space left on device.` (the text from #16's `of`). No content
    request is made (`_transfers(disk) == []`). In both cases the partial folder holds no file after `open`
   .
  - The existing segmented tests (`test_a_file_above_the_segment_threshold_is_fetched_as_parallel_segments_tiling_it`,
    the resume tests) keep passing unchanged, since `tmp_path` is sparse.
- **Deliverable:** on HFS+/exFAT/FAT partial folders, new downloads are append-only single streams. The run says so,
  and the JSON shows it.
- **Validation:** `mise run check`;
  `mise run test -- yandex-disk-rescue/tests/test_download.py -k "sparse or segment or json or probe"`; `mise run ci`.
- **Doc updates:**
  - `mirroring.py` module docstring, paragraph "A file larger than the policy's segment threshold …": rewrite as
    current state. The file is segmented only in a partial folder with sparse files; otherwise it is one stream.
  - `destination.py` module and `Destination` docstrings: the partial folder's sparse-file probe.
  - Root `README.md`:
    - "Segmented files": rewrite as current state. Segments apply only when the partial folder's filesystem keeps
      sparse files; on HFS+/exFAT/FAT, new downloads are one stream and the run prints the line above.
    - "Tuning": rewrite the paragraph "Segments speed up …" as current state. The tool picks one stream there by
      itself; to get segments with such a DEST, set `partial_dir` on a filesystem with sparse files. Delete the
      "prefer `download_segments = 1` or `2`" advice; it is now automatic.
    - "Partial files": the list of disk work that moves to `PATH` ("extending a segmented file to its full size") must
      still be true; adjust it.
    - "JSON reports" `download`: `sparse_partials`.
  - `yandex-disk-rescue/README.md` layout: `destination.py` (the sparse-file probe of the partial folder),
    `mirroring.py` ("the segment plan of a new download" → one segment in a partial folder without sparse files),
    the `test_download.py`/`mirror_tree.py` lines if the helper's location is described there.
  - `CHANGELOG.md` `[Unreleased]` → `### Changed`: "`download` fetches files as one stream, without segments, when
    the partial folder's filesystem has no sparse files (HFS+, exFAT, FAT), instead of writing each segmented file's
    zeros first."
  - `rg -n 'download_segments = 1|zeros first|written twice|preallocat' README.md yandex-disk-rescue/README.md
    yandex-disk-rescue/src` to find no stale sentence.

### Stage 2: A journal that keeps no byte is re-planned with the current options

- **Contract:**
  - `mirroring._resume` (observable through `download_file`/`MirrorRun`): for an entry whose record is `partial` for
    the identity, the cloud check (`client.resource`) and the re-hash of confirmed segments run as today. Then, **when
    no segment keeps a byte** (no segment confirmed any, or each confirmed range failed its re-hash or is not held by
    the partial file), the journal is replaced by the plan a new download would get now. That plan uses the current
    `download_segments`, the threshold, and Stage 1's sparse rule. It confirms nothing, and the turn proceeds exactly
    as a new download: one stream cuts the partial file to 0 bytes, a segmented plan is extended to the size.
    - When that plan equals the recorded segments, nothing is written.
    - A journal where at least one segment keeps bytes keeps all its segments, as today.
    - A zero-byte file (no segments) is unaffected.
  - The `_resume` docstring and `download_file`'s docstring paragraph "A partial file whose `partial` record …" state
    the rule.
- **Behaviour to lock** (`tests/test_download.py`, next to
  `test_an_interrupted_segmented_download_resumes_each_segment_from_its_own_checkpoint`, whose `fewer-segments`
  case keeps locking "a journal with confirmed bytes keeps its plan"):
  - `test_a_segmented_download_that_kept_no_byte_starts_again_with_the_current_segments(segmented, dest, segments,
    fetched)`, parametrized `('1', [None])` and `('2', [bytes=0-2097151, bytes=2097152-4194303])`:
    - First run: `segmented.inject('GET', '/download', 503, 503, 503, 503, where={'path': SEGMENTED_PATH})` with
      `--download-passes 1` → exit 1. Assert the setup: `_journal()` is 4 segments, each `confirmed == start`.
    - Rerun with `--download-segments <segments>` → exit 0, `_fetches(..., earlier)` sorted equals `fetched`, and the
      file is intact.
  - `test_an_empty_segmented_journal_in_a_folder_without_sparse_files_is_fetched_as_one_stream(segmented, dest,
    non_sparse)`: this is the live case. Leave the 4-segment empty journal as above, without emulation (it was made
    before the folder was known to be non-sparse). Then `emulate(dest)` and rerun with default options. Assert
    `_fetches == [None]`, `zero_fill.bytes == 0`, and the file intact.
  - The `tampered` case of the existing resume test still resumes the untampered segments (one segment reset is not
    "no segment keeps a byte").
- **Deliverable:** a changed `--download-segments`, or a move to a non-sparse folder, applies at once to downloads
  that never received usable data.
- **Validation:** `mise run check`; `mise run test -- yandex-disk-rescue/tests/test_download.py -k "segment or
  resume or journal"`; `mise run ci`.
- **Doc updates:**
  - `mirroring.py` module docstring: rewrite "The segments are fixed when the download starts and kept by every later
    turn of it." as current state. A later turn keeps them while any segment keeps confirmed bytes; otherwise the
    download is planned again.
  - Root `README.md` "Segmented files": rewrite "The segments of a download are fixed when it starts: … even when the
    options changed since." the same way.
  - `yandex-disk-rescue/README.md` manifest section ("A partial record carries the checkpoint journal …"), if it says
    the plan is fixed: rewrite it; the `mirroring.py` layout entry ("resume a partial file segment by segment …").
  - `CHANGELOG.md` `[Unreleased]` → `### Changed`: "A download that has not kept any confirmed byte is planned again
    with the current `--download-segments` when it resumes."

### Stage 3: Bytes already on disk are hashed for one file at a time per folder read

- **Contract:**
  - A run-wide hash hold **per folder read**, shared by every turn like the `copying` lock (`_RunTools`):
    - the **partial-folder hold** covers the resume re-hash of a file's confirmed segment bytes (`_resume`: all
      segments of the file in one hold, not the `client.resource` check before it) and the read-back of a finished
      segmented file (`_file_digest`, which only happens with partial files in DEST);
    - the **DEST hold** covers the hash of a local copy at a final path before adoption (`_adopt`), released before
      `placement` is taken;
    - with partial files in DEST both are **one and the same hold** (one disk); with `--partial-dir` they are two
      independent holds, so resume re-hashes on a fast partial folder never wait behind adoption reads of DEST, and
      vice versa.
    At most one file at a time hashes under each hold.
  - The copy to DEST (`copying`) stays a separate hold, so a copy and a hash may run at once. No hold is taken while
    another is held, so there is no lock-order risk.
  - A file waiting for the hold has the `TransferState` `'waiting to hash'` (new member in `progress.py`). It logs
    at debug level `<path> waits for another file's hash`, like `waits for the copy of another file`. A waiting
    file keeps its place among the `download_connections` files in progress, as one waiting for the copy does.
  - Cancellation: a file cancelled while waiting holds nothing. One cancelled while hashing releases the hold once
    its thread call returns, as `DiskThreads` guarantees.
- **Behaviour to lock** (`tests/test_download.py`):
  - `test_bytes_on_disk_are_hashed_for_one_file_at_a_time(disk, dest, monkeypatch, phase)`, parametrized
    `phase=['resume', 'read back', 'adopt']`:
    - Set `destination_module._READ_SIZE = 256 * 1024` so each file takes several reads. An `_io_log(monkeypatch,
      ('pread',))` variant adds a 20 ms sleep per `pread`, so unbounded readers interleave reliably. Today's code
      fails this test.
    - `resume`: four 2 MiB files under `disk:/R`, each cut short by `Truncate(after_bytes=MiB)` in a first run with
      `--download-retries 0 --download-passes 1`. The rerun is logged.
    - `read back`: two `SEGMENTED`-like files above `--download-segment-threshold-mib 1`, partial files in DEST.
    - `adopt`: three files whose exact content already sits at their final paths before the first run.
    - Assertion for each phase: the paths of the logged `pread`s on the files hashed (partial files, or the copies
      in DEST) form one contiguous run per file:
      `len([k for k, _ in itertools.groupby(paths)]) == len(set(paths))`. The run exits 0 and every file is intact.
    - **Red on main first:** the implementer runs each phase against the code before the hold and shows it failing
      (paste the failing output in the stage report). The `read back` phase is green on main if the two segmented
      fetches finish at different times; gate them so they finish together (for example, hold the last chunk of both
      files' last segments on one `threading.Event` in a patched write, released once both are waiting), so both
      read-backs start together.
  - `test_with_a_partial_folder_resume_re_hashes_do_not_wait_for_an_adoption_hash_of_dest(disk, dest, partial_root,
    monkeypatch)`: a run with `--partial-dir` holding one resumed partial (cut short in a first run) and one file
    whose exact content already sits at its final path in DEST. A patched `pread` blocks the first read of the DEST
    copy on an event, which is set only once a `pread` of the partial file below the partial folder has been seen
    (with a 10 s timeout that fails the test). It exits 0 with both files intact; with one shared hold it would
    time out.
  - `progress.py`'s `TransferState` gains `'waiting to hash'`. If `test_output.py` enumerates the states of the
    view, extend it there (today it does not).
- **Deliverable:** a restart on a rotating disk reads resumed partials one at a time, sequentially. Comment 2's seek
  storm is gone.
- **Validation:** `mise run check`; `mise run test -- yandex-disk-rescue/tests/test_download.py -k "hashed or resume
  or adopt"`; `mise run test -- yandex-disk-rescue/tests/test_output.py`; `mise run ci`.
- **Doc updates:**
  - `mirroring.py` module docstring: add as current state, next to the copy paragraph, that re-hashing confirmed
    bytes, reading back a segmented file and hashing a local copy run for one file of the run at a time. Update the
    `_RunTools` and `download_file` docstrings (the hold).
  - Root `README.md`:
    - "Output and logs" states list (README.md:816): add `waiting to hash` (another file's bytes on the same disk are
      being hashed), and fix `hashing (a local copy)`, which is stale today since `_resume` also sets `hashing`
      (`mirroring.py:1397`): `hashing` (a local copy, or the confirmed bytes of a resumed download).
    - "Partial files"/"Tuning": where they mention re-reading confirmed bytes on a resume, say it is one file at a
      time.
  - `yandex-disk-rescue/README.md` layout: `mirroring.py` (one hash of bytes on disk at a time) and `progress.py` (the
    states from waiting for room … to copying, if it lists them).
  - `CHANGELOG.md` `[Unreleased]` → `### Changed`: "Re-hashing the confirmed bytes of resumed downloads, reading back
    segmented files and hashing local copies run for one file at a time per disk (the partial folder, DEST), so a
    restart on a hard disk reads sequentially."

## Discovered issues

- **Fix in this PR:**
  - `_adopt`'s local-copy hash and `_file_digest`'s read-back run in parallel without a bound, just like the resume
    re-hash in comment 2. Stage 3 covers them under the hash holds (DEST hold for adoption).
  - The README "Tuning" advice to set `download_segments = 1` or `2` by hand on HFS+/exFAT/FAT becomes automatic
    (Stage 1 rewrites it).
- **Filed as issues:**
  - #29: a download resumed by a later pass of the same run re-hashes its confirmed bytes again (keep the digests of
    the previous turn in memory).
  - Related, already open: #28, read errors on a partial file during the resume re-hash or the move stop the run. It
    touches `_resume`'s re-hash too; see Risks.

## Risks / open questions

- **The probe's definition.** "`ftruncate` to 1 MiB allocates less than 1 MiB" holds on ext4, xfs, btrfs, tmpfs,
  overlay and ZFS (sparse). It is false on hfsplus, vfat and exfat, which allocate on extension; exFAT's
  valid-data-length writes no zeros on `ftruncate` but zero-fills on a `pwrite` past the valid length, so treating it
  as non-sparse is right. It is also false on ntfs3 for non-sparse-flagged files, which is right too. A network
  filesystem may report either; the outcome is only a choice between one stream and segments, never a correctness
  issue. Reviewers with an HFS+ disk should check the headline on the live mirror (manual step below).
- **Explicit `--download-segments N` on a non-sparse folder is overridden.** The headline makes this visible. A user
  with exFAT on an SSD loses segments for files that never had them. The way out is `--partial-dir`. If the user
  wants an override flag, that is a new option and a follow-up, not part of this plan.
- **Moving partials from a sparse `--partial-dir` back into a non-sparse DEST** (dropping the option) writes each
  segment's confirmed bytes at its offset, which zero-fills the gaps (`_move_partial`). Journals with confirmed bytes
  keep their segments (Stage 2's rule). Journals without any are re-planned to one stream, and their file is cut to
  0 bytes. This is acceptable and is documented by the Stage 2 rule. It is not changed here.
- **Serial adoption hashing.** The DEST hold also serializes `_adopt` (`mirroring.py:1160`, a whole-file
  SHA-256+MD5). A rerun with fresh state over a filled DEST, or a mirror rebuilt after the manifest is lost, can
  mean adopting TBs. On an NVMe disk, one hashing thread does about 0.5–1 GB/s of SHA-256+MD5. Today several cores
  hash in parallel, so 2 TiB could take about 35–70 min instead of perhaps 10–20 min: tens of minutes added. On the
  USB HDD this tool targets, parallel adoption reads are seek-bound and slower than one sequential reader, so the
  hold wins there. Accepted for v1. If the NVMe case matters, a follow-up can make the DEST hold a small semaphore
  (an option), not a lock. Because the holds are per folder, resume re-hashes on a fast `--partial-dir` are never
  delayed by DEST adoption.
- **Serial hashing on fast disks.** About 1 GB/s of SHA-256+MD5 per file. 10 GiB confirmed means about 10–20 s at
  restart, against tens of minutes measured on the HDD. Files waiting for the hold occupy `download_connections`
  slots, so new downloads start later during that time.
- **Coordination.** #27 (failure ordering) and #16 (`DestinationError` action) touch `mirroring.py` and
  `destination.py` before this lands: the stage implementers rebase on them, and Stage 1's probe uses #16's
  `DestinationError` shape. #28, if planned later, composes with Stage 2: bytes dropped after a read error make "no
  segment keeps a byte", so the download is re-planned. No manifest migration is needed (#27 takes v10). The
  journal's schema is unchanged.
- **Manual verification (main with the user, not in CI; never against the live mirror while it runs):** after the
  live run ends, a scratch `XDG_STATE_HOME` and a scratch folder on the HFS+ USB disk. Download a folder with one file
  over 1 GiB. Expect the headline, one `GET` without `Range`, and no zero-fill in `iostat`. Then a run with
  `--partial-dir` on the local disk: segments, and no headline.
