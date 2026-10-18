# Contributing

Thanks for helping improve Yandex Disk Rescue!

1. Read [AGENTS.md](AGENTS.md) — it holds the rules for humans and agents alike.
2. Open the repo in the devcontainer, or run `mise trust && mise run setup` locally.
3. Branch from `main` and use conventional commit messages (`feat:`, `fix:`, `docs:`, `chore:`).
4. Before pushing: `mise run check`; for CI or Dagger changes also `mise run ci` and `mise run ci:selftest`; for packaging changes (the package's metadata, dependencies or `LICENSE`, the build) also `mise run ci:package`.
5. Open a pull request using the template. Keep docs current in the same PR.

Bugs and ideas are welcome as GitHub issues.
