"""#1130: a dispatcher restart adopts every live autopilot session whatever its state, so a `blocked`
or `done` `autopilot #N` session is never left running beside a second session on the same goal.

Seams: `Dispatcher.tick()` (the autopilot tick, which adopts first) and `Dispatcher._adopt_live()` (the
ORCH adoption). Fake `claude` and `gh` layers from test_agentops_dispatch_autopilot, no real process."""

from __future__ import annotations

import os
import time

from tests.test_agentops_dispatch_autopilot import FakeClaude, FakeGh, _dispatcher, _events


def _adopt_events(tmp_path, session: str) -> list[dict]:
    return [e for e in _events(tmp_path) if e["kind"] == "autopilot_adopt" and e["session"] == session]


def _started_for(claude: FakeClaude, goal: int) -> list:
    return [s for g, s in claude.autopilot_started if g == goal]


# -- row 1: a `blocked` live autopilot is adopted, then the `blocked` rule ends it; no second start --

def test_row1_a_blocked_live_autopilot_is_adopted_and_ended_by_the_blocked_rule(tmp_path):
    claude = FakeClaude()
    claude.add_session("bl-486", name="autopilot #486", state="blocked", status="idle", pid=os.getpid(),
                       waitingFor="a question to the owner", startedAt=int(time.time() * 1000) - 60_000)
    gh = FakeGh()
    gh.autopilot_goal(486)
    gh.set_checkpoints(486, ["Autopilot checkpoint: asked about mortar_spectre_volley_overlap"])
    messages: list[str] = []
    d = _dispatcher(tmp_path, claude, gh)
    d.log = messages.append

    d.tick()

    assert len(_adopt_events(tmp_path, "bl-486")) == 1
    assert claude.stopped == ["bl-486"]  # the `blocked` rule ended it
    ends = [e for e in _events(tmp_path) if e["kind"] == "autopilot_end" and e["goal"] == 486]
    assert ends and ends[-1]["why"] == "blocked"
    assert _started_for(claude, 486) == []  # no `autopilot #486 started` line in this tick
    assert not any("autopilot #486 started" in m for m in messages)

    d.tick()  # the next tick restarts it: one session, not two

    assert len(_started_for(claude, 486)) == 1


# -- row 2: a `done` live autopilot is adopted and reset as idle, not left running ---------------

def test_row2_a_done_live_autopilot_is_adopted_and_reset_as_idle(tmp_path):
    claude = FakeClaude()
    claude.add_session("dn-487", name="autopilot #487", state="done", status="idle", pid=os.getpid(),
                       startedAt=int(time.time() * 1000) - 60_000)
    gh = FakeGh()
    gh.autopilot_goal(487)
    d = _dispatcher(tmp_path, claude, gh)

    d.tick()

    assert len(_adopt_events(tmp_path, "dn-487")) == 1
    assert claude.stopped == ["dn-487"]  # ended, not left running
    assert 487 not in d.autopilots
    assert 487 in d.autopilot_waiting  # reset as idle: it waits for a change or the backstop
    assert _started_for(claude, 487) == []  # not restarted on the same tick, nor by the idle reset


# -- row 3: a session `claude stop` marked `stopped` (no live process) is never adopted -----------

def test_row3_a_stopped_session_with_no_process_is_not_adopted(tmp_path):
    claude = FakeClaude()
    claude.add_session("st-488", name="autopilot #488", state="stopped", status="idle")
    gh = FakeGh()
    gh.autopilot_goal(488)
    d = _dispatcher(tmp_path, claude, gh)

    d.tick()

    assert _adopt_events(tmp_path, "st-488") == []
    assert 488 in d.autopilots and d.autopilots[488].session != "st-488"  # a fresh session, not the stopped one


# -- the ORCH side (dispatch.py `_adopt_live`): a `blocked` live `orch #N` session is adopted ----------

def test_orch_blocked_live_session_is_adopted(tmp_path):
    claude = FakeClaude()
    claude.add_session("orch-blk", name="orch #5", state="blocked", status="idle", pid=4444,
                       startedAt=int(time.time() * 1000) - 60_000)
    gh = FakeGh()
    d = _dispatcher(tmp_path, claude, gh)
    d.log = lambda _msg: None

    assert d._adopt_live() == ["orch-blk"]


def test_orch_stopped_session_is_not_adopted(tmp_path):
    claude = FakeClaude()
    claude.add_session("orch-stp", name="orch #6", state="stopped", status="idle")
    gh = FakeGh()
    d = _dispatcher(tmp_path, claude, gh)
    d.log = lambda _msg: None

    assert d._adopt_live() == []
