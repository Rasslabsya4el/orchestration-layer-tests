"""#242: #225's row 5 ("whole-disk sweep: tests pass") was closed with no test. This file supplies
one, on a fake process table and a fake dispatcher clock -- no real process is ever read or stopped.

Row numbers below match #242's own "Done when" table."""

from __future__ import annotations

from pathlib import Path

import tools.agentops.orphans as orphans_module
from tools.agentops.orphans import (
    ProcRow,
    WHOLE_DISK_MIN_AGE_SECONDS,
    find_stale_whole_disk_searches,
    is_whole_disk_search,
    sweep_stale_whole_disk_searches,
)
from tools.agentops.dispatch import Dispatcher

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh


def _row(pid: int, ppid: int, create_time: float, command_line: str) -> ProcRow:
    return ProcRow(pid=pid, ppid=ppid, create_time=create_time, command_line=command_line)


def _claude_row(pid: int = 1000, ppid: int = 1, create_time: float = 0.0) -> ProcRow:
    return _row(pid, ppid, create_time, "claude.exe -p --model haiku")


def _dispatcher(tmp_path: Path, *, claude: FakeClaude, gh: FakeGh, clock: FakeClock,
                whole_disk_sweep=None) -> Dispatcher:
    return Dispatcher(claude=claude, gh=gh, events_file=tmp_path / "events.jsonl",
                      state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                      clock=clock, whole_disk_sweep=whole_disk_sweep)


# -- row 1: an 11 min old whole-disk search tied to a live claude.exe -> stopped -----------------------

def test_row1_stale_whole_disk_search_tied_to_claude_is_stopped():
    claude_row = _claude_row(pid=1000, create_time=0.0)
    find_row = _row(100, ppid=1000, create_time=0.0, command_line="find / -name x")
    now = 660.0  # 11 min

    logged: list[str] = []
    killed: list[int] = []
    stopped = sweep_stale_whole_disk_searches(
        log=logged.append, table=lambda: [claude_row, find_row],
        killer=lambda pid: killed.append(pid) or True, now=now)

    assert killed == [100]
    assert [p.pid for p in stopped] == [100]
    assert len(logged) == 1


# -- row 2: the same, 9 min old -> untouched -----------------------------------------------------------

def test_row2_a_9_minute_old_whole_disk_search_is_left_alone():
    claude_row = _claude_row(pid=1000, create_time=0.0)
    find_row = _row(100, ppid=1000, create_time=0.0, command_line="find / -name x")
    now = 540.0  # 9 min

    killed: list[int] = []
    stopped = sweep_stale_whole_disk_searches(
        log=lambda _msg: None, table=lambda: [claude_row, find_row],
        killer=lambda pid: killed.append(pid) or True, now=now)

    assert killed == []
    assert stopped == []


# -- row 3: a rooted (non-whole-disk) find/grep is never caught -----------------------------------------

def test_row3_a_search_rooted_at_a_real_subdirectory_is_never_caught():
    claude_row = _claude_row(pid=1000, create_time=0.0)
    find_row = _row(100, ppid=1000, create_time=0.0, command_line="find src -name x")
    grep_row = _row(101, ppid=1000, create_time=0.0, command_line="grep -r foo tools/")
    now = 660.0  # 11 min

    killed: list[int] = []
    stopped = sweep_stale_whole_disk_searches(
        log=lambda _msg: None, table=lambda: [claude_row, find_row, grep_row],
        killer=lambda pid: killed.append(pid) or True, now=now)

    assert killed == []
    assert stopped == []


# -- row 4: a whole-disk search whose chain reaches no claude.exe (the owner's own shell) --------------

def test_row4_a_whole_disk_search_tied_to_no_claude_exe_is_left_alone():
    # no claude.exe row at all in the table: the parent (pid 1) is the owner's own shell, never a
    # tracked ORCH session.
    find_row = _row(100, ppid=1, create_time=0.0, command_line="find / -name x")
    now = 660.0  # 11 min

    killed: list[int] = []
    stopped = sweep_stale_whole_disk_searches(
        log=lambda _msg: None, table=lambda: [find_row],
        killer=lambda pid: killed.append(pid) or True, now=now)

    assert killed == []
    assert stopped == []


# -- row 5: the drive-root forms, and the non-matching commands beside them -----------------------------

def test_row5_every_drive_root_form_is_read_as_whole_disk_the_others_are_not():
    roots = ("/", "/c", "/c/", "c:\\", "c:/")
    exes = ("find", "grep", "rg")
    commands = [f"{exe} {root} -name x" if exe == "find" else f"{exe} pattern {root}"
               for root in roots for exe in exes]
    assert len(commands) == 15
    assert all(is_whole_disk_search(cmd) for cmd in commands)

    others = ("findstr /c:foo", "grep foo /cache", "rg foo d:/Work")
    assert not any(is_whole_disk_search(cmd) for cmd in others)


# -- row 6: the dispatcher's own throttle ---------------------------------------------------------------

def test_row6_the_dispatcher_throttles_the_sweep_to_once_a_minute(tmp_path: Path):
    calls: list[None] = []
    clock = FakeClock(start=1000.0)  # far past the sweep's 0.0 initial marker: the first tick sweeps
    claude, gh = FakeClaude(), FakeGh()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock,
                    whole_disk_sweep=lambda: calls.append(None))

    d.tick()  # t=1000: first eligible tick -> sweeps
    clock.advance(30.0)
    d.tick()  # t=1030: 30 s since the last sweep -> throttled, no sweep
    clock.advance(31.0)
    d.tick()  # t=1061: 61 s since the first sweep -> sweeps again

    assert len(calls) == 2


def test_row6_default_construction_never_calls_real_process_table(tmp_path: Path, monkeypatch):
    def _boom():
        raise AssertionError("real_process_table must never be called by a test")

    monkeypatch.setattr(orphans_module, "real_process_table", _boom)
    clock = FakeClock(start=1000.0)
    claude, gh = FakeClaude(), FakeGh()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)  # whole_disk_sweep defaults to None

    d.tick()  # does not raise: real_process_table is never reached


# -- row 7: a raising sweep is logged, the tick still ends -----------------------------------------------

def test_row7_a_raising_sweep_is_logged_and_the_tick_still_ends(tmp_path: Path):
    logged: list[str] = []
    clock = FakeClock(start=1000.0)
    claude, gh = FakeClaude(), FakeGh()

    def _raising_sweep():
        raise TimeoutError("boom")

    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock, whole_disk_sweep=_raising_sweep)
    d.log = logged.append

    report = d.tick()  # never raises: the sweep's own failure is caught and logged

    assert report is not None
    assert len(logged) == 1
    assert "whole-disk search sweep failed" in logged[0]
