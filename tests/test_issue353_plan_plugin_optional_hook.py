"""#353: the plan plugin's xdist hook is optional, so a child pytest without xdist still loads the plugin.

The merge suite on the PC puts `-p tools.agentops.suite_plan_plugin` in PYTEST_ADDOPTS; a child pytest started with
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 has no xdist, and the plugin's xdist hook must then be skipped, not refused (exit 3).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_plan_plugin_loads_in_a_child_pytest_without_xdist(tmp_path):
    (tmp_path / "test_one.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(REPO), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    env.pop("PYTEST_ADDOPTS", None)  # only the -p below names the plugin
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tools.agentops.suite_plan_plugin", "-q", "test_one.py"],
        cwd=tmp_path, capture_output=True, text=True, env=env, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
