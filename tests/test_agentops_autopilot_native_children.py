"""#318 unit 2: `child_issues` walks GitHub native sub-issues at any depth."""

from __future__ import annotations

import json
import subprocess

from tools.agentops.autopilot import _GhAutopilot
from tools.agentops.dispatch import GhCli


def _walk(gh):
    return _GhAutopilot.child_issues(gh, 100)


def _sub(number: int, state: str = "open") -> dict:
    return {"number": number, "state": state, "labels": [{"name": "x"}],
            "updated_at": "2026-09-29T00:00:00Z", "comments": 0}


def _gh(tree: dict[int, list[dict]], fail_on: int | None = None, calls: list | None = None) -> GhCli:
    def runner(args, stdin=None):
        if calls is not None:
            calls.append(args)
        assert args[0] == "api" and "sub_issues" in args[1], args
        parent = int(args[1].split("/issues/")[1].split("/")[0])
        if parent == fail_on:
            return subprocess.CompletedProcess(args, 1, "", "boom")
        return subprocess.CompletedProcess(args, 0, json.dumps([tree.get(parent, [])]), "")

    return GhCli(repo="o/r", runner=runner)


def test_three_tickets_at_two_depths_no_part_of_line():
    tree = {100: [_sub(1), _sub(2, "closed")], 1: [_sub(3)]}
    calls: list = []
    found = _walk(_gh(tree, calls=calls))
    assert sorted(c["number"] for c in found) == [1, 2, 3]
    assert {c["number"]: c["state"] for c in found} == {1: "OPEN", 2: "CLOSED", 3: "OPEN"}
    assert found[0]["labels"] == [{"name": "x"}] and found[0]["updatedAt"]
    assert len(calls) == 4  # goal + 3 descendants


def test_failing_call_anywhere_is_none():
    tree = {100: [_sub(1), _sub(2)], 1: [_sub(3)]}
    assert _walk(_gh(tree, fail_on=1)) is None
    assert _walk(_gh(tree, fail_on=100)) is None


def test_empty_goal_is_empty_list_and_cycle_terminates():
    assert _walk(_gh({})) == []
    cyc = {100: [_sub(1)], 1: [_sub(2)], 2: [_sub(1), _sub(100)]}
    assert sorted(c["number"] for c in _walk(_gh(cyc))) == [1, 2]


def test_waiting_check_reads_children_at_most_every_five_minutes():
    from types import SimpleNamespace

    from tools.agentops.autopilot import _AutopilotWaiting, _DispatcherAutopilot, _child_signature

    class FakeGh:
        def __init__(self):
            self.walks = 0
            self.children = [{"number": 1, "state": "OPEN", "labels": []}]

        def comments(self, goal, *, fresh=False):  # #1113: the memo flag of GhCli.comments
            return []

        def child_issues(self, goal):
            self.walks += 1
            return list(self.children)

    now = [1000.0]
    gh = FakeGh()
    disp = SimpleNamespace(gh=gh, clock=lambda: now[0])
    waiting = _AutopilotWaiting(goal=100, since=now[0], children=_child_signature(gh.children))

    def over():
        return _DispatcherAutopilot._waiting_is_over(disp, waiting)

    assert over() is False
    now[0] += 30
    assert over() is False
    assert gh.walks == 1
    gh.children = [{"number": 1, "state": "CLOSED", "labels": []}]
    now[0] += 30
    assert over() is False and gh.walks == 1  # the change is not read yet
    now[0] += 300
    assert over() is True and gh.walks == 2  # seen at the second walk
