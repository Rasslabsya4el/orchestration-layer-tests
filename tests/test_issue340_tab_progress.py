"""#340 unit 6 (tab Прогресс, parity rows 8-9): web/monitor/tab-progress.js.

The render is checked by a node script (tests/issue340_tab_progress_check.mjs) on the real captured snapshots
(tests/fixtures/monitor/progress_fast.json and progress_tickets.json); this file runs it and asserts each named check.
Without node the checks skip.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECK = ROOT / "tests" / "issue340_tab_progress_check.mjs"
EXPECTED_CHECKS = 47


@pytest.fixture(scope="module")
def report() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed: the progress tab checks need node 24")
    proc = subprocess.run(
        [node, str(CHECK)], capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=str(ROOT),
    )
    assert proc.returncode == 0, f"node check failed to run: {proc.stderr[-2000:]}"
    return json.loads(proc.stdout)


def _check(report: dict, name: str) -> None:
    assert name in report, f"node check {name} did not report"
    assert report[name]["ok"], f"{name}: {report[name]['detail']}"


def test_contract_and_load(report: dict) -> None:
    _check(report, "load")
    _check(report, "contract")


def test_pipeline_card_and_every_line(report: dict) -> None:
    _check(report, "render_full")
    _check(report, "pipe_card")
    rows = [name for name in report if name.startswith("row_")]
    assert len(rows) == 18, f"pipeline lines reported: {len(rows)}"
    for name in rows:
        _check(report, name)
    _check(report, "countdown_parked")
    _check(report, "escapes_title")


def test_groups_cards_and_every_share_bar(report: dict) -> None:
    _check(report, "groups_card_tickets")
    _check(report, "groups_card_other")
    groups = [name for name in report if name.startswith("group_")]
    assert len(groups) == 6, f"group rows reported: {len(groups)}"
    for name in groups:
        _check(report, name)


def test_head_row_links_and_zero_counts(report: dict) -> None:
    _check(report, "head_prog-tickets")
    _check(report, "head_prog-other")
    _check(report, "link_repo_label")
    _check(report, "link_no_entry_plain")
    _check(report, "link_label_without_slash_plain")
    zero = [name for name in report if name.startswith("zero_group_")]
    assert len(zero) == 5, f"zero-count group rows reported: {len(zero)}"
    for name in zero:
        _check(report, name)
    _check(report, "zero_card_other_head_only")


def test_error_text_shown_never_an_empty_list(report: dict) -> None:
    _check(report, "error_tickets")
    _check(report, "error_pipeline")
    _check(report, "error_keeps_last_good")
    _check(report, "loading")


def test_all_checks_ran_and_passed(report: dict) -> None:
    assert len(report) == EXPECTED_CHECKS, f"checks reported: {len(report)} of {EXPECTED_CHECKS}"
    failed = sorted(name for name, r in report.items() if not r["ok"])
    assert not failed, f"failed checks: {failed}"
