"""#1130 (night watch send-back, 2026-10-16 02:22): a dispatcher restart adopts a listed `autopilot #N`
session in `blocked` or `done` only when its process is alive. On main 4ed5deef every listed `done`
session was adopted, so 11 long-dead stretch-1 sessions were adopted and each fake reset restarted
#486's 15-min backstop.

Seam: `Dispatcher.tick()` (the autopilot tick, which adopts first). Fake `claude` and `gh` layers from
test_agentops_dispatch_autopilot, no real process. A "live" pid is this test process's own pid; a
"dead" pid is one of a child process that has exited."""

from __future__ import annotations

import os
import subprocess
import sys
import time

from tests.test_agentops_dispatch_autopilot import FakeClaude, FakeGh, _dispatcher, _events


def _adopt_events(tmp_path, session: str) -> list[dict]:
    return [e for e in _events(tmp_path) if e["kind"] == "autopilot_adopt" and e["session"] == session]


def _started_for(claude: FakeClaude, goal: int) -> list:
    return [s for g, s in claude.autopilot_started if g == goal]


def _exited_pid() -> int:
    """The pid of a child process that has already exited: no live process holds it."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=15)
    return child.pid


# -- (a) a listed `done` session with no live process is NOT adopted (fails on main 4ed5deef) -------

def test_a_done_session_with_no_pid_is_not_adopted(tmp_path):
    claude = FakeClaude()
    claude.add_session("dead-486", name="autopilot #486", state="done", status="idle",
                       startedAt=int(time.time() * 1000) - 3_600_000)  # no pid: no process
    gh = FakeGh()
    gh.autopilot_goal(486)
    d = _dispatcher(tmp_path, claude, gh)
    messages: list[str] = []
    d.log = messages.append

    d.tick()

    assert _adopt_events(tmp_path, "dead-486") == []
    assert not any("adopted live autopilot #486" in m for m in messages)
    assert 486 in d.autopilots and d.autopilots[486].session != "dead-486"  # a fresh session, not the dead one


def test_a_done_session_whose_pid_has_exited_is_not_adopted(tmp_path):
    claude = FakeClaude()
    claude.add_session("gone-486", name="autopilot #486", state="done", status="idle",
                       pid=_exited_pid(), startedAt=int(time.time() * 1000) - 3_600_000)
    gh = FakeGh()
    gh.autopilot_goal(486)
    d = _dispatcher(tmp_path, claude, gh)
    d.log = lambda _msg: None

    d.tick()

    assert _adopt_events(tmp_path, "gone-486") == []
    assert 486 in d.autopilots and d.autopilots[486].session != "gone-486"


# -- (b) a `blocked` session with a live pid is adopted, then the `blocked` rule; no second start -----

def test_b_a_blocked_session_with_a_live_pid_is_adopted_and_no_second_session_starts(tmp_path):
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
    assert _started_for(claude, 486) == []  # no `autopilot #486 started` in this tick
    assert not any("autopilot #486 started" in m for m in messages)

    d.tick()  # the next tick restarts it: one session, not two

    assert len(_started_for(claude, 486)) == 1


# -- (c) a `done` session with a live pid is adopted and handled as idle ---------------------------

def test_c_a_done_session_with_a_live_pid_is_adopted_and_handled_as_idle(tmp_path):
    claude = FakeClaude()
    claude.add_session("dn-487", name="autopilot #487", state="done", status="idle", pid=os.getpid(),
                       startedAt=int(time.time() * 1000) - 60_000)
    gh = FakeGh()
    gh.autopilot_goal(487)
    d = _dispatcher(tmp_path, claude, gh)
    d.log = lambda _msg: None

    d.tick()

    assert len(_adopt_events(tmp_path, "dn-487")) == 1
    assert claude.stopped == ["dn-487"]  # ended, not left running
    assert 487 not in d.autopilots
    assert 487 in d.autopilot_waiting  # reset as idle: waits for a change or the backstop
    assert _started_for(claude, 487) == []
