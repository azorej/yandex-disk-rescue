"""The progress of a run's slow phases: thread-safe states the engines update as they work, and the status lines of
their snapshots, which a renderer in another thread samples.

A state is updated from the thread doing the work and read from any thread; each update and each `snapshot()` holds
the state's lock for a few attribute changes only. Speeds are measured over the bytes the phase itself moved (never
the bytes a resumed file already held): the current speed over the last `SPEED_SECONDS`, and the ETA from the speed
over the last `ETA_SECONDS`, so one stall does not swing it. A speed measured over less than `MIN_SPEED_SECONDS`, which
the first bytes of a phase or a transfer would inflate, is not shown.
"""

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Literal, Protocol

from yandex_disk_rescue.units import format_duration, format_size

# A transfer with a connection open that has received nothing for this long since it got a connection, or since its
# last bytes, is shown as without data.
STALL_SECONDS = 5.0
SPEED_SECONDS = 60.0
TRANSFER_SPEED_SECONDS = 10.0
ETA_SECONDS = 15 * 60.0
MIN_SPEED_SECONDS = 1.0
_SAMPLE_SECONDS = 1.0

type TransferState = Literal[
    'waiting for room',
    'waiting for a connection',
    'downloading',
    'preallocating',
    'waiting to hash',
    'hashing',
    'reading back',
    'waiting for the copy',
    'copying',
]
type Outcome = Literal['downloaded', 'failed', 'quarantined']
type MirrorPhase = Literal['idle', 'checking', 'transferring', 'pausing']


class Snapshot(Protocol):
    def status_line(self, *, final: bool) -> str | None:
        """The status line of the phase (the last one of it when `final`), or `None` before the phase has started."""
        ...


class ProgressState(Protocol):
    def snapshot(self) -> Snapshot: ...


class _Rate:
    """The history of a growing byte count, one sample a second at most, kept for the last `horizon` seconds, the
    longest window it measures; not thread-safe."""

    def __init__(self, now: float, *, horizon: float) -> None:
        self._horizon = horizon
        self._samples: deque[tuple[float, int]] = deque([(now, 0)])

    def note(self, now: float, total: int) -> None:
        if now - self._samples[-1][0] >= _SAMPLE_SECONDS:
            self._samples.append((now, total))
        while len(self._samples) > 1 and self._samples[1][0] <= now - self._horizon:
            self._samples.popleft()

    def per_second(self, now: float, total: int, window: float) -> float | None:
        """The bytes a second from the newest sample at least `window` seconds old (the oldest one kept while none
        is) to `total` now; `None` while that sample is less than `MIN_SPEED_SECONDS` old."""
        since, base = self._samples[0]
        for moment, value in self._samples:
            if moment > now - window:
                break
            since, base = moment, value
        return (total - base) / (now - since) if now - since >= MIN_SPEED_SECONDS else None


@dataclass(frozen=True)
class Speeds:
    """The current speed and the one the ETA uses (`None` until measured over `MIN_SPEED_SECONDS`), and the average
    over the whole phase."""

    current: float | None
    for_eta: float | None
    average: float

    @classmethod
    def of(cls, rate: _Rate, now: float, started: float, moved: int) -> Speeds:
        elapsed = now - started
        return cls(
            current=rate.per_second(now, moved, SPEED_SECONDS),
            for_eta=rate.per_second(now, moved, ETA_SECONDS),
            average=moved / elapsed if elapsed > 0 else 0.0,
        )


def _speed(per_second: float) -> str:
    return f'{format_size(round(per_second))}/s'


def _current(per_second: float | None) -> str:
    """The current speed and the separator after it, or nothing before it is measured."""
    return '' if per_second is None else f'{_speed(per_second)}, '


def _eta(remaining: int, per_second: float | None) -> str:
    return f'ETA {format_duration(remaining / per_second)}' if per_second else 'ETA unknown'


def _share(done: int, total: int) -> str:
    """`done of total` bytes, with the percentage when there is anything to count."""
    share = f'{format_size(done)} of {format_size(total)}'
    return f'{share} ({done / total:.1%})' if total else share


def _count(count: int, noun: str) -> str:
    return f'{count} {noun}' if count == 1 else f'{count} {noun}s'


def _of(done: int, total: int, noun: str) -> str:
    return f'{done} of {_count(total, noun)}'


@dataclass(frozen=True)
class ScanSnapshot:
    root: str | None
    files: int
    folders: int
    size: int
    to_list: int
    elapsed: float

    def status_line(self, *, final: bool) -> str | None:
        if self.root is None:
            return None
        listed = f'{_count(self.files, "file")}, {_count(self.folders, "folder")} ({format_size(self.size)}) listed'
        if final:
            return f'Scanned {self.root}: {listed} in {format_duration(self.elapsed)}'
        return f'Scanning {self.root}: {listed}, {_count(self.to_list, "folder")} to list'


class ScanProgress:
    """A scan's walk: the files, folders and bytes listed so far, and the folders still to list."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._root: str | None = None
        self._started = time.monotonic()
        self._files = self._folders = self._size = self._to_list = 0

    def start(self, root: str) -> None:
        """The walk of `root` starts, with the root itself to list."""
        with self._lock:
            self._root, self._started, self._to_list = root, time.monotonic(), 1

    def listed_file(self, size: int) -> None:
        with self._lock:
            self._files += 1
            self._size += size

    def listed_folder(self) -> None:
        """A folder was listed in its parent; it is to list itself."""
        with self._lock:
            self._folders += 1
            self._to_list += 1

    def folder_done(self) -> None:
        """A folder to list was listed, or left out."""
        with self._lock:
            self._to_list -= 1

    def snapshot(self) -> ScanSnapshot:
        with self._lock:
            elapsed = time.monotonic() - self._started
            return ScanSnapshot(self._root, self._files, self._folders, self._size, self._to_list, elapsed)


@dataclass(frozen=True)
class MoveSnapshot:
    started: bool
    files: int
    size: int
    moved: int
    moved_size: int
    elapsed: float
    speeds: Speeds

    def status_line(self, *, final: bool) -> str | None:
        if not self.started:
            return None
        if final:
            return (
                f'Moved: {_of(self.moved, self.files, "file")}, {format_size(self.moved_size)} in '
                f'{format_duration(self.elapsed)}, average {_speed(self.speeds.average)}'
            )
        return (
            f'Moving: {_of(self.moved, self.files, "file")}, {_share(self.moved_size, self.size)}, '
            f'{_current(self.speeds.current)}{_eta(self.size - self.moved_size, self.speeds.for_eta)}'
        )


class MoveProgress:
    """The move of the partial files of downloads in progress to this run's partial folder: the files moved so far
    and the confirmed bytes copied."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        now = time.monotonic()
        self._started_at = now
        self._started = False
        self._files = self._size = self._moved = self._moved_size = 0
        self._rate = _Rate(now, horizon=ETA_SECONDS)

    def start(self, files: int, size: int) -> None:
        """The move of `files` partial files holding `size` confirmed bytes starts."""
        with self._lock:
            now = time.monotonic()
            self._started, self._started_at, self._rate = True, now, _Rate(now, horizon=ETA_SECONDS)
            self._files, self._size = files, size

    def copied(self, size: int) -> None:
        """`size` more confirmed bytes were copied to this run's partial folder."""
        with self._lock:
            self._moved_size += size
            self._rate.note(time.monotonic(), self._moved_size)

    def moved(self) -> None:
        """One more partial file was moved."""
        with self._lock:
            self._moved += 1

    def snapshot(self) -> MoveSnapshot:
        with self._lock:
            now = time.monotonic()
            return MoveSnapshot(
                started=self._started,
                files=self._files,
                size=self._size,
                moved=self._moved,
                moved_size=self._moved_size,
                elapsed=now - self._started_at,
                speeds=Speeds.of(self._rate, now, self._started_at, self._moved_size),
            )


@dataclass(frozen=True)
class ActiveTransfer:
    """A file in progress: its `held` bytes of `size`, its state (`downloading` while a connection of it is open),
    `idle`, the seconds since it last received bytes or got a connection (`None` while it has none open), `speed`, the
    bytes a second its connections received over the last `TRANSFER_SPEED_SECONDS` (`None` until measured over
    `MIN_SPEED_SECONDS`), and `copied`, the bytes of its copy to the destination written so far."""

    path: str
    size: int
    held: int
    state: TransferState
    idle: float | None
    speed: float | None
    copied: int

    @property
    def without_data(self) -> bool:
        return self.state == 'downloading' and self.idle is not None and self.idle >= STALL_SECONDS


@dataclass(frozen=True)
class MirrorSnapshot:
    phase: MirrorPhase
    destination: str
    to_check: int
    checked: int
    elapsed: float
    files: int
    size: int
    settled: dict[Outcome, int]
    done_size: int
    to_retry: int
    active: tuple[ActiveTransfer, ...]
    pass_number: int
    passes: int
    starts_in: float | None
    speeds: Speeds

    def status_line(self, *, final: bool) -> str | None:
        match self.phase:
            case 'idle':
                return None
            case 'checking':
                if final:
                    return (
                        f'Checked {_of(self.checked, self.to_check, "file")} already in {self.destination} in '
                        f'{format_duration(self.elapsed)}'
                    )
                return f'Checking {_count(self.to_check, "file")} already in {self.destination}: {self.checked} checked'
        settled = sum(self.settled.values())
        outcomes = (
            f'{self.to_retry} to retry, {self.settled["failed"]} failed, {self.settled["quarantined"]} quarantined'
        )
        limit = f' of {self.passes}' if self.passes else ''
        if final:
            passes = f'{self.pass_number}{limit} passes' if limit or self.pass_number > 1 else '1 pass'
            return (
                f'Downloaded: {_of(settled, self.files, "file")}, {format_size(self.done_size)} in '
                f'{format_duration(self.elapsed)}, average {_speed(self.speeds.average)}; {outcomes}; {passes}'
            )
        without_data = sum(1 for transfer in self.active if transfer.without_data)
        starts = '' if self.starts_in is None else f' starts in {format_duration(self.starts_in)}'
        return (
            f'Downloading: {_of(settled, self.files, "file")}, {_share(self.done_size, self.size)}, '
            f'{_current(self.speeds.current)}{_eta(self.size - self.done_size, self.speeds.for_eta)}; '
            f'{len(self.active)} active, {without_data} without data; {outcomes}; '
            f'pass {self.pass_number}{limit}{starts}'
        )


@dataclass
class _Active:
    path: str
    size: int
    held: int
    state: TransferState
    connected: float | None
    rate: _Rate
    connections: int = 0
    received: int = 0
    copied: int = 0


class MirrorProgress:
    """A mirror run: first the check of the files already in the destination, then the passes over the files to
    download.

    The transfer counts cover the files the run attempts. Each one settles once (downloaded or adopted, failed or
    quarantined); one deferred to a later pass waits as to retry, its partial file's bytes still counted, until it
    starts again or settles. A file in progress counts the bytes its partial file holds.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        now = time.monotonic()
        self._phase: MirrorPhase = 'idle'
        self._started = now
        self._destination = ''
        self._to_check = self._checked = 0
        self._files = self._size = 0
        self._settled: dict[Outcome, int] = {'downloaded': 0, 'failed': 0, 'quarantined': 0}
        self._settled_size = 0
        self._active: dict[int, _Active] = {}
        self._retry: dict[int, int] = {}
        self._received = 0
        self._rate = _Rate(now, horizon=ETA_SECONDS)
        self._pass_number, self._passes = 1, 0
        self._starts_at: float | None = None

    def start_checking(self, files: int, destination: str) -> None:
        """The check of `files` files already in `destination` starts."""
        with self._lock:
            self._phase, self._started = 'checking', time.monotonic()
            self._to_check, self._destination = files, destination

    def checked(self) -> None:
        with self._lock:
            self._checked += 1

    def start_transferring(self, files: int, size: int, *, passes: int) -> None:
        """The first of at most `passes` passes (0: no limit) over `files` files of `size` bytes starts."""
        with self._lock:
            now = time.monotonic()
            self._phase, self._started, self._rate = 'transferring', now, _Rate(now, horizon=ETA_SECONDS)
            self._files, self._size, self._passes = files, size, passes

    def pausing(self, number: int, seconds: float) -> None:
        """Pass `number` starts in `seconds`."""
        with self._lock:
            self._phase, self._pass_number = 'pausing', number
            self._starts_at = time.monotonic() + seconds

    def resumed(self) -> None:
        """The pass the run paused for starts."""
        with self._lock:
            self._phase, self._starts_at = 'transferring', None

    def begin(self, key: int, path: str, size: int) -> ActiveFile:
        """File `key` (`path`, `size` bytes) starts its turn; the bytes it held as to retry are left to the turn to
        count again."""
        with self._lock:
            self._retry.pop(key, None)
            rate = _Rate(time.monotonic(), horizon=TRANSFER_SPEED_SECONDS)
            self._active[key] = _Active(path, size, 0, 'waiting for a connection', None, rate)
        return ActiveFile(self, key, path)

    def defer(self, key: int) -> None:
        """File `key`'s turn ended with the file deferred to a later pass; its bytes stay counted."""
        with self._lock:
            active = self._active.pop(key)
            self._retry[key] = active.held

    def settle(self, key: int, size: int, outcome: Outcome) -> None:
        """File `key` of `size` bytes is done for this run, in its turn, after it was deferred, or without a turn."""
        with self._lock:
            self._active.pop(key, None)
            self._retry.pop(key, None)
            self._settled[outcome] += 1
            self._settled_size += size

    def snapshot(self) -> MirrorSnapshot:
        with self._lock:
            now = time.monotonic()
            active = tuple(
                ActiveTransfer(
                    path=a.path,
                    size=a.size,
                    held=a.held,
                    state=a.state,
                    idle=None if a.connected is None else max(now - a.connected, 0.0),
                    speed=a.rate.per_second(now, a.received, TRANSFER_SPEED_SECONDS),
                    copied=a.copied,
                )
                for a in self._active.values()
            )
            held = sum(a.held for a in self._active.values()) + sum(self._retry.values())
            return MirrorSnapshot(
                phase=self._phase,
                destination=self._destination,
                to_check=self._to_check,
                checked=self._checked,
                elapsed=now - self._started,
                files=self._files,
                size=self._size,
                settled=dict(self._settled),
                done_size=self._settled_size + held,
                to_retry=len(self._retry),
                active=active,
                pass_number=self._pass_number,
                passes=self._passes,
                starts_in=None if self._starts_at is None else max(self._starts_at - now, 0.0),
                speeds=Speeds.of(self._rate, now, self._started, self._received),
            )

    def _update(self, key: int, *, state: TransferState | None = None, held: int = 0, copied: int = 0) -> None:
        with self._lock:
            active = self._active[key]
            if state is not None:
                if active.connections:
                    raise RuntimeError(f'{active.path}: its state set to {state} with a connection open')
                active.state = state
            active.held += held
            active.copied += copied

    def _connection(self, key: int, change: Literal[1, -1]) -> None:
        with self._lock:
            active = self._active[key]
            now = time.monotonic()
            active.connections += change
            if active.connections < 0:
                raise RuntimeError(f'{active.path}: a connection closed that was never open')
            if change == 1:
                active.connected = now
                if active.connections == 1:
                    active.state, active.received = 'downloading', 0
                    active.rate = _Rate(now, horizon=TRANSFER_SPEED_SECONDS)
            elif active.connections == 0:
                active.state, active.connected = 'waiting for a connection', None

    def _received_bytes(self, key: int, size: int) -> None:
        with self._lock:
            active = self._active[key]
            if not active.connections:
                raise RuntimeError(f'{active.path}: bytes arrived with no connection open')
            now = time.monotonic()
            active.connected = now
            active.held += size
            active.received += size
            active.rate.note(now, active.received)
            self._received += size
            self._rate.note(now, self._received)

    def _cut(self, key: int) -> None:
        with self._lock:
            self._active[key].held = 0


class ActiveFile:
    """The progress of one file's turn in a `MirrorProgress`."""

    def __init__(self, progress: MirrorProgress, key: int, path: str) -> None:
        self._progress = progress
        self._key = key
        self.path = path

    def state(self, state: TransferState) -> None:
        """The file's state, while none of its connections is open."""
        self._progress._update(self._key, state=state)

    def connected(self) -> None:
        """One more connection of the file is open: the file is downloading."""
        self._progress._connection(self._key, 1)

    def disconnected(self) -> None:
        """A connection of the file closed; once none is open, the file waits for a connection again."""
        self._progress._connection(self._key, -1)

    def kept(self, size: int) -> None:
        """The partial file holds `size` more bytes kept from an earlier turn."""
        self._progress._update(self._key, held=size)

    def wrote(self, size: int) -> None:
        """`size` bytes arrived on an open connection and were written to the partial file."""
        self._progress._received_bytes(self._key, size)

    def cut(self) -> None:
        """The partial file was cut back to byte 0."""
        self._progress._cut(self._key)

    def copied(self, size: int) -> None:
        """`size` more bytes of the finished file were written to its copy in the destination."""
        self._progress._update(self._key, copied=size)


@dataclass(frozen=True)
class VerifySnapshot:
    started: bool
    files: int
    size: int
    checked: int
    done_size: int
    problems: int
    reading: tuple[str, int, int] | None
    elapsed: float
    speeds: Speeds

    def status_line(self, *, final: bool) -> str | None:
        if not self.started:
            return None
        problems = _count(self.problems, 'problem')
        if final:
            return (
                f'Verified: {_of(self.checked, self.files, "file")}, {format_size(self.done_size)} in '
                f'{format_duration(self.elapsed)}, average {_speed(self.speeds.average)}; {problems}'
            )
        line = (
            f'Verifying: {_of(self.checked, self.files, "file")}, {_share(self.done_size, self.size)}, '
            f'{_current(self.speeds.current)}{_eta(self.size - self.done_size, self.speeds.for_eta)}; {problems}'
        )
        if self.reading is not None:
            path, read, size = self.reading
            line += f'; reading {path} ({format_size(read)} of {format_size(size)})'
        return line


class VerifyProgress:
    """A part of a `verify` pass: the files and bytes of this part checked so far, the file being read, and the
    problems the report holds so far."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        now = time.monotonic()
        self._started_at = now
        self._started = False
        self._files = self._size = self._checked = self._done_size = self._problems = self._read = self._current = 0
        self._reading: tuple[str, int] | None = None
        self._rate = _Rate(now, horizon=ETA_SECONDS)

    def start(self, files: int, size: int) -> None:
        """The part over `files` files of `size` bytes starts."""
        with self._lock:
            now = time.monotonic()
            self._started, self._started_at, self._rate = True, now, _Rate(now, horizon=ETA_SECONDS)
            self._files, self._size = files, size

    def reading(self, path: str, size: int) -> None:
        """The check of the file `path` of `size` bytes starts."""
        with self._lock:
            self._reading, self._current = (path, size), 0

    def read(self, size: int) -> None:
        """`size` more bytes of the file being checked were hashed."""
        with self._lock:
            self._current += size
            self._read += size
            self._rate.note(time.monotonic(), self._read)

    def checked(self) -> None:
        """The check of the file being read ended."""
        with self._lock:
            if self._reading is None:
                raise RuntimeError('no file is being checked')
            self._checked += 1
            self._done_size += self._reading[1]
            self._reading, self._current = None, 0

    def problems(self, count: int) -> None:
        with self._lock:
            self._problems = count

    def snapshot(self) -> VerifySnapshot:
        with self._lock:
            now = time.monotonic()
            reading = None if self._reading is None else (self._reading[0], self._current, self._reading[1])
            return VerifySnapshot(
                started=self._started,
                files=self._files,
                size=self._size,
                checked=self._checked,
                done_size=self._done_size + (0 if reading is None else reading[1]),
                problems=self._problems,
                reading=reading,
                elapsed=now - self._started_at,
                speeds=Speeds.of(self._rate, now, self._started_at, self._read),
            )
