#!/bin/bash

# STANDARD PREAMBLE: BEGIN (do not edit)

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
source "${SCRIPT_DIR}/common.sh"

# STANDARD PREAMBLE: END (do not edit)

usage () {
    echo 'Usage: $0 [KEY_NAME]'
    echo ''
    echo 'Generate SSH key pair for GitHub deploy key usage'
    echo ''
    echo 'Arguments:'
    echo '  KEY_NAME    Name for the SSH key (default: deploy_key)'
    echo ''
    echo 'Examples:'
    echo '  $0                    # Generate deploy_key'
    echo '  $0 my_deploy_key      # Generate my_deploy_key'
}

POSITIONAL_ARGS=()

while [[ $# -gt 0 ]]; do
  case $1 in
    -h|--help)
      usage
      exit 0
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

if [[ "$ARGS_COUNT" -gt 1 ]]; then
    echo "Invalid number of arguments: $ARGS_COUNT"
    usage
    exit 1
fi

set -- "${POSITIONAL_ARGS[@]}"

# Set default key name
KEY_NAME="${1:-deploy_key}"

# Generate SSH key
KEY_DIR="$HOME/.ssh"
PRIVATE_KEY="$KEY_DIR/$KEY_NAME"
PUBLIC_KEY="$KEY_DIR/$KEY_NAME.pub"
COMMENT="deploy-key-$(date +%Y%m%d)"

# Create .ssh directory if it doesn't exist
mkdir -p "$KEY_DIR"

# Check if key already exists
if [[ -f "$PRIVATE_KEY" ]]; then
    echo "Error: Key already exists at $PRIVATE_KEY"
    echo "Remove it first or choose a different name"
    exit 1
fi

echo "Generating SSH key: $KEY_NAME"
ssh-keygen -t ed25519 -C "$COMMENT" -f "$PRIVATE_KEY" -N ""

# Set permissions
chmod 600 "$PRIVATE_KEY"
chmod 644 "$PUBLIC_KEY"

echo
echo "✅ SSH key pair generated successfully!"
echo
echo "📋 Public key (add to GitHub Deploy Keys):"
cat "$PUBLIC_KEY"
echo
echo "🔐 Private key (add to GitHub Actions Secrets):"
echo "cat $PRIVATE_KEY"
echo
echo "📖 For setup instructions, see: docs/github-setup.md"
