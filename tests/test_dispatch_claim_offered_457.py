"""#457 unit 2: a claim by a session that was offered the issue is that session's, even while a
finishing session's frozen `held` still lists it (the #43 exception for `woken`, extended to `offered`).

The incident (30.10, #414): finishing A parked n and unassigned it; B, started with n offered,
assigned it a minute later. The dispatcher's claims read is memoised for 1.5 min, so no tick saw n
unassigned: A's `held` kept n (not let go), `_refresh_claims` kept n out of B's claims, and A's end
then stripped B's assignee. Here the stale read is a fake gh that keeps returning the old claim list."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.agentops.dispatch import mark_finishing
from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, IssueStatus, _issue
from tests.test_agentops_dispatch_finishing import _dispatcher
from tests._area_rule import _no_area_rule  # noqa: F401

READY = ("ready-for-agent",)


class MemoGh(FakeGh):
    """`claimed_issues` returns `stale` while it is set: the 1.5 min memo of the real read."""

    stale: list[int] | None = None

    def claimed_issues(self):
        if self.stale is not None:
            return list(self.stale)
        return super().claimed_issues()


def _cur(d, session: str):
    return next(c for c in d.sessions if c.session == session)


def _rig(tmp_path: Path):
    """A holds #1 and is finishing; A parks and unassigns #1 inside the memo, so B starts offered
    #1 and A's `held` still lists it."""
    claude, gh = FakeClaude(), MemoGh(candidates=[_issue(1, 1)])
    clock = FakeClock()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)
    d.test_clock = clock
    msgs: list[str] = []
    d.log = msgs.append
    d.tick()
    a = claude.list_sessions()[0]["id"]
    gh.claim(1)
    d.tick()
    mark_finishing(a, finishing_dir=tmp_path / "finishing")
    gh.stale = [1]  # the memoised claims read still lists #1
    gh.issues[1] = IssueStatus(number=1, open=True, labels=READY, assignees=())  # A's park step
    gh.comment_body[1] = f"ORCH {a} parking checkpoint: parked"
    gh._candidates = [_issue(1, 1)]
    d.tick()  # the pool sees #1 unassigned: B starts offered #1
    b = next(iter({s["id"] for s in claude.list_sessions()} - {a}))
    assert [offered for offered, _ in claude.started] == [[1], [1]]
    return claude, gh, d, msgs, a, b


def test_row1_claim_by_the_offered_session_while_a_frozen_held_lists_it(tmp_path: Path):
    claude, gh, d, msgs, a, b = _rig(tmp_path)
    gh.claim(1)  # B assigns #1 to itself; the claims read never showed it unassigned
    gh._candidates = []
    d.tick()
    assert f"orch {b} claimed #1" in msgs
    assert 1 in _cur(d, b).held and 1 not in _cur(d, a).held
    # A ends with #1 parked
    claude.set_state(a, state="done", status="idle")
    d.tick()
    d.tick()
    assert 1 not in gh.removed_assignees
    assert gh.issues[1].assignees == ("bot",)
    assert 1 in _cur(d, b).held
    assert len(claude.started) == 2  # nothing offered #1 again
    # the pool still lists #1 (a stale read) and the wake grace is long past: no wake names it
    gh._candidates = [_issue(1, 1)]
    for _ in range(4):
        d.test_clock.advance(400)
        d.tick()
    events = [json.loads(l) for l in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e for e in events if e.get("kind") == "orch_wake"] == []  # no wake names #1 (the notices are not wakes)
    assert len(claude.started) == 2


def test_row1_a_ends_before_b_is_attributed(tmp_path: Path):
    """A's end is read before any tick attributed B's claim: A's parking checkpoint must not strip
    B's assignee (`_ended_outcome` reads A's own parking as parked, the assignee stays)."""
    claude, gh, d, msgs, a, b = _rig(tmp_path)
    gh.claim(1)
    gh._candidates = []
    outcomes = d._finish(_cur(d, a), "done")
    assert 1 not in gh.removed_assignees
    assert gh.issues[1].assignees == ("bot",)
    # A's end still records its own parking: the outcome, the event and the parked-by note
    assert outcomes == {1: "parked"}
    assert d._parked_by[1]["session"] == a
    ends = [json.loads(l) for l in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e.get("kind") == "orch_end" and e.get("session") == a and e.get("issue") == 1
               and e.get("outcome") == "parked" for e in ends)
    d.sessions.remove(_cur(d, a))
    d.tick()
    assert f"orch {b} claimed #1" in msgs  # the next tick attributes it to B
    assert 1 in _cur(d, b).held and gh.issues[1].assignees == ("bot",)
    assert 1 not in gh.removed_assignees


def test_row1_a_parking_still_resolves_as_parked_when_nobody_claims(tmp_path: Path):
    """No claim by B: A's end reads #1 unassigned as parked, as before."""
    claude, gh, d, msgs, a, b = _rig(tmp_path)
    gh._candidates = []
    outcomes = d._finish(_cur(d, a), "done")
    assert outcomes == {1: "parked"}
    assert gh.issues[1].assignees == ()


def test_row1_an_assignment_older_than_b_start_is_not_handed_to_b(tmp_path: Path):
    """The newest `assigned` event predates B's start: it is A's (or the owner's), not B's claim."""
    claude, gh, d, msgs, a, b = _rig(tmp_path)
    gh.claim(1)
    gh.assigned_at[1] = datetime.now(timezone.utc) - timedelta(hours=1)
    gh._candidates = []
    d.test_clock.advance(120)  # past the per-issue events read throttle
    d.tick()
    assert f"orch {b} claimed #1" not in msgs
    assert 1 not in _cur(d, b).held
    outcomes = d._finish(_cur(d, a), "done")  # A's end resolves it as before, nothing is skipped
    assert 1 in outcomes
