"""Tests for token storage, resolution and the `auth` commands."""

import socket
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner
from yandex_disk_rescue import auth
from yandex_disk_rescue.cli import app
from yandex_disk_rescue.paths import config_dir

runner = CliRunner()

TOKEN = 'y0_AgAAAAB-sentinel-token-value'


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_saved_token_round_trips_from_a_private_file(tmp_path):
    path = auth.save_token(f'  {TOKEN}\n')

    assert path == tmp_path / 'xdg-config' / 'yandex-disk-rescue' / 'token'
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700
    assert auth.resolve_token() == auth.Token(value=TOKEN, source=auth.TokenSource.FILE)
    assert auth.get_token() == TOKEN
    assert [p.name for p in path.parent.iterdir()] == ['token']


def test_save_token_overwrites_the_previous_token():
    auth.save_token('first')
    auth.save_token('second')

    assert auth.get_token() == 'second'


@pytest.mark.parametrize('value', ['', '   \n', 'two words', 'tab\tinside'])
def test_save_token_rejects_empty_or_whitespace_values_without_echoing_them(value):
    with pytest.raises(auth.InvalidTokenError) as excinfo:
        auth.save_token(value)

    assert not auth.token_path().exists()
    if value.strip():
        assert value.strip() not in str(excinfo.value)


@pytest.mark.parametrize('xdg_config_home', [None, '', 'relative/config'])
def test_config_dir_falls_back_under_home(monkeypatch, tmp_path, xdg_config_home):
    monkeypatch.setenv('HOME', str(tmp_path))
    if xdg_config_home is None:
        monkeypatch.delenv('XDG_CONFIG_HOME')
    else:
        monkeypatch.setenv('XDG_CONFIG_HOME', xdg_config_home)

    assert config_dir() == tmp_path / '.config' / 'yandex-disk-rescue'


def test_env_var_wins_over_the_file(monkeypatch):
    auth.save_token('from-file')
    monkeypatch.setenv(auth.TOKEN_ENV_VAR, 'from-env')

    assert auth.resolve_token() == auth.Token(value='from-env', source=auth.TokenSource.ENV)


@pytest.mark.parametrize('env_value', ['', '   '])
def test_blank_env_var_is_an_error_not_a_fallback(monkeypatch, env_value):
    auth.save_token('from-file')
    monkeypatch.setenv(auth.TOKEN_ENV_VAR, env_value)

    with pytest.raises(auth.NoTokenError, match=auth.TOKEN_ENV_VAR):
        auth.get_token()


def test_missing_token_points_at_login_and_env_var():
    with pytest.raises(auth.NoTokenError, match="Run 'yandex-disk-rescue auth login' or set YANDEX_DISK_RESCUE_TOKEN"):
        auth.get_token()


def test_empty_token_file_is_no_token():
    path = auth.save_token(TOKEN)
    path.write_text('\n')

    with pytest.raises(auth.NoTokenError, match='empty'):
        auth.get_token()


def test_token_file_readable_by_others_is_refused():
    path = auth.save_token(TOKEN)
    path.chmod(0o644)

    with pytest.raises(auth.InsecureTokenFileError) as excinfo:
        auth.get_token()

    assert str(excinfo.value) == f'Token file {path} is accessible by other users; run: chmod 600 {path}'
    assert TOKEN not in str(excinfo.value)


@pytest.mark.parametrize('source', ['file', 'env'])
def test_token_repr_hides_the_value(monkeypatch, source):
    if source == 'file':
        auth.save_token(TOKEN)
    else:
        monkeypatch.setenv(auth.TOKEN_ENV_VAR, TOKEN)

    assert TOKEN not in repr(auth.resolve_token())


def test_delete_token_without_a_file_returns_false():
    assert auth.delete_token() is False


def test_logout_removes_the_token_and_is_idempotent():
    path = auth.save_token(TOKEN)

    first = runner.invoke(app, ['auth', 'logout'])
    assert first.exit_code == 0
    assert not path.exists()
    assert f'Removed the stored token {path}.' in first.stdout
    assert 'https://id.yandex.com/personal/data-access' in first.stdout

    second = runner.invoke(app, ['auth', 'logout'])
    assert second.exit_code == 0
    assert f'No stored token at {path}.' in second.stdout
    assert 'https://id.yandex.com/personal/data-access' in second.stdout
    assert second.stderr == ''


def test_logout_warns_when_the_env_var_still_provides_a_token(monkeypatch):
    monkeypatch.setenv(auth.TOKEN_ENV_VAR, TOKEN)

    result = runner.invoke(app, ['auth', 'logout'])

    assert result.exit_code == 0
    assert auth.TOKEN_ENV_VAR in result.stderr
    assert TOKEN not in result.output


def _closed_port_url() -> str:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    return f'http://127.0.0.1:{port}'


def test_status_with_a_stored_token_shows_the_account(fake_disk_api):
    path = auth.save_token(TOKEN)

    result = runner.invoke(app, ['auth', 'status'])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        f'Token: file {path}',
        'Account: rescue.tester',
        'Disk usage: 1.5 GiB of 10.0 GiB used (15.0%)',
    ]
    assert [(r.method, r.path, r.headers['authorization']) for r in fake_disk_api.requests] == [
        ('GET', '/v1/disk/', f'OAuth {TOKEN}')
    ]


def test_status_names_the_env_var_as_the_source(monkeypatch, fake_disk_api):
    auth.save_token('from-file')
    monkeypatch.setenv(auth.TOKEN_ENV_VAR, TOKEN)

    result = runner.invoke(app, ['auth', 'status'])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == 'Token: environment variable YANDEX_DISK_RESCUE_TOKEN'
    assert fake_disk_api.requests[0].headers['authorization'] == f'OAuth {TOKEN}'
    assert TOKEN not in result.output


@pytest.mark.parametrize(
    ('status', 'expected'),
    [
        (401, "Error: Yandex rejected the token (invalid, expired or revoked). Run 'yandex-disk-rescue auth login'"),
        (403, 'Error: Yandex accepted the token but denied access to Disk'),
        (500, 'Error: Yandex Disk API returned HTTP 500: Internal failure'),
    ],
)
def test_status_reports_api_errors_without_a_traceback_or_the_token(fake_disk_api, status, expected):
    auth.save_token(TOKEN)
    fake_disk_api.route_json('GET', '/v1/disk/', status, {'error': 'SomeError', 'description': 'Internal failure'})

    result = runner.invoke(app, ['auth', 'status'])

    assert result.exit_code == 1
    assert result.stdout == ''
    assert result.stderr.startswith(expected)
    assert 'Traceback' not in result.output
    assert TOKEN not in result.output


def test_status_error_without_a_json_description_uses_the_reason_phrase(fake_disk_api):
    auth.save_token(TOKEN)
    fake_disk_api.route('GET', '/v1/disk/', 502, b'<html>bad gateway</html>')

    result = runner.invoke(app, ['auth', 'status'])

    assert result.exit_code == 1
    assert result.stderr == 'Error: Yandex Disk API returned HTTP 502: Bad Gateway\n'


def test_status_reports_an_unreachable_api(monkeypatch):
    auth.save_token(TOKEN)
    url = _closed_port_url()
    monkeypatch.setenv(auth.API_URL_ENV_VAR, url)

    result = runner.invoke(app, ['auth', 'status'])

    assert result.exit_code == 1
    assert result.stderr.startswith(f'Error: Could not reach the Yandex Disk API at {url}: ')
    assert TOKEN not in result.output


@pytest.mark.parametrize(
    ('body', 'reason'),
    [
        (b'<html>', 'the body is not JSON'),
        (b'[]', 'the body is not a JSON object'),
        (b'{"total_space": 10, "used_space": 1}', 'user.login is missing'),
        (b'{"user": {"login": "a"}, "total_space": 10, "used_space": "x"}', 'used_space is missing'),
        (b'{"user": {"login": "a"}, "total_space": 10, "used_space": true}', 'used_space is missing'),
        (b'{"user": {"login": "a"}, "total_space": -1, "used_space": 1}', 'total_space is missing'),
        (b'{"user": {"login": "a"}, "used_space": 1}', 'total_space is missing'),
    ],
)
def test_status_rejects_an_unexpected_success_body(fake_disk_api, body, reason):
    auth.save_token(TOKEN)
    fake_disk_api.route('GET', '/v1/disk/', 200, body, {'Content-Type': 'application/json'})

    result = runner.invoke(app, ['auth', 'status'])

    assert result.exit_code == 1
    assert result.stderr.startswith(
        f'Error: Yandex Disk API returned an unexpected response to GET /v1/disk/: {reason}'
    )


def test_status_without_a_token_points_at_login(fake_disk_api):
    result = runner.invoke(app, ['auth', 'status'])

    assert result.exit_code == 1
    assert result.stderr == (
        "Error: No Yandex OAuth token. Run 'yandex-disk-rescue auth login' or set YANDEX_DISK_RESCUE_TOKEN.\n"
    )
    assert fake_disk_api.requests == []


@pytest.mark.parametrize(
    ('used', 'total', 'usage'),
    [
        (0, 0, 'Disk usage: 0 B of 0 B used'),
        (512, 1024, 'Disk usage: 512 B of 1.0 KiB used (50.0%)'),
        (1024**2 - 1, 5 * 1024**4, 'Disk usage: 1.0 MiB of 5.0 TiB used (0.0%)'),
    ],
)
def test_status_formats_sizes_in_binary_units(fake_disk_api, used, total, usage):
    auth.save_token(TOKEN)
    fake_disk_api.route_json(
        'GET', '/v1/disk/', 200, {'user': {'login': 'a'}, 'total_space': total, 'used_space': used}
    )

    result = runner.invoke(app, ['auth', 'status'])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[-1] == usage


def test_api_base_url_defaults_to_yandex(monkeypatch):
    assert auth.api_base_url() == 'https://cloud-api.yandex.net'
    monkeypatch.setenv(auth.API_URL_ENV_VAR, 'http://127.0.0.1:1')
    assert auth.api_base_url() == 'http://127.0.0.1:1'
