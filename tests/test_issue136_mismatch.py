"""#136 mismatch check (tools/agentops/monitor_snapshots.py): each snapshot compares the helper states the tile drew
with the app-side state derived from the same snapshot's own inputs. A mismatch counts (`mismatches_confirmed`) only
when the newest earlier snapshot.json of the same out_root, taken no more than 3 loop intervals before, holds the
same mismatch too.

No desktop, no model call: the app list, the rows (`agents_fn`), the window rectangle and the grab are injected;
the transcripts are written under tmp_path. The one real-shaped test feeds the collector's own rows (`drawn_rows`).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tools.agentops import monitor_agents as ma
from tools.agentops import monitor_snapshots as ms

# A naive wall clock: the folder name is the wall clock of `now`, so the test needs no time zone.
NOW = datetime(2026, 10, 17, 12, 0, 0)
NEXT = NOW + timedelta(minutes=2)
EVERY_S = 120
SID = "sess-136-aaaa"
AG_IDS = ["ag1", "ag2", "ag3", "ag4"]
NOTIF_STAMP = "2026-10-17T11:50:00Z"
CAUSE_NOTIFIED = "ended notification newer than last record"
CAUSE_NO_TIME = "ended notification has no time"
CAUSE_PARENT = "parent session ended in the app"
CAUSE_FOREGROUND = "foreground tool_result answered"
# ag1, ag2 and ag3 are ended by one 3-id stopped notification at 11:50Z; ag4 is never notified (running).
MISMATCH = {"session": SID, "helper": "ag1", "tile": "running", "app": "ended", "cause": CAUSE_NOTIFIED}


def _write_lines(path: Path, records: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(records) + "\n", encoding="utf-8")


def _project(tmp_path: Path, *, ag1_last: str = "2026-10-17T11:31:00Z", notif_stamp: str | None = NOTIF_STAMP,
             foreground: bool = False) -> Path:
    """One parent transcript with a 3-id stopped notification (its time is `notif_stamp`; None = no time), and four
    subagent files. `ag1_last` is ag1's last record (a record later than the notification by more than RESUME_MARGIN
    is a resume). `foreground` adds a foreground helper `fg` whose tool_result sits in the parent transcript."""
    projects = tmp_path / "projects"
    notif = {
        "type": "queue-operation",
        "content": ("<task-notification><task-id>ag1</task-id>\n<task-id>ag2</task-id>\n"
                    "<task-id>ag3</task-id><status>stopped</status><summary>3 agents</summary>"
                    "</task-notification>"),
    }
    if notif_stamp is not None:
        notif["timestamp"] = notif_stamp
    records = [json.dumps({"type": "user", "timestamp": "2026-10-17T11:00:00Z",
                           "message": {"role": "user", "content": "start #136"}}),
               json.dumps(notif)]
    if foreground:
        records += [
            json.dumps({"type": "assistant", "timestamp": "2026-10-17T11:10:00Z",
                        "message": {"id": "m-fg-call", "role": "assistant",
                                    "content": [{"type": "tool_use", "id": "tu-fg", "name": "Agent", "input": {}}]}}),
            json.dumps({"type": "user", "timestamp": "2026-10-17T11:20:00Z",
                        "message": {"role": "user",
                                    "content": [{"type": "tool_result", "tool_use_id": "tu-fg", "content": "done"}]}}),
        ]
    _write_lines(projects / f"{SID}.jsonl", records)
    sub = projects / SID / "subagents"
    last = {"ag1": ag1_last, "ag2": "2026-10-17T11:40:00Z",
            "ag3": "2026-10-17T11:45:00Z", "ag4": "2026-10-17T11:55:00Z"}
    if foreground:
        last["fg"] = "2026-10-17T11:20:00Z"
    for agent, stamp in last.items():
        _write_lines(sub / f"agent-{agent}.jsonl", [
            json.dumps({"type": "user", "timestamp": "2026-10-17T11:00:00Z",
                        "message": {"role": "user", "content": f"task {agent} #136"}}),
            json.dumps({"type": "assistant", "timestamp": stamp,
                        "message": {"id": f"m-{agent}", "role": "assistant", "model": "claude-sonnet-5-5",
                                    "content": [{"type": "text", "text": "ok"}],
                                    "usage": {"input_tokens": 10, "output_tokens": 5}}}),
        ])
    if foreground:
        (sub / "agent-fg.meta.json").write_text(
            json.dumps({"agentId": "fg", "requestShape": "foreground", "toolUseId": "tu-fg"}), encoding="utf-8")
    return projects


def _live() -> list[dict]:
    return [{"sessionId": SID, "name": "orch #136", "kind": "interactive", "status": "idle",
             "cwd": "C:/repo"}]


def _rows(running: set[str], extra: tuple[str, ...] = ()) -> list[dict]:
    """The rows the tile would draw: one session with its helpers, `busy` is the drawn state."""
    return [{"sid": SID, "name": "orch #136", "status": "idle",
             "subagents": [{"id": agent, "busy": agent in running} for agent in AG_IDS + list(extra)]}]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _snap(out_root: Path, projects: Path, rows: list[dict], now: datetime, *,
          live: list[dict] | None = None) -> dict:
    """One snapshot with the rows injected (`agents_fn`); returns its saved JSON."""
    path = ms.take_snapshot(out_root, now=now, live_sessions_raw=lambda: _live() if live is None else live,
                            agents_fn=lambda app: rows, projects_dir=projects,
                            pane_cache=projects.parent / "no-cache", window_title="Claude",
                            pane_rect_fn=lambda title: None, every_s=EVERY_S)
    return _load(path)


# -- the pair rule: `mismatches` per snapshot, `mismatches_confirmed` only from the second in a row ---------------

def test_matching_pair_has_no_mismatch_in_either_snapshot(tmp_path):
    projects = _project(tmp_path)
    out_root = tmp_path / "out"
    rows = _rows({"ag4"})  # ag1..ag3 drawn ended (notified), ag4 drawn running (never notified)

    first = _snap(out_root, projects, rows, NOW)
    second = _snap(out_root, projects, rows, NEXT)

    assert first["mismatches"] == []
    assert second["mismatches"] == []
    assert second["mismatches_confirmed"] == []


def test_mismatching_pair_is_confirmed_only_from_the_second_snapshot(tmp_path):
    projects = _project(tmp_path)
    out_root = tmp_path / "out"
    rows = _rows({"ag1", "ag4"})  # the tile draws ag1 running; the notification ended it

    first = _snap(out_root, projects, rows, NOW)
    second = _snap(out_root, projects, rows, NEXT)

    assert first["mismatches"] == [MISMATCH]
    assert first["mismatches_confirmed"] == []  # no earlier snapshot holds it yet
    assert second["mismatches"] == [MISMATCH]
    assert second["mismatches_confirmed"] == [MISMATCH]


def test_mismatch_present_only_in_the_second_snapshot_is_not_confirmed(tmp_path):
    projects = _project(tmp_path)
    out_root = tmp_path / "out"

    first = _snap(out_root, projects, _rows({"ag4"}), NOW)
    second = _snap(out_root, projects, _rows({"ag1", "ag4"}), NEXT)

    assert first["mismatches"] == []
    assert second["mismatches"] == [MISMATCH]
    assert second["mismatches_confirmed"] == []


def test_an_earlier_snapshot_an_hour_old_does_not_confirm(tmp_path):
    projects = _project(tmp_path)
    out_root = tmp_path / "out"
    rows = _rows({"ag1", "ag4"})

    _snap(out_root, projects, rows, NOW - timedelta(hours=1))
    second = _snap(out_root, projects, rows, NOW)

    assert second["mismatches"] == [MISMATCH]
    assert second["mismatches_confirmed"] == []


def test_an_earlier_snapshot_within_three_intervals_confirms(tmp_path):
    projects = _project(tmp_path)
    out_root = tmp_path / "out"
    rows = _rows({"ag1", "ag4"})

    _snap(out_root, projects, rows, NOW - timedelta(minutes=5))  # 5 min < 3 x 120 s
    second = _snap(out_root, projects, rows, NOW)

    assert second["mismatches_confirmed"] == [MISMATCH]


# -- the derivation rules: a resume, a foreground helper, the parent, a notification with no time -----------------

def test_a_helper_resumed_after_its_notification_is_running_and_matches(tmp_path):
    # ag1 wrote a record at 11:51Z, a minute after its 11:50Z notification: a resume, so running in the app too.
    projects = _project(tmp_path, ag1_last="2026-10-17T11:51:00Z")

    data = _snap(tmp_path / "out", projects, _rows({"ag1", "ag4"}), NOW)

    assert data["mismatches"] == []


def test_finished_foreground_helper_gives_no_mismatch(tmp_path):
    # fg's tool_result is in the parent transcript: the tile draws it ended, the app side ends it the same way.
    projects = _project(tmp_path, foreground=True)

    data = _snap(tmp_path / "out", projects, _rows({"ag4"}, extra=("fg",)), NOW)

    assert data["mismatches"] == []


def test_foreground_helper_drawn_running_after_its_answer_is_a_mismatch_with_its_cause(tmp_path):
    projects = _project(tmp_path, foreground=True)

    data = _snap(tmp_path / "out", projects, _rows({"ag4", "fg"}, extra=("fg",)), NOW)

    assert data["mismatches"] == [{"session": SID, "helper": "fg", "tile": "running", "app": "ended",
                                   "cause": CAUSE_FOREGROUND}]


@pytest.mark.parametrize("live", [
    [],  # the parent is absent from the app list
    [{"sessionId": SID, "state": "done", "kind": "background"}],
    [{"sessionId": SID, "state": "stopped", "kind": "background"}],
    [{"sessionId": SID, "state": "failed", "kind": "background"}],
], ids=["absent", "done", "stopped", "failed"])
def test_a_parent_absent_or_ended_in_the_app_ends_its_busy_helpers(tmp_path, live):
    data = _snap(tmp_path / "out", _project(tmp_path), _rows({"ag1", "ag4"}), NOW, live=live)

    assert [(m["helper"], m["tile"], m["app"], m["cause"]) for m in data["mismatches"]] == [
        ("ag1", "running", "ended", CAUSE_PARENT),
        ("ag4", "running", "ended", CAUSE_PARENT),
    ]


def test_an_ended_notification_without_a_time_leaves_the_helper_ended_with_its_cause(tmp_path):
    data = _snap(tmp_path / "out", _project(tmp_path, notif_stamp=None), _rows({"ag1", "ag4"}), NOW)

    assert data["mismatches"] == [{"session": SID, "helper": "ag1", "tile": "running", "app": "ended",
                                   "cause": CAUSE_NO_TIME}]


# -- real-shaped: the collector's own rows (drawn_rows), not a hand-set `agents_fn` -----------------------------------

def test_real_collector_rows_with_ended_running_and_finished_foreground_helpers_give_no_mismatch(tmp_path, monkeypatch):
    projects = _project(tmp_path, foreground=True)
    monkeypatch.setattr(ma, "DEFAULT_PROJECTS_DIR", projects)  # the collector reads the transcripts from there
    collector = ms.load_collector()
    monkeypatch.setattr(collector, "opencode", lambda *a, **k: [])  # the local opencode server is not part of this test

    live = [dict(_live()[0], cwd=str(ma.ROOT))]  # the collector keeps only the sessions of this repo
    path = ms.take_snapshot(tmp_path / "out", now=NOW, live_sessions_raw=lambda: live, projects_dir=projects,
                            pane_cache=tmp_path / "no-cache", window_title="Claude",
                            pane_rect_fn=lambda title: None, every_s=EVERY_S)

    data = _load(path)
    drawn = {s["id"]: s["busy"] for row in data["rows"] for s in row["subagents"]}
    assert drawn == {"ag1": False, "ag2": False, "ag3": False, "ag4": True, "fg": False}
    assert data["mismatches"] == []
