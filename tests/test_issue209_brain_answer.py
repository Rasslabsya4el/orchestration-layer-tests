"""#209 unit 1: a `<!-- brain-answer -->` comment resets the loop-stop counter exactly like `<!-- owner-answer -->`.
Pure functions over comment dicts; no network."""

from __future__ import annotations

from datetime import datetime, timezone

from tools.agentops.loop_stop import has_stop_comment, loop_status
from tools.agentops.loop_stop_cli import replay

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
SB, OA, BA = "<!-- send-back -->", "<!-- owner-answer -->", "<!-- brain-answer -->"


def _c(marker: str | None, hour: int, minute: int = 0, text: str = "body") -> dict:
    body = (marker + "\n" if marker else "") + (text or "body")
    return {"createdAt": f"2026-10-12T{hour:02d}:{minute:02d}:00Z", "body": body}


def _stop(hour: int, minute: int = 0) -> dict:
    return _c(None, hour, minute, "Loop stop (ORCH 3302c1a1, #209). one send-back.")


# (a) and (b): the same count whichever answer marker is used

def test_a_send_backs_before_a_brain_answer_are_not_counted():
    comments = [_c(SB, 6), _c(SB, 7), _c(BA, 8), _c(SB, 10)]
    st = loop_status(comments, NOW)
    assert (st.count, st.first_at) == (1, "2026-10-12T10:00:00Z")


def test_b_same_count_with_owner_answer():
    with_ba = [_c(SB, 6), _c(SB, 7), _c(BA, 8), _c(SB, 10)]
    with_oa = [_c(SB, 6), _c(SB, 7), _c(OA, 8), _c(SB, 10)]
    assert loop_status(with_ba, NOW).count == loop_status(with_oa, NOW).count == 1


# (c): has_stop_comment reads a Loop stop only after the newest answer

def test_c_loop_stop_before_brain_answer_is_not_a_stop():
    assert has_stop_comment([_stop(7), _c(BA, 8, text="go on: the merge is red for an outside reason")]) is False


def test_c_loop_stop_after_brain_answer_is_a_stop():
    assert has_stop_comment([_c(BA, 7, text="go on"), _stop(8)]) is True


# (d): the newest of an owner-answer and a brain-answer wins, in either order

def test_d_brain_answer_newest_resets_past_an_older_owner_answer():
    comments = [_c(SB, 6), _c(OA, 7), _c(SB, 8), _c(BA, 9), _c(SB, 10)]
    assert loop_status(comments, NOW).count == 1


def test_d_owner_answer_newest_resets_past_an_older_brain_answer():
    comments = [_c(SB, 6), _c(BA, 7), _c(SB, 8), _c(OA, 9), _c(SB, 10)]
    assert loop_status(comments, NOW).count == 1


def test_d_stop_is_read_after_the_newest_of_both_answers():
    assert has_stop_comment([_stop(6), _c(OA, 7), _c(BA, 8)]) is False
    assert has_stop_comment([_c(BA, 6), _stop(7), _c(OA, 8)]) is False
    assert has_stop_comment([_c(OA, 6), _c(BA, 7), _stop(8)]) is True


# (e): a brain-answer not on the first line is ignored

def test_e_brain_answer_on_second_line_is_not_an_answer():
    comments = [_c(SB, 6), _c(SB, 7), _c(None, 8, text=f"Brain decided.\n{BA}"), _c(SB, 9)]
    assert loop_status(comments, NOW).count == 3
    assert loop_status(comments, NOW).by_count is True


def test_e_brain_answer_on_second_line_does_not_clear_a_stop():
    assert has_stop_comment([_stop(6), _c(None, 7, text=f"Brain decided.\n{BA}")]) is True


# the replay of the CLI reads brain answers too (its ids, as --brain-ids)

def test_replay_brain_answer_resets_the_count():
    comments = [{"id": i, "createdAt": s, "body": "x"} for i, s in
                [(1, "2026-10-11T12:00:00Z"), (2, "2026-10-11T13:00:00Z"),
                 (3, "2026-10-11T14:00:00Z"), (4, "2026-10-11T15:00:00Z")]]
    res = replay(comments, {1, 2, 4}, set(), {3})
    assert [r["count"] for r in res["rows"]] == [1, 2, 0, 1]
    assert [r["kind"] for r in res["rows"]] == ["send-back", "send-back", "brain-answer", "send-back"]
    assert res["count_fire"] is None
