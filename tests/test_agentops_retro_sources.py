"""Issue 109 unit 1: tools/agentops/retro_sources.py over a tmp fixture folder with planted records. No network.

Seams: `role_rows(root, since, until)` and `file_failures(root, since, until)`; the fixture is the repo's folder
layout (config, docs, var/agentops, artifacts/runs) plus a fake Claude projects folder for the subagent transcripts.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from tools.agentops import retro_sources

SINCE = datetime(2026, 10, 16, 0, 0)
UNTIL = datetime(2026, 10, 17, 15, 0)

CORE_BLOCK = """## Roles (orchestration v2)

The owner talks only to the brain.

| role | where | model / effort | job | protocol |
|---|---|---|---|---|
| brain | the brain chat | Fable 5.1 high | talks to the owner | `docs/agents/brain.md` |
| ORCH | background sessions | Sonnet 5.5 high | takes ready issues | `docs/agents/orch.md` |
| dispatcher | `python -m tools.agentops dispatch` | none | starts ORCH | `docs/agents/orch.md` |
| researcher-find / researcher-how | subagent | Haiku / Sonnet 5.5 | find / how | `.claude/agents/` |
| external researcher | GPT via opencode | Luna 5.6 | web research | `docs/agents/research.md` |
| worker / worker-strong | subagent in its own worktree | Haiku / Sonnet 5.5 | code + tests | `.claude/agents/` |
| reviewer-standards | subagent, read-only | Haiku 5.5 high | Standards axis | `.claude/agents/` |
| reviewer-spec | subagent, read-only | Sonnet 5.5 high | Spec axis | `.claude/agents/` |

The brain row's model binds the owner's desktop chat.
"""

ROLE_NAMES = [
    "brain", "ORCH", "dispatcher", "researcher-find / researcher-how", "external researcher",
    "worker / worker-strong", "reviewer-standards", "reviewer-spec",
]


def _local(naive: datetime) -> str:
    """A naive local moment written as a zoned ISO stamp (the way the sampler and the transcripts write theirs)."""
    return naive.astimezone().isoformat(timespec="seconds")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _merge_result(folder: Path, name: str, **fields) -> None:
    record = {"kind": "merge", "argv": [], "rc": fields.pop("rc", 1), "pushed_sha": None, "suite_output": None,
              "issues": fields.pop("issues", []), "output": "", **fields}
    _write(folder / "var" / "agentops" / "results" / name, json.dumps(record, indent=2))


def _fixture(root: Path, projects: Path, *, targets: bool = True) -> None:
    """The planted records: 2 target-check results (plus one out of the window), one red-suite result, 3 core
    target failures of 2 causes, one gh-budget file with one zero row in the window, a dispatch log with one
    rate-limit line in the window, one ORCH start, subagent transcripts, and no STATE.md and no docs/research."""
    settings = {"repo": "Owner/home", "claude_projects_dir": "proj"}
    if targets:
        settings["targets"] = [{"repo": "Owner/repoA", "folder": "x"}, {"repo": "Owner/repoB", "folder": "y"}]
    _write(root / "config" / "orchestration.json", json.dumps(settings))
    _write(root / "docs" / "agents" / "core-block.md", CORE_BLOCK)

    _merge_result(root, "20261016T100500-merge-a1.json", reason="target-check", started="2026-10-16T10:00:00",
                  ended="2026-10-16T10:05:00", in_line_s=600.0, suite_s=300.0, failed=0, issues=[1])
    _merge_result(root, "20261016T110500-merge-a2.json", reason="target-check", started="2026-10-16T11:00:00",
                  ended="2026-10-16T11:05:00", in_line_s=120.0, suite_s=60.0, failed=0, issues=[2])
    _merge_result(root, "20261016T120500-merge-b1.json", reason="red-suite", started="2026-10-16T12:00:00",
                  ended="2026-10-16T12:05:00", in_line_s=0.0, suite_s=100.0, failed=3, issues=[3])
    _merge_result(root, "20261015T100000-merge-old.json", reason="target-check", started="2026-10-15T09:55:00",
                  ended="2026-10-15T10:00:00", in_line_s=900.0, suite_s=900.0, failed=0, issues=[4])
    _merge_result(root, "20261016T130500-merge-c1.json", rc=0, reason="pushed", started="2026-10-16T13:00:00",
                  ended="2026-10-16T13:05:00", issues=[5], core_targets=[
                      "Owner/repoA: failed: gh api 'commits/main' failed: gh: API rate limit exceeded, waiting until 02:57:31",
                      "Owner/repoB: taken 1234abcd"])
    _merge_result(root, "20261016T140500-merge-c2.json", rc=0, reason="pushed", started="2026-10-16T14:00:00",
                  ended="2026-10-16T14:05:00", issues=[6], core_targets=[
                      "Owner/repoB: failed: core update refused: uncommitted change(s) at config/orchestration.json",
                      "Owner/repoA: failed: gh: API rate limit exceeded for user ID 1"])

    sampler = [
        {"t": _local(datetime(2026, 10, 16, 15, 5, 30)), "used": 5000, "remaining": 0, "limit": 5000,
         "reset_at": "2026-10-16T14:29:17Z"},
        {"t": _local(datetime(2026, 10, 16, 15, 15, 30)), "used": 1000, "remaining": 4000, "limit": 5000,
         "reset_at": "2026-10-16T14:29:17Z"},
        {"t": _local(datetime(2026, 10, 15, 9, 0, 0)), "used": 5000, "remaining": 0, "limit": 5000,
         "reset_at": "2026-10-15T08:00:00Z"},
    ]
    _write(root / "artifacts" / "runs" / "gh-budget-1.jsonl", "\n".join(json.dumps(r) for r in sampler) + "\n")
    _write(root / "artifacts" / "runs" / "dispatch.out", (
        "2026-10-15 09:00:00 orch-dispatch: start (rate limit exceeded before the window)\n"
        "2026-10-16 15:06:00 orch-dispatch: brain notice: #93 autopilot lookup failed "
        "(RuntimeError: gh api issues/93/parent failed: gh: API rate limit exceeded for user ID 1)\n"
        "continued line without a stamp\n"))
    _write(root / "var" / "agentops" / "merge.log",
           "2026-10-16 10:05:00 | a1 | detached result rc 1 | var/agentops/results/x.json\n")
    events = [
        {"ts": _local(datetime(2026, 10, 16, 9, 37, 0)), "kind": "orch_start", "issues": [12], "session": "c1"},
        {"ts": _local(datetime(2026, 10, 16, 9, 37, 5)), "kind": "issue_taken", "issue": 12},
        {"ts": _local(datetime(2026, 10, 15, 9, 0, 0)), "kind": "orch_start", "issues": [11], "session": "c0"},
    ]
    _write(root / "artifacts" / "runs" / "events.jsonl", "\n".join(json.dumps(e) for e in events) + "\n")

    session = projects / "proj" / "sess1" / "subagents"
    _write(session / "agent-w1.meta.json", json.dumps({"agentType": "worker"}))
    _write(session / "agent-w1.jsonl", json.dumps({"type": "user", "timestamp": _local(datetime(2026, 10, 16, 12, 0))})
           + "\n")
    _write(session / "agent-r1.meta.json", json.dumps({"agentType": "researcher-how"}))
    _write(session / "agent-r1.jsonl", json.dumps({"type": "user", "timestamp": _local(datetime(2026, 10, 16, 8, 0))})
           + "\n")
    _write(session / "agent-s1.meta.json", json.dumps({"agentType": "reviewer-spec"}))
    _write(session / "agent-s1.jsonl", json.dumps({"type": "user", "timestamp": _local(datetime(2026, 10, 10, 8, 0))})
           + "\n")


def _rows_by_role(rows: list[dict]) -> dict[str, dict]:
    return {row["role"]: row for row in rows}


def _failures_by(rows: list[dict], kind: str, note_part: str = "") -> list[dict]:
    return [row for row in rows if row["kind"] == kind and note_part in row["note"]]


def test_role_rows_counts_each_role_and_marks_a_missing_location(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    projects = tmp_path / "projects"
    monkeypatch.setattr(retro_sources, "CLAUDE_PROJECTS_BASE", projects)
    _fixture(root, projects)

    rows = retro_sources.role_rows(root, SINCE, UNTIL)
    by_role = _rows_by_role(rows)

    assert [row["role"] for row in rows] == ROLE_NAMES + ["merge queue", "core push"]
    assert by_role["brain"] == {"role": "brain", "location": "no records", "exists": False, "entries": 0}
    assert by_role["external researcher"] == {"role": "external researcher", "location": "no records",
                                              "exists": False, "entries": 0}
    assert by_role["reviewer-standards"]["exists"] is False and by_role["reviewer-standards"]["entries"] == 0
    assert by_role["ORCH"]["exists"] is True and by_role["ORCH"]["entries"] == 1
    assert by_role["dispatcher"]["exists"] is True and by_role["dispatcher"]["entries"] == 1
    assert by_role["researcher-find / researcher-how"]["exists"] is True
    assert by_role["researcher-find / researcher-how"]["entries"] == 1
    assert by_role["worker / worker-strong"]["entries"] == 1
    assert by_role["reviewer-spec"]["exists"] is True and by_role["reviewer-spec"]["entries"] == 0
    assert by_role["merge queue"]["exists"] is True and by_role["merge queue"]["entries"] == 5
    assert by_role["core push"]["exists"] is True and by_role["core push"]["entries"] == 4


def test_role_rows_has_no_core_push_row_without_targets(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    projects = tmp_path / "projects"
    monkeypatch.setattr(retro_sources, "CLAUDE_PROJECTS_BASE", projects)
    _fixture(root, projects, targets=False)

    roles = [row["role"] for row in retro_sources.role_rows(root, SINCE, UNTIL)]

    assert "core push" not in roles
    assert roles[-1] == "merge queue"


def test_file_failures_counts_each_kind_exactly(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    projects = tmp_path / "projects"
    monkeypatch.setattr(retro_sources, "CLAUDE_PROJECTS_BASE", projects)
    _fixture(root, projects)

    rows = retro_sources.file_failures(root, SINCE, UNTIL)

    assert {row["kind"] for row in rows} <= {"a", "b", "c", "h"}
    for row in rows:
        assert {"kind", "count", "lost_s", "pointers", "note"} <= set(row)
        assert len(row["pointers"]) <= 3
        if row["kind"] == "a":
            assert {"reason", "in_line_s", "suite_s"} <= set(row)
        else:
            assert set(row) == {"kind", "count", "lost_s", "pointers", "note"}

    target_check = _failures_by(rows, "a", "target-check")
    assert len(target_check) == 1
    assert target_check[0]["reason"] == "target-check"
    assert target_check[0]["count"] == 2
    assert target_check[0]["in_line_s"] == 600 + 120
    assert target_check[0]["suite_s"] == 300 + 60
    assert target_check[0]["lost_s"] == 600 + 300 + 120 + 60
    assert target_check[0]["pointers"] == [
        "var/agentops/results/20261016T100500-merge-a1.json",
        "var/agentops/results/20261016T110500-merge-a2.json",
    ]
    assert "time in line 12 min" in target_check[0]["note"] and "time in suite 6 min" in target_check[0]["note"]

    red = _failures_by(rows, "b")
    assert len(red) == 1
    assert red[0]["count"] == 1
    assert red[0]["lost_s"] == 100
    assert "red-suite" in red[0]["note"] and "red-on-branch" in red[0]["note"]
    assert "rework" in red[0]["note"]

    rate = _failures_by(rows, "c", "rate limit")
    assert len(rate) == 1 and rate[0]["count"] == 2 and rate[0]["lost_s"] is None
    assert rate[0]["pointers"][0] == "var/agentops/results/20261016T130500-merge-c1.json"
    uncommitted = _failures_by(rows, "c", "uncommitted")
    assert len(uncommitted) == 1 and uncommitted[0]["count"] == 1
    assert len(rows) == 6  # a, b, c rate limit, c uncommitted, h sampler, h logs

    sampler = _failures_by(rows, "h", "gh-budget")
    assert len(sampler) == 1 and sampler[0]["count"] == 1 and sampler[0]["lost_s"] is None
    assert sampler[0]["pointers"] == ["artifacts/runs/gh-budget-1.jsonl:1"]
    logs = _failures_by(rows, "h", "rate limit exceeded")
    assert len(logs) == 1 and logs[0]["count"] == 1
    assert "issues/93/parent" in logs[0]["note"]
    assert logs[0]["pointers"] == ["artifacts/runs/dispatch.out:2"]


def test_file_failures_without_sources_says_no_source(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(retro_sources, "CLAUDE_PROJECTS_BASE", tmp_path / "projects")

    rows = retro_sources.file_failures(empty, SINCE, UNTIL)

    assert sorted(row["kind"] for row in rows) == ["a", "b", "c", "h", "h"]
    for row in rows:
        assert row["count"] == 0
        assert "no source" in row["note"]
        assert row["pointers"] == []


def test_the_commands_write_nothing(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    projects = tmp_path / "projects"
    monkeypatch.setattr(retro_sources, "CLAUDE_PROJECTS_BASE", projects)
    _fixture(root, projects)

    def snapshot() -> dict:
        return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in tmp_path.rglob("*") if p.is_file()}

    before = snapshot()
    retro_sources.role_rows(root, SINCE, UNTIL)
    retro_sources.file_failures(root, SINCE, UNTIL)

    assert snapshot() == before


def test_kind_a_has_one_row_per_reason(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    monkeypatch.setattr(retro_sources, "CLAUDE_PROJECTS_BASE", tmp_path / "projects")
    _merge_result(root, "20261016T100500-merge-t1.json", reason="target-check", started="2026-10-16T10:00:00",
                  ended="2026-10-16T10:05:00", in_line_s=1000.0, suite_s=200.0, failed=0, issues=[1])
    _merge_result(root, "20261016T110500-merge-k1.json", reason="conflict", started="2026-10-16T11:00:00",
                  ended="2026-10-16T11:02:00", in_line_s=30.0, suite_s=0.0, failed=0, issues=[2])

    rows = retro_sources.file_failures(root, SINCE, UNTIL)
    a_rows = [row for row in rows if row["kind"] == "a"]

    assert [row["reason"] for row in a_rows] == ["target-check", "conflict"]
    target, conflict = a_rows
    assert target["count"] == 1 and target["in_line_s"] == 1000 and target["suite_s"] == 200
    assert target["lost_s"] == 1200
    assert conflict["count"] == 1 and conflict["in_line_s"] == 30 and conflict["suite_s"] == 0
    assert conflict["lost_s"] == 30
    assert "plain count" in conflict["note"] and "same window" in conflict["note"]
    assert "plain count" not in target["note"]


def test_kind_b_counts_red_on_branch_with_red_suite(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    monkeypatch.setattr(retro_sources, "CLAUDE_PROJECTS_BASE", tmp_path / "projects")
    _merge_result(root, "20261016T120500-merge-b1.json", reason="red-suite", started="2026-10-16T12:00:00",
                  ended="2026-10-16T12:05:00", in_line_s=0.0, suite_s=100.0, failed=3, issues=[3])
    _merge_result(root, "20261016T130500-merge-b2.json", reason="red-on-branch", started="2026-10-16T13:00:00",
                  ended="2026-10-16T13:05:00", in_line_s=0.0, suite_s=50.0, failed=2, issues=[4])

    rows = retro_sources.file_failures(root, SINCE, UNTIL)
    b_rows = [row for row in rows if row["kind"] == "b"]

    assert len(b_rows) == 1
    assert b_rows[0]["count"] == 2
    assert b_rows[0]["lost_s"] == 150
    assert "red-suite" in b_rows[0]["note"] and "red-on-branch" in b_rows[0]["note"]


def test_core_push_causes_sum_to_failed_core_target_lines(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    monkeypatch.setattr(retro_sources, "CLAUDE_PROJECTS_BASE", tmp_path / "projects")
    core_lines = [
        "Owner/repoA: failed: gh: API rate limit exceeded, waiting until 02:57:31",
        "Owner/repoB: failed: core update refused: uncommitted change(s) at config/orchestration.json",
        "Owner/repoA: failed: core update refused: local module .skill_counter",
        "Owner/repoB: failed: git commit failed: nothing to commit",
        "Owner/repoA: failed: the merge lock is held by another run",
        "Owner/repoB: failed: a list of changed files: a.py, b.py",
        "Owner/repoA: taken 1234abcd",
    ]
    _merge_result(root, "20261016T130500-merge-c1.json", rc=0, reason="pushed", started="2026-10-16T13:00:00",
                  ended="2026-10-16T13:05:00", issues=[5], core_targets=core_lines)
    _merge_result(root, "20261015T100000-merge-c0.json", rc=0, reason="pushed", started="2026-10-15T09:55:00",
                  ended="2026-10-15T10:00:00", issues=[6],
                  core_targets=["Owner/repoA: failed: gh: API rate limit exceeded"])

    rows = retro_sources.file_failures(root, SINCE, UNTIL)
    c_rows = [row for row in rows if row["kind"] == "c"]
    failed_lines = sum(": failed: " in line for line in core_lines)

    assert failed_lines == 6
    assert sum(row["count"] for row in c_rows) == failed_lines
    by_cause = {row["note"].rsplit(": ", 1)[1]: row["count"] for row in c_rows}
    assert by_cause == {"rate limit": 1, "uncommitted": 1, "core update refused": 1,
                        "git commit failed": 1, "merge lock held": 1, "other": 1}


def test_duration_formatter_uses_seconds_minutes_then_hours():
    fmt = retro_sources._duration
    assert fmt(45) == "45 s"
    assert fmt(600) == "10 min"
    assert fmt(5400) == "90 min"
    assert fmt(5460) == "1 h 31 min"
    assert fmt(28380) == "7 h 53 min"
    assert fmt(11520) == "3 h 12 min"


def test_notes_never_print_a_bare_count_of_seconds(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    monkeypatch.setattr(retro_sources, "CLAUDE_PROJECTS_BASE", tmp_path / "projects")
    _merge_result(root, "20261016T100500-merge-t1.json", reason="target-check", started="2026-10-16T10:00:00",
                  ended="2026-10-16T10:05:00", in_line_s=28380.0, suite_s=11520.0, failed=0, issues=[1])

    rows = retro_sources.file_failures(root, SINCE, UNTIL)
    target = [row for row in rows if row.get("reason") == "target-check"][0]

    assert "7 h 53 min" in target["note"] and "3 h 12 min" in target["note"]
    assert not re.search(r"\b\d{3,} s\b", target["note"])
