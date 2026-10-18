"""Tests for the distributions PyPI receives: a real `uv build` of the package, then its sdist and wheel inspected as
files — names, metadata, the PyPI page, the license and the wheel's contents."""

import email.message
import email.parser
import importlib.metadata
import re
import subprocess
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / 'yandex-disk-rescue'
VERSION = importlib.metadata.version('yandex-disk-rescue')
PROJECT_URL_LABELS = {'Homepage', 'Repository', 'Documentation', 'Issues', 'Changelog'}
# Inline links and images `[text](target)` / `![alt](target)`, and reference definitions `[label]: target`.
LINK_TARGET = re.compile(r'\]\(\s*<?([^)\s>]+)|^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)', re.MULTILINE)


@dataclass(frozen=True)
class Distributions:
    sdist: Path
    wheel: Path

    def wheel_text(self, name: str) -> str:
        with zipfile.ZipFile(self.wheel) as wheel:
            return wheel.read(f'yandex_disk_rescue-{VERSION}.dist-info/{name}').decode()

    def wheel_bytes(self, name: str) -> bytes:
        with zipfile.ZipFile(self.wheel) as wheel:
            return wheel.read(name)

    def wheel_names(self) -> list[str]:
        with zipfile.ZipFile(self.wheel) as wheel:
            return wheel.namelist()

    def sdist_bytes(self, name: str) -> bytes:
        with tarfile.open(self.sdist) as sdist:
            member = sdist.extractfile(f'yandex_disk_rescue-{VERSION}/{name}')
            assert member is not None, f'{name} is not a regular file in the sdist'
            return member.read()


@pytest.fixture(scope='module')
def built(tmp_path_factory: pytest.TempPathFactory) -> Distributions:
    out_dir = tmp_path_factory.mktemp('dist')
    subprocess.run(
        ['uv', 'build', '--package', 'yandex-disk-rescue', '--no-sources', '--out-dir', str(out_dir)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        timeout=110,
    )
    return Distributions(
        sdist=out_dir / f'yandex_disk_rescue-{VERSION}.tar.gz',
        wheel=out_dir / f'yandex_disk_rescue-{VERSION}-py3-none-any.whl',
    )


def parse_metadata(text: str) -> email.message.Message:
    return email.parser.Parser().parsestr(text)


@pytest.fixture(scope='module', params=['wheel METADATA', 'sdist PKG-INFO'])
def metadata(request: pytest.FixtureRequest, built: Distributions) -> email.message.Message:
    if request.param == 'wheel METADATA':
        return parse_metadata(built.wheel_text('METADATA'))
    return parse_metadata(built.sdist_bytes('PKG-INFO').decode())


def test_build_makes_one_sdist_and_one_wheel_of_the_installed_version(built: Distributions) -> None:
    # uv writes a `*` .gitignore into every out-dir it creates.
    assert sorted(path.name for path in built.wheel.parent.iterdir()) == [
        '.gitignore',
        f'yandex_disk_rescue-{VERSION}-py3-none-any.whl',
        f'yandex_disk_rescue-{VERSION}.tar.gz',
    ]


def test_metadata_names_the_project_and_its_license(metadata: email.message.Message) -> None:
    assert metadata['Name'] == 'yandex-disk-rescue'
    assert metadata['Version'] == VERSION
    assert metadata['Requires-Python'] == '>=3.14'
    assert metadata['License-Expression'] == 'MIT'
    assert metadata.get_all('License-File') == ['LICENSE']


def test_metadata_classifiers_and_project_urls(metadata: email.message.Message) -> None:
    classifiers = metadata.get_all('Classifier') or []
    assert [c for c in classifiers if c.startswith('License ::')] == []
    assert 'Operating System :: POSIX :: Linux' in classifiers
    assert 'Programming Language :: Python :: 3.14' in classifiers
    urls = dict(entry.split(', ', 1) for entry in metadata.get_all('Project-URL') or [])
    assert set(urls) == PROJECT_URL_LABELS
    assert all(url.startswith('https://github.com/azorej/yandex-disk-rescue') for url in urls.values())


def test_description_is_the_pypi_page_with_absolute_links(metadata: email.message.Message) -> None:
    assert metadata['Description-Content-Type'] == 'text/markdown'
    description = metadata.get_payload()
    assert description == (PACKAGE_DIR / 'PYPI.md').read_text()
    targets = [inline or reference for inline, reference in LINK_TARGET.findall(description)]
    assert targets, 'the PyPI page links to nothing'
    assert [target for target in targets if not target.startswith('https://')] == []


def test_both_distributions_ship_the_repository_license(built: Distributions) -> None:
    license_bytes = (REPO_ROOT / 'LICENSE').read_bytes()
    assert built.wheel_bytes(f'yandex_disk_rescue-{VERSION}.dist-info/licenses/LICENSE') == license_bytes
    assert built.sdist_bytes('LICENSE') == license_bytes


def test_wheel_installs_the_console_script(built: Distributions) -> None:
    entry_points = built.wheel_text('entry_points.txt')
    assert re.search(r'^\[console_scripts\]\nyandex-disk-rescue = yandex_disk_rescue\.cli:app$', entry_points, re.M)


def test_wheel_holds_only_the_package_and_its_dist_info(built: Distributions) -> None:
    stray = [
        name
        for name in built.wheel_names()
        if not name.startswith(('yandex_disk_rescue/', f'yandex_disk_rescue-{VERSION}.dist-info/'))
    ]
    assert stray == []
    assert 'yandex_disk_rescue/__init__.py' in built.wheel_names()
