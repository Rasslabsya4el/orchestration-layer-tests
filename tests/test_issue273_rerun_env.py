"""#273: the test re-run alone starts from the same cleaned environment as every other nested run. A full run started
by the merge suite carries its plugins in ``PYTEST_ADDOPTS``; the re-run in a temp dir cannot import them, so it must
not inherit them (``nested_env`` drops ``BUILD_ENV_VARS``, #195). The caller's ``env`` is still laid over it.

Real pytest on a temp test that passes; no heavy tool; the outcome is the return value, not a wall-clock bound."""
from __future__ import annotations

import sys

import pytest

from tools.agentops import merge

PASSING_TEST = "def test_t():\n    assert True\n"


@pytest.mark.parametrize("attempt", range(10))
def test_a_passing_test_rerun_alone_is_green_with_a_leaked_plugin_env(tmp_path, monkeypatch, attempt):
    """``PYTEST_ADDOPTS`` names the suite's plugin, which the temp dir cannot import: the re-run must not inherit it."""
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p tools.agentops.suite_events_plugin")
    (tmp_path / "test_t.py").write_text(PASSING_TEST, encoding="utf-8")
    passed, line = merge._rerun_alone(f'"{sys.executable}" -m pytest', "test_t.py::test_t", tmp_path, None,
                                      None, limit=300)
    assert passed is True and line == ""
