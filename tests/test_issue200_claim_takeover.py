"""#200: an issue a live session claimed after the ending session let it go is the claimant's.
The claim is attributed to it (logged, in its `held`), and the ending session's end never strips
the assignee nor lets the pool offer the issue to a third session.

The incident (18.10, #196): B started while A still held n, so n joined B's `preexisting`; A let n
go, B claimed it, and `_refresh_claims` kept it out of B's claims for good."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.agentops.dispatch import mark_finishing
from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, IssueStatus, _issue
from tests.test_agentops_dispatch_finishing import _dispatcher
from tests._area_rule import _no_area_rule  # noqa: F401

READY = ("ready-for-agent",)


def _rig(tmp_path: Path):
    """A holds #1 and is finishing; B started offered #2 while A still held #1."""
    claude, gh = FakeClaude(), FakeGh(candidates=[_issue(1, 1)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh)
    msgs: list[str] = []
    d.log = msgs.append
    d.tick()
    a = claude.list_sessions()[0]["id"]
    gh.claim(1)
    d.tick()
    mark_finishing(a, finishing_dir=tmp_path / "finishing")
    gh._candidates = [_issue(2, 2)]
    gh.issues[2] = IssueStatus(number=2, open=True, labels=READY, assignees=())
    d.tick()
    b = next(iter({s["id"] for s in claude.list_sessions()} - {a}))
    return claude, gh, d, msgs, a, b


def _cur(d, session: str):
    return next(c for c in d.sessions if c.session == session)


def _end(claude, d, session: str) -> None:
    claude._sessions.pop(session)
    d.tick()


def test_row1_claim_of_an_issue_the_ending_session_let_go_is_the_claimants(tmp_path: Path):
    claude, gh, d, msgs, a, b = _rig(tmp_path)
    assert 1 in _cur(d, b).preexisting  # B started while A held #1
    # A lets #1 go and the label is back: the pool has it again
    gh.issues[1] = IssueStatus(number=1, open=True, labels=READY, assignees=())
    gh._candidates = [_issue(1, 1)]
    d.tick()
    assert 1 in _cur(d, a).let_go
    gh.claim(1)  # B claims it while A is live and finishing
    gh.assigned_at[1] = datetime.now(timezone.utc) + timedelta(minutes=1)
    d.tick()
    assert f"orch {b} claimed #1" in msgs
    assert 1 in _cur(d, b).held
    # A ends with #1 parked in its own held
    gh.issues[1] = IssueStatus(number=1, open=True, labels=READY, assignees=("bot",))
    _end(claude, d, a)
    d.tick()
    assert 1 not in gh.removed_assignees
    assert gh.issues[1].assignees == ("bot",)
    assert 1 in _cur(d, b).held
    assert [offered for offered, _ in claude.started] == [[1], [2]]  # no third session


def test_row1_end_guard_alone_when_b_does_not_hold_it(tmp_path: Path):
    """The end guard on its own: B is live but its `held` does not list #1; the assignee was set
    after A let #1 go, so A's end leaves it."""
    _claude, gh, d, _msgs, a, b = _rig(tmp_path)
    gh.issues[1] = IssueStatus(number=1, open=True, labels=READY, assignees=())
    gh._candidates = [_issue(1, 1)]
    d.tick()
    assert 1 in _cur(d, a).let_go
    gh.claim(1)
    gh.assigned_at[1] = datetime.now(timezone.utc) + timedelta(minutes=1)
    assert 1 not in _cur(d, b).held  # no tick since the claim: B's attribution has not run
    d._finish(_cur(d, a), "done")
    assert 1 not in gh.removed_assignees
    assert gh.issues[1].assignees == ("bot",)


def test_row2_without_b_claim_the_end_removes_the_assignee(tmp_path: Path):
    claude, gh, d, _msgs, a, _b = _rig(tmp_path)
    # A parks #1 with a label but leaves the assignee on it; nobody claims it again
    gh.issues[1] = IssueStatus(number=1, open=True, labels=("needs-info",), assignees=("bot",))
    gh._candidates = []
    gh.assigned_at[1] = datetime.now(timezone.utc) - timedelta(hours=1)
    d.tick()
    _end(claude, d, a)
    assert 1 in gh.removed_assignees
    assert gh.issues[1].assignees == ()


def test_end_guard_needs_another_live_session_with_a_stake_in_the_issue(tmp_path: Path):
    """A newer `assigned` event and a live B that has no stake in #1 (an owner's hand assignment):
    A's parked end strips the assignee as today."""
    claude, gh, d, _msgs, a, _b = _rig(tmp_path)
    gh.issues[1] = IssueStatus(number=1, open=True, labels=("needs-info",), assignees=("bot",))
    gh._candidates = []
    gh.assigned_at[1] = datetime.now(timezone.utc) + timedelta(minutes=1)
    d.tick()
    _end(claude, d, a)
    assert 1 in gh.removed_assignees
    assert gh.issues[1].assignees == ()


def test_retaken_claims_reads_events_at_most_once_a_minute(tmp_path: Path):
    claude, gh = FakeClaude(), FakeGh(candidates=[_issue(1, 1)])
    clock = FakeClock()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)
    d.tick()
    a = claude.list_sessions()[0]["id"]
    gh.claim(1)
    d.tick()
    mark_finishing(a, finishing_dir=tmp_path / "finishing")
    gh._candidates = [_issue(2, 2)]
    gh.issues[2] = IssueStatus(number=2, open=True, labels=READY, assignees=())
    d.tick()  # B starts with #1 in `preexisting`
    gh.issues[1] = IssueStatus(number=1, open=True, labels=READY, assignees=())
    gh._candidates = [_issue(1, 1)]
    d.tick()  # A lets #1 go; B is woken about it
    gh.claim(1)
    gh.assigned_at[1] = datetime.now(timezone.utc) - timedelta(days=1)  # an old event: not retaken
    gh.assigned_reads.clear()
    d.tick()
    d.tick()
    assert gh.assigned_reads.count(1) == 1
    clock.advance(61)
    d.tick()
    assert gh.assigned_reads.count(1) == 2
