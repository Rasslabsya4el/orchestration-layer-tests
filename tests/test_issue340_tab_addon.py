"""#340 unit 8: web/monitor/tab-addon.js renders the add-on's `addon:fast` fixture (tests/fixtures/monitor/addon_fast.json)
through node. The assertions are on the rendered outcome (the cells and lines a reader sees), not on the markup.

The checks run in tests/issue340_tab_addon_check.mjs (node 24, no DOM). Without node the tests skip.
    python -m pytest -q -p no:faulthandler tests/test_issue340_tab_addon.py
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECK = ROOT / "tests" / "issue340_tab_addon_check.mjs"
FIXTURE = ROOT / "tests" / "fixtures" / "monitor" / "addon_fast.json"


@pytest.fixture(scope="module")
def report() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed: the add-on tab is rendered by node 24")
    proc = subprocess.run(
        [node, str(CHECK)], capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(ROOT),
    )
    assert proc.returncode == 0, f"node check failed to run: {proc.stderr[-2000:]}"
    return json.loads(proc.stdout)


def test_tab_contract(report):
    assert report["id"] == "addon"
    assert report["title"] == "Дополнение"
    assert report["order"] == 60
    assert report["endpoints"] == ["addon:fast"]


def test_the_cards_and_the_status_lines_are_drawn(report):
    assert report["laptop_card"] is True
    assert report["runs_card"] is True
    assert report["readings_card"] is True
    assert report["status_shown"] is True
    assert report["slots_shown"] is True
    assert report["daemon_shown"] is True


def test_running_run_rows_show_their_cells(report):
    assert len(report["running_cells"]) == report["running_expected"] == 2
    # a run on the PC with its progress and its ETA, then one on the laptop with no link
    assert report["running_cells"][0] == ["P1-#801-1", "801 run", "#801", "PC", "3", "1/3 ~20:05", "ещё ~17 min", "benchmark"]
    assert report["running_cells"][1][3] == "laptop"
    assert report["running_cells"][1][6] == "нет связи с ноутом, данные от 19:03"
    assert report["stale_marked"] is True


def test_a_running_build_is_drawn_under_its_run(report):
    assert report["build_cells"] == [
        ["↳", "Duelist build", "Duelist", "—", "—", "этап 2 / 5", "считается", "5 min"],
    ]


def test_queued_rows_show_their_wait_reason(report):
    assert len(report["queued_cells"]) == report["queued_expected"] == 3
    assert report["queued_cells"][0] == ["P1-#803-1", "803 run", "#803", "PC", "2", "normal", "держит упавший #799", "12 min"]
    assert report["queued_cells"][1][6] == "ждёт P1-#803-1 · ~1 h"
    assert report["queued_cells"][2][6] == "ждёт RAM"


def test_recent_and_history_rows_are_all_drawn(report):
    assert report["recent_rows"] == report["recent_expected"] == 8
    assert report["history_rows"] == report["history_expected"] == 20
    assert report["history_cells"][0] == ["P1-#1091-3", "P1 #1091", "тесты", "остановлен", "2/3", "17.10 23:39", "14 h 21 min"]
    assert report["history_cells"][1] == ["P1-#1154-3", "P1 #1154", "replay", "готово", "1/1", "18.10 11:23", "1.9 min"]


def test_readings_rows_and_their_subline_are_drawn(report):
    assert report["readings_rows"] == report["readings_expected"] == 2
    assert report["readings_sub"] == "2026-10-19 09:33 · core14 P2-core-2 (20261017-2102) · P1-#1160"
    assert "Cold Dot Duelist" in report["first_reading"]
    assert "Gladiator 161 %" in report["first_reading"]


def test_laptop_card_with_a_link_shows_its_slots_and_cores(report):
    laptop = report["laptop"]
    assert laptop["squares"] == 6
    assert laptop["caption"] == "0 / 6"
    assert laptop["cores"] == "12 ядер"
    assert "нет связи с 19:03 · 53 min" in laptop["lines"]
    assert "что было запущено на ноуте, монитор не застал" in laptop["lines"]
    assert "шло: P2-#802-1 · 2 слота · 1/4" in laptop["lines"]


def test_laptop_offline_with_only_its_last_sight_names_that_time(report):
    laptop = report["laptop_seen_only"]
    assert "нет связи с 18:40 · 1 h 23 min" in laptop["lines"]
    assert not any("19:03" in line for line in laptop["lines"])


def test_a_repo_without_the_add_on_gets_one_line(report):
    assert report["no_addon_line"] is True
    assert report["no_addon_has_cards"] is False


def test_no_collector_shows_the_error_and_no_placeholder(report):
    assert report["no_collector_banner"] is True
    assert report["no_collector_line"] is False
    assert report["no_collector_no_placeholder"] is True


def test_a_failed_read_shows_its_error_text(report):
    assert report["failed_error_shown"] is True
    assert report["failed_with_data_shows_both"] is True
    assert report["missing_envelope_shows_error"] is True


def test_text_from_the_data_is_escaped(report):
    assert report["status_escaped"] is True


def test_no_repo_name_is_hardcoded_in_the_tab(report):
    assert report["no_repo_name"] is True


def test_fixture_is_small_and_has_no_drive_letter():
    assert len(FIXTURE.read_bytes()) < 150 * 1024
    assert "C:" not in FIXTURE.read_text(encoding="utf-8")


def test_the_host_card_is_named_by_the_payload_with_a_neutral_fallback(report):
    assert report["card_name_default"] == "Add-on"
    assert report["card_name_given"] == "Build host &lt;b&gt;"

