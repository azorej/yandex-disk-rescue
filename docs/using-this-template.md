# Using This Template

How to create a new repository from this template and make it your own.

## 1. Create the repository

On GitHub, click **Use this template → Create a new repository**, pick the
owner and name, and create it. Then open it in a devcontainer (VS Code
*Reopen in Container*, or **Code → Codespaces → Create codespace**), or
clone it and run `mise trust && mise run setup` locally.

## 2. Make it yours

Ask your coding agent to do this with you — `AGENTS.md` gives it the context.

- **Packages:** replace `example-app/` and `example-library/` with your own
  packages and update the root `pyproject.toml` (`name`, workspace
  `members`, `[tool.uv.sources]`, the `dev` group, pytest `testpaths`) —
  see *Adding a package* in [AGENTS.md](../AGENTS.md#uv-workspace). Then
  run `uv sync`.
- **Names:** `name` in `pyproject.toml` and `.devcontainer/devcontainer.json`.
- **Docs:** rewrite `README.md`, the *Project Overview* in `AGENTS.md`, and
  delete this file.
- **License:** update the copyright holder in `LICENSE`, or replace it.
- **VS Code:** adjust `.vscode/launch.json` and `.vscode/tasks.json`.
- **Optional:** extra system packages in `.devcontainer/Dockerfile`; a
  `.env.example` entry for each local secret your project needs.

## 3. Configure GitHub

### Branch protection

**Settings → Rules → Rulesets → New ruleset → Import a ruleset** and pick
[`github-ruleset-main.json`](github-ruleset-main.json). It protects the
default branch: PRs only, one approval, squash merges, resolved threads,
and the **`CI required`** status check (the single aggregate check from
`.github/workflows/ci.yml`). Repository admins can bypass it; add your
teams as bypass actors if needed.

### Dagger Cloud (optional)

For pipeline traces, add a `DAGGER_CLOUD_TOKEN` repository secret
(**Settings → Secrets and variables → Actions**). CI works without it.

### Codespaces prebuilds (optional)

**Settings → Codespaces → Set up prebuild**: branch `main`, trigger
*Configuration change*, and limit regions to the ones your team uses to
keep storage costs down.

### Deploy key for workflows that push (optional)

Pushes made with the default `GITHUB_TOKEN` don't trigger other workflows.
If a workflow needs to push commits that should trigger CI:

1. Run `./scripts/generate_github_deploy_key.sh` — it prints a public and
   a private key.
2. **Settings → Deploy keys → Add deploy key**: paste the public key and
   enable **Allow write access**.
3. **Settings → Secrets and variables → Actions**: add the private key as
   `GA_DEPLOY_KEY`, and use it with `actions/checkout`'s `ssh-key` input.

## 4. Verify

- `mise run check` and `mise run ci` pass locally.
- The first PR shows a green **CI required** check.

## Security notes

- Never commit secrets; `.env` is git-ignored and unreadable to Claude Code
  by the shared settings.
- Review repository access, deploy keys and Actions secrets periodically.
