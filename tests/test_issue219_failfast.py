"""#219 rule 1: the merge suite stops at a confirmed failure. A test that fails during the run is re-run alone at
once; failing again ends the run red (the suite's process tree stopped, the remaining tests not run, the result
names the test and both failures); passing alone lets the suite go on. Real pytest on a temp suite; no heavy tool.

No test here asserts a wall-clock bound: a pytest child can take a minute to start on a loaded machine. An outcome
is proved by a marker the suite writes or does not write, and by the process tree being gone."""
from __future__ import annotations

import shutil
import sys
import threading
import time
from pathlib import Path

import psutil

from tools.agentops import merge, merge_failfast
from tools.agentops.merge_failfast import FailFast
from tests.test_merge_red_batch_ids import quiet_machine

SRC = Path(merge.__file__).parent
WAIT_LIMIT_S = 300  # a safety bound on a wait that only a failing run reaches: the suite then goes on, and the test is red

# A test body waits for a marker (an event), never for a fixed time; the bound is only the safety net.
WAIT_FOR = ("import os, time\n"
            f"def wait_for(path, limit={WAIT_LIMIT_S}):\n"
            "    end = time.monotonic() + limit\n"
            "    while not os.path.exists(path) and time.monotonic() < end:\n"
            "        time.sleep(0.05)\n")

# the suite's own pytest and the re-run load no third-party plugin: less start-up on a loaded machine
# (and never the outer run's PYTEST_ADDOPTS: a full run started by the merge suite names plugins the child cannot import)
FAST_ENV = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""}


def _suite_dir(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "suite"
    (d / "tools" / "agentops").mkdir(parents=True)
    (d / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (d / "tools" / "agentops" / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy(SRC / "suite_events_plugin.py", d / "tools" / "agentops" / "suite_events_plugin.py")
    (d / "test_t.py").write_text(body, encoding="utf-8")
    return d


def _run(tmp_path, monkeypatch, body):
    """The merge suite on a temp suite of one test file; ``(run, output file, suite cmd, suite dir)``."""
    d = _suite_dir(tmp_path, body)
    quiet_machine(monkeypatch)  # #261: the run's red re-check never waits on the real PC load
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.setattr(merge_failfast, "FAILFAST_POLL_SECONDS", 0.2)
    monkeypatch.setattr(merge, "resolve_suite_total_limit", lambda: None)
    monkeypatch.setattr(merge, "resolve_suite_quiet_limit", lambda: None)
    out = tmp_path / "s.out"
    cmd = f'"{sys.executable}" -m pytest test_t.py -q -rf -p no:cacheprovider --basetemp "{tmp_path / "bt"}"'
    run = merge._run_merge_suite(cmd, cmd, 1, "", d, {"PYTHONPATH": str(d), **FAST_ENV}, out)
    return run, out, cmd, d


def _gone(pid: int, seconds: float = 300.0) -> bool:
    """True once ``pid`` is gone; it polls to a deadline (a safety bound), it does not wait a fixed time."""
    end = time.monotonic() + seconds
    while psutil.pid_exists(pid):
        if time.monotonic() >= end:
            return False
        time.sleep(0.2)
    return True


def _record_kills(monkeypatch) -> list[int]:
    """Wraps ``merge._kill_tree``: each call's pid and its children, as they stood, go into the returned list
    before the real kill runs. A test checks afterwards that every one of them is gone."""
    killed: list[int] = []
    real = merge._kill_tree

    def kill(pid):
        try:
            children = [c.pid for c in psutil.Process(pid).children(recursive=True)]
        except psutil.Error:
            children = []
        killed.extend([pid, *children])
        real(pid)

    monkeypatch.setattr(merge, "_kill_tree", kill)
    return killed


def _fails_always(never: Path, after: Path) -> str:
    """``test_t`` fails in every run. The test after it writes ``after`` only when the wait for ``never`` (which no
    one writes) ends, so the suite goes on past the failure only if nothing stops it."""
    return (WAIT_FOR + "def test_a():\n    pass\n"
            "def test_t():\n    assert 0, 'boom'\n"
            f"def test_z_after():\n    wait_for({str(never)!r})\n    open({str(after)!r}, 'w').close()\n")


def _fails_once(flag: Path, rerun_ran: Path, after: Path) -> str:
    """``test_t`` fails in the suite run and leaves ``flag``. Its re-run alone finds the flag, writes ``rerun_ran``
    and passes. The test after it writes ``after`` only once that re-run has run."""
    return (WAIT_FOR + "def test_a():\n    pass\n"
            f"def test_t():\n    import os\n    if not os.path.exists({str(flag)!r}):\n"
            f"        open({str(flag)!r}, 'w').close()\n        assert 0, 'flaky'\n"
            f"    open({str(rerun_ran)!r}, 'w').close()\n"
            f"def test_z_after():\n    wait_for({str(rerun_ran)!r})\n    open({str(after)!r}, 'w').close()\n")


def test_row1_a_test_that_fails_alone_again_ends_the_run_red_at_once(tmp_path, monkeypatch):
    after = tmp_path / "ran_after"
    killed = _record_kills(monkeypatch)
    run, out, _cmd, _d = _run(tmp_path, monkeypatch, _fails_always(tmp_path / "never", after))
    assert not run.ok
    assert not after.exists()  # the remaining tests were not run: the suite was stopped while test_z_after waited
    assert killed and all(_gone(pid) for pid in killed)  # the suite's process tree is gone
    assert run.fail_fast is not None and run.fail_fast.test == "test_t.py::test_t"
    msg = run.fail_fast.message()
    assert "test_t.py::test_t" in msg and msg.count("boom") == 2  # the test and both failures
    assert "boom" in run.fail_fast.first and "boom" in run.fail_fast.again
    assert "test_t.py::test_t" in out.read_text(encoding="utf-8")  # the suite's own output carries the note


def test_row2_a_test_that_passes_alone_lets_the_suite_go_on_green(tmp_path, monkeypatch):
    after, flag, rerun_ran = tmp_path / "ran_after", tmp_path / "failed_once", tmp_path / "rerun_ran"
    run, out, cmd, d = _run(tmp_path, monkeypatch, _fails_once(flag, rerun_ran, after))
    assert run.fail_fast is None
    assert rerun_ran.exists()  # the re-run alone ran the test, and it passed
    assert after.exists()  # the suite went on to the end
    # pytest's own rc is red (T failed once); the end-of-suite step _merge_attempt takes next clears it, as it
    # always did, because T passes alone: the run's result is green
    ids = merge._failed_ids(run.output)
    assert ids == ["test_t.py::test_t"]
    assert merge._recheck_failed(cmd, ids, d, {"PYTHONPATH": str(d), **FAST_ENV}, out)[0] == []


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


def test_a_failure_confirmed_alone_kills_the_tree_once_and_names_both_failures():
    killed, reruns = [], []

    def rerun(test, stop):
        reruns.append(test)
        return False, "AssertionError: second"

    ff = FailFast(lambda: ["t.py::a", "t.py::b"], lambda: {"t.py::a": "AssertionError: first"}, rerun,
                  killed.append, interval=0.05)
    _watch(ff)
    assert killed == [4242] and reruns == ["t.py::a"]  # the first confirmed failure ends it; b is never checked
    assert ff.confirmed.test == "t.py::a"
    assert "AssertionError: first" in ff.confirmed.message() and "AssertionError: second" in ff.confirmed.message()


def test_a_failure_that_passes_alone_is_checked_once_and_kills_nothing():
    killed, reruns = [], []
    ff = FailFast(lambda: ["t.py::a"], dict, lambda t, stop: (reruns.append(t) or True, ""), killed.append, interval=0.05)
    _watch(ff)
    assert killed == [] and reruns == ["t.py::a"] and ff.confirmed is None
