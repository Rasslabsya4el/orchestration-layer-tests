"""#4: the dispatcher offers one ORCH session at most `orch_offer_max` ready tickets, oldest first.

Seams: the first prompt of an ORCH session the dispatcher starts (`Dispatcher.tick`, the command's
last argument), and the settings reader `core_settings.load` (the key, and its default when absent).
"""

from __future__ import annotations

import json
from pathlib import Path

from tools.agentops import core_settings
from tools.agentops.dispatch import Dispatcher
from tools.agentops.worktree import ROOT
from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, _issue


def _start(tmp_path: Path, candidates, **kwargs) -> FakeClaude:
    claude = FakeClaude()
    gh = FakeGh(candidates=candidates)
    d = Dispatcher(claude=claude, gh=gh, max_hours=4.0, events_file=tmp_path / "events.jsonl",
                   state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                   clock=FakeClock(), **kwargs)
    d.tick()
    return claude


def _queue_of_seven() -> list:
    # created in a shuffled order; day == number, so the oldest four are #1-#4
    return [_issue(n, n) for n in (7, 3, 5, 1, 6, 2, 4)]


def _settings(tmp_path: Path, *, value=None) -> Path:
    """A copy of the repo's settings file with `orch_offer_max` removed (absent) or set to `value`."""
    data = json.loads((ROOT / "config" / "orchestration.json").read_text(encoding="utf-8"))
    data.pop("orch_offer_max", None)
    if value is not None:
        data["orch_offer_max"] = value
    path = tmp_path / "orchestration.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _prompt(claude: FakeClaude) -> tuple[list[int], str]:
    assert len(claude.started) == 1
    offered, cmd = claude.started[0]
    return offered, cmd[-1]


def test_queue_of_seven_offers_exactly_the_oldest_four(tmp_path: Path):
    claude = _start(tmp_path, _queue_of_seven())  # the repo's own settings file

    offered, prompt = _prompt(claude)
    assert offered == [1, 2, 3, 4]
    assert prompt.startswith("Take every ready issue (ready now, in dispatch order: #1, #2, #3, #4)")
    for waiting in (5, 6, 7):
        assert f"#{waiting}" not in prompt


def test_key_set_to_two_offers_two(tmp_path: Path):
    value = core_settings.load(_settings(tmp_path, value=2))["orch_offer_max"]
    assert value == 2

    claude = _start(tmp_path, _queue_of_seven(), orch_offer_max=value)

    offered, prompt = _prompt(claude)
    assert offered == [1, 2]
    assert prompt.startswith("Take every ready issue (ready now, in dispatch order: #1, #2)")
    for waiting in (3, 4, 5, 6, 7):
        assert f"#{waiting}" not in prompt


def test_key_absent_reads_four(tmp_path: Path):
    assert core_settings.load(_settings(tmp_path))["orch_offer_max"] == 4


def test_key_absent_offers_four(tmp_path: Path):
    value = core_settings.load(_settings(tmp_path))["orch_offer_max"]

    claude = _start(tmp_path, _queue_of_seven(), orch_offer_max=value)

    offered, prompt = _prompt(claude)
    assert offered == [1, 2, 3, 4]
    assert "#5" not in prompt


# -- #106: the wake obeys the same cap as the start offer ---------------------------------------------

import pytest

from tools.agentops.dispatch import build_wake_message
from tests.test_agentops_dispatch import _dispatcher, _events


def _session_with_four_claims(tmp_path: Path, messages: list[str]):
    """A live session started with #1-#4 (cap 4) that has claimed all four."""
    claude = FakeClaude()
    gh = FakeGh(candidates=[_issue(n, n) for n in (1, 2, 3, 4)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh, orch_offer_max=4)
    d.log = messages.append
    d.tick()
    live_id = claude.list_sessions()[0]["id"]
    for n in (1, 2, 3, 4):
        gh.claim(n)
    gh._candidates = []
    d.tick()  # learns held = {1, 2, 3, 4}
    assert claude.waked == []
    return claude, gh, d, live_id


def test_106_session_with_four_claims_is_never_woken_about_new_ready_issues(tmp_path: Path):
    messages: list[str] = []
    claude, gh, d, live_id = _session_with_four_claims(tmp_path, messages)

    gh._candidates = [_issue(n, n) for n in (5, 6, 7)]
    for _ in range(3):
        report = d.tick()
        assert report.woke == ()

    assert claude.waked == []
    assert not any(e["kind"] == "orch_wake" for e in _events(tmp_path))
    skipped = [m for m in messages if "skipped: holds" in m]
    assert len(skipped) == 1  # one line, not one per tick
    assert skipped[0] == f"wake of {d.sessions[0].name} skipped: holds 4 of 4"


def test_106_session_holding_two_is_woken_about_the_two_oldest_only(tmp_path: Path):
    claude, gh, d, live_id = _session_with_four_claims(tmp_path, [])
    gh.close(3)
    gh.close(4)
    gh._candidates = [_issue(12, 12), _issue(10, 10), _issue(11, 11)]  # t1 < t2 < t3 = #10 < #11 < #12

    report = d.tick()

    assert report.woke == (10, 11)
    assert len(claude.waked) == 1
    assert claude.waked[0][1] == build_wake_message([10, 11], session=live_id)
    assert "#12" not in claude.waked[0][1]


def test_106_skipped_wake_line_repeats_only_when_held_changes(tmp_path: Path):
    messages: list[str] = []
    claude, gh, d, live_id = _session_with_four_claims(tmp_path, messages)
    gh._candidates = [_issue(5, 5)]
    d.tick()
    d.tick()
    gh.close(4)  # held 4 -> 3: room 1, woken about #5
    d.tick()
    assert [m.rsplit(": ", 1)[-1] for m in messages if "skipped: holds" in m] == ["holds 4 of 4"]  # once, over 2 ticks
    assert claude.waked and "#5" in claude.waked[0][1]


@pytest.mark.parametrize("bad", [0, -1, 2.5, "x", True])
def test_106_bad_offer_max_reads_four_with_one_warning(tmp_path: Path, bad):
    value = core_settings.load(_settings(tmp_path, value=bad))["orch_offer_max"]
    messages: list[str] = []
    claude = FakeClaude()
    d = Dispatcher(claude=claude, gh=FakeGh(candidates=_queue_of_seven()), max_hours=4.0,
                   events_file=tmp_path / "events.jsonl", state_file=tmp_path / "orch_session.json",
                   projects_dir=tmp_path / "projects", clock=FakeClock(), orch_offer_max=value,
                   log=messages.append)

    assert d.orch_offer_max == 4
    warnings = [m for m in messages if m.startswith("warning:")]
    assert len(warnings) == 1 and repr(bad) in warnings[0]
    d.tick()
    d.tick()
    assert [m for m in messages if m.startswith("warning:")] == warnings  # not repeated per tick
    assert _prompt(claude)[0] == [1, 2, 3, 4]
