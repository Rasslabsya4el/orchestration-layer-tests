"""#922: a failed `gh` read is "unknown" for the autopilot start rule -- never "no checkpoint / no wait"
and never "a change". Seen 2026-10-12 14:18-14:31 on #780 (HTTP 504 / 403 rate limit): a start with
`(first session)` 6 min after an idle reset, then a start with `(change: comment on #780)` 46 s after
the next reset. The fakes here run the REAL `GhCli` read code (`_GhAutopilot`) over a failing `_run`."""

from __future__ import annotations

import subprocess

from tools.agentops.autopilot import (
    AUTOPILOT_CHECKPOINT_PREFIX,
    AUTOPILOT_IDLE_BACKSTOP_MINUTES,
    _GhAutopilot,
)
from tools.agentops.dispatch import TICK_SECONDS
from tests.test_agentops_dispatch_autopilot import (
    FakeClaude,
    FakeClock,
    FakeGh,
    _InPlaceGh,
    _dispatcher,
    _started_lines,
)

BACKSTOP = AUTOPILOT_IDLE_BACKSTOP_MINUTES * 60.0


class _RealReadsGh(_InPlaceGh):
    """`FakeGh` whose goal list and terms read run the real code over a `_run` that fails on demand
    (rc 1, a 504 on stderr), like the log of 2026-10-12."""

    repo = "o/r"

    def __init__(self) -> None:
        super().__init__()
        self.list_fails = False
        self.terms_fail = False

    def _run(self, args, stdin=None):
        if self.list_fails and args[:2] == ["issue", "list"]:
            return subprocess.CompletedProcess(args, 1, "", "HTTP 403: secondary rate limit")
        if self.terms_fail and args[:2] == ["issue", "view"]:
            return subprocess.CompletedProcess(args, 1, "", "HTTP 504: Gateway Timeout")
        raise AssertionError(f"unexpected gh call {args}")

    def _read(self, args, ttl):
        """#1113: the real `GhCli` reads through its memo; this double has no memo, so it reads straight."""
        return self._run(args)

    def autopilot_goals(self):
        if self.list_fails:
            return _GhAutopilot.autopilot_goals(self)
        return super().autopilot_goals()

    def latest_terms_comment(self, number):
        if self.terms_fail:
            return _GhAutopilot.latest_terms_comment(self, number)
        return super().latest_terms_comment(number)


def _idle_goal(tmp_path, goal: int, gh=None, before_end=lambda gh: None):
    """One goal started, then ended `idle`: it is waiting (backstop 15 min)."""

    clock = FakeClock()
    claude = FakeClaude()
    gh = gh or _RealReadsGh()
    gh.autopilot_goal(goal)
    logs: list[str] = []
    d = _dispatcher(tmp_path, claude, gh, clock=clock, log=logs.append)
    d.tick()
    before_end(gh)
    claude.set_state(d.autopilots[goal].session, state="working", status="idle")
    d.tick()
    assert goal not in d.autopilots and goal in d.autopilot_waiting
    return claude, gh, d, clock, logs


def _step(d, clock, seconds: float = TICK_SECONDS) -> None:
    clock.advance(seconds)
    d.tick()


# -- row 2: a failed read makes the pass skip the goal; the next good read behaves as before ----------

def test_922_failed_goal_list_keeps_the_wait_and_starts_nothing(tmp_path):
    claude, gh, d, clock, logs = _idle_goal(tmp_path, 780)
    gh.list_fails = True
    for _ in range(3):
        _step(d, clock)
    assert 780 in d.autopilot_waiting  # the 14:23:33 rate limit dropped it before the fix
    gh.list_fails = False
    _step(d, clock)
    assert len(claude.autopilot_started) == 1 and not _started_lines(logs, 780)[1:]
    _step(d, clock, BACKSTOP)  # the backstop is what ends the wait, as before
    assert len(claude.autopilot_started) == 2
    assert _started_lines(logs, 780)[-1].endswith("(backstop 15 min)")


def test_922_failed_terms_read_logs_no_first_session_and_starts_nothing(tmp_path):
    claude, gh = FakeClaude(), _RealReadsGh()
    gh.autopilot_goal(781)
    gh.terms_fail = True
    logs: list[str] = []
    d = _dispatcher(tmp_path, claude, gh, log=logs.append)
    for _ in range(3):
        d.tick()
    assert claude.autopilot_started == []
    assert not any("(first session)" in m for m in logs)
    gh.terms_fail = False
    d.tick()  # the next good read behaves as before: one first session
    assert len(claude.autopilot_started) == 1
    assert _started_lines(logs, 781)[-1].endswith("(first session)")


def test_922_failed_terms_read_while_waiting_does_not_end_the_wait(tmp_path):
    claude, gh, d, clock, logs = _idle_goal(tmp_path, 782)
    gh.terms_fail = True
    _step(d, clock, BACKSTOP)  # past the backstop, but the terms are unknown: skip, try next tick
    assert len(claude.autopilot_started) == 1
    gh.terms_fail = False
    _step(d, clock)
    assert len(claude.autopilot_started) == 2
    assert _started_lines(logs, 782)[-1].endswith("(backstop 15 min)")


def test_922_failed_comment_read_at_idle_end_is_not_a_change_at_the_next_good_read(tmp_path):
    # the 14:30:24 reset: the snapshot read failed, so every older owner comment looked new at 14:31:10
    def old_comments(gh):
        gh.add_goal_comment(783, "an owner comment from yesterday")
        gh.add_goal_comment(783, f"{AUTOPILOT_CHECKPOINT_PREFIX}: step 1")
        gh.comments_fail.add(783)  # consumed by the first `comments` call: the one at the idle end

    claude, gh, d, clock, logs = _idle_goal(tmp_path, 783, before_end=old_comments)
    for _ in range(3):
        _step(d, clock)
    assert len(claude.autopilot_started) == 1
    assert not any("change: comment" in m for m in logs)
    gh.add_goal_comment(783, "a really new owner comment")  # a real change after the baseline still wakes
    _step(d, clock)
    assert len(claude.autopilot_started) == 2
    assert _started_lines(logs, 783)[-1].endswith("(change: comment on #783)")


def test_922_failed_children_read_at_idle_end_is_not_a_change_at_the_next_good_read(tmp_path):
    claude, gh, d, clock, logs = _idle_goal(
        tmp_path, 784, before_end=lambda g: (g.add_child(784, 330), g.children_fail.add(784)))
    for _ in range(3):
        _step(d, clock)
    assert len(claude.autopilot_started) == 1
    gh.close_child(784, 330)
    _step(d, clock, 6 * 60)  # past the 5 min child-read throttle, inside the backstop
    assert len(claude.autopilot_started) == 2
    assert _started_lines(logs, 784)[-1].endswith("(change: #330 closed)")


def test_922_unknown_snapshot_survives_a_dispatcher_restart_file(tmp_path):
    claude, gh, d, clock, logs = _idle_goal(
        tmp_path, 785, before_end=lambda g: g.comments_fail.add(785))
    d.autopilot_waits_file = tmp_path / "waits.json"
    d._save_waits()
    d.autopilot_waiting.clear()
    d._waits_loaded = False
    d._load_waits()
    assert d.autopilot_waiting[785].comments_seen is None


# -- row 3: an edit of the goal's own checkpoint comment (same id, updatedAt moves) is no change ------

def test_922_edit_of_the_checkpoint_after_a_failed_snapshot_is_not_change_comment(tmp_path):
    def before_end(gh):
        gh.add_goal_comment(780, f"{AUTOPILOT_CHECKPOINT_PREFIX}: step 1")  # first: `edit_goal_comment` edits it
        gh.add_goal_comment(780, "an owner comment from yesterday")
        gh.comments_fail.add(780)

    claude, gh, d, clock, logs = _idle_goal(tmp_path, 780, before_end=before_end)
    for minute in range(1, 14):
        gh.edit_goal_comment(780, f"{AUTOPILOT_CHECKPOINT_PREFIX}: edited at minute {minute}")
        _step(d, clock, 60)
        assert len(claude.autopilot_started) == 1, f"started at minute {minute}"
    assert not any("change: comment" in m for m in logs)
    _step(d, clock, BACKSTOP - 13 * 60)
    assert len(claude.autopilot_started) == 2
    assert _started_lines(logs, 780)[-1].endswith("(backstop 15 min)")


def test_922_edit_of_the_checkpoint_with_a_good_snapshot_is_not_change_comment(tmp_path):
    claude, gh, d, clock, logs = _idle_goal(
        tmp_path, 786, before_end=lambda g: g.add_goal_comment(786, f"{AUTOPILOT_CHECKPOINT_PREFIX}: step 1"))
    for minute in range(1, 14):
        gh.edit_goal_comment(786, f"{AUTOPILOT_CHECKPOINT_PREFIX}: edited at minute {minute}")
        _step(d, clock, 60)
    assert len(claude.autopilot_started) == 1
    _step(d, clock, BACKSTOP - 13 * 60)
    assert len(claude.autopilot_started) == 2
    assert _started_lines(logs, 786)[-1].endswith("(backstop 15 min)")
