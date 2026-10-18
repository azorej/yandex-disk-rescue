# Plan: Save Yandex's preview of files that cannot be downloaded

Issue: #26 (umbrella #9; relates to #14)
Branch: feat/save-previews

Builds on the approved plans that land first, in order: #23 → #15 → #16 → #27 → #18, and then on #14
(`docs/plans/2026-10-18-issue-14-unavailable-on-yandex.md`, schema v11, `Failure.unavailable`). The migration below
(**v12**) is the next after #14's v11. The number is subject to rebase. #26 does not need #14's code, only its
migration number: if #26 lands first, it takes v11 and #14 renumbers.

## Context

- **A failed file leaves nothing usable.** Observed on `main` (0d2b423) against the fake server: a file whose download
  never sends a byte ends each run under `Failed` with its last reason (see the #14 plan's Context). It leaves an
  empty partial file in `.yandex-disk-rescue/partial/` and nothing else. For photos and videos, Yandex often still
  serves a reduced image, and for a rescue tool that is better than nothing.
- **The tool never asks for previews today.** `DiskClient.resource` and `list_folder` request fixed `fields`
  (`api/client.py:_RESOURCE_FIELDS`, `_LISTING_FIELDS`), and neither includes `preview`. `models.Resource` has no
  such field. The fake server serves none.
- **The Disk API, from the reference ("File and folder meta information", `GET /v1/disk/resources`):**
  - `preview_size` takes `S`/`M`/`L`/`XL`/`XXL`/`XXXL` (longest side 150/300/500/800/1024/1280 px), an exact width
    or height (`120`, `120x`, `x145`), or `WxH`.
  - `preview_crop` (default `false`) crops.
  - The `preview` field is a link to the reduced image, "included only for supported image formats".
  - "The preview can only be requested with the OAuth token of a user who has access to the file." The example
    links are on `downloader.disk.yandex.ru/preview/…`.
  - The resource body also has an undocumented `sizes` list of `{name, url}` (client libraries such as `yadisk`
    parse it as `ResourceObject.sizes`).
- **The token must reach a host it never reached before.** `ContentClient` deliberately never sends the token (the
  fake's download host answers 400 to any `Authorization`). A preview link is a second authenticated host, so the
  plan needs an explicit rule for where the token may go.

## Probe results (real account, read-only, run by main on 2026-10-18)

These answers settle D5 and the token/host rule.

1. **What the resource body offers.** `GET /v1/disk/resources?path=…&preview_size=XXXL`, with no `fields`, returns
   `preview` (host `downloader.disk.yandex.ru`) and `sizes`. It also has `media_type`, `mime_type` and
   `antivirus_status` (`clean`, the dead napkin included, so #14 has nothing to add from it).
   - For images, `sizes` has `ORIGINAL`, `DEFAULT`, `XXXS`, `XXS`, `XS`, `S`, `M`, `L`, `XL`, `XXL`, `XXXL` and `C`.
   - For videos and PDFs it has the same list without `ORIGINAL`.
   - A ZIP has no `preview` and `sizes: []` (`media_type: compressed`).
2. **The token is required.** The preview link with `Authorization: OAuth …` answers 200. Without it, it answers 403
   `text/html` (48 B).
3. **`ORIGINAL` is the original, and it fails like the original.** `ORIGINAL` for a healthy JPEG returned the
   original's bytes (its MD5 matched the resource `md5`). It redirected to `sNNNklg.storage.yandex.net`. It failed
   for the files that cannot be downloaded:
   - the napkin's `ORIGINAL` timed out (20 s read timeout);
   - 4 other files with failure records ("the download host …") answered HTTP 500 (44 B) for `ORIGINAL`;
   - their `XXXL` answered 200 (64 to 136 KB) for all of them.

   So `ORIGINAL` comes from the same storage as the download and is no alternative route.
4. **Yandex caps previews at 1280 px.** `preview_size=4096x` gave the same bytes as `XXXL`, so the cap is 1280 px on
   the longer side:
   - a healthy JPEG gives 853×1280;
   - a video gives a 1280×720 `image/jpeg` frame;
   - a PDF gives an `image/png` (410 KB);
   - the napkin (78 KB original) gives a 639×510 preview of 70 KB, its native size.
5. **Link lifetime is untested.** The links were not fetched again after an hour, so the tool fetches each preview
   right after its metadata request.
6. **Hosts.** `downloader.disk.yandex.ru` (previews) and `*.storage.yandex.net` (`ORIGINAL`'s redirect). Both are
   under `yandex.ru` / `yandex.net`.
7. **The content type varies** (`image/jpeg`, `image/png`), so the extension comes from `Content-Type`, never from
   the original's name.
8. **Live failure records.** Only 4 failure records for media files exist on the live mirror today, because pass-1
   deferrals are recorded only once #27 lands.
9. **`fields` and redirects (follow-up probe).** `fields=preview,type` returns exactly those keys, and
   `fields=preview,type,md5,size,modified` works too. The preview link answers 200 `image/jpeg` directly, with no
   redirect (`follow_redirects=False`: 200, no `Location`). Whether `sha256` passes the filter too is not probed.
   Stage 1 requests it, and the fake honours it. If the real API drops it, the version check compares size, MD5 and
   modification time only.

## Goal

- At the end of every `download` run whose transfers ended normally, each file that failed on Yandex's side (D3)
  gets the largest preview Yandex offers, when
  it offers one, unless a preview for the file's current version is already saved. Previews go to
  `DEST/.yandex-disk-rescue/previews/`, at the file's local path, named `<local name>.preview<ext>`. A preview is
  never at a mirror path.
- The manifest records each saved preview: its path, byte size, content type, the size name asked for, and the
  remote version it was taken for.
- The summary, `--json` and the log report the files saved only as a preview, and the previews that could not be
  fetched. Preview saving never changes the run's outcome or exit code, and never stops the run.
- Once the original is verified, its preview file and record are removed.
- `--no-previews` / `previews = false` / `YANDEX_DISK_RESCUE_PREVIEWS=false` turn fetching off.
- `verify` never reads, checks or reports anything under `previews/`. It names the preview of a not-downloaded file.

## Non-goals

- Video transcodes or any derived format other than the preview image (the issue's Out of Scope).
- Previews of quarantined files: they have a full-size copy in `quarantine/`.
- Previews of folders, or of entries whose name is not valid Unicode (they cannot be requested).
- Verifying a preview. Yandex publishes no checksum of it, so only its size and source are recorded.
- The `ORIGINAL` size. It is the original's bytes from the same storage (probe 3): for a failed file it fails again,
  and for a healthy file it is a plain download.
- Fetching previews during the scan (`fields` of the listing). A link's lifetime is unknown (probe 5), and only
  failed files need one.

## Decisions for the user

- **D1. Where previews live. Recommended: `DEST/.yandex-disk-rescue/previews/<local path>.preview<ext>`.** It sits
  next to `replaced/` and `quarantine/`, so everything the tool keeps besides the mirror is in one work folder. The
  final summary prints the folder. The alternative, a visible folder at DEST's root (for example
  `DEST/Previews of files not downloaded/`), shares a namespace with the mirror: DEST's root mirrors the remote
  root, so a Disk folder of that name would collide, and every later scan would have to exclude it.
- **D2. After the original is verified. Recommended: delete the preview and its record.** `previews/` is then
  exactly the set of files you have only as a preview, a list you can browse. The alternative keeps the file and
  drops only the record. That never deletes anything, but the folder then mixes stale previews with the ones that
  matter, and the README's "Nothing is deleted" paragraph stays as it is.
- **D3. Which files. Recommended: only files whose failure came from Yandex's side.** These are files out of
  passes after transient transfer failures, unavailable files (#14), a download the Disk refuses, and a file the
  download host refuses. Never folders, never entries whose name is not valid Unicode, and never local causes.
  - Local causes are a permission error at the final path, a refused name, an alias, no room in the partial folder,
    a file removed or changed on the Disk, a folder that cannot be made, and every entry below such a folder.
  - The alternative, every file under `Failed`, costs far more than it looks. One folder that cannot be made fails
    every entry below it (`_blocked_by`, mirroring.py:811-812, 828-852). That can mean previews for 100k files over
    a local cause, all deleted again on the next good run.
  - Folder and unencodable entries are in `report.failed` too (mirroring.py:780-784), so the rule must name files
    explicitly.
  - The price is one flag on the report's failures, `Failure.on_yandex`, set where the failure is raised
    (Stage 3).
- **D4. The opt-out. Recommended: a registry option `previews` (bool, default true)**, which gives a flag
  `--previews/--no-previews`, the env var `YANDEX_DISK_RESCUE_PREVIEWS` and a config key. Unattended runs set it
  once in `config.toml`. The registry today knows only `int` and `str`, so Stage 4 adds `bool`. The alternative
  is a CLI-only `--no-previews`, like `--retry-failed`: no registry change, but nothing to set in the config file.
- **D5. Size. Settled by the probe: `XXXL`, the `preview` link requested with `preview_size=XXXL`.** It is the
  largest preview Yandex serves (1280 px on the longer side; a larger `WxH` gives the same bytes, probe 4). It
  answered for every failed file probed. `ORIGINAL` is not used (probe 3, Non-goals). `sizes` is not read: its
  `XXXL` entry is the same image, and the documented `preview` field is the stable contract.

## Stages

### Stage 1: The API client finds and fetches a file's preview; the fake server serves previews

- **Contract (`yandex_disk_rescue.api`):**
  - **Types.**
    - `@dataclass(frozen=True) class PreviewLink: url: str; size_name: str` is the signed link and the size it is
      for, always `XXXL` (D5). Its `repr` never shows `url`, because the link is signed.
    - `@dataclass(frozen=True) class FilePreview: resource: Resource; link: PreviewLink` is the file's metadata as of
      the request, and its preview link.
    - `@dataclass(frozen=True) class Preview: content: bytes; content_type: str`.
  - **`DiskClient.preview(path: str) -> FilePreview | None`.**
    - It sends `GET /v1/disk/resources` with `path`, `preview_size=XXXL` and
      `fields=path,type,size,md5,sha256,modified,preview`. A `fields` filter keeps `preview` (probe 9).
    - The resource part is parsed like `resource`'s, so the caller can check the version.
    - It returns `None` when the body has no `preview`, a ZIP for example (probe 1).
    - A missing path raises `RemotePathNotFoundError`, as `resource` does. Other errors, retries and throttling are
      as for every `DiskClient` request.
  - **`DiskClient.fetch_preview(link: PreviewLink) -> Preview`.**
    - It sends a `GET` of the link with the OAuth token, which the preview host requires (403 without it, probe 2).
      The request goes through the same limiter, with the same retries and timeout as API requests.
    - It **follows no redirect**, since the real host answers directly (probe 9). A 3xx raises
      `PreviewRefusedError('the preview host redirected (HTTP <n>); not followed')`, and nothing else is requested.
      So the token can never be forwarded.
    - The caller fetches a link right after `preview` returns it and never keeps it, because its lifetime is
      unknown (probe 5).
    - The token is sent only to the origin of the configured API URL (the fake), or to `https` on the exact host
      `downloader.disk.yandex.ru`, the only preview host seen (probes 1, 6, 9). A link anywhere else raises
      `PreviewRefusedError('the preview link points to <host>, not to the Yandex Disk preview host; the token is
      not sent there')`, and no request is made. If Yandex moves previews to another host, the error says which
      host, and the rule widens in its own change.
  - **Errors**, as subclasses of the existing API `RescueError` hierarchy in `api/errors.py`:
    - an answer that is not `200` raises `PreviewRefusedError` with the status;
    - a `Content-Type` that is not `image/*` raises `PreviewRefusedError('not an image (<type>)')`. Real previews
      are `image/jpeg`, or `image/png` for a PDF (probe 4);
    - a body over `PREVIEW_MAX_BYTES = 64 * 2**20` raises `PreviewRefusedError('larger than 64 MiB')`, and reading
      stops at the limit;
    - a transport error past the retries raises `DiskApiError`, as other requests do.

    No reason ever quotes the link.
  - `tests/fake_disk_server.py`:
    - `add_file(..., preview: bytes | None = None, preview_type: str = 'image/jpeg')`.
    - `GET /v1/disk/resources` adds `preview` (`<base_url>/preview?path=<disk path>&size=<preview_size>`) for a
      file with a preview, when `preview_size` is given.
    - `GET /preview` serves the bytes with `preview_type`. Unlike `/download`, it answers as the real host does
      without `Authorization: OAuth <token>`: 403 `text/html`. It answers 404 for a file with no preview.
    - `redirect_previews: str | None`: when set to a base URL, `/preview` answers 302 to `<that base>/preview?<same
      query>`.
    - The `fields` filter is honoured for the keys above.
    - `inject` works on `/preview` like any route.
    - The module docstring and `FakeDiskApi` docstring describe it.
- **Behaviour to lock (`tests/test_api_client.py`, real sockets via `fake_disk_api`):**
  - `test_preview_of_a_photo_is_fetched_with_the_token_and_a_file_without_one_has_none`. A JPEG with a preview: the
    link's `size_name == 'XXXL'`, and the resource request carried `preview_size=XXXL`. `fetch_preview` returns the
    bytes and `image/jpeg`, and the `/preview` request carried `Authorization: OAuth <token>`. A ZIP without a
    preview gives `None`. A `/preview` request sent without the token (straight through `httpx`) gets the fake's
    403.
  - `test_the_token_goes_only_to_the_preview_host_and_the_api_origin`: the host rule, as a table over URLs.
    - Accepted: `https://downloader.disk.yandex.ru/…` (decided by the rule, not fetched) and the fake's API origin
      (fetched).
    - Refused: `http://downloader.disk.yandex.ru/…` (not https), `https://s1klg.storage.yandex.net/…`,
      `https://yandex.ru/…`, `https://evil-downloader.disk.yandex.ru.example/…`, `https://example.com/…`, and a
      second fake server's origin (another port).

    Each refused one raises `PreviewRefusedError` naming the host, and the second fake records no request.
  - `test_a_redirecting_preview_host_is_not_followed_and_the_token_never_leaves`: `redirect_previews` points at a
    second fake server. `fetch_preview` raises `the preview host redirected (HTTP 302); not followed`, and the
    second server records no request, so none with `Authorization` either.
  - `test_a_preview_that_is_not_an_image_or_too_large_is_refused`. With `preview_type='text/html'` the error is
    `not an image (text/html)`. With `PREVIEW_MAX_BYTES` monkeypatched to 1000 and a 5000-byte preview, it is
    `larger than …`.
  - `test_a_failing_preview_request_is_retried_like_an_api_request`: `inject('GET', '/preview', 503)` once, and the
    fetch succeeds.
- **Deliverable:** client support for previews and fake-server previews. No command uses them yet.
- **Validation:** `mise run check`; `mise run test -- yandex-disk-rescue/tests/test_api_client.py`.
- **Doc updates:**
  - `yandex-disk-rescue/README.md` § Layout: the `api/client.py` line gets "a file's preview link and the preview
    itself (the token only to the API's origin and `downloader.disk.yandex.ru`, no redirects)". The `models.py` line gets the preview link. The
    `fake_disk_server.py` line gets "previews (`/preview`, which needs the token)".
  - `api/client.py` module and class docstrings: the second authenticated host and its rule.
  - No user-visible change, so no CHANGELOG line yet.

### Stage 2: The manifest records previews; the destination saves and removes them

- **Contract:**
  - Schema migration 12 (subject to rebase), with a comment like its neighbours:
    ```sql
    CREATE TABLE previews (
        mirror_id INTEGER NOT NULL REFERENCES mirrors (id) ON DELETE CASCADE,
        entry_id INTEGER NOT NULL REFERENCES entries (id) ON DELETE CASCADE,
        size INTEGER NOT NULL CHECK (size >= 0),  -- remote identity the preview was taken for
        md5 TEXT,
        sha256 TEXT,
        modified TEXT NOT NULL,
        path TEXT NOT NULL,                       -- relative to the destination
        bytes INTEGER NOT NULL CHECK (bytes >= 0),
        content_type TEXT NOT NULL,
        size_name TEXT NOT NULL,
        PRIMARY KEY (mirror_id, entry_id)
    )
    ```
  - `manifest.PreviewRecord` (frozen): `identity: RemoteIdentity`, `path: PurePosixPath`, `bytes: int`,
    `content_type: str`, `size_name: str`. `MirrorEntry.preview: PreviewRecord | None`.
  - `Mirror.record_preview(entry_id, record: PreviewRecord) -> None` replaces any earlier record.
    `Mirror.forget_preview(entry_id) -> None`. Both raise `ValueError` for an entry outside the mirror, like
    `record_failure`.
  - No other record touches it: `record_verified`, `record_failure`, `record_quarantined`, `forget`, `clear_*` and a
    rescan keep it. Only an entry's removal from the manifest deletes the row. Stage 3's run removes it once the
    original is verified.
  - `destination.PREVIEWS_DIR_NAME = 'previews'`. `Destination.preview_path(local: PurePosixPath, content_type: str)
    -> PurePosixPath` is `.yandex-disk-rescue/previews/<local parent>/<name>`, where name is
    `<local name>.preview<ext>`:
    - the extension is `.jpg` for `image/jpeg`, `.png`, `.webp`, `.gif` and `.heic` for theirs, and `.img` for any
      other `image/*`;
    - a name over `local_names.MAX_NAME_BYTES` keeps the `.preview<ext>` ending and shortens the rest by the
      local-names long-name rule, a hash suffix included.
  - `Destination.save_preview(path: PurePosixPath, content: bytes) -> None`:
    - it creates the folders below `previews/` as needed, through folder descriptors and never through a link, as
      `replaced/` is made;
    - it writes a temporary file in the target folder, fsyncs it, and renames it over `path`;
    - it replaces an earlier preview, a regular file. Anything else at `path` (a folder, a link) raises
      `FileExistsError` and is left as it is;
    - errors are `OSError`, and the caller decides.
  - `Destination.remove_preview(path: PurePosixPath) -> None` deletes that regular file, and nothing when it is
    gone. It then deletes the folders below `previews/` that this left empty, never `previews/` itself.
  - The tool's "nothing is deleted outside …" rule now also names `previews/`.
- **Behaviour to lock:**
  - `tests/test_manifest.py`:
    - `test_a_preview_record_is_replaced_kept_by_every_other_record_and_dropped_with_its_entry`: record, replace,
      `record_verified` / `record_failure` / `forget` keep it, `forget_preview` drops it, and an entry removed by a
      rescan drops it.
    - The rollback-upgrade test is rolled forward to v12: `DROP TABLE previews`, `user_version = SCHEMA_VERSION - 1`,
      and `open_existing` recreates it empty.
  - `tests/test_download.py` (destination-level, real `tmp_path`, next to the existing `Destination.open` tests):
    - `test_a_preview_is_saved_below_the_work_folder_replaced_by_a_newer_one_and_never_through_a_link`. Save at
      `Photos/2024/a.jpg` and check the file at `.yandex-disk-rescue/previews/Photos/2024/a.jpg.preview.jpg`.
      Saving again replaces it. Replace `previews/Photos` with a symlink to another folder: the save raises and
      nothing is written in the link's target. A folder at the preview path raises `FileExistsError` and stays.
    - `test_a_long_name_gets_a_shortened_preview_name_that_fits`: a 250-byte local name gives a name of at most
      255 bytes that ends with `.preview.jpg`.
    - `test_removing_a_preview_deletes_the_folders_it_left_empty_but_not_previews`.
- **Deliverable:** storage for previews. No command uses it yet.
- **Validation:** `mise run check`; `mise run test -- yandex-disk-rescue/tests/test_manifest.py`;
  `mise run test -- yandex-disk-rescue/tests/test_download.py -k preview`.
- **Doc updates:**
  - `yandex-disk-rescue/README.md` § Manifest: add a bullet for the preview record (`previews`). Say what it holds,
    that every other record and a rescan keep it, and that removing the entry deletes it. Add `previews` to the
    "A rescan never touches …" sentence. § Layout: the `destination.py` line gets "the previews folder (saving a
    preview atomically, replacing an earlier one, removing it with the folders it emptied)". The `manifest.py`
    line gets "preview records".
  - `manifest.py` module docstring: the per-mirror tables list.
  - `destination.py` module docstring: the work folder's subfolders.

### Stage 3: `download` saves the preview of every failed file at the end of the run, and reports it

- **Contract:**
  - **Which failures came from Yandex.** `mirroring.Failure` gains `on_yandex: bool` (default `False`), and
    `FileFailed(reason, *, on_yandex: bool = False)` carries it to the report. It is true, for files only, for:
    - a file out of passes after transient transfer failures (the end-of-passes report of a `FileDeferred` that is
      not `NoRoomYet`);
    - an unavailable file (#14);
    - a download the Disk refuses (`DownloadRefusedError` in `_download_link`);
    - any `api.TransferError` turned into `FileFailed` in `_stream` (a host refusal, too many bytes, a whole-file
      range not honoured).

    Everything else is false: removed or changed on the Disk, local errors, names, aliases, room, folders, entries
    below a failed folder, and unencodable entries. `on_yandex` is internal, not in the JSON.
  - **The phase.** A new module `yandex_disk_rescue.previews` has
    `async def save_previews(report: MirrorReport, mirror: Mirror, destination: Destination, client:
    api.DiskClient, progress: PreviewProgress, *, fetch: bool) -> None`.
    - It fills `report.preview_only`, `report.preview_errors` and `report.previews_pending`.
    - `commands/download.py` calls it in `_run`, inside the same event loop and the same `interrupts.run` (#15) as
      the transfer phase, never in a loop of its own. The call comes once `MirrorRun.run` has returned normally and
      the content client is closed, for plain and `--retry-failed` runs alike.
    - **An interrupted or error-stopped transfer phase fetches no previews and cleans up nothing.** Ctrl+C, SIGTERM
      or a run-stopping error propagates as today, before `save_previews` is reached.
    - In this stage `fetch` is always `True`. Stage 4 adds the option.
  - **Cleanup first, whatever `fetch` is.** For every entry of the mirror with a preview record whose download
    record is verified for the entry's current identity: `remove_preview`, then `forget_preview`. A file that is
    already gone counts as removed. An `OSError` other than a missing file goes to `report.preview_errors` with
    the reason `could not delete the preview <path>: <reason>`, and the record stays.
  - **Then the failed files.** For each failure in `report.failed` with `on_yandex`, in path order:
    - With a preview record for its current identity and a regular file at the recorded path, nothing is
      fetched. The file is listed in `preview_only` with that record.
    - Otherwise, with `fetch`: `client.preview(path)`. `None` and `RemotePathNotFoundError` mean no preview: log
      at info `<path>: no preview on Yandex Disk`, and the file is in neither list. Nothing is recorded, so the next
      run asks again. When the returned resource's identity differs from the entry's, it is a preview error, `changed
      on the Disk since the scan; rerun with --scan-max-age 0`, and nothing is fetched. Otherwise:
      1. `client.fetch_preview(file_preview.link)`, at once;
      2. `destination.save_preview(destination.preview_path(local, content_type), content)`, in a worker thread;
      3. `mirror.record_preview(...)`;
      4. if an earlier record had another path, remove its old file;
      5. list the file in `preview_only`.
    - At most `api_concurrency` files are in flight; the shared limiter paces the requests.
  - **Never fails the run.**
    - A per-file `RescueError` from the API (`DiskApiError`, `PreviewRefusedError`, …) or an `OSError` while
      saving is logged as a warning (`<path>: preview not saved: <reason>`) and appended to
      `report.preview_errors`.
    - `TokenRejectedError` or a `DestinationError` ends the phase: every file not yet handled goes to
      `preview_errors` with that reason, and the function returns.
    - **An interrupt (Ctrl+C, or SIGTERM after #15) stops the phase only.** The files in flight are cancelled, and
      previews already recorded stay. `report.previews_pending` is set to the number of files not handled. The
      function returns normally: it takes the cancellation back (`Task.uncancel`), so neither `asyncio.Runner` nor
      #15's `interrupts.run` turns it into `KeyboardInterrupt` / `Terminated`. The command then prints its summary
      and `--json` document as usual, with the line `Previews interrupted: P of N files not handled; the next run
      saves them.` The exit code is the transfer outcome's (`1` when files failed), not 130 or 143. A second Ctrl+C
      during the phase still stops the process at once, as asyncio's second interrupt always does, with exit 130
      and no summary.
    - Once a file's save has started in its worker thread, the interrupt waits for that save and its
      `record_preview` before the cancellation is taken. A preview file therefore never exists without its record.
      Only fetches still in flight are dropped.
    - An interrupt in the moment after `MirrorRun.run` returns and before `save_previews` starts handling interrupts
      is still a run interrupt: exit 130 or 143, no summary, as during the transfers. The next run fetches the
      previews.
    - The report's counts, the failures and the exit code stay as `MirrorRun.run` left them.
  - `MirrorReport` gains `preview_only: list[PreviewOnly]`, `preview_errors: list[Failure]` and
    `previews_pending: int` (0). `PreviewOnly` is a frozen dataclass of `path: str`, `preview: PurePosixPath`
    (relative to DEST) and `bytes: int`. Both lists are in path order.
  - `progress.PreviewProgress`: a phase with the status line `Previews: <done> of <n> files, <k> saved`, shown like
    the other phases (stderr off a terminal, log file, live view). `commands/download.py` prints the headline
    `Saving the previews of N failed files to DEST/.yandex-disk-rescue/previews` before it, only when N > 0.
  - `mirror_summary`: after the `Failed` list (and #14's hint line), before `Partial files not deleted`:
    ```
    Saved only as a preview (a reduced image, not verified): 2
      disk:/…/napkin.jpg -> .yandex-disk-rescue/previews/…/napkin.jpg.preview.jpg
    Previews not saved: 1
      disk:/…/x.jpg: the preview host returned HTTP 500
    ```
    The head line, `Error: …` and the exit code are unchanged.
  - `mirror_document`:
    - `counts.preview_only`, `counts.preview_errors` and `counts.previews_pending`;
    - `preview_only: [{"path", "preview", "bytes"}]`;
    - `preview_errors: [{"path", "reason"}]`.

    `schema_version` stays `1`.
- **Behaviour to lock (`tests/test_download.py`, next to #14's unavailable tests; previews via the fake):**
  - `test_a_failed_photo_gets_its_preview_saved_apart_and_the_summary_says_so`. `disk:/Dead/napkin.jpg` has a
    preview and 503 on every `/download`, with `--download-passes 1`. Check:
    - exit 1, and it is still under `Failed` with its reason;
    - `DEST/.yandex-disk-rescue/previews/Dead/napkin.jpg.preview.jpg` holds the preview bytes;
    - `DEST/Dead/napkin.jpg` does not exist;
    - the summary has `Saved only as a preview (a reduced image, not verified): 1` and its line;
    - the `/preview` request carried the token;
    - a `--json` twin has `counts.preview_only == 1` and the item.
  - `test_a_failed_file_without_a_preview_ends_failed_with_no_preview_and_no_error`: a ZIP with no preview and 503.
    No `previews/` folder, neither summary list, and exit 1 from the failure only.
  - `test_a_preview_already_saved_for_the_current_version_is_not_fetched_again`: a second run with the same fault.
    One `/v1/disk/resources` preview request in run 1 and none in run 2 (filter on `preview_size`), and run 2
    still lists it under `Saved only as a preview`.
  - `test_once_the_original_downloads_its_preview_is_deleted`: remove the fault and run plain. The file is placed,
    and the preview file, its empty folders and the record are gone. `previews/` stays. A `--retry-failed` twin
    does the same.
  - `test_a_preview_that_cannot_be_fetched_is_reported_and_the_run_is_unchanged`:
    `inject('GET', '/preview', *[500] * 10)`. The file is in `Previews not saved` with the reason, exit 1 from the
    failure only, and the other files are unaffected. A healthy run (no failure) makes no `/preview` and no
    `preview_size` request.
  - `test_a_quarantined_file_gets_no_preview`: a corrupt file (`disk.corrupt`) is quarantined and no preview is
    requested.
  - `test_failures_with_a_local_cause_get_no_preview`: two cases, and neither makes a `preview_size` request.
    - A file below a folder that cannot be made: a file stands where the folder must go, and that file cannot be
      moved aside (the permission setup of `test_a_copy_out_of_reach_fails_that_file_only`).
    - A file failing with a permission error on its own path.
  - `test_a_preview_for_a_version_changed_since_the_scan_is_not_saved`: change the file on the fake after the scan,
    keeping its `/download` fault. It is reported in `Previews not saved` with `changed on the Disk since the scan`,
    and `/preview` is not requested.
  - `test_an_interrupt_during_the_preview_phase_keeps_the_summary_and_the_exit_code`, parametrized over SIGINT and
    SIGTERM, with `start_command` and `wait_until` from `mirror_tree`.
    - Setup: a failing photo with a preview, and `inject('GET', '/preview', Delay(30))`. Wait until the fake has
      recorded the `/preview` request, then send the signal.
    - The child exits `1` for both signals, not 130 or 143.
    - stdout has the full summary with `Failed: 1` and `Previews interrupted: 1 of 1 files not handled; the next run
      saves them.`, then the `Manifest:` and `Log:` lines.
    - A `--json` twin has `counts.previews_pending == 1`.
    - A following plain run saves the preview.
  - `test_an_interrupted_or_stopped_transfer_phase_fetches_no_previews`, with two cases. Neither makes a
    `preview_size` or `/preview` request.
    - SIGINT while a failing photo's transfer is gated: exit 130.
    - A run stopped by a persistent 500 on another file's link request (the #30 repro): exit 1 with `Error: Could
      not download …`.
  - Update `test_json_reports_a_run_with_the_counts_of_its_summary_and_every_failure_and_quarantined_copy`: `counts`
    gains `'preview_only': 0` and `'preview_errors': 0`, since `short.bin` has no preview in the fake.
  - `mirror_tree.STATUS_LINE` learns `Previews:`, so `stderr_lines` keeps working.
- **Deliverable:** failed files get their preview, reported in the summary, JSON and log.
- **Validation:** `mise run check`; `mise run test -- yandex-disk-rescue/tests/test_download.py -k "preview or
  json_reports or quarantined"`; `mise run test -- yandex-disk-rescue/tests/test_output.py`; `mise run ci`.
- **Doc updates:**
  - Root `README.md` § Downloading: a new paragraph **Previews of files that cannot be downloaded.** after
    "Retrying failures". Write it as current state:
    - which files get a preview: files failed on Yandex's side (out of passes, unavailable, refused), not local
      failures and not quarantined files. When: at the end of a run whose transfers ended normally, not again while
      a preview of the same version is saved. A file with no preview is asked again on every run;
    - that an interrupt during the preview step stops only that step: the summary is printed, the exit code is the
      transfers', and the next run does the rest;
    - that a newer version of the file that fails too replaces the older version's preview;
    - where it goes and how it is named;
    - that it is a reduced image (Yandex's `XXXL`, at most 1280 px on its longer side), not the original,
      never verified, its size and source recorded;
    - which types have one: images, videos (a JPEG frame) and PDFs (a PNG of the first page), not archives;
    - that it is deleted once the original is verified;
    - that it never changes the outcome or exit code;
    - that the token is sent to `downloader.disk.yandex.ru` and nowhere else, and that redirects are not followed;
    - that a preview of a file later removed from the Disk stays in `previews/`, untracked, until you delete it.
  - Root `README.md` § Summary example: add both new lists. Add a sentence on the `Saving the previews …` headline
    and the `Previews:` status line in § Output and logs.
  - Root `README.md` § JSON reports, `download`: the example and key notes gain `preview_only`, `preview_errors`
    and their counts.
  - Root `README.md` § "Nothing is deleted": rewrite as current state. Add that the tool deletes a preview in
    `previews/` once its file is verified, and replaces it with the preview of a newer version of the same file, which may have been the last trace of
    the older version. § "The
    destination": the work directory also holds `previews/`.
  - `yandex-disk-rescue/README.md` § Layout: a line for `previews.py`. Update the `commands/download.py` line (the
    preview phase, headline and summary), `progress.py` (`PreviewProgress`) and `console.py` if the phase list is
    named there.
  - `CHANGELOG.md` `## [Unreleased]` → `### Added`: `download` saves Yandex's preview of each file it could not
    download in `DEST/.yandex-disk-rescue/previews/`, and lists them.
  - `rg -n "never deletes or truncates|work directory" README.md yandex-disk-rescue/`: every mention is current.

### Stage 4: Previews can be turned off; `verify` names them and never treats them as mirror files

- **Contract:**
  - `settings.Setting` accepts `bool` defaults. `typer_option` gives a `--<name>/--no-<name>` flag for a bool.
    `_TYPE_NAMES[bool] = 'true or false'`, so `previews = "no"` in the config file is a usage error naming the key.
    The env var takes Click's bool spellings (`true/false/1/0/yes/no`).
  - `settings.PREVIEWS = Setting('previews', True, "Save Yandex's preview of each file that could not be
    downloaded in DEST/.yandex-disk-rescue/previews/.", <always valid>)`, with `Previews = Annotated[bool, …]`.
    `download` takes it and passes `fetch=previews` to `save_previews`. With `--no-previews`:
    - no preview request is made;
    - files with a preview already saved for their current version are still listed under `Saved only as a
      preview`;
    - the cleanup of Stage 3 still runs;
    - the headline `Saving the previews …` is not printed.

    The JSON document gains a top-level `previews: true|false`, the option's value, like `partial_dir`.
  - `verify`:
    - A not-downloaded file whose preview record is for its current identity gets `; a preview is saved at <path>`
      after its reason.
    - The JSON `not_downloaded[]` items gain `preview` (the path, or `null`).
    - `verify` never opens anything under `previews/`. It reads only the manifest records, as it does for
      quarantined copies.
- **Behaviour to lock:**
  - `tests/test_config.py`:
    - `test_download_reads_previews_from_the_config_file_and_the_flag_overrides_it`: with `previews = false` in
      `config.toml` and a failing photo, no `preview_size` or `/preview` request is made and there is no
      `previews/`. With `--previews` on the same config, the preview is saved. `YANDEX_DISK_RESCUE_PREVIEWS=0`
      behaves like `false`.
    - Extend `test_an_invalid_config_file_is_a_usage_error_naming_the_path_and_the_key` with `previews = "no"` →
      `must be true or false.`
  - `tests/test_verify.py`, next to `test_files_not_downloaded_and_quarantined_are_listed_without_being_read`:
    `test_verify_names_a_saved_preview_and_never_reads_the_previews_folder`. After Stage 3's preview run:
    - `verify DEST` lists the file under not downloaded with `; a preview is saved at .yandex-disk-rescue/previews/…`;
    - JSON `preview` is set;
    - with the preview file then `chmod 000` and a stray `previews/extra.jpg` added, `verify` has the same exit
      code and counts and reports neither;
    - the `slow_reads`/read-gate helper, or an `os.open` monkeypatch, shows that no file under `previews/` is
      opened.
  - `tests/test_download.py`: `test_no_previews_still_deletes_the_preview_of_a_file_now_downloaded`.
- **Deliverable:** the opt-out, and the `verify` behaviour.
- **Validation:** `mise run check`; `mise run test -- yandex-disk-rescue/tests/test_config.py
  yandex-disk-rescue/tests/test_verify.py`; `mise run test -- yandex-disk-rescue/tests/test_download.py -k preview`;
  `mise run ci`.
- **Doc updates:**
  - Root `README.md` § Configuration: the `previews` row (flag, env var, config key, default `true`). § Downloading:
    the option list in the first paragraph names `--previews/--no-previews`, and the previews paragraph says how
    to turn it off.
  - Root `README.md` § Verifying a mirror: rewrite the not-downloaded description and the JSON key notes as current
    state, with the preview path, and say that `verify` never reads `previews/`.
  - `yandex-disk-rescue/README.md` § Environment variables: `YANDEX_DISK_RESCUE_PREVIEWS`. § Layout:
    `settings.py` ("bool options as `--x/--no-x` flags") and `verifying.py` (the preview of a not-downloaded file).
  - `CHANGELOG.md` `### Added`: `--no-previews` (`previews = false`). `verify` names the saved preview of a file
    not downloaded.

## Discovered issues

- **Fix in this PR:** the settings registry cannot hold a bool option (`settings._TYPE_NAMES` has only `int` and
  `str`). Stage 4 adds it, since this is its first bool.
- **Filed as issues:** #30 (filed with the #14 plan), a download-link request failing for one file stops the whole
  run. It also matters here: a file Yandex refuses at the link step never reaches the end of the run to get a
  preview.

## Risks / open questions

- **Link lifetime (probe 5, untested).** Stage 3 fetches each link right after its metadata request, so a short
  lifetime does not matter. A link that has expired anyway is a per-file preview error, and the next run retries.
- **The acceptance test on the real account is feasible.** The napkin's `XXXL` preview answered 200 (639×510,
  70 KB), and so did 4 other failed media files, while their `ORIGINAL` failed. On the live mirror only 4 such
  files have failure records until #27 lands, so check the acceptance after #27.
- **Sending the token to a second host.** The rule is the exact host `downloader.disk.yandex.ru` over https,
  plus the API origin. Redirects are refused, not followed. If Yandex moves its preview host, previews fail with an
  error naming the new host until the rule is widened. That is a visible failure, never a token sent to a host not
  checked.
- **Cost at scale.**
  - About 3,500 failed files is about 7,000 requests at the end of a run, paced at `api_interval_ms` (50 ms), so
    about 6 min.
  - A file with a saved preview costs nothing on later runs.
  - A failed file with no preview (the live `Загрузки/music/` cluster of mp3s, for example) costs one metadata
    request on every run, because "no preview" is not recorded. For 1,000 such files that is about 1 min a run.
    Recording "no preview for this version" would save it, at the price of a second record kind. It is left out
    until the cost shows.
- **A file that changes on the Disk and still fails.** Its new version's preview replaces the old version's at the
  same name. The old one may have been the last trace of that version. The README says so. Naming previews per
  version would keep both, but the folder would then stop being one preview per file.
- **Interrupt semantics depend on #15's `interrupts.run`.** Stage 3 needs the preview phase to swallow its own
  cancellation (`Task.uncancel`) without `interrupts.run` re-raising `Terminated`. If #15's final code cannot allow
  that, the implementer changes `interrupts.run` in Stage 3 (a contract change, named in the commit). The behaviour
  test is the arbiter.
- **Previews of files removed from the Disk.** When a later scan drops the entry, its record is deleted with the
  entry, and the preview file stays in `previews/` untracked. It may be the last trace of that file. The README
  says so, and the tool never deletes it.
- **Migration number.** v12 assumes #27 takes v10 and #14 takes v11.
