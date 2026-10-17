#!/usr/bin/env bash
# PostToolUse hook: ruff-fix and format a Python file right after Claude edits it,
# so the agent sees (and builds on) the same code the pre-commit hook would produce.
# Lint findings ruff can't auto-fix are reported back to Claude (exit 2).

set -euo pipefail

file_path="$(jq -r '.tool_input.file_path // empty')"

case "$file_path" in
    *.py | *.pyi) ;;
    *) exit 0 ;;
esac
[[ -f "$file_path" ]] || exit 0

cd "${CLAUDE_PROJECT_DIR}"
uv run --no-sync ruff format --quiet --force-exclude "$file_path"
if ! output="$(uv run --no-sync ruff check --fix --quiet --force-exclude "$file_path" 2>&1)"; then
    echo "$output" >&2
    exit 2
fi
