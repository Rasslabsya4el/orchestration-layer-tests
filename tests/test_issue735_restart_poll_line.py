"""#735 row 5: every restart poll writes one reading line (workers, percent, verdict)."""
import itertools

from tools.agentops.merge import SuiteRestart

MIN = 60.0


def _restart(k_new, lines):
    ticks = itertools.count(0, 480)
    r = SuiteRestart(1, probe_k=lambda: k_new, full_for=lambda k: 6.5 * MIN, interval=0.2,
                     clock=lambda: float(next(ticks)))
    r.log = lines.append
    return r


def test_row5_one_line_per_poll_with_workers_percent_verdict():
    lines: list[str] = []
    r = _restart(6, lines)
    r.begin()
    go, _, _ = r.tick("..... [ 28%]")
    assert go and len(lines) == 1
    assert "k_new 6" in lines[0] and "28%" in lines[0] and "-> restart" in lines[0]
    r2 = _restart(1, lines)
    r2.begin()
    r2.tick("..... [ 28%]")
    assert len(lines) == 2
    assert "k_new 1" in lines[1] and "28%" in lines[1] and "-> keep" in lines[1] and "< 2*1" in lines[1]


def test_no_line_without_log():
    r = SuiteRestart(1, probe_k=lambda: 6, full_for=lambda k: 1.0)
    r.begin()
    r.tick("[ 28%]")  # no log set: nothing written, nothing raised
