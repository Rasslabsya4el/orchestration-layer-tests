"""#280: the autopilot's idle wait outlives a dispatcher restart (the waits file). A "restart" is a
second Dispatcher built on the same file, its wall clock 40 s later."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from tests.test_agentops_dispatch_autopilot import (
    FakeClaude, FakeClock, FakeGh, FakeTimestamp,
)
from tools.agentops.autopilot import AUTOPILOT_IDLE_BACKSTOP_MINUTES
from tools.agentops.dispatch import Dispatcher

T0 = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
BACKSTOP = AUTOPILOT_IDLE_BACKSTOP_MINUTES * 60.0


def _build(tmp_path, claude, gh, clock, wall, logs):
    return Dispatcher(claude=claude, gh=gh, events_file=tmp_path / "events.jsonl",
                      state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                      autopilot_waits_file=tmp_path / "autopilot_waits.json",
                      clock=clock, timestamp=wall, log=logs.append)


def _idle_then_restart(tmp_path, state="working"):
    """Goal 50 started, idled (or, `state="done"`, ended done) at t0; a second dispatcher (fresh
    monotonic clock, wall +40 s)."""
    claude, gh, logs = FakeClaude(), FakeGh(), []
    gh.autopilot_goal(50)
    clock, wall = FakeClock(1000.0), FakeTimestamp(T0)
    d1 = _build(tmp_path, claude, gh, clock, wall, logs)
    d1.tick()
    claude.set_state(d1.autopilots[50].session, state=state, status="idle")
    d1.tick()
    assert 50 in d1.autopilot_waiting and len(claude.autopilot_started) == 1
    claude._sessions.clear()
    clock2, wall2 = FakeClock(5.0), FakeTimestamp(T0)
    wall2.advance(seconds=40)
    d2 = _build(tmp_path, claude, gh, clock2, wall2, logs)
    return claude, gh, logs, d2, clock2, wall2


def test_1_restart_keeps_wait_until_backstop(tmp_path):
    claude, gh, logs, d2, clock2, wall2 = _idle_then_restart(tmp_path)
    d2.tick()
    assert len(claude.autopilot_started) == 1  # no new start
    clock2.advance(BACKSTOP - 41)
    wall2.advance(seconds=BACKSTOP - 41)  # t0 + 15 min - 1 s
    d2.tick()
    assert len(claude.autopilot_started) == 1
    clock2.advance(2)
    wall2.advance(seconds=2)
    d2.tick()
    assert len(claude.autopilot_started) == 2
    assert json.loads((tmp_path / "autopilot_waits.json").read_text())["waits"] == []


def test_2_restart_then_new_comment_starts_next_tick(tmp_path):
    claude, gh, logs, d2, clock2, wall2 = _idle_then_restart(tmp_path)
    d2.tick()
    assert len(claude.autopilot_started) == 1
    gh.add_goal_comment(50, "a human note")
    d2.tick()
    assert len(claude.autopilot_started) == 2


def test_3_missing_or_garbage_file_is_todays_behaviour_with_one_log_line(tmp_path):
    for garbage in (False, True):
        sub = tmp_path / str(garbage)
        sub.mkdir()
        claude, gh, logs, d2, clock2, wall2 = _idle_then_restart(sub)
        path = sub / "autopilot_waits.json"
        if garbage:
            path.write_text("{not json", encoding="utf-8")
        else:
            path.unlink()
        logs.clear()  # d1's own first-tick line (a fresh install) is not the restart's
        d2.tick()
        assert len(claude.autopilot_started) == 2
        d2.tick()
        lines = [m for m in logs if m.startswith("autopilot waits:") and "unreadable" in m]
        assert len(lines) == 1


def test_4_goal_closed_while_down_no_start_and_wait_gone(tmp_path):
    claude, gh, logs, d2, clock2, wall2 = _idle_then_restart(tmp_path)
    gh.close_goal(50)
    d2.tick()
    assert len(claude.autopilot_started) == 1
    assert json.loads((tmp_path / "autopilot_waits.json").read_text())["waits"] == []


def test_5_done_wait_survives_a_restart_like_the_idle_wait(tmp_path):
    """#544 row 4: a `done` end writes the same waits-file entry; a restarted dispatcher keeps
    waiting until the backstop, and a new non-checkpoint comment wakes it next tick."""
    claude, gh, logs, d2, clock2, wall2 = _idle_then_restart(tmp_path, state="done")
    entries = json.loads((tmp_path / "autopilot_waits.json").read_text())["waits"]
    assert [e["goal"] for e in entries] == [50]
    d2.tick()
    assert 50 in d2.autopilot_waiting and len(claude.autopilot_started) == 1
    clock2.advance(BACKSTOP - 41)
    wall2.advance(seconds=BACKSTOP - 41)  # t0 + 15 min - 1 s
    d2.tick()
    assert len(claude.autopilot_started) == 1
    gh.add_goal_comment(50, "a human note")
    d2.tick()
    assert len(claude.autopilot_started) == 2
    assert [m for m in logs if m.startswith("autopilot #50 started (")][-1].endswith(
        "(change: comment on #50)")
