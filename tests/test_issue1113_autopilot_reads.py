"""#1113 unit 2: the autopilot's comment reads. A counting fake `gh` (the runner seam `GhCli` already goes
through) counts the `issue view N --json comments` calls. Rows: M dispatcher ticks inside the 90 s window
over K waiting goals make K comment reads, not M x K (the terms read and the waiting check share one read
per goal); a fresh-read caller (the end-of-session snapshot, a checkpoint read) makes its call every time;
a new comment is seen one window later at most; a failed read is retried on the next poll, never memoized.
No real `gh`, no real clock, no PoB."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tools.agentops import autopilot  # noqa: F401  (registers the autopilot methods on GhCli and Dispatcher)
from tools.agentops.autopilot import GhReadFailed, _AutopilotWaiting
from tools.agentops.dispatch import GhCli, area_labels

from tests.test_agentops_dispatch_autopilot import FakeClaude, FakeClock, _dispatcher

REPO = "o/r"
AREA = (area_labels() or ["area:builds"])[0]
WINDOW_TICKS = (0, 30, 60)  # M = 3 polls, all inside the 90 s memo window


class CountingGh:
    """A fake `gh` at the runner seam: records every call. `issue list --label autopilot` answers the
    open goals; `issue view N --json comments` answers `comments[N]` (or rc 1 for the next `view_fail`
    views); every other read answers an empty list; `api` (the native child walk) fails, so the child
    read is "unknown" and never wakes a goal."""

    def __init__(self, goals=(), comments=None) -> None:
        self.calls: list[list[str]] = []
        self.goals = list(goals)
        self.comments: dict[int, list[dict]] = {n: list(c) for n, c in (comments or {}).items()}
        self.view_fail = 0

    def __call__(self, args, stdin=None):
        self.calls.append(list(args))
        if args[:2] == ["issue", "view"]:
            if self.view_fail > 0:
                self.view_fail -= 1
                return subprocess.CompletedProcess(args, 1, "", "HTTP 504: Gateway Timeout")
            body = {"comments": self.comments.get(int(args[2]), [])}
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
        if args[:2] == ["issue", "list"] and "--label" in args and "autopilot" in args:
            body = [{"number": n, "labels": [{"name": "autopilot"}, {"name": AREA}]} for n in self.goals]
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
        if args[:2] == ["issue", "list"]:
            return subprocess.CompletedProcess(args, 0, "[]", "")
        return subprocess.CompletedProcess(args, 1, "", "api is not part of this test")

    def views(self, number: int | None = None) -> int:
        return sum(1 for a in self.calls
                   if a[:2] == ["issue", "view"] and (number is None or int(a[2]) == number))


def _dispatch(tmp_path: Path, gh_fake: CountingGh, clock: FakeClock):
    gh = GhCli(runner=gh_fake, clock=clock)
    d = _dispatcher(tmp_path, FakeClaude(), gh, clock=clock, log=lambda m: None)
    d._waits_loaded = True  # no waits file in this test: the waits below are set by hand
    return d


def _waiting(goal: int) -> _AutopilotWaiting:
    return _AutopilotWaiting(goal=goal, since=0.0, comments_seen=frozenset(), children={})


def _tick(d, clock: FakeClock, t: float) -> None:
    clock.value = t
    d.gh.memo.start_tick()  # what Dispatcher.tick does before the autopilot step
    d._tick_autopilot()


def test_m_polls_inside_the_window_over_k_waiting_goals_read_each_goal_once(tmp_path: Path):
    goals = [101, 102, 103]  # K = 3 waiting goals
    gh_fake = CountingGh(goals=goals)
    clock = FakeClock()
    d = _dispatch(tmp_path, gh_fake, clock)
    for goal in goals:
        d.autopilot_waiting[goal] = _waiting(goal)
    for t in WINDOW_TICKS:
        _tick(d, clock, t)
    assert gh_fake.views() == len(goals)  # K reads, not M x K: the terms read and the waiting check, 2 x K x M = 18 before
    for goal in goals:
        assert gh_fake.views(goal) == 1
        assert goal in d.autopilot_waiting  # nothing new in the comments: still waiting


def test_a_new_comment_wakes_the_goal_at_most_one_window_later(tmp_path: Path):
    gh_fake = CountingGh(goals=[101])
    clock = FakeClock()
    d = _dispatch(tmp_path, gh_fake, clock)
    d.autopilot_waiting[101] = _waiting(101)
    _tick(d, clock, 0)
    gh_fake.comments[101] = [{"id": "c1", "body": "ORCH progress on a child"}]
    _tick(d, clock, 60)  # still inside the window: the memo answers, the goal keeps waiting
    assert 101 in d.autopilot_waiting
    _tick(d, clock, 90)  # the window is over: a fresh read sees the comment and wakes the goal
    assert 101 not in d.autopilot_waiting
    assert 101 in d.autopilots


def test_fresh_reads_make_their_call_every_time(tmp_path: Path):
    gh_fake = CountingGh()
    clock = FakeClock()
    gh = GhCli(runner=gh_fake, clock=clock)
    gh.memo.start_tick()
    gh.comments(7, fresh=True)  # the end-of-session snapshot (#146)
    gh.comments(7, fresh=True)
    gh.latest_checkpoint_comment(7)  # a checkpoint baseline or check (#116)
    gh.latest_checkpoint_comment(7)
    assert gh_fake.views(7) == 4


def test_a_failed_read_is_retried_on_the_next_poll(tmp_path: Path):
    gh_fake = CountingGh()
    clock = FakeClock()
    gh = GhCli(runner=gh_fake, clock=clock)
    gh.memo.start_tick()
    gh_fake.view_fail = 1
    assert gh.comments(7) is None  # rc 1: unknown, not an empty list
    assert gh.comments(7) == []  # the same window: retried, not served from a memo
    assert gh_fake.views(7) == 2

    gh_fake.view_fail = 1
    with pytest.raises(GhReadFailed):
        gh.latest_terms_comment(8)
    assert gh.latest_terms_comment(8) is None
    assert gh_fake.views(8) == 2
