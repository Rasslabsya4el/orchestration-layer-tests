"""#650: a ready issue assigned by a session that is gone is unassigned and offered again."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.test_agentops_dispatch import (
    FakeClaude, FakeClock, FakeGh, READY, _dispatcher, _issue,
)
from tools.agentops.dispatch import IssueStatus
from tests._area_rule import _no_area_rule  # noqa: F401


class ListingGh(FakeGh):
    """Candidates are the ready issues without an assignee, as the real `--search no:assignee`."""

    def ready_for_agent_issues(self):
        return [_issue(n) for n, s in sorted(self.issues.items())
                if s.open and "ready-for-agent" in s.labels and not s.assignees]


def _assigned(gh: FakeGh, n: int, ago: timedelta) -> None:
    gh.issues[n] = IssueStatus(number=n, open=True, labels=READY, assignees=("bot",))
    gh.assigned_at[n] = datetime.now(timezone.utc) - ago


def _setup(tmp_path: Path):
    claude, gh = FakeClaude(), ListingGh()
    msgs: list[str] = []
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=FakeClock(100000.0))
    d.log = msgs.append
    return claude, gh, d, msgs


def test_row1_no_live_session_unassigned_logged_and_offered(tmp_path: Path):
    claude, gh, d, msgs = _setup(tmp_path)
    _assigned(gh, 7, timedelta(hours=1))
    d.tick()
    assert gh.removed_assignees == [7]
    assert "orch-dispatch: #7 stale assignee (its session is gone), unassigned" in msgs
    assert claude.started and 7 in claude.started[0][0]


def test_row2_live_session_older_than_assignment_not_needed(tmp_path: Path):
    claude, gh, d, msgs = _setup(tmp_path)
    gh.issues[1] = IssueStatus(number=1, open=True, labels=READY, assignees=())
    d.tick()  # session starts, offered #1
    cur = d.current
    d.clock.advance(1800)
    gh.claim(1)
    _assigned(gh, 9, timedelta(hours=1))  # assigned before the session started (30 min ago)
    d.tick()
    d.tick()
    assert 9 in gh.removed_assignees
    assert cur.held == {1}


def test_row3_assigned_after_start_and_offered_is_kept(tmp_path: Path):
    claude, gh, d, msgs = _setup(tmp_path)
    gh.issues[5] = IssueStatus(number=5, open=True, labels=READY, assignees=())
    d.tick()
    assert claude.started[0][0] == [5]
    d.clock.advance(60)
    _assigned(gh, 5, timedelta(seconds=-30))  # assigned after the start
    d.tick()
    assert 5 not in gh.removed_assignees
    assert d.current.held == {5}


def test_row4_event_read_fails_left_alone_one_log_line(tmp_path: Path):
    claude, gh, d, msgs = _setup(tmp_path)
    _assigned(gh, 7, timedelta(hours=1))

    def boom(number):
        raise RuntimeError("gh down")

    gh.last_assigned_at = boom
    d.tick()
    d.tick()
    assert gh.removed_assignees == []
    assert len([m for m in msgs if "#7: events not readable, kept" in m]) == 1  # #50: the one wording


def _live_session(claude, gh, d):
    gh.issues[1] = IssueStatus(number=1, open=True, labels=READY, assignees=())
    d.tick()  # a session starts, offered #1
    d.clock.advance(1800)
    gh.claim(1)
    d.tick()
    return d.current


def test_row1_new_live_session_older_than_assignment_still_frees(tmp_path: Path):
    claude, gh, d, msgs = _setup(tmp_path)
    cur = _live_session(claude, gh, d)
    cur.finishing = True  # a long-lived finishing session: its `held` is frozen, 9 is not in it
    _assigned(gh, 9, timedelta(seconds=100))  # 3+ ticks ago, AFTER the session started
    d.tick()
    assert 9 in gh.removed_assignees
    assert cur.held == {1}


def test_row2_new_claimed_now_never_unassigned(tmp_path: Path):
    claude, gh, d, msgs = _setup(tmp_path)
    cur = _live_session(claude, gh, d)
    _assigned(gh, 9, timedelta(seconds=100))
    d.tick()  # a live session takes new work: the claim lands in its `claimed_now`
    assert 9 in cur.claimed_now
    assert 9 not in gh.removed_assignees


def test_row3_new_young_assignment_and_young_comment_kept_then_freed(tmp_path: Path):
    claude, gh, d, msgs = _setup(tmp_path)
    _assigned(gh, 7, timedelta(seconds=30))  # younger than 2 ticks
    _assigned(gh, 8, timedelta(hours=2))
    gh.comment_at[8] = datetime.now(timezone.utc) - timedelta(seconds=20)  # young comment
    d.tick()
    assert gh.removed_assignees == []
    gh.comment_at[8] = datetime.now(timezone.utc) - timedelta(seconds=300)  # comment aged
    d.tick()
    assert gh.removed_assignees == [8]


# -- round 2: re-adoption and hand-over must not free a live session's issue ----------------------


def _two_live_sessions(tmp_path: Path):
    """A state file of a previous dispatcher with two live non-finishing sessions (top-level keys carry nobody)."""
    claude, gh = FakeClaude(), ListingGh()
    for sid, held in (("S1", [10]), ("S2", [11])):
        claude.add_session(sid, name=f"orch #{held[0]}", state="working", status="busy", startedAt=int(time.time() * 1000) - 60_000)
        _assigned(gh, held[0], timedelta(hours=1))
    state = {"updated": "x", "session": None, "sessions": [
        {"session": "S1", "offered": [10], "held": [10], "preexisting": [11], "claimed_now": None,
         "finishing": False, "started_at_wall": time.time() - 60},
        {"session": "S2", "offered": [11], "held": [11], "preexisting": [10], "claimed_now": None,
         "finishing": False, "started_at_wall": time.time() - 60}]}
    (tmp_path / "orch_session.json").write_text(json.dumps(state), encoding="utf-8")
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=FakeClock(100000.0))
    d.log = lambda m: None
    return claude, gh, d


def test_A_restart_with_two_live_sessions_restores_each_held_and_frees_neither(tmp_path: Path):
    claude, gh, d = _two_live_sessions(tmp_path)
    d.tick()
    d.tick()
    by = {c.session: c for c in d.sessions}
    assert by["S1"].held == {10} and by["S2"].held == {11}
    assert gh.removed_assignees == []


def _handover(tmp_path: Path, body: str):
    claude, gh, d, msgs = _setup(tmp_path)
    claude.add_session("S2", name="orch #1", state="working", status="busy", startedAt=int(time.time() * 1000) - 60_000)
    d.tick()  # adopts S2 (no state file entry: held empty)
    assert "S2" in {c.session for c in d.sessions}
    d.sessions[0].finishing = True  # its claims are frozen: 12 is not in `held`, only the stale rule sees it
    _assigned(gh, 12, timedelta(seconds=100))  # after S2 started, older than 2 ticks
    gh.comment_at[12] = datetime.now(timezone.utc) - timedelta(seconds=300)
    gh.comment_body[12] = body
    d.tick()
    d.tick()
    return gh, d


def test_B_checkpoint_naming_a_live_session_keeps_the_issue(tmp_path: Path):
    gh, d = _handover(tmp_path, "Checkpoint 3 (ORCH S2): taken over")
    assert 12 not in {n for c in d.sessions for n in c.held}
    assert 12 not in gh.removed_assignees


def test_C_checkpoint_naming_a_dead_session_still_frees(tmp_path: Path):
    gh, d = _handover(tmp_path, "Checkpoint 3 (ORCH GONE): taken over")
    assert 12 in gh.removed_assignees
