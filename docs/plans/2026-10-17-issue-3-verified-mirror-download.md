# Plan: Resumable, verified mirror download

Issue: #3 (umbrella #9)
Branch: feat/verified-mirror-download (from `main` after the #2 PR, `feat/api-client-scan`, has merged)

## Context

There is no `download` command yet: `uv run yandex-disk-rescue download /tmp/x` exits 2 with
`No such command 'download'`. #2 (on `feat/api-client-scan`, merging before this starts) provides
these pieces:

- `api.DiskClient`, with `account()`, `resource(path)` and `list_folder(path)`, one `AdaptiveLimiter`
  and tenacity retries.
- The `settings` registry and the config file.
- `Manifest` (WAL, `synchronous=FULL`, run lock, `PRAGMA user_version` migrations, schema v1
  `entries(id, account_uid, path, kind, size, md5, sha256, modified)`, with `id` stable across
  rescans) and its staged scans.
- `commands/scan.py`, which holds the walk and the summary.
- `tests/fake_disk_server.py`, whose tree already stores file content.

A test run on the working tree passed 240 tests and then hung on the in-flight #2 Stage 6 test
`test_a_sub_folder_answering_after_its_parent_vanished_adds_no_rows`. That work is still being
written, so Stage 1 starts only after it has merged.

Facts observed for this plan:

- **The real target is HFS+.** `/mnt/oblako` is `hfsplus` (`df -T`; mounted
  `rw,nls=utf8,uid=1000`, without `force`, so most likely unjournaled), with 3.7 TB free. Probed
  in a scratch directory, which was removed afterwards:
  - **Case-insensitive:** writing `a.txt` overwrote `A.txt`.
  - **Normalization-insensitive:** an NFC name and its NFD form both resolve to the same file.
  - **Name limit:** HFS+ allows 255 UTF-16 units of the NFD (decomposed) name; ext4, xfs and btrfs
    allow 255 UTF-8 bytes. `'й'*128` (256 bytes, 256 NFD units) fails with `ENAMETOOLONG`, and
    `'й'*127+'a'` (255/255) works. The two limits differ: `'ΐ'*127` is 254 UTF-8 bytes but 381 NFD
    UTF-16 units (computed; not probed, since the disk is off limits now), so it fits ext4 and not
    HFS+.
  - **Ignorable code points:** Linux hfsplus also ignores default-ignorable code points (U+200C–U+200F,
    U+202A–U+202E, U+206A–U+206F, U+FEFF) when it compares names, so two names that differ only by
    one of them are the same file (critic finding; from the hfsplus case-folding tables).
  - **Accepted:** `: ? * < > | " \`, control characters, trailing dots and spaces, and non-UTF-8
    bytes.
  - **Supported operations:** `rename` over a file, directory fsync, hard links and `posix_fallocate`.
    `renameat2(RENAME_NOREPLACE)` works, and it refuses a case alias with `EEXIST`.
  - **No sparse files:** a write at offset 50 MiB allocated all 50 MiB.
- **The target already holds data.** `/mnt/oblako` already has `Загрузки/` and `Скриншоты/` (566
  files; the default names of the Yandex Downloads and Screenshots folders) and the macOS
  `.Spotlight-V100`, `.Trashes`, `.fseventsd` and `.DS_Store`. A whole-account mirror into
  `/mnt/oblako` meets local files the tool did not write.
- **Hashing is cheap.** MD5 runs at 880 MB/s and SHA-256 at 1935 MB/s (SHA-NI) on this machine,
  far above any download rate. The real account has both checksums on every one of its 133,696
  files.
- **The Yandex docs** (<https://yandex.com/dev/disk-api/doc/en/reference/content>):
  - `GET /v1/disk/resources/download?path=` returns `{href, method, templated}`.
  - The link has "a limited lifetime", with no duration given.
  - Download "using the same OAuth token".
  - The docs say nothing about redirects or `Range`.
  - Third-party clients (ya-disk) fetch `href` with a plain GET, no token, following a 302 to
    `*.storage.yandex.net`.
  - **Not verified offline:** Range support, the link lifetime, the expiry status
    (401/403/410), and whether the token is needed. Manual step 1 checks them.
- **Typer argument order.** An optional positional before a required one does not parse:
  `download [REMOTE] DEST` with one argument reports `Missing argument 'dest'`. One variadic
  argument with metavar `[REMOTE_PATH] DEST`, validated to 1–2 values, parses and shows the right
  usage line (checked in a scratch script).

## Goal

`yandex-disk-rescue download [REMOTE_PATH] DEST` (REMOTE_PATH defaults to `disk:/`) works as follows:

1. It scans REMOTE_PATH into the manifest, the same way `scan` does, unless the last completed scan
   covering it is younger than `scan_max_age` minutes. `--no-scan` uses the manifest as it is.
2. It mirrors that subtree into DEST, one file at a time, keeping the folder structure and creating
   empty folders.
3. Every file at its final path is verified: its size and every checksum the cloud provides.
4. The run state lives in the manifest.
5. A rerun skips verified files, finishes partial files with HTTP `Range` after checking the cloud
   file is unchanged, and fetches missing or changed ones.
6. Expired links are refreshed once.
7. Nothing the user has is ever deleted or overwritten. A local copy that is in the way is moved to
   `DEST/.yandex-disk-rescue/replaced/`.
8. The summary gives downloaded, skipped, failed and size-only counts, and lists failed paths. The
   exit code is 1 when any file failed.

The internal seams are a per-file pipeline, a byte-range transfer, a digest accumulator and
per-entry download rows. They let #4 add concurrency, segments and checkpoints without a rewrite.

## Decisions that go beyond the issue's wording (all approved by the user, 2026-10-17)

1. **Verification checks every checksum the cloud provides.** The order is size, then MD5 when
   present, then SHA-256 when present. The issue says "SHA-256 if MD5 is missing"; this plan's
   rule is strictly stronger. Evidence: both checksums are present on 100% of the real files, and
   hashing both in one pass costs well under 1% of transfer time (880/1935 MB/s against network
   speed). A file with neither checksum is "size only". The verification method is recorded per
   file: `md5+sha256`, `md5`, `sha256` or `size`.
2. **Rescan policy: a freshness window plus `--no-scan`.** The issue says `download` "scans first
   if no current manifest exists". The user's rule:
   - **Window.** `download` rescans unless the last *completed* scan of that account covering REMOTE_PATH
     (a scan of REMOTE_PATH itself or of an ancestor) finished less than `scan_max_age` minutes ago.
     The default is 60 and the range 0–10080; 0 means always rescan.
   - **`--no-scan`.** Uses the manifest as it is, whatever its age. If REMOTE_PATH was never
     scanned, it exits 2 with a hint.
   - **Summary.** It names the scan used.
   - **`scan`** always scans.
   Inside the window, cloud changes since that scan are not seen. Resume still checks the live
   metadata (Stage 7), so a stale manifest can never make a wrong file look verified.
3. **The "replaced" move ships in #3, not #5.** #9 says "never delete user data … replaced copies
   are moved", and "fetch changed files" needs somewhere to put the old copy. #5 keeps quarantine,
   `verify` and `--retry-failed`, and its first deliverable is met here. Main should trim #5's body.
4. **A partial file that fails verification is deleted, and the file is reported as failed.** These
   are temporary bytes under `.yandex-disk-rescue/partial/`, never at a final path. The next run
   downloads it again. #5 turns this into quarantine.
5. **Default local name mapping** (the open question in #9). Recommendation:
   - **Exact names.** Names are stored exactly as the API gives them: no Unicode normalization and
     no character substitution. Linux stores every character a Disk name can hold, including on
     the user's HFS+ disk.
   - **Long names.** A name component is too long when its UTF-8 encoding exceeds 255 bytes *or* its
     NFD form exceeds 255 UTF-16 units. That is a fixed rule, with no probing, and it covers ext4 and
     HFS+. A long name is shortened: the extension is kept, the stem is cut at a character boundary,
     and `~<8 hex of sha256(name)>` is added.
   - **Colliding siblings.** Siblings that collide under canonical caseless matching with the
     HFS+-ignored code points removed (`NFD(casefold(NFD(strip_ignorable(name))))`) keep the name
     for one of them. That is the one already holding it
     in the manifest, otherwise the first in code-point order. The others get the `~<hash>` suffix.
     The name `.yandex-disk-rescue` at the mirror root counts as taken.
   - **Why a fixed key.** The collision key is the same on every filesystem, so a mirror can be
     copied to a case-insensitive disk (HFS+, APFS, exFAT, NTFS) without loss, and the layout
     does not depend on probing the target. On ext4 this renames siblings that differ only in case;
     they are listed in the summary.
   - **Names the target still rejects** (`EINVAL`, `EILSEQ` or `ENAMETOOLONG` when creating the
     name, for example `?` on exFAT/NTFS) are converted, not failed. The *rejected-name conversion*:
     - each of `\ : * ? " < > |`, U+0000–U+001F and U+007F becomes `_`;
     - a trailing run of dots and spaces becomes `_` characters;
     - the long-name rule is applied;
     - the `~<8 hex of sha256(original name)>` suffix is always added, so a converted name is
       recognisable and cannot collide with a real sibling. The collision rule extends the hex if
       needed.
     The converted name is recorded in the manifest, so later runs use it at once without
     re-triggering the error, and it is listed in the summary's renamed section. If the converted
     name is also refused, the file (or the folder and everything under it) fails with the OS error.
     After this, #11 shrinks to an opt-in *proactive* mode: the same conversion applied to every
     name up front, for a mirror on ext4 or HFS+ that must later be copied to exFAT or NTFS.

## Non-goals

- Concurrent or segmented transfers, periodic fsync checkpoints, and the download connection cap
  (#4).
- Quarantine, `verify`, `--retry-failed` (#5).
- Progress output, the log file and `--json` (#6). `download` prints a start line and the final
  summary only.
- Deleting local files that were removed from the cloud (mirror pruning).
- Downloading a single file as REMOTE_PATH. REMOTE_PATH must be a folder, as for `scan`.
- Copying macOS/Windows metadata, permissions or xattrs. File mtimes are set to the cloud
  `modified`; folder mtimes are not set.

## Exit-code contract (extends #2's table)

| Code | `download` meaning |
|---|---|
| 0 | Every file in the subtree is verified at its final path, and no file failed in this run |
| 1 | `RescueError`: every #2 code-1 case, plus one or more files failed (the summary is printed first, then `Error: N files failed; see the list above.`), and a fatal destination error (`Error: Could not write <path>: <strerror>.` for `ENOSPC`, `EIO`, `EROFS`, `EACCES`, or a filesystem without `RENAME_NOREPLACE`) |
| 2 | Usage: 0 or more than 2 positional paths, an invalid option, `--no-scan` for a REMOTE_PATH no completed scan covers, a DEST that exists but is not a directory, a DEST whose parent does not exist, a DEST starting with `disk:` (a swapped argument order), a DEST already mirroring another remote folder or account |
| 130 | Ctrl+C. Stderr shows `Interrupted; rerun to continue the download.`, and partial files and records are kept |

What counts as a per-file failure, rather than aborting the run:

- The link request returns 403, 404 or another non-throttling 4xx (not 401). The message is the
  API description; a 404 reads `removed from the Disk after the scan`.
- The download host still fails after `download_retries`.
- An expired link fails again after one refresh.
- Verification fails, or the cloud file changed during the run.
- The OS refuses the file's name *even after* the rejected-name conversion (Stage 9), or the full
  path exceeds `PATH_MAX`.

Three cases abort the run (exit 1):

- an API 401 (`TokenRejectedError`);
- an API `DiskApiError` after the API retries, which means the API itself is failing (an outage
  longer than the retries ends the run, and a rerun resumes);
- another run already holding the destination lock (`Another yandex-disk-rescue run is using <DEST>`).

## Stages

### Stage 1: The scan as a reusable step

- **Contract:**
  - A new non-command module `yandex_disk_rescue/scanning.py`. Its public API is
    `async def scan_tree(client: api.DiskClient, manifest: Manifest, account: api.Account, root: ScanRoot) -> ScanResult`, with
    `async def resolve_scan_root(client, root: str) -> ScanRoot` run *before* `Manifest.open`, so an
    invalid root never creates a manifest (as built in Stage 1, 86f746c; worker count from
    `DiskClient.limits`)
    and `def scan_summary(result: ScanResult) -> list[str]`.
    - `scan_tree` covers the #2 walk, including Stage 6's vanished folders, and the commit.
    - `scan_summary` returns every line of today's output except `Manifest: <path>`.
    - `ScanResult` is a public frozen dataclass: `root` (the API's spelling), the totals and
      vanished folders, and `diff`.
  - `commands/scan.py` keeps only the Typer command: token, client, account, `Manifest.open`,
    `scan_tree`, printing, and the Ctrl+C message.
  - A command module never imports another command module.
  - `rg '_Walk|_scan\(|_summary\(' yandex-disk-rescue/src` finds them only inside `scanning.py`.
- **Behaviour to lock:** `tests/test_scan.py` passes unchanged and is the regression lock. Two kinds
  of edit are permitted:
  - `from ... import` lines, where a test imports a moved private name;
  - the one `monkeypatch.setattr(scan_command, '_STAGE_BATCH', ...)` (around `test_scan.py:309`),
    retargeted to the new `scanning` module.

  `commands/scan.py` must not re-export `_STAGE_BATCH` or any other moved name to keep the old
  target working.
- **Deliverable:** the scan engine, callable by any command, with `scan` output byte-identical.
- **Validation:** `mise run check`; `mise run test -- yandex-disk-rescue/tests/test_scan.py`.
- **Doc updates:** package `README.md` Layout tree. Rewrite it as current state: `scanning.py` is
  the walk, commit and summary lines; `commands/scan.py` is the `scan` command.

### Stage 2: Manifest schema v2 (mirrors, local names, download records)

- **Contract:**
  - Append migration 2. Do not edit migration 1. It adds:
    - `mirrors(id, account_uid, remote_root, destination UNIQUE)`, where `destination` is the
      resolved absolute DEST.
    - `local_names(mirror_id → mirrors ON DELETE CASCADE, entry_id → entries(id) ON DELETE CASCADE,
      local_path, PRIMARY KEY(mirror_id, entry_id), UNIQUE(mirror_id, local_path))`. `local_path`
      is relative to DEST, '/'-separated, for files and folders.
    - `downloads(mirror_id, entry_id, PRIMARY KEY (mirror_id, entry_id), state CHECK IN
      ('partial','verified'), size, md5, sha256, modified, verified_by CHECK IN
      ('md5+sha256','md5','sha256','size') NULL, local_size NULL, local_mtime_ns NULL,
      local_ino NULL)`, with both keys cascading the same way.
      - The `local_*` columns are the `lstat` fingerprint of the placed file.
      - `local_ino` identifies the file within the mirror's one DEST. With it, a path that resolves to
        another entry's verified file on a case- or ignorable-insensitive filesystem is recognised.
      - No device number is stored. `st_dev` of a removable disk changes on replug, reboot or
        re-enumeration, while the HFS+ inode (the catalog node ID) and ext4/xfs/btrfs inodes
        survive remounts.
    - The identity columns hold the remote identity the row refers to, which is the
      `entries` row as it was when the partial was started or the file was verified.
    - #4 hangs a segments table off `downloads`; #5 adds failure and quarantine state.
    - `scans(account_uid, root, completed_at, PRIMARY KEY (account_uid, root))` records the
      completion time of the last committed scan of each root. `completed_at` is UTC ISO 8601.
      `ScanSession.commit` upserts it in the same transaction as the entries, so the time exists if
      and only if that scan committed. A manifest upgraded from v1 has no rows (v1 never stored scan
      roots), so the first `download` after the upgrade scans.
  - Public API on `Manifest`. The names are the contract; the internals are the implementer's:
    ```python
    def mirror(self, account_uid: str, remote_root: str, destination: Path) -> Mirror: ...


    # Creates the row or returns the existing one. A destination recorded with another
    # account_uid or remote_root raises MirrorMismatchError (RescueError, exit_code 2):
    # "<dest> already mirrors <root> of another account" / "... mirrors disk:/A; download disk:/A there or choose another destination".


    class Mirror:
        id: int
        destination: Path
        remote_root: str

        def entries(self) -> list[MirrorEntry]: ...  # committed entries of the subtree, path order, with their records
        def assign_names(self, names: Mapping[int, PurePosixPath]) -> None: ...  # one transaction
        def record_partial(self, entry_id: int, identity: RemoteIdentity) -> None: ...
        def record_verified(
            self, entry_id: int, identity: RemoteIdentity, verified_by: str, local: os.stat_result
        ) -> None: ...  # stores size, mtime_ns, ino
        def verified_owner(
            self, ino: int
        ) -> MirrorEntry | None: ...  # the entry of this mirror whose verified copy is that inode
        def forget(self, entry_id: int) -> None: ...  # drop the download row
        def rename_local(self, entry_id: int, name: str) -> None: ...

        # Replace the last component of the entry's local_path and rewrite the local paths of all
        # its descendants in one transaction (used by the rejected-name conversion, Stage 9).


    # On Manifest:
    def last_scan(self, account_uid: str, root: str) -> ScanTime | None: ...


    # The newest completed scan of `root` or of any ancestor of it, as (root, completed_at).


    @dataclass(frozen=True)
    class RemoteIdentity:
        size: int
        md5: str | None
        sha256: str | None
        modified: datetime


    #   RemoteIdentity.of(resource) builds one from a file Resource.


    @dataclass(frozen=True)
    class MirrorEntry:
        entry_id: int
        resource: Resource
        local_path: PurePosixPath | None
        download: DownloadRecord | None
    ```
  - Invariants:
    - A rescan (`ScanSession.commit`) never touches `local_names` or `downloads`, except through the
      cascade when an entry is removed.
    - A record whose identity differs from the entry's current identity is *stale*. Callers compare;
      the manifest never rewrites records on a scan.
    - All writes go through the existing `_transaction` and raise `ManifestError` on SQLite errors.
    - `entries()` returns a fully read list, never a lazy cursor. A read cursor left open across the
      per-file write transactions pins the WAL: the critic measured 162 MiB of WAL per 20k files
      with a lazy cursor, against 3.9 MiB. Main keeps its 133k-entry list in memory (tens of MB),
      which is acceptable; #4 may page it if needed.
- **Behaviour to lock:** extend `tests/test_manifest.py`, with real SQLite in `tmp_path`:
  - **Upgrade from v1.** A text fixture `tests/data/manifest-v1.sql` holds a `sqlite3 .dump` of a v1
    manifest with a few entries, made with the code before this stage. Loaded with
    `executescript`, then opened, it ends at `user_version == 2`, with entries intact and no
    mirrors.
  - **Mirror identity.** `mirror()` twice returns the same id. Another `remote_root`, or another
    account for the same destination, raises `MirrorMismatchError` with exit code 2.
  - **Records survive a rescan.** Names and records survive a rescan that changes nothing. A rescan
    that changes a file's md5 keeps the record, whose identity now differs from
    `MirrorEntry.resource`. A rescan that removes the file drops its name and record (cascade).
  - **Unique local paths.** `assign_names` with a duplicate `local_path` raises (UNIQUE), and none of
    that batch is written.
  - **Scan times.**
    - After committing a scan of `disk:/`, `last_scan(uid, 'disk:/A/B')` returns the `disk:/`
      scan time.
    - After a later scan of `disk:/A`, it returns the `disk:/A` time.
    - An abandoned session records no time.
    - Another account gets `None`.
    - The v1 fixture upgrades with no scan times.
  - **Folder rename.** `rename_local` on a folder rewrites its descendants' local paths, and a
    duplicate raises without writing anything.
  - **Fingerprint.** `record_verified` with a real `os.stat` of a `tmp_path` file round-trips size,
    mtime_ns and ino, and `verified_owner(ino)` returns that entry (and `None` for the same inode
    number in another mirror).
- **Deliverable:** schema v2 and the mirror API. No CLI change.
- **Validation:** `mise run check`; `mise run test -- yandex-disk-rescue/tests/test_manifest.py`.
- **Doc updates:** package `README.md` "Manifest" section. Rewrite it as current state: it also
  records, per destination ("mirror"), the local name of every entry and each file's download
  state, keyed by the remote identity it refers to. A destination mirrors exactly one remote
  folder of one account.

### Stage 3: Local name mapping

- **Contract:** new module `yandex_disk_rescue/local_names.py`:
  ```python
  MAX_NAME_BYTES = 255  # UTF-8 bytes (ext4, xfs, btrfs)
  MAX_NAME_UTF16_UNITS = 255  # UTF-16 units of the NFD form (HFS+)
  WORK_DIR_NAME = '.yandex-disk-rescue'


  def collision_key(name: str) -> str: ...  # NFD(casefold(NFD(name))) with HFS+-ignored code points removed
  def map_local_names(
      remote_root: str, entries: Iterable[Resource], assigned: Mapping[str, PurePosixPath]
  ) -> dict[str, PurePosixPath]: ...
  ```
  - It returns the local path (relative to DEST) of every entry below `remote_root`. The root itself
    is not included.
  - **Deterministic:** the result depends only on the inputs.
  - **Stable:** an entry in `assigned` keeps its path as long as its parent's path is unchanged and
    no other sibling already holds that name under `collision_key`.
  - **Unique:** no two siblings share a `collision_key`, and nothing at the root maps to
    `collision_key(WORK_DIR_NAME)`.
  - **Short enough:** every component is at most `MAX_NAME_BYTES` UTF-8 bytes *and* at most
    `MAX_NAME_UTF16_UNITS` UTF-16 units in NFD.
  - **Ignorable code points:** `collision_key` removes U+200C–U+200F, U+202A–U+202E, U+206A–U+206F
    and U+FEFF, the set Linux hfsplus ignores. The stored local name keeps them, since names stay
    exact.
  - **Otherwise exact:** names are unchanged except under the long-name and collision rules
    (Decision 5). The suffix is `~` plus the first 8 hex digits of `sha256(name.encode())`. It goes
    before the last extension when that extension is 1–16 bytes and the name is not only that
    extension; otherwise it goes at the end. If the suffixed name still collides, 16 hex digits are
    used, then all 64.
  - A renamed folder's descendants live under the folder's local path.
  - `def rejected_name_conversion(name: str, taken: Collection[str]) -> str` applies the Decision 5
    rejected-name conversion. `taken` holds the siblings' collision keys, and the result is unique
    among them. It is deterministic, and the output never contains the replaced characters.
- **Behaviour to lock:** new `tests/test_local_names.py`:
  - A table over plain names: Cyrillic, spaces, `:?*`, NFD input left as NFD, a 300-byte Cyrillic
    name with an extension, `'ΐ'*127` (254 bytes, 381 NFD units, so it is shortened),
    `A.txt`/`a.txt`, NFC `й`/NFD `й` siblings, `a.txt` next to `a‍.txt`, a root entry named
    `.Yandex-Disk-Rescue`, and a renamed folder with children.
  - Stability: adding `PHOTO.jpg` next to an assigned `photo.jpg` renames the newcomer, not the
    assigned one.
  - Every mapped path can be created under `tmp_path` and reads back with the same names.
  - Table for `rejected_name_conversion`:
    - `a?b.txt` becomes `a_b~<hex>.txt`;
    - `report. ` becomes `report__~<hex>`;
    - a control character becomes `_`;
    - a name already containing only safe characters still gets the suffix;
    - a `taken` collision extends the hex.
- **Deliverable:** the pure mapping function.
- **Validation:** `mise run check`; `mise run test -- yandex-disk-rescue/tests/test_local_names.py`.
- **Doc updates:** none yet. Stage 5 documents the mapping together with the command.

### Stage 4: Download links, byte-range transfer, and the fake download host

- **Contract:**
  - `DiskClient.download_link(path: str) -> str`. It sends `GET /v1/disk/resources/download` with
    `path` and `fields=href,method,templated`, through the limiter and retries.
    - 401 gives `TokenRejectedError` (#2 text).
    - 404 gives `RemotePathNotFoundError`.
    - Any other non-throttling 4xx (403 included) gives the new
      `DownloadRefusedError(RescueError)`, with `Could not download <path>: Yandex Disk API returned
      HTTP <n>: <description>`. On this endpoint 403 is per file, not a token problem: the token was
      already accepted by `account()`.
    - `templated: true`, `method != 'GET'` or a missing `href` gives the #2 unexpected-response
      `DiskApiError`.
  - New module `yandex_disk_rescue/api/transfer.py`, which owns no file I/O:
    ```python
    class ContentClient:  # async context manager; own httpx.AsyncClient
        def __init__(self, *, timeout: float): ...
        async def fetch(
            self, href: str, *, start: int, end: int, size: int, write: Callable[[int, bytes], None]
        ) -> None: ...
    ```
    - **No token.** It never sends `Authorization`; the href is a signed link to another host.
    - **Redirects** are followed.
    - **Range.** `fetch` asks for bytes `[start, end)`. With `start > 0` it sends
      `Range: bytes=<start>-<end-1>` and requires `206` with `Content-Range: bytes <start>-<end-1>/<total>`.
      With `start == 0` and `end == size` it sends no Range, and `200` or a matching `206`
      are both fine. `size` is the whole file's size, so a segment `[0, k)` is still ranged and the
      `Content-Range` total must equal it. All errors subclass `TransferError` (`.reason`).
    - **Write callback.** `write(offset, chunk)` is called with absolute file offsets, in order.
      That lets #4 run several fetches into one file.
    - **Errors**, all internal to the transfer layer and not `RescueError`. Each carries a
      user-facing reason that never includes the href's query string or a header:
      - `LinkExpiredError`: 401, 403 or 410 from the download host.
      - `TransientTransferError`: a transport error or timeout, 408, 429 or 5xx, with `Retry-After`
        when present. The bytes written so far stay valid.
      - `RangeNotHonouredError`: 200 for a ranged request, or a wrong `Content-Range`.
      - `TransferRefusedError`: any other status, 404 included.
      - `TooManyBytesError`: the body runs past `end`.
  - Fake server growth (`tests/fake_disk_server.py`):
    - **Link endpoint.** A built-in `GET /v1/disk/resources/download` serves
      `{'href': f'{base_url}/download?path=<path>&link=<n>', 'method': 'GET', 'templated': False}`
      for files in the tree, where `<n>` is a counter per issued link. It gives 404 for missing
      paths and 400 for a folder.
      - The route is the fixed path `/download`, so the existing exact-path
        `inject('GET', '/download', ..., where={'path': 'disk:/a.bin'})` targets one file, with no
        prefix matching. `link` lets `expire_links` tell old links from new ones.
    - **Download host.** A built-in `GET /download` serves the node's content, honouring a single
      `bytes=a-b` or `bytes=a-` range with 206 and `Content-Range`. A malformed or unsatisfiable
      range gives 416.
      - `/download` and `/storage` skip the `token` check. They answer
        `400 {"error": "AuthorizationNotExpected"}` to any request that carries an `Authorization`
        header, which locks the no-token rule from the server side too.
    - **Fault controls:**
      - `corrupt(path, offset)` flips one served byte; the metadata keeps the true checksums.
      - `expire_links(status=410)` makes every href issued so far answer `status`.
      - `ignore_range = True` answers 200 with the full body.
      - `redirect_downloads = True` makes `/download` answer 302 to `/storage` (same query), which
        serves the bytes.
      - `Truncate(after_bytes)` is a new `inject` response for `/download`: it sends headers for the
        full body and closes after `after_bytes`.
      - `chunk_delay` (seconds) is a pause between 64 KiB chunks, for tests that kill a client
        mid-file.
    - The request log records `range` and `authorization` headers as today, lower-cased.
- **Behaviour to lock:**
  - Extend `tests/test_api_client.py`: `download_link` returns the href; a missing path gives
    `RemotePathNotFoundError`; 403 gives `DownloadRefusedError` with the description;
    `templated: true` gives `DiskApiError`.
  - New `tests/test_transfer.py`, async against the fake:
    - A full fetch returns the exact bytes. The content request carries no `authorization` header,
      even though `DiskClient` used a token, and the fake server's 400 for that header is never
      seen.
    - `start=N` sends `Range: bytes=N-…` and writes from offset N.
    - Every fault above raises its own error. `Truncate` raises `TransientTransferError` after
      `after_bytes` were written.
    - A redirect is followed.
- **Deliverable:** link requests, the content client and a fake download host.
- **Validation:** `mise run check`;
  `mise run test -- yandex-disk-rescue/tests/test_transfer.py yandex-disk-rescue/tests/test_api_client.py`.
- **Doc updates:**
  - Package `README.md`: rewrite the Layout tree (`api/transfer.py`) and the
    `fake_disk_server.py` line as current state (the download endpoint, the download host and its
    faults).

### Stage 5: `download` command — fresh mirror, verification, skip on rerun, summary

- **Contract:**
  - `yandex_disk_rescue/commands/download.py`, registered in `cli.py` with one line.
    - **Arguments:** one variadic argument with metavar `[REMOTE_PATH] DEST`. One value is DEST
      with REMOTE_PATH `disk:/`; two are REMOTE_PATH then DEST; any other count is a usage error
      (exit 2).
    - **Options:** the `api_*` options, plus one new registry entry (and so a config key and an env
      var). Stage 7 adds `download_retries`.

      | Option / flag | Env var | Default | Valid |
      |---|---|---|---|
      | `download_timeout` / `--download-timeout` (s) | `YANDEX_DISK_RESCUE_DOWNLOAD_TIMEOUT` | 60 | 1–600 |

      In this stage, a transfer error on a file fails that file at once. Stage 7 adds retries.
  - Flow:
    1. **Prepare.** Normalize REMOTE_PATH. A DEST starting with `disk:` exits 2 (the arguments are
       probably swapped). Get the token, the client and the account.
    2. **Resolve DEST.**
       - If DEST exists and is a directory, use it.
       - If it is missing, create only its last component, so a mistyped mount point fails rather
         than filling the system disk. A missing parent exits 2.
       - If it exists and is not a directory, exit 2.
       - Resolve it once.
    3. **Destination lock.** Create `DEST/.yandex-disk-rescue/` and take an exclusive
       non-blocking `flock` on `DEST/.yandex-disk-rescue/lock` for the whole run. If it is held,
       exit 1 with `Another yandex-disk-rescue run is using <DEST>; wait for it to finish and try
       again.` This guards against two runs with different manifests (different `XDG_STATE_HOME`)
       deleting each other's partials, which the manifest lock cannot see.
    4. **Scan and map.** `resolve_scan_root` first, then open the manifest (its path moves from
       `commands/scan.py`'s `MANIFEST_FILE_NAME` to a shared `paths.manifest_path()`; commands never
       import each other) and run `scan_tree`, printing the `scan_summary` lines.
       Then call `mirror()`, run `map_local_names` against the stored names, then `assign_names`.
    5. **Work directory.** Create `DEST/.yandex-disk-rescue/partial/`. Delete files there that have
       no `partial` record (crash leftovers and files removed from the cloud). Print
       `Downloading N files (<size>) to <DEST>` for the files not already verified.
    6. **Walk the entries in path order** (the list from `Mirror.entries()`):
       - A folder becomes a directory.
       - A file whose record is `verified`, whose identity equals the entry's, and whose `lstat` at
         `DEST/local_path` matches the recorded size, mtime_ns and ino, is *skipped*. It is not
         hashed again; re-hashing is #5's `verify`.
       - A file with a current verified record whose `lstat` differs (edited, replaced or touched
         locally) is treated exactly like a file with no current record. In this stage that is the
         "already exists" rule below; Stage 6 hashes it, then adopts it or moves it aside.
       - Any other file goes through the per-file pipeline.
    7. **Per-file pipeline**, as one awaitable over (entry, clients, mirror) with no shared mutable
       state besides them, so #4 can run several:
       1. `record_partial(identity)`.
       2. Get the link.
       3. `fetch` into `partial/<entry_id>` (`O_CREAT`, `pwrite` at offsets), computing MD5 and
          SHA-256 as the bytes are written.
       4. fsync.
       5. Check the size, then every checksum present.
       6. Set the mtime to the cloud `modified`.
       7. Place the file at `DEST/local_path` with `renameat2(RENAME_NOREPLACE)` (through ctypes;
          the stdlib has no flag). It never replaces a path. At run start, a probe rename with the
          flag inside `.yandex-disk-rescue/` checks it is supported, and `EINVAL` there is a fatal
          destination error. Later `EINVAL`, `EILSEQ` or `ENAMETOOLONG` from `mkdir` or the
          placement rename means the name was rejected; Stage 9 converts it, and until then the file
          fails with the OS error.
       8. fsync the parent directory.
       9. `record_verified(verified_by, lstat of the placed file)`.
    8. **Failures.** A verification failure deletes the partial, calls `forget`, and fails the file
       with `MD5 mismatch (expected …, got …)`, `SHA-256 mismatch …` or
       `size mismatch (expected N bytes, got M)`.
    9. **Something already at the final path.** In this stage, anything at the final path other
       than the recorded verified copy fails the file with
       `<local path> already exists and was not written by this mirror; it was left untouched`.
       Stage 6 replaces this rule.
       - **Alias rule** (permanent, not replaced by Stage 6). If the `lstat` at the final path has the
         inode of *another* entry's verified copy (`verified_owner`), the file fails with
         `<local path> is the same file as <other remote path> on this filesystem; it was left untouched`.
         That file is never adopted, moved aside or overwritten. Before declaring an alias, confirm
         that the owner's own `local_path` still `lstat`s to that inode; a stale record (owner
         deleted locally, inode reused) is not an alias.
    10. **Directories in the way.** A parent directory that exists as something other than a real
       directory (a file or a symlink) fails every file under it with the same
       "left untouched" reason. The tool never writes through a symlink inside DEST; Stage 6
       replaces this rule too.
  - Summary on stdout, after the scan lines. The elements below are the contract; the wording is
    the implementer's. Each list is capped at 20 entries followed by `… and N more`, as in `scan`.
    ```
    Mirrored disk:/Photos to /mnt/oblako/Photos: 120 downloaded (1.2 GiB), 1000 skipped, 2 failed
    Verified by size only (no cloud checksum): 3
      disk:/Photos/old.bmp
    Renamed for the destination: 1
      disk:/Photos/A.txt -> Photos/A~1a2b3c4d.txt
    Failed: 2
      disk:/Photos/bad.jpg: MD5 mismatch (expected …, got …)
    Manifest: /home/u/.local/state/yandex-disk-rescue/manifest.sqlite3
    ```
    - "Size only" counts every size-only file now verified in the mirror, whether it was downloaded
      in this run or skipped.
    - "Renamed" lists every mapped name that differs from the remote name.
  - Any failure exits 1 after the summary (`Error: N files failed; see the list above.`).
  - Fatal errors (exit-code table) abort with no summary.
  - Ctrl+C is handled as for `scan`, with the `download` message.
- **Behaviour to lock:** new `tests/test_download.py`, using `CliRunner`, `fake_disk_api` and a
  `tmp_path` DEST:
  - **Fresh mirror.** A nested tree with an empty folder, a size-only file, a zero-byte file and a
    Cyrillic name with spaces exits 0. Every file's bytes and mtime match the fake, and the empty
    folder exists. The summary counts are right, and the size-only path is listed.
    `DEST/.yandex-disk-rescue/partial/` is empty.
  - **Rerun.** A second run exits 0 with `0 downloaded`. The fake's log has no
    `/v1/disk/resources/download` and no `/download` requests (issue acceptance: no transfers).
    - **Nothing is hashed.** Before the rerun, every mirrored file is overwritten in place with
      different bytes of the same size, and its mtime is restored with `os.utime(ns=...)`. Size,
      mtime_ns and inode are unchanged, so only a hash could notice. The rerun still exits 0 with
      `0 downloaded`, no `/download` requests and an empty `replaced/`. Any hashing would show a
      mismatch and trigger a download or move-aside. (`chmod 000` was rejected: CI runs pytest
      as root, which reads mode-000 files, so that lock would pass regardless.)
    - **Replug.** A disk replug changes the device number but not the size, mtime or inode, so the
      skip decision cannot depend on `st_dev`. The schema has no device column, and this rerun
      test locks the rule.
  - **Corruption.** `corrupt()` on one file exits 1. That path is listed with `MD5 mismatch`, no
    file exists at its final path, and its partial is gone. A rerun without the fault downloads it
    and exits 0.
  - **Size and SHA-256.** A file whose metadata has `md5=None` and a wrong `sha256` fails with
    `SHA-256 mismatch`. A size-only file with corrupted bytes still passes, which shows the
    size-only limit and is the documented behaviour; a truncated size-only file fails with
    `size mismatch`.
  - **Name mapping end to end.** Siblings `A.txt` and `a.txt` and a 300-byte name both land, and
    the summary lists the renames. A rerun keeps the same local names and transfers nothing.
  - **In the way.** An untracked local file at a final path is left byte-identical, and that path
    is reported as failed.
  - **Errors.**
    - `download` with no arguments exits 2, and so does `download a b c`.
    - A DEST that is a regular file exits 2. So does a DEST under a missing parent, which leaves
      nothing created, and `download /tmp/x disk:/A`.
    - A second run of `download disk:/Other <same DEST>` exits 2 with the mismatch message.
    - `--download-timeout 0` exits 2.
    - **DEST lock.** A subprocess holds `flock` on `DEST/.yandex-disk-rescue/lock`, and `download`
      into that DEST with a different `XDG_STATE_HOME` exits 1 with the "Another … run is using
      <DEST>" message. A partial planted in `partial/` is still there afterwards.
    - **Alias.** After a mirror of `a.bin` and `b.bin`, `b.bin` is replaced by a hard link to
      `a.bin`, which simulates an alias on ext4/tmpfs. The fake is then given new content for
      `b.bin` (`add_file`), so its record is stale and the run must place a new copy. The run fails `b.bin` with the "same file as
      disk:/…/a.bin" message, `a.bin` is untouched, and `replaced/` is empty. A second run does the
      same: stable, with no growth.
    - A link request answered 403 on one file fails that file only.
    - A 401 on the link request aborts with #1's token text.
  - Extend `tests/test_cli.py`: root `--help` lists `download`.
  - Extend `tests/test_config.py`: `download_timeout` read from the config file reaches the command
    (an invalid value exits 2 naming the key).
- **Deliverable:** a working sequential, verified mirror.
- **Validation:** `mise run check`;
  `mise run test -- yandex-disk-rescue/tests/test_download.py yandex-disk-rescue/tests/test_cli.py yandex-disk-rescue/tests/test_config.py`;
  `uv run yandex-disk-rescue download --help`.
- **Doc updates:**
  - Root `README.md`, rewritten as current state:
    - The Status line: `auth`, `scan` and `download` work; speed and safety features are being
      built.
    - A new "Downloading" section:
      - usage, and the scan step (always, until Stage 8 adds the freshness window);
      - what "verified" means, including size only;
      - the summary, and the `.yandex-disk-rescue/` work directory;
      - "Local file names", with the mapping rules and the HFS+/APFS case note;
      - a file in the way is left untouched and reported (Stage 6 rewrites this sentence);
      - DEST rules: only the last component is created, and one run per destination (the lock);
      - an API outage longer than the retries aborts the run, and a rerun picks up where it
        stopped.
    - The "Exit codes" table rows for code 1, 2 and 130, as above.
    - The "Configuration" table row for `download_timeout`.
  - Package `README.md`: the Layout tree (`commands/download.py`, `local_names.py`, and the
    downloader module the implementer chooses), the usage block, and the env var row.

### Stage 6: Existing local content — adopt identical files, move others aside

- **Contract:** this stage replaces Stage 5's "left untouched" rule. Rewrite that rule, its tests
  and its README sentence; do not keep it alongside.
  - **Existing file, no current verified record.** A regular file at the final path with no
    current verified record is hashed. The case covers an untracked file, a crash between rename
    and record, a manual copy, a record for an older identity, and a verified record whose
    `lstat` fingerprint no longer matches. The check comes after Stage 5's alias rule, which still
    applies first: an inode that is another entry's verified copy is never hashed, adopted or
    moved.
    - If its size and cloud checksums match, it is *adopted*: no download; its mtime is set to the
      cloud `modified`; `record_verified` is called. The summary counts it under skipped, with a
      line `Already on disk and verified: N`.
    - Otherwise, the new version is first downloaded and verified into the partial. Only then is the
      existing path *moved aside*.
  - **Move aside.** The target is
    `DEST/.yandex-disk-rescue/replaced/<run start, UTC, YYYY-MM-DDTHH-MM-SSZ>/<local_path>`. It uses
    `renameat2(RENAME_NOREPLACE)` (same filesystem, so atomic) and creates the parent directories
    under `replaced/`. After that the new file is placed. A size-only file whose local copy has the
    right size is adopted; that is the documented limit of size-only verification.
  - **Changed in the cloud.** A file verified for an older identity that changed in the cloud is
    handled the same way (new version first, then move aside).
  - **Things in the way.** These are moved aside the same way, then the directory or file is
    created:
    - a directory where a file must go;
    - a file or symlink where a folder must go;
    - a symlink anywhere inside DEST.

    A symlink is moved as a link, and its target is never touched. The tool never writes through
    a symlink inside DEST. DEST itself may be a symlink; it is resolved once.
  - **Never.** The tool never deletes or truncates anything outside `.yandex-disk-rescue/partial/`.
  - **Local-only content is invisible.** A path in DEST with no cloud counterpart is never opened,
    hashed, moved, listed or counted, and causes no error. That covers macOS `.Spotlight-V100`,
    `.Trashes`, `.fseventsd`, `.DS_Store`, unrelated files, and extra files inside mirrored folders.
    The tool touches only:
    - the final paths of manifest entries;
    - the parent directories of those paths;
    - `.yandex-disk-rescue/`.

    It never lists a directory to find work.
  - **Summary.** A new list: `Moved aside to .yandex-disk-rescue/replaced/<stamp>/: N`, with the
    paths.
- **Behaviour to lock:** extend `tests/test_download.py`:
  - **Adopt.** An untracked identical file is adopted: the fake log shows no `/download` for it,
    it counts under the new line, and a rerun skips it without hashing.
  - **Differs.** An untracked different file ends up in `replaced/<stamp>/…` byte-identical, and
    the new file is verified in place.
  - **Changed in the cloud.** After a mirror, `add_file` with new content and a rescan-and-download
    moves the old copy aside and places the new one (#5's acceptance criterion 1).
  - **Download fails.** The same change with `corrupt()` keeps the old copy at the final path,
    untouched, and reports the failure; `replaced/` is empty.
  - **Things in the way.** A directory where a file must go, a file where a folder must go, and a
    symlink folder pointing outside `tmp_path/dest` all end up under `replaced/`. Nothing is written
    to the symlink's target, which is checked by listing it.
  - **Touched locally.** A verified file whose content was changed locally (same size, new mtime)
    is hashed: it is moved aside and downloaded again. A file whose mtime only was touched is
    adopted again with no transfer.
  - **Local-only content.** DEST starts with these, which have no counterpart in the fake tree:
    - `.Spotlight-V100/store.db`;
    - `.DS_Store`;
    - `notes.txt`;
    - `Photos/local-only.jpg` inside a mirrored folder;
    - a local-only folder `Загрузки/` with files.

    After a first run and a rerun, each one is byte-identical with an unchanged mtime and inode,
    none appears in the summary, `replaced/` is empty, and the exit code is 0.
  - **Alias.** The Stage 5 hard-link alias test still passes unchanged: no adoption and no move
    aside. That rules out the ping-pong where each run moves the sibling's verified copy to
    `replaced/`.
- **Deliverable:** reruns over existing content are safe and economical.
- **Validation:** `mise run check`; `mise run test -- yandex-disk-rescue/tests/test_download.py`.
- **Doc updates:** root `README.md` "Downloading", rewritten as current state: adoption, the
  `replaced/` folder and its layout, "nothing is deleted", "files and folders that are not on the
  Disk are left alone", and symlinks. Remove the Stage 5
  "left untouched" sentence (`rg 'left untouched'`).

### Stage 7: Resume with HTTP Range, retries and expired links

- **Contract:**
  - **Resume or restart.** For a file with a `partial` record and a partial file:
    - **Record and entry differ.** If the record's identity differs from the entry's, the partial
      is deleted and the file starts from byte 0.
    - **Record and entry agree.** Before resuming, the tool calls `client.resource(path)`. If the
      current cloud identity differs from the entry's, the file fails with
      `changed in the cloud since the scan; rerun to fetch the new version`, and the partial is
      deleted. Otherwise it resumes from `min(partial size, size)`:
      - a partial of exactly `size` goes straight to verification;
      - a partial larger than `size` is deleted, and the file restarts from 0.

    This is the issue's "check that the cloud file has not changed before resuming".
  - **New option** (registry entry, so also a config key and an env var):

    | Option / flag | Env var | Default | Valid |
    |---|---|---|---|
    | `download_retries` / `--download-retries` | `YANDEX_DISK_RESCUE_DOWNLOAD_RETRIES` | 5 | 0–20 |

    It counts *retries after the first attempt*, the same semantics as `api_retries` (#2's
    `stop_after_attempt(retries + 1)`). A file gets at most `download_retries + 1` transfer
    attempts per run.
  - **Within a run.** A `TransientTransferError` mid-body continues from the bytes written so far,
    with a new Range, within that budget. It uses exponential backoff with jitter (0.5 s up to
    30 s) or `Retry-After`, capped at the limiter's 300 s.
  - **Exhausted.** Past the budget, the file fails with the last reason plus ` (after N attempts)`.
    `TransferRefusedError` fails it at once.
  - **Expired link.** A `LinkExpiredError` (401/403/410 from the download host) gets one fresh
    `download_link` and continues from the bytes already written; it does not count against the
    budget. A second `LinkExpiredError` for the same file in the same run fails it with
    `download link rejected again after a refresh (HTTP <n>)`.
  - **Range not honoured.** `RangeNotHonouredError` truncates the partial and restarts from 0
    within the same attempt. It never appends a full body to a prefix.
  - **Bad resumed bytes.** Resumed bytes that later fail verification follow Stage 5 (delete,
    fail). A power cut can leave a garbage tail; #4's checkpoints narrow that.
  - **Digest on resume.** The digest of a resumed file covers the bytes in the partial. The prefix
    is re-read from disk on resume; incremental hashing is the implementer's call.
  - **Messages** never contain the href's query string.
- **Behaviour to lock:** extend `tests/test_download.py`:
  - **Truncate mid-run.** `inject(Truncate(after_bytes=300_000))` on a 1 MiB file's download: the
    run exits 0, and the second `/download` request carries `range: bytes=300000-<size-1>` (Stage 4 sends closed ranges).
  - **SIGKILL** (issue acceptance). A subprocess `yandex-disk-rescue download` runs with
    `chunk_delay`. Once the partial file is over 256 KiB, it is killed with SIGKILL. A rerun
    exits 0, its first `/download` request has `range: bytes=<k>-<size-1>` with `k > 0`, and the file
    verifies.
  - **SIGINT.** The same with SIGINT exits 130, prints the stderr message, and keeps the partial.
  - **Cloud changed before resume.** After the kill, `add_file` changes the content, and the rerun
    rescans. The partial is discarded, the `/download` request has no `range`, and the new
    content is verified.
  - **Range ignored.** `ignore_range = True` on the resume gives a correct file; it never grows
    past `size`.
  - **Refresh once.** A `Truncate` followed by `expire_links()` means exactly two link requests
    for that file. The file verifies, and the second `/download` request has a range.
  - **Second expiry.** Expiry on the refreshed link too fails only that file, with the message, and
    the other files succeed.
  - **Retries.** 503 × (`download_retries`+1) fails with `(after N attempts)`, and 503 × 2 succeeds.
    `download_retries = 0` in the config file plus one `Truncate` fails after one attempt, with no
    suffix. `--download-retries 99` exits 2.
  - **Redirects.** `redirect_downloads = True` gives a verified mirror.
- **Deliverable:** interrupted downloads continue from the bytes on disk; links are refreshed once.
- **Validation:** `mise run check`; `mise run test -- yandex-disk-rescue/tests/test_download.py`.
- **Doc updates:** root `README.md` "Downloading", rewritten as current state: interruption and
  resume, retries, and expired links. Add the "Configuration" table row for `download_retries`,
  and the package `README.md` env var row.

### Stage 8: Scan freshness window and `--no-scan`

- **Contract:**
  - **New option** (registry; config key and env var):

    | Option / flag | Env var | Default | Valid |
    |---|---|---|---|
    | `scan_max_age` / `--scan-max-age` (minutes) | `YANDEX_DISK_RESCUE_SCAN_MAX_AGE` | 60 | 0–10080 |

  - **`--no-scan`.** A plain boolean flag on `download` only. It is a per-invocation intent, not a
    tunable, so it is not a registry entry (registry entries are typed int/str settings with
    ranges).
  - **Decision.** `last = manifest.last_scan(account.uid, root)`:
    - `--no-scan` with `last is None` exits 2 with `disk:/A has never been scanned; run 'yandex-disk-rescue scan disk:/A' or drop --no-scan.`
    - `--no-scan` otherwise uses the manifest as it is.
    - Without the flag, the run scans when `last is None`, when `scan_max_age == 0`, when
      `now - last.completed_at >= scan_max_age`, or when `completed_at` is in the future.
      Otherwise it does not scan.
  - **`root`** is the API spelling. Without a scan, the root is resolved with `client.resource()`,
    as the scan does. That one API call is the same not-a-folder or missing check `scan` performs.
  - **Summary line**, always printed in place of the scan summary when no scan ran:
    `Using the scan of disk:/ completed 2026-10-17 14:05 UTC (23 min ago)`, or with
    `--no-scan`: `Using the scan of … (--no-scan)`. When a scan ran, the scan summary is printed
    as today.
  - `scan` is unchanged: it always scans, and its commit records the time (Stage 2).
- **Behaviour to lock:** extend `tests/test_download.py`:
  - **Inside the window.** Two `download` runs a moment apart (default 60): the second one's fake
    log has no `GET /v1/disk/resources` listing requests, and stdout has `Using the scan of`.
  - **Window of 0.** `--scan-max-age 0` lists again.
  - **Ancestor scan.** `scan disk:/` followed by `download disk:/Photos` within the window does not
    list, and names the `disk:/` scan.
  - **Old scan.** The test sets `scans.completed_at` to two hours ago in the real manifest file,
    with `sqlite3`, outside any run. The next `download` rescans. A `completed_at` one hour in the
    future also forces a rescan.
  - **`--no-scan`.** Before any scan it exits 2 with the hint; after a scan it never lists.
  - **Range.** `--scan-max-age 10081` exits 2. `scan_max_age = 5` in the config file is honoured.
  - **Freshness and resume.** Inside the window, a file changed in the fake after the scan is
    reported as failed through Stage 7's resume check or Stage 5's verification, never kept as
    verified.
- **Deliverable:** reruns within an hour skip the 3-minute rescan; scripted reruns can pin the
  manifest.
- **Validation:** `mise run check`; `mise run test -- yandex-disk-rescue/tests/test_download.py -k "scan"`.
- **Doc updates:**
  - Root `README.md` "Downloading", rewritten as current state: when `download` scans, `--no-scan`,
    and the summary line. Replace the Stage 5 "the scan step" sentence.
  - The "Configuration" row for `scan_max_age` and the package `README.md` env var row.
  - "Scanning a folder": the manifest also records when each scan completed.

### Stage 9: Names the destination rejects

- **Contract:**
  - **When.** Creating a folder or placing a file can fail with `EINVAL`, `EILSEQ` or
    `ENAMETOOLONG` for the *last component*, after the run-start `RENAME_NOREPLACE` probe passed.
    The tool then computes `rejected_name_conversion(name, taken=sibling keys)` and calls
    `Mirror.rename_local(entry_id, converted)`. For a folder, this rewrites every descendant's local
    path. The operation is retried once with the converted name.
  - **Retry fails.** The entry fails with `the destination filesystem refuses the name <name>
    (<strerror>), and its converted form <converted> too`. For a folder, every file below it fails
    with a reason naming that folder.
  - **`ENAMETOOLONG` for the whole path** (over `PATH_MAX`) is not converted: the file fails.
  - **Stable.** Converted names live in `local_names`, so later runs create them directly with no
    error. `map_local_names` keeps them through the existing "assigned" stability rule.
  - **Summary.** Converted names join the "Renamed for the destination" list, in the same form as
    the collision and long-name renames.
  - **Seam for testing.** Every filesystem call that creates a name (mkdir, the placement rename,
    the move aside) goes through one small module of syscall wrappers. This lets tests inject the OS
    error: no Linux filesystem available in CI rejects these characters.
- **Behaviour to lock:** extend `tests/test_download.py`:
  - **Injected rejection.** Using `monkeypatch`, the wrapper module raises
    `OSError(errno.EINVAL)` for any name containing `?`, which is what exFAT and ntfs3 do. The rest
    of the path is real `tmp_path` I/O. A tree with these entries is mirrored:
    - a file `a?b.txt`;
    - a folder `Q?/` holding `x.txt`;
    - a sibling `a_b~<hex>.txt` (a real name that collides with the conversion).

    Expected: exit 0; `a_b~<hex2>.txt` and `Q_~<hex>/x.txt` exist and verify; the summary lists both
    renames.
  - **Stable.** A rerun under the same fault makes no transfers and no rejected calls: the wrapper
    sees no name containing `?`.
  - **Converted name refused too.** Rejecting `_` as well fails `a?b.txt` with the message, and the
    rest of the mirror is verified.
- **Deliverable:** files with names the target refuses are stored under a deterministic, recorded
  name instead of failing.
- **Validation:** `mise run check`; `mise run test -- yandex-disk-rescue/tests/test_download.py`;
  `mise run ci`.
- **Doc updates:** root `README.md` "Local file names", rewritten as current state: the
  rejected-name conversion rule and the example. Remove any sentence saying such names fail
  (`rg -i 'refuse|rejects' README.md`).

## Manual verification (main runs this with the user; not in CI)

Prerequisites:

- A real token.
- A `cp -a` backup of the manifest beforehand (main does this with the user; the architect does not
  touch `~/.local/state`).
- Run from `uv run`.
- Before anything, record the state of the pre-existing local content, so step 6 can prove it was
  not touched:
  - `find /mnt/oblako -maxdepth 1`;
  - `find /mnt/oblako/Загрузки /mnt/oblako/Скриншоты -type f -printf '%P %s %T@ %i\n' | sort > /tmp/oblako-before.txt`.

1. **Small subfolder, first run, and the unverified API facts.** Pick a folder of about 50 files
   with a nested folder, an empty folder, and a Cyrillic name if possible. Run
   `download disk:/<small> /mnt/oblako/ydr-trial`. A destination is bound to one remote root, so
   the trial uses its own folder; the root mirror later leaves `ydr-trial/` alone as local-only
   content, and it can be removed by hand.
   - Expect exit 0 and plausible counts. `sha256sum` and `md5sum` of 3 files should match
     `sqlite3 <manifest> "select md5, sha256 from entries where path = ..."`.
   - This confirms the download host works without the token, follows redirects and honours Range.
   - If the href needs the token, that is a contract change: stop and re-plan Stage 4.
2. **Rerun inside the window.** Expect `Using the scan of … (N min ago)` and `0 downloaded`, with no
   listing and no transfers. `--scan-max-age 0` rescans and still downloads nothing.
3. **Interrupt and resume.** Ctrl+C during a file over 200 MB, then rerun. Expect exit 130, then a
   resume. Compare the partial size before the rerun with the time taken.
4. **Change in the cloud.** Edit one file in the web UI, then rerun with `--scan-max-age 0`. Expect
   the old copy under `/mnt/oblako/ydr-trial/.yandex-disk-rescue/replaced/…` and the new one
   verified.
5. **Collision count.** Before the whole account, run a read-only SQL query on the manifest that
   counts siblings colliding under `lower()`. This roughly counts the HFS+ renames.
6. **Whole account into the disk root:** `download /mnt/oblako`.
   - **Merge with what is there.** Cloud names are mirrored as-is. If the Disk's folders are named
     `Загрузки`/`Скриншоты` (the Yandex web UI may show them in English, as Downloads and
     Screenshots, but the API path is what counts), the local files with the same relative path are
     hashed. Identical ones are adopted ("Already on disk and verified"); differing ones are moved
     to `.yandex-disk-rescue/replaced/<stamp>/`. If the Disk's names differ, those local folders
     are local-only and are left alone.
   - **Afterwards.**
     - Rerun the `find … > /tmp/oblako-after.txt` command and `diff` it against the before file:
       the only differences may be paths the summary lists as moved aside.
     - The macOS folders and `ydr-trial/` are unchanged.
   - **Interrupt.** Interrupt a few hours in, then rerun to prove resume across a long run.
     Finishing the full 2.1 TiB can wait for #4 (sequential: about 133 k link requests plus one
     stream, likely more than a day).
   - **After a crash**, check for a garbage tail: HFS+ on Linux is unjournaled.
7. **Optional:** a small mirror onto an exFAT USB stick with a `?` in a file name, to see the
   rejected-name conversion on a real filesystem.

## Discovered issues

- **Fix in this PR:**
  - The scan walk lives inside a command module and cannot be reused (Stage 1).
  - HFS+ case-insensitivity means a plain `rename` could silently overwrite a sibling's verified
    file. Every placement and move uses `RENAME_NOREPLACE` (Stage 5).
- **Filed as issues:**
  - #11: a portable name mapping for exFAT/FAT/NTFS destinations. Its scope shrinks (main
    edits it). The rejected-name conversion in this PR already stores files whose names the
    destination refuses. #11 becomes an opt-in *proactive* mode (`local_names = "portable"`) that
    applies the same conversion to every Windows-unsafe name up front. It is for a mirror kept on
    ext4 or HFS+ that must later be copied to exFAT or NTFS, plus the rule that a mirror never mixes
    modes.
- **For main to relay (issue edits, not new issues):**
  - #5: trim deliverable 1 (the "replaced" move ships in #3); #5 keeps quarantine of failed
    partials, which #3 deletes.
  - #4: HFS+ (the user's disk) has no sparse files. Writing segments at offsets of one 16.5 GiB
    file zero-fills the gap first, which matters for segment start offsets and the HDD caveat.

## Risks / open questions

- **Removable disks: the fingerprint has no device number.** `st_dev` of a USB disk changes on
  replug, reboot or re-enumeration. Had it been part of the skip fingerprint and the alias lookup,
  the first run after a replug would re-hash all 2.1 TiB, and the alias guard would lapse for that
  run.
  - **What it costs.** Dropping it means a mount point nested *inside* DEST, a different
    filesystem whose inode numbers can repeat, could make `verified_owner` match a stranger's file.
  - **Safe direction.** That file then fails with the "same file as" message; nothing is moved or
    overwritten.
  - **Not handled.** The mirror does not descend into nested mounts specially.

- **Unverified offline:** Range support on the download host, the expiry status codes, the link
  lifetime, and whether `href` works without the token. Manual step 1 decides. A token being
  required would mean sending it only to Yandex-owned hosts, which is a contract change.
- **Freshness window.** Within `scan_max_age` (60 min by default), cloud changes since the last
  scan are not seen until the next rescan, and a stale entry fails at verification or at the
  resume check, never silently. The scan time comes from the local clock, so a clock jump backwards
  can lengthen the window. A `completed_at` in the future counts as stale, which forces a rescan.
- **Testing the rejected-name conversion.** Linux filesystems available in CI accept every
  character a Disk name can hold, so rejection is produced by fault injection at the tool's own
  syscall wrapper (Stage 9). The real exFAT/NTFS path is unverified until someone runs it on such a
  disk; manual step 7 is optional.
- **A destination is bound to one remote root.** That is why the trial in manual step 1 uses
  `/mnt/oblako/ydr-trial`, which stays as a local-only folder in the root mirror (left alone) and
  can be removed by hand later.
- **Collision key.** Decision 5 fixes the key regardless of filesystem. The alternative is to probe
  the destination for case and normalization sensitivity: more faithful on ext4, but untestable in
  CI, where no case-insensitive filesystem is available.
- **Changed destination path.** A mirror is keyed by the resolved DEST path. Remounting the disk
  elsewhere makes a new mirror, and every file is then adopted by hashing (correct, but a full
  read of the data).
- **Unjournaled HFS+.** On Linux, HFS+ after a power cut can leave garbage tails. Verification
  catches them (the file is re-downloaded), but a long file costs a full retry until #4.
- **Sequential speed.** The whole-account manual run under #3 may take more than a day (see manual
  step 6).
- **No progress output.** A long run prints nothing until the summary; that is #6.
- **`renameat2` through ctypes.** It is Linux/glibc-specific, which matches "a Linux CLI".
  `EINVAL` (a filesystem without the flag) aborts the run with a clear message rather than falling
  back to an overwriting rename.
