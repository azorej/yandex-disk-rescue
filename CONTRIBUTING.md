# Contributing

Thanks for helping improve Yandex Disk Rescue!

1. Read [AGENTS.md](AGENTS.md) — it holds the rules for humans and agents alike.
2. Open the repo in the devcontainer, or run `mise trust && mise run setup` locally.
3. Branch from `main` and use conventional commit messages (`feat:`, `fix:`, `docs:`, `chore:`).
4. Before pushing: `mise run check`; for CI or Dagger changes also `mise run ci` and `mise run ci:selftest`; for packaging changes (the package's metadata, dependencies or `LICENSE`, the build) also `mise run ci:package`.
5. Open a pull request using the template. Keep docs current in the same PR.

Bugs and ideas are welcome as GitHub issues.

## Releasing

A pushed tag `vX.Y.Z` on a commit of `main` releases version `X.Y.Z` through
[`.github/workflows/release.yml`](.github/workflows/release.yml), with no stored secrets:

1. `dist` checks that the tagged commit is on `main` and that the tag is `v` + the project version, takes the notes
   from that version's CHANGELOG section, builds the sdist and the wheel, runs `twine check --strict`, and
   install-smokes the wheel with `uv tool` and `pipx`.
2. `publish-pypi` waits for your approval of the `pypi` deployment, then uploads those exact files with Trusted
   Publishing.
3. `verify-pypi` waits until PyPI serves both files with the same SHA-256, then installs the version by name.
4. `github-release` creates the GitHub Release with the CHANGELOG notes and the two files (a pre-release for an
   `a`, `b` or `rc` version).

### What a version promises

Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The public interface it covers:

- the commands and their options;
- the exit codes;
- the `--json` report schema (`schema_version`);
- the config file keys and the environment variables;
- the manifest's upgrade path: a newer tool always opens an older manifest. An older tool may refuse a newer one.

A user-visible change adds a line under `## [Unreleased]` in [`CHANGELOG.md`](CHANGELOG.md) in its own PR.

### Cutting a release

1. Open a release PR `chore: release X.Y.Z`:
   - `uv version --package yandex-disk-rescue X.Y.Z` (it updates `uv.lock` too);
   - in `CHANGELOG.md`, move the `[Unreleased]` entries under a new `## [X.Y.Z] - YYYY-MM-DD` heading, and leave an
     empty `## [Unreleased]` above it.

   The date is the day you mean to push the tag. If tagging slips past it, update the date in the release PR before
   it merges, or in a one-line PR before the tag. Releasing a pre-release's content as final renames its heading
   (`## [1.0.0rc1] - …` becomes `## [1.0.0] - <date>`).
2. Optionally rehearse everything up to the upload: `mise run release:dist vX.Y.Z` leaves the checked files in
   `release/dist/` and the notes in `release/notes.md`.
3. After the merge, tag the merge commit and push the tag:

   ```bash
   git tag -a vX.Y.Z -m "yandex-disk-rescue X.Y.Z" <merge sha>
   git push origin vX.Y.Z
   ```

4. In the Actions run, **Review deployments → approve `pypi`**.

The first release is the pre-release `v1.0.0rc1`, which rehearses the whole pipeline on PyPI. Once it is verified,
a release PR sets `1.0.0` and renames its CHANGELOG heading, and the tag `v1.0.0` follows.

### When a job fails

- **Re-run failed jobs** reuses the same artifact and the tagged commit's workflow and code, so it fixes only
  transient failures (a network error, an index that lagged past the wait).
- **A bug in the release pipeline, or a version already uploaded to PyPI,** calls for a fix on `main` and the next
  version. PyPI never accepts different files under a version it has seen.
- **A version on PyPI without a GitHub Release** gets one by hand. Download the run's `release` artifact into
  `release/` (`gh run download <run id> --name release --dir release`), so the attached files are the bytes PyPI
  serves, then:

  ```bash
  gh release create vX.Y.Z release/dist/* --verify-tag --title "yandex-disk-rescue X.Y.Z" \
    --notes-file release/notes.md   # add --prerelease for an a, b or rc version
  ```
