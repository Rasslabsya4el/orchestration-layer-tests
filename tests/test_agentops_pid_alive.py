"""`util.holder_is_live` (issue #235): a live pid alone is not enough -- a freed pid can be
handed to an unrelated later process (measured on this machine: a freed pid reused after 303
spawns / 7.8 s of ordinary churn). A holder counts as live only when the pid is alive *and* its
own creation time is no later than the lock's `started` plus 2 s of slack.

Every scenario below drives the comparison directly (`_process_creation_time` monkeypatched),
never a real subprocess timed to land inside a particular second: real process creation has no
sub-second control from here, so the exact-second and +2 s/+3 s edges could not be produced
reliably any other way. The one real subprocess used (`live`) only proves the pid-alive half of
the check still gates everything else.

This file also carries #231's own regression bar (rows 1, 2 and 5 below), predating #235: the
issue's premise -- "`pid_alive` never reads the exit code" -- is false: `util.pid_alive` has
read `GetExitCodeProcess` since #195, and rows 1 and 2 already pass on `main`. They are kept
beside the two rows #231 asked for on the two callers that read a lock's holder through it
(`--ensure`'s `dispatcher_status`, `MergeLock`; both now folded into #235's `holder_is_live`),
and row 5 on the dispatcher loop's own survival of a failed tick.

Measured on this machine while working #231 (a throwaway probe, not shipped):

- an exited child whose handle is still held: `OpenProcess` succeeds (handle, error 0) and
  `GetExitCodeProcess` returns 0 -- `pid_alive` False. A held handle also *reserves* the pid,
  so it can never be reused while held; the handle theory therefore cannot produce a false
  live read at all.
- once the handle is closed: `OpenProcess` fails with error 87 -- `pid_alive` False; and the
  freed pid was handed to a new process again after 303 spawns / 7.8 s of ordinary churn on
  this machine. That recycle distance, not a stale handle, is what a lock holding a bare pid
  cannot survive across minutes -- #235's `holder_is_live` records the holder's start time
  beside its pid and compares it.

No real dispatcher is started by any test here: rows 3 and 5 drive a fake launch and a fake
`gh` runner. The only real processes are this file's own `python -c` children.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from timing_bound import bound

import pytest

from tools.agentops import util
from tools.agentops.dispatch import Dispatcher, GhCli, dispatcher_status, run_ensure
from tools.agentops.merge import MergeLock
from tools.agentops.util import holder_is_live, pid_alive


@pytest.fixture
def live_pid():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        yield proc.pid
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _started_at(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def _whole_second_now() -> float:
    """`started`'s own format loses the fractional second (`%H:%M:%S`); every edge below is
    computed off a whole-second epoch so formatting and re-parsing it round-trips exactly."""
    return float(int(time.time()))


# -- row 3: the edge, exactly at `started`, +2 s, and past it -------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="creation-time check is Windows-only")
def test_row3_created_the_same_second_as_started_is_live(live_pid, monkeypatch):
    started_epoch = _whole_second_now()
    monkeypatch.setattr(util, "_process_creation_time", lambda pid: started_epoch)
    assert holder_is_live(live_pid, _started_at(started_epoch)) is True


@pytest.mark.skipif(sys.platform != "win32", reason="creation-time check is Windows-only")
def test_row3_created_2s_after_started_is_still_live(live_pid, monkeypatch):
    started_epoch = _whole_second_now()
    monkeypatch.setattr(util, "_process_creation_time", lambda pid: started_epoch + 2.0)
    assert holder_is_live(live_pid, _started_at(started_epoch)) is True


@pytest.mark.skipif(sys.platform != "win32", reason="creation-time check is Windows-only")
def test_row3_created_3s_after_started_is_stale(live_pid, monkeypatch):
    started_epoch = _whole_second_now()
    monkeypatch.setattr(util, "_process_creation_time", lambda pid: started_epoch + 3.0)
    assert holder_is_live(live_pid, _started_at(started_epoch)) is False


# -- row 4: the creation time cannot be read -> the safe side (live) ------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="creation-time check is Windows-only")
def test_row4_unreadable_creation_time_reads_as_live(live_pid, monkeypatch):
    monkeypatch.setattr(util, "_process_creation_time", lambda pid: None)
    # `started` a year old: if the fallback did anything but trust `pid_alive` alone here, an
    # ancient `started` beside an unreadable creation time would read as stale.
    assert holder_is_live(live_pid, _started_at(time.time() - 365 * 86400)) is True


# -- row 5: no `started`, or one that does not parse -> the pid alone decides, as today -----------

def test_row5_missing_started_is_judged_by_pid_alone(live_pid, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(util, "_process_creation_time", lambda pid: calls.append(pid) or 0.0)
    assert holder_is_live(live_pid, None) is True
    assert calls == []  # never consulted: `started` did not parse, so the pid alone decides


def test_row5_unparsable_started_is_judged_by_pid_alone(live_pid, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(util, "_process_creation_time", lambda pid: calls.append(pid) or 0.0)
    assert holder_is_live(live_pid, "not a timestamp") is True
    assert calls == []


def test_row5_no_started_or_unparsable_never_raises_and_a_dead_pid_is_still_dead(live_pid):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=10)
    assert holder_is_live(dead.pid, None) is False
    assert holder_is_live(dead.pid, "garbage") is False


# -- #231's own regression bar (kept as-is; the two lock-holder rows now go through
#    `holder_is_live` above rather than `pid_alive` alone) ----------------------------------------

def _exited_child_whose_handle_is_still_held() -> subprocess.Popen:
    """A child that has run to the end while this process still holds its handle. The returned
    `Popen` is what holds it: a caller must keep the object alive for as long as it is testing
    against the pid."""

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    assert proc.wait(timeout=30) == 0
    return proc


# -- row 1: an exited child whose handle is still held reads as gone -----------------------------

def test_row1_exited_child_with_a_held_handle_is_not_alive():
    proc = _exited_child_whose_handle_is_still_held()
    # `proc` stays referenced to the end of the test: the handle is open the whole time.
    assert pid_alive(proc.pid) is False
    assert proc.returncode == 0


# -- row 2: a live child reads as live -----------------------------------------------------------

def test_row2_live_child_is_alive():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert pid_alive(proc.pid) is True
    finally:
        proc.terminate()
        proc.wait(timeout=30)
    assert pid_alive(proc.pid) is False


# -- row 3: `--ensure` starts one when the lock's holder is gone ---------------------------------

def test_row3_ensure_starts_one_when_the_lock_holder_exited_with_its_handle_held(tmp_path: Path,
                                                                                capsys):
    """The 00:09:34 reading of #231, reproduced through `--ensure`'s own function: the lock
    names a holder that has exited (its handle still held here), so `--ensure` must start one.
    `launch` is a fake -- no dispatcher process is created."""

    holder = _exited_child_whose_handle_is_still_held()
    lock_path = tmp_path / "dispatcher.lock"
    lock_path.write_text(json.dumps({"pid": holder.pid, "started": "2026-09-27 23:42:57"}),
                         encoding="utf-8")

    assert dispatcher_status(lock_path) == (False, holder.pid)

    launched: list[Path] = []

    def fake_launch(*, out_path: Path) -> int:
        launched.append(out_path)
        return 43908

    out_path = tmp_path / "dispatch.out"
    started = time.monotonic()
    rc = run_ensure(lock_path=lock_path, out_path=out_path, launch=fake_launch, root=tmp_path)
    elapsed = time.monotonic() - started

    assert rc == 0
    assert capsys.readouterr().out.strip() == "started pid 43908"
    assert launched == [out_path]
    # bound = max(floor, factor x a CPU spin timed here) (#826)
    bound_s = bound(5.0, 33.0)
    assert elapsed < bound_s, f"took {elapsed:.1f}s, bound {bound_s:.1f}s"
    assert holder.returncode == 0  # the handle was held for the whole reading


def test_row3_ensure_starts_nothing_beside_a_live_holder(tmp_path: Path, capsys):
    """The other half of the same branch: this process' own pid in the lock is live, so
    `--ensure` starts nothing and says so."""

    lock_path = tmp_path / "dispatcher.lock"
    # "irrelevant": an unparsable `started` (#235's `holder_is_live` then judges by the pid
    # alone, as every other live-holder fixture in this suite does); a real timestamp here
    # would have to match this process' own creation time to still read as live.
    lock_path.write_text(json.dumps({"pid": os.getpid(), "started": "irrelevant"}),
                         encoding="utf-8")

    def fake_launch(*, out_path: Path) -> int:  # pragma: no cover - must never run
        raise AssertionError("a dispatcher was launched beside a live one")

    rc = run_ensure(lock_path=lock_path, out_path=tmp_path / "dispatch.out", launch=fake_launch,
                     root=tmp_path)

    assert rc == 0
    assert capsys.readouterr().out.strip() == f"live pid {os.getpid()}"


# -- row 4: the merge lock takes over such a holder (the rule of #195) ---------------------------

def test_row4_merge_lock_takes_over_an_exited_holder_whose_handle_is_held(tmp_path: Path):
    """`MergeLock.acquire` reads its holder through the same `holder_is_live`: a holder that has
    exited while its handle is still held is an exited holder, taken over at once (no sleep,
    the stale file removed and the lock rewritten with this process' own pid)."""

    holder = _exited_child_whose_handle_is_still_held()
    lock_path = tmp_path / "merge.lock"
    lock_path.write_text(json.dumps({"pid": holder.pid, "started": "2026-09-27 23:42:57"}),
                         encoding="utf-8")

    lock = MergeLock(lock_path)
    started = time.monotonic()
    lock.acquire(max_age_seconds=5.0)  # a holder read as live would raise inside 5 s
    waited = time.monotonic() - started
    try:
        bound_s = bound(1.0, 6.0)
        assert waited < bound_s, f"waited {waited:.1f}s for an exited holder, bound {bound_s:.1f}s"
        assert json.loads(lock_path.read_text(encoding="utf-8"))["pid"] == os.getpid()
    finally:
        lock.release()
    assert not lock_path.exists()
    assert holder.returncode == 0  # the handle was held for the whole acquire


# -- row 5: three ticks failing on a `gh` timeout do not end the loop ----------------------------

class _NoSessions:
    """The least `Dispatcher` needs of a `claude.exe` for a tick that never gets past its first
    `gh` read: no live session to adopt, poll or end."""

    def list_sessions(self) -> list[dict]:
        return []


def _timing_out_gh(calls: list[list[str]]) -> GhCli:
    """A `GhCli` whose every call raises the exception the real 60 s `gh` timeout raises --
    the three failures of 2026-09-27 23:56:03, 23:57:44 and 23:59:25 in the test repo's
    `artifacts/runs/dispatch.out`."""

    def runner(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        calls.append(list(args))
        raise subprocess.TimeoutExpired(["gh", *args], 60.0)

    return GhCli(runner=runner)


def test_row5_three_ticks_failing_on_a_gh_timeout_keep_the_loop_running(tmp_path: Path):
    lines: list[str] = []
    calls: list[list[str]] = []
    slept: list[float] = []
    dispatcher = Dispatcher(
        claude=_NoSessions(), gh=_timing_out_gh(calls),
        events_file=tmp_path / "events.jsonl", state_file=tmp_path / "orch_session.json",
        projects_dir=tmp_path / "projects", log=lines.append, dry_run=True,
    )

    rc = dispatcher.run(ticks=3, interval=0.0, sleep=slept.append)

    assert rc == 0, "the loop gave up on a tick that raised TimeoutExpired"
    failed = [line for line in lines if line.startswith("tick failed (")]
    assert len(failed) == 3, f"expected one line per failed tick, got {failed}"
    assert all("TimeoutExpired" in line and line.endswith("; continuing") for line in failed), failed
    assert calls, "the fake gh was never called: the tick failed before its first gh read"
    # the third tick is the last: `run` sleeps between ticks only, never after the last one.
    assert len(slept) == 2, slept


def test_row5_the_loop_keeps_ticking_after_a_failure(tmp_path: Path):
    """The same guarantee stated as the dispatcher's job: a tick that raises is followed by
    another tick. The fake `gh` fails the first two ticks and works on the third, so the
    third tick's own work is proof the loop reached it."""

    lines: list[str] = []
    ticks_seen: list[int] = []

    def runner(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        ticks_seen.append(len(ticks_seen) + 1)
        if len(ticks_seen) <= 2:
            raise subprocess.TimeoutExpired(["gh", *args], 60.0)
        return subprocess.CompletedProcess(["gh", *args], 0, "[]", "")

    dispatcher = Dispatcher(
        claude=_NoSessions(), gh=GhCli(runner=runner),
        events_file=tmp_path / "events.jsonl", state_file=tmp_path / "orch_session.json",
        projects_dir=tmp_path / "projects", log=lines.append, dry_run=True,
    )

    rc = dispatcher.run(ticks=3, interval=0.0, sleep=lambda _s: None)

    assert rc == 0
    assert len([line for line in lines if line.startswith("tick failed (")]) == 2, lines
    assert len(ticks_seen) >= 3, ticks_seen


@pytest.mark.parametrize("raised", [KeyboardInterrupt, SystemExit])
def test_row5_run_does_not_survive_a_base_exception(tmp_path: Path, raised):
    """Named for #231's report: `run`'s guard is `except Exception` (dispatch.py), so a
    `BaseException` -- `KeyboardInterrupt`, `SystemExit` -- ends the loop. It still unwinds,
    which is why it cannot be the silent end of pid 29660: unwinding releases
    `dispatcher_lock`, and that lock was still on disk holding 29660 afterwards."""

    lines: list[str] = []

    class _Boom:
        def list_sessions(self) -> list[dict]:
            raise raised()

    dispatcher = Dispatcher(
        claude=_Boom(), gh=GhCli(runner=lambda args, stdin=None: subprocess.CompletedProcess(
            ["gh", *args], 0, "[]", "")),
        events_file=tmp_path / "events.jsonl", state_file=tmp_path / "orch_session.json",
        projects_dir=tmp_path / "projects", log=lines.append, dry_run=True,
    )

    with pytest.raises(raised):
        dispatcher.run(ticks=3, interval=0.0, sleep=lambda _s: None)
    assert [line for line in lines if line.startswith("tick failed (")] == []
