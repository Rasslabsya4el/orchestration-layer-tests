"""#118 unit 1, replaced by #209 unit 5: a loop-stopped ticket is never offered to ORCH to write its own Loop stop;
the dispatcher starts an unblocker on it instead (`tests/test_issue209_unblocker_start.py`). Fakes only."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from tests.test_agentops_dispatch import FakeClaude, FakeGh, _dispatcher
from tests._area_rule import _no_area_rule  # noqa: F401
from tools.agentops.dispatch import IssueSummary, READY_LABEL

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
SB, OA = "<!-- send-back -->", "<!-- owner-answer -->"


def _c(marker: str | None, hour: int, text: str = "body") -> dict:
    return {"createdAt": f"2026-10-12T{hour:02d}:00:00Z", "body": (marker + "\n" if marker else "") + text}


def _issue(number: int, comments: list[dict]) -> IssueSummary:
    return IssueSummary(number=number, created_at=f"2026-10-01T00:00:0{number % 10}Z", labels=(READY_LABEL,),
                        comments=tuple((c["createdAt"], c["body"]) for c in comments))


def _run(tmp_path: Path, by_issue: dict[int, list[dict]], ticks: int = 1):
    claude = FakeClaude()
    d = _dispatcher(tmp_path, claude=claude, gh=FakeGh(candidates=[_issue(n, c) for n, c in by_issue.items()]))
    d.timestamp = lambda: NOW
    msgs: list[str] = []
    d.log = msgs.append
    for _ in range(ticks):
        d.tick()
    return claude, msgs


def test_row1_stopped_ticket_without_loop_stop_is_not_offered_an_unblocker_starts_once(tmp_path: Path):
    claude, msgs = _run(tmp_path, {5: [_c(SB, 6), _c(SB, 7), _c(SB, 8)]}, ticks=3)  # 3 in a row: stopped
    assert claude.started == []
    assert [n for n, _ in claude.unblockers] == [5]
    assert not [m for m in msgs if m.startswith("offered for Loop stop")]
    assert [m for m in msgs if m.startswith("started unblocker")] == ["started unblocker for #5 (3 failed attempts)"]


def test_row1b_stopped_ticket_is_left_out_of_the_start_of_an_ordinary_ready_ticket(tmp_path: Path):
    claude, _ = _run(tmp_path, {5: [_c(SB, 6), _c(SB, 7), _c(SB, 8)], 6: []})
    assert [offered for offered, _ in claude.started] == [[6]]
    assert "loop-stopped" not in " ".join(claude.started[0][1])
    assert [n for n, _ in claude.unblockers] == [5]


def test_row2_stopped_ticket_with_its_loop_stop_comment_is_not_offered_and_not_started_twice(tmp_path: Path):
    claude, msgs = _run(tmp_path, {5: [_c(SB, 6), _c(SB, 7), _c(SB, 8),
                                       _c(None, 9, "Loop stop (ORCH 3302c1a1, #5). three failed attempts.")]},
                        ticks=2)
    assert claude.started == []
    assert [m for m in msgs if "loop-stopped" in m] == ["skipped as loop-stopped: #5 (3 failed attempts)"]
    assert len(claude.unblockers) == 1


def test_row3_owner_answer_after_loop_stop_and_send_backs_offers_an_ordinary_ticket(tmp_path: Path):
    comments = [_c(SB, 7), _c(None, 8, "Loop stop (ORCH 3302c1a1, #5). one send-back."), _c(OA, 9)]
    claude, msgs = _run(tmp_path, {5: comments})
    assert [offered for offered, _ in claude.started] == [[5]]
    assert claude.unblockers == []
    assert not [m for m in msgs if "Loop stop" in m]


def test_wake_never_names_a_stopped_ticket(tmp_path: Path):
    claude = FakeClaude()
    gh = FakeGh(candidates=[_issue(1, [])])
    d = _dispatcher(tmp_path, claude=claude, gh=gh)
    d.timestamp = lambda: NOW
    d.log = lambda _line: None
    d.tick()  # starts a session offered #1
    gh.claim(1)
    d.tick()
    gh._candidates = [_issue(5, [_c(SB, 6), _c(SB, 7), _c(SB, 8)])]  # appears while the session sleeps
    d.tick()
    assert claude.waked == []
    assert [n for n, _ in claude.unblockers] == [5]
