---
name: writing-development-tickets
description: >
  Use when creating development tickets as GitHub Issues, scoping work for a
  feature area, or brainstorming what tickets are needed for a new area. Covers
  ticket granularity, required labels, and content structure.
  Triggers on: "write a ticket", "create an issue for", "scope this into tickets",
  "what tickets do we need for".
---

# Writing Development Tickets

Tickets are GitHub Issues in **this** repository. Resolve it, never hardcode it:

```bash
gh repo view --json nameWithOwner -q .nameWithOwner   # → <owner>/<repo>
```

Each ticket = one coherent deliverable for a human+AI pair, taking 1–5 days.

## Ticket Granularity

**Good ticket = one coherent feature, independently deliverable, 3–15 implementation steps.**

| Good | Bad (too small) | Bad (too big) |
|------|-----------------|---------------|
| "Config file support for the CLI" | "Add `--config` flag" | "Make the CLI production-ready" (7 streams) |
| "Structured logging" | "Add a logger to `main.py`" | "Build an observability stack" |
| "Retry policy for HTTP client" | "Wrap one call in `tenacity`" | |

**Rule of thumb:** can't explain it in 2 sentences without listing steps → too big.
The deliverable IS a step → too small.

### When to split
- Deliverables span unrelated systems
- Different people could work on parts independently
- One part could ship without the other
- More than ~15 implementation steps

### When NOT to split
- Parts are tightly coupled (interface + implementation + docs of one feature)
- Splitting creates more coordination overhead than the ticket itself
- One part is useless without the other

## GitHub Issue Structure

### Title
Action-oriented, describes the deliverable, conventional-commit prefix:
`feat: Add config file support to yandex-disk-rescue`

### Labels

| Label | Usage |
|-------|-------|
| `feature` | New capability |
| `bug` | Something broken |
| `tech-debt` | Code quality improvement |
| `docs` | Documentation only |
| `P0` / `P1` / `P2` / `P3` | Priority level |

If a label doesn't exist yet, create it (`gh label create <name>`) rather than
dropping it silently — and tell the user you did.

### Issue body

**Required:**
- **Context** (2–3 sentences): what problem, why now. Only what's unique to this deliverable.
- **Deliverables**: checkboxed list of what's true when done.
- **Acceptance criteria**: user-observable verification (not code-level).
- **Design decisions**: chosen approaches with rationale ("X because Y").

**Optional:** scope boundary (what's explicitly OUT), dependencies (other issues),
open questions (for the design phase).

**What does NOT go in a ticket:** file paths, function signatures, module
structure, implementation steps, code snippets or pseudocode.

**Why?** Implementation details rot. The codebase changes between ticket
creation and execution; design happens just-in-time in
`/executing-development-tickets`.

### Issue template

```markdown
## Context
<What problem this solves, why now — 2-3 sentences>

## Deliverables
- [ ] <What's true when done — user-observable>
- [ ] <Another deliverable>

## Acceptance Criteria
- <How to verify this works>
- <Another verification>

## Design Decisions
- **<Decision>**: <Rationale> ("X because Y")

## Out of Scope
- <What is NOT included in this ticket>
```

## Ticket lifecycle

| Status | Meaning | Maps to |
|--------|---------|---------|
| Open | Defined, ready for work | Backlog / Ready |
| In Progress | Implementation underway | Assigned, branch created |
| In Review | PR created | PR linked |
| Closed | Merged or resolved | Done |

## Creating issues

With the GitHub MCP (`issue_write`, `method: create`, `owner`/`repo` from the
command above) or with the CLI:

```bash
gh issue create --title "feat: <description>" --label feature --label P1 \
  --body-file /tmp/issue-body.md
```

Search first (`gh issue list --search "<keywords>"` or the MCP `search_issues`)
to avoid duplicates.

## Creation checklist

- [ ] Ticket is at feature level (not too small, not too big)
- [ ] No duplicate open issue
- [ ] Priority label set (P0–P3)
- [ ] Type label set (feature / bug / tech-debt / docs)
- [ ] Context explains "why now"
- [ ] Deliverables are checkboxed and user-observable
- [ ] Design decisions list chosen approaches with rationale
- [ ] No implementation details leaked in

## Anti-patterns

| Anti-pattern | Problem |
|-------------|---------|
| Implementation plan as ticket | Steps ≠ deliverables. The ticket is the *what*, not the *how* |
| One ticket per file change | Too granular — think in capabilities |
| Epic disguised as ticket | 3+ weeks, 5+ independent deliverables → split it |
| Stale implementation notes | Signatures written two months ago are wrong now |
| Priority mismatch | A P3 ticket in a P0 area means something is off |
