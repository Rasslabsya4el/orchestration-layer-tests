"""#136 row 1 on the real file: the parent transcript of the 3-agent stop (session b4c071c2) names three
ended subagents. The fixture keeps only its two records that hold the 3-id `stopped` `<task-notification>`
(a queue-operation line and a user line; ended_task_ids gives the same 3 ids on the full 1.2 MB file); the synthetic transcripts of test_issue136_monitor_stale.py do
not cover the real record shapes (queue-operation and user lines)."""

from __future__ import annotations

from pathlib import Path

from tools.agentops.dispatch import ended_task_ids

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "issue136_real_notifications.jsonl"
EXPECTED_IDS = ["abb7148f62f9c74b5", "ac59b1c50cce1ee31", "aeed1c37d752ff068"]


def test_real_file_fixture_returns_the_three_ids():
    assert sorted(ended_task_ids(FIXTURE)) == EXPECTED_IDS
