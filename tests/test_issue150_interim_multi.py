"""#150: an interim `<task-notification>` that lists SEVERAL task ids with one status gives every id of
it that status and note, as `interim_task_times` does for one id (#977). Rows: (1) one interim
notification with 3 ids -> all 3 with the record time; (2) a 3-id notification that is not interim
drops all 3 from an earlier interim state; (3) a single-id interim notification keeps working."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools.agentops.dispatch import INTERIM_NOTE_MARK, interim_task_times

IDS = ("a1b2c3d4e5f60718", "b2c3d4e5f6071829", "c3d4e5f60718293a")
NOW = datetime(2026, 10, 18, 9, 0, 0, tzinfo=timezone.utc)


def _stamp(at: datetime) -> str:
    return at.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _line(ids: tuple[str, ...], *, status: str = "completed", note: bool = False,
          at: datetime = NOW) -> str:
    """One parent-transcript record. The ids sit side by side, one per line inside the text, so the
    raw JSONL line carries a literal `\\n` between them, as `ended_task_ids` expects (#136)."""

    id_tags = "\n".join(f"<task-id>{task}</task-id>" for task in ids)
    note_tag = f"<note>This agent {INTERIM_NOTE_MARK}. It may resume on its own.</note>\n" if note else ""
    text = (f"<task-notification>\n{id_tags}\n<output-file>C:\\Temp\\x.output</output-file>\n"
            f"<status>{status}</status>\n<summary>Agents finished</summary>\n{note_tag}"
            f"<result>still running</result>\n</task-notification>")
    return json.dumps({"type": "user", "message": {"role": "user", "content": text},
                       "timestamp": _stamp(at)})


def _transcript(tmp_path: Path, lines: list[str]) -> Path:
    path = tmp_path / "parent.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_row1_three_ids_in_one_interim_notification_all_count(tmp_path: Path):
    path = _transcript(tmp_path, [_line(IDS, note=True, at=NOW)])

    assert interim_task_times(path) == {task: NOW for task in IDS}


@pytest.mark.parametrize("status", ["completed", "running"])
def test_row2_three_ids_not_interim_drop_all_three_from_an_earlier_interim_state(tmp_path: Path, status: str):
    earlier = NOW - timedelta(minutes=5)
    # one interim notification per id, so each of the three is interim before the multi-id record
    lines = [_line((task,), note=True, at=earlier) for task in IDS]
    lines.append(_line(IDS, status=status, note=False, at=NOW))

    assert interim_task_times(_transcript(tmp_path, lines)) == {}


def test_row3_single_id_interim_notification_still_counts(tmp_path: Path):
    path = _transcript(tmp_path, [_line(IDS[:1], note=True, at=NOW)])

    assert interim_task_times(path) == {IDS[0]: NOW}
