# scripts/

Standalone helper scripts that are **not** part of the everyday dev loop
(those are mise tasks — see `mise.toml` and [AGENTS.md](../AGENTS.md#tasks-and-scripts)).

| Script | Purpose |
|---|---|
| `common.sh` | Shared logging/assert helpers; sourced by every script |
| `generate_github_deploy_key.sh` | Generate an SSH deploy key for workflows that push (see [docs/using-this-template.md](../docs/using-this-template.md)) |

## Template for new scripts

```bash
#!/bin/bash

# STANDARD PREAMBLE: BEGIN (do not edit)

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source "${SCRIPT_DIR}/common.sh"

# STANDARD PREAMBLE: END (do not edit)

usage () {
    echo 'Usage: $0 -a|--arg1 <ARGUMENT1> <POSITIONAL_ARG1>'
}

POSITIONAL_ARGS=()

while [[ $# -gt 0 ]]; do
  case $1 in
    -a|--arg1)
      ARGUMENT1="$2"
      shift
      ;;
    -*|--*)
      echo "Unknown option $1"
      usage
      exit 1
      ;;
    *)
      POSITIONAL_ARGS+=("$1") # save positional arg
      ;;
  esac
  shift
done

ARGS_COUNT="${#POSITIONAL_ARGS[@]}"
check_variable "ARGUMENT1" "-a is required. Usage: $(usage)"

if [[ "$ARGS_COUNT" -ne 1 ]]; then
    echo "Invalid number of arguments: $ARGS_COUNT"
    usage
    exit 1
fi

set -- "${POSITIONAL_ARGS[@]}"

POSITIONAL_ARG1="$1"
```
