---
name: rules-review
description: >-
  Review a branch, PR, git range, or path against this repo's AGENTS.md rules —
  surface-failures / fail-fast, behaviour-not-code tests, no back-compat shims,
  docs kept current in the same PR, repo hygiene, uv workspace rules, Dockerfile
  and CI/Dagger hygiene, comment hygiene. Reports concrete violations with
  file:line and a suggested fix, or states explicitly that the diff is clean.
  Triggers on: "review my branch", "rules review", "review against AGENTS",
  "hygiene review", "review this PR against the rules".
---

# Rules Review

Run a focused review of changes against the rules in the root
[`AGENTS.md`](../../../AGENTS.md). **Mechanical hygiene + architectural fit,
not stylistic taste.** This skill is deliberately named `rules-review` so it
does not shadow the harness's built-in `/code-review` (a correctness-bug
hunt); the two are complementary. For security use `/security-review`.

## Default: delegate to the reviewer agent

When invoked as `/rules-review`, **delegate** to the `branch-code-reviewer`
agent via the Agent tool. The agent reads this file as its rules.

1. Determine the scope (Step 1 below).
2. Launch the agent:

   ```
   Agent({
     subagent_type: "branch-code-reviewer",
     description: "Branch rules review",
     prompt: "Review <scope>. Load .agents/skills/rules-review/SKILL.md and
       the root AGENTS.md as rule sources. Report findings in the structured
       format defined in the agent spec."
   })
   ```

3. Relay the agent's report to the user verbatim.
4. Offer to apply suggested fixes (Step 5).

**Run inline** only when the user asks (`/rules-review --inline`) or no
agent harness is available. The sections below are the same checklist the
agent loads.

## Step 1: Determine review scope

Ask only if it's ambiguous.

| User said | Scope |
|---|---|
| `/rules-review` (no args) | Current branch vs `main` (`git diff main...HEAD`) + staged + unstaged |
| `/rules-review <PR#>` | The PR diff (`gh pr diff <PR#>`) |
| `/rules-review <ref>..<ref>` | That git range |
| Names a file or directory | Just that path |

Always reconcile with the working tree (`git status`, `git diff`,
`git diff --cached`) so uncommitted edits don't slip past.

## Step 2: Gather the diff and the rule context

1. Get the diff (`git diff <base>...HEAD`, `git diff`, or `gh pr diff <N>`).
2. List touched files and the area each belongs to: workspace package
   (`example-app/`, `example-library/`, or any new member), `.dagger/`,
   `mise-tasks/` / `mise.toml`, `.devcontainer/`, `.github/`, `docs/`,
   agent config (`.agents/`, `.claude/`, `AGENTS.md`, `CLAUDE.md`).
3. Read the root `AGENTS.md` and any package-level `README.md` / `AGENTS.md`
   for touched packages.
4. Read full files, not just hunks, when a finding hinges on context.
   No reading = no finding.

## Step 3: Run the checks

Walk every category. For each violation record **file:line**, **rule**,
**why it matters**, **concrete fix**. Skip categories with no violations.

### 1. Surface failures explicitly

- No silent fallbacks: no `except Exception: pass`, no
  `except Exception: return None` where the caller needed an error, no
  `subprocess.run(..., check=False)` without an explicit decision, no
  `try/except` that hides a bug rather than handling a known condition.
  Ruff `BLE001` enforces blind-except mechanically; the only escape is a
  per-line `# noqa: BLE001 — <reason>` on a justified best-effort site.
  Flag any `noqa` without a reason.
- No `if x is None: return None` chains that silently mask a missing
  prerequisite — raise.
- No "log and continue" on operations that should be all-or-nothing.
- CLI commands (Typer) exit non-zero on failure (`raise typer.Exit(1)`),
  never print an error and exit 0.
- Functions over ~100 lines → split. Nesting ≥ 3 levels → early return,
  extracted helper, or `continue`.
- No suppressed warnings (`# noqa`, `# type: ignore`, `pytest.mark.skip`)
  without a one-line justification.

### 2. Contracts over backward compatibility

- No `_legacy_*` aliases, `_v2` suffixes, duplicate exports kept "for
  migration", deprecation wrappers, or "rename later" comments. Refactor
  cleanly and delete the old path in the same change.
- Public API of a workspace library lives in its package `__init__.py`
  (`__all__`); other members import from there, not from private modules.

### 3. Testing

- **Test behaviour, not code.** Flag tests that only assert mocked return
  values or `mock.assert_called_with(...)` with no behavioural meaning,
  tests of language features or dataclass/pydantic round-trips, and tests
  that re-implement the code under test.
- **Prefer real collaborators.** Filesystem (`tmp_path`), subprocess, the
  real CLI via `typer.testing.CliRunner`, a real HTTP server — over deep
  mocks that recreate the system.
- **Extend, don't duplicate.** For every NEW test, `rg` the tested
  function / command across `*/tests/` and `.dagger/tests/`. A new test
  re-locking behaviour an existing test already locks is a Major — name
  the existing test; extending it is the fix. Copy-paste variants that
  differ in one value are one `@pytest.mark.parametrize` test. (This is
  the sanctioned exception to the no-scope-creep rule.)
- New test directories must be collected: root `pyproject.toml`
  `[tool.pytest.ini_options] testpaths` covers every workspace member's
  `tests/`, and `mise run ci` runs them.
- New behaviour without any test → Blocker.

### 4. Repository hygiene

- **Root cleanliness.** New root files must be on the allow-list:
  `README.md`, `AGENTS.md`, `CLAUDE.md`, `LICENSE`, `CONTRIBUTING.md`,
  `CHANGELOG.md`, `SECURITY.md`, `pyproject.toml`, `uv.lock`,
  `mise.toml`, `dagger.json`, `.python-version`, and dotfiles. No data
  files, logs, scratch notes, or one-off scripts in the root.
- **Nothing temporary committed:** `.venv/`, `htmlcov/`, `*.log`,
  `.env`, coverage files, editor junk. Flag if accidentally added.
- **Secrets never committed.** Tokens, keys, `.env` contents, private
  keys, credentials in test fixtures → Blocker.
- **Tasks are mise tasks.** Developer-facing automation lives in
  `mise-tasks/` (or `mise.toml`) and is invoked as `mise run <task>`, not
  as a raw script path. Every bash task uses `set -euo pipefail`.
- **Tool versions are pinned in one place** (`mise.toml`, `dagger.json`
  `engineVersion`, `.pre-commit-config.yaml` revs). A second hard-coded
  version of the same tool elsewhere → Major (they will drift).

### 5. uv workspace

- Runtime deps go in the member package's `pyproject.toml`
  `[project] dependencies`; dev tooling goes in the root
  `[dependency-groups] dev`. No `[project.optional-dependencies]` used as
  a dev-deps substitute in members.
- Workspace members reference each other by name with
  `{ workspace = true }` in root `[tool.uv.sources]`, and are listed in
  `[tool.uv.workspace] members`.
- `uv.lock` is **never hand-edited** — it changes only via `uv lock` /
  `uv add` / `mise run lock`. A `pyproject.toml` dependency change without
  a matching `uv.lock` change (or vice versa) → Major.
- Commands run through `uv run` / `mise run`, never raw `python` or
  `.venv/bin/python`.

### 6. Documentation — current state only

**Binding rule (Blocker, not nit):** source-of-truth docs (`AGENTS.md`,
`README.md`, package `README.md`s, `docs/*.md`, `.agents/skills/*/SKILL.md`,
`.claude/agents/*.md`) describe the system *as it is now*.

- A change in user-facing behaviour, developer command, mise task, or
  config requires a docs update **in the same PR**.
- Forbidden phrasings — delete on sight: "previously X, now Y", "this
  used to…", "before the refactor", "until vN.N", "the old approach was",
  migration-history blocks, removed behaviour described in past tense.
- Renamed / moved / deleted something? `rg <old-name>` across the repo —
  every stale mention is fixed in this PR. Skills and agent specs count.
- New developer-facing mise task → listed in `AGENTS.md` "Commands" and
  the README.
- **Exception:** `docs/plans/*.md` are journals and may be historical.

### 7. Comment & docstring hygiene

Default to **no comment**. Comments rot; code moves, names change.

Flag and delete:

- Tautologies restating the signature (`name: str  # REQUIRED, no default`).
- Issue / stage / PR attribution (`# Added in #42 stage 1`) — that's
  `git blame`'s job.
- `AGENTS.md` rule citations at the callsite (`# per Core Rule 3`).
- Future-tense planning (`# stage 3 will derive this`). Either it
  happened (describe current behaviour) or it's a TODO with an issue link.
- Docstrings that restate the function name.
- Module docstrings carrying migration history.

Keep: hidden constraints a refactorer would trip on, load-bearing
invariants the type system can't express, workarounds that look wrong but
aren't, surprising defaults. In doubt: delete.

### 8. Python style

- Ruff config in root `pyproject.toml` governs (single quotes, line
  length 120, absolute imports). Don't duplicate what `mise run lint`
  already flags — concentrate on what humans miss.
- DRY: shared logic moves to a helper in the owning package (or a new
  workspace library), not copy-pasted across members.
- Self-describing names; type hints on public functions.

### 9. Dockerfile hygiene (`.devcontainer/Dockerfile` or any app image)

- Pinned base image (never `latest`); minimal packages;
  `rm -rf /var/lib/apt/lists/*` in the same `RUN` as `apt-get install`.
- Order layers by change frequency; copy dependency manifests before
  source; BuildKit cache mounts for uv.
- App images: multi-stage, non-root runtime user, `COPY` not `ADD`,
  `PYTHONUNBUFFERED=1`, `.dockerignore` keeps the context lean.
- Devcontainer: user-scoped tools (uv, Claude Code) are installed **as the
  non-root user** so their self-update works; system packages as root.

### 10. CI / Dagger hygiene

- CI logic lives in the Dagger module (`.dagger/`); GitHub workflows are
  thin callers. Local `mise run ci` and GitHub CI run the same functions —
  a check that exists in only one of them → Major.
- **Never pipe `dagger` output** into another command (`| tee`, `| grep`):
  a pipe masks Dagger's exit status and a failing run can go green.
  Redirect to a file: `dagger call ... > /tmp/dagger.log 2>&1`.
- Dagger engine version in `dagger.json` (and `.dagger/tests/dagger.json`)
  matches the version pinned in `mise.toml`.
- Workflows: least-privilege `permissions:`, pinned action major versions,
  path filters that actually match the repo layout, and a single aggregate
  required check so skipped lanes don't block merges.
- New checks are added to the Dagger module and covered by
  `mise run ci:selftest`.

## Step 4: Report

```
## Review Summary
<one-paragraph verdict: clean / N findings>

## Findings

### 🔴 Blocker — <category>: <one-line headline>
- File: `path/to/file.py:42`
- Rule: <AGENTS.md rule or section above>
- Why it matters: <one sentence>
- Fix:
  ```diff
  - <bad>
  + <good>
  ```

### 🟠 Major — ...
### 🟡 Minor — ...
### 🔵 Nit — ...

## Clean Categories
- <categories with zero findings, terse>

## Open Questions
- <anything needing the user's judgement>
```

If everything checks out, the report is one line:
**"Reviewed <scope>. No AGENTS.md violations."** Be willing to say it.

## Step 5: Offer to fix

After the report, ask whether to apply any suggested fixes. Apply only
what the user greenlights; never edit during the review pass.

## Anti-patterns in your own review

- **Don't invent findings.** "Could be cleaner" is not a violation.
- **Don't lecture on style.** This skill enforces the listed rules only.
- **Don't review code you didn't read fully.**
- **Don't repeat what ruff / pytest / pre-commit already flag.**
- **Label violation vs suggestion clearly** — severities are not
  interchangeable.
