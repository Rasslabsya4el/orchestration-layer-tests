"""#340 unit 4 (Тикеты tab): web/monitor/tab-tickets.js, checked against a real collect.py tickets() capture.

The behaviour is checked by a node script (tests/issue340_tab_tickets_check.mjs) that imports the tab and renders the
fixture (tests/fixtures/monitor/tickets.json, 2 goals and 36 loose tickets from `python mods/pob-slots/bin/collect.py
tickets`). This file runs it and asserts each named check. Without node the checks skip.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECK = ROOT / "tests" / "issue340_tab_tickets_check.mjs"
FIXTURE = ROOT / "tests" / "fixtures" / "monitor" / "tickets.json"


@pytest.fixture(scope="module")
def report() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed: the Тикеты tab checks need node 24")
    proc = subprocess.run(
        [node, str(CHECK), str(FIXTURE)], capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=str(ROOT),
    )
    assert proc.returncode == 0, f"node check failed to run: {proc.stderr[-2000:]}"
    return json.loads(proc.stdout)


def _check(report: dict, name: str) -> None:
    assert name in report, f"node check {name} did not report"
    assert report[name]["ok"], f"{name}: {report[name]['detail']}"


def test_fixture_is_a_real_capture_within_size() -> None:
    assert FIXTURE.stat().st_size <= 200 * 1024
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))["tickets"]["data"]
    assert set(data) >= {"at", "goals", "loose", "groups", "issues"}


def test_tab_contract(report: dict) -> None:
    _check(report, "module_exports")


def test_legend_has_eight_states(report: dict) -> None:
    _check(report, "legend_eight_states")
    _check(report, "section_titles")


def test_every_goal_and_loose_ticket_rendered(report: dict) -> None:
    _check(report, "roots_rendered_n_of_n")
    _check(report, "root_rows_state_dot")
    _check(report, "root_rows_link_from_repo")
    _check(report, "root_rows_done_count")


def test_collapsed_tree_shows_no_children(report: dict) -> None:
    _check(report, "collapsed_hides_children")


def test_expanded_tree_shows_every_node(report: dict) -> None:
    _check(report, "expanded_all_n_of_n")
    _check(report, "expanded_children_depth_and_state")


def test_blocked_and_parked_marks(report: dict) -> None:
    _check(report, "blocked_marks")
    _check(report, "parked_marks")


def test_held_mark_comes_from_the_fast_read(report: dict) -> None:
    _check(report, "held_marks_from_fast")
    _check(report, "no_held_without_fast")


def test_links_follow_the_chosen_repo(report: dict) -> None:
    _check(report, "no_link_without_repo")
    _check(report, "no_link_for_bare_label")
    _check(report, "no_link_for_unknown_repo")
    _check(report, "link_follows_chosen_repo")


def test_fast_failure_and_ui_contract(report: dict) -> None:
    _check(report, "fast_failure_no_held")
    _check(report, "stale_fast_error_no_held")
    _check(report, "ui_state_map_expands_nothing")
    _check(report, "held_notice_when_fast_fails")


def test_error_panel_and_last_good_tree(report: dict) -> None:
    _check(report, "error_panel_without_data")
    _check(report, "error_keeps_last_good_tree")
    _check(report, "error_text_escaped")
    _check(report, "missing_snapshot_is_loading_not_empty")
