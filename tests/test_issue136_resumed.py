"""#136 (unit 1): a subagent that ended (a `<task-notification>` with a non-running status) and was then
resumed by SendMessage is drawn RUNNING while its transcript holds a record newer than that last ended
notification; a later ended notification draws it ended again. Seam: `build_session_row` (its `subagents`
rows), reading a parent transcript and a `subagents/agent-<id>.jsonl` file from a tmp projects folder."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.agentops.dispatch import ended_task_ids
from tools.agentops.monitor_agents import build_session_row

SESSION = "sess-136"
T1 = datetime(2026, 10, 10, 11, 0, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
PRICES: dict = {}


def _stamp(at: datetime) -> str:
    return at.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _notification(ids: list[str], status: str, at: datetime | None) -> dict:
    parts = "".join(f"<task-id>{i}</task-id>\n" for i in ids)
    text = f"<task-notification>\n{parts}<status>{status}</status>\n</task-notification>"
    record = {"type": "user", "message": {"role": "user", "content": text}}
    if at is not None:
        record["timestamp"] = _stamp(at)
    return record


def _sub_record(at: datetime) -> dict:
    return {"type": "user", "timestamp": _stamp(at),
            "message": {"role": "user", "content": "resumed work"}}


def _build(tmp_path: Path, notifications: list[dict], subagents: dict[str, datetime]) -> list[dict]:
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / f"{SESSION}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in notifications) + "\n", encoding="utf-8")
    sub = projects / SESSION / "subagents"
    sub.mkdir(parents=True)
    for agent, last in subagents.items():
        (sub / f"agent-{agent}.jsonl").write_text(
            json.dumps(_sub_record(last - timedelta(seconds=1))) + "\n"
            + json.dumps(_sub_record(last)) + "\n", encoding="utf-8")
    row = build_session_row({"sessionId": SESSION}, projects_dir=projects, prices=PRICES, now=NOW)
    return row["subagents"]


def _row(rows: list[dict], agent: str) -> dict:
    return next(r for r in rows if r["agent_id"] == agent)


def test_resumed_after_completed_is_running(tmp_path):
    # completed at T1, then the subagent worked on at T1+5 min (SendMessage resume)
    rows = _build(tmp_path,
                  [_notification(["x1"], "completed", T1)],
                  {"x1": T1 + timedelta(minutes=5)})
    assert _row(rows, "x1")["running"] is True


def test_record_within_margin_after_completed_stays_ended(tmp_path):
    rows = _build(tmp_path,
                  [_notification(["x1"], "completed", T1)],
                  {"x1": T1 + timedelta(seconds=2)})
    assert _row(rows, "x1")["running"] is False


def test_record_older_than_completed_stays_ended(tmp_path):
    rows = _build(tmp_path,
                  [_notification(["x1"], "completed", T1)],
                  {"x1": T1 - timedelta(minutes=1)})
    assert _row(rows, "x1")["running"] is False


def test_second_completed_after_resume_ends_again(tmp_path):
    rows = _build(tmp_path,
                  [_notification(["x1"], "completed", T1),
                   _notification(["x1"], "completed", T1 + timedelta(minutes=10))],
                  {"x1": T1 + timedelta(minutes=5)})
    assert _row(rows, "x1")["running"] is False


def test_running_notification_does_not_end_nor_resume_check_reset(tmp_path):
    # a `running` notification is not an ended one: the last ended time stays T1, so the T1+5 record resumes
    rows = _build(tmp_path,
                  [_notification(["x1"], "completed", T1),
                   _notification(["x1"], "running", T1 + timedelta(minutes=2))],
                  {"x1": T1 + timedelta(minutes=5)})
    assert _row(rows, "x1")["running"] is True


def test_completed_without_timestamp_stays_ended(tmp_path):
    # no timestamp on the ended notification: no resume can be judged, so it counts as ended as today
    rows = _build(tmp_path,
                  [_notification(["x1"], "completed", None)],
                  {"x1": T1 + timedelta(minutes=5)})
    assert _row(rows, "x1")["running"] is False


def test_single_notification_still_ends_all_three_ids(tmp_path):
    # the #136 multi-id notification: one record, three ids, one status; all three ended
    rows = _build(tmp_path,
                  [_notification(["a1", "a2", "a3"], "stopped", T1)],
                  {"a1": T1 - timedelta(minutes=1), "a2": T1 - timedelta(minutes=2),
                   "a3": T1 - timedelta(minutes=3)})
    assert {r["agent_id"] for r in rows if not r["running"]} == {"a1", "a2", "a3"}
    assert ended_task_ids(tmp_path / "projects" / f"{SESSION}.jsonl") == {"a1", "a2", "a3"}


def test_row_carries_transcript_source(tmp_path):
    rows = _build(tmp_path,
                  [_notification(["x1"], "completed", T1)],
                  {"x1": T1 + timedelta(minutes=5)})
    assert _row(rows, "x1")["source"] == "transcript"
