"""Issue #271 row 2: a re-check sent ids that were not collected ends with a named reason, never an xdist crash.

A real pytest run with ``-n 2`` on a temp project, the shard plugin loaded with ``-p`` and ``TEST_IDS_FILE`` set.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN = "tools.agentops.test_shard_plugin"
GONE = "tests/test_t.py::test_gone"


def _project(root: Path, listed: list[str]) -> None:
    (root / "tests").mkdir()
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    body = "".join(f"def test_{name}():\n    pass\n\n\n" for name in ("a", "b", "c", "d"))
    (root / "tests" / "test_t.py").write_text(body, encoding="utf-8")
    (root / "ids.txt").write_text("\n".join(listed) + "\n", encoding="utf-8")


def _run(root: Path, extra: list[str], shard: str | None = None) -> tuple[subprocess.CompletedProcess, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("TEST_SHARD", "TEST_PC_ONLY", "TEST_SKIP_FILE", "PYTEST_ADDOPTS", "PYTHONPATH")}
    env["PYTHONPATH"] = str(REPO)
    env["TEST_IDS_FILE"] = str(root / "ids.txt")
    if shard is not None:
        env["TEST_SHARD"] = shard
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-v", "-p", PLUGIN, "-p", "no:cacheprovider", *extra],
        cwd=root, env=env, capture_output=True, text=True, timeout=180,
    )
    return run, run.stdout + run.stderr


def test_an_id_not_collected_under_xdist_ends_red_naming_it_without_a_crash(tmp_path):
    _project(tmp_path, ["tests/test_t.py::test_a", GONE])
    run, out = _run(tmp_path, ["-n", "2"])
    assert run.returncode != 0, out
    assert "ids sent but not collected: 1 of 2" in out and GONE in out, out
    assert "INTERNALERROR" not in out and "crashitem" not in out, out


def test_an_id_not_collected_under_xdist_with_a_part_is_named_too(tmp_path):
    _project(tmp_path, ["tests/test_t.py::test_a", GONE])
    run, out = _run(tmp_path, ["-n", "2"], shard="1/2")
    assert run.returncode != 0, out
    assert GONE in out and "INTERNALERROR" not in out, out


def test_an_id_not_collected_without_xdist_is_still_an_error_naming_it(tmp_path):
    _project(tmp_path, ["tests/test_t.py::test_a", GONE])
    run, out = _run(tmp_path, [])
    assert run.returncode != 0, out
    assert "ids sent but not collected: 1 of 2" in out and GONE in out, out


def test_all_ids_collected_under_xdist_runs_only_those_and_is_green(tmp_path):
    _project(tmp_path, ["tests/test_t.py::test_a", "tests/test_t.py::test_c"])
    run, out = _run(tmp_path, ["-n", "2"])
    assert run.returncode == 0, out
    assert "2 passed" in out and "INTERNALERROR" not in out, out
    assert "test_b" not in out and "test_d" not in out, out
