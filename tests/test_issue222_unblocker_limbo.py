"""#222: an unblocker that wrote no way within 30 minutes (the limbo case) tells the area's brain chat ONCE, and then
the record is done: no further comment reads, no second line, no second unblocker start.

Through `Dispatcher` with the #209 fakes and `World` (`tests/test_issue209_unblocker_line.py`): the notifier is the real
one with its real store; no real `claude`, no real `gh`.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from tests._area_rule import _no_area_rule  # noqa: F401
from tests.test_issue209_unblocker_line import CHAT, SB, T0, World, _c, chats  # noqa: F401

LIMBO_LINE = "#195: the unblocker wrote no way in 30 min; the ticket stays stopped until a brain-answer"
LIMBO_LOG = "unblocker for #195 ended without a brain-answer"


def _limbo_logs(w: World) -> list[str]:
    return [x for x in w.d.log_lines if "ended without a brain-answer" in x]


def _reads(w: World) -> int:
    return w.gh.reads.count(195)


def test_row1_a_start_31_minutes_old_with_no_answer_tells_the_chat_one_line_and_marks_the_record(tmp_path: Path):
    w = World(tmp_path)
    w.start_unblocker_on()
    for _ in range(31):
        w.tick(1)

    assert w.told() == [(CHAT, LIMBO_LINE)]
    assert _limbo_logs(w) == [LIMBO_LOG]
    record = w.d.unblocker_starts.get(195)
    assert record is not None and record["limbo"] == "1"
    assert record["told"] == "limbo:" + record["at"]


def test_row2_ten_more_passes_over_two_hours_read_nothing_tell_nothing_and_start_nothing(tmp_path: Path):
    w = World(tmp_path)
    w.start_unblocker_on()
    for _ in range(31):
        w.tick(1)
    reads, told, starts = _reads(w), len(w.told()), len(w.claude.unblockers)

    for _ in range(10):
        w.tick(12)  # ten passes, 12 minutes apart: two hours of fake clock

    assert _reads(w) == reads
    assert len(w.told()) == told
    assert len(w.claude.unblockers) == starts
    assert len(_limbo_logs(w)) == 1


def test_a_newer_failed_attempt_after_the_limbo_line_is_a_new_trigger_with_a_fresh_record(tmp_path: Path):
    w = World(tmp_path)
    w.start_unblocker_on()
    for _ in range(31):
        w.tick(1)
    first = w.d.unblocker_starts.get(195)
    assert first["told"].startswith("limbo:")

    # three more failed attempts on the ticket, newer than the first start: a new trigger
    w.gh.threads[195] += [_c(f"{SB}\nagain", 13), _c(f"{SB}\nagain", 13, 5), _c(f"{SB}\nagain", 13, 10)]
    w.gh.ready = {195}
    w.now = T0 + timedelta(hours=1, minutes=15)
    w.tick(0)

    assert len(w.claude.unblockers) == 2
    record = w.d.unblocker_starts.get(195)
    assert record["at"] == w.now.isoformat(timespec="seconds") and record["at"] != first["at"]
    assert "limbo" not in record and "told" not in record


def test_row3_a_restarted_dispatcher_over_the_same_stores_tells_nothing_more(tmp_path: Path):
    w = World(tmp_path)
    w.start_unblocker_on()
    for _ in range(31):
        w.tick(1)
    reads, told, starts = _reads(w), len(w.told()), len(w.claude.unblockers)

    w.restart()
    for _ in range(3):
        w.tick(1)

    assert _reads(w) == reads
    assert len(w.told()) == told == 1
    assert len(w.claude.unblockers) == starts
    assert _limbo_logs(w) == []


def test_an_unconfirmed_line_stays_untold_and_is_retried_by_the_next_pass_with_no_read(tmp_path: Path):
    w = World(tmp_path)
    w.start_unblocker_on()
    w.d._brain_notifier.knows = lambda number, key: False  # the notifier did not confirm the line
    for _ in range(31):
        w.tick(1)

    record = w.d.unblocker_starts.get(195)
    assert record is not None and record["limbo"] == "1" and "told" not in record
    reads = _reads(w)

    del w.d._brain_notifier.knows  # back to the real notifier
    w.tick(1)

    assert _reads(w) == reads
    assert w.d.unblocker_starts.get(195)["told"] == "limbo:" + record["at"]
    assert w.told() == [(CHAT, LIMBO_LINE)]
