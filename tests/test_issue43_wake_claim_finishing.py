"""#43: a ticket woken to a live ORCH is never handed to a second ORCH.

The replay of 2026-10-16 13:45-13:49: session A is woken about #35, #35 is assigned (A took it), A says it is
finishing before the claims are read, a newer session B starts offered #13 (its start-time claim read lags and
lacks #35), then the claims are read. #35 must stay with A and B must never be woken about it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, READY, _dispatcher, _issue
from tools.agentops.dispatch import IssueStatus
from tests._area_rule import _no_area_rule  # noqa: F401


class ListingGh(FakeGh):
    """Candidates are the ready issues without an assignee, as the real `--search no:assignee`."""

    def __init__(self) -> None:
        super().__init__()
        self.lag = False  # the search read at a session's start lags: it lacks the `lagging` issues
        self.lagging: set[int] = set()

    def ready_for_agent_issues(self):
        return [_issue(n) for n, s in sorted(self.issues.items())
                if s.open and "ready-for-agent" in s.labels and not s.assignees]

    def claimed_issues(self):
        claims = super().claimed_issues()
        if claims is not None and self.lag:
            return [n for n in claims if n not in self.lagging]
        return claims


def _setup(tmp_path: Path):
    claude, gh = FakeClaude(), ListingGh()
    msgs: list[str] = []
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=FakeClock(100000.0))
    d.log = msgs.append
    return claude, gh, d, msgs


def _ready(gh: FakeGh, n: int) -> None:
    gh.issues[n] = IssueStatus(number=n, open=True, labels=READY, assignees=())


def _lag_at_start(d, gh: ListingGh, *issues: int) -> None:
    """The next session start reads its `preexisting` claims through a lagging search."""
    original = d._maybe_start
    gh.lagging = set(issues)

    def wrapped(report):
        gh.lag = True
        try:
            original(report)
        finally:
            gh.lag = False

    d._maybe_start = wrapped


def _woken_session(claude, gh, d):
    """Session A started offered #1 (claimed), then woken about #35."""
    _ready(gh, 1)
    d.tick()
    a = d.current
    gh.claim(1)
    d.clock.advance(60)
    _ready(gh, 35)
    d.tick()
    assert any("#35" in message for _, message in claude.waked)
    return a


def test_row1_claim_after_wake_stays_with_the_woken_finishing_session(tmp_path: Path):
    claude, gh, d, msgs = _setup(tmp_path)
    a = _woken_session(claude, gh, d)
    d.clock.advance(43)
    gh.claim(35)
    gh.assigned_at[35] = datetime.now(timezone.utc) - timedelta(seconds=60)  # before B starts
    a.finishing = True  # it said so before the next claims read
    _ready(gh, 13)
    _lag_at_start(d, gh, 35)
    claude.waked.clear()
    d.tick()  # A's claims are read, then B starts offered #13
    assert len(claude.started) == 2 and claude.started[1][0] == [13]
    d.clock.advance(47)
    d.tick()
    d.tick()

    assert "orch %s claimed #35" % a.session in msgs
    assert not [m for m in msgs if "stale assignee" in m]
    assert 35 in a.held and 35 in a.claimed_now
    assert 35 not in d.sessions[1].held
    assert gh.removed_assignees == []
    assert not [1 for _, message in claude.waked if "#35" in message]


def test_row2_empty_event_list_is_not_known_yet_then_attributed(tmp_path: Path):
    claude, gh, d, msgs = _setup(tmp_path)
    _ready(gh, 1)
    d.tick()  # a session starts, offered #1
    cur = d.current
    d.clock.advance(60)
    gh.claim(20)  # taken by the session without an offer
    gh.assigned_at[20] = None  # the event list is still empty at the first tick's reads
    gh.comment_at[20] = datetime.now(timezone.utc)  # a young comment: the #650 sweep waits, only the #378 check is on
    d.tick()
    assert 20 not in cur.held and gh.removed_assignees == []  # not known yet, retried next tick
    gh.assigned_at[20] = datetime.now(timezone.utc) + timedelta(seconds=30)
    d.tick()
    assert 20 in cur.held and gh.removed_assignees == []
    assert not [m for m in msgs if "stale assignee" in m]


def test_row3_assigned_by_an_ended_session_before_the_start_is_unassigned(tmp_path: Path):
    claude, gh, d, msgs = _setup(tmp_path)
    _ready(gh, 1)
    d.tick()
    cur = d.current
    d.clock.advance(1800)
    gh.claim(9)
    gh.assigned_at[9] = datetime.now(timezone.utc) - timedelta(hours=1)  # older than the session
    d.tick()
    assert gh.removed_assignees == [9]
    assert "orch-dispatch: #9 stale assignee (no claim since %s started), unassigned" % cur.session in msgs
    assert 9 not in cur.held
