"""#340 unit 3c review-fix: web/monitor/home-pc.js (PC panel) and web/monitor/home-footer.js (footer).

The behaviour is checked by a node script (tests/issue340_home_pc_check.mjs) that renders the recorded `fast` snapshot
(tests/fixtures/monitor/fast.json) with a ui stub that has a `state` object; this file runs it and asserts each named
check. Without node the node checks skip.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECK = ROOT / "tests" / "issue340_home_pc_check.mjs"


@pytest.fixture(scope="module")
def report() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed: the PC panel checks need node 24")
    proc = subprocess.run(
        [node, str(CHECK)], capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=str(ROOT),
    )
    assert proc.returncode == 0, f"node check failed to run: {proc.stderr[-2000:]}"
    return json.loads(proc.stdout)


def _check(report: dict, name: str) -> None:
    assert name in report, f"node check {name} did not report"
    assert report[name]["ok"], f"{name}: {report[name]['detail']}"


def test_ram_legend_in_gb_under_its_bar(report: dict) -> None:
    _check(report, "pc_ram_legend_in_gb_under_bar")
    _check(report, "pc_ram_legend_skips_visually_zero_parts")


def test_cpu_legend_stays_percent(report: dict) -> None:
    _check(report, "pc_cpu_legend_stays_percent")


def test_history_merge_part_only_with_a_merge_share(report: dict) -> None:
    _check(report, "pc_history_no_merge_part_without_share")
    _check(report, "pc_history_merge_part_with_share")


def test_no_extra_fact_block(report: dict) -> None:
    _check(report, "pc_no_facts_block")


def test_footer_time_cut_to_hh_mm(report: dict) -> None:
    _check(report, "footer_time_cut_to_hh_mm")


def test_footer_version_bare_v_only_when_integer(report: dict) -> None:
    _check(report, "footer_version_bare_v_when_integer")
    _check(report, "footer_version_not_integer_draws_nothing")
    _check(report, "footer_version_error_still_shown")
