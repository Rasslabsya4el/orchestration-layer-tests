"""#223 unit 3a: the merge's main paths give every failed test a verdict word.

judged_recheck (merge_load.py) with injected readings, clock and re-check: rows 3 and 4 (load / code / unclear;
waits while loaded, starts within one poll of quiet; loaded the whole 10 min gives `re-check under load`).
The wiring: a fake suite and a fake re-check inside a real git merge; the word lands in merge.log (one line per failed
test), the `merge-tests:` line (a `verdicts` key) and the suite output. No real pytest, no real sleep."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from tests._git_helper import git
from tools.agentops import detach, merge as merge_module
from tools.agentops.merge import MergeResult, SuiteRun, run_commit, run_merge
from tools.agentops import merge_failfast
from tools.agentops.merge_load import (LoadReading, RE_CHECK_UNDER_LOAD, judged_recheck, record, wait_until_quiet)

SUITE = f'"{sys.executable}" -m pytest tests -q'
A, B = "tests/test_x.py::test_a", "tests/test_x.py::test_b"


def _r(cpu, runs, at=0.0, heavy=None):
    return LoadReading(cpu_percent=cpu, runs=runs, at=at, heavy_runs=heavy)


LOADED, QUIET = _r(97.0, 3, heavy=3), _r(12.0, 0, heavy=0)


class Clock:
    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def _judge(ids, still_after, failure, readings, clock=None):
    """judged_recheck with a scripted reading per poll; returns (result, clock, order of calls)."""
    clock = clock or Clock()
    seq = list(readings)
    order: list[str] = []

    def read():
        order.append("read")
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def failure_reading():
        order.append("failure")
        return failure

    def recheck(got):
        order.append("recheck")
        assert got == ids
        return [i for i in ids if i in still_after]

    out = judged_recheck(ids, recheck, read_failure=failure_reading,
                         wait_quiet=lambda: wait_until_quiet(read, clock, clock.sleep), now_note=RE_CHECK_UNDER_LOAD)
    return out, clock, order


# -- row 3: the three words ------------------------------------------------------------------------------------

def test_row3_failed_loaded_passed_quiet_is_load():
    (still, verdicts, note), _c, _o = _judge([A], [], LOADED, [QUIET])
    assert still == [] and note == ""
    assert verdicts[A]["verdict"] == "load"
    assert verdicts[A]["cpu_percent"] == 97.0 and verdicts[A]["runs"] == 3
    assert verdicts[A]["recheck"]["cpu_percent"] == 12.0 and verdicts[A]["recheck"]["runs"] == 0


def test_row3_failed_again_is_code_and_still_failing():
    (still, verdicts, _n), _c, _o = _judge([A, B], [B], LOADED, [QUIET])
    assert still == [B]
    assert verdicts[B]["verdict"] == "code" and verdicts[A]["verdict"] == "load"


def test_row3_quiet_failure_passed_on_recheck_is_unclear():
    (still, verdicts, _n), _c, _o = _judge([A], [], _r(20.0, 0, heavy=0), [QUIET])
    assert still == [] and verdicts[A]["verdict"] == "unclear"


def test_the_failure_reading_is_taken_before_the_wait_and_the_recheck_after_it():
    _res, _c, order = _judge([A], [], LOADED, [LOADED, QUIET])
    assert order == ["failure", "read", "read", "recheck"]


# -- row 4: the wait --------------------------------------------------------------------------------------------

def test_row4_waits_while_loaded_and_starts_within_one_poll_of_quiet():
    (_still, verdicts, note), clock, order = _judge([A], [], LOADED, [LOADED, LOADED, QUIET])
    assert clock.slept == [30.0, 30.0] and clock.now == 60.0  # the re-check starts at the first quiet poll
    assert order[-1] == "recheck" and note == ""
    assert verdicts[A]["verdict"] == "load"


def test_row4_loaded_for_ten_minutes_names_the_note_and_is_never_load():
    (still, verdicts, note), clock, _o = _judge([A], [], LOADED, [LOADED])
    assert clock.now == 600.0 and note == RE_CHECK_UNDER_LOAD
    assert still == [] and verdicts[A]["verdict"] == "unclear"  # the re-check ran, but not on a quiet machine


# -- the wiring: a real merge with a fake suite -----------------------------------------------------------------

_git = git


class World:
    def __init__(self, tmp_path: Path, monkeypatch, still_after, failure=LOADED, quiet=QUIET, failing=(A, B),
                 suite_fields=None, recheck_raises=False):
        bare = tmp_path / "origin.git"
        _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
        seed = tmp_path / "seed"
        _git(tmp_path, "clone", "-q", str(bare), str(seed))
        (seed / "a.py").write_text("A = 1\n")
        (seed / "tests").mkdir()
        (seed / "tests" / "test_x.py").write_text("def test_a(): pass\n\n\ndef test_b(): pass\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", "initial")
        _git(seed, "push", "-q", "origin", "main")
        self.checkout = tmp_path / "checkout"
        _git(tmp_path, "clone", "-q", str(bare), str(self.checkout))
        worker = tmp_path / "worker"
        _git(tmp_path, "clone", "-q", str(bare), str(worker))
        _git(worker, "checkout", "-q", "-b", "feat")
        (worker / "a.py").write_text("A = 2\n")
        _git(worker, "commit", "-q", "-am", "change a")
        _git(worker, "push", "-q", "origin", "feat")
        self.lock = tmp_path / "var" / "locks" / "merge.lock"
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.log = tmp_path / "merge.log"
        self.rechecks: list[list[str]] = []
        out = "".join(f"FAILED {i} - assert 0\n" for i in failing) + f"=== {len(failing)} failed ===\n"
        monkeypatch.setattr(merge_module, "SUITE_OUTPUT_DIR", tmp_path / "suite_out")
        monkeypatch.setenv(merge_module.ENV_MERGE_LOG, str(self.log))
        monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: False)
        monkeypatch.setattr(merge_module, "machine_holds", lambda: False)
        monkeypatch.setattr(merge_module, "_with_workers", lambda cmd, is_pytest, suite_dir: (cmd, 0, ""))

        def suite(run_cmd, suite_cmd, workers, head_line, suite_dir, env, output_path, **kw):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(out, encoding="utf-8")  # the real suite leaves its output file
            return SuiteRun(False, out, 0, load_reading=failure, **(suite_fields or {}))
        monkeypatch.setattr(merge_module, "_run_merge_suite", suite)
        monkeypatch.setattr(merge_module, "_machine_reading", lambda: quiet)
        monkeypatch.setattr(merge_module, "_machine_sleep", lambda seconds: None)

        def recheck(suite_cmd, ids, suite_dir, env, output_path):
            self.rechecks.append(list(ids))
            if recheck_raises:
                raise merge_module.RecheckInconclusive("the re-run ended red with no output")
            return [i for i in ids if i in still_after], ""
        monkeypatch.setattr(merge_module, "_recheck_failed", recheck)

    def merge(self) -> MergeResult:
        return run_merge("origin/feat", SUITE, cwd=self.checkout, lock_path=self.lock, lock_timeout=60)


def test_wiring_both_pass_the_recheck_the_merge_lands_with_load_verdicts(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, still_after=[])
    result = w.merge()
    assert result.ok and result.pushed, result
    assert result.rechecked == [A, B]
    assert {k: v["verdict"] for k, v in result.verdicts.items()} == {A: "load", B: "load"}
    lines = [x for x in w.log.read_text(encoding="utf-8").splitlines() if "verdict " in x and "| origin/feat |" not in x]
    assert sorted(lines) == sorted([
        f"verdict {A}: load (others' CPU 97%, 3 runs; re-check others' CPU 12%, 0 runs)",
        f"verdict {B}: load (others' CPU 97%, 3 runs; re-check others' CPU 12%, 0 runs)"])
    assert any("0 failed after re-check of 2" in x for x in w.log.read_text(encoding="utf-8").splitlines())


def test_wiring_a_test_that_fails_again_keeps_the_red_result_and_says_code(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, still_after=[B])
    result = w.merge()
    assert not result.ok and result.reason == "red-suite" and result.failed_tests == [B]
    assert result.verdicts[B]["verdict"] == "code" and result.verdicts[A]["verdict"] == "load"
    text = w.log.read_text(encoding="utf-8")
    assert f"verdict {B}: code (others' CPU 97%, 3 runs; re-check others' CPU 12%, 0 runs)" in text
    assert f"verdict {A}: load (" in text


def test_wiring_a_loaded_machine_the_whole_wait_gives_the_note_in_the_log_line(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, still_after=[], quiet=LOADED)
    clock = Clock()
    monkeypatch.setattr(merge_module, "_machine_clock", clock)
    monkeypatch.setattr(merge_module, "_machine_sleep", clock.sleep)
    result = w.merge()
    assert result.ok and result.recheck_note == RE_CHECK_UNDER_LOAD and clock.now == 600.0
    assert {v["verdict"] for v in result.verdicts.values()} == {"unclear"}
    assert f"0 failed after re-check of 2 ({RE_CHECK_UNDER_LOAD}): {A}, {B}" in w.log.read_text(encoding="utf-8")


def test_wiring_the_human_lines_reach_the_suite_output_file(tmp_path, monkeypatch, capsys):
    w = World(tmp_path, monkeypatch, still_after=[B])
    result = w.merge()
    text = Path(result.suite_output_path).read_text(encoding="utf-8")
    assert f"verdict {A}: load (others' CPU 97%, 3 runs; re-check others' CPU 12%, 0 runs)" in text
    assert f"verdict {B}: code (others' CPU 97%, 3 runs; re-check others' CPU 12%, 0 runs)" in text
    assert f"verdict {B}: code" in capsys.readouterr().out


def test_wiring_the_commit_path_judges_the_failed_tests_and_stays_red(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, still_after=[A])
    (w.checkout / "b.py").write_text("B = 1\n")
    result = run_commit(["b.py"], "add b", cwd=w.checkout, lock_path=w.lock, lock_timeout=60, suite_cmd=SUITE)
    assert not result.ok and result.reason == "red-suite"
    assert w.rechecks == [[A, B]]
    text = w.log.read_text(encoding="utf-8")
    assert f"verdict {A}: code (" in text and f"verdict {B}: load (" in text


def test_wiring_a_fail_fast_stop_keeps_its_confirm_and_runs_no_third_run(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, still_after=[])
    confirmed = merge_failfast.Confirmed(A, "assert 0", "assert 0")
    monkeypatch.setattr(merge_module, "_run_merge_suite",
                        lambda *a, **k: SuiteRun(False, "no summary", 0, fail_fast=confirmed, load_reading=LOADED))
    result = w.merge()
    assert not result.ok and result.failed_tests == [A]
    assert w.rechecks == []  # #219: no third run; a verdict, when the suite run made one, comes from its confirm


# -- #219 fail-fast: the confirm alone is the re-check, no third run -------------------------------------------------

def _failfast_world(monkeypatch, readings, alone):
    """_failfast_rerun with scripted machine readings (taken at the first sight and at the end of the confirm) and a
    scripted run alone."""
    seq = list(readings)
    monkeypatch.setattr(merge_module, "_machine_reading", lambda: seq.pop(0))
    monkeypatch.setattr(merge_module, "_rerun_alone", lambda *a, **k: alone)
    held: dict = {}
    got = merge_module._failfast_rerun("pytest", A, Path("."), None, None, held)
    return got, held


def test_failfast_confirm_on_a_quiet_machine_says_code_with_the_log_line(tmp_path, monkeypatch):
    got, held = _failfast_world(monkeypatch, [LOADED, QUIET], (False, "assert 0"))
    assert got == (False, "assert 0")  # the answer to the fail-fast watch is untouched
    out_path = tmp_path / "suite.out"
    verdicts, out = merge_module._failfast_verdicts(merge_failfast.Confirmed(A, "assert 0", "assert 0"), held, "red\n", out_path)
    assert verdicts[A]["verdict"] == "code" and list(verdicts) == [A]
    line = f"verdict {A}: code (others' CPU 97%, 3 runs; re-check others' CPU 12%, 0 runs)"
    assert out.endswith(line + "\n") and line in out_path.read_text(encoding="utf-8")


def test_failfast_confirm_under_load_says_unclear_never_load(tmp_path, monkeypatch):
    _got, held = _failfast_world(monkeypatch, [LOADED, LOADED], (False, "assert 0"))
    verdicts, _out = merge_module._failfast_verdicts(merge_failfast.Confirmed(A, "", "assert 0"), held, "", None)
    assert verdicts[A]["verdict"] == "unclear"


def test_failfast_without_a_reading_gives_no_verdict(tmp_path):
    verdicts, out = merge_module._failfast_verdicts(merge_failfast.Confirmed(A, "", "assert 0"), {}, "red\n", None)
    assert verdicts == {} and out == "red\n"
    assert merge_module._failfast_verdicts(None, {A: (LOADED, QUIET)}, "red\n", None) == ({}, "red\n")


def test_failfast_a_confirm_that_could_not_tell_leaves_no_reading(monkeypatch):
    def raises(*a, **k):
        raise merge_failfast.NoVerdict("past the bound")
    monkeypatch.setattr(merge_module, "_machine_reading", lambda: QUIET)
    monkeypatch.setattr(merge_module, "_rerun_alone", raises)
    held: dict = {}
    try:
        merge_module._failfast_rerun("pytest", A, Path("."), None, None, held)
    except merge_failfast.NoVerdict:
        pass
    assert held == {}


def test_wiring_a_fail_fast_verdict_reaches_the_log_and_the_result_with_no_third_run(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, still_after=[])
    confirmed = merge_failfast.Confirmed(A, "assert 0", "assert 0")
    rec = record(A, LOADED, QUIET, "code")
    monkeypatch.setattr(merge_module, "_run_merge_suite",
                        lambda *a, **k: SuiteRun(False, "no summary", 0, fail_fast=confirmed, load_reading=LOADED,
                                                 verdicts={A: rec}))
    result = w.merge()
    assert not result.ok and result.failed_tests == [A]
    assert w.rechecks == [] and result.verdicts[A]["verdict"] == "code"
    assert f"verdict {A}: code (others' CPU 97%, 3 runs; re-check others' CPU 12%, 0 runs)" in w.log.read_text(encoding="utf-8")


# -- #223: the failure reading leaves the merge's own tree out ---------------------------------------------------

def test_the_machine_sampler_reads_the_others_only(monkeypatch):
    from tools.agentops import merge_load
    monkeypatch.setattr(merge_load, "MACHINE_CPU", merge_load.CpuSampler(sample=lambda: None))
    try:
        merge_module._start_machine_cpu()
        own = merge_load.MACHINE_CPU.own_busy
        assert isinstance(own, merge_module.TreeCpu) and own.pid == os.getpid()
    finally:
        merge_load.MACHINE_CPU.stop()


def test_the_own_tree_reading_counts_the_cpu_of_a_live_child_of_the_merge():
    """The CPU the sampler takes out is the merge's own tree: a suite child that burns one CPU-second shows in it.
    The child works until its own CPU clock reads 1 s, then waits for its stdin to close; the test reads the tree
    until it shows that CPU, up to a 60 s guard (the guard only ends a test that would fail)."""
    code = "import sys,time\nwhile time.process_time() < 1.0: pass\nsys.stdin.read()"
    child = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE)
    try:
        tree = merge_module.TreeCpu(os.getpid())
        deadline = time.monotonic() + 60
        while tree() < 0.9 and time.monotonic() < deadline:
            time.sleep(0.1)
        assert tree() >= 0.9
        assert tree() >= 0.9  # the level is kept, a second read never drops
    finally:
        child.stdin.close()
        child.wait(30)


def test_stats_line_carries_the_verdicts_and_detach_reads_them():
    verdicts = {A: {"verdict": "load", "cpu_percent": 97.0, "runs": 3,
                    "recheck": {"cpu_percent": 12.0, "runs": 0}}}
    result = MergeResult(ok=False, reason="red-suite", suite_duration=5.0, failed_tests=[A], verdicts=verdicts)
    stats = merge_module._stats_line(0.0, {}, result)
    data = json.loads(re.search(r"^merge-tests: (\{.*\})$", stats, re.M).group(1))
    assert data["verdicts"] == {A: {"word": "load", "cpu_percent": 97.0, "runs": 3,
                                    "recheck_cpu_percent": 12.0, "recheck_runs": 0}}
    assert data["failed_tests"] == [A]
    assert detach._tests_of(stats)["verdicts"] == data["verdicts"]


def test_stats_line_without_verdicts_keeps_today_s_keys():
    result = MergeResult(ok=False, reason="red-suite", suite_duration=5.0, failed_tests=[A])
    data = json.loads(re.search(r"^merge-tests: (\{.*\})$", merge_module._stats_line(0.0, {}, result), re.M).group(1))
    assert data == {"failed_tests": [A], "unfinished": []}


# -- #223 review: every failed test gets a word -------------------------------------------------------------------

def _words(result):
    return {k: (v["verdict"], v.get("reason", "")) for k, v in result.verdicts.items()}


def test_a_stuck_run_names_its_failed_tests_unclear_with_the_reason_stuck(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, still_after=[],
              suite_fields={"stuck": "no output for 15 s", "failed_tests": [A, B]})
    result = w.merge()
    assert not result.ok and result.reason == "stuck" and w.rechecks == []
    assert _words(result) == {A: ("unclear", "stuck"), B: ("unclear", "stuck")}
    assert f"verdict {A}: unclear (others' CPU 97%, 3 runs; stuck)" in w.log.read_text(encoding="utf-8")


def test_a_red_mod_test_gets_unclear_with_the_reason_mod_test(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, still_after=[])
    monkeypatch.setattr(merge_module, "_touched_mods", lambda *a, **k: ["pobslots"])
    monkeypatch.setattr(merge_module, "_run_mod_tests", lambda *a, **k: ([], ["pobslots"]))
    result = w.merge()
    assert not result.ok and result.failed_tests == ["mod:pobslots"]
    assert _words(result)["mod:pobslots"] == ("unclear", "mod-test")


def test_a_recheck_that_could_not_tell_leaves_every_failed_test_unclear(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, still_after=[], recheck_raises=True)
    result = w.merge()
    assert not result.ok and result.reason == "red-suite"
    assert _words(result) == {A: ("unclear", "inconclusive"), B: ("unclear", "inconclusive")}


def test_an_id_the_recheck_did_not_run_is_unclear_never_load(tmp_path, monkeypatch):
    gone = "tests/test_gone.py::test_g"
    w = World(tmp_path, monkeypatch, still_after=[], failing=(A, gone))
    result = w.merge()
    assert result.ok, result
    assert _words(result) == {A: ("load", ""), gone: ("unclear", "not-run")}


def test_the_ids_a_recheck_did_not_run_are_the_absent_files_and_the_ones_pytest_could_not_find(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_a(): pass\n")
    gone, odd = "tests/test_gone.py::test_g", "tests/test_x.py::test_odd[1]"
    out = f"ERROR: not found: {tmp_path / 'tests' / 'test_x.py'}::test_odd\n(no match in any of [<Module test_x.py>])\n"
    assert merge_module._ids_not_run([A, gone, odd], out, tmp_path) == {gone, odd}


def test_a_failure_reading_without_cpu_is_unclear_on_the_github_path(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, still_after=[], failure=_r(None, 0, heavy=0))
    result = w.merge()
    assert result.ok and _words(result) == {A: ("unclear", ""), B: ("unclear", "")}
    assert f"verdict {A}: unclear (others' CPU n/a, 0 runs; re-check others' CPU 12%, 0 runs)" in w.log.read_text(encoding="utf-8")


def test_a_fail_fast_stop_without_readings_still_names_its_test(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, still_after=[])
    confirmed = merge_failfast.Confirmed(A, "assert 0", "assert 0")
    monkeypatch.setattr(merge_module, "_run_merge_suite",
                        lambda *a, **k: SuiteRun(False, "no summary", 0, fail_fast=confirmed, load_reading=LOADED))
    result = w.merge()
    assert _words(result) == {A: ("unclear", "unjudged")}


# -- #223 unit 4: the load reading is taken when the failure first shows, not at the end of the suite -------------------

def _early_world(tmp_path, monkeypatch, clock):
    """The real ``_run_merge_suite`` over a fake ``_run_suite``: the suite lasts 46 min on ``clock``; at minute 5 the
    events file names ``A`` and the fail-fast poll reads it; ``B`` fails at minute 45, after the last poll. The
    machine is loaded for the first 10 min and quiet after."""
    suite_dir = tmp_path / "suite"
    plugin = suite_dir / "tools" / "agentops" / "suite_events_plugin.py"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("", encoding="utf-8")
    out = tmp_path / "out" / "suite.out"
    out.parent.mkdir()
    monkeypatch.setattr(merge_module, "_suite_router", lambda: None)
    monkeypatch.setattr(merge_module, "_start_machine_cpu", lambda: None)
    monkeypatch.setattr(merge_module, "_machine_reading", lambda: LOADED if clock.now < 600 else QUIET)
    events = merge_module._suite_events.events_path(out).with_name(out.name + ".events.gw0")

    def fake_run_suite(run_cmd, cwd, env, output_path, *head, stall=None, failfast=None):
        clock.now = 300.0  # minute 5
        events.write_text(f"1 F gw0 {A}\n", encoding="utf-8")
        failfast.failed_now()  # one poll of the fail-fast watch
        clock.now = 45 * 60.0
        events.write_text(f"1 F gw0 {A}\n2 F gw0 {B}\n", encoding="utf-8")
        clock.now = 46 * 60.0  # the suite ends
        return False, f"FAILED {A} - assert 0\nFAILED {B} - assert 0\n"
    monkeypatch.setattr(merge_module, "_run_suite", fake_run_suite)
    return merge_module._run_merge_suite(SUITE, SUITE, 0, "", suite_dir, None, out)


def test_a_failure_at_minute_five_keeps_the_loaded_reading_of_that_minute(tmp_path, monkeypatch):
    run = _early_world(tmp_path, monkeypatch, Clock())
    assert run.failed_tests == [A, B]
    assert run.load_reading is QUIET  # the suite end
    assert run.failure_readings == {A: LOADED}  # B failed after the last poll: no early reading
    merge_module._fill_verdicts(run.verdicts, run.failed_tests, "stuck", run.load_reading, run.failure_readings)
    assert run.verdicts[A]["line"] == f"verdict {A}: unclear (others' CPU 97%, 3 runs; stuck)"
    assert run.verdicts[A]["cpu_percent"] == 97.0 and run.verdicts[A]["loaded"] is True


def test_an_id_with_no_early_reading_falls_back_to_the_reading_at_the_suite_end(tmp_path, monkeypatch):
    run = _early_world(tmp_path, monkeypatch, Clock())
    merge_module._fill_verdicts(run.verdicts, run.failed_tests, "stuck", run.load_reading, run.failure_readings)
    assert run.verdicts[B]["line"] == f"verdict {B}: unclear (others' CPU 12%, 0 runs; stuck)"
    assert run.verdicts[B]["loaded"] is False


def test_the_judged_recheck_judges_each_id_on_its_own_failure_reading(tmp_path, monkeypatch):
    run = _early_world(tmp_path, monkeypatch, Clock())
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_a(): pass\ndef test_b(): pass\n")  # both ids are run
    monkeypatch.setattr(merge_module, "_machine_reading", lambda: QUIET)
    monkeypatch.setattr(merge_module, "_recheck_failed", lambda *a, **k: ([], ""))
    monkeypatch.setattr(merge_module, "_machine_sleep", lambda seconds: None)
    _still, _out, verdicts, _note = merge_module._judged_recheck_failed(
        SUITE, [A, B], tmp_path, None, None, run.load_reading, failure_readings=run.failure_readings)
    assert (verdicts[A]["verdict"], verdicts[A]["cpu_percent"]) == ("load", 97.0)  # loaded at minute 5, quiet re-check
    assert (verdicts[B]["verdict"], verdicts[B]["cpu_percent"]) == ("unclear", 12.0)  # quiet at the suite end


def test_wiring_a_failure_reading_taken_early_reaches_the_merge_log(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, still_after=[], failure=QUIET, suite_fields={"failure_readings": {A: LOADED}})
    result = w.merge()
    assert result.ok, result
    assert _words(result) == {A: ("load", ""), B: ("unclear", "")}
    text = w.log.read_text(encoding="utf-8")
    assert f"verdict {A}: load (others' CPU 97%, 3 runs; re-check others' CPU 12%, 0 runs)" in text
    assert f"verdict {B}: unclear (others' CPU 12%, 0 runs; re-check others' CPU 12%, 0 runs)" in text
