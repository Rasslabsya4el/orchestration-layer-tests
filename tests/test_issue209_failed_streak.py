"""#209 unit 4: the loop-stop counter counts FAILED ATTEMPTS in a row since the newest answer.

An attempt is a `<!-- send-back -->` or a `<!-- merge result -->` that did not land ("Merge of <branch> did not land
(...)"). A landed merge result (`landed on main`) or a `<!-- accepted -->` resets the streak to 0. Three in a row stop
the ticket; the 4 h clause is gone. Pure functions over comment dicts; no network.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from tools.agentops.loop_stop import has_stop_comment, loop_status
from tools.agentops.loop_stop_cli import render_replay, replay, run_loop_stop_command

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
SB, OA, BA, AC, MR = "<!-- send-back -->", "<!-- owner-answer -->", "<!-- brain-answer -->", "<!-- accepted -->", "<!-- merge result -->"
RED = f"{MR}\nMerge of orch-195 did not land (red-suite) after 40 min in line and a suite of 36 min, 1 failed."
CONFLICT = f"{MR}\nMerge of orch-195 did not land (conflict) after 33 min in line and a suite of -, - failed."
LANDED = f"{MR}\nMerge of orch-195 landed on main d0b7d25c6afc after 77 min in line and a suite of 47 min, 0 failed."


def _c(body: str, hour: int, minute: int = 0) -> dict:
    return {"createdAt": f"2026-10-12T{hour:02d}:{minute:02d}:00Z", "body": body}


def _m(marker: str, hour: int, minute: int = 0, text: str = "body") -> dict:
    return _c(f"{marker}\n{text}", hour, minute)


STOP = _c("Loop stop (ORCH 3302c1a1, #209). three failed attempts.", 11, 45)


# (a) three send-backs in a row -> stopped

def test_a_three_send_backs_in_a_row_stop_the_ticket():
    st = loop_status([_m(SB, 6), _m(SB, 7), _m(SB, 8)], NOW)
    assert (st.count, st.by_count, st.stopped) == (3, True, True)


# (b) failed merges count like send-backs, in any order

def test_b_two_send_backs_and_a_red_merge_stop_the_ticket_in_every_order():
    red = _c(RED, 11, 30)  # all within the hour before NOW: only the count can stop them
    orders = [
        [_m(SB, 10), _m(SB, 11), red],
        [_m(SB, 10), red, _m(SB, 11)],
        [red, _m(SB, 10), _m(SB, 11)],
    ]
    for comments in orders:
        assert loop_status(comments, NOW).stopped is True, comments


def test_b_three_red_merges_stop_the_ticket():
    assert loop_status([_c(RED, 6), _c(CONFLICT, 7), _c(RED, 8)], NOW).stopped is True


# (c) two in a row -> not stopped

def test_c_two_failed_attempts_in_a_row_do_not_stop_the_ticket():
    assert loop_status([_m(SB, 6), _m(SB, 7)], NOW).count == 2
    assert loop_status([_m(SB, 6), _c(RED, 7)], NOW).stopped is False


# (d) a landed merge result or an accepted comment between resets the streak

def test_d_three_failures_with_a_landed_merge_between_do_not_stop():
    comments = [_m(SB, 6), _m(SB, 7), _c(LANDED, 8), _m(SB, 9)]
    st = loop_status(comments, NOW)
    assert (st.count, st.stopped) == (1, False)


def test_d_three_failures_with_accepted_between_do_not_stop():
    comments = [_m(SB, 6), _m(SB, 7), _m(AC, 8), _m(SB, 9)]
    st = loop_status(comments, NOW)
    assert (st.count, st.stopped) == (1, False)


def test_d_a_landed_merge_as_the_last_comment_leaves_no_streak():
    assert loop_status([_m(SB, 6), _m(SB, 7), _c(LANDED, 8)], NOW).count == 0


def test_d_failures_after_a_landed_merge_count_from_zero():
    comments = [_c(RED, 6), _c(LANDED, 7), _c(RED, 8), _m(SB, 9), _c(CONFLICT, 10)]
    st = loop_status(comments, NOW)
    assert (st.count, st.first_at, st.stopped) == (3, "2026-10-12T08:00:00Z", True)


# (e) the 4 h clause is gone: one old send-back does not stop a ticket

def test_e_one_send_back_five_hours_old_does_not_stop_the_ticket():
    st = loop_status([_m(SB, 7)], NOW)  # 5 h before NOW, nothing accepted after it
    assert (st.count, st.stopped) == (1, False)


# (f) failures before an answer are not counted

def test_f_send_backs_before_a_brain_answer_are_not_counted():
    assert loop_status([_m(SB, 6), _m(SB, 7), _m(SB, 8), _m(BA, 9), _m(SB, 10)], NOW).count == 1


def test_f_failed_merges_before_a_brain_answer_are_not_counted():
    comments = [_c(RED, 6), _c(CONFLICT, 7), _m(BA, 8), _m(SB, 9)]
    assert loop_status(comments, NOW).count == 1


def test_f_failed_merges_before_an_owner_answer_are_not_counted():
    comments = [_c(RED, 6), _c(CONFLICT, 7), _m(OA, 8), _m(SB, 9)]
    assert loop_status(comments, NOW).count == 1


def test_f_has_stop_comment_is_false_after_a_brain_answer_and_true_after_a_landed_merge():
    assert has_stop_comment([STOP, _m(BA, 12)]) is False
    assert has_stop_comment([STOP, _c(LANDED, 12)]) is True  # a landed merge is not an answer


# (g) replay of #195: a send-back (T), a Loop stop comment, then red merge results

def test_g_replay_of_195_prints_the_count_and_fires_at_the_third_attempt():
    comments = [
        {"id": 1, "createdAt": "2026-10-18T18:56:52Z", "body": f"{SB}\nSend-back 1"},
        {"id": 2, "createdAt": "2026-10-18T23:15:41Z", "body": STOP["body"]},
        {"id": 3, "createdAt": "2026-10-18T23:37:50Z", "body": RED},
        {"id": 4, "createdAt": "2026-10-19T06:20:00Z", "body": RED},
        {"id": 5, "createdAt": "2026-10-19T06:45:00Z", "body": RED},
    ]
    res = replay(comments, {1}, set(), set(), merge_ids={3, 4, 5})
    assert [r["count"] for r in res["rows"]] == [1, 2, 3, 4]
    assert res["count_fire"]["id"] == 4  # send-back, red, red: the third failed attempt in a row
    text = render_replay(res)
    assert "stop by count fires at row 3 (comment 4" in text
    assert "by 4 h" not in text and "4 h clause" not in text


def test_g_command_replay_takes_merge_ids(capsys):
    comments = [
        {"id": 1, "createdAt": "2026-10-18T18:56:52Z", "body": "x"},
        {"id": 3, "createdAt": "2026-10-18T23:37:50Z", "body": "x"},
        {"id": 4, "createdAt": "2026-10-19T06:20:00Z", "body": "x"},
    ]
    args = SimpleNamespace(replay=195, ids="1", owner_ids="", brain_ids="", merge_ids="3,4",
                           landed_ids="", false_stops=False, days=14)
    assert run_loop_stop_command(args, fetch=lambda n: comments) == 0
    assert "stop by count fires at row 3 (comment 4" in capsys.readouterr().out


# an outside event (lock timeout, setup error, silent holder) is not a failed attempt of the ticket's work

def test_outside_event_merge_results_count_for_nothing():
    outside = [_c(f"{MR}\nMerge of orch-195 did not land ({r}) after 5 min in line and a suite of 0 min, - failed.", 8 + i)
               for i, r in enumerate(("lock-timeout", "setup-error", "silent-holder"))]
    st = loop_status([_m(SB, 6), _m(SB, 7), *outside], NOW)
    assert (st.count, st.stopped) == (2, False)
    red = _c(f"{MR}\nMerge of orch-195 did not land (red-suite) after 5 min in line and a suite of 20 min, 1 failed.", 12)
    assert loop_status([_m(SB, 6), _m(SB, 7), *outside, red], NOW).stopped is True
