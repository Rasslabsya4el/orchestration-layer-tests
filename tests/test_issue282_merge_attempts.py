"""Unit 3 of issue 282: merge attempts count only results whose suite ran (or that ended in a conflict or a
red check); results with no suite run are merge waits (count per ticket, hours waited once per result); a
second `pushed` for a ticket already pushed is no attempt; the red class is three rows.

Seam: ops_buckets over a tmp_path root of hand-written result files (outcome fields only, no private text),
merge_attempts over result dicts, and the table rows. Expected values are written out by hand.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from tools.agentops.losses import ROWS, render_block, Window
from tools.agentops.losses_calc import MergeCounts, merge_attempts
from tools.agentops.losses_ops import ops_buckets

SINCE = datetime(2026, 10, 12, 0, 0).astimezone()
UNTIL = datetime(2026, 10, 13, 0, 0).astimezone()
NOW = datetime(2026, 10, 12, 14, 0).astimezone()
BASE = datetime(2026, 10, 12, 1, 0)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


class Folder:
    """Writes merge result files, one minute apart in time order."""

    def __init__(self, root: Path):
        self.dir = root / "var" / "agentops" / "results"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.count = 0

    def add(self, reason: str, issues: list[int], seconds: int = 0, **extra) -> None:
        ended = BASE + timedelta(minutes=self.count)
        body = {"kind": "merge", "argv": ["py", "-m", "tools.agentops", "merge", f"b{self.count}"],
                "rc": 0 if reason == "pushed" else 1, "reason": reason, "issues": issues,
                "started": _iso(ended - timedelta(seconds=seconds)), "ended": _iso(ended), **extra}
        name = f"2026101{2}T{ended:%H%M%S}-merge-b{self.count}.json"
        (self.dir / name).write_text(json.dumps(body), encoding="utf-8")
        self.count += 1


def build_fixture(root: Path) -> None:
    folder = Folder(root)
    ticket = 1000
    # 19 + 4 + 1 + 1 tickets: a failed attempt, then a landing -> one extra each
    for reason, how_many in (("red-suite", 19), ("red-on-branch", 4), ("red-on-base", 1), ("conflict", 1)):
        for _ in range(how_many):
            ticket += 1
            folder.add(reason, [ticket], suite_s=60.0)
            folder.add("pushed", [ticket])
    # a second `pushed` for ten of those tickets: no attempt
    for number in range(1001, 1011):
        folder.add("pushed", [number])
    # 26 merge waits, each ticket then lands at its first attempt
    waits = ([("lock-timeout", 600)] * 18
             + [("machine loaded: 100 % avg, waited 30 min", 2400)] * 3  # the text says 30 min, not 40
             + [("dirty", 0)] * 2
             + [("silent-holder", 3600)] * 2
             + [("already-queued", 0)])
    for reason, seconds in waits:
        ticket += 1
        folder.add(reason, [ticket], seconds=seconds)
        folder.add("pushed", [ticket])


def test_extras_and_waits_on_an_outcome_fixture(tmp_path):
    build_fixture(tmp_path)
    buckets = ops_buckets(tmp_path, SINCE, UNTIL, NOW)
    # 25 tickets with a failed attempt and a landing: 25 extras; the waits and the 10 repeated landings add none
    assert buckets.tickets_with_extra_attempts == 25
    assert buckets.extra_attempts == 25
    assert buckets.extra_red_suite == 19
    assert buckets.extra_red_on_branch == 4
    assert buckets.extra_red_on_base == 1
    assert buckets.extra_conflict == 1
    assert buckets.extra_other == 0
    assert buckets.unattributed_attempts == 0
    # 18 + 3 + 2 + 2 + 1 = 26 waits; hours: 18 x 10 min + 3 x 30 min (reason text) + 2 x 1 h + 0 + 0
    # = 180 + 90 + 120 min = 390 min = 23400 s
    assert buckets.merge_waits == 26
    assert buckets.merge_wait_seconds == 23400.0


def test_a_second_pushed_for_a_landed_ticket_is_no_attempt(tmp_path):
    folder = Folder(tmp_path)
    folder.add("red-suite", [7])
    folder.add("pushed", [7])
    folder.add("pushed", [7])
    buckets = ops_buckets(tmp_path, SINCE, UNTIL, NOW)
    assert buckets.extra_attempts == 1
    assert buckets.extra_red_suite == 1
    assert merge_attempts([
        {"kind": "merge", "reason": "pushed", "issues": [7]},
        {"kind": "merge", "reason": "pushed", "issues": [7]},
    ]) == {7: MergeCounts(attempts=1)}


def test_a_batch_counts_per_ticket_and_its_wait_hours_once(tmp_path):
    folder = Folder(tmp_path)
    folder.add("red-suite", [11, 12])
    folder.add("pushed", [11, 12])
    folder.add("lock-timeout", [13, 14], seconds=900)
    folder.add("pushed", [13, 14])
    buckets = ops_buckets(tmp_path, SINCE, UNTIL, NOW)
    assert buckets.tickets_with_extra_attempts == 2
    assert buckets.extra_attempts == 2
    assert buckets.extra_red_suite == 2
    assert buckets.merge_waits == 2  # one wait for each ticket the batch names
    assert buckets.merge_wait_seconds == 900.0  # the batch waited 15 min once


def test_wait_hours_fall_back_to_the_run_length_and_to_zero(tmp_path):
    folder = Folder(tmp_path)
    folder.add("lock-timeout", [21], seconds=1200)  # no minutes in the reason: ended - started = 20 min
    folder.add("silent-holder", [22], seconds=0, started=None)  # no start: nothing to measure
    buckets = ops_buckets(tmp_path, SINCE, UNTIL, NOW)
    assert buckets.merge_waits == 2
    assert buckets.merge_wait_seconds == 1200.0


def test_merge_attempts_separates_waits_from_attempts():
    results = [
        {"kind": "merge", "reason": "lock-timeout", "issues": [5]},
        {"kind": "merge", "reason": "machine loaded: 100 % avg, waited 30 min", "issues": [5]},
        {"kind": "merge", "reason": "red-on-branch", "issues": [5]},
        {"kind": "merge", "reason": "stuck", "issues": [6]},
    ]
    counts = merge_attempts(results)
    assert counts[5] == MergeCounts(attempts=1, red_suite=1, waits=2)
    assert counts[6] == MergeCounts(attempts=1, other=1)


def test_the_table_has_the_split_red_rows_and_the_waits_rows():
    keys = [row.key for row in ROWS]
    for key in ("extra_red_suite", "extra_red_on_branch", "extra_red_on_base", "extra_conflict",
                "merge_waits", "merge_wait_seconds"):
        assert key in keys
    labels = {row.key: row.label for row in ROWS}
    assert "per ticket" in labels["extra_attempts"]
    assert "per ticket" in labels["merge_waits"]
    values = {row.key: 0.0 for row in ROWS}
    values.update(merge_waits=26.0, merge_wait_seconds=23400.0, extra_red_on_base=1.0)
    window = Window(label="w", since=SINCE, until=UNTIL)
    text = "\n".join(render_block("repo", window, window, values, values, 10.0))
    assert "26" in text
    assert "6 h 30 min" in text


def test_a_wait_no_ticket_owns_is_in_neither_the_count_nor_the_hours(tmp_path):
    folder = Folder(tmp_path)
    folder.add("already-queued", [], seconds=600)  # its branch names no issue either
    folder.add("lock-timeout", [31], seconds=300)
    buckets = ops_buckets(tmp_path, SINCE, UNTIL, NOW)
    assert buckets.merge_waits == 1
    assert buckets.merge_wait_seconds == 300.0
    assert buckets.unattributed_attempts == 0


def test_two_pushes_no_ticket_owns_are_not_one_tickets_repeat():
    from tools.agentops.losses_calc import merge_outcomes

    results = [
        {"kind": "merge", "reason": "pushed", "issues": [], "argv": ["py", "merge", "cutover-home"]},
        {"kind": "merge", "reason": "pushed", "issues": [], "argv": ["py", "merge", "24-chat"]},
        {"kind": "merge", "reason": "pushed", "issues": [7]},
        {"kind": "merge", "reason": "pushed", "issues": [7]},
    ]
    assert merge_outcomes(results) == [(None, "landed"), (None, "landed"), (7, "landed")]
