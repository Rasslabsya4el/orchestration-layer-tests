"""#223: a failed merge test says whether the machine was loaded or the code broke (tools/agentops/merge_load.py).

Row 3: the verdict words from injected readings, and the raw numbers in the record.
Row 4: wait_until_quiet with an injected clock and sleep (no real waiting).
Row 5: the count of other pytest sessions sees a pytest started from another folder, and not the caller's own tree
(its xdist run and that run's workers). Row 5 starts real processes; every wait on them is bounded."""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
import time

import psutil
import pytest

from tools.agentops.merge_load import (
    CpuSampler,
    LoadReading,
    ProcInfo,
    count_other_pytest_sessions,
    failfast_verdict,
    heavy_run_pids,
    human_line,
    other_pytest_run_pids,
    process_table,
    read_load,
    record,
    judged_recheck,
    unjudged,
    verdict,
    verdict_line,
    verdict_summary,
    wait_until_quiet,
)

# A child process started with no console window of its own (Windows); a no-op elsewhere.
_HIDDEN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _reading(cpu, runs, at=0.0, heavy=None):
    """A reading; without `heavy` every other run counts as heavy (the rule for a run whose tests are not known)."""
    return LoadReading(cpu_percent=cpu, runs=runs, at=at, heavy_runs=heavy)


class FakeClock:
    """Seconds that advance only when something sleeps (or when a test sets `now`)."""

    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def _script(readings):
    """A read() that returns the scripted readings in order, then the last one for ever."""
    seq = list(readings)

    def read():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return read


def proc(pid, ppid, *cmd):
    return ProcInfo(pid=pid, ppid=ppid, cmdline=tuple(cmd))


def _end(popen: subprocess.Popen) -> None:
    if popen.poll() is None:
        popen.terminate()
    popen.wait(timeout=30)


# --- row 3: the reading and the verdict ------------------------------------------------------------------------------


def test_reading_is_loaded_by_cpu_or_by_two_runs():
    assert _reading(95, 0).loaded
    assert _reading(20, 2).loaded
    assert not _reading(84.9, 1, heavy=0).loaded
    assert _reading(None, 2).loaded  # an unreadable CPU does not hide the runs


def test_reading_is_quiet_only_with_low_cpu_and_no_heavy_run():
    assert _reading(20, 0).quiet
    assert _reading(20, 2, heavy=0).quiet  # light runs do not make a machine busy for the wait
    assert not _reading(20, 1).quiet  # a run of unknown size is heavy
    assert not _reading(85, 0).quiet
    assert not _reading(95, 0).quiet


def test_row3_failed_under_load_and_passed_quiet_is_load():
    assert verdict(_reading(95, 3), True, _reading(12, 0)) == "load"


def test_row3_failed_again_on_the_quiet_recheck_is_code():
    assert verdict(_reading(95, 3), False, _reading(12, 0)) == "code"
    assert verdict(_reading(20, 0), False, _reading(12, 0)) == "code"


def test_failed_again_with_the_recheck_under_load_is_unclear_like_the_failfast_confirm():
    assert verdict(_reading(95, 3), False, _reading(92, 0)) == "unclear"
    assert verdict(_reading(95, 3), False, _reading(12, 1)) == "unclear"  # a heavy run beside the re-check
    assert verdict(_reading(95, 3), False, _reading(92, 0)) == failfast_verdict(_reading(95, 3), _reading(92, 0))


def test_a_failure_reading_without_cpu_is_unclear_even_with_runs_and_a_quiet_recheck():
    assert verdict(_reading(None, 3), True, _reading(12, 0)) == "unclear"
    assert verdict(_reading(None, 0), True, _reading(12, 0)) == "unclear"
    assert verdict(_reading(None, 0), False, _reading(12, 0)) == "code"  # it failed again on a quiet machine


def test_a_reading_without_cpu_is_not_quiet():
    assert not _reading(None, 0, heavy=0).quiet
    assert not _reading(None, 0).quiet


def test_the_wait_goes_on_while_the_cpu_cannot_be_read_and_ends_on_the_first_real_reading():
    clock = FakeClock()
    read = _script([_reading(None, 0), _reading(None, 0), _reading(20, 0)])
    reading, note = wait_until_quiet(read, clock, clock.sleep, poll_seconds=30)
    assert (reading.cpu_percent, note, clock.now) == (20, "", 60.0)


def test_the_wait_with_no_cpu_reading_at_all_ends_at_600_seconds_with_the_note():
    clock = FakeClock()
    reading, note = wait_until_quiet(lambda: _reading(None, 0), clock, clock.sleep, poll_seconds=30)
    assert (note, clock.now) == ("re-check under load", 600.0)


def test_row3_quiet_failure_passed_on_recheck_is_unclear():
    assert verdict(_reading(20, 1), True, _reading(12, 0)) == "unclear"


def test_recheck_with_one_heavy_run_and_low_cpu_is_unclear():
    assert verdict(_reading(95, 3), True, _reading(12, 1)) == "unclear"


def test_recheck_with_one_light_run_still_gives_load_when_the_failure_was_loaded():
    assert verdict(_reading(95, 3), True, _reading(12, 1, heavy=0)) == "load"


def test_recheck_itself_under_load_is_unclear():
    assert verdict(_reading(95, 3), True, _reading(90, 0)) == "unclear"


def test_record_holds_the_raw_numbers_and_one_line():
    rec = record("tests/x.py::test_a", _reading(95, 3, at=100.0), _reading(12, 1, at=400.0, heavy=0), "load")
    assert rec["test"] == "tests/x.py::test_a"
    assert rec["verdict"] == "load"
    assert (rec["cpu_percent"], rec["runs"], rec["loaded"]) == (95, 3, True)
    assert (rec["recheck"]["cpu_percent"], rec["recheck"]["runs"], rec["recheck"]["loaded"]) == (12, 1, False)
    assert rec["line"] == "verdict tests/x.py::test_a: load (others' CPU 95%, 3 runs; re-check others' CPU 12%, 1 run)"
    assert rec["line"] == verdict_line(rec)  # one human format for the record and the log line


def test_human_line_without_a_recheck():
    assert human_line("t", _reading(20, 1), "unclear") == "verdict t: unclear (others' CPU 20%, 1 run)"


def test_a_reading_without_cpu_reads_n_a_in_the_one_format():
    rec = record("t", _reading(None, 1), _reading(12, 0), "unclear")
    assert rec["line"] == verdict_line(rec) == "verdict t: unclear (others' CPU n/a, 1 run; re-check others' CPU 12%, 0 runs)"


def test_an_unjudged_test_is_unclear_with_a_one_word_reason_in_the_record_and_the_line():
    rec = unjudged("t", "stuck", _reading(95, 3))
    assert (rec["verdict"], rec["reason"], rec["recheck"]) == ("unclear", "stuck", None)
    assert rec["line"] == verdict_line(rec) == "verdict t: unclear (others' CPU 95%, 3 runs; stuck)"
    assert verdict_summary(rec) == {"word": "unclear", "cpu_percent": 95, "runs": 3, "recheck_cpu_percent": None,
                                    "recheck_runs": None, "reason": "stuck"}
    assert unjudged("t", "stuck", None)["line"] == "verdict t: unclear (others' CPU n/a, 0 runs; stuck)"


def test_a_judged_summary_keeps_today_s_keys():
    rec = record("t", _reading(95, 3), _reading(12, 0), "load")
    assert set(verdict_summary(rec)) == {"word", "cpu_percent", "runs", "recheck_cpu_percent", "recheck_runs"}


def test_ids_the_recheck_did_not_run_are_unclear_never_load():
    loaded, quiet = _reading(95, 3), _reading(12, 0)
    still, verdicts, note = judged_recheck(["a", "b", "c"], lambda ids: ["c"], read_failure=lambda: loaded,
                                           wait_quiet=lambda: (quiet, ""), not_run=lambda: {"b", "c"})
    assert still == ["c"] and note == ""
    assert verdicts["a"]["verdict"] == "load"
    assert (verdicts["b"]["verdict"], verdicts["b"]["reason"]) == ("unclear", "not-run")
    assert (verdicts["c"]["verdict"], verdicts["c"]["reason"]) == ("unclear", "not-run")  # it was not run, so not "again"


def test_record_rejects_a_word_outside_the_three():
    with pytest.raises(ValueError):
        record("t", _reading(1, 0), _reading(1, 0), "maybe")


def test_read_load_takes_cpu_runs_and_clock_when_injected():
    r = read_load(own_pid=1, cpu=lambda: 95.0, procs=[], clock=lambda: 1000.0)
    assert (r.cpu_percent, r.runs, r.heavy_runs, r.at, r.loaded) == (95.0, 0, 0, 1000.0, True)


def test_a_run_with_three_node_ids_is_light_and_the_machine_is_quiet():
    procs = [proc(2, 1, "python", "-m", "pytest", "tests/a.py::t1", "tests/a.py::t2", "tests/b.py::t3")]
    r = read_load(999, cpu=lambda: 20.0, procs=procs, clock=lambda: 0.0)
    assert (r.runs, r.heavy_runs, r.quiet) == (1, 0, True)


def test_a_bare_tests_folder_run_is_heavy():
    procs = [proc(2, 1, "python", "-m", "pytest", "tests/")]
    assert heavy_run_pids(999, procs) == [2]
    assert not read_load(999, cpu=lambda: 20.0, procs=procs, clock=lambda: 0.0).quiet


def test_a_run_with_no_path_is_heavy():
    assert heavy_run_pids(999, [proc(2, 1, "python", "-m", "pytest")]) == [2]


def test_a_bare_file_among_node_ids_makes_the_run_heavy():
    assert heavy_run_pids(999, [proc(2, 1, "pytest", "tests/a.py::t1", "tests/b.py")]) == [2]


def test_node_id_count_is_capped_at_fifty():
    fifty = [f"tests/a.py::t{i}" for i in range(50)]
    assert heavy_run_pids(999, [proc(2, 1, "python", "-m", "pytest", *fifty)]) == []
    assert heavy_run_pids(999, [proc(2, 1, "python", "-m", "pytest", *fifty, "tests/a.py::t51")]) == [2]


def test_option_values_are_not_taken_for_paths():
    assert heavy_run_pids(999, [proc(2, 1, "python", "-m", "pytest", "-n", "2", "tests/a.py::t1")]) == []


# --- row 3 (cont.): the 60 s CPU sampler -----------------------------------------------------------------------------


def test_reading_is_the_busy_share_of_the_last_sixty_seconds_after_a_gap():
    clock = FakeClock()
    # one sample every 5 s, t = 0..300 s: idle grows 5 s per tick until t = 240 s, then the machine is busy
    ticks = iter([(5.0 * i, 5.0 * min(i, 48)) for i in range(61)])
    sampler = CpuSampler(sample=lambda: next(ticks), clock=clock, interval=5.0, window=60.0)
    for i in range(61):
        clock.now = 5.0 * i
        sampler.tick()
    # the samples older than the window are dropped: the reading is 240..300 s (100 % busy), not the average
    # over 0..300 s (20 %)
    assert sampler.read() == pytest.approx(100.0)


def test_sampler_reads_nothing_before_it_has_two_samples():
    sampler = CpuSampler(sample=lambda: (0.0, 0.0), clock=FakeClock(), interval=5.0, window=60.0)
    assert sampler.read() is None


def test_sampler_thread_samples_and_stop_joins_it():
    counter = itertools.count()
    sampler = CpuSampler(sample=lambda: (float(next(counter)), 0.0), interval=0.01, window=60.0)
    sampler.start()
    try:
        reading = None
        deadline = time.monotonic() + 5
        while reading is None and time.monotonic() < deadline:
            time.sleep(0.01)
            reading = sampler.read()
        assert reading == pytest.approx(100.0)
    finally:
        assert sampler.stop(timeout=5) is True


def _own_sampler(machine_busy_per_tick, own_busy_per_tick):
    """A sampler over 13 ticks of 5 s on one core-equivalent: the machine and the merge's own tree each burn the
    given seconds per tick."""
    clock = FakeClock()
    machine = iter([(5.0 * i, 5.0 * i - machine_busy_per_tick * i) for i in range(13)])
    own = iter([own_busy_per_tick * i for i in range(13)])
    sampler = CpuSampler(sample=lambda: next(machine), clock=clock, interval=5.0, window=60.0,
                         own_busy=lambda: next(own))
    for i in range(13):
        clock.now = 5.0 * i
        sampler.tick()
    return sampler


def test_own_tree_busy_is_taken_out_of_the_reading_machine_95_own_90_reads_5():
    reading = _own_sampler(4.75, 4.5).read()  # machine 95 % busy, own tree 90 % of the span
    assert reading == pytest.approx(5.0)
    assert not LoadReading(cpu_percent=reading, runs=0, at=0.0).loaded


def test_machine_95_own_10_reads_85_and_is_loaded():
    reading = _own_sampler(4.75, 0.5).read()
    assert reading == pytest.approx(85.0)
    assert LoadReading(cpu_percent=reading, runs=0, at=0.0).loaded


def test_own_busy_larger_than_the_machine_busy_is_clamped_at_zero():
    assert _own_sampler(1.0, 4.0).read() == 0.0


def test_a_sampler_without_own_busy_reads_the_whole_machine():
    assert _own_sampler(4.75, 0.0).read() == pytest.approx(95.0)


def test_an_own_busy_that_raises_counts_as_nothing_own():
    clock = FakeClock()
    machine = iter([(5.0 * i, 0.0) for i in range(3)])

    def boom():
        raise OSError("gone")

    sampler = CpuSampler(sample=lambda: next(machine), clock=clock, interval=5.0, window=60.0, own_busy=boom)
    for i in range(3):
        clock.now = 5.0 * i
        sampler.tick()
    assert sampler.read() == pytest.approx(100.0)


# --- #223/#219: the fail-fast confirm is the re-check ------------------------------------------------------------


def test_failfast_confirm_failing_again_on_a_quiet_machine_is_code():
    assert failfast_verdict(_reading(95, 3), _reading(12, 0)) == "code"


def test_failfast_confirm_under_load_is_unclear_never_load():
    assert failfast_verdict(_reading(95, 3), _reading(92, 0)) == "unclear"
    assert failfast_verdict(_reading(95, 3), _reading(12, 1)) == "unclear"  # a heavy run beside the confirm
    assert failfast_verdict(_reading(20, 0), _reading(None, 0, heavy=0)) == "unclear"  # no CPU reading: not quiet


# --- row 4: waiting for a quiet machine ------------------------------------------------------------------------------


def test_quiet_machine_returns_at_once_with_no_note():
    clock = FakeClock()
    reading, note = wait_until_quiet(_script([_reading(20, 0)]), clock, clock.sleep, poll_seconds=30)
    assert (reading.cpu_percent, note, clock.now, clock.slept) == (20, "", 0.0, [])


def test_wait_polls_every_thirty_seconds_by_default():
    clock = FakeClock()
    reading, note = wait_until_quiet(_script([_reading(95, 0), _reading(20, 0)]), clock, clock.sleep)
    assert (note, clock.now, clock.slept) == ("", 30.0, [30.0])


def test_row4_starts_within_one_poll_of_the_first_quiet_reading():
    clock = FakeClock()
    read = _script([_reading(95, 3), _reading(90, 0), _reading(30, 1), _reading(20, 0)])
    reading, note = wait_until_quiet(read, clock, clock.sleep, poll_seconds=30)
    assert note == ""
    assert reading.cpu_percent == 20
    assert clock.now == 90  # reads at 0, 30, 60 were busy (cpu, cpu, a heavy run); 90 is the first quiet one


def test_row4_loaded_for_the_whole_wait_returns_at_600_seconds_with_note():
    clock = FakeClock()
    reading, note = wait_until_quiet(lambda: _reading(95, 3), clock, clock.sleep, poll_seconds=30)
    assert note == "re-check under load"
    assert clock.now == 600
    assert reading.cpu_percent == 95


def test_row4_a_poll_that_does_not_divide_600_still_returns_at_600():
    clock = FakeClock()
    reading, note = wait_until_quiet(lambda: _reading(95, 3), clock, clock.sleep, poll_seconds=45)
    assert note == "re-check under load"
    assert clock.now == 600


# --- row 5: other pytest sessions, by process ----------------------------------------------------------------------------


def test_a_run_is_counted_once_not_its_children():
    procs = [
        proc(10, 1, "python", "-m", "pytest", "-n", "2"),
        proc(11, 10, "python", "-c", "worker"),
        proc(12, 10, "python", "-m", "pytest", "--child"),  # a pytest under a pytest is part of its tree
    ]
    assert other_pytest_run_pids(999, procs) == [10]


def test_own_ancestors_and_own_tree_are_not_counted():
    procs = [
        proc(1, 0, "python", "-m", "pytest"),  # an ancestor of the caller
        proc(2, 1, "python", "-m", "tools.agentops", "merge"),  # the caller
        proc(3, 2, "python", "-m", "pytest", "-n", "2"),  # the caller's own xdist run
        proc(4, 3, "python", "-c", "worker"),  # its worker
        proc(5, 0, "pytest.exe", "tests/x.py"),  # another run, by the pytest script
    ]
    assert other_pytest_run_pids(2, procs) == [5]
    assert count_other_pytest_sessions(2, procs) == 1


def test_a_run_under_an_excluded_pytest_is_still_a_run():
    # the caller's test runner (an ancestor of the caller) starts a pytest: that one is another run
    procs = [
        proc(1, 0, "python", "-m", "pytest"),  # the caller's ancestor
        proc(2, 1, "python", "-c", "sleep"),  # the caller
        proc(6, 1, "python", "-m", "pytest", "sub.py"),  # started by the ancestor, not by a counted run
    ]
    assert other_pytest_run_pids(2, procs) == [6]


def test_only_pytest_sessions_are_matched():
    procs = [
        proc(7, 0, "python", "-c", "import pytest"),
        proc(8, 0, "git", "log", "pytest-notes"),
        proc(9, 0, "python", "-m", "http.server"),
        proc(13, 0, "python", "/usr/lib/python3/Scripts/pytest", "tests/x.py"),  # the script under python
    ]
    assert other_pytest_run_pids(999, procs) == [13]


class FakeProcess:
    """Stands in for psutil.Process; counts the command-line reads."""

    def __init__(self, name, ppid, cmd):
        self._name, self._ppid, self._cmd = name, ppid, cmd
        self.cmd_reads = 0

    def name(self):
        return self._name

    def ppid(self):
        return self._ppid

    def cmdline(self):
        self.cmd_reads += 1
        return list(self._cmd)


def test_command_line_is_read_only_for_python_like_names():
    fakes = {
        1: FakeProcess("python.exe", 0, ["python", "-m", "pytest"]),
        2: FakeProcess("notepad.exe", 1, ["notepad", "x.txt"]),
    }
    table = process_table(pids=[1, 2], open_process=fakes.__getitem__)
    assert table == [ProcInfo(1, 0, ("python", "-m", "pytest")), ProcInfo(2, 1, ())]
    assert (fakes[1].cmd_reads, fakes[2].cmd_reads) == (1, 0)


def test_row5_sees_a_pytest_started_from_another_folder(tmp_path):
    (tmp_path / "test_sleep.py").write_text("import time\n\n\ndef test_sleep():\n    time.sleep(120)\n")
    own = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], **_HIDDEN)  # the caller's stand-in
    other = subprocess.Popen(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_sleep.py"],
        cwd=tmp_path,
        **_HIDDEN,
    )
    try:
        deadline = time.monotonic() + 90
        while other.pid not in other_pytest_run_pids(own.pid) and time.monotonic() < deadline:
            time.sleep(0.5)
        assert other.pid in other_pytest_run_pids(own.pid)
        assert count_other_pytest_sessions(own.pid) >= 1
    finally:
        _end(other)
        _end(own)


def test_row5_own_xdist_run_and_its_workers_are_not_counted(tmp_path):
    pytest.importorskip("xdist")
    (tmp_path / "test_two.py").write_text(
        "import time\n\n\ndef test_a():\n    time.sleep(120)\n\n\ndef test_b():\n    time.sleep(120)\n"
    )
    own = subprocess.Popen(
        [sys.executable, "-m", "pytest", "-n", "2", "-q", "-p", "no:cacheprovider", "test_two.py"],
        cwd=tmp_path,
        **_HIDDEN,
    )
    workers: list[psutil.Process] = []
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            workers = psutil.Process(own.pid).children(recursive=True)
            if len(workers) >= 2:
                break
            time.sleep(0.5)
        own_tree = {own.pid, *(w.pid for w in workers)}
        assert len(own_tree) >= 3
        assert not own_tree & set(other_pytest_run_pids(own.pid))
    finally:
        for w in workers:
            w.terminate()
        psutil.wait_procs(workers, timeout=30)
        _end(own)
