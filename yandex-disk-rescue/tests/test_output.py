"""Tests for the output every `scan`, `download` and `verify` run shares: the report on stdout, the notices and status
lines on stderr, the verbosity and progress flags and the log file in the XDG state directory."""

import errno
import os
import re
import stat
import subprocess

import pytest
from fake_disk_server import Delay, FakeResponse
from mirror_tree import (
    FILES,
    LOG_LINE,
    MODIFIED,
    TOKEN,
    log_messages,
    run_download,
    runner,
    status_lines,
    stderr_lines,
)
from yandex_disk_rescue import localfs, logs, mirroring
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
    assert re.search(f'^{TIMESTAMP} {re.escape(warning)}$', result.stderr, re.MULTILINE)
    assert stderr_lines(result.stderr) == [warning]
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


def _child(*args: str, merged: bool = False, **env: str) -> subprocess.CompletedProcess[str]:
    """Run `yandex-disk-rescue <args>` to its end in a child process with this process's environment plus `env`,
    stdout and stderr each a pipe, or one pipe when `merged`."""
    return subprocess.run(
        ['yandex-disk-rescue', *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merged else subprocess.PIPE,
        text=True,
        env={**os.environ, **env},
        timeout=120,
    )


SLOW = 'disk:/Photos/2024/a.jpg'
DOWNLOADING = re.compile(r'Downloading: (\d+) of 6 files, .* \((\d+\.\d)%\), ')


@pytest.fixture
def slow(disk):
    """The `disk` tree with `SLOW` grown to six chunks served half a second apart, so its download takes 2.5 s."""
    disk.add_file(SLOW, bytes(6 * 64 * 1024), modified=MODIFIED)
    disk.chunk_delay = 0.5
    return disk


def test_a_run_off_a_terminal_writes_a_status_line_every_interval_and_never_an_escape_code(slow, tmp_path):
    progress = _child(
        'download',
        '--progress-interval',
        '1',
        str(tmp_path / 'dest'),
        FORCE_COLOR='1',
        TTY_COMPATIBLE='1',
        XDG_STATE_HOME=str(tmp_path / 'state'),
    )
    slow.chunk_delay = 0
    quiet = _child('download', '--no-progress', str(tmp_path / 'other'), XDG_STATE_HOME=str(tmp_path / 'elsewhere'))

    assert progress.returncode == 0, progress.stderr
    for text in (progress.stdout, progress.stderr):
        assert '\x1b' not in text
        assert '\r' not in text
    assert all(re.match(f'{TIMESTAMP} ', line) for line in progress.stderr.splitlines())
    lines = status_lines(progress.stderr)
    assert stderr_lines(progress.stderr) == []
    downloading = [DOWNLOADING.match(line) for line in lines if line.startswith('Downloading: ')]
    assert len(downloading) >= 2
    assert all(downloading)
    shares = [float(match[2]) for match in downloading]
    assert shares == sorted(shares)
    assert [int(match[1]) for match in downloading] == sorted(int(match[1]) for match in downloading)
    assert lines[-1].startswith('Downloaded: 6 of 6 files, ')
    assert [line for line in lines if line.startswith('Downloaded: ')] == [lines[-1]]
    assert lines[0].startswith('Scanned disk:/: 6 files, 4 folders (')
    assert quiet.returncode == 0, quiet.stderr
    assert status_lines(quiet.stderr) == []
    assert progress.stdout.replace(str(tmp_path / 'state'), '<state>').replace(str(tmp_path / 'dest'), '<dest>') == (
        quiet.stdout.replace(str(tmp_path / 'elsewhere'), '<state>').replace(str(tmp_path / 'other'), '<dest>')
    )


def test_status_lines_merged_into_the_report_fall_between_its_start_line_and_its_summary(slow, dest):
    merged = _child('download', '--progress-interval', '1', str(dest), merged=True)

    assert merged.returncode == 0, merged.stdout
    lines = merged.stdout.splitlines()
    start = lines.index(next(line for line in lines if line.startswith('Downloading 6 files (')))
    first_status = next(i for i, line in enumerate(lines) if re.match(f'{TIMESTAMP} Downloading: ', line))
    final = next(i for i, line in enumerate(lines) if re.match(f'{TIMESTAMP} Downloaded: ', line))
    summary = lines.index(next(line for line in lines if line.startswith('Mirrored disk:/ to ')))
    assert start < first_status < final < summary


def test_the_last_line_of_each_phase_closes_it_once(disk, dest):
    scanned = runner.invoke(app, ['scan', '--progress-interval', '1'])
    first = run_download(str(dest))
    again = run_download(str(dest))

    assert scanned.exit_code == first.exit_code == again.exit_code == 0
    [scan_line] = status_lines(scanned.stderr)
    assert re.fullmatch(r'Scanned disk:/: 6 files, 4 folders \(.+\) listed in \d+ s', scan_line)
    assert [line.partition(':')[0] for line in status_lines(first.stderr)] == ['Downloaded']
    checked, downloaded = status_lines(again.stderr)
    assert re.fullmatch(f'Checked 6 files already in {re.escape(str(dest))} in \\d+ s', checked)
    assert downloaded.startswith('Downloaded: 0 of 0 files, 0 B in ')
    assert re.search(r'Checked 6 files already in .* in \d+ s', '\n'.join(log_messages()))


def test_the_last_status_line_counts_the_files_the_run_attempted_as_the_report_does(disk, dest, monkeypatch):
    held = 'disk:/Docs/readme.txt'
    disk.corrupt(held, 0)
    assert run_download(str(dest)).exit_code == 1
    disk.add_file('disk:/New/corrupt.bin', b'x' * 100, modified=MODIFIED)
    disk.add_file('disk:/New/Blocked/under.bin', b'y' * 100, modified=MODIFIED)
    disk.corrupt('disk:/New/corrupt.bin', 5)
    make_dir = localfs.make_dir

    def refusing(name, *, dir_fd):
        if name == 'Blocked':
            raise OSError(errno.EACCES, os.strerror(errno.EACCES))
        make_dir(name, dir_fd=dir_fd)

    monkeypatch.setattr(localfs, 'make_dir', refusing)

    result = run_download('--scan-max-age', '0', str(dest))

    assert result.exit_code == 1
    assert 'Downloading 2 files (200 B) to ' in result.stdout
    assert ': 0 downloaded (0 B), 5 skipped, 2 quarantined, 2 failed\n' in result.stdout
    [final] = [line for line in status_lines(result.stderr) if line.startswith('Download')]
    # The held copy and the failed folder are in the report but outside the files the run attempted.
    assert re.fullmatch(
        r'Downloaded: 2 of 2 files, 200 B in \d+ s, average .*; 0 to retry, 1 failed, 1 quarantined; 1 of 7 passes',
        final,
    )


def test_a_transfer_receiving_nothing_shows_as_without_data_then_as_to_retry(disk, dest):
    disk.inject('GET', '/download', Delay(30), where={'path': 'disk:/Docs/readme.txt'})

    result = _child(
        'download', '--download-timeout', '6', '--download-passes', '2', '--progress-interval', '1', str(dest)
    )

    assert result.returncode == 0, result.stderr
    lines = status_lines(result.stderr)
    stalled = next(i for i, line in enumerate(lines) if '; 1 active, 1 without data; 0 to retry,' in line)
    pausing = next(
        i for i, line in enumerate(lines) if re.search(r'; 1 to retry, .*; pass 2 of 2 starts in \d+ s$', line)
    )
    assert stalled < pausing
    assert lines[-1].startswith('Downloaded: 6 of 6 files, ')
    assert lines[-1].endswith('; 0 to retry, 0 failed, 0 quarantined; 2 of 2 passes')


@pytest.mark.parametrize('flag', ['--no-progress', '-q'])
def test_no_progress_and_quiet_leave_the_status_lines_to_the_log_file(disk, dest, flag):
    result = run_download(flag, str(dest))

    assert result.exit_code == 0, result.output
    assert result.stderr == ''
    status = [m for m in log_messages() if re.match(r'Scanned disk:/: .* listed in |Downloaded: ', m)]
    assert [m.partition(' ')[0] for m in status] == ['Scanned', 'Downloaded:']
