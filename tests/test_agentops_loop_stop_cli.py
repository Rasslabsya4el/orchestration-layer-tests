"""#924 unit 2: replay and false-stop lists, no network."""

from datetime import datetime, timezone
from types import SimpleNamespace

from tools.agentops.loop_stop_cli import false_stops, replay, run_loop_stop_command


def _c(i, stamp):
    return {"id": i, "createdAt": stamp, "body": "x"}


COMMENTS = [
    _c(1, "2026-10-11T10:00:00Z"),
    _c(2, "2026-10-11T12:00:00Z"),   # send-back 1
    _c(3, "2026-10-11T13:00:00Z"),   # send-back 2
    _c(4, "2026-10-11T13:30:00Z"),   # chatter
    _c(5, "2026-10-11T14:00:00Z"),   # send-back 3 -> stop by count
    _c(6, "2026-10-11T20:00:00Z"),   # send-back 4
]


def test_replay_fires_at_third():
    res = replay(COMMENTS, {2, 3, 5, 6}, set())
    assert [r["count"] for r in res["rows"]] == [1, 2, 3, 4]
    assert res["count_fire"]["id"] == 5
    assert res["final_count"] == 4


def test_replay_two_far_apart_send_backs_do_not_stop():
    # #209: no 4 h clause; 12:00 and 20:00 are two failed attempts in a row, not a stop
    res = replay(COMMENTS, {2, 6}, set())
    assert res["count_fire"] is None
    assert [r["count"] for r in res["rows"]] == [1, 2]


def test_replay_owner_answer_resets():
    res = replay(COMMENTS, {2, 3, 6}, {5})
    assert [r["count"] for r in res["rows"]] == [1, 2, 0, 1]
    assert res["count_fire"] is None


def test_false_stops_markers_and_proxy():
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    sb = {"body": "<!-- send-back -->", "createdAt": "2026-10-12T00:00:00Z"}
    comments = {10: [sb, sb, sb], 11: [sb], 12: []}
    closed = [{"number": 10}, {"number": 11}, {"number": 12}, {"number": 13}]
    outcomes = ([{"issue": 11, "class": "redone"}] * 2 + [{"issue": 11, "class": "wrong-fact"}]
                + [{"issue": 12, "class": "redone"}] * 2 + [{"issue": 12, "class": "accepted"}] * 5
                + [{"issue": 99, "class": "redone"}] * 4)
    res = false_stops(closed, lambda n: comments.get(n, []), outcomes, now)
    assert res == {"closed": 4, "marker": [10], "proxy": [11]}


def test_command_replay_prints(capsys):
    args = SimpleNamespace(replay=1, ids="2,3,5", owner_ids="", false_stops=False, days=14)
    assert run_loop_stop_command(args, fetch=lambda n: COMMENTS) == 0
    assert "stop by count fires at row 3 (comment 5" in capsys.readouterr().out
