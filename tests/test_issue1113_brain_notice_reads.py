"""#1113: a pending brain notice is re-read from GitHub at most once per PENDING_REREAD_SECONDS, not once per tick.

Counting fake issue lookup and an injected clock only: no gh, no real send, no heavy tool.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from tools.agentops.brain_notice import BrainNotifier

BRAIN = "Brain"
T0 = datetime(2026, 10, 15, 9, 0, 0, tzinfo=timezone.utc)
PENDING = 36
PENDING_REREAD_SECONDS = 1800  # the spec's 30 minutes, written here so the test does not import the fix


class Clock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t = self.t + timedelta(seconds=seconds)


class CountingReader:
    """The issue lookup seam: every call is recorded, the answer is a parked issue with its checkpoint id."""

    def __init__(self) -> None:
        self.reads: list[int] = []

    def __call__(self, number: int):
        self.reads.append(number)
        return (f"Issue {number}", f"<!-- parked-on-run: p{number} -->", f"p{number}", f"c{number}")


def _seed(path) -> None:
    """36 pending records in the shape already on disk (state, key, not done, away, stamped at T0, no target)."""
    stamp = T0.isoformat(timespec="seconds")
    records = {str(n): {"state": "parked", "key": f"p{n}", "attempts": 0, "done": False, "via": "away", "at": stamp}
               for n in range(1, PENDING + 1)}
    path.write_text(json.dumps(records), encoding="utf-8")


def _notifier(path, clock: Clock, reader: CountingReader) -> BrainNotifier:
    return BrainNotifier(
        state_path=path,
        brain_title=BRAIN,
        send=lambda title, message: None,
        issue_info=reader,
        autopilot_live_for=lambda n: None,
        brain_live=lambda: False,
        log=lambda text: None,
        now=clock,
    )


def test_pending_records_are_read_once_then_not_on_each_tick_inside_the_window(tmp_path):
    path = tmp_path / "brain_notices.json"
    _seed(path)
    clock = Clock(T0)
    reader = CountingReader()
    notifier = _notifier(path, clock, reader)

    notifier.observe({})
    first_pass = len(reader.reads)
    for _ in range(19):  # 20 ticks of 30 s: 10 minutes of the injected clock
        clock.advance(30)
        notifier.observe({})

    assert first_pass == PENDING
    assert len(reader.reads) == first_pass, "no read inside the window after the first pass"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["5"]["at"] == T0.isoformat(timespec="seconds"), "a skipped read does not refresh at"
    assert stored["5"]["key"] == "p5" and stored["5"]["state"] == "parked"


def test_each_pending_record_is_read_once_more_after_the_window(tmp_path):
    path = tmp_path / "brain_notices.json"
    _seed(path)
    clock = Clock(T0)
    reader = CountingReader()
    notifier = _notifier(path, clock, reader)
    notifier.observe({})  # first pass at T0

    clock.t = T0 + timedelta(seconds=PENDING_REREAD_SECONDS + 60)  # 31 minutes after the last real read
    before = len(reader.reads)
    notifier.observe({})
    assert len(reader.reads) - before == PENDING

    clock.advance(30)
    before = len(reader.reads)
    notifier.observe({})
    assert len(reader.reads) == before, "the window restarts from the read just made"


def test_a_record_whose_state_word_changed_is_read_at_once(tmp_path):
    path = tmp_path / "brain_notices.json"
    _seed(path)
    clock = Clock(T0)
    reader = CountingReader()
    notifier = _notifier(path, clock, reader)
    notifier.observe({})  # first pass at T0
    clock.advance(60)

    before = len(reader.reads)
    notifier.observe({7: "closed"})  # stored state of #7 is "parked": a changed word inside the window

    assert reader.reads[before:] == [7]
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["7"]["state"] == "closed"
