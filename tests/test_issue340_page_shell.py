"""#340 unit 2 (page shell): web/monitor/shell.js, app.js, index.html, style.css.

The behaviour is checked by a node script (tests/issue340_page_shell_check.mjs) on shell.js with stubs for fetch,
clock and document; this file runs it and asserts each named check. Without node the node checks skip; the
no-external-URL check and the static checks run anyway.
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB = ROOT / "web" / "monitor"
CHECK = ROOT / "tests" / "issue340_page_shell_check.mjs"


@pytest.fixture(scope="module")
def report() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed: the page shell checks need node 24")
    proc = subprocess.run(
        [node, str(CHECK)], capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=str(ROOT),
    )
    assert proc.returncode == 0, f"node check failed to run: {proc.stderr[-2000:]}"
    return json.loads(proc.stdout)


def _check(report: dict, name: str) -> None:
    assert name in report, f"node check {name} did not report"
    assert report[name]["ok"], f"{name}: {report[name]['detail']}"


def test_tabs_sorted_by_order(report: dict) -> None:
    _check(report, "sort_tabs_by_order")
    _check(report, "driver_tabs_sorted_by_order")


def test_failed_fetch_banner_names_mode_and_error(report: dict) -> None:
    _check(report, "banner_names_mode_and_error")
    _check(report, "driver_failed_fetch_banner_names_mode")
    _check(report, "driver_keeps_last_good_data_on_failure")


def test_banners_clear_on_recovery(report: dict) -> None:
    _check(report, "driver_banner_clears_on_recovery")
    _check(report, "driver_tabs_banner_clears_after_load")
    _check(report, "driver_tab_render_banner_clears")
    _check(report, "driver_tab_import_banner_clears")


def test_poll_interval_equals_answered_refresh_s(report: dict) -> None:
    _check(report, "poll_interval_defaults_and_answer")
    _check(report, "driver_poll_interval_equals_answered_refresh_s")


def test_ui_state_survives_rerender(report: dict) -> None:
    _check(report, "driver_ui_state_survives_rerender")


def test_ui_set_persists_and_rerenders(report: dict) -> None:
    _check(report, "driver_ui_set_persists_and_rerenders")
    _check(report, "parse_set_pair")


def test_app_wires_data_set_clicks() -> None:
    text = (WEB / "app.js").read_text(encoding="utf-8")
    for needle in ["data-set", "parseSet(", "shell.ui.set("]:
        assert needle in text, f"app.js does not use {needle}"


def test_first_failed_read_renders_tab_card(report: dict) -> None:
    _check(report, "driver_first_read_error_renders_tab_card")


def test_placeholder_until_every_endpoint_answers(report: dict) -> None:
    _check(report, "driver_placeholder_until_every_endpoint_answers")


def test_snapshot_repos_and_ui_repo(report: dict) -> None:
    _check(report, "driver_snapshot_repos_and_ui_repo")


def test_rerender_keeps_reader_place(report: dict) -> None:
    _check(report, "capture_restore_view_keeps_place")
    _check(report, "capture_restore_view_repo_focus_and_no_focus")
    _check(report, "capture_restore_view_set_focus")


def test_repo_switch_contract(report: dict) -> None:
    _check(report, "reposOf_api_list")
    _check(report, "tab_from_hash")
    _check(report, "hash_parse_and_build")
    _check(report, "driver_repos_api_default_first")
    _check(report, "driver_single_repo_hides_switch")
    _check(report, "driver_repos_failure_hides_switch")
    _check(report, "driver_repo_from_hash_survives_reload")
    _check(report, "driver_repo_switch_rereads_now")
    _check(report, "driver_stale_answer_dropped")
    _check(report, "driver_tab_switch_keeps_repo")
    _check(report, "driver_route_hash_change")


def test_pure_pieces(report: dict) -> None:
    _check(report, "tip_placement_below_and_above")
    _check(report, "escape_and_copy_control")
    _check(report, "driver_copy_control")


# The features the issue excludes (feed, waiting on the owner, «Где мы» floors, the build-tree slider, the schedule)
# and the mockup's tabs do not show them. Each pattern is a selector in style.css.
EXCLUDED_CSS = [
    r"\.feed\b", r"\.fi\b", r"\.mk\b",
    r"\.wr\b",
    r"\.goal\b", r"\.floor\b", r"\.tc\b", r"\.gkey\b", r"\.gin\b", r"\.gscroll\b",
    r"\.play \.sl\b", r"\.play \.trk\b", r"\.play \.th\b", r"input\[type=range\]",
    r"\.epg\b", r"\.erow\b", r"\.blk\b", r"\.nowline\b",
    r"\.(elab|etrack|eend|tick|tickl|night|mid|dband|freeb|closedb|nowtag)\b",
]


def test_css_drops_excluded_features() -> None:
    css = re.sub(r"/\*.*?\*/", "", (WEB / "style.css").read_text(encoding="utf-8"), flags=re.S)
    hits = [p for p in EXCLUDED_CSS if re.search(p, css)]
    assert not hits, f"style.css still has rules of excluded features: {hits}"


def test_app_keeps_reader_place_and_repo_hash() -> None:
    text = (WEB / "app.js").read_text(encoding="utf-8")
    for needle in ["captureView(", "restoreView(", "setHash(", "shell.route(", "shell.select("]:
        assert needle in text, f"app.js does not use {needle}"


PAGE_FILES = ["index.html", "app.js", "style.css", "shell.js"]
EXTERNAL = re.compile(r"https?://|(?<![:\w])//[\w-]+(\.[\w-]+)+")


@pytest.mark.parametrize("name", PAGE_FILES)
def test_no_external_url_in_page_files(name: str) -> None:
    path = WEB / name
    assert path.is_file(), f"{path} is missing"
    text = path.read_text(encoding="utf-8")
    hits = [m.group(0) for m in EXTERNAL.finditer(text)]
    assert not hits, f"{name} makes an external request or links out: {hits}"
