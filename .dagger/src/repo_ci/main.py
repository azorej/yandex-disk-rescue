"""CI pipeline for the repository.

Every check runs in a container built from the committed `uv.lock`, so a
green `dagger call check` locally means a green CI run on GitHub.
"""

import asyncio
from typing import Annotated

import dagger
from dagger import DefaultPath, Doc, Ignore, dag, function, object_type

# Keep the uv version in sync with mise.toml and the Python minor
# version in sync with `requires-python` in pyproject.toml.
UV_IMAGE = 'ghcr.io/astral-sh/uv:0.13.1-python3.14-trixie-slim'

SOURCE_IGNORE = [
    '**/.venv',
    '**/__pycache__',
    '**/.pytest_cache',
    '**/.ruff_cache',
    '.dagger/sdk',
    '.dagger/tests/sdk',
    '.env',
]

Source = Annotated[
    dagger.Directory,
    DefaultPath('/'),
    Ignore(SOURCE_IGNORE),
    Doc('Repository root (defaults to the repository the module lives in)'),
]


@object_type
class RepoCi:
    @function
    def base(self) -> dagger.Container:
        """Base image with uv and Python, plus a persistent uv cache."""
        return (
            dag.container()
            .from_(UV_IMAGE)
            .with_mounted_cache('/root/.cache/uv', dag.cache_volume('uv-cache'))
            .with_env_variable('UV_LINK_MODE', 'copy')
            .with_env_variable('UV_PYTHON_DOWNLOADS', 'never')
        )

    @function
    def workspace(self, source: Source) -> dagger.Container:
        """Base image with the workspace synced from the committed lock file."""
        return (
            self.base()
            .with_workdir('/src')
            # Dependency layer first: it stays cached until a lock or manifest changes.
            .with_directory('/src', source, include=['**/pyproject.toml', 'uv.lock', '**/README.md'])
            .with_exec(['uv', 'sync', '--locked', '--no-install-workspace'])
            .with_directory('/src', source)
            .with_exec(['uv', 'sync', '--locked'])
        )

    @function
    async def lint(self, source: Source) -> str:
        """Ruff lint and format check over the whole repository."""
        return await (
            self.workspace(source)
            .with_exec(['uv', 'run', '--no-sync', 'ruff', 'check', '--output-format=github'])
            .with_exec(['uv', 'run', '--no-sync', 'ruff', 'format', '--check'])
            .stdout()
        )

    @function
    async def test(self, source: Source) -> str:
        """Pytest over every workspace member."""
        return await self.workspace(source).with_exec(['uv', 'run', '--no-sync', 'pytest']).stdout()

    @function
    async def check(self, source: Source) -> str:
        """Everything CI gates on: lint and test, run concurrently."""
        lint, test = await asyncio.gather(self.lint(source), self.test(source))
        return f'{lint}\n{test}'
