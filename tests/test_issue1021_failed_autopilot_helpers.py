"""#1021: a session whose row is `failed` (or `stopped`) never stays live because of its subagents.

Seen 14.10: the autopilot #982 session went `failed` at 09:38 while its designer helper's transcript
never got a terminal notification; `_poll_autopilot` returned None (still live) for 30 min because
`running_subagent_ids` was non-empty, and nothing restarted it until a dispatcher restart. The ORCH
poll (`_poll_session`, the issue's `_poll_current`) held the same guard on a `failed` row (ORCH
e3042291, 09:48:45). Row 2 pins the unchanged case: a `working` + `status idle` row with an unfinished
subagent is still live.

Fake `claude.exe` / `gh` layers from the existing dispatcher tests; the transcripts are written as the
real layout (`<projects>/<uuid>.jsonl` and `<projects>/<uuid>/subagents/agent-<id>.jsonl`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_agentops_dispatch import FakeClaude as OrchClaude, FakeGh as OrchGh, _issue
from tests.test_agentops_dispatch_autopilot import FakeClaude as ApClaude, FakeGh as ApGh
from tests.test_agentops_dispatch_autopilot import _dispatcher as _autopilot_dispatcher
from tests.test_agentops_dispatch_finishing import _dispatcher as _orch_dispatcher
from tests._area_rule import _no_area_rule  # noqa: F401

#: the helper of the real 14.10 case (session 7e644c9d's `agent-af63acf9ac6f19d31.jsonl`).
HELPER = "af63acf9ac6f19d31"


def _transcripts(projects: Path, session_uuid: str, helper: str = HELPER) -> None:
    """A parent transcript with ordinary records (no task notification) and one subagent file."""

    projects.mkdir(parents=True, exist_ok=True)
    parent = {"type": "user", "message": {"role": "user", "content": "working on it"}}
    (projects / f"{session_uuid}.jsonl").write_text(json.dumps(parent) + "\n", encoding="utf-8")
    sub = projects / session_uuid / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / f"agent-{helper}.jsonl").write_text("{}\n", encoding="utf-8")


def _autopilot_with_helper(tmp_path: Path, *, state: str, status: str):
    """One `autopilot #70` session, started by the dispatcher, with a silent helper, then its row set."""

    claude, gh = ApClaude(), ApGh()
    gh.autopilot_goal(70)
    d = _autopilot_dispatcher(tmp_path, claude, gh)
    d.tick()
    session_id = d.autopilots[70].session
    session_uuid = "ap-uuid-70"
    claude._sessions[session_id].update(sessionId=session_uuid)
    _transcripts(tmp_path / "projects", session_uuid)
    claude.set_state(session_id, state=state, status=status)
    return claude, d, session_id


# -- row 1: a failed (or stopped) autopilot row with one silent helper ends and restarts -----------

@pytest.mark.parametrize("state", ["failed", "stopped"])
def test_row1_autopilot_row_ends_with_its_helper_silent_and_restarts_next_tick(tmp_path: Path, state: str):
    claude, d, session_id = _autopilot_with_helper(tmp_path, state=state, status="busy")

    d.tick()  # the poll: the row is over, helper or not
    assert 70 not in d.autopilots
    assert claude.stopped == [session_id]

    started_before = len(claude.autopilot_started)
    d.tick()  # the next tick starts one new session
    assert len(claude.autopilot_started) == started_before + 1
    assert 70 in d.autopilots and d.autopilots[70].session != session_id


def test_row1_the_failed_autopilot_is_logged_as_restarting_next_tick(tmp_path: Path):
    claude, d, session_id = _autopilot_with_helper(tmp_path, state="failed", status="idle")
    logs: list[str] = []
    d.log = logs.append

    d.tick()

    assert any(f"autopilot #70 reset (failed, session {session_id}); restarting next tick" in line
               for line in logs), logs


# -- row 2: a working autopilot with an unfinished helper is still live (unchanged) -----------------

def test_row2_working_idle_autopilot_with_an_unfinished_helper_stays_live(tmp_path: Path):
    claude, d, session_id = _autopilot_with_helper(tmp_path, state="working", status="idle")

    d.tick()

    assert 70 in d.autopilots and d.autopilots[70].session == session_id
    assert claude.stopped == []


# -- row 3 (rule 2): the ORCH poll gives a failed row the same rule --------------------------------

@pytest.mark.parametrize("state", ["failed", "stopped"])
def test_row3_orch_row_ends_with_its_helper_silent(tmp_path: Path, state: str):
    claude, gh = OrchClaude(), OrchGh(candidates=[_issue(1, 1)])
    d = _orch_dispatcher(tmp_path, claude=claude, gh=gh)
    d.tick()
    s1 = claude.list_sessions()[0]
    gh.claim(1)
    d.tick()  # S1 holds #1, working
    _transcripts(tmp_path / "projects", s1["sessionId"])
    claude.set_state(s1["id"], state=state, status="idle")
    gh._candidates = []

    d.tick()

    # a `failed` row is stopped by the dispatcher; a `stopped` row is already stopped (stop=False)
    assert s1["id"] in d.ended_sessions
    assert claude.stopped == ([s1["id"]] if state == "failed" else [])
