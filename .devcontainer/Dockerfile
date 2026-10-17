FROM ubuntu:26.04

SHELL ["/bin/bash", "-o", "pipefail", "-o", "errexit", "-o", "nounset", "-o", "xtrace", "-c"]

USER root

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Etc/UTC

# System packages only. Language toolchains (python, uv, dagger) come from
# mise.toml at container creation, so the image never pins a second copy.
RUN apt-get update -y && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    git \
    git-lfs \
    gnupg \
    jq \
    less \
    locales \
    openssh-client \
    sudo \
    tzdata \
    unzip \
    wget \
    zsh \
    zstd \
    # https://github.com/BurntSushi/ripgrep
    ripgrep \
    # https://github.com/sharkdp/bat
    bat \
    && rm -rf /var/lib/apt/lists/*

# Install GitHub CLI: https://github.com/cli/cli/blob/trunk/docs/install_linux.md
RUN mkdir -p -m 755 /etc/apt/keyrings \
    && wget -nv -O /etc/apt/keyrings/githubcli-archive-keyring.gpg https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update -y \
    && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Set locale
RUN locale-gen en_US.UTF-8
ENV LANG=en_US.UTF-8

# Setup non-root user
ARG USER
ARG USER_UID=1000
ARG USER_GID=$USER_UID

COPY setup_user.sh setup_terminal.sh .zshrc /devcontainer_bootstrap/
RUN /devcontainer_bootstrap/setup_user.sh

USER $USER

ENV PATH="/home/${USER}/.local/bin:${PATH}" \
    # Keep all Claude Code state (incl. .claude.json) in one dir backed by a
    # named volume (see devcontainer.json), so logins survive rebuilds.
    CLAUDE_CONFIG_DIR="/home/${USER}/.claude"

RUN /devcontainer_bootstrap/setup_terminal.sh

# mise (tool versions + tasks): https://mise.jdx.dev/installing-mise.html
# Installed in the user's home so `mise self-update` works without sudo.
ARG MISE_VERSION=v2026.10.12
RUN curl -fsSL https://mise.run | MISE_VERSION="${MISE_VERSION}" sh \
    && mise --version

# Claude Code: https://code.claude.com/docs/en/setup
# Native installer in user context (~/.local/bin/claude -> ~/.local/share/claude),
# so Claude Code's built-in auto-updater and `claude update` work without sudo.
RUN mkdir -p "${CLAUDE_CONFIG_DIR}" \
    && curl -fsSL https://claude.ai/install.sh | bash \
    && rm -rf "${CLAUDE_CONFIG_DIR}/downloads" \
    && claude --version
