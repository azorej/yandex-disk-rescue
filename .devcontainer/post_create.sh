#!/bin/bash
# Runs once when the devcontainer is created (postCreateCommand).
# Usage: .devcontainer/post_create.sh <CONTAINER_USER> <WORKSPACE_PATH>

set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 <CONTAINER_USER> <WORKSPACE_PATH>" >&2
    exit 1
fi
CONTAINER_USER="$1"
WORKSPACE_PATH="$2"

# Named volumes are created root-owned unless the image already had the directory.
sudo chown -R "$CONTAINER_USER":"$CONTAINER_USER" "/home/${CONTAINER_USER}/.cache" "/home/${CONTAINER_USER}/.claude"
# Don't try to change owner for git directories
find "$WORKSPACE_PATH" -mindepth 1 -maxdepth 1 ! -name .git ! -name .github -exec sudo chown -R "$CONTAINER_USER" {} +

git config --global --add safe.directory "$WORKSPACE_PATH"
git config --global core.editor "code --wait"

# Add GitHub to known_hosts to avoid interactive SSH prompts
mkdir -p ~/.ssh
ssh-keyscan -H github.com >> ~/.ssh/known_hosts 2>/dev/null || true

# In Codespaces, use HTTPS (with the Codespaces token) instead of SSH for GitHub
if [[ "${CODESPACES:-false}" == "true" ]]; then
    git config --global url."https://github.com/".insteadOf "git@github.com:"
fi

cd "$WORKSPACE_PATH"
mise trust
mise run setup
