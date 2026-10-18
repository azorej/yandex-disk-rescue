"""CI pipeline for the repository.

Every check runs in a container built from the committed `uv.lock`, so a
green `dagger call check` locally means a green CI run on GitHub.
"""

import asyncio
import datetime
from dataclasses import dataclass
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
    'dist',
    'release',
]

PACKAGE = 'yandex-disk-rescue'
DIST_NAME = PACKAGE.replace('-', '_')
# `uv build` writes this into every out-dir; it is not a distribution.
BUILD_GITIGNORE = '.gitignore'
DIST_DIR = '/dist'
SMOKE_USER = 'smoke'
SMOKE_HOME = f'/home/{SMOKE_USER}'
SMOKE_TOOLS = '/opt/smoke-tools'
# UV_IMAGE sets UV_TOOL_BIN_DIR=/usr/local/bin; the smoke user installs where an ordinary user's uv does.
SMOKE_UV_TOOL_BIN_DIR = f'{SMOKE_HOME}/.local/bin'
SMOKE_CONSTRAINTS = '/smoke/constraints.txt'
SYSTEM_PATH = '/usr/local/bin:/usr/bin:/bin'
PIPX_HOME = f'{SMOKE_HOME}/pipx'
PIPX_BIN_DIR = f'{SMOKE_HOME}/pipx-bin'


@dataclass(frozen=True)
class Installer:
    name: str
    bin_dir: str
    install: list[str]


Source = Annotated[
    dagger.Directory,
    DefaultPath('/'),
    Ignore(SOURCE_IGNORE),
    Doc('Repository root (defaults to the repository the module lives in)'),
]


def installers(wheel_path: str) -> list[Installer]:
    return [
        Installer('uv tool', SMOKE_UV_TOOL_BIN_DIR, ['uv', 'tool', 'install', wheel_path]),
        Installer('pipx', PIPX_BIN_DIR, [f'{SMOKE_UV_TOOL_BIN_DIR}/pipx', 'install', wheel_path]),
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

    @function
    async def dist(self, source: Source) -> dagger.Directory:
        """The sdist and the wheel of the package, checked with `twine check --strict`."""
        workspace = self.workspace(source)
        version = (await workspace.with_exec(['uv', 'version', '--package', PACKAGE, '--short']).stdout()).strip()
        expected = {f'{DIST_NAME}-{version}.tar.gz', f'{DIST_NAME}-{version}-py3-none-any.whl'}
        built = workspace.with_exec(['uv', 'build', '--package', PACKAGE, '--no-sources', '--out-dir', DIST_DIR])
        found = set(await built.directory(DIST_DIR).entries()) - {BUILD_GITIGNORE}
        if found != expected:
            raise ValueError(
                f'{DIST_DIR} must hold exactly {sorted(expected)}; '
                f'missing {sorted(expected - found)}, unexpected {sorted(found - expected)}'
            )
        names = sorted(expected)
        await built.with_exec(
            ['uv', 'run', '--no-sync', 'twine', 'check', '--strict', *(f'{DIST_DIR}/{name}' for name in names)]
        ).sync()
        dist = dag.directory()
        for name in names:
            dist = dist.with_file(name, built.file(f'{DIST_DIR}/{name}'))
        return dist

    @function
    async def smoke(
        self,
        source: Source,
        dist: Annotated[dagger.Directory | None, Doc('Output of `dist` (built from `source` when omitted)')] = None,
    ) -> str:
        """Install the wheel with `uv tool` and with `pipx` in clean containers and run the installed-CLI tests."""
        if dist is None:
            dist = await self.dist(source)
        wheels = [name for name in await dist.entries() if name.endswith('.whl')]
        if len(wheels) != 1:
            raise ValueError(f'dist must hold exactly one wheel, found {wheels}')
        wheel = wheels[0]
        wheel_version = wheel.split('-')[1]
        tests = source.directory(f'{PACKAGE}/tests')
        clean = (
            dag.container()
            .from_(UV_IMAGE)
            .with_env_variable('UV_PYTHON_DOWNLOADS', 'never')
            .with_exec(['useradd', '--create-home', SMOKE_USER])
            .with_file(SMOKE_CONSTRAINTS, self._dev_constraints(source))
            .with_exec(['uv', 'venv', SMOKE_TOOLS])
            .with_exec(
                ['uv', 'pip', 'install', '--python', SMOKE_TOOLS, 'pytest', 'pytest-timeout', '-c', SMOKE_CONSTRAINTS]
            )
            .with_file(f'/smoke/{wheel}', dist.file(wheel))
            .with_directory('/smoke/tests/installed', tests.directory('installed'))
            .with_file('/smoke/tests/fake_disk_server.py', tests.file('fake_disk_server.py'))
            .with_user(SMOKE_USER)
            .with_workdir(SMOKE_HOME)
            .with_env_variable('UV_TOOL_BIN_DIR', SMOKE_UV_TOOL_BIN_DIR)
            .with_env_variable('PIPX_HOME', PIPX_HOME)
            .with_env_variable('PIPX_BIN_DIR', PIPX_BIN_DIR)
            # pipx picks uv as its backend whenever uv is on PATH, as it is in UV_IMAGE; pip is what a pipx without uv
            # uses, and the `uv tool` run already covers uv.
            .with_env_variable('PIPX_DEFAULT_BACKEND', 'pip')
            .with_exec(['uv', 'tool', 'install', 'pipx', '-c', SMOKE_CONSTRAINTS])
        )
        reports = await asyncio.gather(
            *(self._smoke_one(clean, installer, wheel_version) for installer in installers(f'/smoke/{wheel}'))
        )
        return '\n'.join(reports)

    def _dev_constraints(self, source: dagger.Directory) -> dagger.File:
        """Every package of the `dev` group, transitive ones included, pinned at its `uv.lock` version."""
        return (
            self.workspace(source)
            .with_exec(
                [
                    'uv',
                    'export',
                    '--frozen',
                    '--only-group',
                    'dev',
                    '--no-emit-workspace',
                    '--no-hashes',
                    '--no-header',
                    '--no-annotate',
                    '--output-file',
                    SMOKE_CONSTRAINTS,
                ]
            )
            .file(SMOKE_CONSTRAINTS)
        )

    async def _smoke_one(self, clean: dagger.Container, installer: Installer, wheel_version: str) -> str:
        program = f'{installer.bin_dir}/{PACKAGE}'
        try:
            installed = (
                clean.with_env_variable('SMOKE_RESOLVE_DATE', datetime.datetime.now(datetime.UTC).date().isoformat())
                .with_exec(installer.install)
                .without_env_variable('SMOKE_RESOLVE_DATE')
                .with_env_variable('PATH', f'{installer.bin_dir}:{SYSTEM_PATH}')
                .with_env_variable('PYTHONPATH', '/smoke/tests')
            )
            printed = await installed.with_exec([program, '--version']).stdout()
            if printed != f'{PACKAGE} {wheel_version}\n':
                raise ValueError(
                    f'{installer.name}: `{PACKAGE} --version` printed {printed!r}, expected {PACKAGE} {wheel_version}'
                )
            on_path = (await installed.with_exec(['sh', '-c', f'command -v {PACKAGE}']).stdout()).strip()
            if on_path != program:
                raise ValueError(f'{installer.name}: `{PACKAGE}` on PATH is {on_path!r}, expected {program!r}')
            tests = await installed.with_exec(
                [
                    f'{SMOKE_TOOLS}/bin/pytest',
                    '/smoke/tests/installed',
                    '-c',
                    '/dev/null',
                    '--rootdir',
                    '/smoke',
                    '-p',
                    'no:cacheprovider',
                    '--import-mode=importlib',
                    '--timeout',
                    '120',
                    '-v',
                    '--tb=short',
                ]
            ).stdout()
        except dagger.ExecError as error:
            raise RuntimeError(
                f'{installer.name}: `{" ".join(error.command)}` exited {error.exit_code}\n{error.stdout}\n{error.stderr}'
            ) from error
        return f'== {installer.name}: {printed.strip()} at {program}\n{tests}'

    @function
    async def package(self, source: Source) -> str:
        """Build and check the distributions, then install-smoke the wheel with both installers."""
        return await self.smoke(source, await self.dist(source))
