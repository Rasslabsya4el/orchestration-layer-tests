"""Issue #155 (unit 1 of 3): the monitor's Progress groups in Python, and `sweep list` over them. No network.

The expected groups are worked by hand from the monitor's rule, `progressLines` in the monitor plugin hooks (kit.tsx)
(:1199-1224): each open ticket is counted once, on the first line that fits, in this order: parked, its triage
label, autopilot, folder (wayfinder:map / core-epic label, or an open sub-ticket), blocked (an open blocker on it or
on an ancestor), parked-on-run, no label. Lines are PROGRESS_LINES order; a line with 0 is left out except
needs-triage and no label.
"""
from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from tools.agentops import sweep as S
from tools.agentops import ticket_groups as G

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
MARK = "<!-- parked-on-run: 2026-10-14 -->"


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gh(n, labels=(), days=0.0, blockers=(), parent=None, total=0, completed=0, comments=(), assignees=(),
        state="OPEN"):
    """A `gh issue list --json` record with the fields the groups and the sweep read."""
    return {
        "number": n, "title": f"title {n}", "state": state,
        "labels": [{"name": x} for x in labels], "updatedAt": _iso(days),
        "blockedBy": {"nodes": [{"number": b, "state": s} for b, s in blockers], "totalCount": len(blockers)},
        "subIssuesSummary": {"total": total, "completed": completed},
        "parent": {"number": parent, "state": "OPEN"} if parent is not None else None,
        "assignees": [{"login": a} for a in assignees],
        "comments": [{"body": b} for b in comments],
    }


def _by_number(issues):
    return {int(i["number"]): i for i in issues}


def _group_fixture():
    """(a) ready-for-agent + parked, (b) parked-on-run, (c) no label, (d) needs-triage, (e) blocked,
    (f) map folder, plus a ready-for-agent alone and a child of the map."""
    return [
        _gh(1, ["ready-for-agent", "parked"]),
        _gh(2, comments=[MARK + "\nwaiting on the run"]),
        _gh(3),
        _gh(4, ["needs-triage"]),
        _gh(5, blockers=[(90, "OPEN")]),
        _gh(6, ["wayfinder:map"]),
        _gh(7, ["ready-for-agent"]),
        _gh(8, parent=6),
    ]


def test_each_open_issue_gets_the_ts_group_and_the_counts_match_by_hand():
    issues = _group_fixture()
    by = _by_number(issues)
    groups = {int(i["number"]): G.ticket_group(i, by) for i in issues}
    # by hand, from kit.tsx progressLines: 1 parked first; 2 parked-on-run; 3 nothing; 4 triage; 5 blocked;
    # 6 folder by its label; 7 ready-for-agent; 8 nothing (its parent 6 has no blocker, 8 has no kid)
    assert groups == {1: "parked", 2: "parked-on-run", 3: "no label", 4: "needs-triage", 5: "blocked",
                      6: "folder", 7: "ready-for-agent", 8: "no label"}
    assert G.group_counts(groups.values()) == [
        ("needs-triage", 1), ("ready-for-agent", 1), ("parked", 1), ("folder", 1),
        ("blocked", 1), ("parked-on-run", 1), ("no label", 2)]


def test_groups_order_is_the_progress_lines_order():
    assert G.GROUPS == ("needs-triage", "needs-info", "ready-for-agent", "ready-for-human", "wontfix",
                        "parked", "autopilot", "folder", "blocked", "parked-on-run", "no label")


def test_parked_on_run_needs_no_assignee_and_the_marker_in_the_last_thirty_comments():
    issues = [
        _gh(20, comments=[MARK], assignees=["someone"]),                 # assigned: not parked on a run
        _gh(21, comments=[MARK] + ["x"] * 29),                           # marker is the 30th from last: counted
        _gh(22, comments=[MARK] + ["x"] * 30),                           # marker is the 31st from last: not
    ]
    by = _by_number(issues)
    assert [G.ticket_group(i, by) for i in issues] == ["no label", "parked-on-run", "no label"]


def test_folder_comes_from_an_open_kid_and_blocked_from_an_ancestor_only():
    issues = [
        _gh(10),                                    # has an open kid: folder
        _gh(11, parent=10),
        _gh(13),                                    # its only kid is closed: no label
        _gh(12, parent=13, state="CLOSED"),
        _gh(14, blockers=[(90, "OPEN")]),           # blocked on its own open blocker
        _gh(15, parent=14),                         # blocked through its ancestor 14
        _gh(16, blockers=[(91, "CLOSED")]),         # a closed blocker is no blocker
    ]
    by = _by_number(issues)
    assert G.ticket_group(by[10], by) == "folder"
    assert G.ticket_group(by[13], by) == "no label"
    assert G.ticket_group(by[15], by) == "blocked"
    assert G.ticket_group(by[16], by) == "no label"


def test_a_closed_issue_has_no_group_and_counts_nothing():
    issues = [_gh(30, state="CLOSED", labels=["needs-triage"])]
    assert G.ticket_group(issues[0], _by_number(issues)) is None
    assert G.group_counts([None]) == [("needs-triage", 0), ("no label", 0)]


class FakeRunner:
    def __init__(self, issue_json, sub_json=None):
        self.issue_json, self.sub_json, self.calls = issue_json, sub_json or "", []

    def __call__(self, args):
        self.calls.append(list(args))
        out = self.issue_json if args[:2] == ["issue", "list"] else self.sub_json
        return SimpleNamespace(returncode=0, stdout=out, stderr="")


def _sweep_fixture():
    issues = [
        _gh(1, ["ready-for-agent", "parked"], days=5),       # group parked; ready, so never due
        _gh(2, days=3, comments=[MARK]),                     # parked-on-run, 3 days: due
        _gh(3, days=1),                                      # no label, 1 day: not due
        _gh(4, ["needs-triage"], days=4),                    # due
        _gh(5, days=1, blockers=[(90, "OPEN")]),            # blocked, 1 day: not due
        _gh(6, ["wayfinder:map"], days=0, total=1, completed=1),  # folder; its child is 3 days quiet: due
        _gh(7, ["ready-for-agent"], days=5),                 # ready alone: listed, never due
    ]
    sub = json.dumps([{"number": 61, "state": "closed", "updated_at": _iso(3)}])
    return issues, sub


SWEEP_GROUP_LINES = ["needs-triage 1", "ready-for-agent 1", "parked 1", "folder 1", "blocked 1",
                     "parked-on-run 1", "no label 1"]


def test_sweep_list_lists_ready_for_agent_parked_and_counts_it_in_parked():
    issues, sub = _sweep_fixture()
    gh = S.GhSweep("o/r", FakeRunner(json.dumps(issues), sub))
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = S.run_sweep(argparse.Namespace(sweep_command="list"), gh=gh, now=NOW)
    lines = buf.getvalue().splitlines()
    assert code == 0
    assert lines[-8:] == ["rows 7, due 3", *SWEEP_GROUP_LINES]
    row1 = next(line for line in lines if line.startswith("#1 "))
    assert "ready-for-agent,parked" in row1 and "parked" in row1 and "not due" in row1


def test_a_ready_for_agent_issue_is_listed_with_its_group_and_never_due():
    issues, sub = _sweep_fixture()
    rows = {r.number: r for r in S.build_rows(issues, {6: [{"number": 61, "state": "CLOSED",
                                                             "updatedAt": _iso(3)}]}, NOW)}
    assert rows[7].labels == ("ready-for-agent",) and rows[7].group == "ready-for-agent"
    assert rows[1].group == "parked" and rows[1].due is False   # 5 days quiet, no blocker: due by age, not ready
    assert 7 not in [r.number for r in S.due_rows(issues, {6: [{"number": 61, "state": "CLOSED",
                                                                 "updatedAt": _iso(3)}]}, NOW)]


def test_sweep_gateway_reads_the_parent_assignee_and_comment_fields_the_groups_need():
    issues, sub = _sweep_fixture()
    runner = FakeRunner(json.dumps(issues), sub)
    S.GhSweep("o/r", runner).load()
    fields = runner.calls[0][runner.calls[0].index("--json") + 1].split(",")
    assert {"parent", "assignees", "comments", "labels", "state"} <= set(fields)
