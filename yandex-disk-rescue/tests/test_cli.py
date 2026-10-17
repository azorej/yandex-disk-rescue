"""Tests for the command-line entry point."""

import os
import subprocess

import pytest
from typer.testing import CliRunner
from yandex_disk_rescue import __version__
from yandex_disk_rescue.cli import app

runner = CliRunner()


def test_version_prints_package_version():
    result = runner.invoke(app, ['--version'])
    assert result.exit_code == 0
    assert result.stdout.strip() == f'yandex-disk-rescue {__version__}'


def test_help_describes_the_tool():
    result = runner.invoke(app, ['--help'])
    assert result.exit_code == 0
    assert 'Download everything from a Yandex Disk account' in result.stdout


def test_installed_console_script_runs():
    result = subprocess.run(['yandex-disk-rescue', '--version'], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == f'yandex-disk-rescue {__version__}'


@pytest.mark.parametrize('command', ['login', 'status', 'logout'])
def test_auth_help_lists_the_commands(command):
    result = runner.invoke(app, ['auth', '--help'])
    assert result.exit_code == 0
    assert 'Manage the Yandex OAuth token.' in result.stdout
    assert command in result.stdout


def test_console_script_renders_rescue_error_without_traceback(fake_disk_api):
    sentinel = 'y0_garbage-sentinel-token'
    fake_disk_api.route_json('GET', '/v1/disk/', 401, {'error': 'UnauthorizedError', 'description': 'Unauthorized'})

    result = subprocess.run(
        ['yandex-disk-rescue', 'auth', 'status'],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, 'YANDEX_DISK_RESCUE_TOKEN': sentinel},
    )

    assert result.returncode == 1
    assert result.stderr.startswith('Error: Yandex rejected the token')
    assert 'Traceback' not in result.stderr
    assert sentinel not in result.stdout + result.stderr
    assert fake_disk_api.requests[0].headers['authorization'] == f'OAuth {sentinel}'
