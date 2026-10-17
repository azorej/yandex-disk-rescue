"""The verify engine: every file of a mirror checked against the manifest, with no network and nothing written to the
destination, and the lines summarizing the check.

The file entries are walked in path order, one at a time, on the calling thread. A file with a verified record for
its entry's current identity is looked at: nothing at its final path is *missing*; anything there other than a regular
file, or a file whose `lstat` fingerprint changed and whose content no longer matches the record, is *modified*; a
file with the recorded fingerprint whose content does not match, or that cannot be read, is *corrupt*. The content is
its size, then every checksum the record has; a match is *intact*. Each finding is committed to the manifest as soon
as it is made, an intact file drops its earlier finding, and `download` fetches again every file with a finding.

Any other file is reported without being read: under its finding when it has one (the copy `verify` found is still at
its path), else as quarantined when its quarantine record is for its current identity, else as not downloaded. Paths
with no manifest entry are never opened, listed or reported.

A read error stops the check instead of being recorded when the destination itself has become unreadable, as a
disconnected disk makes every file unreadable.
"""

import errno
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from yandex_disk_rescue.destination import ReadOnlyDestination
from yandex_disk_rescue.errors import RescueError
from yandex_disk_rescue.manifest import (
    DownloadRecord,
    Finding,
    LocalFingerprint,
    Mirror,
    MirrorEntry,
    RemoteIdentity,
)
from yandex_disk_rescue.mirroring import (
    Digest,
    QuarantinedFile,
    held_copy,
    is_held,
    size_mismatch,
    unencodable_reason,
)
from yandex_disk_rescue.scanning import display_path, listed

# A read failing with one of these means the device is gone, whatever the destination's path still answers.
_DEVICE_GONE_ERRNOS = frozenset({errno.ENODEV, errno.ENXIO, errno.ESTALE})


class DestinationUnreadableError(RescueError):
    """The destination itself cannot be read any more; the check stops with the findings made so far recorded."""

    def __init__(self, destination: Path, reason: str) -> None:
        super().__init__(f'Could not read {destination}: {reason}; verify stopped. Rerun it once the disk is back.')


@dataclass(frozen=True)
class Problem:
    path: str
    reason: str


@dataclass
class VerifyReport:
    """What a check found. Lists hold every member in path order; the summary shows the first few."""

    remote_root: str
    destination: str
    intact: int = 0
    size_only: list[str] = field(default_factory=list)
    missing: list[Problem] = field(default_factory=list)
    modified: list[Problem] = field(default_factory=list)
    corrupt: list[Problem] = field(default_factory=list)
    quarantined: list[QuarantinedFile] = field(default_factory=list)
    not_downloaded: list[Problem] = field(default_factory=list)

    @property
    def problems(self) -> int:
        return sum(map(len, self._lists()))

    def lists_with_problems(self) -> int:
        return sum(1 for problems in self._lists() if problems)

    def add(self, path: str, finding: Finding) -> None:
        {'missing': self.missing, 'modified': self.modified, 'corrupt': self.corrupt}[finding.kind].append(
            Problem(path, finding.reason)
        )

    def _lists(self) -> tuple[list[Problem] | list[QuarantinedFile], ...]:
        return self.missing, self.modified, self.corrupt, self.quarantined, self.not_downloaded


class VerifyRun:
    """One check of a mirror: the files to read are counted on construction (`files`, `size`); `run` checks them."""

    def __init__(self, mirror: Mirror, destination: ReadOnlyDestination) -> None:
        self._mirror = mirror
        self._destination = destination
        self._entries = [e for e in mirror.entries() if e.resource.kind == 'file']
        checked = [e for e in self._entries if _verified_now(e)]
        self._checked = {e.entry_id for e in checked}
        self.files = len(checked)
        self.size = sum(e.resource.size or 0 for e in checked)

    def run(self) -> VerifyReport:
        """Check every file in path order, recording each finding as it is made.

        Raises `DestinationUnreadableError` when a read fails and the destination itself cannot be read, or the error
        says the device is gone; nothing is recorded for that file.
        """
        report = VerifyReport(remote_root=self._mirror.remote_root, destination=str(self._destination.path))
        for entry in self._entries:
            if entry.entry_id in self._checked:
                self._check(entry, report)
            elif entry.finding is not None:
                report.add(entry.resource.path, entry.finding)
            elif is_held(entry):
                report.quarantined.append(held_copy(entry, entry.failure))
            else:
                report.not_downloaded.append(Problem(entry.resource.path, _not_downloaded_reason(entry)))
        report.not_downloaded += [
            Problem(display_path(e.path), unencodable_reason(e)) for e in self._mirror.unencodable()
        ]
        report.not_downloaded.sort(key=lambda problem: problem.path)
        return report

    def _check(self, entry: MirrorEntry, report: VerifyReport) -> None:
        finding = _finding(entry, self._destination)
        if finding is not None:
            self._mirror.record_finding(entry.entry_id, finding)
            report.add(entry.resource.path, finding)
            return
        report.intact += 1
        if _record(entry).verified_by == 'size':
            report.size_only.append(entry.resource.path)
        if entry.finding is not None:
            self._mirror.clear_finding(entry.entry_id)


def verify_summary(report: VerifyReport) -> list[str]:
    """The lines reporting a check: the counts, the files checked by size only, then each kind of problem with its
    reasons."""
    lines = [
        f'Verified {report.destination} ({report.remote_root}): {report.intact} intact, {len(report.missing)} missing, '
        f'{len(report.modified)} modified, {len(report.corrupt)} corrupt, {len(report.quarantined)} quarantined, '
        f'{len(report.not_downloaded)} not downloaded'
    ]
    if report.size_only:
        lines.append(f'Checked by size only (no cloud checksum): {len(report.size_only)}')
        lines += listed('', report.size_only, len(report.size_only), '')
    for title, problems in (('Missing', report.missing), ('Modified', report.modified), ('Corrupt', report.corrupt)):
        lines += _problem_lines(title, [f'{problem.path}: {problem.reason}' for problem in problems])
    lines += _problem_lines('Quarantined', [f'{q.path}: {q.reason}; kept at {q.copy}' for q in report.quarantined])
    lines += _problem_lines(
        'Not downloaded', [f'{problem.path}: {problem.reason}' for problem in report.not_downloaded]
    )
    return lines


def _problem_lines(title: str, shown: list[str]) -> list[str]:
    return [f'{title}: {len(shown)}', *listed('', shown, len(shown), '')] if shown else []


def _finding(entry: MirrorEntry, destination: ReadOnlyDestination) -> Finding | None:
    """What is wrong with the file at the entry's final path, or `None` when it is intact."""
    record = _record(entry)
    local = _local_path(entry)
    try:
        found = destination.lstat(local)
        if found is None:
            return Finding('missing', f'{local} is missing')
        if not stat.S_ISREG(found.st_mode):
            return Finding('modified', f'{local} is no longer a regular file')
        with destination.existing_file(local) as file:
            opened = file.stat()
            if not stat.S_ISREG(opened.st_mode):
                return Finding('modified', f'{local} is no longer a regular file')
            touched = LocalFingerprint.of(opened) != record.local
            if opened.st_size != record.identity.size:
                mismatch = size_mismatch(record.identity, opened.st_size)
            else:
                mismatch = _content_mismatch(file.chunks(), record.identity)
    except OSError as exc:
        if exc.errno in _DEVICE_GONE_ERRNOS or not destination.reachable():
            raise DestinationUnreadableError(destination.path, exc.strerror or str(exc)) from None
        return Finding('corrupt', f'could not read {local}: {exc.strerror}')
    if mismatch is None:
        return None
    return Finding('modified' if touched else 'corrupt', mismatch)


def _content_mismatch(chunks: Iterator[bytes], identity: RemoteIdentity) -> str | None:
    digest = Digest()
    for chunk in chunks:
        digest.update(digest.size, chunk)
    return digest.mismatch(identity)


def _verified_now(entry: MirrorEntry) -> bool:
    record = entry.download
    return record is not None and record.state == 'verified' and record.identity == RemoteIdentity.of(entry.resource)


def _not_downloaded_reason(entry: MirrorEntry) -> str:
    record = entry.download
    if entry.failure is not None:
        return entry.failure
    if record is None:
        return 'not downloaded yet'
    if record.state == 'partial':
        return 'its download has not finished'
    return 'changed on the Disk since it was downloaded'


def _record(entry: MirrorEntry) -> DownloadRecord:
    if entry.download is None:
        raise ValueError(f'{entry.resource.path} has no download record')
    return entry.download


def _local_path(entry: MirrorEntry) -> PurePosixPath:
    if entry.local_path is None:
        raise ValueError(f'{entry.resource.path} has no local name')
    return entry.local_path
