"""#755 rows 1-3: the suite's collection order is longest first by stored duration. Needs no heavy tool."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.agentops.suite_plan_plugin import order_longest_first
from tools.agentops.util import BUILD_ENV_VARS

ROOT = Path(__file__).resolve().parents[1]
STRIP = ("SUITE_PLAN_FILE", "SUITE_DURATIONS_FILE", *BUILD_ENV_VARS)  # #195: the build's three vars never reach a nested run


def _child(tmp_path, args, extra_env):
    env = {k: v for k, v in os.environ.items() if k not in STRIP}
    env.update(extra_env)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:faulthandler", "--basetemp", str(tmp_path.parent / (tmp_path.name + "-bt")), *args],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180)


def _write_suite(tmp_path, with_new):
    log = (tmp_path / "order.log").as_posix()
    body = "import time\n"
    for name, sleep in (("a", 0.3), ("b", 0.1), ("c", 0.2)):
        body += (f"def test_{name}():\n    time.sleep({sleep})\n"
                 f"    open({log!r}, 'a').write('{name} ')\n")
    if with_new:
        body += f"def test_new():\n    open({log!r}, 'a').write('new ')\n"
    (tmp_path / "test_s.py").write_text(body, encoding="utf-8")


FLAGS = ["--testmon", "--testmon-forceselect", "-p", "tools.agentops.suite_plan_plugin"]


def test_row1_the_ordering_function_puts_the_unknown_first_then_longest():
    stored = {"five": 5.0, "one": 1.0, "big": 438.0}
    assert order_longest_first(["five", "one", "unknown", "big"], stored) == ["unknown", "big", "five", "one"]


def test_row1_ties_keep_the_incoming_order_and_no_stored_leaves_the_order():
    assert order_longest_first(["x", "y", "z"], {"x": 2.0, "y": 2.0, "z": 2.0}) == ["x", "y", "z"]
    assert order_longest_first(["u2", "u1", "k"], {"k": 1.0}) == ["u2", "u1", "k"]
    assert order_longest_first(["b", "a"], {}) == ["b", "a"]


def test_row2_second_run_executes_the_new_test_then_longest_first(tmp_path):
    db = tmp_path / "tm.db"
    env = {"TESTMON_DATAFILE": str(db)}
    _write_suite(tmp_path, with_new=False)
    first = _child(tmp_path, FLAGS, env)
    assert first.returncode == 0, first.stdout + first.stderr
    (tmp_path / "order.log").unlink()
    _write_suite(tmp_path, with_new=True)
    second = _child(tmp_path, FLAGS, env)
    assert second.returncode == 0, second.stdout + second.stderr
    assert (tmp_path / "order.log").read_text().split() == ["new", "a", "c", "b"]


def test_row3_with_xdist_the_run_is_green_and_the_plan_is_descending(tmp_path):
    pytest.importorskip("xdist")
    db = tmp_path / "tm.db"
    plan = tmp_path / "plan.json"
    env = {"TESTMON_DATAFILE": str(db)}
    _write_suite(tmp_path, with_new=False)
    first = _child(tmp_path, FLAGS, env)
    assert first.returncode == 0, first.stdout + first.stderr
    _write_suite(tmp_path, with_new=True)
    second = _child(tmp_path, [*FLAGS, "-n", "2"], {**env, "SUITE_PLAN_FILE": str(plan)})
    out = second.stdout + second.stderr
    assert second.returncode == 0, out
    assert "different tests were collected" not in out
    seconds = json.loads(plan.read_text(encoding="utf-8"))["seconds"]
    assert len(seconds) == 4
    assert seconds[1:] == sorted(seconds[1:], reverse=True)
