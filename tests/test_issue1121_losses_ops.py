"""Unit 4b of issue 1121: ops_buckets in tools/agentops/losses_ops.py.

Seam: the one public function, called with a tmp_path root holding hand-written files of the three
kinds (events log, merge results, dispatch log). Every expected value is written out by hand from the
fixture below (never recomputed with the code's own formula). Times are built as local time with
``astimezone()``, so the test does not depend on the machine's zone.
"""

from __future__ import annotations

import ast
import dataclasses
import json
from datetime import datetime
from pathlib import Path

from tools.agentops.losses_ops import OpsBuckets, ops_buckets

MODULE = Path(__file__).resolve().parents[1] / "tools" / "agentops" / "losses_ops.py"


def local(day: int, hh: int, mm: int = 0, ss: int = 0) -> datetime:
    """A tz-aware local moment on 2026-10-<day>."""
    return datetime(2026, 10, day, hh, mm, ss).astimezone()


def naive_stamp(day: int, hh: int, mm: int = 0, ss: int = 0) -> str:
    """The dispatch log's own form: naive local time."""
    return f"2026-10-{day:02d} {hh:02d}:{mm:02d}:{ss:02d}"


SINCE = local(12, 0)
UNTIL = local(13, 0)
NOW = local(12, 14)


def _event(when: datetime, kind: str, issue: int | None, **extra) -> str:
    row = {"ts": when.isoformat(), "kind": kind, "issue": issue, **extra}
    return json.dumps(row)


def _merge(argv_tail: list[str], reason: str, issues: list[int], **stamps) -> dict:
    return {
        "kind": "merge",
        "argv": ["C:\\python.exe", "-m", "tools.agentops", "merge", *argv_tail],
        "rc": 0 if reason == "pushed" else 1,
        "reason": reason,
        "issues": issues,
        **stamps,
    }


def build_fixture(root: Path) -> None:
    # ---- events log: orch sessions (aware stamps), one naive other-kind line, one malformed line
    events = [
        _event(local(11, 9), "issue_taken", 20, how="claimed"),
        _event(local(12, 10), "orch_end", 20, session="s1", outcome="parked", why="waits"),
        _event(local(12, 10, 30), "issue_taken", 20, how="claimed"),  # resumed 30 min later
        _event(local(11, 8), "issue_taken", 22, how="claimed"),
        _event(local(11, 10), "orch_end", 22, session="s0", outcome="parked", why="waits"),  # before window
        _event(local(12, 11), "issue_taken", 21, how="claimed"),
        _event(local(12, 12), "orch_end", 21, session="s2", outcome="parked", why="waits"),  # never resumed
        _event(local(11, 20), "issue_taken", 30, how="offered"),
        _event(local(11, 21), "orch_end", 30, session="s3", outcome="parked", why="idle"),  # 1 h, before window
        _event(local(12, 6), "issue_taken", 30, how="claimed"),
        _event(local(12, 9), "orch_end", 30, session="s4", outcome="closed", why="done"),  # 3 h
        _event(local(12, 13), "issue_taken", 31, how="claimed"),
        _event(local(12, 15), "orch_end", 31, session="s5", outcome="closed", why="done"),  # 2 h
        _event(local(11, 10), "issue_taken", 32, how="claimed"),
        _event(local(11, 11), "orch_end", 32, session="s6", outcome="closed", why="done"),  # closed before window
        _event(local(12, 16), "orch_end", None, session="s7", outcome="nothing_claimed"),
        json.dumps({"ts": "2026-10-02T15:16:27", "kind": "spike_step", "issue": 1118}),
        "{not json",
    ]
    (root / "artifacts" / "runs").mkdir(parents=True, exist_ok=True)
    (root / "artifacts" / "runs" / "events.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")

    # ---- merge results
    results_dir = root / "var" / "agentops" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    files = {
        # issue 1118: red, conflict (branch-named, started only), pushed, conflict (no stamp: file name)
        "20261012T100500-merge-o1118u3.json": _merge(
            ["o1118u3", "--issue", "1118"], "red-suite", [1118], ended="2026-10-12T10:05:00"),
        "20261012T101000-merge-o1118u4.json": _merge(
            ["origin/o1118u4"], "conflict", [], started="2026-10-12T10:20:00"),
        "20261012T102500-merge-o1118u5.json": _merge(
            ["o1118u5", "--issue", "1118"], "pushed", [1118], ended="2026-10-12T10:40:00"),
        "20261012T150000-merge-o1118u6.json": _merge(
            ["o1118u6"], "conflict", [1118]),
        # issue 77 by branch name only (orch- form): red, red, pushed
        "20261012T110000-merge-orch-77.json": _merge(["orch-77"], "red-suite", [], ended="2026-10-12T11:00:00"),
        "20261012T110300-merge-orch-77b.json": _merge(["orch-77"], "red-suite", [], ended="2026-10-12T11:03:00"),
        "20261012T110600-merge-orch-77c.json": _merge(["orch-77"], "pushed", [], ended="2026-10-12T11:06:00"),
        # no ticket: a lock timeout whose branch names no issue
        "20261012T120000-merge-mainline.json": _merge(
            ["mainline"], "lock-timeout", [], ended="2026-10-12T12:00:00"),
        # not a merge: skipped whatever its reason
        "20261012T130000-full-run-commit.json": {
            "kind": "full-run-commit", "argv": ["-m", "tools.agentops", "full-run"], "reason": "pushed",
            "issues": [], "ended": "2026-10-12T13:00:00"},
        # before the window: skipped
        "20261011T235900-merge-o1118u9.json": _merge(
            ["o1118u9"], "red-suite", [1118], ended="2026-10-11T23:59:00"),
    }
    for name, body in files.items():
        (results_dir / name).write_text(json.dumps(body), encoding="utf-8")
    (results_dir / "20261012T140000-merge-bad.json").write_text("{not json", encoding="utf-8")

    # ---- dispatch log: naive local stamps; one bad date, one line without a stamp
    # the log is appended in time order (log_spans groups consecutive lines)
    dispatch = [
        f"{naive_stamp(11, 23, 59, 0)} orch-dispatch: start held, wait: PC busy 10.0",  # before window
        f"{naive_stamp(12, 10, 0, 0)} orch-dispatch: start held, wait: PC busy 10.3 [issues #5 #6]",
        f"{naive_stamp(12, 10, 0, 30)} orch-dispatch: start held, wait: PC busy 10.5 [issues #5 #6]",
        f"{naive_stamp(12, 10, 1, 30)} orch-dispatch: start held, wait: PC busy 10.9 [issues #5 #6]",
        f"{naive_stamp(12, 11, 0, 0)} orch-dispatch: start held, wait: PC busy 11.0",
        f"{naive_stamp(12, 11, 0, 30)} orch-dispatch: start held, wait: PC busy 11.1",
        f"{naive_stamp(12, 23, 59, 30)} orch-dispatch: start held, wait: PC busy 10.2 [issues #7]",
        f"{naive_stamp(13, 0, 0, 30)} orch-dispatch: start held, wait: PC busy 10.4 [issues #7]",  # clipped at until
        "2026-13-40 10:00:00 orch-dispatch: start held, wait: bad date",
        "start held with no stamp at all",
        f"{naive_stamp(12, 14, 23, 33)} orch-dispatch: gh issue list failed: HTTP 403: secondary rate limit [issues #9]",
        f"{naive_stamp(12, 14, 24, 3)} orch-dispatch: gh issue list failed: rate limit exceeded [issues #9]",
        f"{naive_stamp(12, 16, 0, 0)} orch-dispatch: gh issue list failed: rate limit exceeded",
    ]
    (root / "artifacts" / "runs" / "dispatch.out").write_text("\n".join(dispatch) + "\n", encoding="utf-8")


def test_ops_buckets_on_the_fixed_fixture(tmp_path):
    build_fixture(tmp_path)
    buckets = ops_buckets(tmp_path, SINCE, UNTIL, NOW)
    assert isinstance(buckets, OpsBuckets)
    # merges: 1118 has 4 attempts in the window (red, conflict, pushed, conflict): extras 3, and its
    # 3 not-landed attempts all fit (red 1, conflict 2). 77 has 3 (red, red, pushed): extras 2, and
    # its 2 not-landed attempts fit (red 2).
    assert buckets.tickets_with_extra_attempts == 2
    assert buckets.extra_attempts == 5
    assert buckets.extra_red_suite == 3
    assert buckets.extra_conflict == 2
    assert buckets.extra_other == 0
    # #282: the lock timeout of the branch that names no issue is a wait, not an attempt; no ticket
    # owns it, so the per-ticket waits do not count it either
    assert buckets.unattributed_attempts == 0
    assert buckets.merge_waits == 0
    assert buckets.merge_wait_seconds == 0.0
    # dispatch (#282 rule 4: join at most 600 s apart, one 300 s interval per span's last line):
    # 10:00:00..10:01:30 + 300 s = 390 s (5 and 6 get 195 s each); 11:00:00..11:00:30 + 300 s = 330 s
    # with no issue; 23:59:30 .. 00:00:30 + 300 s clipped at until = 30 s (issue 7); the 11 Oct 23:59:00
    # line is before the window, but its 300 s interval runs into it: 00:00 .. 00:04 = 240 s, no issue.
    # Total 390 + 330 + 30 + 240 = 990 s (was 210 s under the old 120 s / 30 s rule).
    assert buckets.held_start_seconds == 990.0
    assert buckets.held_start_by_issue == {5: 195.0, 6: 195.0, 7: 30.0}
    # rate limit: 14:23:33..14:24:03 + tick = 60 s (issue 9); 16:00:00 alone = one tick, no issue
    assert buckets.github_limit_seconds == 90.0  # #282 rule 5: renamed field, same value
    assert buckets.github_limit_by_issue == {9: 60.0}
    assert buckets.claude_limit_seconds == 0.0  # no Claude usage-limit line in this fixture
    assert buckets.claude_limit_by_issue == {}
    # parked seconds inside the window, each park ending at the earliest of the next taking, the next
    # closed end, until, now: 20 parked 10:00, taken 10:30 = 1800 s; 21 parked 12:00, now 14:00 = 7200 s;
    # 22 parked on 11 Oct, never taken, now 14:00 on 12 Oct = 14 h inside the window = 50400 s;
    # 30's first park (21:00 on 11 Oct) until its taking at 06:00 on 12 Oct = 6 h inside = 21600 s
    assert buckets.parked_seconds == 81000.0
    # closed in the window: 30 (1 h + 3 h = 14400 s) and 31 (2 h = 7200 s); median 10800 s.
    # 32 closed before the window does not count.
    assert buckets.hours_worked_median_seconds == 10800.0
    assert buckets.no_ticket_events_note == ""


def test_missing_files_give_zeros(tmp_path):
    buckets = ops_buckets(tmp_path, SINCE, UNTIL, NOW)
    assert buckets == OpsBuckets(
        tickets_with_extra_attempts=0,
        extra_attempts=0,
        extra_red_suite=0,
        extra_red_on_branch=0,
        extra_red_on_base=0,
        extra_conflict=0,
        extra_other=0,
        unattributed_attempts=0,
        merge_waits=0,
        merge_wait_seconds=0.0,
        held_start_seconds=0.0,
        github_limit_seconds=0.0,
        claude_limit_seconds=0.0,
        held_start_by_issue={},
        github_limit_by_issue={},
        claude_limit_by_issue={},
        parked_seconds=0.0,
        hours_worked_median_seconds=None,
        no_ticket_events_note="",
    )


def _write_events(root: Path, rows: list[str]) -> None:
    (root / "artifacts" / "runs").mkdir(parents=True, exist_ok=True)
    (root / "artifacts" / "runs" / "events.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _write_result(root: Path, name: str, body: dict) -> None:
    folder = root / "var" / "agentops" / "results"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps(body), encoding="utf-8")


def test_spec_example_red_conflict_pushed_gives_one_red_and_one_conflict(tmp_path):
    # the spec's example: the first attempt is not a free pass, its class counts (extras = attempts - 1)
    _write_result(tmp_path, "20261012T100000-merge-o88u1.json",
                  _merge(["o88u1", "--issue", "88"], "red-suite", [88], ended="2026-10-12T10:00:00"))
    _write_result(tmp_path, "20261012T110000-merge-o88u2.json",
                  _merge(["o88u2", "--issue", "88"], "conflict", [88], ended="2026-10-12T11:00:00"))
    _write_result(tmp_path, "20261012T120000-merge-o88u3.json",
                  _merge(["o88u3", "--issue", "88"], "pushed", [88], ended="2026-10-12T12:00:00"))
    buckets = ops_buckets(tmp_path, SINCE, UNTIL, NOW)
    assert buckets.tickets_with_extra_attempts == 1
    assert buckets.extra_attempts == 2
    assert buckets.extra_red_suite == 1
    assert buckets.extra_conflict == 1
    assert buckets.extra_other == 0


def test_never_landed_ticket_drops_its_last_not_landed_attempts_first(tmp_path):
    # three red suites, never landed: extras 2, so the third (last) red is dropped from the classes
    for hour in (10, 11, 12):
        _write_result(tmp_path, f"20261012T{hour:02d}0000-merge-o89u{hour}.json",
                      _merge([f"o89u{hour}", "--issue", "89"], "red-suite", [89],
                             ended=f"2026-10-12T{hour:02d}:00:00"))
    buckets = ops_buckets(tmp_path, SINCE, UNTIL, NOW)
    assert buckets.tickets_with_extra_attempts == 1
    assert buckets.extra_attempts == 2
    assert buckets.extra_red_suite == 2
    assert buckets.extra_conflict == 0
    assert buckets.extra_other == 0


def test_park_crossing_the_window_end_is_clipped_to_the_window(tmp_path):
    # issue 40 parked at 23:00 on 12 Oct, never taken again; now is 05:00 on 13 Oct: inside the window
    # only 23:00 .. 00:00 = 1 h = 3600 s
    _write_events(tmp_path, [
        _event(local(12, 22), "issue_taken", 40, how="claimed"),
        _event(local(12, 23), "orch_end", 40, session="s1", outcome="parked", why="waits"),
    ])
    buckets = ops_buckets(tmp_path, SINCE, UNTIL, local(13, 5))
    assert buckets.parked_seconds == 3600.0


def test_park_closed_while_parked_ends_at_the_close(tmp_path):
    # issue 41 parked at 06:00, then closed at 08:00 with no taking in between: 2 h = 7200 s,
    # not up to now (14:00)
    _write_events(tmp_path, [
        _event(local(12, 4), "issue_taken", 41, how="claimed"),
        _event(local(12, 6), "orch_end", 41, session="s8", outcome="parked", why="waits"),
        _event(local(12, 8), "orch_end", 41, session="s9", outcome="closed", why="done"),
    ])
    buckets = ops_buckets(tmp_path, SINCE, UNTIL, NOW)
    assert buckets.parked_seconds == 7200.0


def test_park_of_an_issue_closed_by_rest_ends_at_its_closed_at(tmp_path):
    # issue 42 parked at 10:00 on 12 Oct and never taken again; the REST listing says it closed at 11:00
    # (no orch_end closed it). Now is 14:00: the park counts 10:00 .. 11:00 = 1 h = 3600 s, not 4 h.
    _write_events(tmp_path, [
        _event(local(12, 8), "issue_taken", 42, how="claimed"),
        _event(local(12, 10), "orch_end", 42, session="s10", outcome="parked", why="waits"),
    ])
    without_rest = ops_buckets(tmp_path, SINCE, UNTIL, NOW)
    assert without_rest.parked_seconds == 14400.0
    with_rest = ops_buckets(tmp_path, SINCE, UNTIL, NOW, closed_at={42: local(12, 11)})
    assert with_rest.parked_seconds == 3600.0


def test_buckets_is_a_frozen_dataclass():
    assert dataclasses.is_dataclass(OpsBuckets)
    assert OpsBuckets.__dataclass_params__.frozen


def test_module_imports_nothing_from_src_and_holds_no_issue_numbers_or_dates():
    text = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(text)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(("." * node.level) + (node.module or ""))
    assert not [n for n in names if n.startswith("src")]
    import re
    assert not re.search(r"#\d+\b|\bD\d{2,3}\b|\bADR \d{3,4}\b|\b20\d\d-\d\d-\d\d\b", text)
