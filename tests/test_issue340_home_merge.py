"""#340 unit 3b review-fix (Главная, Merge panel): web/monitor/home-merge.js and web/monitor/home-util.js.

The behaviour is checked by a node script (tests/issue340_home_merge_check.mjs) that renders the recorded `fast` snapshot
(tests/fixtures/monitor/fast.json) and synthetic variants of its merge read; this file runs it and asserts each named check.
Without node the node checks skip; the static checks run anyway.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MERGE = ROOT / "web" / "monitor" / "home-merge.js"
UTIL = ROOT / "web" / "monitor" / "home-util.js"
CHECK = ROOT / "tests" / "issue340_home_merge_check.mjs"


@pytest.fixture(scope="module")
def report() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed: the Главная merge checks need node 24")
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


def test_running_track_draws_tests_done_over_total(report: dict) -> None:
    _check(report, "running_track_from_tests")
    _check(report, "running_track_falls_back_to_percent")
    _check(report, "running_track_empty_is_zero")


def test_full_run_row_and_track(report: dict) -> None:
    _check(report, "full_run_track_and_row")
    _check(report, "failed_full_run")


def test_commit_holder_lists_every_issue(report: dict) -> None:
    _check(report, "commit_holder_lists_every_issue")


def test_core_update_running_and_waiting_rows(report: dict) -> None:
    _check(report, "core_update_running_row")
    _check(report, "core_update_waiting_row")


def test_failed_running_merge(report: dict) -> None:
    _check(report, "failed_running_merge")


def test_failed_reads_keep_last_good_and_name_the_error(report: dict) -> None:
    _check(report, "tickets_error_shown_with_rows")
    _check(report, "merge_error_keeps_last_good")
    _check(report, "fast_error_keeps_last_good")


def test_one_human_duration_rule_in_shared_module(report: dict) -> None:
    _check(report, "human_durations_one_rule")
    _check(report, "no_local_duration_copies")
    _check(report, "no_bare_seconds_in_panel")


def test_payload_text_escaped_and_no_hardcoded_path(report: dict) -> None:
    _check(report, "escapes_text_from_payload")
    _check(report, "no_hardcoded_repo_or_path")


def test_util_file_exists() -> None:
    assert UTIL.exists(), "web/monitor/home-util.js is missing"
    assert MERGE.exists(), "web/monitor/home-merge.js is missing"
