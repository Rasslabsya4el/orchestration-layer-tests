"""#148 unit 4 (rule 3): a core update that fails on the GitHub rate limit writes the limit's reset time into the
pending mark; neither the dispatcher's mark retry nor the before-start update runs before it; after it exactly one
attempt runs. Clock: a fake wall clock (`timestamp`) moved by `FakeClock.advance`; the update is a counting fake.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import core_pending, core_targets, gh_call
from tools.agentops.core_pending import MARK_REL, mark_waits, read_mark, write_mark
from tools.agentops.core_update import CoreUpdateError, CoreUpdateRateLimited, HomeReader
from tools.agentops.dispatch import Dispatcher, _Current

from tests._area_rule import _no_area_rule  # noqa: F401
from tests._gh_guard import _no_real_gh  # noqa: F401
from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, _issue

TAKEN = "core update (auto): updated: 3 file(s) changed; core_version -> abc12345"
MINUTE = 60.0
WALL0 = float(int(time.time()))  # import-time value only; each test resets it to the real now (_wall0_at_test_start)
SHA = "5ebb24f0aa11bb22cc33dd44ee55ff6677889900"


@pytest.fixture
def wall_shift() -> float:
    """Seconds the real clock (`time.time`, which the code under test reads) runs ahead for this test; 0 by default."""
    return 0.0


@pytest.fixture(autouse=True)
def _wall0_at_test_start(monkeypatch, wall_shift):
    """WALL0 is the real now at each test's start (the code under test reads the real clock), so a long run
    after the import does not make the scenarios' `WALL0 + 20 min` stale. The one-hour variants move the real
    clock an hour ahead first: with a WALL0 fixed at import they fail, which is the check that it is refreshed."""
    real = time.time
    if wall_shift:
        monkeypatch.setattr(time, "time", lambda: real() + wall_shift)
    monkeypatch.setattr(sys.modules[__name__], "WALL0", float(int(time.time())))


class Update:
    """The `core_update` callable: counts calls; an exception in `answers` is raised, a string returned."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


class Rig:
    def __init__(self, tmp_path: Path, update, *, candidates=(), admission_go=False) -> None:
        self.root = tmp_path
        self.clock = FakeClock()
        self.claude = FakeClaude()
        self.update = update
        self.log: list[str] = []
        self.d = Dispatcher(claude=self.claude, gh=FakeGh(candidates=list(candidates)), max_hours=4.0,
                            events_file=tmp_path / "events.jsonl", state_file=tmp_path / "state" / "orch_session.json",
                            projects_dir=tmp_path / "projects", finishing_dir=tmp_path / "finishing",
                            context_line=1_000_000, clock=self.clock,
                            timestamp=lambda: datetime.fromtimestamp(WALL0 + self.clock.value, timezone.utc),
                            core_update=update, merge_lock_path=tmp_path / "var" / "locks" / "merge.lock",
                            core_update_wait_s=5.0, core_root=tmp_path, orch_offer_max=99)
        self.d.admission = lambda previous_wait: SimpleNamespace(go=admission_go, reason="busy", line=lambda: "go")
        self.d.log = self.log.append

    def at(self, minutes: float) -> None:
        self.clock.value = minutes * MINUTE
        self.d.tick()

    def wait_lines(self) -> list[str]:
        return [m for m in self.log if "rate limit" in m and "waits" in m]


def _limited(minutes: float) -> CoreUpdateRateLimited:
    return CoreUpdateRateLimited("gh: API rate limit exceeded", until=WALL0 + minutes * MINUTE)


def test_a_rate_limited_retry_writes_until_and_no_attempt_runs_before_it(tmp_path: Path):
    write_mark(tmp_path, SHA)
    rig = Rig(tmp_path, Update(_limited(20), TAKEN))

    rig.at(0)
    assert rig.update.calls == 1
    mark = read_mark(tmp_path)
    assert mark["sha"] == SHA and mark["until"] == WALL0 + 20 * MINUTE

    for minute in (1, 10, 19):
        rig.at(minute)

    assert rig.update.calls == 1
    assert len(rig.wait_lines()) == 1  # one line per distinct until, not one per tick


def test_after_the_reset_exactly_one_attempt_runs_and_success_clears_the_mark(tmp_path: Path):
    write_mark(tmp_path, SHA, until=WALL0 + 20 * MINUTE)
    rig = Rig(tmp_path, Update(TAKEN))

    rig.at(19)
    assert rig.update.calls == 0
    rig.at(21)
    assert rig.update.calls == 1
    assert read_mark(tmp_path) is None
    rig.at(22)

    assert rig.update.calls == 1


def test_a_second_rate_limit_failure_after_the_reset_writes_a_new_until(tmp_path: Path):
    write_mark(tmp_path, SHA, until=WALL0 + 20 * MINUTE)
    rig = Rig(tmp_path, Update(_limited(40)))

    rig.at(21)
    assert rig.update.calls == 1
    assert read_mark(tmp_path)["until"] == WALL0 + 40 * MINUTE
    rig.at(30)

    assert rig.update.calls == 1
    rig.at(41)
    assert rig.update.calls == 2


def test_a_rate_limit_failure_before_an_orch_start_leaves_the_mark_and_the_start_goes_on(tmp_path: Path):
    rig = Rig(tmp_path, Update(_limited(20)), candidates=[_issue(1)], admission_go=True)

    rig.at(0)

    assert rig.update.calls == 1
    assert read_mark(tmp_path)["until"] == WALL0 + 20 * MINUTE
    assert len(rig.claude.started) == 1


def test_the_before_start_path_makes_no_attempt_while_a_mark_waits_and_the_start_proceeds(tmp_path: Path):
    write_mark(tmp_path, SHA, until=WALL0 + 20 * MINUTE)
    rig = Rig(tmp_path, Update(TAKEN), candidates=[_issue(1)], admission_go=True)

    rig.at(1)

    assert rig.update.calls == 0
    assert len(rig.claude.started) == 1


def _rule_auto(monkeypatch) -> None:
    """#188: the hold applies only to an `auto` target; the repo's own config says `home`."""
    import tools.agentops.dispatch as dispatch
    real = dispatch._load_settings
    monkeypatch.setattr(dispatch, "_load_settings", lambda: {**real(), "update_rule": "auto"})


def test_a_mark_that_waits_does_not_hold_orch_starts(tmp_path: Path, monkeypatch):
    _rule_auto(monkeypatch)
    write_mark(tmp_path, SHA, until=WALL0 + 20 * MINUTE)
    rig = Rig(tmp_path, Update(TAKEN))
    rig.d.sessions.append(_Current(session="sess-1", started_at=0.0, offered=(1,), name="",
                                   preexisting=frozenset(), finishing=True))

    rig.clock.value = 5 * MINUTE
    assert rig.d._hold_start_for_core() is False
    rig.clock.value = 21 * MINUTE  # expired: holds as today
    assert rig.d._hold_start_for_core() is True


def test_a_mark_without_until_still_holds_as_today(tmp_path: Path, monkeypatch):
    _rule_auto(monkeypatch)
    write_mark(tmp_path, SHA)
    rig = Rig(tmp_path, Update(TAKEN))
    rig.d.sessions.append(_Current(session="sess-1", started_at=0.0, offered=(1,), name="",
                                   preexisting=frozenset(), finishing=True))

    assert rig.d._hold_start_for_core() is True


def test_mark_waits_reads_only_a_numeric_until_in_the_future(tmp_path: Path):
    assert mark_waits({"sha": "x", "until": 100.0}, 99.0) is True
    assert mark_waits({"sha": "x", "until": 100.0}, 100.0) is False
    assert mark_waits({"sha": "x"}, 0.0) is False
    assert mark_waits({"sha": "x", "until": "soon"}, 0.0) is False
    assert mark_waits(None, 0.0) is False
    path = write_mark(tmp_path, SHA, until=123.5)
    assert path == tmp_path / MARK_REL
    assert read_mark(tmp_path) == {"sha": SHA, "at": read_mark(tmp_path)["at"], "until": 123.5}
    write_mark(tmp_path, SHA)
    assert "until" not in read_mark(tmp_path)


# -- HomeReader._api ----------------------------------------------------------------------------------------------

def _runner(stderr: str):
    return lambda args: SimpleNamespace(returncode=1, stdout="", stderr=stderr)


def test_home_reader_raises_rate_limited_with_the_injected_reset():
    reader = HomeReader("o/r", gh_runner=_runner("gh: API rate limit exceeded, waiting until 12:20:00"),
                        rate_limit_reset=lambda: 5000.0)

    with pytest.raises(CoreUpdateRateLimited) as info:
        reader.head_sha()

    assert info.value.until == 5000.0
    assert isinstance(info.value, CoreUpdateError)


def test_home_reader_matches_rate_limit_case_insensitively():
    reader = HomeReader("o/r", gh_runner=_runner("HTTP 403: API Rate Limit Exceeded for user"),
                        rate_limit_reset=lambda: 7.0)

    with pytest.raises(CoreUpdateRateLimited):
        reader.head_sha()


def test_home_reader_failed_reset_read_gives_now_plus_a_minute():
    def broken():
        raise OSError("no network")

    reader = HomeReader("o/r", gh_runner=_runner("API rate limit exceeded"), rate_limit_reset=broken)
    before = time.time()

    with pytest.raises(CoreUpdateRateLimited) as info:
        reader.head_sha()

    assert before + 59 <= info.value.until <= time.time() + 61


def test_home_reader_plain_error_stays_a_plain_core_update_error():
    reader = HomeReader("o/r", gh_runner=_runner("HTTP 404: Not Found"), rate_limit_reset=lambda: 5000.0)

    with pytest.raises(CoreUpdateError) as info:
        reader.head_sha()

    assert type(info.value) is CoreUpdateError


def test_gh_call_exposes_rate_limit_reset_as_a_wrapper_of_the_read(monkeypatch):
    monkeypatch.setattr(gh_call, "_read_reset", lambda resource: 42.0 if resource == "core" else None)

    assert gh_call.rate_limit_reset() == 42.0
    assert gh_call.rate_limit_reset("graphql") is None


# -- run_core_targets ---------------------------------------------------------------------------------------------

def test_run_core_targets_leaves_a_mark_with_until_and_still_returns_failed(tmp_path, monkeypatch):
    folder = tmp_path / "target"
    folder.mkdir()

    def limited(folder, gh_runner=None, lock_wait_s=None, home_count=None):
        raise _limited(20)

    monkeypatch.setattr(core_targets, "run_core_update", limited)
    monkeypatch.setattr(core_targets, "blocking_orch_live", lambda f: False)
    monkeypatch.setattr(core_targets, "_home_head", lambda home_root: SHA)

    lines = core_targets.run_core_targets([{"repo": "r", "folder": str(folder)}], home_root=tmp_path)

    assert lines == ["r: failed: gh: API rate limit exceeded"]
    mark = core_pending.read_mark(folder)
    assert mark["sha"] == SHA and mark["until"] == WALL0 + 20 * MINUTE


# -- apply_auto_core_update (the dispatcher's own update callable) ------------------------------------------------

@pytest.mark.parametrize("wall_shift", [0.0, 3600.0])
def test_apply_auto_core_update_leaves_the_mark_and_skips_while_it_waits(tmp_path, monkeypatch, wall_shift):
    from tools.agentops import core_update, dispatch

    calls: list[int] = []
    def limited(root, gh_runner=None, lock_wait_s=None, home_count=None):
        calls.append(1)
        raise _limited(20)

    monkeypatch.setattr(core_update, "run_core_update", limited)
    settings = {"update_rule": "auto", "core_version": SHA}

    first = dispatch.apply_auto_core_update(tmp_path, settings=settings)
    second = dispatch.apply_auto_core_update(tmp_path, settings=settings)

    assert first.startswith("core update (auto) refused:")
    assert second.startswith("core update (auto) waits for the GitHub rate limit")
    assert calls == [1]
    mark = read_mark(tmp_path)
    assert mark["sha"] == SHA and mark["until"] == WALL0 + 20 * MINUTE


# -- send-back: a refused mark keeps a waiting until; a reset is always set and capped; pushes wait ----------------

@pytest.mark.parametrize("wall_shift", [0.0, 3600.0])
def test_a_refused_push_keeps_the_waiting_until_of_the_standing_mark(tmp_path, monkeypatch, wall_shift):
    rig = Rig(tmp_path, Update(TAKEN))
    write_mark(tmp_path, SHA, until=WALL0 + 20 * MINUTE)
    monkeypatch.setattr(core_targets, "blocking_orch_live", lambda f: True)
    monkeypatch.setattr(core_targets, "_home_head", lambda home_root: SHA)

    core_targets._mark_pending(tmp_path, tmp_path)

    assert read_mark(tmp_path)["until"] == WALL0 + 20 * MINUTE
    rig.at(10)
    assert rig.update.calls == 0


def test_a_refused_mark_over_an_expired_until_carries_none(tmp_path):
    write_mark(tmp_path, SHA, until=time.time() - 5)

    write_mark(tmp_path, SHA)

    assert "until" not in read_mark(tmp_path)


def test_a_rate_limit_failure_with_no_reset_from_the_read_still_gets_an_until():
    reader = HomeReader("o/r", gh_runner=_runner("secondary rate limit"), rate_limit_reset=lambda: None)
    before = time.time()

    with pytest.raises(CoreUpdateRateLimited) as info:
        reader.head_sha()

    assert before + 59 <= info.value.until <= time.time() + 61


def test_an_until_more_than_70_minutes_ahead_is_cut_to_now_plus_70(tmp_path):
    before = time.time()
    write_mark(tmp_path, SHA, until=before + 600 * MINUTE)
    after = time.time()

    until = read_mark(tmp_path)["until"]

    assert before + 70 * MINUTE <= until <= after + 70 * MINUTE
    write_mark(tmp_path, SHA, until=before + 30 * MINUTE)
    assert read_mark(tmp_path)["until"] == before + 30 * MINUTE


def test_home_pushes_during_a_wait_make_no_attempt_and_one_after_it_makes_one(tmp_path, monkeypatch):
    folder = tmp_path / "target"
    folder.mkdir()
    until = time.time() + 20 * MINUTE
    write_mark(folder, SHA, until=until)
    calls: list[int] = []
    monkeypatch.setattr(core_targets, "blocking_orch_live", lambda f: False)
    monkeypatch.setattr(core_targets, "_home_head", lambda home_root: SHA)

    def update(folder, gh_runner=None, lock_wait_s=None, home_count=None):
        calls.append(1)
        return SimpleNamespace(home_head=SHA, waited=False)

    monkeypatch.setattr(core_targets, "run_core_update", update)
    entry = [{"repo": "r", "folder": str(folder)}]

    lines = [core_targets.run_core_targets(entry, home_root=tmp_path)[0] for _ in range(3)]

    assert calls == []
    stamp = time.strftime("%H:%M:%S", time.localtime(until))
    assert lines == [f"r: failed: waits for the GitHub rate limit until {stamp}"] * 3
    assert read_mark(folder)["until"] == until
    write_mark(folder, SHA, until=time.time() - 1)  # the limit has reset

    assert core_targets.run_core_targets(entry, home_root=tmp_path) == [f"r: taken {SHA[:8]}"]
    assert calls == [1]
