"""CI pipeline for the repository.

Every check runs in a container built from the committed `uv.lock`, so a
green `dagger call check` locally means a green CI run on GitHub.
"""

import asyncio
import datetime
import json
import re
import time
import urllib.error
import urllib.request
import uuid
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
PIPX = f'{SMOKE_UV_TOOL_BIN_DIR}/pipx'
PIPX_BACKEND = 'pip'
CHANGELOG = 'CHANGELOG.md'
LINK_REFERENCE = re.compile(r'^\[[^\]]+\]:\s')
PYPI_RELEASE_JSON = f'https://pypi.org/pypi/{PACKAGE}/{{version}}/json'
PYPI_HTTP_TIMEOUT_SECONDS = 30
PUBLISHED_RETRY_SECONDS = 15


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
        Installer('pipx', PIPX_BIN_DIR, [PIPX, 'install', wheel_path]),
    ]


def dist_names(version: str) -> list[str]:
    return [f'{DIST_NAME}-{version}.tar.gz', f'{DIST_NAME}-{version}-py3-none-any.whl']


def changelog_section(changelog: str, version: str) -> str:
    """The stripped body of the `## [<version>] - YYYY-MM-DD` section, up to the next `## [` or link reference."""
    heading = re.compile(rf'^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$')
    lines = changelog.splitlines()
    starts = [index for index, line in enumerate(lines) if heading.match(line)]
    if not starts:
        raise ValueError(f'{CHANGELOG} has no section for {version}')
    if len(starts) > 1:
        raise ValueError(f'{CHANGELOG} has {len(starts)} sections for {version}')
    body = []
    for line in lines[starts[0] + 1 :]:
        if line.startswith('## [') or LINK_REFERENCE.match(line):
            break
        body.append(line)
    notes = '\n'.join(body).strip()
    if not notes:
        raise ValueError(f"{CHANGELOG}'s section for {version} is empty")
    return notes


def pypi_sha256s(version: str) -> dict[str, str]:
    """File name to SHA-256 of every file PyPI lists for `version`."""
    url = PYPI_RELEASE_JSON.format(version=version)
    with urllib.request.urlopen(url, timeout=PYPI_HTTP_TIMEOUT_SECONDS) as response:
        release = json.load(response)
    return {file['filename']: file['digests']['sha256'] for file in release['urls']}


def clean_container() -> dagger.Container:
    """A fresh `UV_IMAGE` container with no uv cache and an unprivileged smoke user, still running as root."""
    return (
        dag.container()
        .from_(UV_IMAGE)
        .with_env_variable('UV_PYTHON_DOWNLOADS', 'never')
        .with_exec(['useradd', '--create-home', SMOKE_USER])
    )


def as_smoke_user(container: dagger.Container) -> dagger.Container:
    """Run as the smoke user, with `uv tool` installing to its `~/.local/bin`."""
    return (
        container.with_user(SMOKE_USER)
        .with_workdir(SMOKE_HOME)
        .with_env_variable('UV_TOOL_BIN_DIR', SMOKE_UV_TOOL_BIN_DIR)
    )


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
        version = await self._project_version(source)
        expected = set(dist_names(version))
        built = self.workspace(source).with_exec(
            ['uv', 'build', '--package', PACKAGE, '--no-sources', '--out-dir', DIST_DIR]
        )
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
            clean_container()
            .with_file(SMOKE_CONSTRAINTS, self._dev_constraints(source))
            .with_exec(['uv', 'venv', SMOKE_TOOLS])
            .with_exec(
                ['uv', 'pip', 'install', '--python', SMOKE_TOOLS, 'pytest', 'pytest-timeout', '-c', SMOKE_CONSTRAINTS]
            )
            .with_file(f'/smoke/{wheel}', dist.file(wheel))
            .with_directory('/smoke/tests/installed', tests.directory('installed'))
            .with_file('/smoke/tests/fake_disk_server.py', tests.file('fake_disk_server.py'))
            .with_(as_smoke_user)
            .with_env_variable('PIPX_HOME', PIPX_HOME)
            .with_env_variable('PIPX_BIN_DIR', PIPX_BIN_DIR)
            # pipx picks uv as its backend whenever uv is on PATH, as it is in UV_IMAGE; pip is what a pipx without uv
            # uses, and the `uv tool` run already covers uv.
            .with_env_variable('PIPX_DEFAULT_BACKEND', PIPX_BACKEND)
            .with_exec(['uv', 'tool', 'install', 'pipx', '-c', SMOKE_CONSTRAINTS])
        )
        backend = (await clean.with_exec([PIPX, 'environment', '--value', 'PIPX_RESOLVED_BACKEND']).stdout()).strip()
        if backend != PIPX_BACKEND:
            raise ValueError(f'pipx resolved its backend to {backend!r}, expected {PIPX_BACKEND!r}')
        reports = await asyncio.gather(
            *(self._smoke_one(clean, installer, wheel_version) for installer in installers(f'/smoke/{wheel}'))
        )
        return '\n'.join([f'== pipx backend: {backend}', *reports])

    async def _project_version(self, source: dagger.Directory) -> str:
        return (
            await self.workspace(source).with_exec(['uv', 'version', '--package', PACKAGE, '--short']).stdout()
        ).strip()

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

    @function
    async def release_notes(
        self, source: Source, version: Annotated[str, Doc('Project version, without the leading `v`')]
    ) -> str:
        """The body of the `## [<version>] - YYYY-MM-DD` section of CHANGELOG.md."""
        return changelog_section(await source.file(CHANGELOG).contents(), version)

    @function
    async def release(
        self, source: Source, tag: Annotated[str, Doc('The release tag: `v` + the project version')]
    ) -> dagger.Directory:
        """Everything a release uploads, checked: `dist/` (the sdist and the wheel, install-smoked) and `notes.md`."""
        version = await self._project_version(source)
        if tag != f'v{version}':
            raise ValueError(f'tag {tag} does not match the project version {version}')
        notes = await self.release_notes(source, version)
        dist = await self.dist(source)
        await self.smoke(source, dist)
        return dag.directory().with_directory('dist', dist).with_new_file('notes.md', f'{notes}\n')

    @function(cache='never')
    async def published(
        self,
        tag: Annotated[str, Doc('The release tag: `v` + the published version')],
        dist: Annotated[dagger.Directory, Doc('The `dist/` that `release` returned, as uploaded')],
        timeout: Annotated[int, Doc('Seconds to wait for PyPI to serve the files and the version')] = 600,
    ) -> str:
        """Wait until PyPI serves exactly the files of `dist`, then `uv tool install` the version by name and check it."""
        if not tag.startswith('v'):
            raise ValueError(f'tag {tag} must be `v` + the version')
        version = tag.removeprefix('v')
        names = sorted(dist_names(version))
        found = sorted(await dist.entries())
        if found != names:
            raise ValueError(f'dist must hold exactly {names} for tag {tag}, found {found}')
        deadline = time.monotonic() + timeout
        await self._wait_for_pypi_files(version, await self._sha256s(dist, names), deadline, timeout)
        return await self._install_published(version, deadline, timeout)

    async def _sha256s(self, dist: dagger.Directory, names: list[str]) -> dict[str, str]:
        listing = await (
            dag.container()
            .from_(UV_IMAGE)
            .with_directory(DIST_DIR, dist)
            .with_workdir(DIST_DIR)
            .with_exec(['sha256sum', *names])
            .stdout()
        )
        return {name: digest for digest, name in (line.split() for line in listing.splitlines())}

    async def _wait_for_pypi_files(self, version: str, local: dict[str, str], deadline: float, timeout: int) -> None:
        while True:
            try:
                remote = await asyncio.to_thread(pypi_sha256s, version)
            except (urllib.error.URLError, TimeoutError) as error:
                last_seen = f'{PYPI_RELEASE_JSON.format(version=version)}: {error}'
            else:
                for name, digest in local.items():
                    if name in remote and remote[name] != digest:
                        raise ValueError(f'PyPI serves {name} with sha256 {remote[name]}, the local file has {digest}')
                if local.keys() <= remote.keys():
                    return
                last_seen = f'PyPI lists {sorted(remote)}'
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f'PyPI did not list {sorted(local)} for {version} within {timeout}s; last seen: {last_seen}'
                )
            await asyncio.sleep(PUBLISHED_RETRY_SECONDS)

    async def _install_published(self, version: str, deadline: float, timeout: int) -> str:
        requirement = f'{PACKAGE}=={version}'
        user = clean_container().with_(as_smoke_user)
        while True:
            # A fresh value per attempt, so Dagger never answers a retry from its cache.
            installed = user.with_env_variable('PUBLISHED_ATTEMPT', uuid.uuid4().hex).with_exec(
                ['uv', 'tool', 'install', requirement]
            )
            try:
                await installed.sync()
                break
            except dagger.ExecError as error:
                last_seen = error.stderr.strip()
            if time.monotonic() >= deadline:
                raise TimeoutError(f'`uv tool install {requirement}` kept failing for {timeout}s; last: {last_seen}')
            await asyncio.sleep(PUBLISHED_RETRY_SECONDS)
        program = f'{SMOKE_UV_TOOL_BIN_DIR}/{PACKAGE}'
        printed = await installed.with_exec([program, '--version']).stdout()
        if printed != f'{PACKAGE} {version}\n':
            raise ValueError(f'`{PACKAGE} --version` printed {printed!r}, expected {PACKAGE} {version}')
        return f'PyPI serves {PACKAGE} {version} with the local sha256s; `uv tool install {requirement}`: {printed.strip()}'
