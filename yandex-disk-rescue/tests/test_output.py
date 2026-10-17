"""Tests for the output every `scan`, `download` and `verify` run shares: the report on stdout, the notices on stderr,
the verbosity flags and the log file in the XDG state directory."""

import errno
import os
import re
import stat

import pytest
from fake_disk_server import FakeResponse
from mirror_tree import FILES, LOG_LINE, MODIFIED, TOKEN, log_messages, run_download, runner, stderr_lines
from yandex_disk_rescue import logs, mirroring
from yandex_disk_rescue.cli import app
from yandex_disk_rescue.commands import scan as scan_command
from yandex_disk_rescue.paths import log_path, manifest_path, state_dir
from yandex_disk_rescue.units import format_size

TIMESTAMP = r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z'


def test_every_run_logs_its_start_and_end_to_a_private_log_file(disk, dest):
    downloaded = run_download(str(dest))
    missing = runner.invoke(app, ['scan', 'disk:/missing'])

    assert downloaded.exit_code == 0, downloaded.output
    assert downloaded.stdout.endswith(f'Manifest: {manifest_path()}\nLog: {log_path()}\n')
    assert (missing.exit_code, missing.stderr) == (1, "Error: Remote path 'disk:/missing' does not exist.\n")
    assert stat.S_IMODE(state_dir().stat().st_mode) == 0o700
    assert stat.S_IMODE(log_path().stat().st_mode) == 0o600
    lines = log_path().read_text(encoding='utf-8').splitlines()
    assert {LOG_LINE.match(line)['pid'] for line in lines} == {str(os.getpid())}
    assert [LOG_LINE.match(line)['command'] for line in (lines[0], lines[-1])] == ['download', 'scan']
    messages = log_messages()
    assert messages[0].startswith(
        f'download started: version 0.1.0, pid {os.getpid()}; remote_path=disk:/, dest={dest}'
    )
    assert 'download_connections=20' in messages[0]
    assert messages.index('download completed (exit 0)') > messages.index('Summary:')
    assert messages[-2].startswith('scan started:')
    assert messages[-1] == "scan failed (exit 1): Remote path 'disk:/missing' does not exist."
    assert lines[-1].split()[1] == 'ERROR'
    assert disk.token not in log_path().read_text(encoding='utf-8')


def test_the_log_holds_the_whole_summary_one_prefixed_line_each(disk, dest):
    result = run_download(str(dest))

    assert result.exit_code == 0, result.output
    report = result.stdout.splitlines()
    summary = report[report.index(next(line for line in report if line.startswith('Mirrored '))) : -2]
    messages = log_messages()
    start = messages.index('Summary:', messages.index('Summary:') + 1)
    assert messages[start + 1 : start + 1 + len(summary)] == summary


def test_an_unexpected_error_is_logged_with_its_traceback(disk, monkeypatch):
    async def broken(*args):
        raise ValueError('a bug')

    monkeypatch.setattr(scan_command, 'scan_tree', broken)

    result = runner.invoke(app, ['scan'])

    assert result.exit_code == 1
    assert isinstance(result.exception, ValueError)
    lines = log_path().read_text(encoding='utf-8').splitlines()
    end = next(index for index, line in enumerate(lines) if 'scan failed with an unexpected error' in line)
    assert lines[end].split()[1] == 'ERROR'
    assert all(LOG_LINE.match(line) for line in lines[end:])
    traceback = log_messages()[end + 1 :]
    assert traceback[0] == 'Traceback (most recent call last):'
    assert traceback[-1] == 'ValueError: a bug'


def test_the_log_rotates_and_every_file_stays_private(disk, monkeypatch):
    monkeypatch.setattr(logs, 'MAX_BYTES', 2000)

    for _ in range(4):
        assert runner.invoke(app, ['scan']).exit_code == 0

    rotated = log_path().with_name(f'{log_path().name}.1')
    assert rotated.exists()
    files = sorted(state_dir().glob(f'{log_path().name}*'))
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files)
    assert all(path.stat().st_size <= 2000 for path in files)
    assert log_messages()[-1] == 'scan completed (exit 0)'
    assert log_messages(rotated)


def test_the_log_keeps_at_most_five_older_files(disk, monkeypatch):
    monkeypatch.setattr(logs, 'MAX_BYTES', 200)

    for _ in range(3):
        assert runner.invoke(app, ['scan']).exit_code == 0

    names = sorted(path.name for path in state_dir().glob(f'{log_path().name}*'))
    assert names == [log_path().name, *(f'{log_path().name}.{number}' for number in range(1, 6))]
    assert log_messages()[-1] == 'scan completed (exit 0)'


@pytest.mark.parametrize(
    'args',
    [
        ['download', 'a', 'b', 'c'],
        ['download', '-v', '-q', 'dest'],
        ['scan', '--verbose', '--quiet'],
        ['verify', '-v', '-q', 'dest'],
        ['scan', 'trash:/'],
        ['download', 'disk:/', 'disk:/A'],
    ],
    ids=['download-three-paths', 'download-v-q', 'scan-v-q', 'verify-v-q', 'unsupported-path', 'disk-dest'],
)
def test_a_usage_error_opens_no_log(disk, args):
    result = runner.invoke(app, args)

    assert result.exit_code == 2
    assert not log_path().exists()


def test_verbose_with_quiet_is_a_usage_error(disk, dest):
    result = run_download('-v', '-q', str(dest))

    assert result.exit_code == 2
    assert '--verbose and --quiet cannot be used together.' in ' '.join(result.stderr.replace('│', ' ').split())
    assert not dest.exists()


def test_a_log_file_that_cannot_be_opened_stops_the_run_before_anything_else(disk, dest):
    state_dir().parent.mkdir(parents=True)
    state_dir().write_bytes(b'')

    result = run_download(str(dest))

    assert result.exit_code == 1
    assert result.stderr == f'Error: Could not open the log file {log_path()}: {os.strerror(errno.EEXIST)}.\n'
    assert result.stdout == ''
    assert disk.requests == []
    assert not dest.exists()


@pytest.mark.parametrize('flags', [[], ['-q']], ids=['default', 'quiet'])
def test_a_log_file_that_stops_taking_writes_never_stops_the_rescue(disk, dest, flags):
    state_dir().mkdir(parents=True)
    log_path().symlink_to('/dev/full')

    result = run_download(*flags, str(dest))

    assert result.exit_code == 0, result.output
    reason = os.strerror(errno.ENOSPC)
    warning = f'Warning: could not write the log file {log_path()}: {reason}; logging to it is off for this run.'
    assert re.fullmatch(f'{TIMESTAMP} {re.escape(warning)}\n', result.stderr)
    assert result.stdout.endswith(f'Manifest: {manifest_path()}\nLog: {log_path()} (incomplete: {reason})\n')
    assert (dest / 'Docs' / 'readme.txt').read_bytes() == b'read me'


def test_the_log_names_every_failed_file_and_a_rerun_appends_to_it(disk, dest):
    refused = [f'disk:/Refused/{index:02}.bin' for index in range(25)]
    for path in refused:
        disk.add_file(path, b'x', modified=MODIFIED)
        disk.inject('GET', '/v1/disk/resources/download', 404, where={'path': path})
    reason = 'removed from the Disk after the scan'

    failed = run_download(str(dest))
    retried = run_download('--retry-failed', str(dest))

    assert failed.exit_code == 1
    listed = ''.join(f'  {path}: {reason}\n' for path in refused[:20])
    assert f'Failed: 25\n{listed}  … and 5 more\nManifest: ' in failed.stdout
    assert retried.exit_code == 0, retried.output
    messages = log_messages()
    assert sorted(m for m in messages if m.endswith(f' failed: {reason}')) == [f'{p} failed: {reason}' for p in refused]
    assert all(f'  {path}: {reason}' in messages for path in refused)
    runs = [m for m in messages if m.startswith('download ') and ('started:' in m or '(exit ' in m)]
    assert [m.partition(';')[0] if 'started:' in m else m for m in runs] == [
        f'download started: version 0.1.0, pid {os.getpid()}',
        'download failed (exit 1): 25 failed; see the list above.',
        f'download started: version 0.1.0, pid {os.getpid()}',
        'download completed (exit 0)',
    ]


def test_the_verbosity_sets_the_notices_and_the_log_level_and_never_the_report(disk, tmp_path, monkeypatch):
    readme = 'disk:/Docs/readme.txt'
    failure = f'{readme} failed: removed from the Disk after the scan'
    runs = {}
    for verbosity, flags in {'quiet': ['-q'], 'normal': [], 'verbose': ['-v']}.items():
        monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / verbosity))
        (tmp_path / verbosity).mkdir()
        disk.inject('GET', '/v1/disk/resources/download', 404, where={'path': readme})
        result = run_download(*flags, str(tmp_path / verbosity / 'dest'))
        assert result.exit_code == 1
        assert all(re.match(f'{TIMESTAMP} ', line) for line in result.stderr.splitlines()[:-1])
        *notices, result_line = stderr_lines(result.stderr)
        assert result_line == 'Error: 1 failed; see the list above.'
        runs[verbosity] = result.stdout.replace(str(tmp_path / verbosity), '<run>'), notices, _levels()

    assert runs['quiet'][0] == runs['normal'][0] == runs['verbose'][0]
    assert 'Failed: 1\n  disk:/Docs/readme.txt: removed from the Disk after the scan\n' in runs['normal'][0]
    assert runs['quiet'][1] == []
    assert runs['normal'][1] == [failure]
    verbose = runs['verbose'][1]
    assert failure in verbose
    assert 'Scanning disk:/' in verbose
    downloaded = [notice.partition(' downloaded: ')[0] for notice in verbose if ' downloaded: ' in notice]
    assert sorted(downloaded) == sorted(path for path in FILES if path != readme)
    assert runs['quiet'][2] == runs['normal'][2] == {'INFO', 'WARNING', 'ERROR'}
    assert 'DEBUG' in runs['verbose'][2]


def test_the_log_never_holds_the_token_or_a_download_link(disk, dest):
    disk.redirect_downloads = True
    disk.inject('GET', '/download', 410, where={'path': 'disk:/Docs/readme.txt'})

    result = run_download('-v', str(dest))

    assert result.exit_code == 0, result.output
    log = log_path().read_text(encoding='utf-8')
    assert 'disk:/Docs/readme.txt: the download host rejected the link; getting a new one' in log_messages()
    assert TOKEN not in log
    assert 'link=' not in log
    assert '/storage?' not in log


def _levels() -> set[str]:
    return {LOG_LINE.match(line)['level'] for line in log_path().read_text(encoding='utf-8').splitlines()}


def test_the_log_records_api_throttling_retries_deferrals_and_passes(disk, dest, monkeypatch):
    monkeypatch.setattr(mirroring, 'PASS_PAUSE_SECONDS', 0)
    disk.inject('GET', '/v1/disk/', FakeResponse(429, headers={'Retry-After': '0'}))
    disk.inject('GET', '/download', 503, where={'path': 'disk:/Docs/readme.txt'})

    result = run_download('-v', str(dest))

    assert result.exit_code == 0, result.output
    messages = log_messages()
    assert 'Disk API throttling: HTTP 429; request interval now 50 ms; Retry-After 0 s' in messages
    [retry] = [m for m in messages if m.startswith('Disk API request failed: ')]
    assert retry.startswith('Disk API request failed: Yandex Disk API returned HTTP 429')
    assert retry.endswith('; retry 1 of 5 in 0.0 s')
    total = format_size(sum(map(len, FILES.values())))
    assert [m for m in messages if m.startswith(('Pass ', 'disk:/Docs/readme.txt deferred'))] == [
        f'Pass 1 of 7 over 6 files ({total}) starts',
        'disk:/Docs/readme.txt deferred to a later pass: the download host 127.0.0.1 returned HTTP 503',
        'Pass 2 of 7 over 1 file (7 B) starts in 0 s',
    ]
    assert 'Disk API throttling: HTTP 429; request interval now 50 ms; Retry-After 0 s' in stderr_lines(result.stderr)
