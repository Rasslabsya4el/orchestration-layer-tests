"""#903: a ticket a live ORCH took over (comment names it, no leading word "Checkpoint") is not stale."""

from __future__ import annotations

from tests.test_dispatch_stale_assignee import _handover

TAKEOVER = "ORCH {sid} takes #12 over from ff8c3a9b (claimed; rows 1-3 open)"


def test_row1_takeover_naming_live_session_not_stale_not_woken(tmp_path):
    gh, d = _handover(tmp_path, TAKEOVER.format(sid="S2"))
    assert 12 not in gh.removed_assignees
    assert gh.issues[12].assignees


def test_row2_takeover_naming_dead_session_still_stale(tmp_path):
    gh, d = _handover(tmp_path, TAKEOVER.format(sid="GONE"))
    assert gh.removed_assignees == [12]
    assert not gh.issues[12].assignees
