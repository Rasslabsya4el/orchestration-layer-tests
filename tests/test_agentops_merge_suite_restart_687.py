"""#687: a running merge suite restarts once with more workers when the PC frees up."""

import sys

from tools.agentops import merge
from tools.agentops.merge import SuiteRestart, decide_restart, last_full_suite_by_workers, nearest_full_suite

MIN = 60.0


def test_row1_k1_20_percent_after_8_min_restarts():
    go, rest = decide_restart(1, 5, 8 * MIN, 20, 6.5 * MIN, False)
    assert go and round(rest / MIN) == 32


def test_row2_k3_80_percent_no_restart():
    go, rest = decide_restart(3, 6, 5 * MIN, 80, 4 * MIN, False)
    assert not go and round(rest) == 75  # 1.25 min


def test_row3_second_chance_after_restart_yes():
    # #763: the once-only limit is gone; a restarted suite restarts again when k_new >= 2*k
    assert decide_restart(1, 5, 8 * MIN, 20, 6.5 * MIN, True)[0]
    assert not decide_restart(5, 5, 8 * MIN, 20, 6.5 * MIN, True)[0]


def test_low_percent_and_missing_full_and_small_gain_no_restart():
    # #763: under 5 % or unknown there is no estimate and little to lose: restart at once
    assert decide_restart(1, 5, 8 * MIN, 4, 6.5 * MIN, False) == (True, None)
    assert decide_restart(1, 5, 8 * MIN, None, None, False) == (True, None)
    assert not decide_restart(3, 5, 8 * MIN, 4, None, False)[0]  # 5 < 2 x 3
    assert not decide_restart(1, 5, 8 * MIN, 20, None, False)[0]
    assert not decide_restart(3, 5, 8 * MIN, 20, 6.5 * MIN, False)[0]  # 5 < 2 x 3


def test_merge_log_helper_nearest(tmp_path):
    log = tmp_path / "merge.log"
    log.write_text(
        "t | b | landed | suite 21 min | 0 failed | output x | suite dir y\n"
        "t | b | landed | suite 4 min, 6 workers | 0 failed | output x | suite dir y\n"
        "t | b | landed | suite 6.5 min, 6 workers | 0 failed | output x | suite dir y\n"
        "t | b | red | suite 1 min, 5 workers | 3 failed | output x | suite dir y\n", encoding="utf-8")
    by = last_full_suite_by_workers(log)
    assert by == {1: 21 * MIN, 6: 6.5 * MIN}
    assert nearest_full_suite(by, 5) == 6.5 * MIN
    assert nearest_full_suite({}, 5) is None


def _row4(tmp_path, start_delay):
    script = tmp_path / "fake_suite.py"
    script.write_text(
        "import sys, time\n"
        f"time.sleep({start_delay})\n"
        "n = int(sys.argv[sys.argv.index('-n') + 1])\n"
        "if n == 1:\n"
        "    print('.....                                  [ 20%]', flush=True)\n"
        "    time.sleep(60)\n"
        "print('..........                             [100%]', flush=True)\n"
        "print('10 passed in 1s', flush=True)\n", encoding="utf-8")
    out = tmp_path / "suite.out"

    def clock():  # the fake time stands still until the suite has really printed its percent
        return 480.0 if "[ 20%]" in (out.read_text(encoding="utf-8") if out.exists() else "") else 0.0

    def probe_k():
        # Cause (#844): with the PC busy the child needs more than one `interval` to print its percent; a tick
        # before it (merge.py:1128, percent None -> :1052) restarted "at once". With no delay the PC "frees up"
        # only once the percent line is in the file, so every tick that can restart sees the percent.
        if start_delay or "[ 20%]" in (out.read_text(encoding="utf-8") if out.exists() else ""):
            return 5
        return 1

    restart = SuiteRestart(1, probe_k=probe_k, full_for=lambda k: 6.5 * MIN, interval=0.2, clock=clock)
    cmd = f'"{sys.executable}" "{script}" -n 1'
    ok, output = merge._run_suite(cmd, tmp_path, None, out, "workers 1: busy", restart=restart)
    text = out.read_text(encoding="utf-8")
    assert ok
    assert text.splitlines()[0] == "workers 1: busy"
    if start_delay:  # #763: before the first percent line there is no estimate, the restart is at once
        assert "restarted: workers 1 -> 5 (percent under 5 or unknown: little to lose)" in text
    else:
        assert "restarted: workers 1 -> 5 (rest ~32 min vs full ~6.5 min)" in text
    assert "10 passed" in output and "[ 20%]" not in output


def test_row4_restart_line_in_out_file_and_green(tmp_path):
    _row4(tmp_path, 0)


def test_row4_restart_line_with_a_late_started_suite(tmp_path):
    _row4(tmp_path, 2)


# -- #763: a suite restarts whenever the machine frees up (more than once, any percent) ----

def _fake_763(tmp_path, percent_line, slow_below):
    """A suite that is slow (60 s) on fewer than ``slow_below`` workers and green on more."""
    script = tmp_path / "fake763.py"
    script.write_text(
        "import sys, time\n"
        "n = int(sys.argv[sys.argv.index('-n') + 1])\n"
        f"if n < {slow_below}:\n"
        + (f"    print({percent_line!r}, flush=True)\n" if percent_line else "")
        + "    time.sleep(60)\n"
        "print('10 passed in 1s', flush=True)\n", encoding="utf-8")
    return script


def _readings(*seq):
    """A probe: the readings in order, the last one for ever."""
    it = iter(seq)
    last = [seq[-1]]

    def probe():
        last[0] = next(it, last[0])
        return last[0]
    return probe


def _drive(tmp_path, script, k, probe, fixed_clock=None):
    out = tmp_path / "suite.out"
    if fixed_clock is not None:
        def clock():  # stands at ``fixed_clock`` once the suite has printed its percent, else 0
            return fixed_clock if "[ 20%]" in (out.read_text(encoding="utf-8") if out.exists() else "") else 0.0
    else:
        clock = __import__("time").monotonic
    restart = SuiteRestart(k, probe_k=probe, full_for=lambda kn: 6.5 * MIN, interval=0.2, clock=clock)
    ok, _ = merge._run_suite(f'"{sys.executable}" "{script}" -n {k}', tmp_path, None, out, f"workers {k}: busy",
                             restart=restart)
    return ok, out.read_text(encoding="utf-8")


def test_763_row2_percent_known_restarts_when_reading_turns(tmp_path):
    script = _fake_763(tmp_path, ".....  [ 20%]", 6)
    ok, text = _drive(tmp_path, script, 1, _readings(1, 1, 1, 6), fixed_clock=480.0)
    assert ok and "restarted: workers 1 -> 6" in text


def test_763_row3_percent_unknown_restarts_at_once(tmp_path):
    script = _fake_763(tmp_path, "", 6)
    ok, text = _drive(tmp_path, script, 1, _readings(1, 1, 6))
    assert ok and "restarted: workers 1 -> 6" in text
    assert "percent under 5 or unknown: little to lose" in text


def test_763_row4_ninety_percent_on_one_worker_no_restart():
    go, rest = decide_restart(1, 6, 480.0, 90, 6.5 * MIN, False)
    assert not go and round(rest) == 53
    script_free = decide_restart(6, 6, 480.0, None, None, False)
    assert script_free == (False, None)  # already on 6 with reading 6: never


def test_763_second_restart_1_to_2_then_2_to_6(tmp_path):
    script = _fake_763(tmp_path, "", 6)
    ok, text = _drive(tmp_path, script, 1, _readings(2, 2, 6))
    assert ok
    assert "restarted: workers 1 -> 2" in text and "restarted: workers 2 -> 6" in text
