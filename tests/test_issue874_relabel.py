"""#874 unit 1: a dispatcher tick puts `ready-for-agent` back on an open, unassigned ticket whose
newest comment is a `<!-- merge result -->` comment (the park step erased the handback's label).
Driven through the real `GhCli`, `merge_handback.post_merge_result` and `Dispatcher.tick` over a fake
`gh` runner; no real `gh`, no heavy tool."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tools.agentops import merge_handback
from tools.agentops.dispatch import Dispatcher, GhCli, build_command
from tools.agentops.worktree import ROOT

READY = "ready-for-agent"


def _done(rc: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gh"], returncode=rc, stdout=out, stderr=err)


class FakeGh:
    def __init__(self) -> None:
        self.issues: dict[int, dict] = {}
        self.clock = 0

    def stamp(self) -> str:
        self.clock += 1
        return f"2026-10-11T17:{self.clock // 60:02d}:{self.clock % 60:02d}Z"

    def add(self, n: int, *, labels=(), assignees=(), open_=True) -> None:
        self.issues[n] = {"labels": list(labels), "assignees": list(assignees), "open": open_, "comments": []}

    def say(self, n: int, body: str) -> None:
        self.issues[n]["comments"].append({"body": body, "createdAt": self.stamp()})

    def park(self, n: int) -> None:
        """The session's park step: a checkpoint, then label and assignee off."""
        self.say(n, f"ORCH deadbeef, #{n} checkpoint: parked on merge.")
        self.issues[n]["labels"] = [x for x in self.issues[n]["labels"] if x != READY]
        self.issues[n]["assignees"] = []

    def _rec(self, n: int) -> dict:
        i = self.issues[n]
        return {"number": n, "createdAt": "2026-10-01T00:00:00Z", "updatedAt": "2026-10-11T17:00:00Z",
                "state": "OPEN" if i["open"] else "CLOSED",
                "labels": [{"name": x} for x in i["labels"]],
                "assignees": [{"login": x} for x in i["assignees"]],
                "comments": list(i["comments"]), "blockedBy": {"nodes": []}}

    def __call__(self, args, stdin=None):
        if args[:2] == ["issue", "list"]:
            search = args[args.index("--search") + 1] if "--search" in args else ""
            want = args[args.index("--label") + 1] if "--label" in args else None
            out = []
            for n, i in self.issues.items():
                if not i["open"] or (want and want not in i["labels"]):
                    continue
                if "no:assignee" in search and i["assignees"]:
                    continue
                if f"-label:{READY}" in search and READY in i["labels"]:
                    continue
                out.append(self._rec(n))
            return _done(0, json.dumps(out))
        if args[:2] == ["issue", "view"]:
            return _done(0, json.dumps(self._rec(int(args[2]))))
        if args[:2] == ["issue", "comment"]:
            self.say(int(args[2]), stdin or "")
            return _done()
        if args[:2] == ["issue", "edit"]:
            i = self.issues[int(args[2])]
            for k, a in enumerate(args):
                if a == "--add-label" and args[k + 1] not in i["labels"]:
                    i["labels"].append(args[k + 1])
                if a == "--remove-assignee":
                    i["assignees"] = []
            return _done()
        raise AssertionError(f"unexpected gh call {args!r}")


class FakeClaude:
    cwd = ROOT

    def start(self, offered):
        return "sess-1", build_command(offered, "claude.exe")

    def start_autopilot(self, goal):
        """#990: an `autopilot` goal's start is refused here; the relabel pass is what these tests measure."""
        raise RuntimeError("claude --bg failed: refused by the relabel test")

    def list_sessions(self):
        return []

    def wake(self, target, message):
        return True, "{}"

    def stop(self, session_id):
        return True

    def end_process(self, pid):
        return True


def _tick(gh: FakeGh, tmp_path: Path) -> None:
    d = Dispatcher(claude=FakeClaude(), gh=GhCli(runner=gh), events_file=tmp_path / "e.jsonl",
                   state_file=tmp_path / "s.json", projects_dir=tmp_path / "p",
                   clock=lambda: 0.0, log=lambda m: None)
    d.tick()


def _tick_logged(gh: FakeGh, tmp_path: Path) -> list[str]:
    """One tick; returns every line the dispatcher logged."""
    lines: list[str] = []
    d = Dispatcher(claude=FakeClaude(), gh=GhCli(runner=gh), events_file=tmp_path / "e.jsonl",
                   state_file=tmp_path / "s.json", projects_dir=tmp_path / "p",
                   clock=lambda: 0.0, log=lines.append)
    d.tick()
    return lines


def _record(issue: int, *, landed: bool) -> dict:
    if landed:
        return {"kind": "merge", "issues": [issue], "rc": 0, "pushed_sha": "a" * 40}
    return {"kind": "merge", "issues": [issue], "rc": 1, "reason": "conflict"}


def _raced(gh: FakeGh, n: int, *, landed: bool = True, post: bool = True) -> None:
    """Held and labelled when the result arrives (add_label a no-op), parked right after."""
    gh.add(n, labels=(READY,), assignees=("me",))
    if post:
        merge_handback.post_merge_result(_record(n, landed=landed), "r.json", f"orch-{n}", GhCli(runner=gh))
    gh.park(n)


def _comments(gh: FakeGh, n: int) -> int:
    return sum(1 for c in gh.issues[n]["comments"] if merge_handback.MARKER in c["body"])


def test_single_landed(tmp_path):
    gh = FakeGh()
    _raced(gh, 837)
    assert READY not in gh.issues[837]["labels"]  # the race happened
    _tick(gh, tmp_path)
    assert (_comments(gh, 837), gh.issues[837]["labels"].count(READY), len(gh.issues[837]["assignees"])) == (1, 1, 0)


def test_batch_of_five(tmp_path):
    gh = FakeGh()
    for n in range(840, 845):
        _raced(gh, n)
    _tick(gh, tmp_path)
    assert [_comments(gh, n) for n in range(840, 845)] == [1] * 5
    assert [READY in gh.issues[n]["labels"] for n in range(840, 845)] == [True] * 5


def test_conflict(tmp_path):
    gh = FakeGh()
    _raced(gh, 869, landed=False)
    _tick(gh, tmp_path)
    merge = [c["body"] for c in gh.issues[869]["comments"] if merge_handback.MARKER in c["body"]]
    assert len(merge) == 1 and "did not land (conflict)" in merge[0]
    assert gh.issues[869]["labels"].count(READY) == 1


def test_closed_before_result(tmp_path):
    gh = FakeGh()
    gh.add(850, labels=(READY,), assignees=("me",), open_=False)
    gh.park(850)
    gh.say(850, f"{merge_handback.MARKER}\nMerge of orch-850 landed on main abc.\n")
    _tick(gh, tmp_path)
    assert gh.issues[850]["labels"].count(READY) == 0


def test_quoted_marker_in_checkpoint_is_no_result(tmp_path):
    """#990 row 1: an autopilot checkpoint that only quotes the marker mid-line is no merge result:
    no `ready-for-agent`, no "put back" log line."""
    gh = FakeGh()
    gh.add(313)
    gh.say(313, "ORCH deadbeef, #313 checkpoint: the merge result is quoted as `<!-- merge result -->` "
                "in this line; the goal stays open.")
    lines = _tick_logged(gh, tmp_path)
    assert gh.issues[313]["labels"].count(READY) == 0
    assert not any("put back" in line for line in lines)


def test_autopilot_goal_with_marker_first_is_skipped(tmp_path):
    """#990 row 2: a goal carrying the `autopilot` label is skipped whatever its newest comment says."""
    gh = FakeGh()
    gh.add(313, labels=("autopilot",))
    gh.say(313, f"{merge_handback.MARKER}\nMerge of orch-313 landed on main abc.\n")
    _tick(gh, tmp_path)
    assert gh.issues[313]["labels"].count(READY) == 0
