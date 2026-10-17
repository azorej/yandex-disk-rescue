"""Tests for the repo-ci Dagger module. Run with `mise run ci:selftest`."""

import asyncio

from dagger import dag, function, object_type

from tests.helpers import expect_output

EXPECTED_PYTHON = 'Python 3.14'


@object_type
class Tests:
    @function
    async def all(self) -> None:
        """Run every test concurrently."""
        await asyncio.gather(self.base_has_toolchain(), self.workspace_installs_members())

    @function
    async def base_has_toolchain(self) -> None:
        """The base image ships uv and the Python version the project requires."""
        base = dag.repo_ci().base()
        await expect_output(base.with_exec(['uv', '--version']), 'uv ')
        await expect_output(base.with_exec(['python', '--version']), EXPECTED_PYTHON)

    @function
    async def workspace_installs_members(self) -> None:
        """Workspace members and dev tools are importable after the locked sync."""
        workspace = dag.repo_ci().workspace()
        await expect_output(
            workspace.with_exec(['uv', 'run', '--no-sync', 'yandex-disk-rescue', '--version']),
            'yandex-disk-rescue ',
        )
        await expect_output(workspace.with_exec(['uv', 'run', '--no-sync', 'ruff', '--version']), 'ruff ')
