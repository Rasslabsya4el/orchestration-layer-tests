"""#924 unit 1: the loop-stop counter and the dispatcher's offered-candidate filter. Fakes only."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from tests.test_agentops_dispatch import FakeClaude, FakeGh, _dispatcher
from tools.agentops.dispatch import IssueSummary, READY_LABEL, parse_candidates
from tools.agentops.loop_stop import loop_status
from tests._area_rule import _no_area_rule  # noqa: F401

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)


def _c(marker: str | None, hour: int, minute: int = 0, text: str = "") -> dict:
    """A comment at 2026-10-12 `hour:minute` UTC whose first line is `marker`."""
    body = (marker + "\n" if marker else "") + (text or "body")
    return {"createdAt": f"2026-10-12T{hour:02d}:{minute:02d}:00Z", "body": body}


SB, OA, AC = "<!-- send-back -->", "<!-- owner-answer -->", "<!-- accepted -->"


def _stop(*comments: dict) -> list[dict]:
    """#118: the comments plus the `Loop stop` comment whoever held the ticket writes; a stopped ticket
    without one is offered (to write it), so these tests, about the stop itself, give every stopped ticket its own."""
    return [*comments, _c(None, 11, 45, "Loop stop (ORCH 3302c1a1, #924). stopped.")]


def _issue(number: int, comments: list[dict]) -> IssueSummary:
    return IssueSummary(number=number, created_at="2026-10-01T00:00:00Z", labels=(READY_LABEL,),
                        comments=tuple((c["createdAt"], c["body"]) for c in comments))


def _offered(tmp_path: Path, comments_by_issue: dict[int, list[dict]], messages: list[str] | None = None,
             ticks: int = 1) -> list[list[int]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    claude = FakeClaude()
    gh = FakeGh(candidates=[_issue(n, c) for n, c in comments_by_issue.items()])
    d = _dispatcher(tmp_path, claude=claude, gh=gh)
    d.timestamp = lambda: NOW
    if messages is not None:
        d.log = messages.append
    for _ in range(ticks):
        d.tick()
    return [offered for offered, _ in claude.started]


# -- the counter ------------------------------------------------------------------------------

def test_counter_counts_send_backs_since_the_newest_owner_answer():
    comments = [_c(SB, 6), _c(SB, 7), _c(OA, 8), _c(SB, 10), _c(None, 11)]
    st = loop_status(comments, NOW)
    assert (st.count, st.first_at, st.stopped) == (1, "2026-10-12T10:00:00Z", False)


def test_counter_marker_is_the_first_line_only_after_leading_whitespace():
    assert loop_status([_c(None, 1, text=f"text\n{SB}")], NOW).count == 0
    assert loop_status([{"createdAt": "2026-10-12T01:00:00Z", "body": f"\n  {SB}\nrest"}], NOW).count == 1


def test_counter_no_four_hour_clause():
    # #209: one send-back is never a stop by its age; the 4 h clause is gone
    assert loop_status([_c(SB, 7)], NOW).stopped is False  # 5 h old, nothing after it
    assert loop_status([_c(AC, 6), _c(SB, 7)], NOW).count == 1  # accepted BEFORE it leaves the count at 1


# -- the dispatcher filter --------------------------------------------------------------------

def test_a_three_send_backs_offered_0_then_after_owner_answer_offered_1(tmp_path: Path):
    three = [_c(SB, 9), _c(SB, 10), _c(SB, 11)]
    assert _offered(tmp_path / "a1", {924: _stop(*three)}) == []
    assert _offered(tmp_path / "a2", {924: three + [_c(OA, 11, 30)]}) == [[924]]


def test_b_two_send_backs_offered_1(tmp_path: Path):
    assert _offered(tmp_path, {924: [_c(SB, 10), _c(SB, 11)]}) == [[924]]


def test_c_three_send_backs_straddling_an_owner_answer_offered_1(tmp_path: Path):
    assert _offered(tmp_path, {924: [_c(SB, 9), _c(SB, 10), _c(OA, 10, 30), _c(SB, 11)]}) == [[924]]


def test_d_one_old_send_back_is_offered_three_in_a_row_are_not(tmp_path: Path):
    # #209: the 4 h clause is gone; a single send-back, however old, is offered
    assert _offered(tmp_path / "d1", {1: [_c(SB, 7)]}) == [[1]]  # 5 h old, no accepted
    assert _offered(tmp_path / "d2", {1: [_c(SB, 7), _c(AC, 8)]}) == [[1]]
    assert _offered(tmp_path / "d3", {1: [_c(SB, 9)]}) == [[1]]
    assert _offered(tmp_path / "d4", {1: _stop(_c(SB, 7), _c(SB, 7, 30), _c(SB, 8))}) == []  # 3 in a row


def test_e_log_line_text_once_per_changed_set(tmp_path: Path):
    msgs: list[str] = []
    assert _offered(tmp_path / "e1", {924: _stop(_c(SB, 9), _c(SB, 10), _c(SB, 11)),
                                      5: _stop(_c(SB, 7), _c(SB, 7, 10), _c(SB, 7, 20))},
                    msgs, ticks=3) == []
    lines = [m for m in msgs if "loop-stopped" in m]
    assert lines == ["skipped as loop-stopped: #924 (3 failed attempts), #5 (3 failed attempts)"]


def test_e_log_line_single_ticket_exact_text(tmp_path: Path):
    msgs: list[str] = []
    _offered(tmp_path, {924: _stop(_c(SB, 9), _c(SB, 10), _c(SB, 11))}, msgs, ticks=2)
    assert [m for m in msgs if "loop-stopped" in m] == ["skipped as loop-stopped: #924 (3 failed attempts)"]


def test_parse_candidates_carries_every_comment():
    data = [{"number": 7, "createdAt": "2026-10-01T00:00:00Z", "updatedAt": "", "blockedBy": {"nodes": []},
             "labels": [], "comments": [{"createdAt": "2026-10-12T01:00:00Z", "body": "a"},
                                        {"createdAt": "2026-10-12T02:00:00Z", "body": "b"}]}]
    (c,) = parse_candidates(data)
    assert c.comments == (("2026-10-12T01:00:00Z", "a"), ("2026-10-12T02:00:00Z", "b"))
    assert c.latest_comment == "b"
