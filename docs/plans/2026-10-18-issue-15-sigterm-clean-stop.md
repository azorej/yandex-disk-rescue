# Plan: SIGTERM stops scan, download and verify cleanly, like Ctrl+C

Issue: #15 (umbrella #9)
Branch: feat/sigterm-clean-stop (from `main` at 0d2b423)

## Context

Observed on `main` (0d2b423) against the fake server, with the real console script in a child process (a scratch
copy of the existing SIGINT subprocess tests, `signal.SIGINT` swapped for `signal.SIGTERM`):

```
download disk:/Big DEST, SIGTERM once 256 KiB of the 1 MiB file is on disk:
  returncode -15                       (a shell reports 143; killed by the signal, no exit status of its own)
  stderr     only the scan status line; no 'Interrupted; rerun to continue the download.'
  log        last record 'Pass 1 of 7 over 1 file (1.0 MiB) starts'; no end record at all
  journal    [(0, 1048576, 0, None)] with 327680 bytes in the partial file (no final checkpoint)
scan, SIGTERM mid-walk:
  returncode -15, stderr '', stdout ''; log has 'Scanning disk:/' and no end record; previous manifest intact
```

Nothing in `src/` installs a signal handler (`rg -n signal yandex-disk-rescue/src` finds none). Ctrl+C works only
because Python's `default_int_handler` raises `KeyboardInterrupt` (sync code: `verify`, and the sync parts of `scan`
and `download`), `asyncio.Runner` turns SIGINT into a cancellation of the main task (the async phases), and Typer
maps `KeyboardInterrupt` to exit 130 (`typer/core.py`, `except KeyboardInterrupt: raise Exit(130)`). SIGTERM — the
signal of `kill`, `systemctl stop`, `docker stop` and most supervisors — keeps its default action. The README works
around it today (`KillSignal=SIGINT` in the systemd unit, and a nohup paragraph saying SIGTERM "ends the run at
once"), and the reported case — a background job started by a script, whose SIGINT is ignored — has no clean stop
at all: Python leaves an inherited `SIG_IGN` for SIGINT in place, so `asyncio.Runner` installs no SIGINT handler
either.

## Decision for the user: the exit code of a SIGTERM stop

| Option | What a supervisor sees | Cost |
|---|---|---|
| **A. 143 (recommended)** | exit status 143 (128 + 15), the shell convention for "stopped by SIGTERM"; docker shows 143, as it does for any SIGTERM'd process | a dedicated `Terminated` exception so the code differs from Ctrl+C; systemd units list `SuccessExitStatus=143` |
| B. 130 | the same code as Ctrl+C | none in code; a script cannot tell `kill` from Ctrl+C, and 130 says "SIGINT" to anyone reading 128+N |

Recommendation: **A, 143.** It is the truthful 128+N code, what shells, docker and cron wrappers already report for
a SIGTERM'd process today (so existing scripts checking 143 keep working), and it keeps the documented exit-code
table meaningful: 130 means Ctrl+C, 143 means a supervisor stopped the run. A third option — re-raising SIGTERM with
the default action after the cleanup, so the process dies "by signal" and systemd counts it clean without
`SuccessExitStatus` — is rejected: the tool already chose an exit status (130) over re-raising for SIGINT, and a
self-kill after Typer's `sys.exit` needs a second entry point wrapper for no gain the `SuccessExitStatus` line does
not give.

The plan below is written for **A**. If the user picks **B**: `Terminated` is not introduced, the SIGTERM handlers
raise / cancel exactly as Ctrl+C does (plain `KeyboardInterrupt`), the end record stays `<command> interrupted
(exit 130)`, every 143 in the tests and docs below reads 130, and the README's systemd unit keeps
`SuccessExitStatus=130`. Everything else is unchanged.

## Goal

For the lifetime of `scan`, `download` and `verify` (the scope of `console.command_output`), SIGTERM stops the run
exactly as Ctrl+C does — the same cleanup path, the same final checkpoints (download), the same committed cursor
(verify), the same untouched manifest (scan), the same `Interrupted; …` line on stderr, the same `--json` document
(with `exit_code` 143) — and the process exits with status 143. This holds also when SIGINT is ignored (a background
job started by a script). Repeated SIGTERMs after the first are ignored: the run is already stopping, and a stop
that hangs is what the supervisor's SIGKILL is for.

## Non-goals

- SIGHUP / reload semantics (issue: out of scope; `nohup` ignores it anyway).
- Changing Ctrl+C behaviour (asyncio's "second Ctrl+C raises at once" stays as it is).
- The `auth` commands: short, stateless, no checkpoints — SIGTERM keeps its default action there.
- A Docker image for the tool (there is none; the README mentions `docker stop` only as a sender of SIGTERM).

## Stages

### Stage 1: SIGTERM is a clean stop of scan, download and verify (exit 143)

- **Contract:**
  - New module `yandex_disk_rescue/interrupts.py` (the only home of signal handling):
    - `class Terminated(KeyboardInterrupt)` — the run was stopped by SIGTERM. Subclassing `KeyboardInterrupt` is the
      point: every site that cleans up on Ctrl+C (`verifying.VerifyRun.run`'s cursor commit, `command_output`'s
      `Interrupted; …` line, the mirror engine's cancellation path) handles SIGTERM identically with no change.
    - `sigterm_stops() -> ContextManager[None]` — a stop scope with **one** "stopping" state for its whole
      lifetime. While active, the first SIGTERM the scope takes (by its own handler or through `run` below) makes the
      run stop; every later SIGTERM until the scope exits is ignored, wherever the main thread then is (inside
      `run`, in the sync cleanup, writing the `Interrupted; …` line, the end record or the JSON document). The first
      SIGTERM taken outside `run` raises `Terminated` in the main thread, as `default_int_handler` raises
      `KeyboardInterrupt` for Ctrl+C. On exit the SIGTERM handler that was there before is restored, whatever the
      exit. In a thread other than the main thread it does nothing (signals reach only the main thread; the
      in-process test at `test_download.py::test_a_slow_download_takes_a_checkpoint_every_checkpoint_seconds` runs
      the command in a worker thread and must keep passing).
    - `run[T](coro: Coroutine[Any, Any, T]) -> T` — `asyncio.run` for the commands: SIGINT keeps `asyncio.Runner`'s
      own handling. Inside an active `sigterm_stops` scope, while the coroutine runs, the scope's first SIGTERM
      cancels its task the way `asyncio.Runner` does for Ctrl+C (so every `except CancelledError` / `finally`
      cleanup in the engines runs), and when the task ends cancelled because of it, `run` raises `Terminated`; a
      SIGTERM the scope takes while no task is running (before it starts, after it is done) raises `Terminated` at
      once. Later SIGTERMs are ignored by the scope's shared state, also after `run` has returned. With no active
      scope (a worker thread) it is plain `asyncio.run`. It must not read or write private `asyncio` attributes
      (`Runner._interrupt_count` and the like).
  - `console.command_output` makes `interrupts.sigterm_stops()` its **outermost** scope: it covers the command body,
    the `Interrupted; …` line, `_logged_run`'s end record (`_log_end`, console.py:292-295) and `--json`'s
    `_print_document` (console.py:244-256). The only window it leaves is after `command_output` has returned or
    raised — everything is written by then, and a SIGTERM there ends the process by its default action while Typer
    turns the exception into the exit status. Keeping the scope until the process exits would leak the handler into
    an in-process caller (CliRunner), which the `test_output.py` check below forbids.
  - `commands/scan.py` and `commands/download.py` call `interrupts.run(...)` in place of `asyncio.run(...)`
    (`cli.py`'s two `asyncio.run` calls for `auth` stay — non-goal).
  - Ending with `Terminated`:
    - process exit status **143**; stderr ends with the command's `INTERRUPTED` line and nothing else (no `Aborted!`,
      no traceback). The conversion lives in the CLI layer that already maps exceptions to exit codes
      (`cli.RescueErrorGroup.invoke`), before Typer's `KeyboardInterrupt → Exit(130)` can see it; rename the class if
      its name stops describing what it does.
    - log end record `<command> interrupted by SIGTERM (exit 143)` (WARNING, file only); Ctrl+C keeps
      `<command> interrupted (exit 130)`.
    - `--json`: `exit_code` 143 and `error.message` = the command's `INTERRUPTED` message, same keys as a Ctrl+C
      document.
    - `console._result` / `_log_end` match `Terminated` before `KeyboardInterrupt`.
- **Behaviour to lock** (all subprocess tests against the fake server, via `mirror_tree.start_command`; extend the
  existing SIGINT tests by parametrizing over `(signal.SIGINT, 130)` / `(signal.SIGTERM, 143)` rather than copying
  them):
  - `tests/test_download.py::test_ctrl_c_mid_file_checkpoints_every_byte_written_and_the_rerun_resumes_there` —
    parametrized; for SIGTERM: return code 143, `stderr_lines == ['Interrupted; rerun to continue the download.']`,
    last log message `download interrupted by SIGTERM (exit 143)`, `_journal()` confirms every byte held on disk, and
    the rerun resumes from there (rename the test so it names both signals).
  - `tests/test_download.py` — new test, the reported background-job case and the ignored second SIGTERM: a
    download of `disk:/Big` started through `sh -c 'trap "" INT; exec yandex-disk-rescue download …'` (SIGINT
    ignored), with a new `sitecustomize` gate installed by the existing `_gated(monkeypatch, tmp_path, …)` helper:
    once the marker `go` exists, an fsync of a partial file creates `blocked-<n>` and waits for `release`, then runs
    the real fsync (unlike `_FSYNC_GATE`, it never fails, so the final checkpoint lands). Steps: wait for 256 KiB on
    disk, touch `go`, send SIGTERM, wait for a `blocked-*` marker (the stop is visibly being handled: the final
    checkpoint is in its fsync), send the second SIGTERM, sleep 0.5 s, touch `release`. Assert exit 143,
    `stderr_lines == ['Interrupted; rerun to continue the download.']`, the last log message `download interrupted
    by SIGTERM (exit 143)`, and `_journal()` confirms every byte on disk. Sending the second signal only after the
    marker matters: CPython coalesces two SIGTERMs that arrive before the handler runs, so back-to-back sends would
    prove nothing.
  - `tests/test_scan.py::test_a_scan_stopped_mid_walk_keeps_the_previous_scan` — add `signal.SIGTERM` to the
    parametrization: return code 143, `stderr_lines == ['Interrupted; the manifest holds the last completed scan.']`,
    stdout empty, `_rows() == previous`, and the rescan finds the new file. Use `start_command` instead of the
    hand-rolled `Popen` while there.
  - `tests/test_verify.py::test_ctrl_c_keeps_the_pass_and_the_rerun_continues_it_reporting_what_an_uninterrupted_verify_does`
    — parametrized; for SIGTERM: return code 143, the `Interrupted; rerun verify to continue where it stopped.` line,
    the pass cursor committed (`EARLY <= through < LATE`), and the rerun continues it with the same report as an
    uninterrupted verify.
  - `tests/test_verify.py::test_json_of_a_continued_pass_says_so_and_counts_what_an_uninterrupted_pass_does` —
    parametrized; for SIGTERM `(exit_code, error) == (143, {'message': verify_command.INTERRUPTED})`.
  - `tests/test_output.py` — an in-process `CliRunner` run of `scan` leaves `signal.getsignal(signal.SIGTERM)`
    what it was before (the handler is scoped to the run, not leaked into the host process).
  - `tests/test_download.py:2791` comment ("`asyncio.run` installs no signal handler outside the main thread")
    rewritten to name what holds now: the commands install no signal handler outside the main thread.
- **Deliverable:** `interrupts.py`; `command_output`, `scan`, `download` wired to it; the CLI maps `Terminated` to
  143; the tests above green; docs current.
- **Validation:** `mise run check`; `mise run test -- -k "ctrl_c or stopped_mid_walk or json_of_a_continued_pass or
  sigterm or getsignal"`; `rg -n "asyncio.run" yandex-disk-rescue/src` lists only `cli.py`; `rg -n -i "sigterm|sigkill|a kill|kill -INT|ctrl\+c|KillSignal|SuccessExitStatus|exit 130|exit_code. 130"
  README.md yandex-disk-rescue/README.md CHANGELOG.md yandex-disk-rescue/src` shows only current-state text.
- **Doc updates** (rewrite as current state, no "previously"):
  - `README.md` → *Running unattended / In the background with nohup*: rewrite the paragraph after the bullet list —
    `kill <pid>` (SIGTERM) stops a run as Ctrl+C does (last checkpoints, `Interrupted; …`, exit 143) also in a
    background job whose SIGINT is ignored; `kill -INT` works from an interactive shell only. Drop "SIGTERM … ends
    the run at once".
  - `README.md` → *As a systemd user service*: remove `KillSignal=SIGINT` from the unit, change
    `SuccessExitStatus=130` to `SuccessExitStatus=143`, and rewrite the bullet: `systemctl stop` (and a shutdown)
    sends SIGTERM, which stops the run as Ctrl+C does; `TimeoutStopSec` is how long systemd waits for the last
    checkpoints before SIGKILL; `SuccessExitStatus=143` counts a stopped run as a clean stop. The "Stopped with
    `systemctl stop` … `exit_code` 130" bullet reads 143.
  - `README.md` → the per-command Ctrl+C sentences (*Scanning a folder*, *Downloading*, *Verifying a mirror*):
    "Ctrl+C or SIGTERM (`kill`, `systemctl stop`, `docker stop`) prints `Interrupted; …` and exits with 130 (Ctrl+C)
    or 143 (SIGTERM)". The scan sentence "interrupted (Ctrl+C, a kill, a crash)" names SIGKILL for "a kill" where it
    means the uncatchable one.
  - `README.md` → *Output and logs*: the list of end records adds `interrupted by SIGTERM (exit 143)`.
  - `README.md` → every "a kill" that means the uncatchable stop now says SIGKILL, and the clean-stop lists name
    SIGTERM: :285 ("stopped anywhere on the way (Ctrl+C, SIGTERM, SIGKILL, a crash, a power cut)"); :414 ("whenever
    a download attempt ends — a dropped connection, a stall, Ctrl+C or SIGTERM. After SIGKILL, a crash or a power
    cut, at most one checkpoint interval …"); :764 (verify: "so SIGKILL or a power cut costs …"); :995 (JSON: the
    no-report cases read "Ctrl+C or SIGTERM").
  - Source docstrings and comments, as current state: `verifying.py:17-18` ("stopped by Ctrl+C, SIGTERM, SIGKILL
    or an unreadable destination"), `verifying.py:57` ("a pass stopped by SIGKILL reads again …"),
    `console.py:231` ("On Ctrl+C or SIGTERM stderr gets `interrupted`"; and the scope now also restores the SIGTERM
    handler), `mirroring.py:513` ("A cancellation of the walk from outside (Ctrl+C, SIGTERM) wins over both"),
    `manifest.py:5` (adds SIGTERM to the ways a scan stops).
  - `README.md` → *Exit codes*: new row `143 | Stopped with SIGTERM (kill, systemctl stop, docker stop): the same
    clean stop as Ctrl+C`.
  - `yandex-disk-rescue/README.md` → layout: add `interrupts.py` (SIGTERM as a clean stop: `Terminated`, the
    run-scoped handler, the asyncio runner of the commands); `console.py`'s line "the Ctrl+C line" → "the
    `Interrupted; …` line on Ctrl+C or SIGTERM"; :126 and :134 ("Ctrl+C message" of `scan` and `verify`, and
    `download`'s entry) → "interrupt message"; the manifest bullet "stopped by an error, Ctrl+C or SIGKILL" adds
    SIGTERM.
  - `CHANGELOG.md` → `## [Unreleased]` / `### Added`: "SIGTERM (`kill`, `systemctl stop`, `docker stop`) stops
    `scan`, `download` and `verify` cleanly, as Ctrl+C does, with exit code 143."

## Discovered issues

- **Fix in this PR:**
  - The README's systemd unit and nohup paragraph document a workaround (`KillSignal=SIGINT`, "SIGTERM ends the run
    at once") for exactly this gap; rewritten in Stage 1 above.
  - `test_scan.py::test_a_scan_stopped_mid_walk_keeps_the_previous_scan` hand-rolls the `Popen` that
    `mirror_tree.start_command` provides; switched while the test is extended.
- **Filed as issues:** None. Nothing out of scope turned up.

## Risks / open questions

- **Exit code (user decision):** 143 recommended; see the decision table. Choosing 130 shrinks the stage (no
  `Terminated`, no CLI mapping) but loses the distinction.
- **Second signal semantics differ by signal:** a second Ctrl+C raises at once (asyncio's behaviour, kept), a second
  SIGTERM is ignored for the rest of the `command_output` scope. Deliberate: supervisors escalate with SIGKILL, and an impatient repeated `kill` must not cut
  the final checkpoint short. Stated in the Goal so the reviewer can challenge it.
- **The tail after `command_output`:** a SIGTERM after every output is written and before Typer's `sys.exit`
  ends the process by the default action (-15 / shell 143) — nothing is lost, and the window is microseconds.
- **SIGTERM raised in sync code** lands wherever the main thread is, exactly like Ctrl+C today (e.g. mid
  `Manifest.open`); the manifest's transactions and the verify cursor commit already tolerate a `KeyboardInterrupt`
  there, so no new exposure — but the new tests only stop runs in their long phases.
- **Final-checkpoint time vs `TimeoutStopSec` / `docker stop -t`** (10 s default for docker): the cleanup is bounded
  (`DiskThreads` waits for one chunk / fsync per call), and a SIGKILL during it only loses the last checkpoint
  interval, as today. The README's systemd bullet says so; no code change.
