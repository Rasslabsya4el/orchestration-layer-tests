"""#298: a start `claude --bg` refuses is reported once (log + the brain's chat), quiet while it
repeats, reported over when a start goes through; other tick failures still reach `tick failed`."""

from __future__ import annotations

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, _dispatcher, _issue
from tests.test_agentops_dispatch_autopilot import (
    FakeClaude as APClaude,
    FakeClock as APClock,
    FakeGh as APGh,
    _dispatcher as _ap_dispatcher,
)
from tools.agentops.core_settings import load as load_settings
from tests._area_rule import _no_area_rule  # noqa: F401

TRUST = ("claude --bg failed: Workspace not trusted. Run `claude` in C:\\X once and accept the "
         "trust prompt, then retry.")


class RefusingClaude(FakeClaude):
    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []  # the refusal text per attempt; empty -> `text` every time
        self.refuse_until = 10**9
        self.text = TRUST
        self.attempts = 0

    def start(self, offered):
        self.attempts += 1
        if self.attempts <= self.refuse_until:
            raise RuntimeError(self.texts[self.attempts - 1] if self.texts else self.text)
        return super().start(offered)


def _setup(tmp_path):
    claude, clock = RefusingClaude(), FakeClock()
    gh = FakeGh(candidates=[_issue(1)])
    lines: list[str] = []
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)
    d.log = lines.append
    return d, claude, gh, clock, lines


def _run(d, clock, n):
    d.run(ticks=n, sleep=lambda s: clock.advance(30))


def test_11_refused_ticks_one_line_one_wake_then_over(tmp_path):
    d, claude, gh, clock, lines = _setup(tmp_path)
    claude.refuse_until = 11
    _run(d, clock, 11)
    refused = [l for l in lines if "refused" in l]
    assert len(refused) == 1 and not any("tick failed" in l for l in lines)  # row 1
    assert claude.attempts == 11
    assert len(claude.waked) == 1  # row 2
    target, msg = claude.waked[0]
    assert target == load_settings()["brain_chat_title"]
    assert str(claude.cwd) in msg and TRUST in msg
    clock.advance(30)
    _run(d, clock, 1)  # tick 12: goes through
    assert len(claude.started) == 1  # row 3
    over = [l for l in lines if "refusal is over" in l]
    assert len(over) == 1 and "after 5.5 min (11 ticks)" in over[0], over
    assert d.refused_starts == {}


def test_61_ticks_one_reminder(tmp_path):
    d, claude, gh, clock, lines = _setup(tmp_path)
    _run(d, clock, 61)  # 60 * 30 s = 30 min since the first
    assert len([l for l in lines if "refused (" in l]) == 1
    still = [l for l in lines if "still refused" in l]
    assert len(still) == 1 and "30 min" in still[0], still
    assert len(claude.waked) == 1  # row 4


def test_second_text_is_a_new_refusal(tmp_path):
    d, claude, gh, clock, lines = _setup(tmp_path)
    claude.texts = [TRUST, TRUST, "claude --bg failed: other"]
    claude.refuse_until = 3
    _run(d, clock, 3)
    assert len([l for l in lines if "refused (" in l]) == 2
    assert len(claude.waked) == 2  # row 5


def test_wake_fails_one_line_and_start_still_tried(tmp_path):
    d, claude, gh, clock, lines = _setup(tmp_path)
    claude.wake_fails = True
    _run(d, clock, 5)
    assert len([l for l in lines if "could not tell" in l and "boom" in l]) == 1
    assert len(claude.waked) == 1 and claude.attempts == 5  # row 6


def test_gh_failure_still_tick_failed(tmp_path):
    d, claude, gh, clock, lines = _setup(tmp_path)

    def boom():
        raise RuntimeError("gh issue list failed: x")

    gh.ready_for_agent_issues = boom
    _run(d, clock, 3)
    assert len([l for l in lines if "tick failed" in l and "gh issue list failed" in l]) == 3  # row 7
    assert claude.waked == []


def test_autopilot_non_trust_refusal_one_line_one_wake(tmp_path):
    claude, clock, gh = APClaude(), APClock(), APGh()
    claude.start_autopilot_fails[90] = "claude --bg failed: something else"
    gh.autopilot_goal(90)
    lines: list[str] = []
    d = _ap_dispatcher(tmp_path, claude, gh, clock=clock)
    d.log = lines.append
    for _ in range(3):
        d.tick()
        clock.advance(30)
    assert len([l for l in lines if "autopilot #90 start refused" in l]) == 1
    assert len(claude.wake_calls) == 1
    assert 90 in gh.autopilot_goals()  # not stuck-labelled
