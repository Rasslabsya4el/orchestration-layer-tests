"""#1113 unit 4b: the dispatcher's own reads of GitHub. A counting fake `gh` (the runner seam every reader
already goes through) counts the calls. Rows: the ready-for-agent list is read once per tick however
many callers ask; two ticks 30 s apart read it twice; a write between two reads makes the second read
real; the two one-point readers (claims, autopilot goals) are read once inside 90 s;
StreamKeeper is read once inside 5 min. No real `gh`, no real clock."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from tools.agentops import autopilot  # noqa: F401  (registers autopilot_goals on GhCli)
from tools.agentops.dispatch import GhCli
from tools.agentops.streams import StreamKeeper

from tests.test_agentops_dispatch import FakeClaude, FakeClock
from tests.test_agentops_dispatch_finishing import FakeTimestamp, _dispatcher

NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)


class CountingGh:
    """A fake `gh`: records every call; every read answers an empty list, every write succeeds."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args, stdin=None):
        self.calls.append(list(args))
        body = json.dumps({"data": {"search": {"nodes": []}}}) if args and args[0] == "api" else "[]"
        return subprocess.CompletedProcess(args, 0, body, "")

    def count(self, pred) -> int:
        return sum(1 for a in self.calls if pred(a))


def is_ready_list(a: list[str]) -> bool:
    """The ready-for-agent candidate pool: `--search no:assignee` (dispatch.py ready_for_agent_issues)."""
    return a[:2] == ["issue", "list"] and "no:assignee" in a and "ready-for-agent" in a


def is_claims(a: list[str]) -> bool:
    return a[:2] == ["issue", "list"] and "--assignee" in a


def is_autopilot(a: list[str]) -> bool:
    return a[:2] == ["issue", "list"] and "autopilot" in a


def is_stream_read(a: list[str]) -> bool:
    return a[0] == "api"


def _dispatch(tmp_path: Path, gh_fake: CountingGh, clock: FakeClock):
    return _dispatcher(tmp_path, claude=FakeClaude(), gh=GhCli(runner=gh_fake), clock=clock,
                       timestamp=FakeTimestamp(NOW))


def test_row1_ready_list_read_once_per_tick(tmp_path: Path):
    """A tick and its three ready-list callers (`_track_ready_waits`, `_maybe_start`, `_maybe_wake`
    through `_eligible_candidates`) share one `gh issue list --label ready-for-agent` call."""
    gh_fake = CountingGh()
    d = _dispatch(tmp_path, gh_fake, FakeClock())
    d.tick()
    d._eligible_candidates()
    d._eligible_candidates(offering=True)
    assert gh_fake.count(is_ready_list) == 1


def test_row2_two_ticks_thirty_seconds_apart_read_twice(tmp_path: Path):
    gh_fake = CountingGh()
    clock = FakeClock()
    d = _dispatch(tmp_path, gh_fake, clock)
    d.tick()
    clock.advance(30)
    d.tick()
    assert gh_fake.count(is_ready_list) == 2


def test_row3_write_between_two_reads_makes_the_second_read_real(tmp_path: Path):
    gh_fake = CountingGh()
    d = _dispatch(tmp_path, gh_fake, FakeClock())
    d.tick()  # one read
    d.gh.add_label(5, "ready-for-agent")  # a write the dispatcher itself did in this tick
    d._eligible_candidates()  # must see fresh data: a second read
    assert gh_fake.count(is_ready_list) == 2


def test_row4_one_point_readers_read_once_inside_ninety_seconds():
    gh_fake = CountingGh()
    clock = FakeClock()
    gh = GhCli(runner=gh_fake, clock=clock)
    for t in (0, 30, 60, 80):  # four ticks, all inside 90 s
        clock.value = float(t)
        gh.memo.start_tick()
        gh.claimed_issues()
        gh.autopilot_goals()
    assert gh_fake.count(is_claims) == 1
    assert gh_fake.count(is_autopilot) == 1


def test_row5_write_drops_the_timed_memo():
    gh_fake = CountingGh()
    clock = FakeClock()
    gh = GhCli(runner=gh_fake, clock=clock)
    gh.memo.start_tick()
    gh.claimed_issues()
    gh.add_label(7, "ready-for-agent")
    clock.value = 10.0
    gh.claimed_issues()
    assert gh_fake.count(is_claims) == 2


def test_row6_stream_keeper_read_four_times_inside_five_minutes_is_one_call():
    gh_fake = CountingGh()
    clock = FakeClock()
    keeper = StreamKeeper("o/r", runner=gh_fake, clock=clock, log=lambda m: None)
    for t in (0, 60, 120, 180):
        clock.value = float(t)
        keeper.tick()
    assert gh_fake.count(is_stream_read) == 1
