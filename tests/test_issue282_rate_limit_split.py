"""Unit 2 of issue 282: the rate-limit row of the daily loss note is split in two.

Seams: ops_buckets (tools/agentops/losses_ops.py), called with a tmp_path root that holds only a dispatch log;
the note's rows (ROWS in tools/agentops/losses.py) and doubling_lines (tools/agentops/losses_daily.py).
Expected values are written out by hand from the fixtures (never recomputed with the code's own formula).

Rule 5 of #282: `GitHub API limit` is every `rate limit` line that is not a Claude line; `Claude usage limit` is
every line that matches a usage-limit pattern (checked first). Spans are as before: at most 120 s apart, each span
credited from its first line to its last plus 30 s.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from tools.agentops.losses import ROWS
from tools.agentops.losses_daily import doubling_lines
from tools.agentops.losses_ops import ops_buckets

SINCE = datetime(2026, 10, 14, 0, 0, 0).astimezone()
UNTIL = datetime(2026, 10, 17, 0, 0, 0).astimezone()
NOW = datetime(2026, 10, 16, 12, 0, 0).astimezone()


def write_dispatch(root: Path, lines: list[str]) -> None:
    (root / "artifacts" / "runs").mkdir(parents=True, exist_ok=True)
    (root / "artifacts" / "runs" / "dispatch.out").write_text("\n".join(lines) + "\n", encoding="utf-8")


def stamp(moment: datetime) -> str:
    """The dispatch log's own form: naive local time."""
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def gh_line(moment: datetime, text: str = "gh: API rate limit exceeded, waiting until 16:00:00") -> str:
    return f"{stamp(moment)} orch-dispatch: gh issue list failed: {text}"


def run_of(first: datetime, last: datetime, step_s: int, text: str) -> list[str]:
    """One GitHub limit run: a line every ``step_s`` seconds from ``first`` to ``last`` (both included)."""
    lines = []
    moment = first
    while moment <= last:
        lines.append(gh_line(moment, text))
        moment += timedelta(seconds=step_s)
    return lines


def test_github_lines_in_three_spans_make_1h48min_and_no_claude_time(tmp_path):
    day = datetime(2026, 10, 15, 0, 0, 0)
    lines = (
        # span 1: 14:00:00 .. 14:30:00 = 1800 s, + 30 s = 1830 s
        run_of(day.replace(hour=14), day.replace(hour=14, minute=30), 60,
               "GraphQL: API rate limit already exceeded for user ID 1"),
        # span 2: 15:00:00 .. 15:50:00 = 3000 s, + 30 s = 3030 s
        run_of(day.replace(hour=15), day.replace(hour=15, minute=50), 60,
               "gh: API rate limit exceeded, waiting until 16:00:00"),
        # span 3: 17:00:00 .. 17:26:30 = 1590 s, + 30 s = 1620 s
        run_of(day.replace(hour=17), day.replace(hour=17, minute=26, second=30), 30,
               "gh: API rate limit exceeded, waiting until 17:30:00"),
    )
    write_dispatch(tmp_path, [line for run in lines for line in run])
    buckets = ops_buckets(tmp_path, SINCE, UNTIL, NOW)
    # 1830 + 3030 + 1620 = 6480 s = 1 h 48 min, all GitHub; no Claude line
    assert buckets.github_limit_seconds == 6480.0
    assert buckets.claude_limit_seconds == 0.0
    assert buckets.github_limit_by_issue == {}
    assert buckets.claude_limit_by_issue == {}


def test_a_claude_usage_limit_line_counts_in_the_claude_row_only(tmp_path):
    lines = [
        # GitHub alone at 10:00:00: one tick, 30 s
        gh_line(datetime(2026, 10, 15, 10, 0, 0), "gh: API rate limit exceeded, waiting until 10:30:00"),
        # Claude, at 12:00:00 and 12:01:00 (one span, 60 s + 30 s = 90 s). The first line also says
        # "rate limit", and it is a Claude line: it must not reach the GitHub row.
        f"{stamp(datetime(2026, 10, 15, 12, 0, 0))} orch-dispatch: ORCH stopped: You've hit your limit, "
        "resets 13:00 (rate limit window)",
        f"{stamp(datetime(2026, 10, 15, 12, 1, 0))} orch-dispatch: claude usage limit reached, waiting",
    ]
    write_dispatch(tmp_path, lines)
    buckets = ops_buckets(tmp_path, SINCE, UNTIL, NOW)
    assert buckets.github_limit_seconds == 30.0
    assert buckets.claude_limit_seconds == 90.0
    assert buckets.github_limit_by_issue == {}
    assert buckets.claude_limit_by_issue == {}


def test_the_note_names_the_two_limit_rows_apart():
    limit_rows = [row for row in ROWS if "limit" in row.key]
    assert [row.key for row in limit_rows] == ["github_limit_seconds", "claude_limit_seconds"]
    assert [row.label for row in limit_rows] == ["hours lost to GitHub API limit", "hours lost to Claude usage limit"]


def test_each_limit_row_is_compared_with_its_own_week_before():
    # GitHub doubled (2 h against 1 h); Claude unchanged (15 min against 15 min): one line, for GitHub only
    this_week = {"github_limit_seconds": 7200.0, "claude_limit_seconds": 900.0}
    week_before = {"github_limit_seconds": 3600.0, "claude_limit_seconds": 900.0}
    lines = doubling_lines({"Owner/repo": (this_week, week_before)})
    assert len(lines) == 1
    assert lines[0].startswith("Owner/repo: hours lost to GitHub API limit doubled in 7 days:")
