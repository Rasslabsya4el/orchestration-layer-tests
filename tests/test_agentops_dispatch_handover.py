"""tools/agentops/dispatch.py (#336): an issue a finishing session let go (parked) and a live session
then claimed is the live session's, never the ended one's: no comment, no unassign, outcome `parked`.
Replays the Other #198 timeline through the fakes `tests/test_agentops_dispatch.py` already uses."""

from __future__ import annotations

from pathlib import Path

from tools.agentops.dispatch import mark_finishing

from tests.test_agentops_dispatch import FakeClaude, FakeGh, _issue, _events
from tests.test_agentops_dispatch_finishing import _dispatcher
from tests._area_rule import _no_area_rule  # noqa: F401


def _one_session(tmp_path: Path):
    """A holds 198."""
    claude, gh = FakeClaude(), FakeGh(candidates=[_issue(198, 1)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh)
    d.tick()
    a = claude.list_sessions()[0]["id"]
    gh.claim(198)
    d.tick()
    return claude, gh, d, a


def test_336_an_issue_a_finishing_session_let_go_and_another_claimed_is_left_to_the_claimer(tmp_path: Path):
    claude, gh, d, a = _one_session(tmp_path)

    gh.park(198)  # A parks it: needs-info, unassigned
    d.tick()
    mark_finishing(a, finishing_dir=tmp_path / "finishing")
    gh._candidates = [_issue(2, 2)]
    d.tick()  # A finishing discovered; B starts
    b = next(iter({s["id"] for s in claude.list_sessions()} - {a}))
    d.tick()  # A's frozen held is seen unclaimed: let go
    a_cur = next(c for c in d.sessions if c.session == a)
    assert a_cur.let_go == {198}

    gh.claim(198)  # the brain put ready-for-agent back, B claims
    d.tick()
    b_cur = next(c for c in d.sessions if c.session == b)
    assert 198 in b_cur.held and 198 not in a_cur.claimed_now

    claude.set_state(a, state="done", status="idle")
    report = d.tick()

    # #514: B holds it, so A's end leaves it alone (resolved at B's own end), let go or not
    assert report.ended == a and 198 not in report.outcomes
    assert gh.comments == [] and gh.removed_assignees == []
    assert gh.issues[198].assignees  # B's claim is intact
    assert 198 in next(c for c in d.sessions if c.session == b).held


def test_336_an_issue_another_live_session_holds_is_skipped_at_the_end_of_the_first(tmp_path: Path):
    claude, gh, d, a = _one_session(tmp_path)

    mark_finishing(a, finishing_dir=tmp_path / "finishing")
    gh._candidates = [_issue(2, 2)]
    d.tick()  # A finishing; B starts
    b_cur = next(c for c in d.sessions if c.session != a)
    b_cur.held.add(198)  # B holds 198 while A's frozen held still has it and it stays assigned

    claude.set_state(a, state="done", status="idle")
    report = d.tick()

    assert report.ended == a and 198 not in report.outcomes
    assert gh.comments == [] and gh.removed_assignees == []
    assert gh.issues[198].assignees
