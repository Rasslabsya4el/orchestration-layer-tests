"""Unit 1 of issue 282: the `start held` spans of the daily loss note.

Seam: ops_buckets (tools/agentops/losses_ops.py), called with a tmp_path root that holds only a
dispatch log. Expected values are written out by hand from the fixtures (never recomputed with the
code's own formula).

Rule 4 of #282: `start held` lines join into one span when at most 600 s apart (2 x the dispatcher's
300 s write interval); a span is credited from its first line to its last plus one 300 s interval.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from tools.agentops.losses_ops import ops_buckets

SINCE = datetime(2026, 10, 12, 0, 0, 0).astimezone()
UNTIL = datetime(2026, 10, 13, 0, 0, 0).astimezone()
NOW = datetime(2026, 10, 12, 14, 0, 0).astimezone()


def write_dispatch(root: Path, lines: list[str]) -> None:
    (root / "artifacts" / "runs").mkdir(parents=True, exist_ok=True)
    (root / "artifacts" / "runs" / "dispatch.out").write_text("\n".join(lines) + "\n", encoding="utf-8")


def held_line(moment: datetime, suffix: str = "") -> str:
    """A `start held` line in the dispatch log's own form: naive local time, then the message."""
    return f"{moment.strftime('%Y-%m-%d %H:%M:%S')} orch-dispatch: start held, wait: PC busy 10.3 cores{suffix}"


def test_three_lines_300s_apart_then_a_2h_gap_then_one_line_make_15_and_5_minutes(tmp_path):
    base = datetime(2026, 10, 12, 10, 0, 0)
    lines = [
        held_line(base, " [issues #3]"),                      # 10:00:00
        held_line(base + timedelta(seconds=300)),             # 10:05:00
        held_line(base + timedelta(seconds=600)),             # 10:10:00
        held_line(base + timedelta(hours=2, seconds=600)),    # 12:10:00: a 2 h gap, a new span
    ]
    write_dispatch(tmp_path, lines)
    buckets = ops_buckets(tmp_path, SINCE, UNTIL, NOW)
    # span 1: 10:00:00 .. 10:10:00 = 600 s, plus one 300 s interval = 900 s (15 min), issue 3 alone
    # span 2: one line = one 300 s interval (5 min), no issue
    assert buckets.held_start_seconds == 1200.0
    assert buckets.held_start_by_issue == {3: 900.0}


def test_poe_week_lines_about_325s_apart_for_34h_give_over_30h_held(tmp_path):
    # 377 lines from 14 Oct 00:00 at 325 s apart (about 34 h); three gaps of 700 s (under 15 min,
    # over the 600 s join gap) split the week into four spans.
    moment = datetime(2026, 10, 14, 0, 0, 0)
    lines = []
    for index in range(377):
        lines.append(held_line(moment))
        step = 700 if index in (100, 200, 300) else 325
        moment += timedelta(seconds=step)
    write_dispatch(tmp_path, lines)
    since = datetime(2026, 10, 14, 0, 0, 0).astimezone()
    until = datetime(2026, 10, 15, 12, 0, 0).astimezone()
    buckets = ops_buckets(tmp_path, since, until, until)
    # hand count: 121225 s inside the four spans (123325 s from the first line to the last, minus the
    # three 700 s breaks) plus 4 x 300 s = 122425 s, about 34 h
    assert buckets.held_start_seconds > 30 * 3600
