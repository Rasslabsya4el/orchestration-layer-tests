"""Test support (#96): the merge paths redirected to a temp dir for every test.

A core test file that writes or reads the merge paths does `from tests._merge_paths import _agentops_merge_paths_in_tmp  # noqa: F401`:
importing an autouse fixture into the module namespace activates it for every test of that file, and a repo whose conftest
lacks the fixture still runs the file.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _agentops_merge_paths_in_tmp(tmp_path_factory: pytest.TempPathFactory,
                                 monkeypatch: pytest.MonkeyPatch):
    """No test writes the live `var/agentops/` (merge.log, merge_suite/, results/) (issue #417).

    #399 only patched the module constants in two of the six merge test files, and a constant
    patch never reaches a child process (`--detach` runner, `python -m tools.agentops merge`).
    Here every one of those paths is redirected for each test twice: the module constants
    (in-process) and the env overrides merge.py / detach.py read at call time (children
    inherit the environment). One dir per test: the suite-output pruning of one test must not
    delete another test's output file.
    """
    from tools.agentops import detach, merge
    base = tmp_path_factory.mktemp("agentops_var")
    for key, val in ((merge.ENV_MERGE_LOG, base / "merge.log"),
                     (merge.ENV_SUITE_OUTPUT_DIR, base / "merge_suite"),
                     (detach.ENV_RESULTS_DIR, base / "results"),
                     (detach.ENV_MERGE_LOG, base / "detach_merge.log")):
        monkeypatch.setenv(key, str(val))
    monkeypatch.setattr(merge, "MERGE_LOG_PATH", base / "merge.log")
    monkeypatch.setattr(merge, "SUITE_OUTPUT_DIR", base / "merge_suite")
    monkeypatch.setattr(detach, "RESULTS_DIR", base / "results")
    monkeypatch.setattr(detach, "MERGE_LOG", base / "detach_merge.log")
    yield base
