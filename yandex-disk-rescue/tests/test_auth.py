"""Tests for token storage, resolution and the `auth` commands."""

import stat
import urllib.parse
from importlib.metadata import version
from pathlib import Path

import pytest
from fake_disk_server import FakeResponse
from typer.testing import CliRunner
from yandex_disk_rescue import auth
from yandex_disk_rescue.api import ApiLimits, DiskClient
from yandex_disk_rescue.cli import app
from yandex_disk_rescue.paths import config_dir, state_dir

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


def test_save_token_narrows_an_existing_config_dir_but_not_its_parents():
    directory = config_dir()
    directory.mkdir(parents=True)
    directory.chmod(0o755)
    directory.parent.chmod(0o755)

    auth.save_token(TOKEN)

    assert _mode(directory) == 0o700
    assert _mode(directory.parent) == 0o755


def test_save_token_failure_is_a_clean_error_that_leaves_no_temp_file():
    path = auth.token_path()
    (path / 'occupied').mkdir(parents=True)

    with pytest.raises(auth.TokenFileError) as excinfo:
        auth.save_token(TOKEN)

    assert str(excinfo.value) == f'Could not save the token to {path}: Is a directory.'
    assert [p.name for p in path.parent.iterdir()] == ['token']
    assert TOKEN not in str(excinfo.value)


def test_save_token_overwrites_the_previous_token():
    auth.save_token('first')
    auth.save_token('second')

    assert auth.get_token() == 'second'


@pytest.mark.parametrize('value', ['', '   \n', 'two words', 'tab\tinside', 'y0_t\u00f6ken', 'nul\x00byte'])
def test_save_token_rejects_empty_or_whitespace_values_without_echoing_them(value):
    with pytest.raises(auth.InvalidTokenError) as excinfo:
        auth.save_token(value)

    assert not auth.token_path().exists()
    if value.strip():
        assert value.strip() not in str(excinfo.value)


@pytest.mark.parametrize(
    ('directory', 'env_var', 'default_under_home'),
    [(config_dir, 'XDG_CONFIG_HOME', '.config'), (state_dir, 'XDG_STATE_HOME', '.local/state')],
)
@pytest.mark.parametrize('xdg_value', [None, '', 'relative/dir'])
def test_xdg_dirs_fall_back_under_home(monkeypatch, tmp_path, directory, env_var, default_under_home, xdg_value):
    monkeypatch.setenv('HOME', str(tmp_path))
    if xdg_value is None:
        monkeypatch.delenv(env_var)
    else:
        monkeypatch.setenv(env_var, xdg_value)

    assert directory() == tmp_path / default_under_home / 'yandex-disk-rescue'


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


@pytest.mark.parametrize(
    ('value', 'defect'),
    [
        ('SECRETAAA\nSECRETBBB', 'the token must not contain whitespace.'),
        ('SECRETöx', 'the token must contain only printable ASCII characters.'),
        ('SECRET\x07x', 'the token must contain only printable ASCII characters.'),
    ],
)
@pytest.mark.parametrize('source', ['env', 'file'])
def test_status_refuses_a_malformed_token_naming_the_source_not_the_value(
    monkeypatch, fake_disk_api, source, value, defect
):
    if source == 'env':
        monkeypatch.setenv(auth.TOKEN_ENV_VAR, value)
        expected = f'Error: YANDEX_DISK_RESCUE_TOKEN is not a valid token: {defect}\n'
    else:
        path = auth.save_token(TOKEN)
        path.write_text(f'{value}\n')
        expected = f'Error: Token file {path} does not hold a valid token: {defect}\n'

    result = runner.invoke(app, ['auth', 'status'])

    assert result.exit_code == 1
    assert result.stderr == expected
    assert 'SECRET' not in result.output
    assert fake_disk_api.requests == []


def test_status_refuses_a_token_file_that_is_not_utf8(fake_disk_api):
    path = auth.save_token(TOKEN)
    path.write_bytes(b'SEC\xffRET\n')

    result = runner.invoke(app, ['auth', 'status'])

    assert result.exit_code == 1
    assert result.stderr == f'Error: Token file {path} is not valid UTF-8 text.\n'
    assert fake_disk_api.requests == []


def test_status_refuses_a_token_path_that_is_a_directory(fake_disk_api):
    path = auth.token_path()
    path.mkdir(parents=True, mode=0o700)

    result = runner.invoke(app, ['auth', 'status'])

    assert result.exit_code == 1
    assert result.stderr == f'Error: Token file {path} is not a regular file.\n'
    assert fake_disk_api.requests == []


@pytest.mark.parametrize('token', ['SECRETAAA\nSECRETBBB', 'SECRETöx'])
def test_disk_client_refuses_a_token_unfit_for_a_header_before_any_request(fake_disk_api, token):
    with pytest.raises(auth.InvalidTokenError) as excinfo:
        DiskClient(
            token, base_url=fake_disk_api.base_url, limits=ApiLimits(interval=0.05, concurrency=1, timeout=1, retries=0)
        )

    assert 'SECRET' not in str(excinfo.value)
    assert fake_disk_api.requests == []


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


def test_status_with_a_stored_token_shows_the_account(fake_disk_api):
    path = auth.save_token(TOKEN)

    result = runner.invoke(app, ['auth', 'status'])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        f'Token: file {path}',
        'Account: rescue.tester',
        'Disk usage: 1.5 GiB of 10.0 GiB used (15.0%)',
    ]
    assert [
        (r.method, r.path, r.headers['authorization'], r.headers['user-agent']) for r in fake_disk_api.requests
    ] == [
        (
            'GET',
            '/v1/disk/',
            f'OAuth {TOKEN}',
            f'yandex-disk-rescue/{version("yandex-disk-rescue")} (+https://github.com/azorej/yandex-disk-rescue)',
        )
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
def test_status_reports_api_errors_without_a_traceback_or_the_token(monkeypatch, fake_disk_api, status, expected):
    monkeypatch.setenv('YANDEX_DISK_RESCUE_API_RETRIES', '0')
    auth.save_token(TOKEN)
    fake_disk_api.route_json('GET', '/v1/disk/', status, {'error': 'SomeError', 'description': 'Internal failure'})

    result = runner.invoke(app, ['auth', 'status'])

    assert result.exit_code == 1
    assert result.stdout == ''
    assert result.stderr.startswith(expected)
    assert 'Traceback' not in result.output
    assert TOKEN not in result.output


def test_status_error_without_a_json_description_uses_the_reason_phrase(monkeypatch, fake_disk_api):
    monkeypatch.setenv('YANDEX_DISK_RESCUE_API_RETRIES', '0')
    auth.save_token(TOKEN)
    fake_disk_api.route('GET', '/v1/disk/', 502, b'<html>bad gateway</html>')

    result = runner.invoke(app, ['auth', 'status'])

    assert result.exit_code == 1
    assert result.stderr == 'Error: Yandex Disk API returned HTTP 502: Bad Gateway\n'


def test_status_reports_an_unreachable_api(monkeypatch, closed_port_url):
    monkeypatch.setenv('YANDEX_DISK_RESCUE_API_RETRIES', '0')
    auth.save_token(TOKEN)
    url = closed_port_url
    monkeypatch.setenv('YANDEX_DISK_RESCUE_API_URL', url)

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
        (b'{"user": {"login": "a"}, "total_space": 10, "used_space": 1}', 'user.uid is missing'),
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
        'GET', '/v1/disk/', 200, {'user': {'login': 'a', 'uid': '1'}, 'total_space': total, 'used_space': used}
    )

    result = runner.invoke(app, ['auth', 'status'])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[-1] == usage


def test_status_api_url_option_reaches_the_api_without_the_env_var(monkeypatch, fake_disk_api):
    monkeypatch.delenv('YANDEX_DISK_RESCUE_API_URL')
    auth.save_token(TOKEN)

    result = runner.invoke(app, ['auth', 'status', '--api-url', fake_disk_api.base_url])

    assert result.exit_code == 0, result.output
    assert 'Account: rescue.tester' in result.stdout
    assert [(r.method, r.path) for r in fake_disk_api.requests] == [('GET', '/v1/disk/')]


@pytest.mark.parametrize(
    ('args', 'env', 'flag', 'reason'),
    [
        (['--api-interval-ms', '10'], {}, '--api-interval-ms', 'must be an integer from 25 to 1000.'),
        (
            [],
            {'YANDEX_DISK_RESCUE_API_INTERVAL_MS': '1001'},
            '--api-interval-ms',
            'must be an integer from 25 to 1000.',
        ),
        ([], {'YANDEX_DISK_RESCUE_API_CONCURRENCY': '99'}, '--api-concurrency', 'must be an integer from 1 to 30.'),
        (['--api-concurrency', '0'], {}, '--api-concurrency', 'must be an integer from 1 to 30.'),
        (['--api-timeout', '301'], {}, '--api-timeout', 'must be an integer from 1 to 300.'),
        ([], {'YANDEX_DISK_RESCUE_API_RETRIES': '21'}, '--api-retries', 'must be an integer from 0 to 20.'),
        (['--api-retries', 'many'], {}, '--api-retries', "'many' is not a valid int"),
        (['--api-url', 'ftp://example.com'], {}, '--api-url', 'must be an http:// or https:// URL.'),
        (
            [],
            {'YANDEX_DISK_RESCUE_API_URL': 'cloud-api.yandex.net'},
            '--api-url',
            'must be an http:// or https:// URL.',
        ),
    ],
)
def test_status_rejects_an_invalid_api_option_from_any_source(monkeypatch, fake_disk_api, args, env, flag, reason):
    auth.save_token(TOKEN)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    result = runner.invoke(app, ['auth', 'status', *args])

    assert result.exit_code == 2
    error = ' '.join(result.stderr.replace('│', ' ').split())
    assert f"Invalid value for '{flag}'" in error
    assert reason in error
    assert fake_disk_api.requests == []


@pytest.mark.parametrize('command', [['auth', 'status'], ['auth', 'login']])
def test_auth_commands_retry_a_throttled_account_check(fake_disk_api, command):
    auth.save_token(TOKEN)
    fake_disk_api.inject('GET', '/v1/disk/', FakeResponse.error(503, {'Retry-After': '0'}))

    result = runner.invoke(app, command, input=f'{TOKEN}\n')

    assert result.exit_code == 0, result.output
    assert 'rescue.tester' in result.stdout
    assert [r.path for r in fake_disk_api.requests] == ['/v1/disk/', '/v1/disk/']


def _authorize_url_from(output: str) -> str:
    [url] = [line.strip() for line in output.splitlines() if line.strip().startswith('https://oauth.yandex.ru/')]
    return url


def test_login_stores_a_verified_token_that_status_then_uses(fake_disk_api):
    result = runner.invoke(app, ['auth', 'login'], input=f'{TOKEN}\n')

    assert result.exit_code == 0, result.output
    path = auth.token_path()
    assert result.stdout.splitlines()[-1] == f'Logged in as rescue.tester. Token saved to {path}.'
    assert _mode(path) == 0o600
    assert path.read_text() == f'{TOKEN}\n'
    assert TOKEN not in result.output
    assert fake_disk_api.requests[0].headers['authorization'] == f'OAuth {TOKEN}'

    status_result = runner.invoke(app, ['auth', 'status'])
    assert status_result.exit_code == 0, status_result.output
    assert 'Account: rescue.tester' in status_result.stdout


@pytest.mark.parametrize(
    ('args', 'env_client_id', 'expected_client_id'),
    [
        ([], None, '9e2a0382c430429c9d57913b61d7d48f'),
        (['--client-id', '0123456789abcdef0123456789abcdef'], None, '0123456789abcdef0123456789abcdef'),
        ([], 'fedcba9876543210fedcba9876543210', 'fedcba9876543210fedcba9876543210'),
        (
            ['--client-id', '0123456789abcdef0123456789abcdef'],
            'fedcba9876543210fedcba9876543210',
            '0123456789abcdef0123456789abcdef',
        ),
    ],
)
def test_login_prints_the_implicit_flow_url_on_stderr(
    monkeypatch, fake_disk_api, args, env_client_id, expected_client_id
):
    if env_client_id is not None:
        monkeypatch.setenv('YANDEX_DISK_RESCUE_CLIENT_ID', env_client_id)

    result = runner.invoke(app, ['auth', 'login', *args], input=f'{TOKEN}\n')

    assert result.exit_code == 0, result.output
    url = _authorize_url_from(result.stderr)
    parts = urllib.parse.urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == ('https', 'oauth.yandex.ru', '/authorize')
    assert urllib.parse.parse_qs(parts.query, strict_parsing=True) == {
        'response_type': ['token'],
        'client_id': [expected_client_id],
        'scope': ['cloud_api:disk.read cloud_api:disk.info'],
    }
    assert 'scope=cloud_api%3Adisk.read%20cloud_api%3Adisk.info' in url
    assert 'https://oauth.yandex.ru/' not in result.stdout


@pytest.mark.parametrize(
    'pasted',
    [
        f'https://oauth.yandex.ru/verification_code#access_token={TOKEN}&token_type=bearer&expires_in=31536000',
        f'  access_token={TOKEN}&token_type=bearer  ',
    ],
)
def test_login_accepts_a_pasted_redirect_url(fake_disk_api, pasted):
    result = runner.invoke(app, ['auth', 'login'], input=f'{pasted}\n')

    assert result.exit_code == 0, result.output
    assert auth.token_path().read_text() == f'{TOKEN}\n'
    assert fake_disk_api.requests[0].headers['authorization'] == f'OAuth {TOKEN}'


@pytest.mark.parametrize(
    ('pasted', 'expected'),
    [
        ('   ', 'Error: The token is empty.\n'),
        ('two words', 'Error: The token must not contain whitespace.\n'),
        ('y0_t\u00f6ken', 'Error: The token must contain only printable ASCII characters.\n'),
        (
            'https://oauth.yandex.ru/verification_code#access_token=&token_type=bearer',
            'Error: The pasted URL has an empty access_token parameter.\n',
        ),
    ],
)
def test_login_rejects_an_unusable_paste_before_calling_the_api(fake_disk_api, pasted, expected):
    result = runner.invoke(app, ['auth', 'login'], input=f'{pasted}\n')

    assert result.exit_code == 1
    assert result.stderr.endswith(expected)
    assert fake_disk_api.requests == []
    assert not auth.token_path().exists()


def test_login_with_a_rejected_token_stores_nothing(fake_disk_api):
    fake_disk_api.route_json('GET', '/v1/disk/', 401, {'error': 'UnauthorizedError', 'description': 'Unauthorized'})

    result = runner.invoke(app, ['auth', 'login'], input=f'{TOKEN}\n')

    assert result.exit_code == 1
    assert result.stderr.endswith(
        "Error: Yandex rejected the token (invalid, expired or revoked). Run 'yandex-disk-rescue auth login' to store a new one.\n"
    )
    assert 'Logged in' not in result.stdout
    assert not auth.token_path().exists()
    assert not config_dir().exists()
    assert TOKEN not in result.output


def test_login_warns_that_the_env_var_overrides_the_saved_token(monkeypatch, fake_disk_api):
    monkeypatch.setenv(auth.TOKEN_ENV_VAR, 'from-env')

    result = runner.invoke(app, ['auth', 'login'], input=f'{TOKEN}\n')

    assert result.exit_code == 0, result.output
    assert result.stderr.endswith('Warning: YANDEX_DISK_RESCUE_TOKEN is set and overrides the saved token.\n')
    assert fake_disk_api.requests[0].headers['authorization'] == f'OAuth {TOKEN}'


@pytest.mark.parametrize('source', ['option', 'env'])
@pytest.mark.parametrize('client_id', ['nothex', '9E2A0382C430429C9D57913B61D7D48F', '9e2a0382c430429c9d57913b61d7d48'])
def test_login_rejects_a_malformed_client_id_as_a_usage_error(monkeypatch, fake_disk_api, source, client_id):
    if source == 'option':
        args = ['--client-id', client_id]
    else:
        args = []
        monkeypatch.setenv('YANDEX_DISK_RESCUE_CLIENT_ID', client_id)

    result = runner.invoke(app, ['auth', 'login', *args], input=f'{TOKEN}\n')

    assert result.exit_code == 2
    assert 'must be 32 lowercase hexadecimal characters' in result.stderr
    assert fake_disk_api.requests == []
    assert not auth.token_path().exists()


@pytest.mark.parametrize('command', [[], ['auth'], ['auth', 'login'], ['auth', 'status'], ['auth', 'logout']])
def test_help_never_shows_the_token(monkeypatch, command):
    monkeypatch.setenv(auth.TOKEN_ENV_VAR, TOKEN)
    auth.save_token(TOKEN)

    result = runner.invoke(app, [*command, '--help'])

    assert result.exit_code == 0, result.output
    assert TOKEN not in result.output
