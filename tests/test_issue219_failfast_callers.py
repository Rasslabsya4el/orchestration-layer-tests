"""#219 rule 1, the callers and the edges: the fail-fast verdict through ``run_merge`` (``_merge_attempt``), the commit
gate and ``prequeue``; a re-run alone that hangs or outlives the suite is ended; a reading or a re-run that could not
tell leaves a trace and never stops the suite. Real pytest on temp repos; no heavy tool.

No test here asserts a wall-clock bound: an outcome is proved by a marker the suite writes or does not write, and by
the process tree being gone. The helpers come from ``tests/test_issue219_failfast.py``."""
from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.test_issue219_failfast import (FAST_ENV, WAIT_FOR, WAIT_LIMIT_S, _fails_always, _fails_once, _gone,
                                          _record_kills, _run)
from tests.test_prequeue import RED_A, _change_a, _fixture
from tests.test_select_build_map import SUITE as PQ_SUITE
from tools.agentops import core_targets, merge, merge_failfast, prequeue
from tools.agentops.merge import SuiteRun, run_commit, run_merge
from tools.agentops.merge_failfast import FailFast, NoVerdict

SRC = Path(merge.__file__).parent
TEST_ID = "tests/test_t.py::test_t"


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _world(tmp_path: Path, body: str):
    """A bare origin whose main holds the events plugin and one green test, a checkout of it, and the branch
    ``origin/feat`` that adds ``tests/test_t.py`` with ``body``."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(bare), str(seed))
    (seed / "tools" / "agentops").mkdir(parents=True)
    (seed / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (seed / "tools" / "agentops" / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy(SRC / "suite_events_plugin.py", seed / "tools" / "agentops" / "suite_events_plugin.py")
    (seed / "tests").mkdir()
    (seed / "tests" / "test_base.py").write_text("def test_base():\n    pass\n", encoding="utf-8")
    (seed / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "initial")
    _git(seed, "push", "-q", "origin", "main")
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(bare), str(checkout))
    worker = tmp_path / "worker"
    _git(tmp_path, "clone", "-q", str(bare), str(worker))
    _git(worker, "checkout", "-q", "-b", "feat")
    (worker / "tests" / "test_t.py").write_text(body, encoding="utf-8")
    _git(worker, "add", "-A")
    _git(worker, "commit", "-q", "-m", "feat")
    _git(worker, "push", "-q", "origin", "feat")
    lock = tmp_path / "var" / "locks" / "merge.lock"
    lock.parent.mkdir(parents=True)
    return bare, checkout, lock


def _setup(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.setattr(merge, "SUITE_OUTPUT_DIR", tmp_path / "suite_out")
    monkeypatch.setattr(merge, "MERGE_LOG_PATH", tmp_path / "merge.log")
    monkeypatch.setenv(merge.ENV_MERGE_LOG, str(tmp_path / "merge.log"))
    monkeypatch.setenv(merge.ENV_SUITE_OUTPUT_DIR, str(tmp_path / "suite_out"))
    monkeypatch.setattr(merge, "resolve_select_tests", lambda: False)
    monkeypatch.setattr(merge, "_spawn_map_run", lambda cwd, sha: "")
    monkeypatch.setattr(core_targets, "run_core_targets", lambda *a, **k: [])
    monkeypatch.setattr(merge_failfast, "FAILFAST_POLL_SECONDS", 0.2)
    monkeypatch.setattr(merge, "resolve_suite_total_limit", lambda: None)
    monkeypatch.setattr(merge, "resolve_suite_quiet_limit", lambda: None)
    calls = {"recheck": 0, "alone": 0}
    real_recheck, real_alone = merge._recheck_failed, merge._rerun_alone

    def recheck(*a, **k):
        calls["recheck"] += 1
        return real_recheck(*a, **k)

    def alone(*a, **k):
        calls["alone"] += 1
        return real_alone(*a, **k)

    monkeypatch.setattr(merge, "_recheck_failed", recheck)
    monkeypatch.setattr(merge, "_rerun_alone", alone)
    return calls, f'"{sys.executable}" -m pytest tests -q -p no:cacheprovider --basetemp "{tmp_path / "bt"}"'


# -- through run_merge (_merge_attempt) ------------------------------------------------------------------------------

def test_merge_row1_red_names_the_test_and_both_failures_with_no_third_run(tmp_path, monkeypatch):
    after = tmp_path / "ran_after"
    bare, checkout, lock = _world(tmp_path, _fails_always(tmp_path / "never", after))
    calls, suite = _setup(tmp_path, monkeypatch)
    killed = _record_kills(monkeypatch)
    main_before = _git(bare, "rev-parse", "main")
    result = run_merge("origin/feat", suite, cwd=checkout, lock_path=lock, lock_timeout=60)
    assert not after.exists()  # the suite was stopped while test_z_after waited: the remaining tests were not run
    assert killed and all(_gone(pid) for pid in killed)  # the suite's process tree is gone
    assert not result.ok and result.reason == "red-suite", result.render()
    assert TEST_ID in result.message and result.message.count("boom") == 2  # the test and both failures
    assert "the remaining tests were not run" in result.message
    assert result.failed_tests == [TEST_ID] and result.failed_count == 1
    assert calls == {"recheck": 0, "alone": 1}  # one re-run alone, no third run
    assert _git(bare, "rev-parse", "main") == main_before


def test_merge_row2_a_test_that_passes_alone_lets_the_merge_land_green(tmp_path, monkeypatch):
    after, rerun_ran = tmp_path / "ran_after", tmp_path / "rerun_ran"
    bare, checkout, lock = _world(tmp_path, _fails_once(tmp_path / "failed_once", rerun_ran, after))
    calls, suite = _setup(tmp_path, monkeypatch)
    main_before = _git(bare, "rev-parse", "main")
    result = run_merge("origin/feat", suite, cwd=checkout, lock_path=lock, lock_timeout=60)
    assert result.ok and result.pushed, result.render()
    assert rerun_ran.exists()  # the re-run alone ran the test, and it passed
    assert after.exists()  # the suite went on to the end
    assert calls["alone"] == 1
    assert _git(bare, "rev-parse", "main") != main_before


# -- the commit gate ----------------------------------------------------------------------------------------------

def test_commit_gate_red_suite_carries_the_fail_fast_message(tmp_path, monkeypatch):
    after = tmp_path / "ran_after"
    bare, checkout, lock = _world(tmp_path, "def test_old():\n    pass\n")
    calls, suite = _setup(tmp_path, monkeypatch)
    killed = _record_kills(monkeypatch)
    (checkout / "tests").mkdir(exist_ok=True)
    (checkout / "tests" / "test_t.py").write_text(_fails_always(tmp_path / "never", after), encoding="utf-8")
    main_before = _git(bare, "rev-parse", "main")
    result = run_commit(["tests/test_t.py"], "add a failing test", cwd=checkout, lock_path=lock, lock_timeout=60,
                        suite_cmd=suite)
    assert not after.exists()  # the suite was stopped while test_z_after waited
    assert killed and all(_gone(pid) for pid in killed)  # the suite's process tree is gone
    assert not result.ok and result.reason == "red-suite", result.message
    assert TEST_ID in result.message and result.message.count("boom") == 2
    assert calls["alone"] == 1 and _git(bare, "rev-parse", "main") == main_before


# -- prequeue -----------------------------------------------------------------------------------------------------

def test_prequeue_prints_the_fail_fast_message_and_runs_no_third_time(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(merge, "resolve_select_tests", lambda: True)
    repo, _base = _fixture(tmp_path, with_map=True)
    _change_a(repo, RED_A)
    confirmed = merge_failfast.Confirmed("tests/test_a.py::test_a", "assert 2 == 1", "assert 2 == 1")
    monkeypatch.setattr(merge, "_run_merge_suite", lambda *a, **k: SuiteRun(False, "no summary", 0, fail_fast=confirmed,
                                                                             fail_fast_notes=["fail-fast: a note"]))

    def never(*a, **k):
        raise AssertionError("a third run")

    monkeypatch.setattr(merge, "_recheck_failed", never)
    rc = prequeue.run_prequeue(repo, suite_cmd=PQ_SUITE, log_path=tmp_path / "merge.log", output_dir=tmp_path / "out",
                               suite_shared=())
    out = capsys.readouterr().out
    assert rc == 1
    assert "prequeue: red, fail-fast: tests/test_a.py::test_a failed in the suite" in out and "fail-fast: a note" in out
    assert "names no failing test id" not in out


# -- a re-run alone is bounded and ended with the suite -----------------------------------------------------------------

def _hang_body(flag: Path, note: Path, after: Path) -> str:
    """``test_t`` fails in the suite run and leaves ``flag``; its re-run alone finds the flag and hangs. The test after
    it writes ``after`` only once ``note`` exists, which the parent writes when it records the note for the hang."""
    return (WAIT_FOR + "def test_a():\n    pass\n"
            f"def test_t():\n    import os, time\n    if os.path.exists({str(flag)!r}):\n        time.sleep(120)\n"
            f"    open({str(flag)!r}, 'w').close()\n    assert 0, 'first'\n"
            f"def test_z_after():\n    wait_for({str(note)!r})\n    open({str(after)!r}, 'w').close()\n")


def test_a_hanging_rerun_is_ended_by_its_bound_and_the_suite_goes_on(tmp_path, monkeypatch):
    after, noted = tmp_path / "ran_after", tmp_path / "noted"
    monkeypatch.setattr(merge, "FAILFAST_RERUN_SECONDS", 1.5)
    real_note = FailFast._note

    def note_and_mark(self, text):  # the note hook: the suite waits for its note, so the note is in before it goes on
        real_note(self, text)
        noted.touch()

    monkeypatch.setattr(FailFast, "_note", note_and_mark)
    killed = _record_kills(monkeypatch)
    run, out, _cmd, _d = _run(tmp_path, monkeypatch, _hang_body(tmp_path / "flag", noted, after))
    assert run.fail_fast is None and after.exists()  # no verdict: the suite went on to its end
    assert not run.ok  # T failed once in the suite and was never judged alone: the end-of-suite step judges
    assert killed and all(_gone(pid) for pid in killed)  # the hung re-run's process tree was ended
    assert len(run.fail_fast_notes) == 1 and "ran past" in run.fail_fast_notes[0]
    assert run.fail_fast_notes[0] in out.read_text(encoding="utf-8")  # the trace is in the suite output
    result_message = merge._red_with_fail_fast(run, 1, ["test_t.py::test_t"], out)
    assert "ran past" in result_message  # and in the result's text


def _stop_once_started(pidfile: Path, stop: threading.Event) -> None:
    """Thread body: sets ``stop`` once the re-run has written its pid (it is in flight), or at the safety bound."""
    end = time.monotonic() + WAIT_LIMIT_S
    while time.monotonic() < end:
        try:
            if pidfile.is_file() and pidfile.read_text(encoding="utf-8").strip():
                break
        except OSError:  # the child may be mid-write: poll again
            pass
        time.sleep(0.05)
    stop.set()


@pytest.mark.parametrize("outer_addopts", ["", "-p tools.agentops.suite_events_plugin"])
def test_a_rerun_in_flight_is_ended_when_the_suite_stops_it(tmp_path, monkeypatch, outer_addopts):
    """``outer_addopts``: a full run started by the merge suite carries its plugins in ``PYTEST_ADDOPTS``, and every
    test's child process inherits it; the re-run in ``tmp_path`` cannot import them and would end red at once."""
    if outer_addopts:
        monkeypatch.setenv("PYTEST_ADDOPTS", outer_addopts)
    pidfile = tmp_path / "rerun.pid"
    (tmp_path / "test_t.py").write_text(
        f"def test_t():\n    import os, time\n    open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n    time.sleep(120)\n",
        encoding="utf-8")
    stop = threading.Event()
    threading.Thread(target=_stop_once_started, args=(pidfile, stop), daemon=True).start()
    with pytest.raises(NoVerdict, match="the suite ended"):
        merge._rerun_alone(f'"{sys.executable}" -m pytest', "test_t.py::test_t", tmp_path,
                           {"PYTHONPATH": str(tmp_path), **FAST_ENV}, stop, limit=300)
    assert pidfile.is_file() and _gone(int(pidfile.read_text(encoding="utf-8")))  # the re-run was in flight, and ended


# -- fail-soft, with a trace --------------------------------------------------------------------------------------------

class _Proc:
    pid = 4242

    def poll(self):
        return None


def _watch(ff, seconds=0.6):
    stop = threading.Event()
    t = threading.Thread(target=ff.watch, args=(_Proc(), stop), daemon=True)
    t.start()
    t.join(seconds)
    stop.set()
    t.join(2)


def test_a_failed_reading_or_rerun_leaves_a_note_each_and_stops_nothing():
    killed = []

    def reading():
        raise OSError("events file locked")

    ff = FailFast(reading, dict, lambda t, s: (True, ""), killed.append, interval=0.05)
    _watch(ff)
    assert killed == [] and ff.confirmed is None
    assert ff.notes == ["fail-fast: reading the failed tests raised OSError: events file locked"]  # once, not per tick

    def rerun(test, stop):
        raise NoVerdict("it ran past 5 min")

    ff = FailFast(lambda: ["t.py::a"], dict, rerun, killed.append, interval=0.05)
    _watch(ff)
    assert killed == [] and ff.notes == ["fail-fast: no verdict for t.py::a: it ran past 5 min"]

    def crash(test, stop):
        raise RuntimeError("spawn failed")

    ff = FailFast(lambda: ["t.py::b"], dict, crash, killed.append, interval=0.05)
    _watch(ff)
    assert killed == [] and ff.notes == ["fail-fast: the re-run of t.py::b raised RuntimeError: spawn failed"]
