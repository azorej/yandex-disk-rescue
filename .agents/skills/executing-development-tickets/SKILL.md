---
name: executing-development-tickets
description: >
  Drive a GitHub issue end-to-end. Main orchestrates: architect plans, critic
  pressure-tests the plan, user approves, stage implementers execute, reviewer
  reviews, main opens the PR. Single entry point.
  Triggers on: "execute issue #N", "implement issue N",
  "/executing-development-tickets <N>", "let's do #N", "build issue N".
---

# Executing Development Tickets

Main-process orchestrator that takes a GitHub issue and drives it to a reviewed
PR. **Main owns orchestration.** Subagents are workers, not orchestrators — they
do their narrow job and return.

Four workers (`.claude/agents/`), each launched by main:

1. `system-architect-planner` — studies the repo, reproduces current behaviour,
   writes a staged plan, returns it. Does NOT execute it.
2. `plan-critic` — adversarially critiques the plan before the user sees it,
   returns a verdict. Does NOT edit the plan.
3. `plan-stage-implementer` — implements one stage in a fresh context, returns
   a structured report.
4. `branch-code-reviewer` — reviews the whole branch against `AGENTS.md` and
   `.agents/skills/rules-review/SKILL.md`, returns findings.

Main is responsible for: target confirmation, critique-loop mediation, the plan
approval gate, branch creation, the stage dispatch loop, drift handling,
reviewer dispatch, fix dispatch, and opening the PR.

## Invocation

```
/executing-development-tickets <issue-number>
```

or natural language: "execute issue #12", "let's do #14".

The issue lives in this repository — resolve it with
`gh repo view --json nameWithOwner -q .nameWithOwner`. If the user means a
different repo, ask.

## Workflow

### Step 1 — Fetch the issue

`gh issue view <N> --json title,body,labels,state` (or GitHub MCP `issue_read`).
Capture title, body, labels.

If the issue is closed or already has a linked PR, stop and ask the user how to
proceed.

### Step 2 — Confirm the target

Show the user a one-paragraph restatement of what the issue asks for and ask:

> "Is this the work you want me to plan and implement?"

Wait for "yes" / a correction / "ask the architect first". Don't proceed silently.

### Step 3 — Launch the architect (planning only)

Agent tool, `subagent_type=system-architect-planner`, `name=architect` (so you
can revise via `SendMessage`). Prompt:

```
Issue #<N> in <owner>/<repo> — <title>

Body:
<verbatim issue body>

Produce a staged plan only. Do NOT execute it — main will dispatch stages.

Drive your standard planning workflow:
1. Read root AGENTS.md, the README.md of every package you touch, and related
   docs/plans/ entries.
2. Reproduce current behaviour locally (mise run test, the targeted test, or the
   CLI via uv run). For a bugfix, capture the failing output so Stage 1 can
   encode it as a test.
3. File any out-of-scope "Discovered issues" on GitHub and reference the numbers
   in the plan.
4. Write the plan to docs/plans/<YYYY-MM-DD>-issue-<N>-<short-name>.md.
5. Return the structured "Handoff to main" block per your spec. Stop there —
   do not create the branch, do not launch other agents, do not open a PR.
```

### Step 4 — Critique loop (architect ↔ critic)

If the architect returned `DISCOVERY-BLOCKER: ...` instead of a plan, relay it to
the user and stop. No critique, no branch, no execution.

Otherwise, pressure-test the plan before the user sees it:

1. **Launch** `plan-critic` with `name=critic` (re-critique rounds go through
   `SendMessage` and keep its context). Prompt:
   ```
   Critique the plan at <docs/plans/...> for issue #<N> (round 1).

   Issue body:
   <verbatim issue body>

   Architect's handoff:
   <verbatim handoff block>

   Apply your critique dimensions. Verify the plan's claims against the repo.
   Return your structured verdict block. Do not edit the plan.
   ```
2. **`APPROVE` / `APPROVE WITH NOTES`** → Step 5, carrying any 🟡 notes into the
   user summary.
3. **`REVISE`** → `SendMessage` the findings verbatim to the architect. The
   architect revises and returns per-finding dispositions (fixed /
   rebutted-with-evidence). `SendMessage` the revision summary + dispositions
   back to the critic for re-critique.
4. **Cap: 3 critique rounds.** On `DEADLOCK` or an unresolved 🔴 at the cap,
   proceed to Step 5 and present the disagreement to the user — both positions,
   one paragraph each. Never bury it, never pick a side silently.
5. **Main enforces protocol, not substance.** Don't argue findings yourself;
   ensure every finding is fixed or explicitly rebutted, and that the critic
   isn't redesigning the plan (overstep — push back).

### Step 5 — Relay the plan, get approval

1. **Read the plan file yourself.** You're about to execute it. Look for gaps: a
   vague contract, a missing test, a back-compat shim, a stage mixing interface
   and implementation, doc updates not named per stage. Push back to the
   architect via `SendMessage` *before* the user sees it.
2. **Surface to the user:** plan path, stage count, one-paragraph summary, critic
   verdict + round count (plus unresolved findings or accepted rebuttals),
   discovery surprises, issues filed, risks. Ask: "Approve, or revise?"
3. **Revise** → `SendMessage` the architect with the feedback. Re-run the critic
   (one round) only if contracts or stages changed materially. Loop until
   approved.
4. **Approve** → continue. For large plans (≥ 4 stages) optionally suggest an
   extra independent review (e.g. Codex, if registered). Don't force it.

### Step 6 — Create the branch

```bash
git status                     # must be clean — if dirty, ask the user first
git fetch origin
git checkout main
git pull --ff-only origin main
git checkout -b <branch-from-plan>
```

### Step 7 — Stage dispatch loop

For each stage, **serially** (the working tree and commit history are shared):

1. **Launch** `plan-stage-implementer`, fresh context per stage. Prompt:
   ```
   Implement Stage <N> of <docs/plans/...>. Full plan path: <docs/plans/...>.
   Stage block (verbatim):

   <paste stage block from plan>

   Contract: the stage is not done until a test locking the behaviour exists,
   exercises real collaborators (CLI runner, filesystem, subprocess, HTTP), and
   passes, and `mise run check` is green. One stage = one commit. Return your
   structured report.
   ```
2. **Validate the report.**
   - Contract matches the stage spec — flag drift in your next user message.
   - Test exists: open it, confirm it exercises behaviour, not mocks.
   - Docs updated in the same commit — `git show --stat HEAD`.
   - Verification ran cleanly (`mise run check`, targeted test).
   - "Discovered issues": fix-in-this-PR (next stage / follow-up) or file it
     (`gh issue create` / MCP `issue_write`).
3. **Drift handling.**
   - **Justified** (the plan was wrong and the implementer caught it): update the
     plan file yourself, show the diff to the user, continue.
   - **Unjustified** (scope expansion, weakened check, suppressed lint, mocks
     where behaviour should be tested): launch a corrective
     `plan-stage-implementer` with explicit revert instructions. Never accept
     drift silently.
4. **Don't dispatch the next stage** until the current one is verified clean. If
   you can't verify, stop and ask the user.

Per-stage cap: more than 2 corrective launches → stop and surface to the user;
the plan likely needs revision by the architect.

### Step 8 — Full verification + branch review

1. Run the full pipeline yourself — the same one GitHub CI runs:
   ```bash
   mise run ci > /tmp/ci.log 2>&1; echo "exit=$?"
   ```
   (Redirect, never pipe — a pipe masks Dagger's exit status.) Red → treat as a
   Blocker and dispatch a fix implementer before review.
2. Launch `branch-code-reviewer`:
   ```
   Review the current branch vs main against AGENTS.md and
   .agents/skills/rules-review/SKILL.md. Plan: <docs/plans/...>.
   Cover branch + staged + unstaged. Return findings in the standard format.
   ```

### Step 9 — Fix Blocker / Major findings

1. Group findings by file / theme.
2. Launch `plan-stage-implementer`:
   ```
   Apply these reviewer corrections to the branch. Each finding has file:line and
   a suggested fix. Update or add tests if behaviour changes. One commit. Return
   your structured report.

   <paste reviewer findings verbatim>
   ```
3. Re-run `mise run ci`, then re-launch `branch-code-reviewer`.
4. Loop until `APPROVE` or `APPROVE WITH NITS`. **Cap: 2 fix rounds.** Same
   Blocker keeps coming back → stop and ask the user.

Minor / Nit findings: surface as optional follow-ups; don't block the PR.

### Step 10 — Open the PR

Use `.github/pull_request_template.md` if present.

```bash
git push -u origin HEAD
gh pr create --title "<concise conventional-commit title>" --body "$(cat <<'EOF'
## Summary
<2–3 bullets from the plan's Goal>

Closes #<N>

## Plan
See docs/plans/<file>

## Discovered issues
- Filed: #<n>, #<m>
- Fixed in this PR: <one line each>

## Test plan
- [x] mise run ci
- <bullets — what to verify post-merge>
EOF
)"
```

### Step 11 — Hand-off

Surface to the user: PR URL, plan file path, issues filed from "Discovered
issues", one-line summary of what shipped.

## Boundaries

- **Main orchestrates.** Architect plans; critic critiques; implementer
  implements one stage; reviewer reviews. A worker that oversteps (architect
  creates a branch, critic edits the plan, implementer launches an agent) is a
  bug — push back via `SendMessage` and report it.
- **Main never writes production code.** All code edits go through
  `plan-stage-implementer`. Main edits the plan file, files issues, runs git
  plumbing.
- **One stage = one commit.** Don't squash or amend after the fact.
- **Serial stages.** The working tree is shared.
- **The approval gate is non-negotiable.** No branch before the user approved the
  plan. The critic's APPROVE is a quality gate, not a substitute for the user.

## Failure modes

| Symptom | Resolution |
|---|---|
| Can't read the issue | Ask for the number; on auth errors ask the user to run `gh auth login`. |
| Architect returns `DISCOVERY-BLOCKER` | Relay to the user, stop. No branch. |
| Critic ↔ architect deadlock after 3 rounds | Present both positions at Step 5; the user arbitrates. |
| Critic raises new findings every round | Goalpost-moving — remind it of the round protocol; if it persists, take surviving 🔴/🟠 to the user and drop the rest. |
| Can't reproduce locally (missing tool, broken env) | Ask the user to run `mise install` / `mise run setup`, or rebuild the devcontainer. Architect retries after. |
| Plan crosses 5+ stages | Acceptable if warranted; surface the count so the user can split the issue. |
| Implementer drifts | Justified → update plan, show diff. Unjustified → corrective launch. Cap 2 per stage. |
| Reviewer repeats the same Blocker | Stop after 2 fix rounds and ask the user. |
| Working tree dirty at Step 6 | Ask before mutating — the changes may be the user's. |

## Anti-patterns

- **Relaying the plan unread.** Read it first; push back on gaps.
- **Skipping the critique loop.** One round on a clean plan is cheap; an
  ungrounded plan discovered at stage 3 is not.
- **Letting the critique loop grind.** 3 rounds max; findings fixed or rebutted.
- **Letting workers orchestrate.**
- **Parallel stages.**
- **Skipping the user approval gate.**
- **Using this skill for `docs:` / `chore:` issues** that don't need staged
  implementation — just edit and commit.
- **Using it for issues already in progress on a branch** — continue manually or
  ask the user.
