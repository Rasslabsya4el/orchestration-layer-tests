"""#141: the dispatcher exits after a tick when its own code (`tools/agentops/**/*.py`) changed since
it started, so the keepalive's `dispatch --ensure` restarts it on the new code; a fingerprint
failure is logged and does not exit."""

from __future__ import annotations

from pathlib import Path

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, _issue
from tools.agentops import dispatch as dp
from tools.agentops.dispatch import Dispatcher

A = "a" * 64
B = "b" * 64


def _dispatcher_fp(tmp_path: Path, **kwargs) -> Dispatcher:
    # the same arguments as tests/test_agentops_dispatch.py::_dispatcher, plus the fingerprint hooks
    return Dispatcher(claude=FakeClaude(), gh=FakeGh(candidates=[_issue(5)]), max_hours=4.0,
                      events_file=tmp_path / "events.jsonl", orch_offer_max=99,
                      state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                      clock=FakeClock(), **kwargs)


def _count_ticks(d: Dispatcher) -> dict:
    counter = {"ticks": 0}
    real_tick = d.tick

    def tick():
        counter["ticks"] += 1
        return real_tick()

    d.tick = tick  # run() calls self.tick(): the instance attribute counts every tick
    return counter


def test_code_change_after_tick_two_exits_after_two_ticks_and_releases_the_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(dp, "_head_commit", lambda: "0123abcd")  # #367: the lock's commit, not the real checkout's HEAD
    lock = tmp_path / "d.lock"
    # the start value is passed in (as run_dispatch does), so the first check is after tick 1
    d = _dispatcher_fp(tmp_path, start_fingerprint=A)
    counter = _count_ticks(d)
    d.code_fingerprint = lambda: A if counter["ticks"] < 2 else B
    lines: list[str] = []
    d.log = lines.append
    with dp.dispatcher_lock(lock):
        assert d.run(ticks=5, sleep=lambda s: None) == 0
    assert counter["ticks"] == 2
    changed = [ln for ln in lines if "code changed" in ln]
    assert len(changed) == 1
    assert A[:8] in changed[0] and B[:8] in changed[0]
    assert not lock.exists()  # the lock is released when run returns


def test_unchanged_code_runs_all_ticks_with_no_exit(tmp_path):
    d = _dispatcher_fp(tmp_path, code_fingerprint=lambda: A)  # start recorded by run itself
    counter = _count_ticks(d)
    lines: list[str] = []
    d.log = lines.append
    assert d.run(ticks=5, sleep=lambda s: None) == 0
    assert counter["ticks"] == 5
    assert [ln for ln in lines if "code changed" in ln] == []


def test_fingerprint_follows_python_under_tools_agentops_only(tmp_path):
    (tmp_path / "tools" / "agentops").mkdir(parents=True)
    (tmp_path / "tools" / "agentops" / "dispatch.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("doc\n", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "orchestration.json").write_text("{}\n", encoding="utf-8")

    first = dp.code_fingerprint(tmp_path)
    assert dp.code_fingerprint(tmp_path) == first  # an unchanged tree gives an equal value

    (tmp_path / "tools" / "agentops" / "dispatch.py").write_text("x = 2\n", encoding="utf-8")
    changed = dp.code_fingerprint(tmp_path)
    assert changed != first  # a .py under tools/agentops/ changes it

    (tmp_path / "docs" / "guide.md").write_text("doc changed\n", encoding="utf-8")
    (tmp_path / "config" / "orchestration.json").write_text('{"x": 1}\n', encoding="utf-8")
    assert dp.code_fingerprint(tmp_path) == changed  # docs/ and config/ do not


def test_a_failing_fingerprint_is_logged_and_the_run_goes_on(tmp_path):
    def boom():
        raise RuntimeError("disk gone")

    d = _dispatcher_fp(tmp_path, code_fingerprint=boom)
    counter = _count_ticks(d)
    lines: list[str] = []
    d.log = lines.append
    assert d.run(ticks=3, sleep=lambda s: None) == 0  # no exit from the failing check
    assert counter["ticks"] == 3
    assert [ln for ln in lines if "code changed" in ln] == []
    assert [ln for ln in lines if ln.startswith("fingerprint failed")]
