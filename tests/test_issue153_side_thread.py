"""#153: a finished side thread (a `/btw` thread) leaves the monitor like a closed chat.

A side thread's transcript copies its parent chat's records (they carry the parent's `sessionId`) before its own
records (the thread's own `sessionId`, the file's stem). The app keeps its process alive after the thread ends, so the
entry stays `interactive` with status `idle`. Rule: such a side thread with no own record newer than
`monitor.side_thread_idle_drop_s` (default ten minutes) gets no row; a non-side-thread chat is never dropped by it; a chat's
duration counts from the entry's `startedAt` (ms) when present.

Fixtures are shaped like the real btw transcript (167 parent-`sessionId` records, then own records). No PoB, no
network, no real `claude` call: `panel_agents` is called directly, and `collect.agents()` with its live list stubbed.

    python -m pytest -q -p no:faulthandler tests/test_issue153_side_thread.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.agentops import monitor_agents as ma  # noqa: E402

COLLECT = ROOT / "mods" / "pob-slots" / "bin" / "collect.py"

PARENT = "59c0ab7d-0000-4000-8000-000000000001"
THREAD = "b4c071c2-c07e-4e08-aadb-12cf36ef7956"
NORMAL = "5e0e0c11-0000-4000-8000-0000000000aa"
START_MS = 1792254684338  # the app's startedAt of the thread (17.10 18:31:24 local)
START = datetime.fromtimestamp(START_MS / 1000, tz=timezone.utc)
PARENT_FIRST = START - timedelta(minutes=3)  # the copied parent records begin before the thread
LAST_OWN = START + timedelta(hours=2)  # the thread's newest own record


def _iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def _write_transcript(projects: Path, stem: str, lines: list[dict]) -> None:
    projects.mkdir(parents=True, exist_ok=True)
    (projects / f"{stem}.jsonl").write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n", encoding="utf-8")


def _record(session_id: str, ts: datetime, n: int) -> dict:
    kind = "user" if n % 2 == 0 else "assistant"
    message = {"role": kind, "content": f"record {n}"}
    return {"type": kind, "sessionId": session_id, "timestamp": _iso(ts), "message": message}


def _btw_transcript(projects: Path, own_records: int = 3) -> None:
    """The real shape: 167 copied parent records, then the thread's own records (last one LAST_OWN)."""
    lines = [_record(PARENT, PARENT_FIRST + timedelta(seconds=i), i) for i in range(167)]
    own = [START + timedelta(seconds=60 * (i + 1)) for i in range(own_records)]
    own[-1] = LAST_OWN
    lines += [_record(THREAD, ts, 168 + i) for i, ts in enumerate(own)]
    _write_transcript(projects, THREAD, lines)


def _normal_transcript(projects: Path, last: datetime) -> None:
    """A normal chat: every record carries its own `sessionId`; its newest record is `last`."""
    first = START - timedelta(hours=1)
    lines = [_record(NORMAL, first + timedelta(minutes=i), i) for i in range(4)]
    lines.append(_record(NORMAL, last, 99))
    _write_transcript(projects, NORMAL, lines)


def _raw(session_id: str, *, status: str = "idle", started_at: int | None = START_MS,
         kind: str = "interactive") -> dict:
    raw = {"sessionId": session_id, "name": "btw: хей", "kind": kind, "status": status, "pid": 9856}
    if started_at is not None:
        raw["startedAt"] = started_at
    return raw


def _panel(raws: list[dict], projects: Path, now: datetime, **kwargs) -> list[dict]:
    panel = ma.panel_agents(raws, projects_dir=projects, prices={}, repo_cwd=projects, now=now, **kwargs)
    return panel["sessions"]


# ---- acceptance row 1: the btw transcript shape, last own record 6 h old, idle -> no row


def test_row1_idle_side_thread_with_six_hour_old_own_record_has_no_row(tmp_path):
    _btw_transcript(tmp_path)
    rows = _panel([_raw(THREAD)], tmp_path, now=LAST_OWN + timedelta(hours=6))
    assert rows == []


# ---- acceptance row 2: the same fixture, last own record 2 min old -> a row, duration from startedAt


def test_row2_side_thread_with_two_minute_old_own_record_keeps_its_row(tmp_path):
    _btw_transcript(tmp_path)
    rows = _panel([_raw(THREAD)], tmp_path, now=LAST_OWN + timedelta(minutes=2))
    assert len(rows) == 1
    assert rows[0]["session_id"] == THREAD


def test_row2_duration_is_counted_from_started_at(tmp_path):
    _btw_transcript(tmp_path)
    now = LAST_OWN + timedelta(minutes=2)
    row = _panel([_raw(THREAD)], tmp_path, now=now)[0]
    assert row["age_seconds"] == pytest.approx((now - START).total_seconds())


# ---- acceptance row 3: a normal chat, idle, last record 5 h old -> still a row


def test_row3_normal_idle_chat_five_hours_old_keeps_its_row(tmp_path):
    last = START + timedelta(minutes=10)
    _normal_transcript(tmp_path, last)
    raw = _raw(NORMAL, started_at=None)
    rows = _panel([raw], tmp_path, now=last + timedelta(hours=5))
    assert len(rows) == 1
    assert rows[0]["session_id"] == NORMAL


# ---- acceptance row 4: a side thread's duration = now minus startedAt, not minus the parent's first record


def test_row4_side_thread_duration_is_not_counted_from_the_parent_first_record(tmp_path):
    _btw_transcript(tmp_path)
    now = LAST_OWN + timedelta(minutes=2)
    row = _panel([_raw(THREAD)], tmp_path, now=now)[0]
    from_parent = (now - PARENT_FIRST).total_seconds()
    assert row["age_seconds"] != pytest.approx(from_parent)
    assert row["age_seconds"] == pytest.approx((now - START).total_seconds())


# ---- the rule's edges (Rule 1-4 of #153)


def test_busy_side_thread_is_not_dropped_however_old_its_own_record(tmp_path):
    _btw_transcript(tmp_path)
    rows = _panel([_raw(THREAD, status="busy")], tmp_path, now=LAST_OWN + timedelta(hours=6))
    assert len(rows) == 1


def test_a_new_own_record_brings_a_dropped_side_thread_back(tmp_path):
    _btw_transcript(tmp_path)
    now = LAST_OWN + timedelta(hours=6)
    assert _panel([_raw(THREAD)], tmp_path, now=now) == []
    fresh = now - timedelta(minutes=1)
    with (tmp_path / f"{THREAD}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_record(THREAD, fresh, 500)) + "\n")
    rows = _panel([_raw(THREAD)], tmp_path, now=now)
    assert [row["session_id"] for row in rows] == [THREAD]


def test_drop_threshold_is_a_parameter_with_default_600(tmp_path):
    _btw_transcript(tmp_path)
    now = LAST_OWN + timedelta(minutes=2)  # two minutes old
    assert len(_panel([_raw(THREAD)], tmp_path, now=now)) == 1  # default 600: kept
    assert _panel([_raw(THREAD)], tmp_path, now=now, side_thread_idle_drop_s=60) == []


def test_side_thread_without_any_own_record_is_not_dropped(tmp_path):
    lines = [_record(PARENT, PARENT_FIRST + timedelta(seconds=i), i) for i in range(5)]
    _write_transcript(tmp_path, THREAD, lines)
    rows = _panel([_raw(THREAD)], tmp_path, now=LAST_OWN + timedelta(hours=6))
    assert len(rows) == 1


# ---- the collector: collect.agents() drops the same thread, and reads the threshold from config


def _collect():
    spec = importlib.util.spec_from_file_location("collect_153", COLLECT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _use_collector(monkeypatch, tmp_path: Path, projects: Path, now: datetime, raws: list[dict],
                   monitor: dict | None = None):
    collect = _collect()
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True, exist_ok=True)
    body = {"repo": "owner/name"}
    if monitor is not None:
        body["monitor"] = monitor
    (root / "config" / "orchestration.json").write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setattr(collect, "REPO", root)
    monkeypatch.setattr(collect, "CACHE_DIR", root / ".cache")
    monkeypatch.setattr(collect, "NOW", now)
    monkeypatch.setattr(collect, "_agent_status", lambda: None)
    monkeypatch.setattr(collect, "shared_result", lambda mode, fresh_for, compute: json.dumps(compute()))
    monkeypatch.setattr(ma, "live_sessions_raw", lambda *a, **k: raws)
    monkeypatch.setattr(ma, "load_orch_state", lambda *a, **k: None)
    monkeypatch.setattr(ma, "DEFAULT_PROJECTS_DIR", projects)
    monkeypatch.setattr(ma, "ROOT", root)
    return collect


def test_collector_drops_the_idle_side_thread_with_the_default_threshold(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    _btw_transcript(projects)
    collect = _use_collector(monkeypatch, tmp_path, projects, LAST_OWN + timedelta(hours=6),
                             [dict(_raw(THREAD), cwd=None)])
    assert collect.agents() == []


def test_collector_keeps_the_side_thread_recently_active(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    _btw_transcript(projects)
    collect = _use_collector(monkeypatch, tmp_path, projects, LAST_OWN + timedelta(minutes=2),
                             [dict(_raw(THREAD), cwd=None)])
    assert [row["sid"] for row in collect.agents()] == [THREAD]


def test_collector_reads_side_thread_idle_drop_s_from_config(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    _btw_transcript(projects)
    collect = _use_collector(monkeypatch, tmp_path, projects, LAST_OWN + timedelta(minutes=2),
                             [dict(_raw(THREAD), cwd=None)], monitor={"side_thread_idle_drop_s": 60})
    assert collect.agents() == []


def test_only_an_interactive_side_thread_is_dropped(tmp_path):
    # Rule 1: a side thread is an `interactive` entry; a background entry is never dropped by this rule
    _btw_transcript(tmp_path)
    background = _raw(THREAD, kind="background")
    rows = _panel([background], tmp_path, now=LAST_OWN + timedelta(hours=6))
    assert len(rows) == 1
