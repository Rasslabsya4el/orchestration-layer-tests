"""Tests for tools/agentops/comment_markers.py: the machine-comment test judges the FIRST non-blank line only."""

from __future__ import annotations

import sys

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.agentops.comment_markers import is_machine_comment  # noqa: E402


def test_merge_result_marker_is_machine() -> None:
    assert is_machine_comment("<!-- merge result -->\nmerged #57 at 4ed5deef") is True


def test_runs_daemon_result_marker_is_machine() -> None:
    assert is_machine_comment("<!-- runs-daemon result -->\njob done") is True


def test_send_back_marker_is_machine() -> None:
    assert is_machine_comment("<!-- send-back -->\nnot accepted: the test is red") is True


def test_accepted_marker_is_machine() -> None:
    assert is_machine_comment("<!-- accepted -->\nthe report is accepted") is True


def test_parked_on_run_marker_is_machine() -> None:
    assert is_machine_comment("<!-- parked-on-run: job 3302c1a1 -->\nwaiting for the run") is True


def test_parked_on_merge_marker_is_machine() -> None:
    assert is_machine_comment("<!-- parked-on-merge: #57 -->\nwaiting for the merge") is True


def test_owner_answer_is_not_machine() -> None:
    assert is_machine_comment("<!-- owner-answer -->\nDo the research on 15.10 as a ticket.") is False


def test_plain_brain_comment_is_not_machine() -> None:
    assert is_machine_comment("Brain (map #22), the choice for unit 3: keep the old path.") is False


def test_checkpoint_of_an_orch_session_is_machine() -> None:
    assert is_machine_comment("Checkpoint 1 (ORCH 3302c1a1, #53). unit 1 merged, next unit 2.") is True


def test_diagnosis_of_an_orch_session_is_machine() -> None:
    assert is_machine_comment("Diagnosis (ORCH 3302c1a1, #53 unit 2 of 3, the cache is stale).") is True


def test_closing_and_loop_stop_of_an_orch_session_are_machine() -> None:
    assert is_machine_comment("Closing (ORCH 3302c1a1, #53). all units merged.") is True
    assert is_machine_comment("Loop stop (ORCH 3302c1a1, #53). three send-backs.") is True


def test_orch_finishing_line_is_machine() -> None:
    assert is_machine_comment("ORCH a241f462 finishing: still working this issue, takes nothing new; "
                              "if the session ends, the next one continues from the last checkpoint above") is True


def test_checkpoint_word_without_orch_is_not_machine() -> None:
    assert is_machine_comment("Checkpoint 1 (brain, #53). the owner wants a table.") is False


def test_marker_only_on_second_line_is_judged_by_first_line() -> None:
    body = "Brain (map #22), the choice for unit 3.\n<!-- merge result -->\nmerged"
    assert is_machine_comment(body) is False


def test_leading_blank_lines_are_skipped_to_the_first_non_blank_line() -> None:
    assert is_machine_comment("\n\n   \n<!-- send-back -->\nnot accepted") is True


def test_empty_body_is_machine() -> None:
    assert is_machine_comment("") is True
    assert is_machine_comment("   \n\n") is True


@pytest.mark.parametrize("body", [
    "**Checkpoint 2 (ORCH 3302c1a1, #53)**\nmore",
    "## Checkpoint 1 (ORCH addaabc): claimed",
    "<!-- parked-on-merge -->\nparked",
    "Title before the rewrite (2026-10-12): old title",
    "Autopilot checkpoint\nstate",
    "The runs daemon: notebook unreachable",
    "Another red full run names the same test",
])
def test_markdown_checkpoints_and_tool_lines_are_machine(body):
    assert is_machine_comment(body) is True


def test_a_brain_comment_with_markdown_is_still_a_person():
    assert is_machine_comment("**Brain (autopilot #12, session 3):** add row 4 to the table") is False
