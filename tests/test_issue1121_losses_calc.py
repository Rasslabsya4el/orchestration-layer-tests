"""Unit 1 of issue 1121: the pure loss-table functions in tools/agentops/losses_calc.py.

Seams: the seven public functions of the module, called with literal fixtures. Expected values are
written out by hand (never recomputed with the code's own formula).
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tools.agentops.losses_calc import (
    MergeCounts,
    Span,
    doubled,
    held_spans,
    held_weights,
    log_spans,
    merge_attempts,
    parked_seconds,
    session_issue_weights,
)

MODULE = Path(__file__).resolve().parents[1] / "tools" / "agentops" / "losses_calc.py"


def at(hhmm: str, day: int = 15) -> datetime:
    return datetime.fromisoformat(f"2026-10-{day:02d}T{hhmm}:00+00:00")


def iso(hhmm: str, day: int = 15) -> str:
    return at(hhmm, day).isoformat()


def naive(hh: int, mm: int, ss: int = 0) -> datetime:
    """The dispatch log carries local time with no zone: log_spans returns naive datetimes."""
    return datetime(2026, 10, 12, hh, mm, ss)


# ---------------------------------------------------------------- held_spans

def test_held_spans_pairs_latest_taken_with_its_orch_end():
    events = [
        {"ts": iso("10:00"), "kind": "issue_taken", "issue": 1, "how": "offered"},
        {"ts": iso("11:00"), "kind": "orch_end", "issue": 1, "session": "s1", "outcome": "closed", "why": "done"},
        {"ts": iso("10:30"), "kind": "issue_taken", "issue": 2, "how": "claimed"},
        {"ts": iso("10:40"), "kind": "issue_taken", "issue": 2, "how": "claimed"},
        {"ts": iso("11:30"), "kind": "orch_end", "issue": None, "session": "s9", "outcome": "nothing_claimed"},
        {"ts": iso("12:00"), "kind": "orch_end", "issue": 2, "session": "s2", "outcome": "parked", "why": "idle"},
        {"ts": iso("12:30"), "kind": "orch_end", "issue": 7, "session": "s3", "outcome": "closed", "why": "done"},
        {"ts": iso("13:00"), "kind": "issue_taken", "issue": 3, "how": "offered"},
    ]
    spans = held_spans(events)
    assert spans == [
        Span(issue=1, session="s1", start=at("10:00"), end=at("11:00"), outcome="closed"),
        Span(issue=2, session="s2", start=at("10:40"), end=at("12:00"), outcome="parked"),
        Span(issue=3, session=None, start=at("13:00"), end=None, outcome=None),
    ]


def test_held_spans_reads_a_real_shaped_line():
    events = [
        {"ts": "2026-09-26T17:21:04+00:00", "kind": "orch_end", "issue": 6, "session": "3a25b860",
         "outcome": "closed", "why": "done"},
        {"ts": "2026-09-26T17:00:00+00:00", "kind": "issue_taken", "issue": 6, "how": "claimed"},
    ]
    spans = held_spans(events)
    assert spans == [
        Span(issue=6, session="3a25b860",
             start=datetime.fromisoformat("2026-09-26T17:00:00+00:00"),
             end=datetime.fromisoformat("2026-09-26T17:21:04+00:00"), outcome="closed"),
    ]


def test_held_spans_ignores_other_kinds_whatever_their_stamp():
    # the real log holds other kinds with a naive stamp or no stamp at all: they must not break the pairing
    events = [
        {"ts": "2026-10-02T15:16:27", "kind": "spike_step", "issue": 1118},
        {"kind": "watcher_start", "session": "fa4dbd56"},
        {"ts": iso("10:00"), "kind": "issue_taken", "issue": 1, "how": "offered"},
        {"ts": "2026-10-15T10:30:00", "kind": "end"},
        {"ts": iso("11:00"), "kind": "orch_end", "issue": 1, "session": "s1", "outcome": "closed", "why": "done"},
    ]
    assert held_spans(events) == [
        Span(issue=1, session="s1", start=at("10:00"), end=at("11:00"), outcome="closed"),
    ]


# ------------------------------------------------------------- held_weights

def test_held_weights_shares_of_seconds():
    assert held_weights({1118: 3600.0, 1119: 10800.0}) == pytest.approx({1118: 0.25, 1119: 0.75})


def test_held_weights_equal_shares_when_total_is_zero():
    assert held_weights({1: 0.0, 2: 0.0}) == {1: 0.5, 2: 0.5}


def test_held_weights_empty_is_empty():
    assert held_weights({}) == {}


# ------------------------------------------------------ session_issue_weights

def test_session_weights_one_hour_and_three_hours_give_a_quarter_and_three_quarters():
    spans = [
        Span(issue=1118, session="2593ce20", start=at("10:00"), end=at("11:00"), outcome="closed"),
        Span(issue=1119, session="2593ce20", start=at("11:00"), end=at("14:00"), outcome="closed"),
    ]
    weights = session_issue_weights(spans)
    assert set(weights) == {"2593ce20"}
    assert weights["2593ce20"] == pytest.approx({1118: 0.25, 1119: 0.75})


def test_session_weights_sum_the_spans_of_one_issue_and_skip_open_ones():
    spans = [
        Span(issue=1118, session="s1", start=at("10:00"), end=at("10:30"), outcome="parked"),
        Span(issue=1118, session="s1", start=at("11:00"), end=at("11:30"), outcome="closed"),
        Span(issue=1119, session="s1", start=at("12:00"), end=at("14:00"), outcome="closed"),
        Span(issue=1120, session="s1", start=at("15:00"), end=None, outcome=None),
    ]
    weights = session_issue_weights(spans)
    # 1118: 30 min + 30 min = 1 h; 1119: 2 h; the open span of 1120 is not closed
    assert weights["s1"] == pytest.approx({1118: 1 / 3, 1119: 2 / 3})


# ------------------------------------------------------------ parked_seconds

def test_parked_seconds_two_parks_one_resumed_one_still_parked():
    now = at("12:00")
    spans = [
        Span(issue=5, session="s1", start=at("09:00"), end=at("10:00"), outcome="parked"),
        Span(issue=5, session="s2", start=at("10:30"), end=at("11:00"), outcome="parked"),
        Span(issue=6, session="s3", start=at("08:00"), end=at("09:30"), outcome="closed"),
    ]
    events = [
        # resumed 30 min after the first park
        {"ts": iso("10:30"), "kind": "issue_taken", "issue": 5, "how": "claimed"},
        # taken before the first park ended: does not resume anything
        {"ts": iso("09:50"), "kind": "issue_taken", "issue": 5, "how": "claimed"},
        # the second park is never resumed
    ]
    assert parked_seconds(spans, events, now) == {5: 1800.0 + 3600.0}


def test_parked_seconds_still_parked_runs_to_now():
    now = at("12:00")
    spans = [Span(issue=8, session="s1", start=at("09:00"), end=at("11:30"), outcome="parked")]
    assert parked_seconds(spans, [], now) == {8: 1800.0}


# ------------------------------------------------------------ merge_attempts

def _merge(argv_tail: list[str], reason: str, issues: list[int] | None) -> dict:
    return {
        "kind": "merge",
        "argv": ["C:\\python.exe", "-m", "tools.agentops", "merge", *argv_tail],
        "rc": 0 if reason == "pushed" else 1,
        "reason": reason,
        "issues": issues if issues is not None else [],
    }


def test_merge_attempts_attributes_by_issues_then_branch_name():
    results = [
        _merge(["o1118u3", "--issue", "1118"], "red-suite", [1118]),
        _merge(["origin/o1118u4"], "conflict", None),
        _merge(["orch-464"], "lock-timeout", None),
        _merge(["o1119u1"], "red-on-branch", None),
        _merge(["o1119u2", "--issue", "1119"], "pushed", [1119]),
        _merge(["orch-779-u10"], "stuck", None),
        _merge(["o1118u5"], "red-on-base", None),
        {"kind": "full-run-commit", "argv": ["-m", "tools.agentops", "full-run"], "reason": "pushed",
         "issues": []},
    ]
    counts = merge_attempts(results)
    assert counts[1118] == MergeCounts(attempts=3, red_suite=2, conflict=1, other=0)
    assert counts[1119] == MergeCounts(attempts=2, red_suite=1, conflict=0, other=0)
    assert counts[464] == MergeCounts(attempts=1, red_suite=0, conflict=0, other=1)
    assert counts[779] == MergeCounts(attempts=1, red_suite=0, conflict=0, other=1)
    assert set(counts) == {1118, 1119, 464, 779}


# ---------------------------------------------------------------- log_spans

LOG = [
    "2026-10-12 04:57:06 orch-dispatch: orch-dispatch: start held, wait: PC busy 11.4 cores",
    "2026-10-12 05:02:23 orch-dispatch: orch-dispatch: start held, wait: PC busy 10.1 cores",
    "2026-10-12 05:03:00 orch-dispatch: orch-dispatch: start held, wait: PC busy 10.3 [issues #1118 #1119]",
    "2026-10-12 05:03:30 orch-dispatch: orch-dispatch: START HELD, wait: PC busy 10.7",
    "2026-10-12 05:08:00 orch-dispatch: gh issue list failed: rate limit exceeded",
    "2026-10-12 05:30:00 orch-dispatch: gh issue list failed: secondary rate limit [issues #1120]",
]


def test_log_spans_groups_matching_lines_by_gap_and_adds_one_tick():
    spans = log_spans(LOG, "start held")
    assert [s.start for s in spans] == [naive(4, 57, 6), naive(5, 2, 23)]
    first, second = spans
    assert first.end - first.start == timedelta(seconds=30)
    # 05:02:23 .. 05:03:30 is 67 s, plus one 30 s tick
    assert second.end - second.start == timedelta(seconds=97)
    assert first.issues == ()
    assert second.issues == (1118, 1119)


def test_log_spans_needle_is_case_insensitive():
    assert len(log_spans(LOG, "start HELD")) == 2


def test_log_spans_rate_limit_needle_collects_issues_of_its_own_lines():
    spans = log_spans(LOG, "rate limit")
    # 05:08:00 and 05:30:00 are 22 min apart: two spans
    assert len(spans) == 2
    assert spans[0].issues == ()
    assert spans[1].issues == (1120,)


def test_log_spans_skips_lines_without_timestamp():
    assert log_spans(["not a log line start held"], "start held") == []


# ------------------------------------------------------------------ doubled

def test_doubled_ten_to_twenty_is_doubled():
    assert doubled({"runs": 20.0}, {"runs": 10.0}) == ["runs"]


def test_doubled_ten_to_nineteen_is_not():
    assert doubled({"runs": 19.0}, {"runs": 10.0}) == []


def test_doubled_zero_before_is_not_doubled():
    assert doubled({"runs": 5.0}, {"runs": 0.0}) == []
    assert doubled({"runs": 5.0}, {}) == []


def test_doubled_returns_sorted_names():
    assert doubled({"b": 20.0, "a": 30.0, "c": 5.0}, {"b": 10.0, "a": 10.0, "c": 10.0}) == ["a", "b"]


# ------------------------------------------------------------ core rules

def test_module_imports_nothing_from_src():
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(("." * node.level) + (node.module or ""))
    assert not [n for n in names if n.startswith("src")]
