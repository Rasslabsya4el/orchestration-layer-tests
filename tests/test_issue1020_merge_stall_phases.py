"""#1020: the merge's StallWatch opens its "no test finished" window only between the first progress character and
pytest's summary line. Before the first dot (xdist bring-up) and after the summary (the process's exit) no test is
due: the quiet limit and the total limit still end those phases. Fake clock and CPU, as in test_issue901_stall_window."""
import json
from pathlib import Path

from tools.agentops.merge import StallWatch
from tools.agentops.suite_plan import plan_path


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _plan(out: Path, longest: float) -> None:
    plan_path(out).write_text(json.dumps({"seconds": [longest, 1.0, 0.5]}), encoding="utf-8")


def _tick(w, clock, t, text="", cpu=None):
    clock.t = t
    return w.tick(text, cpu_now=t if cpu is None else cpu)


# -- row 1: the o1015 shape: plan complete, no dot, longest 5 s, the run gets 0.82 of a core --------------

O1015_TEXT = "bringing up nodes...\nrestart poll: 0.82 of a core\n"


def test_row1_o1015_shape_no_dot_is_no_stall_at_20_s_nor_at_14_min_59_s(tmp_path):
    out = tmp_path / "s.out"
    _plan(out, 5.0)
    clock = Clock()
    w = StallWatch(out, interval=10, factor=3, min_share=0.05, clock=clock, quiet_limit=900.0)
    assert _tick(w, clock, 0.0, O1015_TEXT, cpu=0.0) == ""
    assert _tick(w, clock, 20.0, O1015_TEXT, cpu=0.82 * 20.0) == ""      # bound would be 3 x 5 / 0.82 = 18 s
    assert _tick(w, clock, 899.0, O1015_TEXT, cpu=0.82 * 899.0) == ""    # 14 min 59 s: still no reason


def test_row1_o1015_shape_flat_cpu_with_no_dot_stalls_at_the_quiet_limit(tmp_path):
    out = tmp_path / "s.out"
    _plan(out, 5.0)
    clock = Clock()
    w = StallWatch(out, interval=10, factor=3, min_share=0.05, clock=clock, quiet_limit=900.0)
    assert _tick(w, clock, 0.0, O1015_TEXT, cpu=0.0) == ""
    assert _tick(w, clock, 899.0, O1015_TEXT, cpu=0.0) == ""
    line = _tick(w, clock, 900.0, O1015_TEXT, cpu=0.0)
    assert "merge_suite_quiet_limit 15 min" in line and line.startswith("stalled: the suite's pytest progress stood still")


# -- #47: collection prints no dot and no percent; a tree whose CPU keeps growing is slow, not dead ---------

def test_47_no_dot_with_cpu_growing_is_no_stall_at_the_quiet_limit_nor_at_twice_it(tmp_path):
    out = tmp_path / "s.out"
    _plan(out, 5.0)
    clock = Clock()
    w = StallWatch(out, interval=10, factor=3, min_share=0.05, clock=clock, quiet_limit=900.0)
    assert _tick(w, clock, 0.0, O1015_TEXT, cpu=0.0) == ""
    assert _tick(w, clock, 900.0, O1015_TEXT, cpu=0.25 * 900.0) == ""    # a quarter core, as the w1152 nodes
    assert _tick(w, clock, 1800.0, O1015_TEXT, cpu=0.25 * 1800.0) == ""  # 2 x the limit


def test_47_cpu_that_goes_flat_after_growth_stalls_one_quiet_limit_after_the_last_growth(tmp_path):
    out = tmp_path / "s.out"
    _plan(out, 5.0)
    clock = Clock()
    w = StallWatch(out, interval=10, factor=3, min_share=0.05, clock=clock, quiet_limit=900.0)
    assert _tick(w, clock, 0.0, O1015_TEXT, cpu=0.0) == ""
    assert _tick(w, clock, 1000.0, O1015_TEXT, cpu=250.0) == ""          # last growth at 1000 s
    assert _tick(w, clock, 1899.0, O1015_TEXT, cpu=250.0) == ""
    assert "merge_suite_quiet_limit 15 min" in _tick(w, clock, 1900.0, O1015_TEXT, cpu=250.0)


def test_47_idle_cpu_below_the_min_share_is_no_life(tmp_path):
    out = tmp_path / "s.out"
    _plan(out, 5.0)
    clock = Clock()
    w = StallWatch(out, interval=10, factor=3, min_share=0.05, clock=clock, quiet_limit=900.0)
    assert _tick(w, clock, 0.0, O1015_TEXT, cpu=0.0) == ""
    for t in range(10, 900, 10):                                         # heartbeats: 1 % of a core
        assert _tick(w, clock, float(t), O1015_TEXT, cpu=0.01 * t) == ""
    assert "stood still" in _tick(w, clock, 900.0, O1015_TEXT, cpu=9.0)


def test_47_total_limit_still_ends_a_run_whose_cpu_keeps_growing(tmp_path):
    out = tmp_path / "s.out"
    _plan(out, 5.0)
    clock = Clock()
    w = StallWatch(out, interval=10, factor=3, min_share=0.05, clock=clock, quiet_limit=900.0, total_limit=2700.0)
    assert _tick(w, clock, 0.0, O1015_TEXT, cpu=0.0) == ""
    assert _tick(w, clock, 1800.0, O1015_TEXT, cpu=0.5 * 1800.0) == ""
    assert "passed its total limit" in _tick(w, clock, 2700.0, O1015_TEXT, cpu=0.5 * 2700.0)


def test_47_begin_on_a_restart_resets_the_quiet_window(tmp_path):
    out = tmp_path / "s.out"
    _plan(out, 5.0)
    clock = Clock()
    w = StallWatch(out, interval=10, factor=3, min_share=0.05, clock=clock, quiet_limit=900.0)
    assert _tick(w, clock, 0.0, O1015_TEXT, cpu=0.0) == ""
    assert _tick(w, clock, 800.0, O1015_TEXT, cpu=0.0) == ""
    clock.t = 800.0
    w.begin()                                                            # SuiteRestart killed the child and started a new one
    assert _tick(w, clock, 1000.0, O1015_TEXT, cpu=0.0) == ""            # 200 s after the restart, not 1000 s
    assert _tick(w, clock, 1699.0, O1015_TEXT, cpu=0.0) == ""
    assert "stood still" in _tick(w, clock, 1700.0, O1015_TEXT, cpu=0.0)


def test_47_a_restarted_childs_cpu_counter_starting_from_zero_is_read_as_growth_not_a_fall(tmp_path):
    out = tmp_path / "s.out"
    _plan(out, 5.0)
    clock = Clock()
    w = StallWatch(out, interval=10, factor=3, min_share=0.05, clock=clock, quiet_limit=900.0)
    assert _tick(w, clock, 0.0, O1015_TEXT, cpu=0.0) == ""
    assert _tick(w, clock, 500.0, O1015_TEXT, cpu=400.0) == ""
    clock.t = 500.0
    w.begin()
    assert _tick(w, clock, 1000.0, O1015_TEXT, cpu=100.0) == ""          # new tree: 100 s of CPU over 500 s
    assert _tick(w, clock, 1900.0, O1015_TEXT, cpu=100.0 + 0.2 * 900.0) == ""


def test_row1_no_dot_before_the_total_limit_still_ends_the_run(tmp_path):
    out = tmp_path / "s.out"
    _plan(out, 5.0)
    clock = Clock()
    w = StallWatch(out, interval=10, factor=3, min_share=0.05, clock=clock, total_limit=600.0)
    assert _tick(w, clock, 0.0, O1015_TEXT, cpu=0.0) == ""
    assert _tick(w, clock, 599.0, O1015_TEXT, cpu=0.82 * 599.0) == ""
    assert "passed its total limit" in _tick(w, clock, 600.0, O1015_TEXT, cpu=0.82 * 600.0)


# -- row 2: the 979 shape: `[100%]` and `47 passed in 14.71s`, then 10 s without output, longest 1.9 s ----

SUMMARY_979 = "." * 47 + " [100%]\n47 passed in 14.71s\n"


def test_row2_979_shape_summary_then_silence_is_no_stall(tmp_path):
    out = tmp_path / "s.out"
    _plan(out, 1.9)
    clock = Clock()
    w = StallWatch(out, interval=1, factor=3, min_share=0.05, clock=clock)
    assert _tick(w, clock, 0.0, "", cpu=0.0) == ""
    assert _tick(w, clock, 14.71, SUMMARY_979, cpu=14.71) == ""          # the summary arrives
    assert _tick(w, clock, 24.71, SUMMARY_979, cpu=24.71) == ""          # 10 s of silence: bound would be 5.7 s
    assert _tick(w, clock, 3000.0, SUMMARY_979, cpu=3000.0) == ""        # the process outliving its summary: no test is due


def test_row2_979_shape_the_total_limit_still_ends_the_run_after_the_summary(tmp_path):
    out = tmp_path / "s.out"
    _plan(out, 1.9)
    clock = Clock()
    w = StallWatch(out, interval=1, factor=3, min_share=0.05, clock=clock, total_limit=600.0)
    assert _tick(w, clock, 0.0, "", cpu=0.0) == ""
    assert _tick(w, clock, 14.71, SUMMARY_979, cpu=14.71) == ""
    assert "passed its total limit" in _tick(w, clock, 600.0, SUMMARY_979, cpu=600.0)


# -- row 3: first dot, then no further dot, longest 79 s, the run gets a full core -------------------------

def test_row3_first_dot_then_silence_stalls_at_237_s_after_the_dot_not_before(tmp_path):
    out = tmp_path / "s.out"
    _plan(out, 79.0)
    clock = Clock()
    w = StallWatch(out, interval=1, factor=3, min_share=0.05, clock=clock)
    assert _tick(w, clock, 0.0, "", cpu=0.0) == ""                       # before the first dot: no window
    assert _tick(w, clock, 300.0, ".", cpu=300.0) == ""                  # the first dot opens the window
    assert _tick(w, clock, 300.0 + 236.0, ".", cpu=300.0 + 236.0) == ""  # 236 s after it: bound 3 x 79 = 237 s
    line = _tick(w, clock, 300.0 + 237.0, ".", cpu=300.0 + 237.0)
    assert line.startswith("stalled: no test finished for") and "/ 1.00 of a core" in line
