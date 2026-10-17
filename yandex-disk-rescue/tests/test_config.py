"""Tests for the config file layer: real TOML files feeding the registry options of `scan` and `auth`."""

import itertools
import sqlite3
import urllib.parse
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from fake_disk_server import Delay, Truncate
from typer.testing import CliRunner
from yandex_disk_rescue.cli import app
from yandex_disk_rescue.paths import config_dir, manifest_path

runner = CliRunner()

TOKEN = 'y0_AgAAAAB-config-sentinel-token'
OPTIONS = (
    'api_interval_ms, api_concurrency, api_timeout, api_retries, api_url, download_timeout, download_retries, '
    'download_passes, download_connections, download_checkpoint_mib, scan_max_age, client_id'
)


@pytest.fixture
def disk(fake_disk_api, monkeypatch):
    """The fake API with four folders under the root, so their listings start back to back at the limiter's pace."""
    monkeypatch.setenv('YANDEX_DISK_RESCUE_TOKEN', TOKEN)
    fake_disk_api.token = TOKEN
    for name in 'ABCD':
        fake_disk_api.add_file(f'disk:/{name}/file.txt', name.encode())
    return fake_disk_api


def _write_default_config(content: str | bytes):
    path = config_dir() / 'config.toml'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    return path


def _start_gaps(fake) -> list[float]:
    starts = sorted(request.started for request in fake.requests)
    assert len(starts) == 6
    return [later - earlier for earlier, later in itertools.pairwise(starts)]


def test_the_default_config_file_paces_scan(disk):
    _write_default_config('api_interval_ms = 200\n')

    result = runner.invoke(app, ['scan'])

    assert result.exit_code == 0, result.output
    gaps = _start_gaps(disk)
    assert sum(gaps) >= 0.9 * 0.2 * len(gaps)


@pytest.mark.parametrize('override', ['flag', 'env'])
def test_the_flag_and_the_env_var_override_the_config_file(disk, monkeypatch, tmp_path, override):
    path = tmp_path / 'rescue.toml'
    path.write_text('api_interval_ms = 200\n')
    monkeypatch.setenv('YANDEX_DISK_RESCUE_CONFIG', str(path))
    args = ['scan']
    if override == 'flag':
        args.append('--api-interval-ms=50')
    else:
        monkeypatch.setenv('YANDEX_DISK_RESCUE_API_INTERVAL_MS', '50')

    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    assert min(_start_gaps(disk)) < 0.15


def test_auth_status_reads_the_api_url_from_the_config_file(fake_disk_api, monkeypatch):
    monkeypatch.delenv('YANDEX_DISK_RESCUE_API_URL')
    monkeypatch.setenv('YANDEX_DISK_RESCUE_TOKEN', TOKEN)
    _write_default_config(f'api_url = "{fake_disk_api.base_url}"\n')

    result = runner.invoke(app, ['auth', 'status'])

    assert result.exit_code == 0, result.output
    assert 'Account: ' in result.stdout
    assert [(r.method, r.path) for r in fake_disk_api.requests] == [('GET', '/v1/disk/')]


@pytest.mark.parametrize(
    ('args', 'expected_client_id'),
    [
        ([], '0123456789abcdef0123456789abcdef'),
        (['--client-id', 'fedcba9876543210fedcba9876543210'], 'fedcba9876543210fedcba9876543210'),
    ],
)
def test_auth_login_reads_the_client_id_from_the_config_file(fake_disk_api, tmp_path, args, expected_client_id):
    path = tmp_path / 'rescue.toml'
    path.write_text('client_id = "0123456789abcdef0123456789abcdef"\n')

    result = runner.invoke(app, ['--config', str(path), 'auth', 'login', *args], input=f'{TOKEN}\n')

    assert result.exit_code == 0, result.output
    [url] = [line.strip() for line in result.stderr.splitlines() if line.strip().startswith('https://oauth.yandex.ru/')]
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)['client_id'] == [expected_client_id]


@pytest.mark.parametrize(
    ('fault', 'reason'),
    [(Delay(2), 'the download stalled: no data for 1 s\n'), (Truncate(after_bytes=500), 'the download failed: ')],
    ids=['stall', 'cut-short'],
)
def test_download_reads_its_transfer_options_from_the_config_file(disk, tmp_path, fault, reason):
    _write_default_config('download_timeout = 1\ndownload_retries = 0\ndownload_passes = 1\n')
    disk.add_file('disk:/A/file.txt', b'x' * 1000)
    disk.inject('GET', '/download', fault, where={'path': 'disk:/A/file.txt'})

    result = runner.invoke(app, ['download', 'disk:/A', str(tmp_path / 'dest')])

    assert result.exit_code == 1
    [failure] = [line for line in result.stdout.splitlines() if line.startswith('  disk:/A/file.txt: ')]
    assert f'{failure}\n'.startswith(f'  disk:/A/file.txt: {reason}')
    assert '(after' not in failure
    assert 'Passes: 1 of 1\n' in result.stdout
    assert len([r for r in disk.requests if r.path == '/download']) == 1


@pytest.mark.parametrize(('age', 'scans'), [(timedelta(minutes=4), False), (timedelta(minutes=6), True)])
def test_download_reads_the_scan_max_age_from_the_config_file(disk, tmp_path, age, scans):
    _write_default_config('scan_max_age = 5\n')
    assert runner.invoke(app, ['scan']).exit_code == 0
    with closing(sqlite3.connect(manifest_path())) as connection, connection:
        completed_at = (datetime.now(UTC) - age).isoformat(timespec='microseconds')
        connection.execute('UPDATE scans SET completed_at = ?', (completed_at,))

    result = runner.invoke(app, ['download', str(tmp_path / 'dest')])

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith('Scanned disk:/' if scans else 'Using the scan of disk:/ completed ')


@pytest.mark.parametrize(
    ('text', 'reason'),
    [
        (f'token = "{TOKEN}"\n', f'token: unknown option; the options are {OPTIONS}.'),
        ('api_concurrency = 0\n', 'api_concurrency: must be an integer from 1 to 30.'),
        ('api_concurrency = "x"\n', 'api_concurrency: must be an integer.'),
        ('api_retries = true\n', 'api_retries: must be an integer.'),
        ('api_url = 8080\n', 'api_url: must be a string.'),
        ('api_url = "ftp://example.com"\n', 'api_url: must be an http:// or https:// URL.'),
        ('download_timeout = 0\n', 'download_timeout: must be an integer from 1 to 600.'),
        ('download_retries = 21\n', 'download_retries: must be an integer from 0 to 20.'),
        ('download_passes = -1\n', 'download_passes: must be an integer from 0 to 1000.'),
        ('download_connections = 65\n', 'download_connections: must be an integer from 1 to 64.'),
        ('download_checkpoint_mib = 0\n', 'download_checkpoint_mib: must be an integer from 1 to 1024.'),
        ('scan_max_age = 10081\n', 'scan_max_age: must be an integer from 0 to 10080.'),
        ('[api]\ninterval_ms = 100\n', f'api: unknown option; the options are {OPTIONS}.'),
        ('api_interval_ms = \n', 'is not valid TOML: Invalid value (at line 1, column 19).'),
        (b'client_id = "\xff"\n', 'is not UTF-8 text.'),
    ],
)
@pytest.mark.parametrize('command', [['scan'], ['download', 'dest'], ['auth', 'status'], ['auth', 'logout']])
def test_an_invalid_config_file_is_a_usage_error_naming_the_path_and_the_key(fake_disk_api, command, text, reason):
    path = _write_default_config(text)

    result = runner.invoke(app, command)

    assert result.exit_code == 2
    assert result.stderr == f'Error: config file {path}: {reason}\n'
    assert TOKEN not in result.output
    assert fake_disk_api.requests == []


@pytest.mark.parametrize('source', ['flag', 'env'])
def test_a_named_config_file_that_does_not_exist_is_a_usage_error(monkeypatch, tmp_path, source):
    path = tmp_path / 'nonexistent.toml'
    args = ['auth', 'status']
    if source == 'flag':
        args = ['--config', str(path), *args]
    else:
        monkeypatch.setenv('YANDEX_DISK_RESCUE_CONFIG', str(path))

    result = runner.invoke(app, args)

    assert result.exit_code == 2
    assert result.stderr == f'Error: config file {path}: does not exist.\n'


def test_a_config_path_that_is_a_directory_is_a_usage_error(tmp_path):
    result = runner.invoke(app, ['--config', str(tmp_path), 'auth', 'status'])

    assert result.exit_code == 2
    assert result.stderr == f'Error: config file {tmp_path}: cannot be read: Is a directory.\n'


def test_download_reads_its_connection_cap_from_the_config_file(disk, tmp_path):
    _write_default_config('download_connections = 1\n')
    for name in 'ABCD':
        disk.add_file(f'disk:/{name}/file.txt', name.encode() * 3 * 64 * 1024)
    disk.chunk_delay = 0.05

    result = runner.invoke(app, ['download', str(tmp_path / 'dest')])

    assert result.exit_code == 0, result.output
    assert len([r for r in disk.requests if r.path == '/download']) == 4
    assert disk.peak_content_in_flight == 1
