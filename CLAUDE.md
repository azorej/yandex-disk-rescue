# CLAUDE.md

All project rules, commands and conventions live in [`AGENTS.md`](./AGENTS.md)
— **read it first**. Claude Code loads this file automatically; the notes
below are the few things specific to Claude.

@AGENTS.md

## Claude-specific notes

- **Skills** (`.claude/skills/` → `.agents/skills/`): `/writing-development-tickets`,
  `/executing-development-tickets <N>`, `/rules-review`. Don't invent other
  project skill names. Built-in `/code-review` (correctness bugs) and
  `/rules-review` (this repo's rules) complement each other.
- **Subagents** (`.claude/agents/`): `system-architect-planner`, `plan-critic`,
  `plan-stage-implementer`, `branch-code-reviewer` — launched by the skills
  above; you rarely need to call them directly.
- **Hooks** (`.claude/settings.json`): every edited `.py` file is ruff-fixed and
  formatted automatically; unfixable lint findings come back to you — fix them.
- **Permissions:** `.env` is unreadable and lock files are not editable by
  design — use `uv add` / `mise run lock`.
- Personal overrides go in `.claude/settings.local.json` and `CLAUDE.local.md`
  (both git-ignored).
