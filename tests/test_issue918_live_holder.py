"""#918: a live session that posted the issue's newest ORCH checkpoint holds it: the stale-assignee
rule does not unassign it and the dispatcher does not wake that session about it again."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, READY, _dispatcher, _issue
from tools.agentops.dispatch import IssueStatus, checkpoint_holder_of
from tests._area_rule import _no_area_rule  # noqa: F401

B = "addbbbbb"
N = 889
CHECKPOINT = f"**Checkpoint 9 (ORCH {B} takes #{N} over; unit 1 running).**"
MACHINE = "<!-- runs-daemon result -->\nrun finished"


class CommentsGh(FakeGh):
    """Candidates = ready issues without an assignee; comments kept as a list, the holder rule real."""

    def __init__(self) -> None:
        super().__init__()
        self.thread: list[dict] = []

    def ready_for_agent_issues(self):
        return [_issue(n, labels=("area:builds",)) for n, s in sorted(self.issues.items())
                if s.open and "ready-for-agent" in s.labels and not s.assignees]

    def last_comment_body(self, number):
        return str(self.thread[-1]["body"]) if self.thread else None

    def checkpoint_holder(self, number, session_ids=()):
        return checkpoint_holder_of(self.thread, tuple(session_ids))


def _world(tmp_path: Path, *, live: bool):
    claude, gh = FakeClaude(), CommentsGh()
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    gh.issues[N] = IssueStatus(number=N, open=True, labels=READY, assignees=("bot",))
    gh.assigned_at[N] = old
    gh.comment_at[N] = old
    if live:
        claude.add_session(B, name="orch B", state="working", status="busy",
                           startedAt=int(time.time() * 1000) - 600_000)
        state = {"updated": "x", "session": None, "sessions": [
            {"session": B, "offered": [], "held": [], "preexisting": [N], "claimed_now": None,
             "finishing": False, "started_at_wall": time.time() - 600}]}
        (tmp_path / "orch_session.json").write_text(json.dumps(state), encoding="utf-8")
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=FakeClock(100000.0))
    msgs: list[str] = []
    d.log = msgs.append
    return claude, gh, d, msgs


def _woke_about(claude, n: int) -> int:
    return sum(1 for _, m in claude.waked if f"#{n}" in m)


def test_row2_live_session_checkpoint_then_machine_comment_keeps_issue_no_wake(tmp_path: Path):
    claude, gh, d, msgs = _world(tmp_path, live=True)
    gh.thread = [{"body": "Checkpoint 8 (ORCH aaaaaaaa): parked"},  # A, ended
                 {"body": CHECKPOINT}, {"body": MACHINE}]
    gh.comment_body[N] = MACHINE
    for _ in range(3):
        d.tick()
        d.clock.advance(700)
    assert [m for m in msgs if "stale assignee" in m] == []
    assert gh.removed_assignees == []
    assert _woke_about(claude, N) == 0


def test_row2b_unassigned_candidate_not_woken_for_the_checkpoint_holder(tmp_path: Path):
    claude, gh, d, msgs = _world(tmp_path, live=True)
    gh.thread = [{"body": CHECKPOINT}, {"body": MACHINE}]
    gh.issues[N] = IssueStatus(number=N, open=True, labels=READY, assignees=())
    for _ in range(3):
        d.tick()
        d.clock.advance(700)
    assert _woke_about(claude, N) == 0


def test_row2c_a_foreign_comment_after_the_checkpoint_lets_the_wake_through(tmp_path: Path):
    claude, gh, d, msgs = _world(tmp_path, live=True)
    gh.thread = [{"body": CHECKPOINT}, {"body": "owner: please look at this"}]
    gh.issues[N] = IssueStatus(number=N, open=True, labels=READY, assignees=())
    d.tick()  # adopts B
    d.tick()
    assert _woke_about(claude, N) == 1


def test_row3_assignee_session_gone_and_nobody_live_posted_is_unassigned_once(tmp_path: Path):
    claude, gh, d, msgs = _world(tmp_path, live=False)
    gh.thread = [{"body": "Checkpoint 8 (ORCH aaaaaaaa): parked"}, {"body": MACHINE}]
    gh.comment_body[N] = MACHINE
    d.tick()
    d.tick()
    assert gh.removed_assignees == [N]
    assert len([m for m in msgs if f"#{N} stale assignee" in m]) == 1


def test_row2_red_on_main_emulation(tmp_path: Path, monkeypatch):
    """Without the holder rule (as on main) the same world unassigns the issue and wakes B about it."""

    import sys
    monkeypatch.setattr(sys.modules[__name__], "checkpoint_holder_of", lambda c, ids: (None, False))
    claude, gh, d, msgs = _world(tmp_path, live=True)
    gh.thread = [{"body": CHECKPOINT}, {"body": MACHINE}]
    gh.comment_body[N] = MACHINE
    for _ in range(3):
        d.tick()
        d.clock.advance(700)
    assert len([m for m in msgs if "stale assignee" in m]) == 1
    assert _woke_about(claude, N) >= 1
