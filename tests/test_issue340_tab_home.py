"""#340 unit 3 (tab Главная): web/monitor/tab-home.js, rows 1-4 of the pane's main tab.

The behaviour is checked by a node script (tests/issue340_tab_home_check.mjs) that renders the recorded `fast` snapshot
(tests/fixtures/monitor/fast.json) and an empty `tickets` snapshot; this file runs it and asserts each named check.
Without node the node checks skip; the static check runs anyway.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TAB = ROOT / "web" / "monitor" / "tab-home.js"
CHECK = ROOT / "tests" / "issue340_tab_home_check.mjs"


@pytest.fixture(scope="module")
def report() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed: the Главная tab checks need node 24")
    proc = subprocess.run(
        [node, str(CHECK)], capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=str(ROOT),
    )
    assert proc.returncode == 0, f"node check failed to run: {proc.stderr[-2000:]}"
    return json.loads(proc.stdout)


def _check(report: dict, name: str) -> None:
    assert name in report, f"node check {name} did not report"
    assert report[name]["ok"], f"{name}: {report[name]['detail']}"


def test_module_contract(report: dict) -> None:
    _check(report, "module_contract")


def test_rows_1_to_4_present(report: dict) -> None:
    _check(report, "rows_1_to_4_present")


def test_agents_row_sessions_and_whole_lists(report: dict) -> None:
    _check(report, "agents_sessions_and_sections")
    _check(report, "agents_whole_subagent_lists")
    _check(report, "agents_subagent_titles_and_words")


def test_merge_row(report: dict) -> None:
    _check(report, "merge_running_queue_header_and_eta")
    _check(report, "merge_waiting_row")
    _check(report, "merge_landed_reasons")
    _check(report, "merge_landed_capped_then_toggle")
    _check(report, "merge_lost_rows_from_tickets")


def test_pc_row(report: dict) -> None:
    _check(report, "pc_cpu_ram_split_and_sub")


def test_footer_row(report: dict) -> None:
    _check(report, "footer_time_and_version")


def test_failed_reads_show_their_error_in_the_panel(report: dict) -> None:
    _check(report, "merge_error_shown_in_panel")
    _check(report, "agents_error_shown_in_panel")
    _check(report, "machine_error_shown_in_panel")
    _check(report, "version_error_shown_in_footer")
    _check(report, "fast_read_failure_keeps_data_and_names_error")


def test_no_empty_panel_where_data_exists(report: dict) -> None:
    _check(report, "no_empty_panel_where_data_exists")


def test_payload_text_escaped_and_no_tickets_crash(report: dict) -> None:
    _check(report, "escapes_text_from_payload")
    _check(report, "no_tickets_snapshot_does_not_crash")


def test_no_hardcoded_repo_or_path() -> None:
    text = TAB.read_text(encoding="utf-8") if TAB.exists() else ""
    assert TAB.exists(), "web/monitor/tab-home.js is missing"
    for word in ("orchestration-layer", "Rasslabsya4el", "C:\\", "C:/"):
        assert word not in text, f"{word!r} is hardcoded in tab-home.js"
