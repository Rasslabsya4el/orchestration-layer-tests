"""Issue 355 unit 2: `tools.agentops rework`, over a fixture of issues and a fake gh. No network."""

from __future__ import annotations

import json
from pathlib import Path

from tools.agentops import rework

REPO = "o/r"

ISSUES = [
    {"number": 1, "title": "Runs: add a thing", "body": "", "createdAt": "2026-09-25T10:00:00Z",
     "closedAt": "2026-09-27T10:00:00Z", "state": "CLOSED"},
    {"number": 2, "title": "Runs: add another", "body": "", "createdAt": "2026-09-26T10:00:00Z",
     "closedAt": "2026-09-28T10:00:00Z", "state": "CLOSED"},
    {"number": 3, "title": "core: broke: dispatch log", "body": "**caused by:** #1\n**caught by:** suite",
     "createdAt": "2026-09-27T10:00:00Z", "closedAt": "2026-09-29T10:00:00Z", "state": "CLOSED"},
    {"number": 4, "title": "core: looks off: a table", "body": "Caused by: #2\ncaught by: Owner",
     "createdAt": "2026-09-28T10:00:00Z", "closedAt": None, "state": "OPEN"},
    {"number": 5, "title": "Fix: the runner", "body": "caught by: brain-review\ncaused by: unknown",
     "createdAt": "2026-09-28T11:00:00Z", "closedAt": "2026-10-01T10:00:00Z", "state": "CLOSED"},
    {"number": 6, "title": "Fix the other runner", "body": "caught by: nonsense",
     "createdAt": "2026-09-29T11:00:00Z", "closedAt": "2026-09-29T12:00:00Z", "state": "CLOSED"},
    {"number": 7, "title": "Prefix: not a fix", "body": "", "createdAt": "2026-09-29T11:00:00Z",
     "closedAt": None, "state": "OPEN"},
    {"number": 8, "title": "core: broke: too late", "body": "caught by: later-run",
     "createdAt": "2026-10-20T11:00:00Z", "closedAt": None, "state": "OPEN"},
]
EVENTS = [{"n": 1, "at": "2026-09-28T00:00:00Z"}, {"n": 1, "at": "2026-09-29T00:00:00Z"},
          {"n": 2, "at": "2026-09-01T00:00:00Z"}]


class FakeGh:
    def __init__(self):
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["issue", "list"]:
            return json.dumps(ISSUES)
        if args[0] == "api" and "issues/events" in args[1]:
            return "\n".join(json.dumps(e) for e in EVENTS)
        raise AssertionError(f"unexpected gh call: {args}")


def _run(tmp_path, since="2026-09-26T00:00:00Z", until="2026-10-10T00:00:00Z", turns=(), prices=None):
    gh = FakeGh()
    out = rework.run_rework(since, until, repo=REPO, runner=gh, root=tmp_path, turns=list(turns),
                            prices=prices if prices is not None else {})
    return out, gh


def test_exact_counts(tmp_path):
    out, gh = _run(tmp_path)
    lines = out.splitlines()
    assert "closed: 5" in lines  # #1, #2, #3, #5, #6
    assert "reopened: 1 (#1)" in lines
    assert "broke: 1 (#3)" in lines  # #8 is created after the window
    assert "looks off: 1 (#4)" in lines
    assert "Fix: 2 (#5, #6)" in lines
    assert "rework tickets: 4; rate: 80.0 % of closed" in lines
    assert len(gh.calls) == 2  # one list, one events call


def test_caused_by_known_and_caught_by_split(tmp_path):
    out, _ = _run(tmp_path)
    assert "caused-by known: 2 of 4" in out  # #3 bold, #4 plain and case-insensitive; #5 is `unknown`
    assert "caught by: suite 1, orch-diff 0, brain-review 1, owner 1, later-run 0, unknown 1" in out


def test_tokens_not_measured_when_the_meter_has_nothing(tmp_path):
    out, _ = _run(tmp_path, turns=[])
    assert out.splitlines()[-1].startswith("tokens: not measured (")


def test_tokens_not_measured_without_a_closed_rework_ticket(tmp_path):
    out, _ = _run(tmp_path, since="2026-09-28T10:30:00Z", until="2026-09-28T10:45:00Z")
    assert out.splitlines()[-1] == "tokens: not measured (no closed rework ticket)"


def test_title_rule():
    assert rework.title_kinds("x: broke: y") == ["broke"]
    assert rework.title_kinds("x: looks off: y") == ["looks off"]
    assert rework.title_kinds("Fix: y") == ["Fix"]
    assert rework.title_kinds("Fix y") == ["Fix"]
    assert rework.title_kinds("Prefix: y") == []
