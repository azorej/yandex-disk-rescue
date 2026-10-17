# GitHub Setup

One-time repository settings on GitHub.

## Branch protection

**Settings → Rules → Rulesets → New ruleset → Import a ruleset** and pick
[`github-ruleset-main.json`](github-ruleset-main.json). It protects the
default branch: PRs only, one approval, squash merges, resolved threads,
and the **`CI required`** status check (the single aggregate check from
`.github/workflows/ci.yml`). Repository admins can bypass it; add your
teams as bypass actors if needed.

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
