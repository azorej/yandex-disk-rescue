"""Tests for `yandex-disk-rescue download` against the fake Disk API and download host, into a real `tmp_path`
destination, with the real manifest in the XDG state directory."""

import errno
import fcntl
import hashlib
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

import pytest
from fake_disk_server import Truncate
from typer.testing import CliRunner
from yandex_disk_rescue import localfs
from yandex_disk_rescue.cli import app
from yandex_disk_rescue.paths import manifest_path
from yandex_disk_rescue.units import format_size

runner = CliRunner()

TOKEN = 'y0_AgAAAAB-download-sentinel-token'
CYRILLIC = 'disk:/Docs/Отчёт за 2024 год.txt'
MODIFIED = datetime(2023, 2, 3, 4, 5, 6, 789012, tzinfo=UTC)
FILES = {
    'disk:/Photos/2024/a.jpg': b'a' * 70_000,
    'disk:/Photos/2024/b.jpg': b'b' * 3000,
    'disk:/Photos/old.bmp': b'bitmap',
    'disk:/Docs/readme.txt': b'read me',
    CYRILLIC: 'годовой отчёт'.encode(),
    'disk:/top.txt': b'',
}
SIZE_ONLY = 'disk:/Photos/old.bmp'
_STAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z')


@pytest.fixture
def disk(fake_disk_api, monkeypatch):
    """The fake API with a nested tree, an empty folder, a size-only file, a zero-byte file and a Cyrillic name."""
    monkeypatch.setenv('YANDEX_DISK_RESCUE_TOKEN', TOKEN)
    monkeypatch.setenv('YANDEX_DISK_RESCUE_API_INTERVAL_MS', '25')
    fake_disk_api.token = TOKEN
    for path, content in FILES.items():
        checksums = {'md5': None, 'sha256': None} if path == SIZE_ONLY else {}
        fake_disk_api.add_file(path, content, modified=MODIFIED, **checksums)
    fake_disk_api.add_dir('disk:/Empty')
    return fake_disk_api


@pytest.fixture
def dest(tmp_path):
    return tmp_path / 'dest'


def _download(*args: str):
    return runner.invoke(app, ['download', *args])


def _local(dest, remote_path: str):
    return dest / remote_path.removeprefix('disk:/')


def _transfers(fake, since: int = 0) -> list[str]:
    """The paths of the link requests and content requests after the first `since` requests."""
    return [r.path for r in fake.requests[since:] if r.path in ('/v1/disk/resources/download', '/download', '/storage')]


def _linked(fake) -> list[str]:
    """The remote paths download links were requested for."""
    return [r.params['path'] for r in fake.requests if r.path == '/v1/disk/resources/download']


def _replaced_runs(dest) -> list:
    return sorted((dest / '.yandex-disk-rescue' / 'replaced').iterdir())


def _overwrite_keeping_mtime(local, content: bytes) -> None:
    before = local.stat()
    with local.open('r+b') as file:
        file.write(content)
    os.utime(local, ns=(before.st_atime_ns, before.st_mtime_ns))


def _mtime_ns(moment: datetime) -> int:
    return int(moment.timestamp()) * 10**9 + moment.microsecond * 1000


@pytest.mark.parametrize('redirect', [False, True], ids=['direct', 'redirected'])
def test_a_fresh_mirror_holds_every_file_with_its_bytes_and_cloud_mtime(disk, dest, redirect):
    disk.redirect_downloads = redirect

    result = _download(str(dest))

    assert result.exit_code == 0, result.output
    for path, content in FILES.items():
        local = _local(dest, path)
        assert local.read_bytes() == content
        assert local.stat().st_mtime_ns == _mtime_ns(MODIFIED)
    assert (dest / 'Empty').is_dir()
    assert list((dest / '.yandex-disk-rescue' / 'partial').iterdir()) == []
    total = format_size(sum(len(content) for content in FILES.values()))
    lines = result.stdout.splitlines()
    assert lines[lines.index(f'Downloading 6 files ({total}) to {dest}') :] == [
        f'Downloading 6 files ({total}) to {dest}',
        f'Mirrored disk:/ to {dest}: 6 downloaded ({total}), 0 skipped, 0 failed',
        'Verified by size only (no cloud checksum): 1',
        f'  {SIZE_ONLY}',
        f'Manifest: {manifest_path()}',
    ]
    assert lines[0].startswith('Scanned disk:/: 6 files, 4 folders')
    if redirect:
        assert len([r for r in disk.requests if r.path == '/storage']) == len(FILES)


def test_a_rerun_skips_verified_files_without_reading_them(disk, dest):
    assert _download(str(dest)).exit_code == 0
    altered = {}
    for path, content in FILES.items():
        altered[path] = bytes(byte ^ 0xFF for byte in content)
        _overwrite_keeping_mtime(_local(dest, path), altered[path])
    earlier = len(disk.requests)

    rerun = _download(str(dest))

    assert rerun.exit_code == 0, rerun.output
    assert f'Downloading 0 files (0 B) to {dest}\n' in rerun.stdout
    assert f'Mirrored disk:/ to {dest}: 0 downloaded (0 B), 6 skipped, 0 failed\n' in rerun.stdout
    assert f'Verified by size only (no cloud checksum): 1\n  {SIZE_ONLY}\n' in rerun.stdout
    assert _transfers(disk, earlier) == []
    assert all(_local(dest, path).read_bytes() == content for path, content in altered.items())
    assert not (dest / '.yandex-disk-rescue' / 'replaced').exists()


def test_a_corrupted_download_fails_that_file_and_a_rerun_fetches_it(disk, dest):
    path = 'disk:/Photos/2024/a.jpg'
    disk.corrupt(path, offset=10)
    good = hashlib.md5(FILES[path]).hexdigest()
    corrupted = bytearray(FILES[path])
    corrupted[10] ^= 0xFF
    bad = hashlib.md5(corrupted).hexdigest()

    result = _download(str(dest))

    assert result.exit_code == 1
    assert f'5 downloaded ({format_size(sum(map(len, FILES.values())) - 70_000)}), 0 skipped, 1 failed\n' in (
        result.stdout
    )
    assert f'Failed: 1\n  {path}: MD5 mismatch (expected {good}, got {bad})\n' in result.stdout
    assert result.stderr == 'Error: 1 failed; see the list above.\n'
    assert not _local(dest, path).exists()
    assert list((dest / '.yandex-disk-rescue' / 'partial').iterdir()) == []

    disk.repair(path)
    earlier = len(disk.requests)
    rerun = _download(str(dest))

    assert rerun.exit_code == 0, rerun.output
    assert '1 downloaded (68.4 KiB), 5 skipped, 0 failed\n' in rerun.stdout
    assert _local(dest, path).read_bytes() == FILES[path]
    assert _transfers(disk, earlier) == ['/v1/disk/resources/download', '/download']


def test_each_checksum_the_cloud_provides_is_checked_and_size_only_files_only_by_size(disk, dest):
    content = b'sha-checked'
    disk.add_file('disk:/Checks/sha.bin', content, md5=None, sha256='0' * 64)
    disk.add_file('disk:/Checks/size-only.bin', b'size only', md5=None, sha256=None)
    disk.corrupt('disk:/Checks/size-only.bin', offset=0)
    disk.add_file('disk:/Checks/short.bin', b'0123456789', md5=None, sha256=None)
    disk.inject('GET', '/download', Truncate(after_bytes=4), where={'path': 'disk:/Checks/short.bin'})

    result = _download('disk:/Checks', str(dest))

    assert result.exit_code == 1
    actual = hashlib.sha256(content).hexdigest()
    assert (
        f'Mirrored disk:/Checks to {dest}: 1 downloaded (9 B), 0 skipped, 2 failed\n'
        'Verified by size only (no cloud checksum): 1\n'
        '  disk:/Checks/size-only.bin\n'
        'Failed: 2\n'
        f'  disk:/Checks/sha.bin: SHA-256 mismatch (expected {"0" * 64}, got {actual})\n'
        '  disk:/Checks/short.bin: the download failed: '
    ) in result.stdout
    assert (dest / 'size-only.bin').read_bytes() == bytes([ord('s') ^ 0xFF]) + b'ize only'
    assert not (dest / 'sha.bin').exists()
    assert not (dest / 'short.bin').exists()


def test_names_are_mapped_for_the_destination_and_kept_on_a_rerun(disk, dest):
    long_name = 'я' * 150 + '.txt'
    disk.add_file('disk:/Names/A.txt', b'upper')
    disk.add_file('disk:/Names/a.txt', b'lower')
    disk.add_file(f'disk:/Names/{long_name}', b'long')

    result = _download('disk:/Names', str(dest))

    assert result.exit_code == 0, result.output
    lower = f'a~{hashlib.sha256(b"a.txt").hexdigest()[:8]}.txt'
    [shortened] = [p.name for p in dest.iterdir() if p.name.startswith('я')]
    assert shortened.endswith(f'~{hashlib.sha256(long_name.encode()).hexdigest()[:8]}.txt')
    assert len(shortened.encode()) <= 255
    assert (dest / 'A.txt').read_bytes() == b'upper'
    assert (dest / lower).read_bytes() == b'lower'
    assert (dest / shortened).read_bytes() == b'long'
    assert (
        f'Renamed for the destination: 2\n  disk:/Names/a.txt -> {lower}\n  disk:/Names/{long_name} -> {shortened}\n'
    ) in result.stdout

    earlier = len(disk.requests)
    rerun = _download('disk:/Names', str(dest))

    assert rerun.exit_code == 0, rerun.output
    assert f'  disk:/Names/a.txt -> {lower}\n' in rerun.stdout
    assert _transfers(disk, earlier) == []
    assert sorted(p.name for p in dest.iterdir()) == sorted(['.yandex-disk-rescue', 'A.txt', lower, shortened])


def test_an_untracked_identical_file_is_adopted_without_a_download(disk, dest):
    readme = dest / 'Docs' / 'readme.txt'
    readme.parent.mkdir(parents=True)
    readme.write_bytes(FILES['disk:/Docs/readme.txt'])
    inode = readme.stat().st_ino

    result = _download(str(dest))

    assert result.exit_code == 0, result.output
    assert f'Mirrored disk:/ to {dest}: 5 downloaded ' in result.stdout
    assert ', 1 skipped, 0 failed\nAlready on disk and verified: 1\n  disk:/Docs/readme.txt\n' in result.stdout
    assert 'disk:/Docs/readme.txt' not in _linked(disk)
    assert readme.stat().st_ino == inode
    assert readme.stat().st_mtime_ns == _mtime_ns(MODIFIED)
    assert not (dest / '.yandex-disk-rescue' / 'replaced').exists()

    _overwrite_keeping_mtime(readme, b'X' * readme.stat().st_size)
    earlier = len(disk.requests)
    rerun = _download(str(dest))

    assert rerun.exit_code == 0, rerun.output
    assert '0 downloaded (0 B), 6 skipped, 0 failed\n' in rerun.stdout
    assert 'Already on disk' not in rerun.stdout
    assert _transfers(disk, earlier) == []
    assert readme.read_bytes() == b'X' * len(FILES['disk:/Docs/readme.txt'])


def test_a_mirror_whose_manifest_was_lost_is_adopted_without_any_transfer(disk, dest, tmp_path, monkeypatch):
    assert _download(str(dest)).exit_code == 0
    inodes = {path: _local(dest, path).stat().st_ino for path in FILES}
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'new-state'))
    earlier = len(disk.requests)

    result = _download(str(dest))

    assert result.exit_code == 0, result.output
    assert f'Mirrored disk:/ to {dest}: 0 downloaded (0 B), 6 skipped, 0 failed\n' in result.stdout
    assert 'Already on disk and verified: 6\n  disk:/Docs/readme.txt\n' in result.stdout
    assert _transfers(disk, earlier) == []
    assert {path: _local(dest, path).stat().st_ino for path in FILES} == inodes
    assert not (dest / '.yandex-disk-rescue' / 'replaced').exists()


@pytest.mark.parametrize('mine', [b'my own notes', b'not mine'], ids=['other-size', 'same-size'])
def test_an_untracked_different_file_is_moved_aside_and_the_cloud_file_placed(disk, dest, mine):
    readme = dest / 'Docs' / 'readme.txt'
    readme.parent.mkdir(parents=True)
    readme.write_bytes(mine)
    os.utime(readme, ns=(10**18, 10**18))

    result = _download(str(dest))

    assert result.exit_code == 0, result.output
    [run] = _replaced_runs(dest)
    assert _STAMP.fullmatch(run.name)
    assert (
        f'Moved aside to .yandex-disk-rescue/replaced/{run.name}/: 1\n  Docs/readme.txt\nManifest: '
    ) in result.stdout
    assert (run / 'Docs' / 'readme.txt').read_bytes() == mine
    assert (run / 'Docs' / 'readme.txt').stat().st_mtime_ns == 10**18
    assert readme.read_bytes() == FILES['disk:/Docs/readme.txt']
    assert '6 downloaded' in result.stdout


def test_a_run_never_reuses_the_replaced_folder_of_an_earlier_run(disk, dest):
    replaced = dest / '.yandex-disk-rescue' / 'replaced'
    now = datetime.now(UTC).replace(microsecond=0)
    taken = [(now + timedelta(seconds=s)).strftime('%Y-%m-%dT%H-%M-%SZ') for s in range(-1, 30)]
    for name in taken:
        (replaced / name).mkdir(parents=True)
    _local(dest, 'disk:/top.txt').write_bytes(b'mine')

    result = _download(str(dest))

    assert result.exit_code == 0, result.output
    [run] = [p for p in replaced.iterdir() if p.name not in taken]
    assert run.name.rpartition('-')[0] in taken
    assert run.name.endswith('-2')
    assert (run / 'top.txt').read_bytes() == b'mine'
    assert all(list((replaced / name).iterdir()) == [] for name in taken)


@pytest.mark.parametrize('fault', [None, 'corrupt'])
def test_a_file_changed_in_the_cloud_replaces_the_old_copy_only_once_verified(disk, dest, fault):
    path = 'disk:/Docs/readme.txt'
    readme = _local(dest, path)
    assert _download(str(dest)).exit_code == 0
    before = readme.stat()
    disk.add_file(path, b'read me, edited', modified=MODIFIED)
    if fault:
        disk.corrupt(path, offset=0)

    result = _download(str(dest))

    if fault:
        assert result.exit_code == 1
        assert f'Failed: 1\n  {path}: MD5 mismatch' in result.stdout
        assert readme.read_bytes() == FILES[path]
        assert (readme.stat().st_ino, readme.stat().st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
        assert not (dest / '.yandex-disk-rescue' / 'replaced').exists()
    else:
        assert result.exit_code == 0, result.output
        [run] = _replaced_runs(dest)
        assert '1 downloaded (15 B), 5 skipped, 0 failed\n' in result.stdout
        assert f'Moved aside to .yandex-disk-rescue/replaced/{run.name}/: 1\n  Docs/readme.txt\n' in result.stdout
        assert (run / 'Docs' / 'readme.txt').read_bytes() == FILES[path]
        assert readme.read_bytes() == b'read me, edited'


def test_things_in_the_way_are_moved_aside_and_links_are_never_followed(disk, dest, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    secret = outside / 'secret.txt'
    secret.write_bytes(b'linked')
    disk.add_file('disk:/Links/target.txt', b'linked')
    dest.mkdir()
    (dest / 'Photos').write_bytes(b'not a folder')
    (dest / 'Docs').symlink_to(outside)
    (dest / 'Links').mkdir()
    (dest / 'Links' / 'target.txt').symlink_to(secret)
    (dest / 'top.txt').mkdir()
    (dest / 'top.txt' / 'inside.txt').write_bytes(b'kept')

    result = _download(str(dest))

    assert result.exit_code == 0, result.output
    [run] = _replaced_runs(dest)
    assert (
        f'Moved aside to .yandex-disk-rescue/replaced/{run.name}/: 4\n  Docs\n  Links/target.txt\n  Photos\n  top.txt\n'
    ) in result.stdout
    assert f'Mirrored disk:/ to {dest}: 7 downloaded ' in result.stdout
    assert (run / 'Photos').read_bytes() == b'not a folder'
    assert (run / 'Docs').readlink() == outside
    assert (run / 'Links' / 'target.txt').readlink() == secret
    assert (run / 'top.txt' / 'inside.txt').read_bytes() == b'kept'
    assert sorted(outside.iterdir()) == [secret]
    assert secret.read_bytes() == b'linked'
    for path, content in {**FILES, 'disk:/Links/target.txt': b'linked'}.items():
        local = _local(dest, path)
        assert not local.is_symlink()
        assert local.read_bytes() == content


@pytest.mark.parametrize('change', ['content', 'mtime'])
def test_a_verified_file_touched_locally_is_hashed_again(disk, dest, change):
    path = 'disk:/Photos/2024/b.jpg'
    local = _local(dest, path)
    assert _download(str(dest)).exit_code == 0
    if change == 'content':
        local.write_bytes(b'B' * len(FILES[path]))
    else:
        os.utime(local, ns=(10**18, 10**18))
    earlier = len(disk.requests)

    result = _download(str(dest))

    assert result.exit_code == 0, result.output
    assert local.read_bytes() == FILES[path]
    assert local.stat().st_mtime_ns == _mtime_ns(MODIFIED)
    if change == 'content':
        [run] = _replaced_runs(dest)
        assert (run / 'Photos' / '2024' / 'b.jpg').read_bytes() == b'B' * len(FILES[path])
        assert _transfers(disk, earlier) == ['/v1/disk/resources/download', '/download']
    else:
        assert f'Already on disk and verified: 1\n  {path}\n' in result.stdout
        assert _transfers(disk, earlier) == []
        assert not (dest / '.yandex-disk-rescue' / 'replaced').exists()


def test_local_only_content_is_never_touched_or_reported(disk, dest):
    local_only = {
        '.Spotlight-V100/store.db': b'spotlight',
        '.DS_Store': b'finder',
        'notes.txt': b'my notes',
        'Photos/local-only.jpg': b'my photo',
        'Загрузки/файл.pdf': b'download',
        'Загрузки/inner/more.bin': b'more',
    }
    for relative, content in local_only.items():
        (dest / relative).parent.mkdir(parents=True, exist_ok=True)
        (dest / relative).write_bytes(content)
        os.utime(dest / relative, ns=(10**18, 10**18))
    stats = {relative: (dest / relative).stat() for relative in local_only}

    for _ in range(2):
        result = _download(str(dest))

        assert result.exit_code == 0, result.output
        for relative, content in local_only.items():
            now = (dest / relative).stat()
            assert (dest / relative).read_bytes() == content
            assert (now.st_mtime_ns, now.st_ino) == (stats[relative].st_mtime_ns, stats[relative].st_ino)
            assert PurePosixPath(relative).name not in result.stdout
        assert '.Spotlight-V100' not in result.stdout
        assert 'Загрузки' not in result.stdout
        assert 'Moved aside' not in result.stdout
        assert 'Already on disk' not in result.stdout
        assert not (dest / '.yandex-disk-rescue' / 'replaced').exists()


def test_a_hard_link_to_another_verified_file_is_never_replaced(disk, dest):
    disk.add_file('disk:/Pair/a.bin', b'first')
    disk.add_file('disk:/Pair/b.bin', b'second')
    assert _download('disk:/Pair', str(dest)).exit_code == 0
    (dest / 'b.bin').unlink()
    os.link(dest / 'a.bin', dest / 'b.bin')
    disk.add_file('disk:/Pair/b.bin', b'second, edited')
    inode = (dest / 'a.bin').stat().st_ino

    for _ in range(2):
        result = _download('disk:/Pair', str(dest))

        assert result.exit_code == 1
        assert (
            'Failed: 1\n  disk:/Pair/b.bin: b.bin is the same file as disk:/Pair/a.bin on this filesystem; '
            'it was left untouched\n'
        ) in result.stdout
        assert (dest / 'a.bin').read_bytes() == b'first'
        assert (dest / 'a.bin').stat().st_ino == inode
        assert not (dest / '.yandex-disk-rescue' / 'replaced').exists()


@pytest.mark.parametrize(
    ('status', 'reason'),
    [
        (403, 'Yandex Disk API returned HTTP 403: Injected HTTP 403'),
        (404, 'removed from the Disk after the scan'),
    ],
)
def test_a_link_refused_for_one_file_fails_only_that_file(disk, dest, status, reason):
    disk.inject('GET', '/v1/disk/resources/download', status, where={'path': 'disk:/Docs/readme.txt'})

    result = _download(str(dest))

    assert result.exit_code == 1
    assert '5 downloaded' in result.stdout
    assert f'Failed: 1\n  disk:/Docs/readme.txt: {reason}\n' in result.stdout
    assert not (dest / 'Docs' / 'readme.txt').exists()


def test_a_token_rejected_on_a_link_request_stops_the_run(disk, dest):
    disk.inject('GET', '/v1/disk/resources/download', 401)

    result = _download(str(dest))

    assert result.exit_code == 1
    assert result.stderr.startswith('Error: Yandex rejected the token (invalid, expired or revoked).')
    assert 'Mirrored' not in result.stdout


def test_a_name_that_is_not_valid_unicode_fails_that_entry_only(disk, dest):
    disk.add_file('disk:/Docs/bad\udc80.txt', b'unreachable')
    disk.add_file('disk:/Bad\udc81/inside.txt', b'unreachable too')

    result = _download(str(dest))

    assert result.exit_code == 1
    assert (
        'Failed: 2\n'
        '  disk:/Bad\\udc81: the folder name is not valid Unicode, so the Disk API cannot list it or download '
        'anything in it\n'
        '  disk:/Docs/bad\\udc80.txt: the name is not valid Unicode, so the Disk API cannot download it\n'
    ) in result.stdout
    assert '6 downloaded' in result.stdout
    assert all(_local(dest, path).read_bytes() == content for path, content in FILES.items())


@pytest.mark.parametrize('args', [[], ['a', 'b', 'c']], ids=['none', 'three'])
def test_download_takes_one_or_two_paths(disk, args):
    result = _download(*args)

    assert result.exit_code == 2
    assert disk.requests == []


def test_an_unusable_destination_is_a_usage_error_that_creates_nothing(disk, tmp_path):
    regular = tmp_path / 'file'
    regular.write_bytes(b'')
    orphan = tmp_path / 'missing' / 'dest'

    cases = [
        ([str(regular)], f'Error: Destination {regular} exists and is not a folder.\n'),
        (
            [str(orphan)],
            f'Error: The parent folder of {orphan} does not exist; download creates only the last folder of the '
            'destination.\n',
        ),
        ([str(tmp_path / 'x'), 'disk:/A'], 'disk:/A is a Disk path'),
    ]
    for args, message in cases:
        result = _download(*args)

        assert result.exit_code == 2
        assert message in result.stderr

    assert not orphan.parent.exists()
    assert not (tmp_path / 'x').exists()
    assert not manifest_path().exists()
    assert disk.requests == []


def test_a_destination_mirrors_one_remote_folder(disk, dest):
    assert _download('disk:/Photos', str(dest)).exit_code == 0

    result = _download('disk:/Docs', str(dest))

    assert result.exit_code == 2
    assert result.stderr == (
        f'Error: {dest} already mirrors disk:/Photos; download disk:/Photos there or choose another destination.\n'
    )
    assert not (dest / 'readme.txt').exists()


def test_an_invalid_download_timeout_is_a_usage_error(disk, dest):
    result = _download('--download-timeout', '0', str(dest))

    assert result.exit_code == 2
    assert 'must be an integer from 1 to 600' in result.stderr
    assert disk.requests == []


def test_a_destination_in_use_by_another_run_is_refused_whatever_the_manifest(disk, dest, tmp_path, monkeypatch):
    partial = dest / '.yandex-disk-rescue' / 'partial' / '999'
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b'another run is writing this')
    lock_fd = os.open(dest / '.yandex-disk-rescue' / 'lock', os.O_RDWR | os.O_CREAT)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'other-state'))
    try:
        result = _download(str(dest))
    finally:
        os.close(lock_fd)

    assert result.exit_code == 1
    assert result.stderr == (
        f'Error: Another yandex-disk-rescue run is using {dest}; wait for it to finish and try again.\n'
    )
    assert partial.read_bytes() == b'another run is writing this'
    assert _transfers(disk) == []


@pytest.mark.parametrize('rename', ['refused', 'replacing'])
def test_a_filesystem_without_the_no_replace_rename_stops_the_run_before_any_download(disk, dest, monkeypatch, rename):
    def unsupported(src, dst, *, src_dir_fd, dst_dir_fd):
        raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))

    def replacing(src, dst, *, src_dir_fd, dst_dir_fd):
        os.rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(localfs, 'rename_noreplace', unsupported if rename == 'refused' else replacing)

    result = _download(str(dest))

    assert result.exit_code == 1
    assert result.stderr == (
        f'Error: Could not write {dest}: its filesystem does not support renaming without replacing '
        '(RENAME_NOREPLACE), which download needs so it never overwrites a file.\n'
    )
    assert _transfers(disk) == []
    assert sorted(p.name for p in dest.iterdir()) == ['.yandex-disk-rescue']
