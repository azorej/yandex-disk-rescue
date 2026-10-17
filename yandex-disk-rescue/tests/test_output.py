"""Tests for the output every `scan`, `download` and `verify` run shares: the report on stdout, the notices on stderr,
the verbosity flags and the log file in the XDG state directory."""

import errno
import os
import re
import stat

import pytest
from mirror_tree import LOG_LINE, log_messages, run_download, runner
from yandex_disk_rescue import logs
from yandex_disk_rescue.cli import app
from yandex_disk_rescue.commands import scan as scan_command
from yandex_disk_rescue.paths import log_path, manifest_path, state_dir

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
