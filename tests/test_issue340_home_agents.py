"""#340 unit 3a (tab Главная, Agents panel): web/monitor/home-agents.js, the review-fix pass against the pane.

The behaviour is checked by a node script (tests/issue340_home_agents_check.mjs) that renders the recorded `fast`
snapshot (tests/fixtures/monitor/fast.json); this file runs it and asserts each named check. The static checks run
without node. Without node the node checks skip.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PANEL = ROOT / "web" / "monitor" / "home-agents.js"
CHECK = ROOT / "tests" / "issue340_home_agents_check.mjs"


@pytest.fixture(scope="module")
def report() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed: the Agents panel checks need node 24")
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


def test_card_subtitle_busy_and_idle(report: dict) -> None:
    _check(report, "card_subtitle_busy_and_idle")


def test_chat_subagent_rows_keep_their_ticket(report: dict) -> None:
    _check(report, "chat_subagent_rows_keep_their_ticket")


def test_ticket_subagent_rows_have_no_ticket_number(report: dict) -> None:
    _check(report, "ticket_subagent_rows_have_no_ticket_number")


def test_finished_ticket_subagents_are_counts_not_rows(report: dict) -> None:
    _check(report, "finished_ticket_subagents_are_counts_not_rows")
    _check(report, "ticket_row_counts_finished_subagents")


def test_all_other_subagents_listed(report: dict) -> None:
    _check(report, "all_other_subagents_listed")


def test_parked_on_words(report: dict) -> None:
    _check(report, "parked_on_merge_wait_words")
    _check(report, "parked_on_needs_info_words")


def test_escapes_text_from_payload(report: dict) -> None:
    _check(report, "escapes_text_from_payload")


def test_parked_on_drops_the_add_on_branch() -> None:
    # Fix 4: the pane's JOB_QUEUES branch (the Poe-Junkroan add-on's job queues) is dropped here, and the comment says so.
    text = PANEL.read_text(encoding="utf-8")
    assert "JOB_QUEUES(" not in text and "JOB_WAIT" not in text, "the add-on job-queue branch is still in parkedOn"
    assert "add-on row P: unit 8" in text, "parkedOn does not say where the add-on branch goes (add-on row P: unit 8)"


def test_no_hardcoded_repo_or_path() -> None:
    text = PANEL.read_text(encoding="utf-8")
    for word in ("orchestration-layer", "Rasslabsya4el", "C:\\", "C:/"):
        assert word not in text, f"{word!r} is hardcoded in home-agents.js"
