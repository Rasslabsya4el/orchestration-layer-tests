"""#223 unit 3b: the line check, prequeue and the red-batch triage wait for a quiet machine before they re-check
failed tests, and give each failed test the verdict word (`load` / `code` / `unclear`).

Readings and the clock are injected; no real sleep, no machine read. The line check is driven through its state
machine (`_advance`) with a fake child; prequeue runs a real tmp repo (as tests/test_prequeue.py does) with the
failure reading replaced; the triage runs with a fake re-check."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.test_merge_red_batch_ids import quiet_machine
from tests.test_prequeue import GREEN_A, RED_A, _change_a, _fixture, _flaky, _run
from tools.agentops import line_check, merge as merge_module
from tools.agentops.line_check import LineCheck, _Prepared, read_status
from tools.agentops.merge_load import LoadReading

pytestmark = pytest.mark.usefixtures("github_suite_off")  # #343 u6: no run here reaches GitHub

A, B = "tests/test_x.py::test_a", "tests/test_x.py::test_b"
FAILED_OUTPUT = f"FAILED {A} - assert 1\nFAILED {B} - assert 2\n=== 2 failed ===\n"


def _r(cpu, runs, heavy):
    return LoadReading(cpu_percent=cpu, runs=runs, heavy_runs=heavy, at=0.0)


LOADED, QUIET = _r(97.0, 3, 3), _r(10.0, 0, 0)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Child:
    """A finished child process: poll() says its exit code."""

    def __init__(self, rc):
        self.rc, self.pid = rc, 4242

    def poll(self):
        return self.rc


class Rig:
    """A LineCheck whose first run just ended red on A and B; the re-check child ends with `recheck_rc` and prints
    `recheck_text`. `readings` answers the machine reads in order (the last one repeats)."""

    def __init__(self, tmp_path, monkeypatch, readings, recheck_rc, recheck_text=""):
        self.clock, self.readings, self.reads = Clock(), list(readings), []
        self.started: list[tuple[str, float]] = []
        repo = tmp_path / "repo"
        repo.mkdir()
        self.out = tmp_path / "check.out"
        self.out.write_text(FAILED_OUTPUT, encoding="utf-8")
        self.chk = LineCheck(repo, tmp_path / "locks" / "merge.lock", "1-1-1", "branch-b", "sha", "pytest tests",
                             read_machine=self._read, clock=self.clock)
        prep = _Prepared()
        prep.output_path, prep.check_dir = self.out, tmp_path
        self.chk._prep, self.chk._phase, self.chk.state = prep, "run", "running"
        self.chk._proc = Child(1)

        def start_child(cmd):
            self.started.append((cmd, self.clock()))
            with open(self.out, "a", encoding="utf-8") as fh:
                fh.write(recheck_text)
            self.chk._proc = Child(recheck_rc)
            self.chk.state = "running"

        monkeypatch.setattr(self.chk, "_start_child", start_child)

    def _read(self):
        self.reads.append(self.clock())
        return self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]

    def poll_until_started(self, step, limit):
        """Polls every `step` seconds of the injected clock until the re-check child is started or `limit` passed."""
        t0 = self.clock()
        outcome = self.chk._advance()
        while not self.started and self.clock() - t0 < limit:
            self.clock.now += step
            outcome = self.chk._advance()
        return outcome

    def status(self):
        return read_status(self.chk.lock_path, "1-1-1")


# -- row 3 + 4 in the line check ---------------------------------------------------------------------------------

def test_a_failed_test_passing_on_recheck_after_load_waits_for_quiet_and_is_load(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, [LOADED, LOADED, LOADED, QUIET], recheck_rc=0)
    start = rig.clock()
    rig.poll_until_started(step=5.0, limit=120.0)
    assert len(rig.started) == 1
    assert rig.started[0][1] - start == 60.0  # polls at 0 and 30 read loaded, the one at 60 reads quiet
    assert rig.chk.state == "running"  # the re-check child runs now
    outcome = rig.chk._advance()  # the child ended green
    assert outcome is None and rig.chk.state == "green"
    status = rig.status()
    assert status["state"] == "green"
    assert status["verdicts"][A]["word"] == "load" and status["verdicts"][B]["word"] == "load"
    text = rig.out.read_text(encoding="utf-8")
    assert re.search(rf"verdict {re.escape(A)}: load \(others' CPU 97%, 3 runs; re-check others' CPU 10%, 0 runs\)", text)
    assert "re-check under load" not in text


def test_the_recheck_starts_within_one_poll_of_quiet(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, [LOADED, LOADED, QUIET], recheck_rc=0)
    start = rig.clock()
    rig.poll_until_started(step=1.0, limit=120.0)
    quiet_read_at = rig.reads[-1]
    assert rig.started[0][1] == quiet_read_at  # started in the poll that read quiet
    assert quiet_read_at - start == 30.0       # one reading per 30 s poll: failure at 0, loaded at 0, quiet at 30


def test_loaded_for_ten_minutes_rechecks_at_ten_minutes_and_says_so(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, [LOADED], recheck_rc=0)
    start = rig.clock()
    rig.poll_until_started(step=10.0, limit=1200.0)
    assert rig.started[0][1] - start == 600.0
    assert rig.chk._advance() is None and rig.chk.state == "green"
    status = rig.status()
    assert status["note"] == "re-check under load"
    assert status["verdicts"][A]["word"] == "unclear" and status["verdicts"][B]["word"] == "unclear"
    assert "re-check under load" in rig.out.read_text(encoding="utf-8")


def test_a_test_that_fails_again_on_a_quiet_machine_is_code_and_the_outcome_carries_it(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, [LOADED, QUIET], recheck_rc=1, recheck_text=f"FAILED {B} - assert 2\n")
    rig.poll_until_started(step=30.0, limit=120.0)
    outcome = rig.chk._advance()
    assert outcome is not None and outcome.red and outcome.ids == [B]
    assert outcome.verdicts[B]["verdict"] == "code" and outcome.verdicts[A]["verdict"] == "load"
    assert rig.chk.state == "red" and rig.status()["verdicts"][B]["word"] == "code"


def test_the_wait_does_not_block_the_waiters_loop(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, [LOADED], recheck_rc=0)
    assert rig.chk._advance() is None
    assert rig.started == [] and rig.chk.state == "running"  # busy: returned at once, the child not yet started
    reads = len(rig.reads)
    assert rig.chk._advance() is None and len(rig.reads) == reads  # between polls it reads nothing


# -- prequeue ----------------------------------------------------------------------------------------------------

def _failure_reads(monkeypatch, reading):
    """The merge suite's failure reading (SuiteRun.load_reading) is `reading`."""
    real = merge_module._run_merge_suite

    def wrapped(*args, **kwargs):
        run = real(*args, **kwargs)
        if not run.ok:
            run.load_reading = reading
        return run

    monkeypatch.setattr(merge_module, "_run_merge_suite", wrapped)


def test_prequeue_prints_the_verdict_word_of_a_test_that_passes_on_recheck(tmp_path, monkeypatch, capsys):
    quiet_machine(monkeypatch)  # #261: the re-check never waits on the real PC load
    monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: True)
    _failure_reads(monkeypatch, LOADED)
    repo, _base = _fixture(tmp_path, with_map=False)
    _flaky(repo, tmp_path)
    _change_a(repo, GREEN_A)
    (tmp_path / "merge.log").write_text("")
    rc, lines = _run(repo, tmp_path, capsys)
    assert rc == 0
    assert any(re.match(r"verdict tests/test_flaky\.py::test_flaky: load \(others' CPU 97%, 3 runs; re-check ", line)
               for line in lines), lines
    assert "prequeue: green after re-check of 1: tests/test_flaky.py::test_flaky" in lines


def test_prequeue_prints_the_verdict_word_of_each_failing_test(tmp_path, monkeypatch, capsys):
    quiet_machine(monkeypatch)  # #261: the re-check never waits on the real PC load
    monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: True)
    repo, _base = _fixture(tmp_path, with_map=True)
    _change_a(repo, RED_A)
    rc, lines = _run(repo, tmp_path, capsys)
    assert rc == 1
    verdict_lines = [line for line in lines if line.startswith("verdict ")]
    failing = [line.strip().removeprefix("FAILED ") for line in lines if line.startswith("  FAILED ")]
    assert failing and len(verdict_lines) == len(failing)
    for test_id in failing:
        assert any(line.startswith(f"verdict {test_id}: code (") for line in verdict_lines), (test_id, verdict_lines)
    assert "prequeue: red, failing tests:" in lines


# -- the red-batch triage ----------------------------------------------------------------------------------------

def test_the_red_batch_triage_gives_a_verdict_per_id(tmp_path, monkeypatch):
    from tests import test_merge_red_batch_ids as batch
    batch.quiet_machine(monkeypatch)  # #261: no test waits on the real PC load
    class Lock:
        def heartbeat(self):
            pass

    monkeypatch.setattr(merge_module, "ensure_suite_worktree", lambda *a, **k: tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(merge_module, "_recheck_failed",
                        lambda suite_cmd, ids, suite_dir, env, out: ([B] if B in ids else [], ""))
    out = tmp_path / "suite.out"
    out.write_text("", encoding="utf-8")
    verdicts: dict = {}
    on_base, culprits = merge_module._triage_red_batch(
        tmp_path, "base", [], "pytest tests", [A, B], tmp_path, None, out, (), Lock(),
        failure=LOADED, verdicts=verdicts)
    assert on_base == [B] and culprits == {}
    assert verdicts[A]["verdict"] == "load" and verdicts[B]["verdict"] == "code"
    text = out.read_text(encoding="utf-8")
    assert f"verdict {A}: load" in text and f"verdict {B}: code" in text


def test_the_red_batch_triage_waits_for_a_quiet_machine_before_the_rerun(tmp_path, monkeypatch):
    class Lock:
        def heartbeat(self):
            pass

    order, readings, clock = [], [LOADED, QUIET], Clock()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(merge_module, "ensure_suite_worktree", lambda *a, **k: tmp_path)
    monkeypatch.setattr(merge_module, "_machine_reading", lambda: order.append("read") or readings.pop(0))
    monkeypatch.setattr(merge_module, "_machine_clock", clock)
    monkeypatch.setattr(merge_module, "_machine_sleep",
                        lambda s: order.append(f"sleep {s:g}") or setattr(clock, "now", clock.now + s))
    monkeypatch.setattr(merge_module, "_recheck_failed", lambda *a: order.append("recheck") or ([], ""))
    out = tmp_path / "suite.out"
    out.write_text("", encoding="utf-8")
    verdicts: dict = {}
    merge_module._triage_red_batch(tmp_path, "base", [], "pytest tests", [A], tmp_path, None, out, (), Lock(),
                                   failure=LOADED, verdicts=verdicts)
    assert order == ["read", "sleep 30", "read", "recheck"]
    assert verdicts[A]["verdict"] == "load"


# -- review round: hook, result JSON, merge.log, triage results, stuck, early sleep, errors ----------------------------

def _hook(tmp_path, rig, log):
    hook = merge_module._LineCheckHook(
        tmp_path / "repo", tmp_path / "locks" / "merge.lock", "branch-b", "pytest tests", remote="origin",
        main="main", suite_env=None, suite_shared=(), suite_output_dir=tmp_path, merge_log_path=log)
    hook.chk = rig.chk
    return hook


def _count(text, prefix):
    return sum(1 for line in text.splitlines() if line.startswith(prefix))


def _as_result(posted: dict):
    known = merge_module.MergeResult.__dataclass_fields__
    return merge_module.MergeResult(**{k: v for k, v in posted.items() if k in known})


def test_a_red_on_branch_result_carries_the_verdicts_and_merge_log_and_result_json_hold_each_word_once(
        tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, [LOADED, QUIET], recheck_rc=1, recheck_text=f"FAILED {B} - assert 2\n")
    log = tmp_path / "merge.log"
    hook = _hook(tmp_path, rig, log)
    assert hook._turn("1-1-1", True, False) is None  # the wait ended at once, the re-check child is started
    red = hook._turn("1-1-1", True, False)
    assert red is not None and red["reason"] == "red-on-branch"
    assert red["verdicts"][A]["verdict"] == "load" and red["verdicts"][B]["verdict"] == "code"
    result = _as_result(red)
    merge_module._append_merge_log("branch-b", result, log_path=log)
    text = log.read_text(encoding="utf-8")
    assert _count(text, f"verdict {A}: load") == 1 and _count(text, f"verdict {B}: code") == 1
    tests_line = merge_module._stats_line(0.0, {}, result).splitlines()[0]
    tests = json.loads(tests_line.removeprefix(merge_module.TESTS_LINE_PREFIX))
    assert tests["verdicts"][A]["word"] == "load" and tests["verdicts"][B]["word"] == "code"


def test_a_red_on_branch_result_after_the_cap_names_the_note(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, [LOADED], recheck_rc=1, recheck_text=f"FAILED {B} - assert 2\n")
    hook = _hook(tmp_path, rig, tmp_path / "merge.log")
    rig.poll_until_started(step=10.0, limit=1200.0)
    red = hook._turn("1-1-1", True, False)
    assert "re-check under load" in red["message"] and red["recheck_note"] == "re-check under load"
    assert red["verdicts"][B]["verdict"] == "unclear"


def test_a_green_recheck_writes_its_verdict_lines_to_merge_log_once(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, [LOADED, QUIET], recheck_rc=0)
    log = tmp_path / "merge.log"
    hook = _hook(tmp_path, rig, log)
    hook._turn("1-1-1", True, False)
    assert hook._turn("1-1-1", True, False) is None and rig.chk.state == "green"
    hook._turn("1-1-1", True, False)
    text = log.read_text(encoding="utf-8")
    assert _count(text, f"verdict {A}: load") == 1 and _count(text, f"verdict {B}: load") == 1


def test_the_red_batch_culprit_result_carries_the_verdicts_of_its_ids(tmp_path, monkeypatch):
    from tests import test_merge_red_batch_ids as batch
    _result, posted, log, _bare, refs, _triage, _calls = batch._run_red_batch(tmp_path, monkeypatch)
    charlie = [r for b, r in posted if b == refs[2]][0]
    assert sorted(charlie["verdicts"]) == sorted(batch.RED_IDS)
    assert {rec["verdict"] for rec in charlie["verdicts"].values()} == {"code"}
    for test_id in batch.RED_IDS:
        assert _count(log, f"verdict {test_id}: code") == 1, test_id


def test_a_red_on_base_result_carries_the_verdicts_and_the_log_holds_them(tmp_path, monkeypatch):
    from tests import test_merge_red_batch_ids as batch
    real_world, real_branch = batch._world, batch._branch

    def world(path):
        bare, checkout, lock = real_world(path)
        (checkout / "tests").mkdir(exist_ok=True)
        (checkout / batch.ADDED).write_text(batch._added_file_text(), encoding="utf-8")
        batch._git(checkout, "add", "-A")
        batch._git(checkout, "commit", "-q", "-m", "red on main")
        batch._git(checkout, "push", "-q", "origin", "main")
        return bare, checkout, lock

    def branch(path, bare, name, relpath, text):
        if relpath == batch.ADDED:  # the red file is already on main: charlie adds a plain file
            relpath, text = "notes/charlie.txt", "charlie\n"
        return real_branch(path, bare, name, relpath, text)

    monkeypatch.setattr(batch, "_world", world)
    monkeypatch.setattr(batch, "_branch", branch)
    result, _posted, log, _bare, _refs, _triage, _calls = batch._run_red_batch(tmp_path, monkeypatch)
    assert result.reason == "red-on-base", result
    assert sorted(result.verdicts) == sorted(batch.RED_IDS)
    for test_id in batch.RED_IDS:
        assert _count(log, f"verdict {test_id}: code") == 1, test_id


def test_prequeue_on_a_stuck_run_prints_unclear_stuck_per_failed_test(tmp_path, monkeypatch, capsys):
    quiet_machine(monkeypatch)  # #261: the re-check never waits on the real PC load
    monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: True)
    repo, _base = _fixture(tmp_path, with_map=False)
    _change_a(repo, GREEN_A)
    (tmp_path / "merge.log").write_text("")
    monkeypatch.setattr(merge_module, "_run_merge_suite",
                        lambda *a, **k: merge_module.SuiteRun(False, "", 1, stuck="no output for 5 min",
                                                              failed_tests=[A, B], load_reading=LOADED))
    rc, lines = _run(repo, tmp_path, capsys)
    assert rc == 1
    assert f"verdict {A}: unclear (others' CPU 97%, 3 runs; stuck)" in lines
    assert f"verdict {B}: unclear (others' CPU 97%, 3 runs; stuck)" in lines
    assert any(line.startswith("prequeue: red, the run went silent") for line in lines)


def test_wait_until_quiet_sleeps_only_the_remaining_time_when_a_sleep_returns_early():
    from tools.agentops.merge_load import wait_until_quiet
    clock = Clock()
    asked = []

    def short_sleep(seconds):  # returns after at most 10 s whatever was asked
        asked.append(seconds)
        clock.now += min(seconds, 10.0)

    t0 = clock()
    _reading, note = wait_until_quiet(lambda: LOADED, clock, short_sleep)
    assert note == "re-check under load" and clock() - t0 == 600.0
    assert max(asked) <= 30.0 and asked[:3] == [30.0, 20.0, 10.0]


def test_failed_again_after_the_cap_is_unclear_and_failed_again_on_a_quiet_recheck_is_code(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    again = f"FAILED {B} - assert 2\n"
    capped = Rig(tmp_path / "a", monkeypatch, [LOADED], recheck_rc=1, recheck_text=again)
    capped.poll_until_started(step=10.0, limit=1200.0)
    out_capped = capped.chk._advance()
    assert out_capped.verdicts[B]["verdict"] == "unclear" and out_capped.note == "re-check under load"
    quiet = Rig(tmp_path / "b", monkeypatch, [LOADED, QUIET], recheck_rc=1, recheck_text=again)
    quiet.poll_until_started(step=30.0, limit=120.0)
    out_quiet = quiet.chk._advance()
    assert out_quiet.verdicts[B]["verdict"] == "code" and out_quiet.note == ""


def test_a_failed_verdict_write_is_a_note_of_the_outcome_not_silence(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, [LOADED, QUIET], recheck_rc=1, recheck_text=f"FAILED {B} - assert 2\n")
    rig.poll_until_started(step=30.0, limit=120.0)
    real_open = open

    def refusing(path, mode="r", *a, **k):
        if "a" in mode:
            raise PermissionError("denied")
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr(line_check, "open", refusing, raising=False)
    outcome = rig.chk._advance()
    assert outcome.verdicts[B]["verdict"] == "code"
    assert "verdict lines not written" in outcome.note and "denied" in outcome.note


def test_a_failed_sampler_start_reads_n_a_and_is_noted(tmp_path, monkeypatch):
    chk = LineCheck(tmp_path / "repo", tmp_path / "locks" / "merge.lock", "1-1-1", "branch-b", "sha", "pytest tests")

    def broken():
        raise RuntimeError("no psutil")

    monkeypatch.setattr(merge_module, "_start_machine_cpu", broken)
    chk._start_cpu()
    reading = chk._machine()
    assert reading.cpu_percent is None and not reading.quiet
    assert "cpu sampler not started" in chk.notes() and "no psutil" in chk.notes()
