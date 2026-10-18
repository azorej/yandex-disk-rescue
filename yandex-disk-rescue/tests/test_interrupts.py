"""Tests for `interrupts`: how SIGINT and SIGTERM, sent one after the other, stop a coroutine run by `run`."""

import signal
import subprocess
import sys

import pytest

# Run in a child process: `interrupts.run` runs a coroutine that, once cancelled, cleans up until the file named by
# argv[1] exists, and prints each step and how the run ended.
_STOPPED_RUN = """
import asyncio, pathlib, sys
from yandex_disk_rescue import interrupts

release = pathlib.Path(sys.argv[1])


async def work():
    try:
        print('running', flush=True)
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        print('stopping', flush=True)
        try:
            while not release.exists():
                await asyncio.sleep(0.01)
            print('cleanup finished', flush=True)
        except asyncio.CancelledError:
            print('cleanup cut short', flush=True)
        raise


try:
    with interrupts.sigterm_stops():
        interrupts.run(work())
except KeyboardInterrupt as stopped:
    print(f'ended with {type(stopped).__name__}', flush=True)
"""


@pytest.mark.parametrize(
    ('first', 'second', 'cleanup', 'ended'),
    [
        (signal.SIGTERM, signal.SIGTERM, 'cleanup finished', 'Terminated'),
        (signal.SIGINT, signal.SIGTERM, 'cleanup finished', 'KeyboardInterrupt'),
        (signal.SIGTERM, signal.SIGINT, 'cleanup cut short', 'KeyboardInterrupt'),
    ],
    ids=['sigterm-then-sigterm', 'ctrl-c-then-sigterm', 'sigterm-then-ctrl-c'],
)
def test_a_signal_sent_while_the_run_stops_is_ignored_unless_it_is_ctrl_c_after_sigterm(
    tmp_path, first, second, cleanup, ended
):
    release = tmp_path / 'release'
    child = subprocess.Popen(
        [sys.executable, '-c', _STOPPED_RUN, str(release)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout.readline() == 'running\n'
        child.send_signal(first)
        assert child.stdout.readline() == 'stopping\n'
        child.send_signal(second)
        # Lets the second signal reach the run before the cleanup can finish.
        try:
            child.wait(timeout=0.3)
        except subprocess.TimeoutExpired:
            release.touch()
        stdout, stderr = child.communicate(timeout=30)
    finally:
        child.kill()
        child.communicate()

    assert child.returncode == 0, stderr
    assert stdout.splitlines() == [cleanup, f'ended with {ended}']
