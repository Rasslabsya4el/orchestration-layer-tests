"""#343 unit 1: three tests were red in every full map build and green alone. Cause: a map build runs the suite with
``PYTEST_ADDOPTS=-p tools.agentops.suite_reads_plugin`` (and ``SUITE_READS_DIR`` / ``TESTMON_DATAFILE``); a pytest that a
test starts from a temp folder inherits it, cannot import ``tools`` there and dies at once (exit 4), so a sleeping
pytest never shows in the process table, an xdist run has no workers, and ``_run_ids`` reads exit 4 as "every id red".

The fixes: ``safety_revert._run_ids`` starts its pytest through ``nested_env`` (module), and the two ``test_row5_*``
tests start theirs with the same scrubbed env (test). Fast, no sleeping children."""
from __future__ import annotations

import subprocess
import sys

import pytest

from tools.agentops import safety_revert

def _hidden() -> dict:
    from tests import test_issue223_merge_load  # the helper the two row-5 tests launch their pytest with
    return test_issue223_merge_load._hidden()


LEAKED = {
    "PYTEST_ADDOPTS": "-p tools.agentops.suite_reads_plugin",
    "SUITE_READS_DIR": "reads-of-the-outer-build",
    "TESTMON_DATAFILE": "testmondata-of-the-outer-build",
}


@pytest.fixture
def leaked_build_env(monkeypatch):
    for k, v in LEAKED.items():
        monkeypatch.setenv(k, v)


def test_run_ids_in_a_temp_folder_ignores_the_outer_builds_plugin_env(tmp_path, leaked_build_env):
    (tmp_path / "test_x.py").write_text(
        "import pytest\n@pytest.fixture\ndef boom():\n    raise RuntimeError('x')\n"
        "def test_errors(boom):\n    pass\ndef test_fails():\n    assert 0\ndef test_ok():\n    pass\n", encoding="utf-8")
    ids = ["test_x.py::test_errors", "test_x.py::test_fails", "test_x.py::test_ok"]
    assert sorted(safety_revert._run_ids(tmp_path, ids)) == sorted(ids[:2])


def test_a_pytest_started_the_way_the_row5_tests_start_theirs_runs_in_a_temp_folder(tmp_path, leaked_build_env):
    (tmp_path / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_a.py"],
                         cwd=tmp_path, capture_output=True, text=True, timeout=120, **_hidden())
    assert run.returncode == 0, run.stdout + run.stderr


def test_the_row5_launch_options_carry_none_of_the_build_vars(leaked_build_env):
    env = _hidden()["env"]
    assert not set(LEAKED) & set(env)
