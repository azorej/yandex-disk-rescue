# GitHub Setup

One-time repository settings on GitHub.

## Branch protection

**Settings → Rules → Rulesets → New ruleset → Import a ruleset** and pick
[`github-ruleset-main.json`](github-ruleset-main.json). It protects the
default branch: PRs only, one approval, squash merges, resolved threads,
and the **`CI required`** status check (the single aggregate check from
`.github/workflows/ci.yml`). Repository admins can bypass it; add your
teams as bypass actors if needed.

## Releases

[`.github/workflows/release.yml`](../.github/workflows/release.yml) publishes to PyPI with Trusted Publishing
(OIDC), so no token is stored anywhere. It needs this one-time setup before the first tag (the process itself is in
[CONTRIBUTING.md](../CONTRIBUTING.md#releasing)):

1. **PyPI pending publisher.** On <https://pypi.org> (an account with 2FA): **Your account → Publishing → Add a new
   pending publisher → GitHub**, with:
   - PyPI Project Name: `yandex-disk-rescue`
   - Owner: `azorej`
   - Repository name: `yandex-disk-rescue`
   - Workflow name: `release.yml`
   - Environment name: `pypi`

   The first upload turns it into the project's trusted publisher. It does not reserve the name.
2. **GitHub environment `pypi`.** **Settings → Environments → New environment** `pypi`:
   - **Required reviewers:** `azorej`. This is the manual approval before anything reaches PyPI.
   - **Untick "Prevent self-review"**, so the sole maintainer can approve their own run. Left ticked, every release
     stops before PyPI.
   - **Deployment branches and tags:** "Selected branches and tags", with a tag rule `v*`.
3. **Tag ruleset.** **Settings → Rules → Rulesets → New ruleset → Import a ruleset** and pick
   [`github-ruleset-tags.json`](github-ruleset-tags.json). It stops anyone but repository admins from creating,
   moving or deleting a `v*` tag.

## SHA-pinned actions (optional)

Every `uses:` in `.github/workflows/` is pinned to a full commit SHA, and
Dependabot (`.github/dependabot.yml`) proposes updates weekly. To make
GitHub enforce it: **Settings → Actions → General → Require actions to be
pinned to a full-length commit SHA**.

## Dagger Cloud (optional)

For pipeline traces, add a `DAGGER_CLOUD_TOKEN` repository secret
(**Settings → Secrets and variables → Actions**). CI works without it.

## Codespaces prebuilds (optional)

**Settings → Codespaces → Set up prebuild**: branch `main`, trigger
*Configuration change*, and limit regions to the ones your team uses to
keep storage costs down.

## Deploy key for workflows that push (optional)

Pushes made with the default `GITHUB_TOKEN` don't trigger other workflows.
If a workflow needs to push commits that should trigger CI:

1. Run `./scripts/generate_github_deploy_key.sh` — it prints a public and
   a private key.
2. **Settings → Deploy keys → Add deploy key**: paste the public key and
   enable **Allow write access**.
3. **Settings → Secrets and variables → Actions**: add the private key as
   `GA_DEPLOY_KEY`, and use it with `actions/checkout`'s `ssh-key` input.

## Security notes

- Never commit secrets; `.env` is git-ignored and unreadable to Claude Code
  by the shared settings.
- Review repository access, deploy keys and Actions secrets periodically.
