"""#999: the dispatcher's end reading of an ORCH session that closed an issue it was offered without
ever claiming it (`gh issue edit --add-assignee` skipped; f687cd9d closed #986 that way). The end line
names it `closed #N without a claim`, never `no issue claimed`, and the closed issue is not re-offered.
A session offered an issue it touched not at all still ends `no issue claimed`."""

from __future__ import annotations

from pathlib import Path

from tools.agentops.dispatch import IssueStatus
from tests.test_agentops_dispatch import FakeClaude, FakeGh, _dispatcher, _issue
from tests._area_rule import _no_area_rule  # noqa: F401

READY = ("ready-for-agent",)


def _offered_open(tmp_path: Path, number: int):
    """One ORCH session offered `number`, which is open, `ready-for-agent` and unassigned at the offer."""
    messages: list[str] = []
    claude = FakeClaude()
    gh = FakeGh(candidates=[_issue(number)])
    gh.issues[number] = IssueStatus(number=number, open=True, labels=READY, assignees=())
    d = _dispatcher(tmp_path, claude=claude, gh=gh)
    d.log = messages.append
    d.tick()
    live_id = claude.list_sessions()[0]["id"]
    return claude, gh, d, live_id, messages


def test_a_session_that_closes_its_offered_issue_without_a_claim_is_named_so(tmp_path: Path):
    claude, gh, d, live_id, messages = _offered_open(tmp_path, 986)
    # the f687cd9d shape: no assignee ever set, then ORCH's own `Closing (ORCH, #986)` comment and a close
    gh.comment(986, "Closing (ORCH, #986): done, merged.")
    gh.close(986)
    gh._candidates = []  # GitHub's open list no longer shows #986
    claude.set_state(live_id, state="done", status="idle")

    report = d.tick()

    assert report.ended == live_id and report.outcomes == {}
    end_lines = [m for m in messages if m.startswith(f"orch {live_id} ended (done):")]
    assert len(end_lines) == 1
    assert "closed #986 without a claim" in end_lines[0]
    assert "no issue claimed" not in end_lines[0]
    d.tick()  # the next tick: nothing open to offer, #986 is not offered again
    assert [offered for offered, _ in claude.started] == [[986]]


def test_a_session_that_touches_nothing_of_its_offer_still_ends_no_issue_claimed(tmp_path: Path):
    claude, gh, d, live_id, messages = _offered_open(tmp_path, 987)
    claude.set_state(live_id, state="done", status="idle")

    report = d.tick()

    assert report.ended == live_id and report.outcomes == {}
    assert f"orch {live_id} ended (done): no issue claimed" in messages
