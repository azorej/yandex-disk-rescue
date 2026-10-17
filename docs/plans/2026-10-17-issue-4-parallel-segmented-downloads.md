# Plan: Parallel and segmented downloads with crash-safe checkpoints

Issue: #4 (umbrella #9)
Branch: feat/parallel-segmented-downloads (from `main` after the #3 PR, `feat/verified-mirror-download`, has merged)

## Context

`download` (#3) mirrors one file at a time. Each file is one HTTP stream into
`DEST/.yandex-disk-rescue/partial/<entry id>`, hashed as it arrives. A rerun resumes from the partial file's *length*.
All disk work (hashing an existing file before adopting it, re-reading a partial's prefix, writes, fsyncs) runs on
the event loop thread.

Facts observed for this plan:

- **Dead files serialize the run.** On the real account, 11 of 46 files in a trial folder never sent a byte, the same
  files every time, over 7 passes with fresh links. Each one costs a full stall timeout per pass: 11 × 15 s × 7 passes
  ≈ 19 min of pure waiting. Reproduced against the fake server: 3 dead files (`Delay(3)` on every `/download`) named
  before 8 healthy ones, `--download-timeout 1 --download-passes 1`:
  ```
  a-dead0.bin    +0.00 s
  a-dead1.bin    +1.01 s
  a-dead2.bin    +2.01 s
  b-healthy0.bin +3.02 s   <- the first healthy file waits for every dead one
  ...
  b-healthy7.bin +5.17 s
  exit 1 elapsed 5.6 s
  ```
  (Scratch script, fake server and scratch XDG dirs only. `test_transfer.py` passes, 36 tests, on the branch.)
- **The target disk.** `/mnt/oblako` is HFS+ on USB, probably unjournaled, with 3.7 TB free. The account holds 133,696
  files, 2.1 TiB; the largest file is about 16.5 GiB.
  - **No sparse files.** On Linux, hfsplus extends a file with `cont_write_begin`: a write or `ftruncate` past EOF
    zero-fills the gap first, synchronously, in the calling thread. Writing segment 4 of a 16.5 GiB file into an empty
    file would write about 12 GiB of zeros inside one `pwrite`. Done on the event loop, that would freeze every
    transfer for minutes.
  - **Power cuts.** An unjournaled disk can keep a garbage tail after a power cut. A partial file's length proves
    nothing.
- **The original tool** (fefenke/Yandex-Disk-Rescue) splits files above 256 MB into 4 streams (1–8 configurable). It
  keeps one data file plus a segment map, checkpoints every 32 MiB or 15 s, and keeps a SHA-256 over its journal so a
  corrupt or unconfirmed tail is thrown away.
- **Seams #3 left.**
  - `mirroring.download_file(...)` is the per-file awaitable.
  - `MirrorRun.run(client, content, *, retries, passes)` drives the passes.
  - `ContentClient.fetch(href, *, start, end, size, write)` fetches one closed byte range with absolute offsets and
    already sends a `Range` for `[0, k)` when `k < size`.
  - `Digest` needs its updates in offset order.
  - The manifest is synchronous `sqlite3` in WAL mode, and its connection is bound to the loop thread.
  - `LocalNames` keeps sibling keys per folder.
  - `downloads` is written with an UPSERT (`ON CONFLICT ... DO UPDATE`), so a cascade from it never fires on a
    rewrite.
  - The schema is at version 3.
- **`ContentClient`'s HTTP limits.** It uses httpx's default limits (100 connections, 20 keep-alive) and a pool
  timeout equal to the stall timeout. Under concurrency, waiting for a pooled connection would be reported as `the
  download stalled`.
- **`api_interval_ms` already exists.** It covers the issue's "request interval" (#2): every link request and
  resource check goes through the API limiter. Content requests are not paced; only the connection cap limits them.

## Goal

- **Concurrent files.** Up to `download_connections` (default 20) files are in progress at once, and at most that
  many requests to the download host are in flight. A file that never answers holds one connection for one stall
  timeout and does not delay any other file.
- **Segments.** A file larger than `download_segment_threshold_mib` (default 256 MiB) is fetched as
  `download_segments` (default 4) ranged segments in parallel, into one partial file. Once all segments are in, the
  whole file is read back and verified against its size, MD5 and SHA-256.
- **Checkpoints.** Every download keeps a checkpoint journal in the manifest: per segment, the confirmed offset and a
  SHA-256 of the confirmed bytes. A checkpoint is written after every `download_checkpoint_mib` MiB of a segment
  (default 32), every 15 s while bytes arrive, and whenever an attempt ends, Ctrl+C included. The data is fsynced
  before the checkpoint is recorded.
  - **Resume.** A run resumes each segment from its confirmed offset only after re-hashing the confirmed bytes on
    disk. Bytes past a checkpoint, and confirmed bytes that no longer match, are fetched again and never trusted.
- **Validation.** All four new limits go through the options registry (flag > env > config > default) with validated
  ranges. An out-of-range value exits 2 at startup.
- **Event loop.** No disk work that scales with file size or waits for a flush runs on it.

## Non-goals

- Bandwidth caps (out of scope in the issue).
- Splitting a slow segment again while it runs (work stealing), or adapting the connection count at runtime.
- Progress display, logging, `--json` (#6); quarantine, `verify`, `--retry-failed` (#5).
- Finding out why some files never start (#14).
- A checkpoint interval in seconds as an option: 15 s is a constant, as in the issue.

## Exit-code contract

Unchanged from #3. Concurrency must not change what the user sees when a run stops:

- A run-stopping error (`DestinationError`, `TokenRejectedError`, `DiskApiError`) in any file cancels the other
  files. Each cancelled file checkpoints. The first such error is then raised *as itself*, never as an
  `ExceptionGroup`, so it renders as the one `Error: ...` line it does today.
- Ctrl+C still exits 130 with `Interrupted; rerun to continue the download.`

## Stages

### Stage 1: Disk work off the event loop

- **Contract:**
  - **`ContentClient.fetch` write callback.** It becomes `fetch(href, *, start, end, size, write: Callable[[int,
    bytes], Awaitable[None]])`. Each chunk's `write` is awaited before the next chunk is read; that is the
    backpressure. Time spent awaiting `write` still does not count toward the stall timeout. Exceptions from `write`
    propagate unchanged.
  - **Off-loop rule (mirroring and destination).** Disk work that scales with file size or waits for the disk runs in
    worker threads (`asyncio.to_thread` or equivalent), never on the event loop thread:
    - hashing an existing file for adoption;
    - re-reading a partial file's prefix;
    - writing partial bytes together with their digest updates;
    - `fsync` of files and folders (`PartialFile.finish`, `place`, `move_aside`, `make_folder`, `set_mtime`).

    `Mirror` (SQLite) and `LocalNames` stay on the loop thread: the sqlite3 connection is bound to it, and a name
    conversion must reserve its sibling key with no `await` between computing and recording it.
  - **Bounded thread calls.** Every call handed to a thread does a bounded amount of work, never a whole file:
    - one read chunk plus its hash update (at most 1 MiB);
    - one received chunk written plus its digest update;
    - one `fsync`;
    - one `ftruncate` step of at most 64 MiB (Stage 4);
    - one rename plus its directory `fsync`.

    Hashing a 16.5 GiB file is therefore many calls issued from the loop, not one call. A cancelled file waits for at
    most one call in flight, and a write never queues behind a whole-file job.
  - **Executor.** The mirror run owns a dedicated `ThreadPoolExecutor` sized `streams + 4`. `streams` is the number
    of fetches that can be in flight: 1 in this stage, `download_connections` from Stage 2, because a write only
    happens while its fetch holds a connection. The extra 4 serve hashing, fsync and placement. The run shuts the
    executor down on exit, also on Ctrl+C, with `cancel_futures=True`, waiting only for the calls already running.
    The shutdown happens only after every file task, including its cancellation checkpoint (Stage 3), has
    finished. `asyncio`'s default executor is not used for this work.
  - **Read seam.** Stage 1 adds `localfs.read(fd, size) -> bytes` (one chunk), used by `ExistingFile.chunks` and
    `PartialFile.chunks`. The Stage 1 ticker test and the Stage 2 "adoption and download overlap" test both slow
    reads by patching this one function.
  - **Descriptor lifetime.** A partial or existing file's descriptor is closed only after every thread operation
    submitted on it has returned, including when the awaiting task is cancelled. Otherwise a cancelled `pwrite` could
    land in a reused descriptor number.
  - **Chunk order.** Writes and digest updates of one stream stay in offset order.
  - No CLI-visible change: still one file at a time.
- **Behaviour to lock:**
  - **The existing suite** (`test_transfer.py`, `test_download.py`) passes with the callback made async. The
    `test_transfer.py` helper and `test_time_spent_writing_does_not_count_against_the_stall_timeout` move to an async
    `write` that awaits `asyncio.sleep` longer than the timeout.
  - **New `test_transfer.py` test.** A `write` raising after the first chunk propagates its exception, and no further
    chunk is written.
  - **Cancellation waits for the write in flight** (new, `test_download.py`, deterministic).
    - Setup: run `MirrorRun.run` as an asyncio task in-process against the fake server, a real `Destination` in
      `tmp_path` and a real `Manifest`. The partial file's write seam (`PartialFile.write`) is wrapped so that its
      second chunk blocks on a `threading.Event`.
    - Once the blocked write has started, cancel the task. The task must not finish while the write is blocked.
    - Release the event, then await the task (`CancelledError`).
    - The wrapper records that `os.fstat` on its descriptor still succeeded after the write returned. The partial
      file holds exactly the bytes of both chunks.
  - **The loop keeps ticking during local hashing** (new, `test_download.py`, in-process as above).
    - Setup: an untracked 8 MiB local file at a final path is adopted, with its read seam slowed by 50 ms per chunk.
      An asyncio ticker task records its wake-ups every 10 ms.
    - No gap between two ticks exceeds 200 ms while the adoption runs. On the current code the whole hash runs in
      one synchronous loop step, so the gap is over 400 ms.
  - **SIGINT test.** `test_a_download_stopped_mid_file_resumes_from_its_partial_file[sigint]` still exits 130 and keeps
    a partial file of the bytes written.
- **Deliverable:** the same behaviour, with every size-proportional or flushing disk operation off the loop.
- **Validation:** `mise run check`;
  `mise run test -- yandex-disk-rescue/tests/test_transfer.py yandex-disk-rescue/tests/test_download.py`.
- **Doc updates:** package `README.md` Layout lines for `api/transfer.py` ("into an async write callback") and
  `mirroring.py` (disk work in worker threads). Rewrite them as current state.

### Stage 2: Concurrent files under a download connection cap

The first test of this stage is the reproduction: the dead-files test below fails on the current code (healthy
files start only after every dead file's stall, as in the Context output).

- **Contract:**
  - **New option** (registry entry, config key and env var):

    | Option / flag | Env var | Default | Valid |
    |---|---|---|---|
    | `download_connections` / `--download-connections` (`download`) | `YANDEX_DISK_RESCUE_DOWNLOAD_CONNECTIONS` | 20 | 1–64 |

  - **`ContentClient(*, timeout: float, connections: int)`.**
    - At most `connections` fetches are in flight, from the start of the request (redirect hops included) until the
      body ends or fails. A fetch over the cap waits for a free connection.
    - That wait never counts toward the stall timeout.
    - The httpx pool is sized so it never makes a request wait (fixes the pool-timeout-as-stall problem in Context).
    - `connections` is exposed as a read-only property.
  - **`MirrorRun.run(client, content, *, retries, passes) -> MirrorReport`.**
    - Up to `content.connections` files are in progress at once, in the first pass and in every later pass.
    - The walk stays in path order. A folder is created (or moved aside, or converted) before any entry below it
      starts. Files are started in path order as slots free up.
    - Entries are never all turned into tasks at once: the walk waits for a slot. This bounds memory for 133k
      entries.
  - **Placement lock: one critical section per final path, never per step.** One mirror-wide `asyncio.Lock` is held,
    in a single acquisition, across each of these sequences:
    - **A fetched file:** `lstat` of the final path → `_alias` → move aside → place, including the name conversion
      and second attempt inside `_create` → `record_verified`.
    - **An adopted file:** the hash runs *outside* the lock and records the `lstat` taken when it opened the file.
      Then one acquisition spans: re-`lstat` the path (same inode, size and mtime as at hash time; otherwise not
      adopted, and the file goes on to download) → `_alias` → `set_mtime` → `record_verified` → `remove_partial`.
      The mtime is set only after the re-check, so it cannot break it.
    - **A folder in the walk:** `make_folder`, any move aside and the second attempt, and the conversion.

    This closes two races. Releasing the lock between place and record would let a file whose path the filesystem
    folds onto it (a hard link, or an HFS+ folding the collision key misses) see no verified owner in `_alias` and
    move the freshly placed file aside. Two concurrent adoptions of one inode would let `record_verified`'s release
    of same-inode records silently delete the first record, with no alias failure. With the single section, the
    second file always sees the first one's committed record and fails with the "same file as" reason, which is the
    sequential outcome. The thread calls inside the section are the bounded ones listed in Stage 1.
  - **Report.** Every list in `MirrorReport` is in path order whatever the completion order. `mirror_summary` output
    is byte-identical to a sequential run's.
  - **Stopping errors.** As in the exit-code contract above: the first run-stopping error is raised as itself and the
    other files are cancelled. Errors raised by other files while they are cancelled are attached to it with
    `add_note`; they are not rendered, and only the first `Error:` line is printed. `FileFailed` and `FileDeferred`
    stay per file. A Ctrl+C always wins: exit 130, whatever the cancelled files raise.
  - **Passes.** `_later_passes` keeps its semantics (pause, `Retry-After`, the pass limit, `(after N passes)`) and runs
    the deferred files concurrently under the same bound.
  - **`download`.** It passes `download_connections` to `ContentClient`. `_Transfers` gains `connections`.
- **Behaviour to lock** (extend `tests/test_download.py` and `tests/test_transfer.py`; grow
  `tests/fake_disk_server.py`):
  - **Fake server.**
    - A peak-in-flight counter for content requests only (`/download`, `/storage`), separate from `peak_in_flight`,
      which `test_api_client.py` uses for API requests. It decrements *before* the response's last chunk is written,
      so a client can never see the end of a body (and start its next request) while the server still counts the
      old one. Without this, a peak of cap + 1 would be a flaky false failure.
    - Each request-log entry records when its response finished (last byte written or connection broken).
    - Cap tests use no `Delay` faults: a server thread sleeping in a `Delay` after the client gave up still counts
      as in flight.
  - **Dead files do not hold up healthy ones** (the reproduction).
    - Setup: 3 files whose every `/download` gets `STALL`, named before 12 healthy files, with
      `--download-timeout 3 --download-passes 1` and *no* connections flag (the Stage 2 default of 20). Written this
      way, the test runs on the code before this stage and fails for the right reason: the healthy files start only
      after the dead files' stalls, as in the Context output. It does not fail because of an unknown option.
    - Exit 1, with exactly the 3 dead files failed `stalled` and every healthy file verified.
    - Every healthy file's `/download` request starts less than `STALL_TIMEOUT` seconds after the first dead file's
      request, from the request-log `started` times.
  - **Throughput scales with the cap and never passes it:**
    - Setup: 8 files of 10 × 64 KiB chunks with `chunk_delay = 0.05`, run with `--download-connections 1` and then 4,
      into separate destinations.
    - The content peak in flight is `<= cap` in both runs and reaches the cap (exactly 1, exactly 4).
    - The transfer span of each run is measured from the request log: the first content request's start to the last
      content response's finish. That excludes scan and process start-up. The 4-connection span is less than half
      the 1-connection span. The bound is loose against the ideal of a quarter, so CI noise does not fail it.
  - **ContentClient cap** (`test_transfer.py`):
    - Setup: 5 concurrent fetches with `connections=2`, `timeout=1`, and bodies that take about 1.5 s each at a
      steady `chunk_delay`.
    - All 5 succeed with the right bytes, and the server's content peak is 2.
    - Waiting longer than the timeout for a slot does not stall.
  - **Adoption and download overlap.**
    - Setup: one file's adoption reads its local copy slowly, through the Stage 1 read seam with an injected delay.
    - Another file's `/download` request starts and finishes while those reads are still going, from timestamps
      recorded by the injected reader.
  - **Alias under concurrency.** Extend `test_a_hard_link_to_another_verified_file_is_never_replaced` with a case
    where neither path is recorded yet and both are handled concurrently.
    - Setup: two Disk files with identical content, and untracked local copies at both final paths that are one
      inode (a hard link).
    - Exactly one is adopted. The other fails with the "same file as" reason, nothing is moved aside, and the
      manifest holds one verified record for that inode.
    - The case runs 20 times in a loop with fresh state, because the race it guards is timing-dependent.
  - **Existing tests stay green at the default of 20 connections:**
    - the summary ordering tests (several adopted, renamed and moved-aside entries);
    - `test_a_full_or_failing_destination_stops_the_run_and_a_rerun_goes_on`, which still shows the single
      `Error: Could not write ...` line and exit 1;
    - the token-rejected test;
    - the folder-blocking tests.
  - **Options:** `--download-connections 0` and `65` exit 2. Extend `test_a_transfer_option_out_of_range_is_a_usage_error`
    and the `tests/test_config.py` registry tests.
- **Deliverable:** several files download at once under a global connection cap; a dead file costs one connection
  for one stall timeout.
- **Validation:** `mise run check`;
  `mise run test -- yandex-disk-rescue/tests/test_download.py yandex-disk-rescue/tests/test_transfer.py yandex-disk-rescue/tests/test_config.py`.
- **Doc updates:**
  - **Root `README.md` "Downloading":**
    - Rewrite "one file at a time" as current state: up to `download_connections` files at once, folders still
      created before their contents, and the summary in path order (`rg -n 'one file at a time|one at a time'
      README.md yandex-disk-rescue/README.md`).
    - Add the "Configuration" table row.
    - Add a new subsection "Tuning", written as current state:
      - what `download_connections` caps;
      - that link requests are paced by `api_interval_ms` and `api_concurrency`;
      - the **HDD caveat**: many parallel writes make a hard disk seek; on a USB HDD start around 8 connections and
        raise them while throughput grows.
  - **Package `README.md`:** the env var row, and the `fake_disk_server.py` line (content peak counter).

### Stage 3: Checkpoint journal for every download

Every download still has one segment here; Stage 4 splits large files.

- **Contract:**
  - **Migration 4** (append; earlier migrations untouched):
    ```sql
    CREATE TABLE download_segments (
        mirror_id INTEGER NOT NULL,
        entry_id INTEGER NOT NULL,
        start INTEGER NOT NULL,
        end INTEGER NOT NULL,
        confirmed INTEGER NOT NULL,
        digest TEXT,                -- SHA-256 hex of bytes [start, confirmed)
        PRIMARY KEY (mirror_id, entry_id, start),
        FOREIGN KEY (mirror_id, entry_id) REFERENCES downloads (mirror_id, entry_id) ON DELETE CASCADE,
        CHECK (0 <= start AND start < end AND start <= confirmed AND confirmed <= end),
        CHECK ((confirmed = start) = (digest IS NULL))
    )
    ```
    It also inserts one segment `[0, size)` with `confirmed = 0` for every existing `partial` row with `size > 0`. The
    invariant then holds for every manifest: *a partial record's segments tile `[0, size)` in order; a zero-byte file
    and a verified record have none.* Partial files left by #3-era runs therefore restart from byte 0 (their lengths
    were never confirmed). The README says so in the Manifest section's migration note; no compatibility path is
    kept.
  - **Manifest API** (the names are the contract):
    ```python
    @dataclass(frozen=True)
    class Segment:
        start: int
        end: int
        confirmed: int  # bytes [start, confirmed) were fsynced before this was recorded
        digest: str | None  # SHA-256 hex of [start, confirmed); None exactly when confirmed == start


    # DownloadRecord gains: segments: tuple[Segment, ...]   (partial: tiles [0, size); verified: ())


    class Mirror:
        def download(self, entry_id: int) -> DownloadRecord | None: ...

        # The entry's current record with its segments, read now. Every turn of a file starts from it, the first
        # pass and each later pass alike; `_later_passes` no longer builds a partial record by itself.
        def record_partial(self, entry_id: int, identity: RemoteIdentity, segments: Sequence[Segment]) -> None: ...

        # Replaces the record and all its segments in one transaction; ValueError unless `segments` tile [0, size).
        def checkpoint(self, entry_id: int, segments: Sequence[Segment]) -> None: ...

        # Updates confirmed/digest of each segment, matched by start, in one transaction; ValueError when the
        # record is not partial or the boundaries differ from the recorded ones.
        # record_verified and forget delete the segments (the UPSERT does not cascade; delete them explicitly).
    ```
  - **New option:**

    | Option / flag | Env var | Default | Valid |
    |---|---|---|---|
    | `download_checkpoint_mib` / `--download-checkpoint-mib` (`download`) | `YANDEX_DISK_RESCUE_DOWNLOAD_CHECKPOINT_MIB` | 32 | 1–1024 |

    Also a module constant `CHECKPOINT_SECONDS = 15` in `mirroring`, which tests may patch in-process.
  - **When a checkpoint is taken.** While a file transfers, a checkpoint is taken:
    - when a segment has received `download_checkpoint_mib` MiB since its last checkpoint;
    - or `CHECKPOINT_SECONDS` after the file's last checkpoint, when new bytes arrived since;
    - and whenever an attempt ends with new bytes: a transient failure, a non-transient failure that keeps the
      partial, or cancellation (Ctrl+C, or another file's run-stopping error).
  - **What a checkpoint does.** It fsyncs the partial file off-loop, then calls `Mirror.checkpoint`. It records, per
    segment, only bytes whose write returned before that fsync began, with the SHA-256 of `[start, confirmed)`. That
    digest is kept incrementally as the segment's bytes arrive in order. The recorded value is a snapshot
    (`hash.copy().hexdigest()`) of each segment's hash taken when the fsync begins, so it covers exactly the
    confirmed bytes. For a segment starting at 0 it equals the file SHA-256's running state, which the implementer may
    share.
  - **A checkpoint that fails** (the fsync or the commit raises, `EIO` or `ENOSPC` for example, also during
    cancellation) leaves the journal at its previous checkpoint. The journal only ever under-claims. The error is a
    run-stopping error under the Stage 2 rules; during a Ctrl+C the exit is still 130.
  - **Resume (replaces "resume from the partial's length").** At the start of a file's turn, a `partial` record for
    the entry's identity is resumed once `client.resource` confirms the cloud file. That check and the
    changed/removed/stale rules stay exactly as in #3. Then:
    - **Re-hash.** For each segment, `[start, confirmed)` is re-read off-loop and its SHA-256 compared with `digest`.
    - **Match:** the segment resumes at `confirmed`.
    - **Mismatch, or a partial file too short to hold it:** that segment is reset to `confirmed = start` in the
      journal and fetched from its start.
    - **Bytes past `confirmed`** are never trusted. A single-segment partial is truncated to `confirmed` before
      fetching; for multi-segment files see Stage 4.
    - **Trust within a turn.** Immediate retries after progress continue from the bytes received in this turn
      (written by this process); every *new* turn (a later pass, a later run) starts from the journal as above.
  - **Zero-byte files** are still fetched once without a range, as today.
- **Behaviour to lock:**
  - **`tests/test_manifest.py`** (real SQLite in `tmp_path`):
    - `record_partial` with segments round-trips through `Mirror.entries()` and `Mirror.download(entry_id)`.
      `Mirror.download` returns `None` for an entry with no record, and sees a `checkpoint` written after `entries()`
      was read.
    - **Later pass reads the journal.** In `test_download.py`, a file deferred in pass 1 after a `Truncate` at
      300,000 bytes resumes in pass 2 from the journal (`range: bytes=300000-...`), not from a record built in
      memory.
    - `checkpoint` updates confirmed and digest.
    - `checkpoint` with other boundaries, or on a verified record, raises `ValueError` and writes nothing.
    - A `confirmed` beyond `end` raises (CHECK).
    - `record_verified` and `forget` leave no segment rows.
    - **Upgrade from v3.** A new fixture `tests/data/manifest-v3.sql`, a `.dump` made with the code *before* this
      stage (as `manifest-v1.sql` was) and holding one partial and one verified record, ends at `user_version == 4`.
      The partial has one `[0, size)` segment with `confirmed == 0`, and the verified record has none. The v1-fixture
      upgrade test now ends at version 4 too.
  - **`tests/test_download.py`:**
    - **SIGKILL re-fetches at most one checkpoint interval.**
      - Setup: a 4 MiB single-segment file with `--download-checkpoint-mib 1` and `chunk_delay = 0.25` (64 KiB
        chunks). The child is killed once the journal (read with a separate `sqlite3` connection) confirms at least
        1 MiB.
      - The rerun exits 0 and verifies the file. Its first fetch is `bytes=<confirmed>-<size-1>`, where `confirmed`
        is the journal value after the kill.
      - `bytes_served_before_kill - confirmed <= 1 MiB + 4 chunks`, with each part of the slack accounted for:
        - 1 chunk: the checkpoint triggers on the first chunk that crosses the interval;
        - 1 chunk: received but its write not yet returned when the fsync began;
        - 2 chunks: served but not yet read by the client. At `chunk_delay = 0.25` that is 0.5 s of client latency,
          which covers CI scheduling.

        This needs a fake-server addition: the request log records the body bytes each response sent, including a
        response cut by a broken pipe.
      - This replaces the `[sigkill]` case of `test_a_download_stopped_mid_file_resumes_from_its_partial_file`, whose
        expectation (resume from the length) no longer holds.
    - **SIGINT checkpoints.** The `[sigint]` case: the journal's confirmed offset equals the bytes written (more than
      256 KiB), and the rerun's first fetch starts there.
    - **Time-based checkpoint.**
      - Setup: `CHECKPOINT_SECONDS` patched to 0.3, and the run in a background thread over a slow file smaller than
        the MiB interval. The thread runs `CliRunner`; `asyncio.run` installs no signal handler off the main thread.
      - The journal's confirmed offset grows above 0 and stays below the size while the transfer is still running.
    - **Tampered confirmed bytes are not trusted.** After `_leave_partial` (journal at 300,000), flip one byte inside
      `[0, 300000)` of the partial file. The rerun fetches the whole file (no `range`) and verifies it.
    - **A deleted or shortened partial file** restarts from 0.
    - **Rewrite `test_a_partial_file_holding_the_whole_file_is_not_fetched_again[complete|longer]` against the
      journal:**
      - a partial whose journal confirms the whole file goes straight to verification with no content request;
      - a partial holding bytes past `confirmed` is truncated to `confirmed`, and only the rest is fetched.
    - **Unchanged:** `test_ctrl_c_in_the_pause_between_passes_...` (partial size 300,000, two fetches) and
      `test_a_partial_file_is_resumed_by_the_next_run_once_the_cloud_file_is_confirmed`.
    - **Options:** `--download-checkpoint-mib 0` and `1025` exit 2.
- **Deliverable:** crash-safe, integrity-checked resume for every file.
- **Validation:** `mise run check`;
  `mise run test -- yandex-disk-rescue/tests/test_manifest.py yandex-disk-rescue/tests/test_download.py yandex-disk-rescue/tests/test_config.py`.
- **Doc updates:**
  - **Root `README.md` "Interruptions, stalls and retries":** rewrite as current state:
    - bytes are kept up to the last checkpoint (every `download_checkpoint_mib` MiB, every 15 s, and whenever a
      download attempt ends, Ctrl+C included);
    - after a kill or a crash, at most one checkpoint interval is fetched again;
    - confirmed bytes are re-hashed before a resume, and any that changed on disk are fetched again;
    - remove the "partial file larger than the file" sentence (`rg -n 'larger than the file|bytes received so far'
      README.md`).
  - **Root `README.md`:** the Configuration row, and a Tuning line (a smaller checkpoint interval means less
    re-fetched after a power cut, at the cost of more fsyncs).
  - **Package `README.md` "Manifest":** download records carry the checkpoint journal (segments, confirmed offsets and
    digests); a #3-era partial restarts from byte 0 after the upgrade. Also the env var row, and the fake server line
    (bytes sent per response).

### Stage 4: Segmented downloads of large files

- **Contract:**
  - **New options:**

    | Option / flag | Env var | Default | Valid |
    |---|---|---|---|
    | `download_segments` / `--download-segments` (`download`) | `YANDEX_DISK_RESCUE_DOWNLOAD_SEGMENTS` | 4 | 1–8 (1: never split) |
    | `download_segment_threshold_mib` / `--download-segment-threshold-mib` (`download`) | `YANDEX_DISK_RESCUE_DOWNLOAD_SEGMENT_THRESHOLD_MIB` | 256 | 1–65536 |

  - **Plan.** A file with `size > threshold × 2**20` gets `k = download_segments` segments with
    `start_i = i * size // k`; any other file gets one segment.
    - The plan is fixed by `record_partial` when the partial starts.
    - A resumed partial keeps its recorded plan even when the options changed since.
    - A pure function (e.g. `segment_plan(size, *, segments, threshold) -> tuple[Segment, ...]`) owns the rule.
  - **Preallocation.** Before fetching a multi-segment file, its partial is extended to `size` off-loop, in
    `ftruncate` steps of at most 64 MiB:
    - on ext4, xfs or btrfs a step is instant (sparse);
    - on HFS+, exFAT or FAT each step writes zeros;
    - a Ctrl+C waits for at most one step.

    An `ENOSPC` here is the usual fatal destination error, before any byte is fetched. Single-segment files are not
    preallocated (Stage 3 truncates them to `confirmed`).
  - **Fetching.**
    - Each unfinished segment is one ranged `fetch` of `[confirmed, end)` (closed `Range`). All of them start at
      once, and the `ContentClient` cap queues them.
    - Each segment has its own retry-after-progress budget (`download_retries` per pass), with #3's rule: no
      immediate retry without new bytes.
    - The file's turn ends when every segment is complete or has stopped:
      - **A segment fails** (`FileFailed`): the other segments are cancelled (each checkpoints) and the file fails
        with that reason.
      - **Some segments are deferred:** the others still run to completion or deferral, then the file is deferred
        with the longest `Retry-After` among them. A later pass fetches only the unfinished segments.
  - **Links.** There is one download link per file per turn, shared by its segments.
    - A `LinkExpiredError` refreshes it once per turn for the whole file.
    - A segment that failed on a link already replaced retries with the current one without spending the refresh.
    - An expiry of the refreshed link fails the file with #3's message.
  - **Range not honoured.** A `RangeNotHonouredError` on any segment cancels the others and replaces the journal with
    one whole-file segment (`record_partial`). The partial is truncated to 0 and the file is fetched as one stream
    within the same turn, never appended to existing bytes.
  - **Verification.** Once every segment is confirmed, a multi-segment partial is read back in full off-loop and
    checked (size, MD5, SHA-256), then finished and placed as today. A mismatch discards the partial and fails the
    file, as today. Single-segment files keep the streaming digest.
  - Checkpoints and resume as in Stage 3, per segment. Preallocated bytes past `confirmed` are unconfirmed.
- **Behaviour to lock** (`tests/test_download.py`; fake server grows a way to target an `inject`ed fault at one
  `Range` header value, for example `where_headers={'range': ...}`):
  - **Segments tile the file.**
    - A 4 MiB file with `--download-segment-threshold-mib 1` is fetched as exactly four requests,
      `bytes=0-1048575`, `bytes=1048576-2097151`, `bytes=2097152-3145727` and `bytes=3145728-4194303`, with a content
      peak of 4. It verifies.
    - A file of exactly the threshold size is fetched as one request without `range` ("above" is strict).
    - `--download-segments 1` never splits.
  - **Segments under a small cap.** With `--download-connections 2`: four ranged requests, a peak of 2, and the file
    verifies.
  - **The issue's kill -9 acceptance.**
    - Setup: a 16 MiB file, `--download-segment-threshold-mib 1 --download-segments 4 --download-checkpoint-mib 1`,
      and `chunk_delay = 0.25`. The child is SIGKILLed once the journal confirms at least 1 MiB in every segment.
    - The rerun exits 0 and the file verifies.
    - Each unfinished segment is requested exactly once, from its journal `confirmed`.
    - For each segment, `served_before_kill - confirmed <= 1 MiB + 4 chunks`, with the same accounting as in Stage 3.
      The bound holds per segment even though the four segments cross their intervals at different moments: a
      checkpoint triggered by one segment records every segment's written bytes, so it can only lower the others'
      unconfirmed bytes.
  - **A corrupt segment is caught by the read-back.**
    - `corrupt(path, offset)` inside segment 3 fails the file with an MD5 mismatch, and the partial is deleted.
    - After `repair`, the rerun fetches all four segments again and verifies the file. This extends
      `test_a_corrupted_download_fails_that_file_and_a_rerun_fetches_it` with a segmented case.
  - **A tampered confirmed region is fetched again, alone.** After a SIGINT, flip one byte inside segment 2's
    confirmed region on disk. The rerun requests segment 2 from its start, the others from their `confirmed`, and the
    file verifies.
  - **One stalled segment.** Segment 3 stalls on pass 1 and is served on pass 2. Pass 2 fetches only segment 3, and
    the summary shows `Passes: 2 of 7`.
  - **One link refresh per file.** After a `Truncate` on one segment, `expire_links()` leads to exactly two link
    requests for the file, and it verifies.
  - **Range ignored.** With `ignore_range = True`, a segmented file ends as one whole-file fetch. It verifies and its
    partial never exceeds `size`.
  - **The plan survives an option change.** Start with 4 segments, SIGINT, then rerun with `--download-segments 1`:
    the rerun fetches the remaining ranges of the recorded 4-segment plan.
  - **Options:** `--download-segments 0` and `9`, and `--download-segment-threshold-mib 0` and `65537`, exit 2
    (flag and config file).
- **Deliverable:** large files download as parallel segments, resume per segment, and are verified end to end.
- **Validation:** `mise run check`; `mise run test -- yandex-disk-rescue/tests`; `mise run ci`.
- **Doc updates:**
  - **Root `README.md` "Downloading":** a paragraph on segmented files, as current state: the threshold, the count,
    one partial file, verification by reading the file back, the plan kept on resume.
  - **Root `README.md` "Configuration":** both rows.
  - **Root `README.md` "Tuning":** complete the section:
    - segments help one large file on a fast link, not a folder of small files;
    - **on HFS+, exFAT or FAT (no sparse files) a segmented file is written twice (zeros, then data) and read once
      more for verification**, so on a USB HDD prefer `download_segments = 1` or 2;
    - fewer connections on an HDD;
    - the original tool's defaults.
  - **Package `README.md`:** env var rows, and the fake server line (faults targeted by `Range`).
  - `rg -n 'segment' README.md yandex-disk-rescue/README.md` to check no sentence still says files are fetched in one
    stream.

## Manual verification (main runs this with the user; not in CI)

**The live run must stay untouched.** A real full-account `download` built from `main` is using
`~/.local/state/yandex-disk-rescue` and `/mnt/oblako`. This branch adds migration 4. Opening the live manifest with it
would upgrade the manifest in place, and the running `main` build would then refuse it ("newer than 3"). So, until
this PR has merged and the live run has finished:

- **No live state.** Every manual step runs with `XDG_STATE_HOME` set to a scratch directory holding a *copy* of the
  state directory (`cp -a ~/.local/state/yandex-disk-rescue <scratch>/yandex-disk-rescue`, taken while no run writes
  it, or a fresh empty one). It also sets `XDG_CONFIG_HOME` to a scratch copy when a config file is needed. The
  branch never opens the live manifest.
- **No HFS+ disk until the live run ends.** Steps that need `/mnt/oblako` wait for it, so the two runs do not compete
  for its I/O. Steps that need no HFS+ use a scratch destination on another disk.
- Use a real token and `uv run`.

1. **Dead files.** Rerun the trial folder that had 11 dead files into its existing `/mnt/oblako/ydr-trial`. Expect
   each pass to take about 15–20 s, not 11 × 15 s, with the healthy files done in the first pass. Note any 429s from
   the download host at 20 connections, and lower `download_connections` if they appear.
2. **Segmented file on HFS+.** Choose a folder holding one file over 1 GiB.
   - Watch the zero-fill time before the first byte, and the read-back time.
   - Kill -9 midway and rerun; expect at most about 32 MiB per segment fetched again.
   - Verify with `sha256sum` against the manifest.
3. **Throughput.** On a folder of medium files, compare `--download-connections 4`, `8` and `20` on the USB HDD.
   Record the numbers in the PR description; they feed the README's HDD guidance.

## Discovered issues

- **Fix in this PR:**
  - `ContentClient` relies on httpx's default pool. Its pool timeout equals the stall timeout, so a wait for a pooled
    connection would read as `the download stalled` under concurrency. Stage 2 sizes the pool to the cap.
  - Resume trusted a partial file's length. An unjournaled HFS+ disk can keep a garbage tail after a power cut, and
    #3 risks named exactly this. Stage 3 replaces it with the verified journal.
  - Disk I/O ran on the event loop, including a zero-fill that would block for minutes on HFS+ (Stages 1 and 4).
  - The fake server's `peak_in_flight` counts handler threads still sleeping in a `Delay` after the client gave up,
    and it mixes API and content requests. Stage 2 adds a content-only counter and keeps `Delay` out of cap tests.
- **Filed as issues:**
  - #14: files whose download never starts (0 bytes on every attempt with fresh links, the same 11 of 46 files every
    time). Parallelism stops them from costing the run time, but they still are not downloaded; that needs
    investigation on the real account.

## Risks / open questions

- **HFS+ cost of segments.** No sparse files means a segmented file is written twice and read back once: about 3× the
  disk I/O of one stream. The defaults (4 segments above 256 MiB) come from the original tool, as the issue asks. The
  README tells HDD users to lower them, and manual step 3 measures the actual effect.
- **Re-hashing on every new turn.** A large file deferred in pass after pass re-reads its confirmed bytes each time
  (minutes for a 16 GiB file on USB). That keeps "never trusted" simple. If it hurts in manual step 2, keeping the
  digests in memory across passes of one run is a contained follow-up.
- **SQLite on the loop thread.** Each file costs a few `synchronous=FULL` commits (record, checkpoints, verified) on
  the local state disk, a few ms each. Over 133k files that is tens of minutes of loop time spread through a
  multi-hour run. It is acceptable, but it is the next bottleneck after the download host.
- **Placement lock.** It serializes the check-then-record steps (rename, directory fsync, commit), about 10–40 ms each
  on an HDD, so roughly 25–100 placements per second at most. That is above the link-request rate of about 20/s
  allowed by `api_interval_ms = 50`.
- **Download-host throttling at 20 connections** is unknown. 429 and 5xx from the host are transient and defer the
  file to the next pass with `Retry-After`. Manual step 1 checks it.
- **Timing-based tests.** The dead-files and throughput tests use request-log timestamps with wide margins. If CI
  still flakes, widen the margins; do not switch to mocks.
- **Partial files from #3-era runs** (the user's trial folder) restart from byte 0 after migration 4. They are
  temporary bytes, not user data.
