"""#136 part B: the monitor snapshots (tools/agentops/monitor_snapshots.py), the API of commit c40e474.

No desktop, no model call: the app list, the rows (`agents_fn`), the window rectangle and the grab are all
injected; the transcripts are written under tmp_path.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

from tools.agentops import monitor_snapshots as ms

# A naive wall clock: the folder name is the wall clock of `now`, so the test needs no time zone.
NOW = datetime(2026, 10, 17, 12, 0, 0)
SID = "sess-136-aaaa"
REPO_NAME = "repo"
RECT = (100, 50, 900, 700)  # left, top, right, bottom


def _folder(when: datetime) -> str:
    return when.strftime("%Y%m%d-%H%M%S")


def _write_lines(path: Path, records: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(records) + "\n", encoding="utf-8")


def _project(tmp_path: Path) -> Path:
    """One parent transcript with a 3-id stopped notification, and four subagent files: ag1, ag2 and ag3
    are ended by that notification, ag4 is still running."""
    projects = tmp_path / "projects"
    notif = {
        "type": "queue-operation",
        "timestamp": "2026-10-17T11:50:00Z",
        "content": ("<task-notification><task-id>ag1</task-id>\n<task-id>ag2</task-id>\n"
                    "<task-id>ag3</task-id><status>stopped</status><summary>3 agents</summary>"
                    "</task-notification>"),
    }
    _write_lines(projects / f"{SID}.jsonl", [json.dumps({"type": "user", "timestamp": "2026-10-17T11:00:00Z",
                                                         "message": {"role": "user", "content": "start #136"}}),
                                             json.dumps(notif)])
    sub = projects / SID / "subagents"
    last = {"ag1": "2026-10-17T11:31:00Z", "ag2": "2026-10-17T11:40:00Z",
            "ag3": "2026-10-17T11:45:00Z", "ag4": "2026-10-17T11:55:00Z"}
    for agent, stamp in last.items():
        _write_lines(sub / f"agent-{agent}.jsonl", [
            json.dumps({"type": "user", "timestamp": "2026-10-17T11:00:00Z",
                        "message": {"role": "user", "content": f"task {agent} #136"}}),
            json.dumps({"type": "assistant", "timestamp": stamp,
                        "message": {"id": f"m-{agent}", "role": "assistant", "model": "claude-sonnet-5-5",
                                    "content": [{"type": "text", "text": "ok"}],
                                    "usage": {"input_tokens": 10, "output_tokens": 5}}}),
        ])
    return projects


def _live() -> list[dict]:
    return [{"sessionId": SID, "name": "orch #136", "kind": "interactive", "status": "idle",
             "cwd": "C:/repo"}]


def _drawn() -> list[dict]:
    """What an injected `agents_fn` returns: the rows the tile would draw (one per session, keyed by `sid`)."""
    return [{"sid": SID, "name": "orch #136", "status": "idle"}]


def _snap(tmp_path: Path, **overrides) -> tuple[Path, Path]:
    kwargs = dict(
        now=NOW,
        live_sessions_raw=lambda: _live(),
        agents_fn=lambda app: _drawn(),
        projects_dir=_project(tmp_path),
        pane_cache=tmp_path / "no-cache",
        window_title="Claude",
        pane_rect_fn=lambda title: None,
    )
    kwargs.update(overrides)
    out_root = tmp_path / "out"
    path = ms.take_snapshot(out_root, **kwargs)
    return out_root, path


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_rows_are_what_agents_fn_returned_and_it_got_the_saved_app_list(tmp_path):
    seen: list[list[dict]] = []
    drawn = [{"sid": SID, "name": "orch #136", "status": "idle", "marker": "from agents_fn"}]

    def agents(app: list[dict]) -> list[dict]:
        seen.append(app)
        return drawn

    _, path = _snap(tmp_path, live_sessions_raw=lambda: _live(), agents_fn=agents)

    data = _load(path)
    assert data["rows"] == drawn
    assert seen == [data["app_agents"]]
    assert data["app_agents"] == _live()


def test_snapshot_holds_agent_times_notifications_and_the_cache_stamp(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "shared-fast.json").write_text(json.dumps({"at": "12:34:56"}), encoding="utf-8")

    _, path = _snap(tmp_path, pane_cache=cache)

    data = _load(path)
    # per-agent last record time, sorted by file name
    files = data["per_session"][SID]["subagent_files"]
    assert [f["agent_id"] for f in files] == ["ag1", "ag2", "ag3", "ag4"]
    assert datetime.fromisoformat(files[0]["last_record_at"]) == datetime.fromisoformat("2026-10-17T11:31:00+00:00")
    assert datetime.fromisoformat(files[3]["last_record_at"]) == datetime.fromisoformat("2026-10-17T11:55:00+00:00")
    # the stopped notification: three ids, one status, the same time for all three
    notes = data["per_session"][SID]["notifications"]
    assert sorted(n["task_id"] for n in notes) == ["ag1", "ag2", "ag3"]
    assert {n["status"] for n in notes} == {"stopped"}
    assert all(n["timestamp"] == "2026-10-17T11:50:00+00:00" for n in notes)
    assert data["per_session"][SID]["ended_task_ids"] == ["ag1", "ag2", "ag3"]
    # the collector's cache stamp: the `at` and the file's own mtime (ISO, present)
    assert data["pane_refresh_time"] == "12:34:56"
    assert data["pane_refresh_source"] == "shared-fast.json"
    assert isinstance(data["pane_refresh_mtime"], str)
    datetime.fromisoformat(data["pane_refresh_mtime"])


def test_pane_refresh_mtime_is_null_when_the_cache_file_is_absent(tmp_path):
    _, path = _snap(tmp_path, pane_cache=tmp_path / "no-cache")

    data = _load(path)
    assert data["pane_refresh_time"] is None
    assert data["pane_refresh_source"] == "none"
    assert data["pane_refresh_mtime"] is None


def test_hidden_pane_writes_the_json_and_no_picture(tmp_path):
    grabbed: list[tuple[Path, tuple]] = []
    titles: list[str] = []

    def find(title: str):
        titles.append(title)
        return None

    _, path = _snap(tmp_path, pane_rect_fn=find,
                    grab_fn=lambda target, rect: grabbed.append((target, rect)))

    assert path.is_file()
    assert titles == ["Claude"]
    assert grabbed == []
    assert not (path.parent / "pane.png").exists()
    assert _load(path)["picture"] == "hidden"


def test_visible_pane_grabs_its_rectangle_into_pane_png(tmp_path):
    grabbed: list[tuple[Path, tuple]] = []

    def grab(target: Path, rect: tuple) -> None:
        grabbed.append((target, rect))
        target.write_bytes(b"\x89PNG fake")

    _, path = _snap(tmp_path, pane_rect_fn=lambda title: RECT, grab_fn=grab)

    assert (path.parent / "pane.png").is_file()
    assert grabbed == [(path.parent / "pane.png", RECT)]
    data = _load(path)
    assert data["picture"] == "pane.png"
    assert "picture_reason" not in data


def test_no_pil_means_hidden_with_a_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "pil_available", lambda: False)
    _, path = _snap(tmp_path, pane_rect_fn=lambda title: RECT)

    data = _load(path)
    assert data["picture"] == "hidden"
    assert data["picture_reason"] == "no PIL"
    assert not (path.parent / "pane.png").exists()


def test_find_pane_takes_the_first_visible_restored_window_with_the_title():
    windows = [("Other app", True, False, (0, 0, 10, 10)),
               ("Claude - monitor", False, False, (1, 1, 11, 11)),
               ("Claude - minimised", True, True, (2, 2, 12, 12)),
               ("Claude - monitor", True, False, RECT)]
    assert ms.find_pane("Claude", windows=windows) == RECT
    assert ms.find_pane("Claude", windows=windows[:3]) is None
    assert ms.find_pane("Missing", windows=windows) is None


def test_prune_removes_a_25_hour_folder_and_keeps_a_23_hour_one(tmp_path):
    old = tmp_path / _folder(NOW - timedelta(hours=25))
    young = tmp_path / _folder(NOW - timedelta(hours=23))
    for folder in (old, young):
        folder.mkdir()
        (folder / "snapshot.json").write_text("{}", encoding="utf-8")

    ms.prune(tmp_path, now=NOW, keep_hours=24)

    assert not old.exists()
    assert young.is_dir()


def test_run_loop_takes_three_snapshots_and_survives_one_failure(tmp_path):
    stop = threading.Event()
    calls: list[int] = []
    sleeps: list[float] = []
    logs: list[str] = []

    def fake_take(out_root, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("one snapshot fails")
        return out_root / "x"

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            stop.set()

    ms.run_loop(tmp_path, every_s=120, keep_hours=24, stop_event=stop, sleep=fake_sleep,
                take=fake_take, log=logs.append)

    assert len(calls) == 3
    assert sleeps == [120, 120, 120]
    assert len(logs) == 3  # one log line per snapshot, the failed one included


def test_settings_read_the_three_monitor_keys():
    assert ms.snapshot_settings() == {"every_s": 120, "keep_h": 24, "window_title": "Claude"}


def test_default_output_root_is_under_var():
    assert ms.DEFAULT_OUT_ROOT == ms.ma.ROOT / "var" / "agentops" / "monitor_snapshots"
