"""tools/agentops/dispatch.py (#229): a session started (or adopted) with an issue offered gets no
wake about that issue in the first `WAKE_GRACE_SECONDS` after its own start -- the dispatcher used to
wake a fresh ORCH about the very issues it was just started on, 47-55 s in, before it had time to
claim them. Numbers 1-6 of #229's "Numbers the tests must reproduce", through the same fake
`ClaudeCli` / `GhCli` the other dispatch tests use."""

from __future__ import annotations

import json
import time
from pathlib import Path

from tools.agentops.dispatch import TICK_SECONDS, WAKE_GRACE_SECONDS

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, _dispatcher, _issue
from tests._area_rule import _no_area_rule  # noqa: F401


# -- number 1 ---------------------------------------------------------------------------------

def test_1_no_wake_about_offered_issues_within_the_grace(tmp_path: Path):
    """A session started with #A and #B offered, both unclaimed, ticks every 30 s up to 3 min: 0
    wakes."""

    clock = FakeClock()
    claude = FakeClaude()
    gh = FakeGh(candidates=[_issue(1, 1), _issue(2, 2)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)

    d.tick()  # starts S1 offered [1, 2]
    assert claude.started[0][0] == [1, 2]

    ticks = int(WAKE_GRACE_SECONDS // TICK_SECONDS)
    for _ in range(ticks):
        clock.advance(TICK_SECONDS)
        d.tick()

    assert claude.waked == []


# -- number 2 ---------------------------------------------------------------------------------

def test_2_first_wake_past_the_grace_names_the_still_unclaimed_offered_issue(tmp_path: Path):
    """The first tick past 3 min, #A still unclaimed: 1 wake naming #A, its wake count 1."""

    clock = FakeClock()
    claude = FakeClaude()
    gh = FakeGh(candidates=[_issue(1, 1)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)

    d.tick()  # starts S1 offered [1]
    live_id = claude.list_sessions()[0]["id"]

    clock.advance(WAKE_GRACE_SECONDS)
    d.tick()  # still inside the grace at exactly WAKE_GRACE_SECONDS: no wake yet
    assert claude.waked == []

    clock.advance(TICK_SECONDS)
    report = d.tick()  # the first tick strictly past the grace

    assert report.woke == (1,)
    cur = next(c for c in d.sessions if c.session == live_id)
    assert cur.wake_counts[1] == 1


# -- number 3 ---------------------------------------------------------------------------------

def test_3_a_non_offered_issue_is_woken_as_today(tmp_path: Path):
    """#C labelled 1 min after the start, not offered: 1 wake at the next tick (as today)."""

    clock = FakeClock()
    claude = FakeClaude()
    gh = FakeGh(candidates=[_issue(1, 1)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)

    d.tick()  # starts S1 offered [1]
    gh.claim(1)
    clock.advance(60.0)
    d.tick()  # refresh: S1 holds {1}

    gh._candidates = [_issue(3, 3)]  # #C becomes ready, never offered to S1
    report = d.tick()

    assert report.woke == (3,)


# -- number 4 ---------------------------------------------------------------------------------

def test_4_claiming_one_offered_issue_leaves_the_other_woken_only_past_the_grace(tmp_path: Path):
    """The session claims #A at 2 min and leaves #B: 0 wakes before 3 min, 1 wake about #B at the
    first tick past it, 0 about #A."""

    clock = FakeClock()
    claude = FakeClaude()
    gh = FakeGh(candidates=[_issue(1, 1), _issue(2, 2)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)

    d.tick()  # starts S1 offered [1, 2]

    clock.advance(120.0)
    gh.claim(1)
    d.tick()  # S1 holds {1}; still inside the grace (2 min < 3 min)
    assert claude.waked == []

    clock.advance(WAKE_GRACE_SECONDS - 120.0)
    d.tick()  # exactly at the grace boundary: still no wake
    assert claude.waked == []

    clock.advance(TICK_SECONDS)
    report = d.tick()  # the first tick past 3 min from the start

    assert report.woke == (2,)


# -- number 5 ---------------------------------------------------------------------------------

def test_5_replay_of_the_measured_starts_zero_wakes_about_an_offered_issue(tmp_path: Path):
    """The replay of #229's table: a session offered one or two issues, ticked every 30 s, each
    claiming its issue(s) at the measured time (47-55 s): 0 wakes about an offered issue."""

    starts = [
        ((169,), 48.0),
        ((200, 201), 55.0),
        ((185,), 50.0),
        ((171, 186), 47.0),
        ((213,), 50.0),
    ]
    woke_about_offered = 0
    for offered, claim_at in starts:
        clock = FakeClock()
        claude = FakeClaude()
        gh = FakeGh(candidates=[_issue(n, n) for n in offered])
        d = _dispatcher(tmp_path / f"case-{offered[0]}", claude=claude, gh=gh, clock=clock)

        d.tick()  # starts, offered `offered`
        elapsed = 0.0
        while elapsed < claim_at:
            clock.advance(TICK_SECONDS)
            elapsed += TICK_SECONDS
            report = d.tick()
            woke_about_offered += sum(1 for n in report.woke if n in offered)
        for n in offered:
            gh.claim(n)
        d.tick()

    assert woke_about_offered == 0


# -- number 6 ---------------------------------------------------------------------------------

def test_6_a_restarted_dispatcher_adopts_and_keeps_the_grace_from_the_original_start(tmp_path: Path):
    """A dispatcher restarted 1 min after a session's start adopts it: 0 wakes about its offered
    issues until 3 min from the session's start (not from the adoption)."""

    state_file = tmp_path / "orch_session.json"
    events_file = tmp_path / "events.jsonl"

    clock1 = FakeClock()
    claude = FakeClaude()
    gh = FakeGh(candidates=[_issue(1, 1)])
    d1 = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock1)
    d1.tick()  # starts S1 offered [1], writes started_at_wall to the state file

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["offered"] == [1]
    assert "started_at_wall" in state

    # simulate a restart 1 min later: a fresh dispatcher, its own monotonic clock at 0, but the
    # same claude/gh/state/events files -- it adopts the live session instead of starting one.
    live = claude.list_sessions()[0]
    live["startedAt"] = int(time.time() * 1000) - 60_000  # the live listing also says ~1 min old

    clock2 = FakeClock()
    d2 = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock2)
    d2.state_file = state_file
    d2.events_file = events_file
    d2.tick()  # adopts S1, restoring offered + the original start time

    assert len(d2.sessions) == 1
    cur = d2.sessions[0]
    assert cur.offered == (1,)

    for _ in range(3):  # ~1.5 min more of ticks in the new process's clock
        clock2.advance(TICK_SECONDS)
        d2.tick()

    assert claude.waked == []  # still short of 3 min since the ORIGINAL start


def test_8_core_check_is_clean():
    """Number 8: `python -m tools.agentops core check` counts 0 / 0 / 0 (L1/L2/L3) for
    `dispatch.py`, the file this issue touches."""

    from tools.agentops.core_check import check_file, read_patterns, DEFAULT_ROOT, PATTERNS_PATH

    patterns = read_patterns(PATTERNS_PATH)
    row = check_file(DEFAULT_ROOT / "tools/agentops/dispatch.py", "tools/agentops/dispatch.py", patterns)
    assert (row.counts.get("L1", 0), row.counts.get("L2", 0), row.counts.get("L3", 0)) == (0, 0, 0)
