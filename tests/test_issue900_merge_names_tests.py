"""#900: a failed or stuck merge suite names the failed tests and the ones still running; the failed count is one
number (status line, result file, merge.log). Temp suites run through the merge tool's own suite function; no heavy tool."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from tests.test_merge_red_batch_ids import quiet_machine
from tools.agentops import detach, merge, merge_status
from tools.agentops.merge import MergeResult, _append_merge_log, _stats_line

SRC = Path(merge.__file__).parent

SUITE = {
    "fail_a": "assert 0",
    "fail_b": "assert 0",
    "ok_1": "pass", "ok_2": "pass", "ok_3": "pass",
}


def _suite_dir(tmp_path: Path, names: list[str], hang: bool, start_delay: float = 0.0) -> Path:
    d = tmp_path / "suite"
    (d / "tools" / "agentops").mkdir(parents=True)
    (d / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (d / "tools" / "agentops" / "__init__.py").write_text("", encoding="utf-8")
    for f in ("suite_events_plugin.py",):
        shutil.copy(SRC / f, d / "tools" / "agentops" / f)
    body = "".join(f"def test_{n}():\n    {SUITE[n]}\n" for n in names)
    if hang:
        body += "def test_sleeper():\n    import time\n    time.sleep(600)\n"
    (d / "test_t.py").write_text(body, encoding="utf-8")
    if start_delay:  # a slow start-up: every pytest process (controller and workers) is silent this long before its first test starts
        (d / "conftest.py").write_text(f"import time\ntime.sleep({start_delay})\n", encoding="utf-8")
    return d


def _stall_after_start_up(monkeypatch, out: Path, failures: int, give_up: float = 300.0) -> None:
    """The quiet window of a real StallWatch opens when the suite starts, so on a loaded PC a slow start-up can eat it
    before any worker ran a test (#273). The window here stays shut (restarts at every tick) until the workers' events
    show the stuck state: ``failures`` failed tests and a test started and not finished; after ``give_up`` seconds
    the real rule runs anyway, so a suite that never gets there still ends."""
    real = merge.StallWatch

    class Gated(real):
        def tick(self, text, cpu_now, now=None):
            failed, unfinished = merge._suite_events.read_events(out)
            if len(failed) < failures or not unfinished:
                if (self.clock() if now is None else now) - self._start < give_up:
                    self._q_sig = None
                    return ""
            return super().tick(text, cpu_now, now)

    monkeypatch.setattr(merge, "StallWatch", Gated)


def _run(tmp_path, monkeypatch, names, hang, quiet=None, start_delay=0.0):
    quiet_machine(monkeypatch)  # #261: a failed run's reading never waits on the real PC load
    d = _suite_dir(tmp_path, names, hang, start_delay)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)  # the merge suite sets `-p tools.agentops.suite_plan_plugin`; the temp suite holds only its own plugin
    quiet = quiet or (15.0 if hang else 90.0)  # a run that ends alone is spared; startup and a first traceback can be slow
    out = tmp_path / "s.out"
    if hang:
        _stall_after_start_up(monkeypatch, out, failures=sum(n.startswith("fail") for n in names))
    monkeypatch.setattr(merge, "STALL_POLL_SECONDS", 0.5)
    monkeypatch.setattr(merge, "_rerun_alone", lambda *a, **k: (True, ""))  # #219: these failures pass alone, so the stall is what ends the run
    monkeypatch.setattr(merge, "resolve_suite_total_limit", lambda: None)
    monkeypatch.setattr(merge, "resolve_suite_quiet_limit", lambda: quiet)
    cmd = f'"{sys.executable}" -m pytest test_t.py -q -rf -n 2 -p no:cacheprovider --basetemp "{tmp_path / "bt"}"'
    env = {"PYTHONPATH": str(d)}
    run = merge._run_merge_suite(cmd, cmd, 2, "", d, env, out)
    return run, out


def test_a_stuck_suite_names_the_failed_and_the_unfinished_tests(tmp_path, monkeypatch):
    run, out = _run(tmp_path, monkeypatch, ["fail_a", "fail_b", "ok_1", "ok_2", "ok_3"], hang=True)
    assert not run.ok and run.stuck.startswith("stalled:")
    assert sorted(run.failed_tests) == ["test_t.py::test_fail_a", "test_t.py::test_fail_b"]
    assert run.unfinished == ["test_t.py::test_sleeper"]
    assert "unfinished: test_t.py::test_sleeper" in run.stuck
    res = MergeResult(ok=False, reason="stuck", message=run.stuck, suite_duration=5.0,
                      failed_count=len(run.failed_tests), failed_tests=run.failed_tests, unfinished=run.unfinished)
    stats = _stats_line(0.0, {}, res)
    assert "failed=2" in stats
    rec = {**detach._stats_of(stats), **detach._tests_of(stats)}
    assert rec["failed"] == 2 and rec["failed_tests"] == run.failed_tests and rec["unfinished"] == run.unfinished


def test_a_slow_start_is_no_stall_in_the_stuck_test(tmp_path, monkeypatch):
    """The cause of the stuck test's flake (#273): the quiet window opens when the suite starts, so on a loaded PC a
    start-up longer than the quiet limit ended the run before a worker ran a test (failed_tests empty). A start-up
    of 5 s (twice: controller, worker) against a 3 s quiet limit makes that certain; the helper keeps the window shut until the stuck state."""
    run, _ = _run(tmp_path, monkeypatch, ["fail_a", "fail_b", "ok_1"], hang=True, quiet=3.0, start_delay=5.0)
    assert run.stuck.startswith("stalled:")
    assert sorted(run.failed_tests) == ["test_t.py::test_fail_a", "test_t.py::test_fail_b"]
    assert run.unfinished == ["test_t.py::test_sleeper"]


def test_a_red_suite_names_the_failures_and_all_three_counts_agree(tmp_path, monkeypatch):
    run, out = _run(tmp_path, monkeypatch, ["fail_a", "fail_b", "ok_1", "ok_2", "ok_3"], hang=False)
    assert not run.ok and run.stuck == "" and run.unfinished == []
    assert sorted(run.failed_tests) == ["test_t.py::test_fail_a", "test_t.py::test_fail_b"]
    assert sorted(merge._failed_ids(run.output)) == sorted(run.failed_tests)   # -rf names them too
    res = MergeResult(ok=False, reason="red-suite", suite_duration=5.0, failed_count=len(run.failed_tests),
                      failed_tests=run.failed_tests, suite_output_path=str(out))
    stats = _stats_line(0.0, {}, res)
    log = tmp_path / "merge.log"
    _append_merge_log("orch-900", res, log)
    assert "failed=2" in stats and "| 2 failed |" in log.read_text(encoding="utf-8")
    assert merge_status._progress(out.read_text(encoding="utf-8"))[1] == 2


def test_a_green_suite_has_empty_lists_and_a_zero_count(tmp_path, monkeypatch):
    run, out = _run(tmp_path, monkeypatch, ["ok_1", "ok_2", "ok_3"], hang=False)
    assert run.ok and run.failed_tests == [] and run.unfinished == []
    stats = _stats_line(0.0, {}, MergeResult(ok=True, reason="pushed", suite_duration=5.0))
    rec = {**detach._stats_of(stats), **detach._tests_of(stats)}
    assert rec["failed"] == 0 and rec["failed_tests"] == [] and rec["unfinished"] == []


def test_a_restart_poll_line_inside_a_progress_line_hides_no_f():
    text = ("........................F...............................restart poll: k_new 6, k 6, 100% done -> keep\n"
            "................ [ 36%]\n"
            ".F.........................F....................F....F.................. [ 53%]\n")
    assert merge_status._progress(text) == (53, 5)


def test_two_workers_writing_at_once_lose_no_line(tmp_path):
    """The cause of a passed test read as unfinished (#900, 2026-10-12): on Windows one O_APPEND file shared by two
    processes loses lines (measured 9349 of 10000). Every process has its file now; the reader joins them."""
    import os
    import subprocess

    from tools.agentops import suite_events, suite_events_plugin

    out = tmp_path / "s.out"
    n = 3000
    code = ("import sys; from tools.agentops import suite_events_plugin as p\n"
            "for i in range(%d):\n"
            "    n = 't.py::test_%%d' %% i\n"
            "    p._write('S', n); p._write('E', n)\n" % n)
    root = str(Path(suite_events_plugin.__file__).parents[2])
    procs = [subprocess.Popen([sys.executable, "-c", code], cwd=root,
                              env={**os.environ, "PYTEST_XDIST_WORKER": w,
                                   suite_events.ENV_EVENTS: str(suite_events.events_path(out)), "PYTHONPATH": root})
             for w in ("gw0", "gw1")]
    assert [p.wait(timeout=120) for p in procs] == [0, 0]
    lines = sum(len(f.read_text(encoding="utf-8").splitlines()) for f in suite_events.event_files(out))
    assert lines == 4 * n
    assert suite_events.read_events(out) == ([], [])
