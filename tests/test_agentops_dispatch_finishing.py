"""tools/agentops/dispatch.py (D110, issue #191): a further ORCH session starts only when every
live one is finishing. Numbers 1-9 of #191, reproduced through the same fake `ClaudeCli` / `GhCli`
`tests/test_agentops_dispatch.py` already uses -- no real `claude` or `gh` calls.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.agentops.core_check import run_check
from tools.agentops.dispatch import Dispatcher, TICK_SECONDS, mark_finishing

from tests.test_agentops_dispatch import FakeClaude, FakeGh, FakeClock, _issue, _events
from tests._area_rule import _no_area_rule  # noqa: F401

REPO = Path(__file__).resolve().parents[1]


class FakeTimestamp:
    """A fake wall clock for `Dispatcher(timestamp=...)` (mirrors
    `tests/test_agentops_dispatch_autopilot.py`'s own fixture)."""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


def _dispatcher(tmp_path: Path, *, claude: FakeClaude, gh: FakeGh, clock: FakeClock | None = None,
               timestamp=None, context_line: int = 1_000_000, max_hours: float = 4.0) -> Dispatcher:
    kwargs = {}
    if timestamp is not None:
        kwargs["timestamp"] = timestamp
    return Dispatcher(claude=claude, gh=gh, max_hours=max_hours, events_file=tmp_path / "events.jsonl",
                      state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                      finishing_dir=tmp_path / "finishing", context_line=context_line,
                      orch_offer_max=99,  # test-only: these tests probe finishing and wakes, not the offer cap (#4)
                      clock=clock or FakeClock(), **kwargs)


def _write_transcript(projects_dir: Path, session_uuid: str, context: int) -> None:
    projects_dir.mkdir(parents=True, exist_ok=True)
    path = projects_dir / f"{session_uuid}.jsonl"
    line = {"type": "assistant", "message": {"id": "m1", "model": "claude-opus-5",
                                            "usage": {"input_tokens": context,
                                                      "cache_creation_input_tokens": 0,
                                                      "cache_read_input_tokens": 0,
                                                      "output_tokens": 10}}}
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")


# -- number 1 -------------------------------------------------------------------------------------

def test_1_one_live_not_finishing_one_wake_zero_started(tmp_path: Path):
    claude, gh = FakeClaude(), FakeGh(candidates=[_issue(1, 1)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh)

    d.tick()  # starts S1 offered [1]
    gh.claim(1)
    d.tick()  # refresh: S1 holds {1}

    gh._candidates = [_issue(2, 2)]  # #B appears, unclaimed
    report = d.tick()

    assert report.woke == (2,) and len(claude.started) == 1


# -- number 2 -------------------------------------------------------------------------------------

def test_2_session_says_finishing_zero_wakes_one_started(tmp_path: Path):
    claude, gh = FakeClaude(), FakeGh(candidates=[_issue(1, 1)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh)

    d.tick()
    s1 = claude.list_sessions()[0]["id"]
    gh.claim(1)
    d.tick()

    mark_finishing(s1, finishing_dir=tmp_path / "finishing")
    gh._candidates = [_issue(2, 2)]  # #B is unclaimed
    report = d.tick()

    assert report.woke == () and len(claude.started) == 2
    assert claude.started[-1][0] == [2]


# -- number 3 -------------------------------------------------------------------------------------

def test_3_context_at_line_same_as_saying_so(tmp_path: Path):
    claude, gh = FakeClaude(), FakeGh(candidates=[_issue(1, 1)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh, context_line=1_000)

    d.tick()
    s1 = claude.list_sessions()[0]
    gh.claim(1)
    d.tick()

    _write_transcript(tmp_path / "projects", s1["sessionId"], context=1_000)
    gh._candidates = [_issue(2, 2)]
    report = d.tick()

    assert report.woke == () and len(claude.started) == 2
    assert claude.started[-1][0] == [2]


# -- number 4 -------------------------------------------------------------------------------------

def test_4_finishing_holds_frozen_other_session_untouched(tmp_path: Path):
    claude, gh = FakeClaude(), FakeGh(candidates=[_issue(1, 1)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh)

    d.tick()
    s1 = claude.list_sessions()[0]["id"]
    gh.claim(1)
    d.tick()

    mark_finishing(s1, finishing_dir=tmp_path / "finishing")
    gh._candidates = [_issue(2, 2), _issue(3, 3)]
    d.tick()  # S1 finishing discovered; S2 starts offered [2, 3]
    s2 = next(iter({s["id"] for s in claude.list_sessions()} - {s1}))

    gh.claim(2)
    gh.claim(3)
    d.tick()  # S1's held stays frozen at {1}; the new claims go to S2

    s1_cur = next(c for c in d.sessions if c.session == s1)
    s2_cur = next(c for c in d.sessions if c.session == s2)
    assert s1_cur.held == {1} and s2_cur.held == {2, 3}

    gh.close(1)
    claude.set_state(s1, state="done", status="idle")
    report = d.tick()

    assert report.ended == s1 and report.outcomes == {1: "closed"}
    assert len(claude.started) == 2  # nothing further started
    s2_after = next(c for c in d.sessions if c.session == s2)
    assert s2_after.held == {2, 3}  # untouched


# -- number 5 -------------------------------------------------------------------------------------

def test_5_second_becomes_finishing_while_first_still_lives(tmp_path: Path):
    claude, gh = FakeClaude(), FakeGh(candidates=[_issue(1, 1)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh)

    d.tick()
    s1 = claude.list_sessions()[0]["id"]
    gh.claim(1)
    d.tick()

    mark_finishing(s1, finishing_dir=tmp_path / "finishing")
    gh._candidates = [_issue(2, 2)]
    d.tick()  # S1 finishing; S2 starts offered [2]
    s2 = next(iter({s["id"] for s in claude.list_sessions()} - {s1}))
    gh.claim(2)
    d.tick()  # S2 holds {2}; S1 stays live, still finishing
    assert len(claude.started) == 2 and len(d.sessions) == 2

    mark_finishing(s2, finishing_dir=tmp_path / "finishing")
    gh._candidates = [_issue(3, 3)]  # #D
    report = d.tick()  # every live session (S1, S2) is now finishing -> a third starts

    assert len(claude.started) == 3
    assert claude.started[-1][0] == [3]
    assert len(d.sessions) == 3


# -- number 6 -------------------------------------------------------------------------------------

def test_6_no_eligible_candidate_starts_nothing(tmp_path: Path):
    claude, gh = FakeClaude(), FakeGh(candidates=[_issue(1, 1)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh)

    d.tick()
    s1 = claude.list_sessions()[0]["id"]
    gh.claim(1)
    d.tick()

    mark_finishing(s1, finishing_dir=tmp_path / "finishing")
    gh._candidates = []  # nothing eligible, whatever the session's state
    d.tick()

    assert len(claude.started) == 1


# -- number 7 -------------------------------------------------------------------------------------

def test_7_restart_adopts_every_live_session(tmp_path: Path):
    claude = FakeClaude()
    now_ms = int(time.time() * 1000)
    claude.add_session("s1", name="orch #10", state="working", status="busy", startedAt=now_ms - 120_000,
                       sessionId="uuid-s1")
    claude.add_session("s2", name="orch #20 #21", state="working", status="busy", startedAt=now_ms - 60_000,
                       sessionId="uuid-s2")
    gh = FakeGh(candidates=[])
    gh.claim(10)
    gh.claim(20)
    gh.claim(21)
    d = _dispatcher(tmp_path, claude=claude, gh=gh)

    d.tick()

    assert claude.started == []
    assert {c.session for c in d.sessions} == {"s1", "s2"}
    s1 = next(c for c in d.sessions if c.session == "s1")
    s2 = next(c for c in d.sessions if c.session == "s2")
    assert s1.held == {10} and s2.held == {20, 21}
    assert not s1.finishing and not s2.finishing


# -- number 8 -------------------------------------------------------------------------------------

def test_8_replay_2026_09_27_zero_wakes_after_finishing_further_within_one_tick(tmp_path: Path):
    """The nine issues of #191's table appear one per tick while S1 lives and is not finishing (each
    wakes it once); S1 says it is finishing at 14:20:13 -- from then on 0 wakes, and a further
    session starts within 1 tick of that mark."""

    finish_at = datetime(2026, 9, 27, 14, 20, 13, tzinfo=timezone.utc)
    ts = FakeTimestamp(finish_at - timedelta(minutes=40))
    clock = FakeClock()
    claude, gh = FakeClaude(), FakeGh(candidates=[_issue(1, 1)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock, timestamp=ts)

    d.tick()  # starts S1 offered [1]
    s1 = claude.list_sessions()[0]["id"]
    gh.claim(1)
    d.tick()
    gh._candidates = []  # #1 is claimed now: gone from the real `no:assignee` ready queue

    issue_numbers = [148, 149, 150, 169, 171, 172, 174, 178, 179]
    marked = False
    for i in range(90):  # 90 x 30 s = 45 min, past the 40 min of #191's own table
        clock.advance(TICK_SECONDS)
        ts.advance(seconds=TICK_SECONDS)
        if i < len(issue_numbers):
            gh._candidates = list(gh._candidates) + [_issue(issue_numbers[i], 27)]
        if not marked and ts.value >= finish_at:
            mark_finishing(s1, finishing_dir=tmp_path / "finishing")
            marked = True
        report = d.tick()
        if report.started:  # a real ORCH claims what it was offered at once (`orch.md`, "On wake")
            for number in report.offered:
                gh.claim(number)
            gh._candidates = [c for c in gh._candidates if c.number not in report.offered]

    events = _events(tmp_path)
    finish_iso = finish_at.isoformat(timespec="seconds")
    wakes_after = [e for e in events if e["kind"] == "orch_wake" and e["ts"] > finish_iso]
    wakes_before = [e for e in events if e["kind"] == "orch_wake" and e["ts"] <= finish_iso]
    assert wakes_after == [] and len(wakes_before) > 0

    finishing_events = [e for e in events if e["kind"] == "orch_finishing"]
    assert len(finishing_events) == 1 and finishing_events[0]["ts"] >= finish_iso

    starts_from_then = [e for e in events if e["kind"] == "orch_start" and e["ts"] >= finish_iso]
    assert len(starts_from_then) == 1


# -- #1120: the hard line ------------------------------------------------------------------------

def test_1120_at_the_line_session_is_told_to_finish_what_it_holds_and_park_only_past_the_hard_line(tmp_path: Path):
    claude, gh = FakeClaude(), FakeGh(candidates=[_issue(1, 1), _issue(2, 2)])
    d = Dispatcher(claude=claude, gh=gh, max_hours=4.0, events_file=tmp_path / "events.jsonl",
                   state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                   finishing_dir=tmp_path / "finishing", context_line=150_000, context_hard_line=400_000,
                   clock=FakeClock())

    d.tick()
    s1 = claude.list_sessions()[0]
    gh.claim(1)
    gh.claim(2)
    d.tick()  # S1 holds {1, 2}
    cur = next(c for c in d.sessions if c.session == s1["id"])
    assert cur.held == {1, 2}

    _write_transcript(tmp_path / "projects", s1["sessionId"], context=160_000)
    gh._candidates = [_issue(3, 3)]
    waked_before = len(claude.waked)
    report = d.tick()

    texts = [m for _t, m in claude.waked[waked_before:]]
    told = [m for m in texts if m.startswith("You are finishing")]
    assert len(told) == 1
    assert "400k" in told[0] and "finish what you hold" in told[0] and "Take nothing new" in told[0]
    assert report.woke == ()  # nothing new offered to the 160k session
    assert cur.finishing and cur.held == {1, 2}  # finishing, still holding both
    assert report.ended != s1["id"]  # not ended while it works
    assert [e for e in _events(tmp_path) if e["kind"] == "orch_finishing"][0]["why"].startswith("context 160000")

    # the `marked` branch: a session that said `finishing` itself below the line gets the same words
    marked_dir = tmp_path / "marked"
    claude2, gh2 = FakeClaude(), FakeGh(candidates=[_issue(1, 1)])
    d2 = Dispatcher(claude=claude2, gh=gh2, max_hours=4.0, events_file=marked_dir / "events.jsonl",
                    state_file=marked_dir / "orch_session.json", projects_dir=marked_dir / "projects",
                    finishing_dir=marked_dir / "finishing", context_line=150_000, context_hard_line=400_000,
                    clock=FakeClock())
    d2.tick()
    s2 = claude2.list_sessions()[0]["id"]
    gh2.claim(1)
    d2.tick()
    mark_finishing(s2, finishing_dir=marked_dir / "finishing")
    waked2 = len(claude2.waked)
    d2.tick()
    told2 = [m for _t, m in claude2.waked[waked2:] if m.startswith("You are finishing")]
    assert len(told2) == 1
    assert "400k" in told2[0] and "finish what you hold" in told2[0] and "Take nothing new" in told2[0]

    # the role file (a core file) says the same rule in one sentence, without the repo's number
    role = " ".join((REPO / ".claude" / "agents" / "orch.md").read_text(encoding="utf-8").split())
    sentences = re.split(r"(?<=\.)\s", role)
    assert any("take nothing new" in s.lower() and "finish what you hold" in s and "hard line" in s
               for s in sentences)


# -- number 9 -------------------------------------------------------------------------------------

def test_9_core_check_is_clean():
    _rows, ok = run_check()
    assert ok
