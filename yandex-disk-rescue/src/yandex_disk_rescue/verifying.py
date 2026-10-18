"""The verify engine: every file of a mirror checked against the manifest, with no network and nothing written to the
destination, and the lines summarizing the check.

The file entries are walked in path order, one at a time, on the calling thread. A file with a verified record for
its entry's current identity is looked at: nothing at its final path is *missing*; anything there other than a regular
file, or a file whose `lstat` fingerprint changed and whose content no longer matches the record, is *modified*; a
file with the recorded fingerprint whose content does not match, or that cannot be read, is *corrupt*. The content is
its size, then every checksum the record has; a match is *intact*. Each finding is committed to the manifest as soon
as it is made, an intact file drops its earlier finding, and `download` fetches again every file with a finding.

Any other file is reported without being read: under its finding when it has one (the copy `verify` found is still at
its path), else as quarantined when its quarantine record is for its current identity, else as not downloaded. A file
listed under its finding whose quarantine record is for its current identity has its repair quarantined, and its line
says so with the quarantine reason and the copy's path.
Paths with no manifest entry are never opened, listed or reported.

A check is one pass over the mirror, kept in the manifest until every file is checked: a pass stopped by Ctrl+C,
SIGTERM, SIGKILL or an unreadable destination continues where its committed cursor stands, and its report covers the
whole pass.

A read error stops the check instead of being recorded when the destination itself has become unreadable, as a
disconnected disk makes every file unreadable.
"""

import errno
import logging
import stat
import time
from collections.abc import Callable, Iterator
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
    VerifyPass,
)
from yandex_disk_rescue.mirroring import (
    Digest,
    QuarantinedFile,
    held_copy,
    is_held,
    quarantined_document,
    quarantined_line,
    quarantined_reason,
    size_mismatch,
    unencodable_reason,
)
from yandex_disk_rescue.progress import VerifyProgress
from yandex_disk_rescue.scanning import SHOWN_PATHS, listed

# The longest a pass keeps its cursor uncommitted while files are checked: a pass stopped by SIGKILL reads again at most
# the files checked in this time, and the one in flight.
CURSOR_SECONDS = 5.0

# A read failing with one of these means the device is gone, whatever the destination's path still answers.
_DEVICE_GONE_ERRNOS = frozenset({errno.ENODEV, errno.ENXIO, errno.ESTALE})
_log = logging.getLogger(__name__)


class DestinationUnreadableError(RescueError):
    """The destination itself cannot be read any more; the check stops with the findings made so far recorded."""

    def __init__(self, destination: Path, reason: str) -> None:
        super().__init__(f'Could not read {destination}: {reason}; verify stopped. Rerun it once the disk is back.')


@dataclass(frozen=True)
class Problem:
    """A file listed for `reason`; under a finding, `repair` is its quarantine record for its current identity: the
    download meant to repair it was quarantined."""

    path: str
    reason: str
    repair: QuarantinedFile | None = None


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

    def add(self, entry: MirrorEntry, finding: Finding) -> None:
        """List `entry` under `finding`, with its current quarantine record as its repair, if any."""
        repair = held_copy(entry, None) if is_held(entry) else None
        {'missing': self.missing, 'modified': self.modified, 'corrupt': self.corrupt}[finding.kind].append(
            Problem(entry.resource.path, finding.reason, repair)
        )

    def _lists(self) -> tuple[list[Problem] | list[QuarantinedFile], ...]:
        return self.missing, self.modified, self.corrupt, self.quarantined, self.not_downloaded


class VerifyRun:
    """One part of a `verify` pass over a mirror: the files still to read are counted on construction (`files`,
    `size`); `run` checks them, reporting each file and the bytes it hashes to `progress`.

    The pass checks the files in path order and moves its cursor (`VerifyPass.checked_through`) past each file once
    its check is recorded, committing it at least every `CURSOR_SECONDS` and whenever the run stops, so a later part
    reads only the files after the cursor. A file at or before the cursor counts by what the manifest holds: intact
    while it is verified for its current identity and has no finding.
    """

    def __init__(
        self, mirror: Mirror, destination: ReadOnlyDestination, verify_pass: VerifyPass, progress: VerifyProgress
    ) -> None:
        self._mirror = mirror
        self._destination = destination
        self._progress = progress
        self._entries = [e for e in mirror.entries() if e.resource.kind == 'file']
        through = verify_pass.checked_through
        verified = [e for e in self._entries if _verified_now(e)]
        pending = [e for e in verified if through is None or e.resource.path > through]
        self._pending = {e.entry_id for e in pending}
        self._verified = {e.entry_id for e in verified}
        self._through = through
        self._committed = through
        self._committed_at = time.monotonic()
        self.files = len(pending)
        self.size = sum(e.resource.size or 0 for e in pending)

    def run(self) -> VerifyReport:
        """Check every file still to read in path order, recording each finding as it is made, and end the pass.

        Raises `DestinationUnreadableError` when a read fails and the destination itself cannot be read, or the error
        says the device is gone; nothing is recorded for that file, and the cursor stays before it.
        """
        report = VerifyReport(remote_root=self._mirror.remote_root, destination=str(self._destination.path))
        self._progress.start(self.files, self.size)
        try:
            for entry in self._entries:
                self._report(entry, report)
                self._progress.problems(report.problems)
        except KeyboardInterrupt, DestinationUnreadableError:
            self._commit_cursor()
            raise
        self._mirror.end_verify_pass()
        report.not_downloaded += [Problem(e.path, unencodable_reason(e)) for e in self._mirror.unencodable()]
        report.not_downloaded.sort(key=lambda problem: problem.path)
        self._progress.problems(report.problems)
        return report

    def _report(self, entry: MirrorEntry, report: VerifyReport) -> None:
        if entry.entry_id in self._pending:
            self._check(entry, report)
            self._through = entry.resource.path
            if time.monotonic() - self._committed_at >= CURSOR_SECONDS:
                self._commit_cursor()
        elif entry.finding is not None:
            report.add(entry, entry.finding)
        elif entry.entry_id in self._verified:
            _count_intact(entry, report)
        elif is_held(entry):
            held = held_copy(entry, entry.failure)
            _log.debug('%s quarantined: kept at %s', entry.resource.path, held.copy)
            report.quarantined.append(held)
        else:
            reason = _not_downloaded_reason(entry)
            _log.debug('%s not downloaded: %s', entry.resource.path, reason)
            report.not_downloaded.append(Problem(entry.resource.path, reason))

    def _commit_cursor(self) -> None:
        if self._through is not None and self._through != self._committed:
            self._mirror.checked_through(self._through)
            self._committed = self._through
        self._committed_at = time.monotonic()

    def _check(self, entry: MirrorEntry, report: VerifyReport) -> None:
        self._progress.reading(entry.resource.path, entry.resource.size or 0)
        finding = _finding(entry, self._destination, self._progress.read)
        self._progress.checked()
        if finding is not None:
            _log.warning('%s %s: %s', entry.resource.path, finding.kind, finding.reason)
            self._mirror.record_finding(entry.entry_id, finding)
            report.add(entry, finding)
            return
        _log.debug('%s intact', entry.resource.path)
        _count_intact(entry, report)
        if entry.finding is not None:
            self._mirror.clear_finding(entry.entry_id)


def verify_summary(report: VerifyReport, *, limit: int | None = SHOWN_PATHS) -> list[str]:
    """The lines reporting a check: the counts, the files checked by size only, then each kind of problem with its
    reasons; each list shows its first `limit` members (`None`: all)."""
    lines = [
        f'Verified {report.destination} ({report.remote_root}): {report.intact} intact, '
        f'{len(report.missing)} missing, {len(report.modified)} modified, {len(report.corrupt)} corrupt, {len(report.quarantined)} quarantined, '
        f'{len(report.not_downloaded)} not downloaded'
    ]
    if report.size_only:
        lines.append(f'Checked by size only (no cloud checksum): {len(report.size_only)}')
        lines += listed('', report.size_only, len(report.size_only), '', limit=limit)
    for title, problems in (('Missing', report.missing), ('Modified', report.modified), ('Corrupt', report.corrupt)):
        lines += _problem_lines(title, _problems(problems), limit)
    lines += _problem_lines('Quarantined', [quarantined_line(held) for held in report.quarantined], limit)
    lines += _problem_lines('Not downloaded', _problems(report.not_downloaded), limit)
    return lines


def verify_document(report: VerifyReport) -> dict[str, object]:
    """The JSON object of a check: the counts of the summary's head line and the problems, and every list in full."""
    return {
        'destination': report.destination,
        'remote_root': report.remote_root,
        'counts': {
            'intact': report.intact,
            'size_only': len(report.size_only),
            'missing': len(report.missing),
            'modified': len(report.modified),
            'corrupt': len(report.corrupt),
            'quarantined': len(report.quarantined),
            'not_downloaded': len(report.not_downloaded),
            'problems': report.problems,
        },
        'size_only': report.size_only,
        'missing': [_finding_document(problem) for problem in report.missing],
        'modified': [_finding_document(problem) for problem in report.modified],
        'corrupt': [_finding_document(problem) for problem in report.corrupt],
        'quarantined': [quarantined_document(held) for held in report.quarantined],
        'not_downloaded': [{'path': problem.path, 'reason': problem.reason} for problem in report.not_downloaded],
    }


def _finding_document(problem: Problem) -> dict[str, object]:
    repair = None if problem.repair is None else {'reason': problem.repair.reason, 'copy': str(problem.repair.copy)}
    return {'path': problem.path, 'reason': problem.reason, 'repair_quarantined': repair}


def _problems(problems: list[Problem]) -> list[str]:
    return [f'{problem.path}: {problem.reason}{_repair(problem.repair)}' for problem in problems]


def _repair(held: QuarantinedFile | None) -> str:
    if held is None:
        return ''
    return f'; repair quarantined: {quarantined_reason(held)}; kept at {held.copy}'


def _problem_lines(title: str, shown: list[str], limit: int | None) -> list[str]:
    return [f'{title}: {len(shown)}', *listed('', shown, len(shown), '', limit=limit)] if shown else []


def _finding(entry: MirrorEntry, destination: ReadOnlyDestination, read: Callable[[int], None]) -> Finding | None:
    """What is wrong with the file at the entry's final path, or `None` when it is intact; `read` gets the size of
    each chunk hashed."""
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
                mismatch = _content_mismatch(file.chunks(), record.identity, read)
    except OSError as exc:
        if exc.errno in _DEVICE_GONE_ERRNOS or not destination.reachable():
            raise DestinationUnreadableError(destination.path, exc.strerror or str(exc)) from None
        return Finding('corrupt', f'could not read {local}: {exc.strerror}')
    if mismatch is None:
        return None
    return Finding('modified' if touched else 'corrupt', mismatch)


def _count_intact(entry: MirrorEntry, report: VerifyReport) -> None:
    report.intact += 1
    if _record(entry).verified_by == 'size':
        report.size_only.append(entry.resource.path)


def _content_mismatch(chunks: Iterator[bytes], identity: RemoteIdentity, read: Callable[[int], None]) -> str | None:
    digest = Digest()
    for chunk in chunks:
        digest.update(digest.size, chunk)
        read(len(chunk))
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
