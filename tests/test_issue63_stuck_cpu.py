"""#63 rule D: a suite is `stuck` only when its progress stood still for the quiet limit AND its whole process tree
gained less than STALL_MIN_CPU_SECONDS of CPU in that window; a silent suite that keeps burning CPU is slow, so the
window restarts and one "slow" line is logged. Without a total limit a suite ends after 4 h (`ceiling`). Fake clock,
fake CPU readings, injected machine-CPU probe: no real suite."""
from tools.agentops.suite_plan import finished_count, last_percent
from tools.agentops.merge import STALL_CEILING_SECONDS, STALL_MIN_CPU_SECONDS, StallWatch

NL = chr(10)
SILENT = "bringing up nodes...\nrestart poll: n/a done\n"


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _watch(tmp_path, **kw):
    clock = Clock()
    kw.setdefault("machine_cpu", lambda: 87.0)
    w = StallWatch(tmp_path / "s.out", interval=10, factor=3, min_share=0.05, clock=clock, quiet_limit=900.0, **kw)
    w.slow = []                 # the test's own record of what the runner would write into the out file
    w.log = w.slow.append
    return w, clock


def _tick(w, clock, t, text=SILENT, cpu=0.0):
    clock.t = t
    return w.tick(text, cpu_now=cpu)


def test_threshold_is_30_s_of_cpu():
    assert STALL_MIN_CPU_SECONDS == 30.0 and STALL_CEILING_SECONDS == 4 * 3600.0


def test_row1_silent_16_min_with_60_s_of_cpu_is_not_killed_and_leaves_one_slow_line(tmp_path):
    w, clock = _watch(tmp_path)
    assert _tick(w, clock, 0.0, cpu=0.0) == ""
    assert _tick(w, clock, 900.0, cpu=56.0) == ""        # the window ends: 56 s >= 30 s, slow not stuck
    assert _tick(w, clock, 960.0, cpu=60.0) == ""        # 16 min: the run goes on
    assert len(w.slow) == 1 and w.slow[0].startswith("suite slow:")


def test_row1_the_slow_line_is_once_per_window_and_the_next_window_starts_after_it(tmp_path):
    w, clock = _watch(tmp_path)
    assert _tick(w, clock, 0.0, cpu=0.0) == ""
    assert _tick(w, clock, 900.0, cpu=100.0) == ""
    for t in range(910, 1800, 10):                       # 15 min later the next window is not over yet
        assert _tick(w, clock, float(t), cpu=100.0 + (t - 900) * 0.1) == ""
    assert len(w.slow) == 1
    assert _tick(w, clock, 1800.0, cpu=100.0 + 900 * 0.1) == ""   # second window: 90 s of CPU -> a second slow line
    assert len(w.slow) == 2


def test_row2_silent_15_min_with_under_30_s_of_cpu_is_stuck(tmp_path):
    w, clock = _watch(tmp_path)
    assert _tick(w, clock, 0.0, cpu=0.0) == ""
    assert _tick(w, clock, 899.0, cpu=29.0) == ""
    line = _tick(w, clock, 900.0, cpu=29.0)
    assert line.startswith("stalled: the suite's pytest progress stood still for 15 min")
    assert "merge_suite_quiet_limit 15 min" in line
    assert w.slow == []


def test_row2_exactly_30_s_of_cpu_is_slow_not_stuck(tmp_path):
    w, clock = _watch(tmp_path)
    assert _tick(w, clock, 0.0, cpu=0.0) == ""
    assert _tick(w, clock, 900.0, cpu=30.0) == ""
    assert len(w.slow) == 1


def test_row2_cpu_before_the_window_does_not_count(tmp_path):
    """A burst of CPU while progress still moved is spent before the window opens: the baseline is the window's start."""
    w, clock = _watch(tmp_path)
    assert _tick(w, clock, 0.0, text=".", cpu=0.0) == ""
    assert _tick(w, clock, 100.0, text=".." , cpu=500.0) == ""      # progress moved here: the window opens at 100 s
    assert _tick(w, clock, 999.0, text="..", cpu=520.0) == ""
    assert "stood still" in _tick(w, clock, 1000.0, text="..", cpu=520.0)   # 20 s in the window


def test_row2_cpu_that_comes_with_progress_is_not_the_windows(tmp_path):
    w, clock = _watch(tmp_path)
    assert _tick(w, clock, 0.0, cpu=0.0) == ""
    assert _tick(w, clock, 600.0, cpu=100.0) == ""
    assert _tick(w, clock, 899.0, cpu=100.0) == ""
    assert _tick(w, clock, 900.0, cpu=100.0) == ""      # 100 s of the window's 900 s: slow
    assert len(w.slow) == 1


def test_row3_the_stuck_line_carries_cpu_gained_and_machine_cpu_percent(tmp_path):
    w, clock = _watch(tmp_path, machine_cpu=lambda: 96.4)
    assert _tick(w, clock, 0.0, cpu=0.0) == ""
    line = _tick(w, clock, 900.0, cpu=12.0)
    assert "12 s of CPU in 15 min" in line and "machine CPU 96 %" in line


def test_row3_the_slow_line_carries_cpu_gained_and_machine_cpu_percent(tmp_path):
    w, clock = _watch(tmp_path, machine_cpu=lambda: 87.0)
    assert _tick(w, clock, 0.0, cpu=0.0) == ""
    assert _tick(w, clock, 900.0, cpu=42.0) == ""
    (line,) = w.slow
    assert "42 s of CPU in 15 min" in line and "machine CPU 87 %" in line
    assert "\n" not in line


def test_row3_an_unreadable_machine_probe_leaves_the_cpu_out_not_the_line(tmp_path):
    w, clock = _watch(tmp_path, machine_cpu=lambda: None)
    assert _tick(w, clock, 0.0, cpu=0.0) == ""
    line = _tick(w, clock, 900.0, cpu=1.0)
    assert "1 s of CPU in 15 min" in line and "machine CPU" not in line


def test_row4_the_ceiling_ends_a_suite_past_4_h_whatever_its_cpu(tmp_path):
    w, clock = _watch(tmp_path)
    assert _tick(w, clock, 0.0, text=".", cpu=0.0) == ""
    assert _tick(w, clock, 4 * 3600.0 - 10, text="." * 5, cpu=9000.0) == ""
    line = _tick(w, clock, 4 * 3600.0, text="." * 6, cpu=14400.0)    # progress moves, CPU is full: still ended
    assert "ceiling" in line and line.startswith("stalled:") and "4 h 00 min" in line


def test_row4_a_set_total_limit_stays_as_it_is_and_the_ceiling_is_not_added(tmp_path):
    w, clock = _watch(tmp_path, total_limit=6 * 3600.0)
    assert _tick(w, clock, 0.0, text=".", cpu=0.0) == ""
    assert _tick(w, clock, 4 * 3600.0, text="." * 6, cpu=14400.0) == ""
    line = _tick(w, clock, 6 * 3600.0, text="." * 7, cpu=21600.0)
    assert "merge_suite_total_limit" in line and "ceiling" not in line


def test_begin_on_a_restart_resets_the_window_cpu_baseline(tmp_path):
    w, clock = _watch(tmp_path)
    assert _tick(w, clock, 0.0, cpu=0.0) == ""
    assert _tick(w, clock, 500.0, cpu=400.0) == ""
    clock.t = 500.0
    w.begin()                                            # a new child: its CPU counter starts at zero
    assert _tick(w, clock, 1400.0, cpu=10.0) != ""       # 10 s of CPU in 15 min


def test_a_slow_line_inside_a_progress_line_does_not_change_the_finished_count(tmp_path):
    """#900 shape: the watcher writes its line in the middle of a progress line; a reader drops it."""
    w, clock = _watch(tmp_path)
    assert _tick(w, clock, 0.0, cpu=0.0) == ""
    assert _tick(w, clock, 900.0, cpu=100.0) == ""
    (line,) = w.slow
    mixed = "....." + line + NL + ".... [ 36%]" + NL
    assert finished_count(mixed) == finished_count(".........  [ 36%]" + NL) == 9
    assert last_percent(mixed) == 36


def test_a_failing_log_restarts_the_window_first_and_is_not_repeated_every_tick(tmp_path):
    w, clock = _watch(tmp_path)
    calls = []

    def bad(line):
        calls.append(line)
        raise OSError("disk")
    w.log = bad
    assert _tick(w, clock, 0.0, cpu=0.0) == ""
    try:
        _tick(w, clock, 900.0, cpu=100.0)
    except OSError:
        pass                                             # `watch` catches it like any failed reading
    assert _tick(w, clock, 910.0, cpu=101.0) == "" and len(calls) == 1
