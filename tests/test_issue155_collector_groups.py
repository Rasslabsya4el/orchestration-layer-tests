"""Issue #155 (unit 2 of 3): the collector's «По тикетам» group and `sweep list` decide each ticket with the same
function (`tools.agentops.ticket_groups`), so their group counts over one fixture of open issues are equal.

Acceptance row 1: one fixture of open issues, read (a) through `collect.tickets()` with a fake `gh api graphql` read
and (b) through `sweep list` (`GhSweep` over a fake `gh issue list`). No network; the cache lives in tmp_path.
Seams under test (as the spec names them): `collect.tickets()` (its `groups` and each issue's `group`) and
`sweep.run_sweep` (the `<group> <count>` lines after the `rows` line).

    python -m pytest -q -p no:faulthandler tests/test_issue155_collector_groups.py
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BIN = next((ROOT / "mods").glob("*/bin/collect.py")).parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BIN))

import collect  # noqa: E402
from tools.agentops import sweep as S  # noqa: E402
from tools.agentops import ticket_groups as G  # noqa: E402

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
UPDATED = "2026-10-14T12:00:00Z"
MARK = "<!-- parked-on-run: 2026-10-14 -->"

# ONE fixture of open issues, neutral: `n` number, `labels`, `comments` (bodies), `blockers` ((number, state)),
# `parent` (number), `assignees` (logins). Read in two shapes below: the GraphQL node of the collector, the gh JSON of sweep.
FIXTURE = [
    {"n": 1, "labels": ["ready-for-agent", "parked"]},             # parked (first, whatever its other labels)
    {"n": 2, "comments": [MARK + "\nwaiting on the run"]},         # no label, parked on a run
    {"n": 3},                                                      # no label
    {"n": 4, "labels": ["needs-triage"]},                          # triage
    {"n": 5, "blockers": [(90, "OPEN")]},                          # blocked on its own open blocker
    {"n": 6, "labels": ["wayfinder:map"]},                         # folder (map label)
    {"n": 7, "labels": ["ready-for-agent"]},                       # ready alone
    {"n": 8, "parent": 6},                                         # child of the map: no label
]
#: the groups by hand, from kit.tsx progressLines (:1202-1224), first fit in order
EXPECTED_GROUPS = {1: "parked", 2: "parked-on-run", 3: "no label", 4: "needs-triage", 5: "blocked",
                   6: "folder", 7: "ready-for-agent", 8: "no label"}
EXPECTED_COUNTS = {"needs-triage": 1, "ready-for-agent": 1, "parked": 1, "folder": 1, "blocked": 1,
                   "parked-on-run": 1, "no label": 2}
MAP_CHILDREN = json.dumps([{"number": 8, "state": "open", "updated_at": UPDATED}])


def _graphql_node(spec: dict) -> dict:
    """The node `_issues_query(\"[OPEN]\", True)` returns for one issue (collect.py :953-972)."""
    return {
        "number": spec["n"], "title": f"title {spec['n']}", "state": "OPEN", "stateReason": None,
        "updatedAt": UPDATED, "createdAt": UPDATED, "author": {"login": "dev"},
        "labels": {"nodes": [{"name": x} for x in spec.get("labels", [])]},
        "parent": {"number": spec["parent"]} if spec.get("parent") else None,
        "blockedBy": {"nodes": [{"number": b, "state": s} for b, s in spec.get("blockers", [])]},
        "comments": {"nodes": [{"body": b, "createdAt": UPDATED} for b in spec.get("comments", [])]},
        "assignees": {"totalCount": len(spec.get("assignees", []))},
    }


def _gh_issue(spec: dict) -> dict:
    """The `gh issue list --json` record of one issue (sweep.ISSUE_FIELDS)."""
    blockers = spec.get("blockers", [])
    return {
        "number": spec["n"], "title": f"title {spec['n']}", "state": "OPEN",
        "labels": [{"name": x} for x in spec.get("labels", [])], "updatedAt": UPDATED,
        "blockedBy": {"nodes": [{"number": b, "state": s} for b, s in blockers], "totalCount": len(blockers)},
        "subIssuesSummary": {"total": 0, "completed": 0},
        "parent": {"number": spec["parent"]} if spec.get("parent") else None,
        "assignees": [{"login": a} for a in spec.get("assignees", [])],
        "comments": [{"body": b} for b in spec.get("comments", [])],
    }


class GraphqlGh:
    """`subprocess.run` stand-in: the open read answers the fixture, the closed read is empty, any other gh call fails."""

    def __init__(self, nodes: list[dict]) -> None:
        self.nodes = nodes

    def __call__(self, cmd, *args, **kwargs):
        if cmd[:3] != ["gh", "api", "graphql"]:
            return subprocess.CompletedProcess(cmd, 1, "", "not in this test")
        query = next(p for p in cmd if p.startswith("query="))
        nodes = [] if "states: [CLOSED]" in query else self.nodes
        page = {"data": {"repository": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                                   "nodes": nodes}}}}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(page), "")


class SweepGh:
    """The `gh` runner of `GhSweep`: `issue list` answers the fixture, the map's `sub_issues` answer MAP_CHILDREN."""

    def __init__(self, issues: list[dict]) -> None:
        self.issues = issues

    def __call__(self, args):
        from types import SimpleNamespace
        out = json.dumps(self.issues) if args[:2] == ["issue", "list"] else MAP_CHILDREN
        return SimpleNamespace(returncode=0, stdout=out, stderr="")


def _timeline_unanswered(args, *a, **k):
    """The pipeline's first-`ready-for-agent` time reads a ticket's timeline through `gh_call.run_gh`; answered as a
    failure (no time, nothing cached), so no test asks GitHub for a timeline."""
    return subprocess.CompletedProcess(args, 1, "", "timeline not read in this test")


@pytest.fixture
def collector(tmp_path, monkeypatch) -> None:
    cache = tmp_path / ".cache"
    monkeypatch.setattr(collect, "CACHE_DIR", cache)  # #182: the pipeline files follow CACHE_DIR (fast_path's folder)
    monkeypatch.setattr(collect.subprocess, "run", GraphqlGh([_graphql_node(s) for s in FIXTURE]))
    monkeypatch.setattr("tools.agentops.gh_call.run_gh", _timeline_unanswered)


def _sweep_count_lines(issues: list[dict]) -> tuple[dict[str, int], list[str], dict[int, str]]:
    """Run `sweep list` over the fixture; return (count lines as {group: count} in printed order, the group
    names in printed order, each row's group)."""
    gh = S.GhSweep("o/r", SweepGh(issues))
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = S.run_sweep(argparse.Namespace(sweep_command="list"), gh=gh, now=NOW)
    assert code == 0
    lines = buf.getvalue().splitlines()
    rows_at = next(i for i, line in enumerate(lines) if line.startswith("rows "))
    counts = {}
    for line in lines[rows_at + 1:]:
        group, count = line.rsplit(" ", 1)
        counts[group] = int(count)
    row_groups = {r.number: r.group for r in S.build_rows(issues, {}, NOW)}
    return counts, list(counts), row_groups


def test_collector_and_sweep_list_count_each_group_the_same_over_one_fixture(collector):
    out = collect.tickets()
    collector_counts = out["groups"]
    collector_issue_groups = {int(k): v["group"] for k, v in out["issues"].items() if v["open"]}

    sweep_counts, sweep_order, sweep_rows = _sweep_count_lines([_gh_issue(s) for s in FIXTURE])

    assert collector_counts == sweep_counts                      # acceptance row 1: equal per group
    assert list(collector_counts) == sweep_order                 # and in the monitor's order
    assert collector_counts == EXPECTED_COUNTS                   # both equal the hand count
    assert collector_issue_groups == EXPECTED_GROUPS             # each issue, decided by the same function
    assert sweep_rows == EXPECTED_GROUPS


def test_collector_issue_facts_still_carry_its_own_parked_flags(collector):
    out = collect.tickets()
    assert out["issues"]["2"]["parked_on_run"] is True           # the collector's flag, unchanged
    assert out["issues"]["1"]["parked"] is True
    assert out["issues"]["1"]["group"] == "parked"


def test_collector_groups_are_the_monitor_order_and_skip_closed(collector):
    out = collect.tickets()
    assert list(out["groups"]) == [g for g in G.GROUPS if g in out["groups"]]
    assert out["groups"]["needs-triage"] == 1 and out["groups"]["no label"] == 2
