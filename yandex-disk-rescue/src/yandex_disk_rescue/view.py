"""The live view of a slow phase on a terminal, drawn with rich `Live` on stderr: the phase's status line, a bar of
its bytes (a spinner for a scan), and the files in progress with their state. The view is cleared when the phase ends.

While a view is shown, every other write to the terminal goes through it, so it lands above the view: a notice is
printed through the view's console with markup, emoji and highlighting off, and a report line is written to stdout
while the view is taken down.
"""

import threading
from collections.abc import Callable, Iterator
from typing import Self

from rich.cells import cell_len, split_graphemes
from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.live import Live
from rich.progress_bar import ProgressBar
from rich.segment import Segment
from rich.spinner import Spinner
from rich.text import Text

from yandex_disk_rescue.display import display_line
from yandex_disk_rescue.logs import STDERR, stderr_off
from yandex_disk_rescue.progress import (
    ActiveTransfer,
    MirrorSnapshot,
    MoveSnapshot,
    ProgressState,
    ScanSnapshot,
    Snapshot,
    VerifySnapshot,
)
from yandex_disk_rescue.units import format_duration, format_size

REFRESH_PER_SECOND = 2
SHOWN_TRANSFERS = 10


class LiveView:
    """The view of the phase `state` follows, on a terminal on `sys.stderr`, shown while it is used as a context
    manager and cleared, the cursor shown again, on every exit from it. Once stderr is off (`logs.stderr_off`), the
    view draws nothing."""

    def __init__(self, state: ProgressState) -> None:
        self._console = Console(
            file=STDERR, force_terminal=True, force_interactive=True, highlight=False, markup=False, emoji=False
        )
        self._state = state
        self._spinner = Spinner('dots')
        self._lock = threading.Lock()
        self._live = self._new_live()

    def __enter__(self) -> Self:
        with self._lock:
            self._live.start(refresh=True)
        return self

    def __exit__(self, *exc_info: object) -> None:
        with self._lock:
            self._live.stop()

    def notice(self, text: str) -> None:
        """Print `text` and a newline above the view, literally."""
        with self._lock:
            self._console.print(text, markup=False, highlight=False, emoji=False, soft_wrap=True)

    def above(self, write: Callable[[], None]) -> None:
        """Take the view down, call `write`, which writes whole lines to the terminal, then put the view back below
        them."""
        with self._lock:
            self._live.stop()
            write()
            self._live = self._new_live()
            self._live.start(refresh=True)

    def _new_live(self) -> Live:
        return Live(
            console=self._console,
            get_renderable=self._render,
            refresh_per_second=REFRESH_PER_SECOND,
            transient=True,
            redirect_stdout=False,
            redirect_stderr=False,
        )

    def _render(self) -> RenderableType:
        if stderr_off():
            return Text()
        return _View(self._state.snapshot(), self._spinner)


class _View:
    """One frame of the view: the lines of `snapshot` at the console's width."""

    def __init__(self, snapshot: Snapshot, spinner: Spinner) -> None:
        self._snapshot = snapshot
        self._spinner = spinner

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        line = self._snapshot.status_line(final=False)
        if line is None:
            return
        line = display_line(line)
        # A notice printed above the view comes with no wrapping, which the view, drawn in the same pass, must not
        # take on.
        wrapped = options.update(no_wrap=False, overflow='fold')
        for part in self._parts(line, options.max_width):
            if isinstance(part, Segment):
                yield part
            else:
                yield from console.render(part, wrapped)

    def _parts(self, line: str, width: int) -> Iterator[RenderableType | Segment]:
        match self._snapshot:
            case ScanSnapshot():
                self._spinner.update(text=Text(line))
                yield self._spinner
            case MirrorSnapshot(phase='checking') as mirror:
                yield Text(line)
                yield from _bar(mirror.checked, mirror.to_check, width)
            case MirrorSnapshot() as mirror:
                yield Text(line)
                yield from _bar(mirror.done_size, mirror.size, width)
                yield from _transfers(mirror.active, width)
            case MoveSnapshot() as move:
                yield Text(line)
                yield from _bar(move.moved_size, move.size, width)
            case VerifySnapshot() as verify:
                yield Text(line)
                yield from _bar(verify.done_size, verify.size, width)
                if verify.reading is not None:
                    path, read, size = verify.reading
                    yield from _rows([(path, _amount(read, size), 'reading')], width)


def _bar(done: int, total: int, width: int) -> Iterator[ProgressBar | Segment]:
    """A line with a bar `width` cells wide, full when there is nothing to count."""
    yield ProgressBar(total=total or 1, completed=done if total else 1, width=width)
    yield Segment.line()


def _transfers(active: tuple[ActiveTransfer, ...], width: int) -> Iterator[Text]:
    rows = [(t.path, _amount(t.held, t.size), _detail(t)) for t in active[:SHOWN_TRANSFERS]]
    yield from _rows(rows, width)
    if len(active) > SHOWN_TRANSFERS:
        yield Text(f'… and {len(active) - SHOWN_TRANSFERS} more')


def _amount(held: int, size: int) -> str:
    amount = f'{format_size(held)} of {format_size(size)}'
    return f'{amount} ({held / size:.1%})' if size else amount


def _detail(transfer: ActiveTransfer) -> str:
    match transfer.state:
        case 'downloading' if transfer.without_data and transfer.idle is not None:
            return f'no data for {format_duration(transfer.idle)}'
        case 'downloading' if transfer.speed is None:
            return '…'
        case 'downloading':
            return f'{format_size(round(transfer.speed))}/s'
        case 'copying' if transfer.size:
            return f'copying {transfer.copied / transfer.size:.1%}'
        case state:
            return state


def _rows(rows: list[tuple[str, str, str]], width: int) -> Iterator[Text]:
    """One line per `(path, amount, detail)`, the amounts and details in columns and each path shortened from the left
    to the room left."""
    if not rows:
        return
    amounts = max(cell_len(amount) for _, amount, _ in rows)
    details = max(cell_len(detail) for _, _, detail in rows)
    room = max(width - amounts - details - 6, 1)
    for path, amount, detail in rows:
        shown = _shortened(display_line(path), room)
        padding = ' ' * (room - cell_len(shown))
        yield Text(f'  {shown}{padding}  {" " * (amounts - cell_len(amount))}{amount}  {detail}', no_wrap=True)


def _shortened(path: str, room: int) -> str:
    """`path`, or its end after `…`, in at most `room` cells, cut between two graphemes."""
    graphemes, width = split_graphemes(path)
    if width <= room:
        return path
    start, kept = len(path), 0
    for begin, _, cells in reversed(graphemes):
        if kept + cells > room - 1:
            break
        start, kept = begin, kept + cells
    return f'…{path[start:]}'
