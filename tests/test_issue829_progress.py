"""#829 unit 2: the suite's percent counts the dots of the unfinished last line too (heavy-tool-free)."""
from __future__ import annotations

import json
import sys
import textwrap
import threading
import time
from pathlib import Path

from tools.agentops import merge
from tools.agentops.merge_status import _progress
from tools.agentops.suite_plan import finished_count, load_plan, plan_path, work_share

FAKE = textwrap.dedent("""
    import sys, time
    for i in range(3):
        sys.stdout.write("."); sys.stdout.flush()
        time.sleep(3 if i < 2 else 0.5)
""")


def test_partial_tail_is_counted():
    assert finished_count("") == 0
    assert finished_count("...") == 3
    assert finished_count("head\n..F.. [ 10%]\n...") == 5 + 3
    assert finished_count("..... [ 10%]") == 5  # complete line without newline: counted once
    assert finished_count("..s.. [ ") == 5  # percent being written
    assert finished_count("12 passed in 1s") == 0
    assert _progress("..F.. [ 10%]\n.F") == (10, 2)


def test_full_run_at_one_worker_percent_moves_after_first_dot(tmp_path: Path):
    out = tmp_path / "suite.out"
    plan_path(out).write_text(json.dumps({"seconds": [1.0, 1.0, 1.0]}), encoding="utf-8")
    cmd = f'"{sys.executable}" -c "{FAKE.strip()}"'.replace("\n", ";")  # one-line program
    cmd = f'"{sys.executable}" "{_write(tmp_path)}"'
    seen: list[tuple[float, float]] = []
    done = threading.Event()
    t0 = time.monotonic()

    def poll():
        while not done.is_set():
            try:
                text = out.read_text(encoding="utf-8")
            except OSError:
                text = ""
            plan = load_plan(out)
            share = work_share(plan, finished_count(text)) if plan else 0
            seen.append((time.monotonic() - t0, share))
            time.sleep(0.1)

    th = threading.Thread(target=poll, daemon=True)
    th.start()
    ok, output = merge._run_suite(cmd, tmp_path, None, out)
    done.set()
    th.join(timeout=5)
    assert ok and output == "..."
    first = next(t for t, s in seen if s > 0)
    assert first < 3.0, f"first nonzero share only after {first:.1f}s (the child's whole run is ~6.5s)"
    assert abs(max(s for _, s in seen) - 1.0) < 1e-9
    assert any(abs(s - 1 / 3) < 1e-9 for _, s in seen)


def test_real_pytest_child_dots_reach_the_file_at_once(tmp_path: Path):
    # the bound follows the machine: startup = one whole run of the same child over a file of a single fast test
    # (collection + import + the test); the slow test sleeps 8 x startup, the first dot must be seen within 3 x startup,
    # so a dot that shows only after the child ends (>= 8 x startup) stays red under any load
    cmd = f'"{sys.executable}" -m pytest test_slow.py -q -p no:faulthandler -p no:cacheprovider'
    (tmp_path / "test_slow.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    t0 = time.monotonic()
    merge._run_suite(cmd, tmp_path, None, tmp_path / "warm.out")
    startup = time.monotonic() - t0
    sleep_s = 8 * startup
    (tmp_path / "test_slow.py").write_text(
        f"import time\ndef test_a():\n    pass\ndef test_b():\n    time.sleep({sleep_s})\n", encoding="utf-8")
    out = tmp_path / "p.out"
    th = threading.Thread(target=merge._run_suite, args=(cmd, tmp_path, None, out), daemon=True)
    t0 = time.monotonic()
    th.start()
    first = None
    while th.is_alive() and time.monotonic() - t0 < sleep_s:
        if out.exists() and finished_count(out.read_text(encoding="utf-8")) >= 1:
            first = time.monotonic() - t0
            break
        time.sleep(0.05)
    th.join(timeout=sleep_s + 60)
    assert first is not None and first < 3 * startup, \
        f"first dot seen after {first} (startup {startup:.2f}; test_b alone runs {sleep_s:.1f} s)"


def _write(tmp_path: Path) -> Path:
    p = tmp_path / "fake_suite.py"
    p.write_text(FAKE, encoding="utf-8")
    return p
