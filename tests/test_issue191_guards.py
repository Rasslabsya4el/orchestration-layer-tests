"""Acceptance row 4 of #191: the guard and repair part of `logic-audit` (`tools/agentops/logic_audit_guards.py`).

Fixtures live in tmp_path; no real repo log is read. Every test injects `rule_changed`, so no git call runs.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from tools.agentops.logic_audit_guards import guard_rows, guard_stats
from tools.agentops.logic_audit_rows import NO_SOURCE

UNTIL = datetime(2026, 10, 18, 17, 0)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _no_change(guard: str, when: datetime) -> bool:
    return False


def _by_guard(stats):
    return {stat.guard: stat for stat in stats}


def test_blocking_guard_counts_the_false_fire_and_flags_it(tmp_path):
    # three ejects on three branches; a.txt still holds `foo` on line 3 (same text went through unchanged),
    # b.txt and c.txt no longer do, and their branches get a later `pushed` line
    _write(tmp_path, "var/agentops/merge.log",
           "2026-10-18 10:00:00 | b1 | ejected: target-check R rejects core text: a.txt:3 foo\n"
           "2026-10-18 10:05:00 | b2 | ejected: target-check R rejects core text: b.txt:3 foo\n"
           "2026-10-18 10:06:00 | b2 | pushed | suite 1 min\n"
           "2026-10-18 10:10:00 | b3 | ejected: target-check R rejects core text: c.txt:3 foo\n"
           "2026-10-18 10:12:00 | b3 | pushed | suite 1 min\n")
    texts = {"a.txt": "one\ntwo\nfoo here\n", "b.txt": "one\ntwo\nbar\n", "c.txt": "one\ntwo\nbaz\n"}

    stats = guard_stats(tmp_path, UNTIL, file_text=texts.get, rule_changed=_no_change)

    stat = _by_guard(stats)["merge eject: target-check"]
    assert (stat.kind, stat.fires, stat.false, stat.real, stat.unknown) == ("blocking", 3, 1, 2, 0)
    assert "blocking: false fire" in stat.flags


def test_warning_guard_with_one_false_in_twenty_is_not_flagged(tmp_path):
    _write(tmp_path, "var/logs/tracker_guard.jsonl", "".join(
        json.dumps({"time": f"2026-10-17T09:{minute:02d}:00", "session": f"s{minute}", "kind": "issue",
                    "command": f"gh issue create {minute}"}) + "\n"
        for minute in range(20)))

    def only_one(guard: str, when: datetime) -> bool:
        return when == datetime(2026, 10, 17, 9, 7, 0)

    stats = guard_stats(tmp_path, UNTIL, rule_changed=only_one)

    stat = _by_guard(stats)["tracker guard: issue"]
    assert (stat.kind, stat.fires, stat.false) == ("warning", 20, 1)
    assert stat.flags == ()


def test_known_guard_with_no_fires_is_listed_with_the_zero_flag(tmp_path):
    _write(tmp_path, "artifacts/runs/events.jsonl",
           json.dumps({"ts": "2026-10-17T15:04:15", "kind": "orch_start", "issues": [191]}) + "\n")

    stats = guard_stats(tmp_path, UNTIL, rule_changed=_no_change,
                        known_guards={"dispatch permission_refused": "blocking"})

    stat = _by_guard(stats)["dispatch permission_refused"]
    assert stat.fires == 0
    assert "0 fires in 14 days (listed, not removed)" in stat.flags


def test_durations_in_the_row_read_for_a_human(tmp_path):
    # the eject at 10:00 is pushed at 12:01: a 7260 s stop, which must read `2 h 01 min`
    _write(tmp_path, "var/agentops/merge.log",
           "2026-10-18 10:00:00 | b1 | ejected: target-check R rejects core text: a.txt:3 foo\n"
           "2026-10-18 12:01:00 | b1 | pushed | suite 1 min\n")

    rows = guard_rows(tmp_path, UNTIL, rule_changed=_no_change, known_guards={})

    assert len(rows) == 1
    row = rows[0]
    assert row.kind == "guard"
    assert "stopped work 2 h 01 min" in row.finding
    assert "7260" not in row.finding
    assert re.search(r"\b\d{3,} s\b", row.finding) is None


def test_warning_guard_stops_no_work_and_a_blocking_guard_does(tmp_path):
    # two fires five minutes apart in one session: a warning stops nothing, a blocking guard stops 300 s
    _write(tmp_path, "var/logs/tracker_guard.jsonl",
           json.dumps({"time": "2026-10-17T09:00:00", "session": "s1", "kind": "issue", "command": "gh issue list"}) + "\n"
           + json.dumps({"time": "2026-10-17T09:05:00", "session": "s1", "kind": "issue", "command": "gh issue view 1"}) + "\n")
    _write(tmp_path, "var/agentops/permission_guard.log",
           json.dumps({"ts": "2026-10-17T09:00:00", "session": "p1", "tool": "Bash", "line": 1,
                       "rule": "bulk delete", "hit": "rm -r a", "text": "rm -r a"}) + "\n"
           + json.dumps({"ts": "2026-10-17T09:05:00", "session": "p1", "tool": "Bash", "line": 1,
                         "rule": "bulk delete", "hit": "rm -r b", "text": "rm -r b"}) + "\n")

    stats = _by_guard(guard_stats(tmp_path, UNTIL, rule_changed=_no_change, known_guards={}))

    assert stats["tracker guard: issue"].stopped_s == 0
    assert stats["permission: bulk delete"].stopped_s == 300


def test_batch_push_line_pushes_every_member_branch(tmp_path):
    # u1 is ejected 30 min before the batch push (1800 s), u2 20 min before it (1200 s): both count as pushed
    _write(tmp_path, "var/agentops/merge.log",
           "2026-10-18 10:00:00 | u1 | ejected: red-suite R failed\n"
           "2026-10-18 10:10:00 | u2 | ejected: red-suite R failed\n"
           "2026-10-18 10:30:00 | batch of 2: u1, u2 | pushed | suite 1 min | "
           "0 failed after re-check of 1: tests/t.py::x\n")

    stats = _by_guard(guard_stats(tmp_path, UNTIL, rule_changed=_no_change, known_guards={}))

    stat = stats["merge eject: red-suite"]
    assert (stat.fires, stat.real, stat.unknown) == (2, 2, 0)
    assert stat.stopped_s == 3000


def test_known_guard_with_a_missing_log_is_listed_while_another_source_exists(tmp_path):
    _write(tmp_path, "var/agentops/merge.log", "2026-10-18 10:00:00 | b1 | pushed | suite 1 min\n")

    stats = _by_guard(guard_stats(tmp_path, UNTIL, rule_changed=_no_change,
                                  known_guards={"tracker guard: issue": "warning"}))

    stat = stats["tracker guard: issue"]
    assert (stat.fires, stat.source) == (0, "var/logs/tracker_guard.jsonl")
    assert "0 fires in 14 days (listed, not removed)" in stat.flags


def test_git_failure_flags_every_stat_as_rule_check_unavailable(tmp_path):
    # the default rule check runs git in a folder that is no repo: the git call fails, and every stat says so
    _write(tmp_path, "tools/hooks/tracker_guard.py", "# guard\n")
    _write(tmp_path, "var/logs/tracker_guard.jsonl",
           json.dumps({"time": "2026-10-17T09:00:00", "session": "s1", "kind": "issue", "command": "gh issue list"}) + "\n")

    stats = guard_stats(tmp_path, UNTIL)

    assert stats
    assert all("rule check unavailable" in stat.flags for stat in stats)


def test_repo_without_any_guard_record_gives_one_no_source_row(tmp_path):
    rows = guard_rows(tmp_path, UNTIL, rule_changed=_no_change)

    assert [row.kind for row in rows] == [NO_SOURCE]
