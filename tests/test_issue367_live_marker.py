"""Issue #367 unit 1: the pytest marker `live` keeps this machine's real-install checks out of the merge suite."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from tools.agentops import provisional
from tools.agentops.util import no_window_flags

REPO_ROOT = Path(__file__).resolve().parents[1]

LIVE_TEST_FILES = [
    "tests/test_issue1080_pocock_skill_check.py",
    "tests/test_issue1081_home_files_match_templates.py",
    "tests/test_issue1082_pocock_contract.py",
    "tests/test_issue166_fast_path.py",
    "tests/test_issue191_command.py",
]

LIVE_TEST_IDS = {
    "tests/test_issue1080_pocock_skill_check.py::test_installed_plugin_ships_every_named_skill",
    "tests/test_issue1081_home_files_match_templates.py::test_every_home_heading_exists_in_the_pocock_template",
    "tests/test_issue1082_pocock_contract.py::test_the_real_contract_names_only_skills_the_installed_plugin_ships",
    "tests/test_issue1082_pocock_contract.py::test_every_name_the_manifest_files_name_is_in_the_contract",
    "tests/test_issue166_fast_path.py::test_b_real_project_ts_names_the_installed_monitor_folder",
    "tests/test_issue191_command.py::test_core_check_exits_zero",
}

MERGE_EXPR = "not slow and not live"


def _collect(expr: str) -> set[str]:
    res = subprocess.run(
        [sys.executable, "-m", "pytest", *LIVE_TEST_FILES, "--collect-only", "-q", "-m", expr, "-p", "no:faulthandler"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=600, creationflags=no_window_flags(),
    )
    assert res.returncode in (0, 5), res.stdout[-2000:] + res.stderr[-2000:]
    # parametrized tests list one id per parameter: strip the [...] suffix to get the test's own id
    return {re.sub(r"\[.*\]$", "", line.strip()) for line in res.stdout.splitlines() if "::" in line}


def test_pytest_ini_registers_the_live_marker():
    text = (REPO_ROOT / "pytest.ini").read_text(encoding="utf-8")
    assert re.search(r"(?m)^\s+live:", text), text


def test_merge_suite_setting_excludes_live():
    cfg = json.loads((REPO_ROOT / "config" / "orchestration.json").read_text(encoding="utf-8"))
    assert MERGE_EXPR in cfg["merge_suite"], cfg["merge_suite"]


def test_both_workflow_pytest_commands_exclude_live():
    text = (REPO_ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    markers = re.findall(r'-m "not slow[^"]*"', text)
    assert markers == [f'-m "{MERGE_EXPR}"'] * 2, markers


def test_merge_selection_collects_none_of_the_six_live_tests():
    collected = _collect(MERGE_EXPR)
    assert collected, "the merge selection collected nothing: the five files were not found"
    assert not (collected & LIVE_TEST_IDS), sorted(collected & LIVE_TEST_IDS)


def test_default_test_cmd_excludes_live():
    assert provisional.DEFAULT_TEST_CMD == f'python -m pytest -q -m "{MERGE_EXPR}"', provisional.DEFAULT_TEST_CMD


def test_live_selection_collects_exactly_the_live_tests():
    assert _collect("live") == LIVE_TEST_IDS
