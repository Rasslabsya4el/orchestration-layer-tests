"""#154 unit 3 (row 5, plus the one-time sweep): the dispatcher takes `ready-for-agent` off a closed issue.

Seams: `Dispatcher.tick()` with the real `GhCli` over a fake runner (the sweep's one list and one
`issue edit` per closed issue), and `Dispatcher._resolve_outcome` through the in-memory `FakeGh`
(a held issue that ends closed). No network, no real `gh`."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tools.agentops.dispatch import CLOSED_LABEL_SWEEP_SECONDS, GhCli, IssueStatus, READY_LABEL

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, _dispatcher, _issue, _start
from tests._area_rule import _no_area_rule  # noqa: F401


def _cp(args: list[str], stdout: str = "", rc: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args, rc, stdout=stdout, stderr=stderr)


class FakeGhRunner:
    """The `runner` seam of `GhCli`: records every call. The closed-`ready-for-agent` list answers
    `closed` (or fails with `list_rc`); every other read answers an empty list, every write rc 0."""

    def __init__(self, closed: tuple[int, ...] = (), list_rc: int = 0) -> None:
        self.closed = list(closed)
        self.list_rc = list_rc
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if args[:2] == ["issue", "list"] and "closed" in args and READY_LABEL in args:
            if self.list_rc:
                return _cp(args, rc=self.list_rc, stderr="gh: HTTP 502")
            return _cp(args, stdout=json.dumps([{"number": n} for n in self.closed]))
        if args[:2] == ["issue", "edit"]:
            return _cp(args)
        return _cp(args, stdout="[]")

    def list_calls(self) -> list[list[str]]:
        return [a for a in self.calls if a[:2] == ["issue", "list"] and "closed" in a and READY_LABEL in a]

    def removals(self) -> list[tuple[str, str]]:
        """(issue number, label) of every `issue edit ... --remove-label` call, in order."""
        out = []
        for a in self.calls:
            if a[:2] == ["issue", "edit"] and "--remove-label" in a:
                out.append((a[2], a[a.index("--remove-label") + 1]))
        return out


def _sweep_dispatcher(tmp_path: Path, runner: FakeGhRunner, clock: FakeClock):
    d = _dispatcher(tmp_path, claude=FakeClaude(), gh=GhCli(runner=runner), clock=clock)
    lines: list[str] = []
    d.log = lines.append
    return d, lines


def test_sweep_removes_ready_for_agent_from_each_closed_issue_that_carries_it(tmp_path: Path):
    runner = FakeGhRunner(closed=(154, 135))
    d, _ = _sweep_dispatcher(tmp_path, runner, FakeClock())

    d.tick()

    assert runner.removals() == [("154", READY_LABEL), ("135", READY_LABEL)]
    lst = runner.list_calls()[0]
    assert lst[lst.index("--limit") + 1] == "1000"


def test_sweep_is_not_before_ten_minutes_and_again_after(tmp_path: Path):
    assert CLOSED_LABEL_SWEEP_SECONDS == 600.0
    runner = FakeGhRunner(closed=(154,))
    clock = FakeClock()
    d, _ = _sweep_dispatcher(tmp_path, runner, clock)

    d.tick()  # t=0: the first sweep, on the first normal-mode tick
    clock.advance(599)
    d.tick()  # t=599: not yet due
    assert len(runner.list_calls()) == 1
    assert runner.removals() == [("154", READY_LABEL)]

    clock.advance(1)
    d.tick()  # t=600: due again
    assert len(runner.list_calls()) == 2
    assert runner.removals() == [("154", READY_LABEL), ("154", READY_LABEL)]


def test_issue_closed_unheld_after_the_first_pass_is_swept_at_the_next_interval(tmp_path: Path):
    runner = FakeGhRunner(closed=())
    clock = FakeClock()
    d, _ = _sweep_dispatcher(tmp_path, runner, clock)

    d.tick()  # t=0: nothing closed carries the label yet
    runner.closed = [200]  # closed later by someone the dispatcher never held
    clock.advance(CLOSED_LABEL_SWEEP_SECONDS)
    d.tick()  # t=600: the next sweep takes the label off it

    assert runner.removals() == [("200", READY_LABEL)]


def test_closed_issue_without_the_label_costs_no_remove_call(tmp_path: Path):
    runner = FakeGhRunner(closed=())  # the closed list is empty: nothing carries the label
    d, _ = _sweep_dispatcher(tmp_path, runner, FakeClock())

    d.tick()

    assert len(runner.list_calls()) == 1
    assert runner.removals() == []


def test_failed_sweep_logs_one_line_and_is_retried_after_the_interval(tmp_path: Path, capsys):
    runner = FakeGhRunner(list_rc=1)
    clock = FakeClock()
    d, _ = _sweep_dispatcher(tmp_path, runner, clock)

    report = d.tick()  # must not raise
    assert report is not None
    assert runner.removals() == []
    err = capsys.readouterr().err
    assert len([ln for ln in err.splitlines() if "gh issue list (closed, ready-for-agent) failed" in ln]) == 1

    clock.advance(599)
    d.tick()  # not before the interval: no retry yet
    assert len(runner.list_calls()) == 1

    runner.list_rc = 0
    runner.closed = [154]
    clock.advance(1)
    report = d.tick()  # t=600: the retry succeeds and the tick goes on
    assert report is not None
    assert len(runner.list_calls()) == 2
    assert runner.removals() == [("154", READY_LABEL)]


def test_held_issue_closed_with_the_label_loses_it_when_its_session_ends(tmp_path: Path):
    claude, gh, d, live_id = _start(tmp_path, [_issue(9)])
    gh.claim(9)
    d.tick()
    gh.issues[9] = IssueStatus(number=9, open=False, labels=(READY_LABEL,), assignees=())
    claude.set_state(live_id, state="done", status="idle")

    d.tick()

    assert gh.removed_labels == [(9, READY_LABEL)]


def test_held_issue_closed_without_the_label_costs_no_remove_call(tmp_path: Path):
    claude, gh, d, live_id = _start(tmp_path, [_issue(9)])
    gh.claim(9)
    d.tick()
    gh.issues[9] = IssueStatus(number=9, open=False, labels=(), assignees=())
    claude.set_state(live_id, state="done", status="idle")

    d.tick()

    assert gh.removed_labels == []
