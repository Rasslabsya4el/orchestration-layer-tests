"""Tests for tools/agentops/outcomes_transcripts.py (#646): transcripts built in tmp_path."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tools.agentops import outcomes_transcripts as ot

NOW_TS = 1790700000  # after SONNET_55_SINCE


def _agents(tmp_path: Path) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    (d / "worker.md").write_text("---\nname: worker\nmodel: claude-sonnet-5-5\n---\nx\n", encoding="utf-8")
    (d / "researcher-find.md").write_text("---\nname: researcher-find\nmodel: haiku\n---\nx\n", encoding="utf-8")
    (d / "effort-high.md").write_text("---\nname: effort-high\nmodel: inherit\n---\nx\n", encoding="utf-8")
    return d


def _sub(projects: Path, sid: str, agent_type: str, models: list[str | None]) -> None:
    d = projects / "sess" / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, m in enumerate(models):
        message = {"id": f"m{i}", "role": "assistant", "content": []}
        if m is not None:
            message["model"] = m
        lines.append(json.dumps({"type": "assistant", "message": message}))
    (d / f"agent-{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (d / f"agent-{sid}.meta.json").write_text(json.dumps({"agentType": agent_type}), encoding="utf-8")


def test_route_drift(tmp_path: Path):
    agents, projects = _agents(tmp_path), tmp_path / "projects"
    _sub(projects, "ok", "worker", ["claude-sonnet-5-5", "<synthetic>", None])
    _sub(projects, "bad", "worker", ["claude-sonnet-5-5", "claude-opus-5-5"])
    _sub(projects, "hk", "researcher-find", ["claude-haiku-4-5-20251001"])
    _sub(projects, "inh", "effort-high", ["claude-opus-5-5"])
    rows = [
        {"ts": NOW_TS, "route": "worker", "session": "ok"},
        {"ts": NOW_TS, "route": "worker", "session": "bad"},
        {"ts": NOW_TS, "route": "worker", "session": "missing"},
        {"ts": NOW_TS, "route": "worker", "session": None},
        {"ts": NOW_TS, "route": "researcher-find", "session": "hk"},
        {"ts": NOW_TS, "route": "effort-high", "session": "inh"},
    ]
    out = ot.route_drift(rows, projects_dir=projects, agents_dir=agents)
    assert out["worker/sonnet-5.5"] == {"spawns": 2, "drift": 1, "no_transcript": 2}
    assert out["researcher-find/haiku-4.5"] == {"spawns": 1, "drift": 0, "no_transcript": 0}
    assert out["effort-high"] == {"spawns": 1, "drift": None, "no_transcript": 0}


def _orch(projects: Path, sid: str, title: str, skill_days: list[int]) -> None:
    projects.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "custom-title", "customTitle": title})]
    for i, day in enumerate(skill_days):
        ts = f"2026-10-{day:02d}T10:00:00Z"
        msg = {"id": f"m{i}", "model": "claude-opus-5-5", "usage": {"input_tokens": 1, "output_tokens": 1},
               "content": [{"type": "tool_use", "id": f"t{i}", "name": "Skill", "input": {}}]}
        line = json.dumps({"type": "assistant", "timestamp": ts, "message": msg})
        lines.append(line)
        if i == 0:
            lines.append(line)  # streamed duplicate of the same message id + block id
    (projects / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_skill_calls_per_orch_session(tmp_path: Path):
    projects = tmp_path / "projects"
    # turn at 2026-10-05 so the session is in the window, no Skill call there is impossible by
    # construction (a turn is a Skill call), so give s0 a plain turn instead:
    projects.mkdir()
    plain = [json.dumps({"type": "custom-title", "customTitle": "orch 1"}),
             json.dumps({"type": "assistant", "timestamp": "2026-10-05T10:00:00Z",
                         "message": {"id": "p", "model": "claude-opus-5-5",
                                     "usage": {"input_tokens": 1, "output_tokens": 1}, "content": []}})]
    (projects / "s0.jsonl").write_text("\n".join(plain) + "\n", encoding="utf-8")
    _orch(projects, "s3", "orch 2", [2, 3, 4, 20])  # day 20 is outside the window
    _orch(projects, "other", "brain", [2, 3])
    since = datetime(2026, 10, 1, tzinfo=timezone.utc)
    until = datetime(2026, 10, 8, tzinfo=timezone.utc)
    out = ot.skill_calls_per_orch_session(projects_dir=projects, since=since, until=until)
    assert out == {"s0": 0, "s3": 3}


def test_median_max():
    assert ot.median_max([]) == (None, None)
    assert ot.median_max([0, 3]) == (1.5, 3)
    assert ot.median_max([4, 1, 2]) == (2, 4)


def _sub_ts(projects: Path, sid: str, agent_type: str, turns: list[tuple[str, str, int]]) -> None:
    d = projects / "sess" / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, (ts, model, tok) in enumerate(turns):
        msg = {"id": f"m{i}", "model": model, "usage": {"input_tokens": tok, "output_tokens": 0}, "content": []}
        lines.append(json.dumps({"type": "assistant", "timestamp": ts, "message": msg}))
    (d / f"agent-{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (d / f"agent-{sid}.meta.json").write_text(json.dumps({"agentType": agent_type}), encoding="utf-8")


def test_drift_by_agent_type(tmp_path: Path):
    agents, projects = _agents(tmp_path), tmp_path / "projects"
    other = tmp_path / "wtprojects"
    S, S2 = "claude-sonnet-5-5", "claude-opus-5-5"
    _sub_ts(projects, "a", "worker", [("2026-10-02T10:00:00Z", S, 5), ("2026-10-02T10:01:00Z", "<synthetic>", 0)])
    _sub_ts(projects, "b", "worker", [("2026-10-03T10:00:00Z", S, 5), ("2026-10-03T10:01:00Z", S2, 5)])
    _sub_ts(projects, "c", "worker", [("2026-10-20T10:00:00Z", S2, 5)])  # outside the window
    _sub_ts(projects, "d", "worker", [("2026-10-04T10:00:00Z", "<synthetic>", 0)])  # no real turn
    _sub_ts(projects, "e", "effort-high", [("2026-10-04T10:00:00Z", S2, 5)])
    _sub_ts(other, "f", "worker", [("2026-10-05T10:00:00Z", S, 5)])
    since = datetime(2026, 10, 1, tzinfo=timezone.utc)
    until = datetime(2026, 10, 8, tzinfo=timezone.utc)
    out = ot.drift_by_agent_type(projects_dir=[projects, other], since=since, until=until, agents_dir=agents)
    assert out["worker"] == {"spawns": 3, "drift": 1, "models": {"sonnet-5.5": 3, "opus-5.5": 1}}
    assert out["effort-high"] == {"spawns": 1, "drift": None, "models": {"opus-5.5": 1}}


def test_skill_session_needs_real_turn(tmp_path: Path):
    projects = tmp_path / "projects"
    projects.mkdir()
    dead = [json.dumps({"type": "custom-title", "customTitle": "orch 9"}),
            json.dumps({"type": "assistant", "timestamp": "2026-10-05T10:00:00Z",
                        "message": {"id": "x", "model": "<synthetic>",
                                    "usage": {"input_tokens": 0, "output_tokens": 0}, "content": []}})]
    (projects / "dead.jsonl").write_text("\n".join(dead) + "\n", encoding="utf-8")
    out = ot.skill_calls_per_orch_session(projects_dir=projects, since=datetime(2026, 10, 1, tzinfo=timezone.utc),
                                          until=datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert out == {}
