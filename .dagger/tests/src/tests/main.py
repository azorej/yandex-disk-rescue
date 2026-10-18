"""Tests for the repo-ci Dagger module. Run with `mise run ci:selftest`."""

import asyncio

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
