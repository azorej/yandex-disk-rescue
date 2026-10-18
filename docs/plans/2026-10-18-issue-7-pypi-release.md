# Plan: PyPI releases and install via uv tool / pipx

Issue: #7 (last issue of milestone v1.0, umbrella #9)
Branch: chore/pypi-release (from `main` at 1a84acf)

## Context

Observed on `main` (1a84acf), with nothing published and no network calls to Yandex:

- **The name is free.** `curl -s -o /dev/null -w '%{http_code}'` returns `404` for
  `https://pypi.org/pypi/yandex-disk-rescue/json`, `https://pypi.org/simple/yandex-disk-rescue/`,
  `https://pypi.org/pypi/yandex_disk_rescue/json` and `https://test.pypi.org/pypi/yandex-disk-rescue/json`.
- **A build works, but the distributions are not fit to publish.**
  `uv build --package yandex-disk-rescue --out-dir <scratch>` builds the sdist, then the wheel from the sdist, with
  hatchling (unpinned, `hatchling>=1.32.4`, so not in `uv.lock`).
  - **The PyPI page would be the developer README.** The long description is `yandex-disk-rescue/README.md`, the
    26 KB developer doc (env-var table, manifest internals, module layout).
  - **No license file.** The wheel has no `License-File` and no `licenses/LICENSE`, and the sdist has no `LICENSE`.
    The only `LICENSE` is at the repository root, outside the member's project directory.
  - **Bare metadata.** There are no classifiers, keywords or project URLs.
  - **`twine check --strict` passes** all the same, so it cannot be the only gate.
- **Experiments in a scratch copy of the member** (not in the repo):
  - A `LICENSE -> ../LICENSE` symlink goes into the hatchling sdist as a dangling symlink, and the wheel still has
    no license.
  - `[tool.hatch.build.targets.sdist.force-include] "../LICENSE"` gives the wheel its `License-File`, but the
    sdist's `PKG-INFO` has none, so the two metadata sets disagree.
  - `uv_build>=0.13.1,<0.14` with a real `LICENSE` in the member dir gives `License-File: LICENSE` in both the sdist
    and the wheel, the license under `dist-info/licenses/`, and an sdist of only `src/`, `pyproject.toml`, the
    readme and the license. That is the same backend and range the `.dagger` module already uses.
- **Version.** It is static: `version = "0.1.0"` in `yandex-disk-rescue/pyproject.toml`. `__init__.py` reads
  `importlib.metadata.version('yandex-disk-rescue')`. `yandex-disk-rescue --version` prints `yandex-disk-rescue 0.1.0`,
  and `test_cli.py` locks that format in-process and through the console script on `PATH`. The same `__version__`
  goes into the log's start record and the JSON reports' `version` key. `uv version --package <p> <v>` (uv 0.13.1)
  sets the version and re-locks.
- **CI.** It has three lanes (`check`, `dagger-selftest`, `hygiene`) behind `CI required`. No workflow is triggered
  by tags. Running `uvx zizmor --offline .github/workflows` (zizmor 1.31.1) gives 10 findings:
  - 7 × `unpinned-uses`, because every action is pinned by tag, not by SHA;
  - 3 × `artipacked`, because `actions/checkout` runs without `persist-credentials: false`.

  `actionlint` passes. Neither tool runs in CI today.
- **GitHub state.** The repository is public. It has no environments, no tags and no ruleset. The API shows
  `main` as **unprotected**, although `docs/github-setup.md` describes an importable ruleset. Actions
  `sha_pinning_required` is `false`.
- **The fake Disk server is stdlib-only.** `tests/fake_disk_server.py` imports nothing outside the standard library,
  so it can serve an installed wheel in a container that holds no project source.

## Goal

A pushed tag `vX.Y.Z` on a commit of `main` runs `.github/workflows/release.yml`, with no stored secrets:

1. It builds one sdist and one wheel of version `X.Y.Z` and checks them.
2. It installs the wheel in clean containers with `uv tool install` and `pipx install`, and runs the CLI end to end
   against the fake Disk server.
3. **[option (a) only]** It publishes to TestPyPI with Trusted Publishing (OIDC) and verifies the upload.
4. After a manual approval, it publishes the same files to PyPI with Trusted Publishing and verifies that
   `uv tool install yandex-disk-rescue==X.Y.Z` gives `yandex-disk-rescue X.Y.Z`.
5. It creates the GitHub Release, with that version's CHANGELOG section as its notes and the two files attached.

A tag that does not equal `v` + the project version fails before anything is uploaded. So does a version with no
CHANGELOG section.

The same build, check and install smoke run on every PR that touches the package (a new `package` lane in
`CI required`) and on the weekly backstop.

The README documents `uv tool install yandex-disk-rescue` and `pipx install yandex-disk-rescue`. CONTRIBUTING.md
documents the release process. This PR ends with version `1.0.0` under option (a), or `1.0.0rc1` under option (b)
(Decision 1), and its CHANGELOG entry, ready to tag.

## Decisions that go beyond the issue's wording (for the user to approve)

1. **TestPyPI leg or rc rehearsal: the user picked (b) on 2026-10-18: no TestPyPI leg; the first release is `v1.0.0rc1`, then `v1.0.0`.** Everything marked **[option (a) only]** is not built. Both keep the full pre-upload checks (the tag and
   version check, the CHANGELOG notes, the build, `twine check`, the install smoke with both installers) and the
   manual approval gate on the `pypi` environment. The workflow takes pre-release tags in either case
   (`v1.0.0rc1` for version `1.0.0rc1`), and `uv tool install` does not pick a pre-release by default.

   | | (a) Keep the TestPyPI leg | (b) Drop TestPyPI; first release is `v1.0.0rc1` on PyPI |
   |---|---|---|
   | **Every release** | dist → publish-testpypi → verify-testpypi → (approval) publish-pypi → verify-pypi → github-release | dist → (approval) publish-pypi → verify-pypi → github-release |
   | **First release** | Stage 6 sets `1.0.0`; the user tags `v1.0.0` | Stage 6 sets `1.0.0rc1` (CHANGELOG `## [1.0.0rc1]`); the user tags `v1.0.0rc1`; then a release PR (`uv version 1.0.0`, heading renamed to `## [1.0.0]`) and the tag `v1.0.0` |
   | **User setup** | Two accounts (PyPI and TestPyPI, separately, both with 2FA), two pending publishers, two environments | One account, one pending publisher, one environment |
   | **Code** | `published` also has a `testpypi` mode (install by file URL so dependencies never come from TestPyPI); two more jobs | `published` has only the `pypi` mode |
   | **Benefit** | The PyPI page and the OIDC setup are tried on an index nobody installs from, before anything public | The rehearsal runs on the real index, through the very code paths (`published` by name, the CDN lag retry) every later release uses. A burnt rc number costs nothing |
   | **Cost** | A version uploaded to TestPyPI is burnt there: a rebuild needs the next patch (Risks). TestPyPI's rendering and availability are not PyPI's, so it proves less than it seems. More setup and code to keep working | The rc is public on PyPI forever (as a pre-release). One extra release PR for 1.0.0 |

   **Recommendation: (b).** `test_packaging.py` and `twine check --strict` already lock the PyPI page and metadata
   before any upload. The approval gate already allows a stop before PyPI. And only the real index exercises
   `published` as every later release runs it. (a) adds a second account, a second trusted publisher and a code
   path that exists only for it.

   **The plan is written so that either choice is a small delta.** The TestPyPI parts are marked **[option (a)
   only]**: the `testpypi` mode of `published`, the two `*-testpypi` jobs, the TestPyPI setup steps and docs.
   Under (b) they are left out, and `publish-pypi` needs `dist` directly. The only other difference is the version
   Stage 6 sets.
2. **The version source is the static `version` in `yandex-disk-rescue/pyproject.toml`.** It is bumped with
   `uv version --package yandex-disk-rescue X.Y.Z` in a release PR, which also updates `uv.lock`.
   - The tag must be `v` + exactly that string. No VCS-derived versions (hatch-vcs, uv-dynamic-versioning): they
     need `.git` in the Dagger source and cache keys for `uv sync`, and they make an untagged build's version
     depend on git state.
   - `--version` keeps its format, `yandex-disk-rescue X.Y.Z` from the installed metadata.
3. **The build backend moves from hatchling to `uv_build>=0.13.1,<0.14`.**
   - Its range follows uv in `mise.toml`, as the `.dagger` module's already does, and goes into the Versions table.
     The backend is then bounded by the pinned toolchain instead of floating.
   - Hatchling cannot ship the root `LICENSE` with consistent metadata (see Context).
4. **The PyPI page is a dedicated short `yandex-disk-rescue/PYPI.md`** (`readme = "PYPI.md"`). It holds what the
   tool is, the safety promise, the install commands, the four-command quick start, the requirements and links.
   - Every link in it is absolute `https://`. PyPI has no repository-relative links and no heading anchors.
   - The member `README.md` stays the developer doc, and the root `README.md` stays the full user manual.
   - Alternative: move the developer content out and make the member README the PyPI page. That would change where
     agents look for package internals (AGENTS.md: "the README of every package you touch").
5. **`yandex-disk-rescue/LICENSE` is a copy of the root `LICENSE`**, which stays for GitHub's license detection. A
   test asserts that both distributions ship it byte-identical to the root file.
6. **Classifiers and URLs.**
   - Classifiers:
     - `Development Status :: 5 - Production/Stable` (also on a `1.0.0rc1` under option (b); accepted as cosmetic)
     - `Environment :: Console`
     - `Intended Audience :: End Users/Desktop`
     - `Intended Audience :: System Administrators`
     - `Operating System :: POSIX :: Linux`
     - `Programming Language :: Python :: 3`
     - `Programming Language :: Python :: 3 :: Only`
     - `Programming Language :: Python :: 3.14`
     - `Topic :: System :: Archiving :: Backup`
     - `Topic :: Utilities`
   - No `License ::` classifier: PEP 639 `License-Expression` replaces it.
   - Keywords: `yandex`, `yandex-disk`, `backup`, `download`, `mirror`, `cli`.
   - `[project.urls]`:
     - `Homepage` and `Repository`: the GitHub repo.
     - `Documentation`: the root README on GitHub.
     - `Issues`.
     - `Changelog`: `blob/main/CHANGELOG.md`.
7. **Runtime dependencies get an upper bound at the next major:** `httpx>=0.28.1,<1`, `rich>=15.0.0,<16`,
   `tenacity>=9.1.4,<10` and `typer>=0.27.3,<1`.
   - Users install a frozen release and cannot patch it. httpx 1.0 in particular is a known breaking release.
   - Minor-version breakage is still caught by the weekly fresh-resolution smoke (Decision 10).
   - Alternative: no upper bounds.
8. **Publishing runs in `release.yml` with `pypa/gh-action-pypi-publish`, not in Dagger.**
   - This is an explicit exception to "the workflow is a thin caller of `mise run` tasks". Trusted Publishing needs
     the Actions OIDC token, and nothing local should ever be able to publish.
   - The action also uploads PEP 740 attestations.
   - Everything else is a Dagger function behind a mise task, so it can be rehearsed locally with
     `mise run release:dist v1.0.0`: the tag check, the CHANGELOG notes, the build, the checks, the install smoke
     and the post-publish verification.
9. **The release jobs**, in order:
   1. `dist`
   2. `publish-testpypi`, in environment `testpypi` **[option (a) only]**
   3. `verify-testpypi` **[option (a) only]**
   4. `publish-pypi`, in environment `pypi` with a required reviewer: the manual gate
   5. `verify-pypi`
   6. `github-release`

   One build is uploaded as a workflow artifact, and every index gets those same bytes. The `dist` job also fails
   unless the tagged commit is an ancestor of `origin/main`. The jobs that can publish run no repository code
   (Stage 5).
10. **The install smoke covers both `uv tool install` and `pipx install`**, as the README promises both.
    - Dependencies are resolved afresh from PyPI, keyed on the UTC date so Dagger's cache does not hide a new
      release. So the weekly run, and any PR touching the package, catches a third-party release that breaks the
      installed tool.
    - pipx, twine, pytest and pytest-timeout come from the root `dev` group, so `uv.lock` pins them and the Versions
      table gains no new pin location.
11. **Workflow hygiene comes first, because `release.yml` cannot run before a tag exists.**
    - `zizmor --offline` and `actionlint` run as pre-commit hooks in the existing `hygiene` lane, pinned via the
      `dev` group.
    - Every `uses:` is pinned to a full commit SHA with a `# vX.Y.Z` comment, and `actions/checkout` gets
      `persist-credentials: false`.
    - `.github/dependabot.yml` updates the github-actions ecosystem weekly, grouped into one PR.
    - Alternative: lint only, with no Dependabot. The SHA pins would then go stale silently.
12. **CHANGELOG and versioning policy.**
    - The root `CHANGELOG.md` follows Keep a Changelog 1.1.0: a `## [Unreleased]` section, then
      `## [X.Y.Z] - YYYY-MM-DD` sections.
    - A user-visible change adds a line under `[Unreleased]` in its own PR. The PR template gets a checkbox and
      AGENTS.md gets the rule. CI does not enforce it: a path heuristic would misfire.
    - CONTRIBUTING.md states what SemVer covers: the commands and their options, exit codes, the `--json` report
      schema (`schema_version`), the config keys and env vars, and the manifest's upgrade path. A newer tool always
      opens an older manifest, while an older tool refusing a newer one is allowed.
13. **A tag ruleset is shipped as `docs/github-ruleset-tags.json`**, importable like the branch one. It protects
    `refs/tags/v*` against creation, update and deletion by anyone but the bypass actors (repository admins).
14. **The root README is restructured.**
    - The status banner is rewritten for 1.0.
    - A new `## Installation` section goes after Goals.
    - `## Quick start` becomes the user quick start: install, `auth login`, `download`, `verify`.
    - Today's devcontainer and mise quick start, Everyday commands, Layout and How it fits together move under one
      `## Development` heading.

## Non-goals

- Standalone binaries, distro packages, Homebrew or conda (the issue's out-of-scope list).
- Lowering `requires-python` below 3.14.
- Automated version bumps or release PRs (release-please and the like).
- Build provenance for the GitHub Release assets beyond the PyPI attestations.
- A `User-Agent` naming the tool: filed as #23.
- Publishing anything from this PR. The first upload happens when the user pushes the first tag after the merge
  (`v1.0.0` under (a), `v1.0.0rc1` under (b)).

## Stages

### Stage 1: Workflow linting and SHA-pinned actions

- **Contract:**
  - `uv add --dev zizmor actionlint-py` (pinned by `uv.lock`).
  - `.pre-commit-config.yaml`, local hooks (`language: system`, `pass_filenames: false`):
    - `actionlint` runs `uv run --no-sync actionlint` on changes under `^\.github/workflows/`.
    - `zizmor` runs `uv run --no-sync zizmor --offline .github` on changes under `^\.github/`.

    They run in the `hygiene` lane and locally on commit.
  - `.github/workflows/ci.yml`:
    - Every `uses:` is a full 40-hex commit SHA followed by `# vX.Y.Z`, the exact release tag it resolves to today
      (resolved with `gh api repos/<owner>/<repo>/commits/<tag> --jq .sha`; the major versions stay the same).
    - Every `actions/checkout` has `with: persist-credentials: false`.
  - New `.github/dependabot.yml`: `package-ecosystem: github-actions`, `directory: /`, weekly, one group for all
    actions, conventional-commit prefix `chore`.
  - Zero zizmor findings and zero actionlint findings over `.github/`. A finding is fixed, never suppressed, unless
    it is a false positive; then it gets an inline `# zizmor: ignore[<audit>]` with the reason.
- **Behaviour to lock:**
  - `uv run --no-sync pre-commit run --all-files` passes.
  - The implementer temporarily reverts one `uses:` to a tag and confirms that the `zizmor` hook fails, then restores
    it. The report states this.
- **Deliverable:** CI workflows are linted in the hygiene lane, and every action is pinned to a SHA with
  automated updates.
- **Validation:** `uv lock --check`, `uv run --no-sync pre-commit run --all-files`, `mise run check`.
- **Doc updates:**
  - `AGENTS.md`:
    - Versions table, two new rows:
      - "GitHub Actions: full commit SHA in `.github/workflows/*.yml` (updated by Dependabot)".
      - "zizmor, actionlint: `uv.lock`".
    - CI section: a bullet saying workflows are linted by actionlint and zizmor in the hygiene lane, and that a new
      `uses:` is pinned to a SHA with its tag as a comment.
    - Repository Layout: `.github/` now holds the workflows, the PR template and the Dependabot config.
  - `docs/github-setup.md`: an optional step, **Settings → Actions → General → Require actions to be pinned to a
    full-length commit SHA**.

### Stage 2: Package metadata, build backend, license and the PyPI page

- **Contract** (`yandex-disk-rescue/pyproject.toml`):
  - `[build-system] requires = ["uv_build>=0.13.1,<0.14"]`, `build-backend = "uv_build"`. The
    `[tool.hatch.build.targets.wheel]` table is deleted.
  - `readme = "PYPI.md"`, `license = "MIT"`, `license-files = ["LICENSE"]`.
  - The classifiers, keywords and `[project.urls]` of Decision 6.
  - The dependency bounds of Decision 7, applied with `uv add --package yandex-disk-rescue '<dep>>=x,<y>'`, so
    `uv.lock` is regenerated rather than hand-edited.
  - The version stays `0.1.0` (Stage 6 sets the release version, Decision 1).
  - New `yandex-disk-rescue/LICENSE`, byte-identical to the root `LICENSE`.
  - New `yandex-disk-rescue/PYPI.md`:
    - a one-paragraph description;
    - five feature bullets;
    - "Install": `uv tool install yandex-disk-rescue` (uv fetches Python 3.14 when needed) and
      `pipx install yandex-disk-rescue`, which needs a Python 3.14 interpreter (`pipx install --python python3.14 …`);
    - "Quick start": `auth login`, `download disk:/ /mnt/backup`, rerun after an interruption, `verify /mnt/backup`;
    - "Requirements": Linux and Python ≥ 3.14;
    - links to the full documentation, the changelog and issues;
    - the "not affiliated with Yandex" line.

    Absolute `https://github.com/azorej/yandex-disk-rescue…` links only.
  - New root `CHANGELOG.md`: a Keep a Changelog header and `## [Unreleased]` listing the v1 features under
    `### Added`. The features are auth, scan, download (resumable, verified, parallel and segmented with
    checkpoints, `--partial-dir`, quarantine, `--retry-failed`), `verify`, progress, the log file, `--json`, the
    config file and env vars, and exit codes.
  - **Version literals in tests.** `tests/test_output.py` hard-codes the version today: `version 0.1.0` at lines
    62, 305 and 307, and `'version': '0.1.0'` at line 936. Each becomes `yandex_disk_rescue.__version__`, so a
    version bump never breaks a test.
- **Behaviour to lock:** new `yandex-disk-rescue/tests/test_packaging.py`. It is a new file because no existing test
  covers the distributions.
  - **Setup.** It runs `uv build --package yandex-disk-rescue --no-sources --out-dir <tmp_path>` from the repository
    root as a subprocess, the real build.
  - **Files and version.** The out-dir holds exactly `yandex_disk_rescue-<v>.tar.gz` and
    `yandex_disk_rescue-<v>-py3-none-any.whl`, with `<v> == importlib.metadata.version('yandex-disk-rescue')`.
  - **Metadata.** The wheel's `METADATA` and the sdist's `PKG-INFO` are parsed with `email.parser`. In both:
    - `Name` and `Version` are right, and `Requires-Python` is `>=3.14`;
    - `License-Expression` is `MIT` and `License-File` is `LICENSE`;
    - no classifier starts with `License ::`;
    - `Operating System :: POSIX :: Linux` and `Programming Language :: Python :: 3.14` are present;
    - the `Project-URL` labels are exactly the five of Decision 6;
    - `Description-Content-Type` is `text/markdown`, and the description equals `PYPI.md`'s text.
  - **Links.** Every Markdown link and image target in the description starts with `https://`.
  - **License file.** The wheel's `*.dist-info/licenses/LICENSE` and the sdist's `<name>/LICENSE` equal the root
    `LICENSE` bytes.
  - **Entry point.** The wheel's `entry_points.txt` maps `yandex-disk-rescue` to `yandex_disk_rescue.cli:app`.
  - **Contents.** Every wheel member is under `yandex_disk_rescue/` or the dist-info, so no tests are shipped.
- **Deliverable:** both distributions carry complete metadata, the license and the PyPI page, and the test suite
  locks it.
- **Validation:**
  - `uv sync`, then `uv run yandex-disk-rescue --version` (the editable install still works under uv_build);
  - `mise run test -- yandex-disk-rescue/tests/test_packaging.py`;
  - `uv lock --check`, `mise run check`;
  - `uv run --no-sync twine check --strict` over the build. That uses `uvx twine` until Stage 4 adds twine; the
    report gives the command used.
- **Doc updates:**
  - `yandex-disk-rescue/README.md` Layout: rewrite to show `LICENSE`, `PYPI.md` (the PyPI page) and
    `tests/test_packaging.py`.
  - `AGENTS.md`:
    - Versions table: a row "uv_build (the build backend): range in `yandex-disk-rescue/pyproject.toml`, moved
      together with uv in `mise.toml`". The Dagger module's identical range is named in the same row.
    - Repository Layout: `CHANGELOG.md`.
  - Root `README.md` Layout block: `CHANGELOG.md`.

### Stage 3: An end-to-end test of whatever `yandex-disk-rescue` is on PATH

- **Contract:**
  - `tests/fake_disk_server.py` gains `serve() -> contextlib.AbstractContextManager[FakeDiskApi]`, which starts the
    server and shuts it down on exit. `conftest.py`'s `fake_disk_api` fixture uses it, so the start/stop code exists
    once.
  - New `yandex-disk-rescue/tests/installed/test_installed_cli.py`:
    - It imports only the standard library, `pytest` and `fake_disk_server`, and never `yandex_disk_rescue`. Its
      module docstring says why: it also runs against an installed wheel in a container that holds no source.
    - It runs the `yandex-disk-rescue` found on `PATH` in child processes. The environment is built explicitly:
      `PATH`, `HOME` and both XDG dirs under `tmp_path`, `YANDEX_DISK_RESCUE_TOKEN`, `YANDEX_DISK_RESCUE_API_URL` (the
      fake) and `YANDEX_DISK_RESCUE_API_INTERVAL_MS=25`, and nothing inherited beyond `PATH`. So it needs no
      conftest.
  - The test runs in the normal suite against the dev install, the venv's console script on `PATH`.
  - `test_cli.py::test_installed_console_script_runs` is deleted. The installed suite's `--version` test covers it,
    and the exact version is asserted against the built wheel in Stage 4.
- **Behaviour to lock** (all in `tests/installed/test_installed_cli.py`):
  - `--version` exits 0, and its stdout matches `^yandex-disk-rescue \S+\n$`.
  - `--help` exits 0 and lists `scan`, `download`, `verify` and `auth`.
  - `auth status` exits 0 and names the fake account's login.
  - **End to end.** The tree has a nested folder, a Cyrillic file name, a zero-byte file, an empty folder and a
    3 MiB file.
    - `download --json --download-segment-threshold-mib 1 --download-segments 4 disk:/ DEST` exits 0. The JSON
      `version` equals the `--version` string's version, the counts show every file downloaded, and every local file
      equals its remote bytes.
    - A second `download` exits 0 and downloads nothing.
    - `verify --json DEST` exits 0 and reports every file intact.
- **Deliverable:** one behavioural test of the CLI as an installed program, independent of the source tree.
- **Validation:** `mise run test -- yandex-disk-rescue/tests/installed`, `mise run check`.
- **Doc updates:** `yandex-disk-rescue/README.md` Layout: rewrite the `tests/` block with `installed/` (the
  end-to-end test of the `yandex-disk-rescue` on `PATH`, run against the dev install and against the built wheel)
  and `fake_disk_server.serve`.

### Stage 4: The `package` CI lane: build, check, install smoke

- **Contract:**
  - `uv add --dev twine pipx`.
  - `SOURCE_IGNORE` adds `dist` and `release`.
  - `RepoCi.dist(source) -> dagger.Directory`:
    - In the `workspace` container it runs `uv build --package yandex-disk-rescue --no-sources`, then
      `uv run --no-sync twine check --strict` over the result.
    - It returns a directory holding exactly the sdist and the wheel, named for the project version
      (`uv version --package yandex-disk-rescue --short`). Any other content is an error naming the files.
  - `RepoCi.smoke(source, dist: dagger.Directory | None = None) -> str` takes `dist()` when no `dist` is given.
    - For each installer in order, `uv tool install <wheel>` then `pipx install <wheel>`, it builds a fresh container
      from `UV_IMAGE`: a non-root user, no uv cache volume, holding only the wheel, `tests/installed/` and
      `tests/fake_disk_server.py`.
    - **Locked tool versions.** In the `workspace` container it runs
      `uv export --frozen --only-group dev --no-emit-workspace --no-hashes --no-header --no-annotate`, which writes
      every package of the `dev` group, transitive dependencies included, at its locked version. That file is mounted
      into the clean container as `/smoke/constraints.txt`. It is used only as constraints, so pytest, pytest-timeout
      and pipx and everything they pull in get their locked versions, and nothing else is installed from it.
      - Test runner: `uv venv /opt/smoke-tools`, then
        `uv pip install --python /opt/smoke-tools pytest pytest-timeout -c /smoke/constraints.txt`.
      - pipx: `uv tool install pipx -c /smoke/constraints.txt`.
      - The wheel under test is never installed with these constraints, so its dependencies resolve fresh.
    - **Layout.** The wheel is at `/smoke/<wheel>`, the tests at `/smoke/tests/installed/`, and the fake server at
      `/smoke/tests/fake_disk_server.py`.
    - **Installing.** The wheel is installed with dependencies resolved from PyPI, with no constraints. The exec
      carries the UTC date in an env var, so Dagger re-runs it at least daily.
      - `uv tool install /smoke/<wheel>` puts the command in `~/.local/bin`.
      - `pipx install /smoke/<wheel>`, with `PIPX_HOME` and `PIPX_BIN_DIR` under the user's home, puts it in
        `PIPX_BIN_DIR`.
    - It asserts that `<bin dir>/yandex-disk-rescue --version` prints exactly `yandex-disk-rescue <wheel version>`.
    - **Pytest invocation**, with `PATH=<installer's bin dir>:/usr/local/bin:/usr/bin:/bin` and
      `PYTHONPATH=/smoke/tests`, so `import fake_disk_server` works:
      `/opt/smoke-tools/bin/pytest /smoke/tests/installed -c /dev/null --rootdir /smoke -p no:cacheprovider --import-mode=importlib --timeout 120 -v --tb=short`.
      - `-c /dev/null` keeps any ini file out.
      - `/opt/smoke-tools/bin` is not on `PATH`, so the only `yandex-disk-rescue` there is the installed one.
    - A failure raises with the installer's name and the pytest output.
  - `RepoCi.package(source) -> str` runs `dist`, then `smoke` on that directory. This is the lane's gate.
  - `mise.toml`: `[tasks."ci:package"]`, "Build, check and install-smoke the distributions in Dagger",
    `run = "dagger call package"`.
  - `.github/workflows/ci.yml`:
    - A `package` filter in `changes` with `*dagger`, `yandex-disk-rescue/**`, `LICENSE`, `pyproject.toml`,
      `uv.lock` and `.python-version`, and its boolean output, written like the others.
    - Job `package` ("Package & install smoke"), shaped like `check` (SHA-pinned actions as in Stage 1), runs
      `mise run ci:package`.
    - `required.needs` gains `package`.
  - `.dagger/tests` gains `dist_builds_one_sdist_and_one_wheel`, which asserts that the exported entries are exactly
    the two file names for the workspace's project version. It is added to `all`.
- **Behaviour to lock:**
  - `mise run ci:package` passes, with both installers.
  - The selftest above.
  - A wrong `--version` (simulated by passing a `dist` whose wheel was renamed to another version) fails `smoke`
    with the mismatch message. The implementer checks this by hand once and reports it; it is not kept as a test.
- **Deliverable:** every PR touching the package, and the weekly backstop, proves that the wheel installs and works
  in a clean container with both installers.
- **Validation:**
  - `mise run ci:package > /tmp/claude-…/package.log 2>&1` (never piped);
  - `mise run ci:selftest`, `mise run ci`, `mise run check`;
  - `uv run --no-sync pre-commit run --all-files`.
- **Doc updates:**
  - `.dagger/README.md`: rewrite the Functions table with `dist`, `smoke` and `package`.
  - `AGENTS.md` Commands: add `mise run ci:package`.
  - `AGENTS.md` CI: the lane list, if one is spelled out.
  - Root `README.md` "Everyday commands" table: add `mise run ci:package`.
  - `CONTRIBUTING.md` step 4: packaging changes also run `mise run ci:package`.

### Stage 5: The release pipeline

- **Contract:**
  - **CHANGELOG format** (the parser's contract): sections start at lines `## [Unreleased]` or
    `## [<version>] - YYYY-MM-DD`. A section ends at the next `## [` line, or at the link-reference block or the end
    of the file.
  - **`RepoCi.release_notes(source, version: str) -> str`.**
    - It returns the stripped body of `CHANGELOG.md`'s `## [<version>] - <date>` section.
    - It raises `CHANGELOG.md has no section for <version>` when there is none, or
      `CHANGELOG.md's section for <version> is empty`.
  - **`RepoCi.release(source, tag: str) -> dagger.Directory`.**
    - `tag` must be `v<project version>` exactly, or it raises
      `tag <tag> does not match the project version <version>`.
    - It then runs `release_notes`, `dist` and `smoke`, in that order, and fails at the first failure.
    - It returns `dist/` (the two files) and `notes.md`.
  - **`RepoCi.published(tag: str, dist: dagger.Directory, index: str = 'pypi') -> str`.** It needs no source.
    - It polls `https://pypi.org/pypi/yandex-disk-rescue/<version>/json` for up to 10 minutes, until both file
      names are listed with SHA-256 digests equal to the local files.
    - In a clean container it then installs `uv tool install yandex-disk-rescue==<version>` by name, retried while
      the simple index lags, within the same budget.
    - It asserts `--version`.
    - **[option (a) only]** `index='testpypi'` polls `test.pypi.org` instead, and installs by the wheel's file URL
      from that JSON, so dependencies come from PyPI and never from TestPyPI. Under (b) the parameter does not
      exist.
    - Every exec carries a fresh cache-busting value, and a timeout raises with what was last seen.
  - **mise tasks:**
    - `release:dist`: takes a `tag` argument and runs `dagger call release --tag=<tag> export --path=release --wipe`.
      `--wipe` means a rehearsal never leaves stale wheels. The result is `release/dist/` (exactly the two files)
      and `release/notes.md`.
    - `release:verify`: takes `tag` (and under (a) `index`) and runs `dagger call published --dist=release/dist …`.
  - **`.gitignore`:** add `release/`.
  - **`.github/workflows/release.yml`:**
    - **Top level:** `on: push: tags: ['v*']`, `permissions: {}`, and
      `concurrency: {group: release-${{ github.ref }}, cancel-in-progress: false}`. The workflow-level `env` sets
      `MISE_TRUSTED_CONFIG_PATHS: ${{ github.workspace }}` and `TAG: ${{ github.ref_name }}`. `run:` scripts read
      `$TAG` and other values only through `env:`, never as `${{ }}`.
    - **Checkout:** every `actions/checkout` has `persist-credentials: false`.
    - **mise:** every `jdx/mise-action` has `install_args: dagger` and `cache: false`. No job gets
      `DAGGER_CLOUD_TOKEN` or any other secret. All actions are SHA-pinned, and zizmor and actionlint are clean.
    - **One artifact:** `dist` uploads the folder `release/` as the artifact `release`. Every consumer downloads it
      with `actions/download-artifact` and `name: release, path: release`, so the paths are always
      `release/dist/*` and `release/notes.md`.
    - **The publishing jobs run exactly two steps:** `actions/download-artifact`, then
      `pypa/gh-action-pypi-publish` with `packages-dir: release/dist/`. That is the contract for the jobs that hold
      `id-token: write`: no checkout, no mise and no repository code.
    - Jobs:

      | Job | `needs` | `permissions` | Steps / env |
      |---|---|---|---|
      | `dist` | none | `contents: read` | checkout (`fetch-depth: 0`) → `git merge-base --is-ancestor "$GITHUB_SHA" origin/main` or fail with a message → mise-action → `mise run release:dist "$TAG"` → a step writing `version=${TAG#v}` to `$GITHUB_OUTPUT` → upload artifact `release` (`path: release/`, `if-no-files-found: error`). `outputs: version` from that step. |
      | `publish-testpypi` **[option (a) only]** | `dist` | `id-token: write` | `environment: {name: testpypi, url: https://test.pypi.org/project/yandex-disk-rescue/${{ needs.dist.outputs.version }}/}`. The two steps, the publish with `repository-url: https://test.pypi.org/legacy/`. |
      | `verify-testpypi` **[option (a) only]** | `publish-testpypi` | `contents: read` | checkout → mise-action → download `release` → `mise run release:verify "$TAG" testpypi`. |
      | `publish-pypi` | `dist`, and under (a) also `verify-testpypi` | `id-token: write` | `environment: {name: pypi, url: https://pypi.org/project/yandex-disk-rescue/${{ needs.dist.outputs.version }}/}`. The two steps, the publish to the default repository. |
      | `verify-pypi` | `publish-pypi` | `contents: read` | checkout → mise-action → download `release` → `mise run release:verify "$TAG"` (under (a), `… "$TAG" pypi`). |
      | `github-release` | `dist`, `verify-pypi` | `contents: write` | download `release` → `gh release create "$TAG" release/dist/* --repo "$GITHUB_REPOSITORY" --verify-tag --title "yandex-disk-rescue $VERSION" --notes-file release/notes.md`, plus `--prerelease` when `$VERSION` has an `a`, `b` or `rc` segment. Env: `GH_TOKEN: ${{ github.token }}`, `VERSION: ${{ needs.dist.outputs.version }}`. No checkout: `--repo` names the repository and `--verify-tag` checks the tag on GitHub. |

  - `.dagger/tests` gains three tests, each feeding a synthetic `dag.directory()` source where it needs one, and adds
    them to `all`:
    - `release_notes_returns_the_section`;
    - `release_notes_refuses_a_missing_or_empty_section`;
    - `release_refuses_a_tag_that_is_not_the_project_version`.
  - New `docs/github-ruleset-tags.json` (Decision 13).
- **Behaviour to lock:**
  - The three selftests.
  - `mise run release:dist v0.1.0` fails with `CHANGELOG.md has no section for 0.1.0`, since only `[Unreleased]`
    exists yet.
  - `mise run release:dist v9.9.9` fails with the tag mismatch.
  - The success path is locked in Stage 6. `published` is exercised for the first time by the first real tag's run
    (see Risks).
- **Deliverable:** a tag-triggered, secret-free release with a manual gate. Locally it rehearses up to
  the upload.
- **Validation:** `mise run ci:selftest`, the two failing `release:dist` calls above (their output in the report),
  `uv run --no-sync pre-commit run --all-files` (actionlint and zizmor over `release.yml`), `mise run check`.
- **Doc updates:**
  - `CONTRIBUTING.md`, a new "Releasing" section (current state):
    1. The SemVer scope of Decision 12.
    2. A release PR `chore: release X.Y.Z`: `uv version --package yandex-disk-rescue X.Y.Z`, then move
       `[Unreleased]` to `## [X.Y.Z] - <date>`. The date is the day the tag is meant to be pushed. If tagging slips
       past it, the release PR updates the date before it merges, or a one-line PR does before the tag.
       Releasing a pre-release's content as final renames its heading (`## [1.0.0rc1]` → `## [1.0.0] - <date>`).
    3. Optionally rehearse with `mise run release:dist vX.Y.Z`.
    4. After the merge, `git tag -a vX.Y.Z -m "yandex-disk-rescue X.Y.Z" <merge sha>` and
       `git push origin vX.Y.Z`.
    5. Approve the `pypi` deployment. **[option (a) only]** Check the TestPyPI page first.
    6. What to do when a job fails: re-run failed jobs reuses the same artifact and the tagged commit's code, so it
       fixes only transient failures. A code bug in the release pipeline, or a version already uploaded to an index,
       calls for a fix on `main` and the next version. A version left without a GitHub Release gets one by hand:
       `gh release create vX.Y.Z --notes-file release/notes.md --verify-tag` with the files from `release/dist/`.
  - `docs/github-setup.md`, a new "Releases" section with the PyPI pending publisher (and **[option (a) only]** the
    TestPyPI one), the environments and the tag ruleset import: the exact fields listed under "User's manual steps" below.
  - `AGENTS.md`:
    - CI section: a paragraph on `release.yml` and the Decision 8 exception (publishing uses the PyPI action with
      OIDC; everything else is Dagger behind `mise run release:*`).
    - Commands: `mise run release:dist <tag>` and `mise run release:verify <tag> <index>`.
    - Git: "Releases: see CONTRIBUTING.md", and the `[Unreleased]` rule of Decision 12.
  - `.github/pull_request_template.md`: a checkbox "CHANGELOG `[Unreleased]` updated for user-visible changes".
  - `.dagger/README.md` Functions table: `release_notes`, `release` and `published`.

### Stage 6: The first release: version, CHANGELOG entry, README install section

- **Contract:**
  - **The version is `V`:** `1.0.0` under option (a), `1.0.0rc1` under option (b) (Decision 1). It is set with
    `uv version --package yandex-disk-rescue V`, which updates the pyproject and `uv.lock`.
  - `CHANGELOG.md`: `[Unreleased]` is emptied into `## [V] - <date>`, the date the user means to push the tag (ask
    main; default the stage's date). A new empty `## [Unreleased]` goes above it.
  - Root `README.md:866`, the sample log line `download started: version 0.1.0, …`, shows `V`.
  - Root `README.md` (Decision 14), every section current state:
    - **Status banner.** It describes 1.0, with no "early development" and no "further features are being built".
    - **`## Installation`.**
      - `uv tool install yandex-disk-rescue`: uv fetches Python 3.14 when the system has none.
      - `pipx install yandex-disk-rescue`, with `--python python3.14` when the default Python is older.
      - Check it with `yandex-disk-rescue --version`.
      - Upgrade with `uv tool upgrade yandex-disk-rescue` or `pipx upgrade yandex-disk-rescue`.
      - Uninstall, noting that the token, config, manifest and logs stay in their XDG directories until removed.
    - **`## Quick start`.** The user flow: install, `auth login`, `download disk:/ /mnt/backup` (interrupt and
      rerun), `verify /mnt/backup`.
    - **`## Development`.** Today's devcontainer/mise steps, Everyday commands, Layout and How it fits together,
      all as subsections.
    - The `systemd`/cron examples' `%h/.local/bin/yandex-disk-rescue` already matches a `uv tool` or pipx install;
      a sentence in Installation says that is where both put it.
  - `PYPI.md` is checked against the final README wording (install flags, Python note).
- **Behaviour to lock:**
  - The whole suite passes at `V`. The version literals in `test_output.py` became `__version__` in Stage 2, and
    the `rg` sweep below proves no other test or doc literal is left.
  - `mise run release:dist vV` succeeds. `release/dist/` holds exactly `yandex_disk_rescue-V.tar.gz` and
    `yandex_disk_rescue-V-py3-none-any.whl`, `release/notes.md` equals the CHANGELOG's `V` body, and both installer
    smokes pass.
  - `mise run release:dist v9.9.9` fails with the mismatch.
- **Deliverable:** `main` after the merge is ready to tag `vV`.
- **Validation:** `mise run check`, `mise run ci`, `mise run ci:package`, `mise run ci:selftest`,
  `mise run release:dist vV` (output to a file), `uv run --no-sync pre-commit run --all-files`, and
  `rg -n "early development|Further features"` over README.md, yandex-disk-rescue/, docs/github-setup.md,
  CONTRIBUTING.md and AGENTS.md with no hits, and `rg -n '0\.1\.0' --glob '!uv.lock' --glob '!docs/plans/**'`
  over the whole repo with no hits other than the unpublished projects' own `version = "0.1.0"`: the workspace
  root `pyproject.toml`, `.dagger/pyproject.toml` and `.dagger/tests/pyproject.toml`.
- **Doc updates:** root `README.md` (above), `CHANGELOG.md`, `PYPI.md`, and `yandex-disk-rescue/README.md` (its
  `uv run …` examples stay, as they are the developer view; nothing else changes).

## User's manual steps (cannot be automated; needed before the first tag)

**PyPI** (<https://pypi.org>, an account with 2FA):

1. Go to **Your account → Publishing → Add a new pending publisher → GitHub**.
2. Fill in:
   - PyPI Project Name `yandex-disk-rescue`
   - Owner `azorej`
   - Repository name `yandex-disk-rescue`
   - Workflow name `release.yml`
   - Environment name `pypi`

**TestPyPI [option (a) only]** (<https://test.pypi.org>, a separate account, also with 2FA): the same form, with
Environment name **`testpypi`**.

**GitHub** (repository Settings):

1. **[option (a) only] Environments → New environment `testpypi`.** Under Deployment branches and tags, choose "Selected branches and
   tags" and add a tag rule `v*`. No reviewers.
2. **Environments → New environment `pypi`.**
   - Required reviewers: `azorej`.
   - **Untick "Prevent self-review"**: as the sole maintainer you must be able to approve your own run.
   - Deployment tag rule `v*`.
3. **Rules → Rulesets → New ruleset → Import a ruleset** with `docs/github-ruleset-tags.json`. It protects `v*` tags.
4. **Recommended, already documented but not active today:** import `docs/github-ruleset-main.json`. `main` is
   currently unprotected (the rulesets API returns `[]` and branch protection returns 404).
5. Optional: **Actions → General → Require actions to be pinned to a full-length commit SHA**, once Stage 1 has
   merged.

**The release itself (after the PR merges):**

1. Tag and push: `git tag -a vV -m "yandex-disk-rescue V" <merge sha>`, then `git push origin vV`, with `V` being
   `1.0.0` under (a) or `1.0.0rc1` under (b). Main can do this on the user's word.
2. **[option (a) only]** In the Actions run, check <https://test.pypi.org/project/yandex-disk-rescue/1.0.0/>: the
   README renders and the files are there.
3. **Review deployments → approve `pypi`.**
4. The run's `verify-pypi` job checks the install by version.
   - Under (a), on a clean machine `uv tool install yandex-disk-rescue` and `yandex-disk-rescue --version` print
     `yandex-disk-rescue 1.0.0`.
   - Under (b), check <https://pypi.org/project/yandex-disk-rescue/1.0.0rc1/>. Then a release PR
     (`uv version --package yandex-disk-rescue 1.0.0`, the CHANGELOG heading renamed to `## [1.0.0] - <date>`),
     merge, tag `v1.0.0`, approve, and run the same `uv tool install` check.
5. Close #7 and #9.

## Discovered issues

- **Fix in this PR:**
  - The PyPI long description would be the developer README (Stage 2, `PYPI.md`).
  - Neither distribution carries the license file (Stage 2).
  - No classifiers, keywords or project URLs (Stage 2).
  - The build backend is not pinned by anything (Stage 2, uv_build).
  - zizmor's 10 findings on `ci.yml`, tag-pinned actions and persisted checkout credentials (Stage 1).
  - The root README's "Quick start" is developer-only and its status banner predates 1.0 (Stage 6).
- **Filed as issues:**
  - #23: identify the tool in the `User-Agent` of its HTTP requests (the default is `python-httpx/…`).
- **Surfaced to the user (a setting, not code):** `main` has no active ruleset or branch protection, although
  `docs/github-setup.md` documents one. See manual step 4.

## Risks / open questions

- **The name is not reserved.** PyPI can still refuse `yandex-disk-rescue` at the first upload, for example under
  its name-similarity rules. A pending publisher does not reserve the name. A refusal would mean renaming the
  package (the command name could stay). The risk is low: no similar project exists.
- **TestPyPI burns a version [option (a)].** A version uploaded to TestPyPI can never be re-uploaded with different bytes. If a
  later job exposes a defect that needs a rebuild, that version is spent on TestPyPI, though not on PyPI, and the
  fix ships as the next patch. Mitigation: the `dist` job runs the full install smoke before any upload. Re-running
  failed jobs reuses the same artifact. Option (b) avoids this, since a burnt rc number costs nothing.
- **`published` first runs for real.** The selftests cannot exercise it without a published version. Its first
  execution is the first tag's run (an rc under (b), so a bug there costs only an rc number). A bug there fails a verify job after an upload. Re-running a job runs the workflow
  and the Dagger module of the tagged commit, so a re-run only covers transient failures: a code bug in `published`
  is fixed on `main` and shipped with a new tag and version (under (b), the next rc). Until then that version has no
  GitHub Release; CONTRIBUTING gives the manual `gh release create vX.Y.Z --notes-file release/notes.md` fallback.
- **Fresh resolution can fail a PR for someone else's release.** It fails the `package` lane on a PR when a
  third-party release breaks the installed tool, by design (Decision 10). The fix is a bound or a code fix, never a
  retry.
- **uv_build range drift.** When `mise.toml` moves uv to 0.14, the `<0.14` bound must move in the same commit, both
  in the member and in `.dagger/pyproject.toml`. Otherwise uv silently fetches an older uv_build. The Versions table
  row names both.
- **pipx needs Python 3.14.** pipx users need a Python 3.14 interpreter (`requires-python >=3.14`). The smoke runs
  on the image's 3.14, while a distro with an older default Python needs `--python`. The README says so, and
  lowering the floor is a non-goal.
- **Self-approval.** Approving one's own deployment requires "Prevent self-review" to be off (manual step 2). Left
  on, the release stops before PyPI forever.
