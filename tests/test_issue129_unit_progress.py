"""#129: the held issues of the ORCH row carry `units`, the `done/total` of a sliced issue's `k/n` outcome rows.

    python -m pytest -q tests/test_issue129_unit_progress.py
"""
from pathlib import Path

import pytest

from tools.agentops import monitor_agents as ma


def _row(ts: int, issue: int, unit: str | None, cls: str) -> dict:
    row = {"ts": ts, "issue": issue, "route": "worker", "class": cls, "why": "", "session": "s"}
    if unit is not None:
        row["unit"] = unit
    return row


# one outcome fixture, rows in ts order
ROWS = [
    _row(1, 50, "1/3", "accepted"),
    _row(2, 50, "2/3", "redone"),
    _row(3, 51, "1/2", "accepted"),
    _row(4, 50, "2/3", "accepted"),
    _row(5, 51, "1/4", "accepted"),
    _row(6, 52, None, "accepted"),
    _row(7, 53, "1/1", "accepted"),
    _row(8, 50, "3/3", "redone"),
    _row(9, 51, "2/4", "accepted"),
    _row(10, 54, "3/3", "accepted"),
    _row(11, 55, "1/3", "accepted"),
    _row(12, 55, "1/3", "wrong-fact"),
]


@pytest.mark.parametrize("issue, expected", [
    (50, {"done": 3, "total": 3}),  # #160: redone counts as done
    (51, {"done": 2, "total": 4}),
    (52, None),
    (53, None),
    (54, {"done": 1, "total": 3}),
    (55, {"done": 0, "total": 3}),
])
def test_unit_progress_fixture(issue, expected):
    assert ma.unit_progress(ROWS, issue) == expected


def test_rows_of_issue_50_do_not_change_issue_54():
    without_50 = [row for row in ROWS if row["issue"] != 50]
    assert ma.unit_progress(ROWS, 54) == ma.unit_progress(without_50, 54) == {"done": 1, "total": 3}


def test_latest_row_is_by_ts_not_by_file_order():
    # #160: the newest row was redone, which now counts as done; wrong-fact keeps the test's point
    rows = [_row(5, 60, "1/3", "wrong-fact"), _row(2, 60, "1/3", "accepted"), _row(1, 60, "2/3", "accepted"),
            _row(3, 60, "3/3", "accepted")]
    assert ma.unit_progress(rows, 60) == {"done": 2, "total": 3}


def test_newest_cut_decides_total_and_older_cut_is_ignored():
    rows = [_row(1, 61, "1/2", "accepted"), _row(2, 61, "1/4", "accepted"), _row(3, 61, "2/4", "accepted")]
    assert ma.unit_progress(rows, 61) == {"done": 2, "total": 4}


def test_attach_held_issues_units_key_and_none_without_outcomes():
    row = {"subagents": []}
    orch_state = {"held": [50, 52], "claimed_now": [], "claimed_at": {}}
    ma.attach_held_issues(row, orch_state, ROWS)
    by_number = {held["number"]: held for held in row["held_issues"]}
    assert by_number[50]["units"] == {"done": 3, "total": 3}
    assert by_number[52]["units"] is None
    assert all("units" in held for held in row["held_issues"])

    bare = {"subagents": []}
    ma.attach_held_issues(bare, orch_state)
    assert [held["units"] for held in bare["held_issues"]] == [None, None]


def _orch_panel(tmp_path: Path, outcomes_path: Path) -> dict:
    sessions = [{"sessionId": "orchsess", "name": "orch", "cwd": None, "kind": "background", "status": "working"}]
    orch_state = {"session": "orchsess", "held": [50, 54], "claimed_now": [], "claimed_at": {}}
    return ma.panel_agents(sessions, projects_dir=tmp_path, prices={}, repo_cwd=tmp_path,
                           orch_state=orch_state, outcomes_path=outcomes_path)


def test_panel_agents_units_from_log(tmp_path):
    import json

    log = tmp_path / "outcomes.jsonl"
    log.write_text("\n".join(json.dumps(row) for row in ROWS) + "\n", encoding="utf-8")
    panel = _orch_panel(tmp_path, log)
    held = {h["number"]: h for h in panel["sessions"][0]["held_issues"]}
    assert held[50]["units"] == {"done": 3, "total": 3}
    assert held[54]["units"] == {"done": 1, "total": 3}


def test_panel_agents_missing_log_gives_none_and_returns(tmp_path):
    panel = _orch_panel(tmp_path, tmp_path / "no-such-log.jsonl")
    held = panel["sessions"][0]["held_issues"]
    assert [h["units"] for h in held] == [None, None]


def test_panel_agents_unreadable_log_gives_none_and_returns(tmp_path):
    # a folder where the log file should be: reading it raises OSError, which must not escape
    panel = _orch_panel(tmp_path, tmp_path)
    held = panel["sessions"][0]["held_issues"]
    assert [h["units"] for h in held] == [None, None]
