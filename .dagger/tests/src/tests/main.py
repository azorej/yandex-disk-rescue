"""Tests for the repo-ci Dagger module. Run with `mise run ci:selftest`."""

import asyncio
import hashlib
import json

import dagger
from dagger import dag, function, object_type

from tests.helpers import expect_error, expect_output

EXPECTED_PYTHON = 'Python 3.14'
PACKAGE = 'yandex-disk-rescue'
CHANGELOG = """# Changelog

## [Unreleased]

### Added

- Something not yet released.

## [1.1.0] - 2026-11-02

## [1.0.0] - 2026-10-20

### Added

- The first release.

### Fixed

- A bug.

## [1.0.0rc1] - 2026-10-18

- The release candidate.

[1.0.0]: https://example.com/compare/v1.0.0rc1...v1.0.0
"""
INDEX_VERSION = '1.0.0'
INDEX_TAG = f'v{INDEX_VERSION}'
INDEX_SDIST = f'yandex_disk_rescue-{INDEX_VERSION}.tar.gz'
INDEX_WHEEL = f'yandex_disk_rescue-{INDEX_VERSION}-py3-none-any.whl'
INDEX_DIST = {INDEX_SDIST: 'the sdist', INDEX_WHEEL: 'the wheel'}
INDEX_PORT = 8000
# Serves the i-th body listed for a path on its i-th request, and the last one from then on.
INDEX_SERVER = """
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

with open('/srv/bodies.json') as bodies_file:
    BODIES = json.load(bodies_file)
served = {}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        bodies = BODIES.get(self.path)
        if bodies is None:
            self.send_error(404)
            return
        count = served.get(self.path, 0)
        served[self.path] = count + 1
        body = bodies[min(count, len(bodies) - 1)].encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ThreadingHTTPServer(('', PORT), Handler).serve_forever()
""".replace('PORT', str(INDEX_PORT))


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def release_json(files: dict[str, str]) -> str:
    """A PyPI release JSON body listing `files` (name to SHA-256)."""
    return json.dumps({'urls': [{'filename': name, 'digests': {'sha256': digest}} for name, digest in files.items()]})


LOCAL_SHA256S = {name: sha256(content) for name, content in INDEX_DIST.items()}


def index_dist() -> dagger.Directory:
    dist = dag.directory()
    for name, content in INDEX_DIST.items():
        dist = dist.with_new_file(name, content)
    return dist


async def fake_index(bodies: list[str]) -> str:
    """Start an index serving `bodies` in turn for the release JSON of INDEX_VERSION; return its base URL."""
    path = f'/pypi/{PACKAGE}/{INDEX_VERSION}/json'
    service = (
        dag.repo_ci()
        .base()
        .with_new_file('/srv/server.py', INDEX_SERVER)
        .with_new_file('/srv/bodies.json', json.dumps({path: bodies}))
        .with_exposed_port(INDEX_PORT)
        .as_service(args=['python', '/srv/server.py'])
    )
    await service.start()
    return f'{await service.endpoint(scheme="http")}/pypi'


@object_type
class Tests:
    @function
    async def all(self) -> None:
        """Run every test concurrently."""
        await asyncio.gather(
            self.base_has_toolchain(),
            self.workspace_installs_members(),
            self.dist_builds_one_sdist_and_one_wheel(),
            self.release_notes_returns_the_section(),
            self.release_notes_refuses_a_missing_or_empty_section(),
            self.release_refuses_a_tag_that_is_not_the_project_version(),
            self.index_serves_refuses_a_digest_mismatch(),
            self.index_serves_refuses_files_the_dist_lacks(),
            self.index_serves_retries_until_the_index_lists_every_file(),
            self.index_serves_times_out_with_the_last_seen_listing(),
        )

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

    @function
    async def dist_builds_one_sdist_and_one_wheel(self) -> None:
        """`dist` exports exactly the sdist and the wheel named for the workspace's project version."""
        repo_ci = dag.repo_ci()
        version = await repo_ci.workspace().with_exec(['uv', 'version', '--package', PACKAGE, '--short']).stdout()
        dist_name = PACKAGE.replace('-', '_')
        expected = sorted([f'{dist_name}-{version.strip()}.tar.gz', f'{dist_name}-{version.strip()}-py3-none-any.whl'])
        entries = sorted(await repo_ci.dist().entries())
        if entries != expected:
            raise AssertionError(f'expected dist entries {expected}, got {entries}')

    @function
    async def release_notes_returns_the_section(self) -> None:
        """`release_notes` returns one version's stripped body, up to the next section or the link references."""
        source = dag.directory().with_new_file('CHANGELOG.md', CHANGELOG)
        cases = {
            '1.0.0': '### Added\n\n- The first release.\n\n### Fixed\n\n- A bug.',
            '1.0.0rc1': '- The release candidate.',
        }
        for version, expected in cases.items():
            notes = await dag.repo_ci().release_notes(version, source=source)
            if notes != expected:
                raise AssertionError(f'expected the notes of {version} to be {expected!r}, got {notes!r}')

    @function
    async def release_notes_refuses_a_missing_or_empty_section(self) -> None:
        """A version with no dated section, or with an empty one, is an error naming the version."""
        source = dag.directory().with_new_file('CHANGELOG.md', CHANGELOG)
        repo_ci = dag.repo_ci()
        await asyncio.gather(
            expect_error(repo_ci.release_notes('1.2.0', source=source), 'CHANGELOG.md has no section for 1.2.0'),
            expect_error(repo_ci.release_notes('Unreleased', source=source), 'CHANGELOG.md has no section for'),
            expect_error(repo_ci.release_notes('1.1.0', source=source), "CHANGELOG.md's section for 1.1.0 is empty"),
        )

    @function
    async def release_refuses_a_tag_that_is_not_the_project_version(self) -> None:
        """`release` refuses a tag other than `v` + the workspace's project version."""
        repo_ci = dag.repo_ci()
        version = (
            await repo_ci.workspace().with_exec(['uv', 'version', '--package', PACKAGE, '--short']).stdout()
        ).strip()
        await asyncio.gather(
            *(
                expect_error(repo_ci.release(tag).entries(), f'tag {tag} does not match the project version {version}')
                for tag in ('v9.9.9', version, f'v{version}.post1')
            )
        )

    @function
    async def index_serves_refuses_a_digest_mismatch(self) -> None:
        """A file the index serves with another SHA-256 is an error naming the file and both digests."""
        wrong = sha256('another wheel')
        index_url = await fake_index([release_json({**LOCAL_SHA256S, INDEX_WHEEL: wrong})])
        await expect_error(
            dag.repo_ci().index_serves(INDEX_TAG, index_dist(), index_url=index_url),
            f'the index serves {INDEX_WHEEL} with sha256 {wrong}, the local file has {LOCAL_SHA256S[INDEX_WHEEL]}',
        )

    @function
    async def index_serves_refuses_files_the_dist_lacks(self) -> None:
        """A file the index lists beyond the local dist is an error naming it."""
        extra = f'yandex_disk_rescue-{INDEX_VERSION}-py3-none-manylinux_2_17_x86_64.whl'
        index_url = await fake_index([release_json({**LOCAL_SHA256S, extra: sha256('extra')})])
        await expect_error(
            dag.repo_ci().index_serves(INDEX_TAG, index_dist(), index_url=index_url),
            f"the index lists ['{extra}'], which the local dist lacks",
        )

    @function
    async def index_serves_retries_until_the_index_lists_every_file(self) -> None:
        """A body that is not JSON, then a listing missing a file, are retried until the index lists both files."""
        index_url = await fake_index(
            [
                '{"urls": [',
                release_json({INDEX_WHEEL: LOCAL_SHA256S[INDEX_WHEEL]}),
                release_json(LOCAL_SHA256S),
            ]
        )
        served = await dag.repo_ci().index_serves(INDEX_TAG, index_dist(), timeout=120, index_url=index_url)
        expected = f'lists exactly {sorted(INDEX_DIST)} with the local sha256s'
        if expected not in served:
            raise AssertionError(f'expected {expected!r} in {served!r}')

    @function
    async def index_serves_times_out_with_the_last_seen_listing(self) -> None:
        """Past the deadline, the error names the files waited for and what the index last returned."""
        partial = await fake_index([release_json({INDEX_WHEEL: LOCAL_SHA256S[INDEX_WHEEL]})])
        not_json = await fake_index(['not json'])
        repo_ci = dag.repo_ci()
        await asyncio.gather(
            expect_error(
                repo_ci.index_serves(INDEX_TAG, index_dist(), timeout=0, index_url=partial),
                f"did not list exactly {sorted(INDEX_DIST)} within 0s; last seen: the index lists ['{INDEX_WHEEL}']",
            ),
            expect_error(
                repo_ci.index_serves(INDEX_TAG, index_dist(), timeout=0, index_url=not_json),
                'last seen: JSONDecodeError: Expecting value',
            ),
        )
