"""Tests for #166: one canonical route name (`<agent type>/<model>`), whatever spelling
`--route` or an old log line used. Each test that needs agent definitions builds its own tmp
`agents/` fixture, so the result never depends on the repo's `.claude/agents/`; no Session, no heavy tool run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.agentops import outcomes as oc


# ----------------------------------------------------------------------- number 1: routes ----


def _write_log(path: Path, rows: list[tuple[str, int]]) -> None:
    """One outcome line per (route spelling, count) pair, class 'accepted', no session."""

    ts = 1
    lines = []
    for route, count in rows:
        for _ in range(count):
            lines.append(json.dumps({"ts": ts, "issue": 166, "route": route, "class": "accepted",
                                     "why": "x"}))
            ts += 1
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _table_agents(tmp_path: Path) -> Path:
    """A tmp `agents/` with the roles the fixture table names, each with the model its real
    definition pins (not the repo's own files), plus two effort-level stubs."""
    agents = tmp_path / "agents"
    agents.mkdir()
    pins = {"worker": "claude-haiku-5-5", "researcher-find": "claude-haiku-5-5",
            "researcher-how": "claude-sonnet-5-5", "worker-strong": "claude-sonnet-5-5",
            "orch": "claude-sonnet-5-5", "effort-high": "claude-opus-5-5",
            "effort-medium": "claude-opus-5-5"}
    for role, model in pins.items():
        (agents / f"{role}.md").write_text(f"---\nname: {role}\nmodel: {model}\n---\nx\n", encoding="utf-8")
    return agents


def test_routes_folds_the_issues_fixture_table_to_ten_rows_summing_88(tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(oc, "AGENTS_DIR", _table_agents(tmp_path))
    log = tmp_path / "agentops_outcomes.jsonl"
    _write_log(log, [
        ("worker/sonnet-5", 18), ("worker/sonnet", 10), ("worker/sonnet5", 1),
        ("researcher-how/sonnet-5", 10), ("researcher-how", 7), ("researcher-how/sonnet5", 1),
        ("researcher-find", 13), ("researcher-find/haiku-4.5", 2), ("researcher-find/haiku", 1),
        ("worker-strong/opus-5", 4), ("worker-strong/opus5", 2),
        ("opus5-high", 3), ("opus5-high/opus-5", 1), ("opus5-high/opus5", 1),
        ("effort-high/sonnet-5", 4),
        ("effort-medium/sonnet-5", 2), ("effort-medium:sonnet", 1),
        ("orch/opus-5.5", 3),
        ("claude-code-guide", 2),
        ("general-purpose", 2),
    ])
    rows = oc.routes(outcomes_path=log, metering_path=tmp_path / "no_metering.jsonl")
    assert len(rows) == 10
    counts = [r["delegations"] for r in rows]
    assert counts == [29, 18, 16, 6, 5, 4, 3, 3, 2, 2]
    assert sum(counts) == 88
    by_route = {r["route"]: r["delegations"] for r in rows}
    assert by_route["worker/sonnet-5"] == 29
    assert by_route["researcher-how/sonnet-5"] == 18
    assert by_route["researcher-find/haiku-4.5"] == 16
    assert by_route["worker-strong/opus-5"] == 6
    assert by_route["opus5-high/opus-5"] == 5
    assert by_route["effort-high/sonnet-5"] == 4
    assert by_route["effort-medium/sonnet-5"] == 3
    assert by_route["orch/opus-5.5"] == 3
    assert by_route["claude-code-guide"] == 2
    assert by_route["general-purpose"] == 2


# ----------------------------------------------------------------- number 2: outcome writes ----


def _sonnet_55_agents(tmp_path: Path, roles: tuple[str, ...]) -> Path:
    agents = tmp_path / "agents"
    agents.mkdir()
    for role in roles:
        (agents / f"{role}.md").write_text("---\nmodel: claude-sonnet-5-5\n---\nx\n", encoding="utf-8")
    return agents


def test_outcome_bare_worker_writes_sonnet_5_5(tmp_path: Path):
    agents = _sonnet_55_agents(tmp_path, ("worker",))
    log = tmp_path / "agentops_outcomes.jsonl"
    record = oc.record_outcome(issue=297, route="worker", cls="accepted", why="x", log_path=log,
                               agents_dir=agents)
    assert record["route"] == "worker/sonnet-5.5"
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[0])["route"] == "worker/sonnet-5.5"


def test_routes_reads_by_ts_around_the_sonnet_5_5_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(oc, "AGENTS_DIR", _sonnet_55_agents(tmp_path, ("worker", "researcher-how")))
    since = oc.SONNET_55_SINCE
    rows = [{"ts": since - 10 - i, "route": "researcher-how", "class": "accepted"} for i in range(7)]
    rows += [{"ts": since + i, "route": "researcher-how", "class": "accepted"} for i in range(3)]
    rows += [{"ts": since + 5, "route": "worker/sonnet-5", "class": "accepted"}]
    result = oc.routes(outcomes=rows, metering_records=[], prices={}, projects_dir=tmp_path)
    assert {r["route"]: r["delegations"] for r in result} == {
        "researcher-how/sonnet-5": 7, "researcher-how/sonnet-5.5": 3, "worker/sonnet-5.5": 1}


@pytest.mark.parametrize("spelling", ["worker/sonnet5", "worker/sonnet"])
def test_outcome_folds_worker_spellings_to_worker_sonnet_5(tmp_path: Path, spelling: str):
    log = tmp_path / "agentops_outcomes.jsonl"
    record = oc.record_outcome(issue=166, route=spelling, cls="accepted", why="x", log_path=log)
    assert record["route"] == "worker/sonnet-5"
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["route"] == "worker/sonnet-5"


def test_outcome_luna_terra_pass_unchanged(tmp_path: Path):
    log = tmp_path / "agentops_outcomes.jsonl"
    for model in ("luna", "terra"):
        record = oc.record_outcome(issue=46, route=model, cls="accepted", why="x", log_path=log)
        assert record["route"] == model


def test_canonicalize_route_luna_terra_unchanged():
    assert oc.canonicalize_route("luna") == "luna"
    assert oc.canonicalize_route("terra") == "terra"


# --------------------------------------------------------------- number 3: unknown route ----


def test_outcome_rejects_unknown_agent_type_route(tmp_path: Path):
    log = tmp_path / "agentops_outcomes.jsonl"
    with pytest.raises(oc.UnknownRoute) as excinfo:
        oc.record_outcome(issue=166, route="wroker/sonnet-5", cls="accepted", why="x", log_path=log)
    assert "worker" in str(excinfo.value)  # known names printed
    assert not log.exists()


def test_cli_outcome_unknown_route_nonzero_exit_no_lines_appended(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch,
                                                                  capsys):
    from tools.agentops import cli

    log = tmp_path / "agentops_outcomes.jsonl"
    monkeypatch.setattr(cli, "record_outcome", lambda **kw: oc.record_outcome(log_path=log, **kw))
    rc = cli.main(["outcome", "--issue", "166", "--route", "wroker/sonnet-5", "--class", "accepted",
                  "--why", "x"])
    assert rc != 0
    assert not log.exists()
    out = capsys.readouterr().out
    assert "worker" in out  # known names printed


# --------------------------------------------------------------- fold is lenient at read time ----


def test_fold_route_leaves_unrecognized_opencode_model_id_unchanged(tmp_path: Path):
    log = tmp_path / "agentops_outcomes.jsonl"
    _write_log(log, [("gpt-5.6-terra", 1)])
    rows = oc.routes(outcomes_path=log, metering_path=tmp_path / "no_metering.jsonl")
    assert rows[0]["route"] == "gpt-5.6-terra"


def test_bare_effort_route_folds_to_opus_5_5(tmp_path: Path):
    # #636: an effort-level definition pins Opus 5.5, so a bare route takes it (tmp fixture).
    agents = tmp_path / "agents"
    agents.mkdir()
    for level in ("low", "medium", "high", "xhigh", "max"):
        (agents / f"effort-{level}.md").write_text("---\nmodel: claude-opus-5-5\n---\nx\n", encoding="utf-8")
        assert oc.canonicalize_route(f"effort-{level}", agents_dir=agents) == f"effort-{level}/opus-5.5"


# ------------------------------------------------------- #635: researcher-find on Haiku 5.5 ----


def test_canonicalize_route_bare_researcher_find_is_haiku_5_5():
    assert oc.canonicalize_route("researcher-find") == "researcher-find/haiku-5.5"
    assert oc.canonicalize_route("researcher-find/claude-haiku-5-5") == "researcher-find/haiku-5.5"


def test_routes_reads_researcher_find_by_ts_around_the_haiku_5_5_pin(tmp_path: Path):
    since = oc.HAIKU_55_SINCE
    rows = [{"ts": since - 10 - i, "route": "researcher-find", "class": "accepted"} for i in range(4)]
    rows += [{"ts": since + i, "route": "researcher-find", "class": "accepted"} for i in range(2)]
    rows += [{"ts": since + 5, "route": "researcher-find/haiku-4.5", "class": "accepted"}]
    result = oc.routes(outcomes=rows, metering_records=[], prices={}, projects_dir=tmp_path)
    assert {r["route"]: r["delegations"] for r in result} == {
        "researcher-find/haiku-4.5": 5, "researcher-find/haiku-5.5": 2}


def test_routes_reads_bare_worker_by_ts_around_the_haiku_5_5_pin(tmp_path: Path):
    since = oc.WORKER_HAIKU_55_SINCE
    rows = [{"ts": since - 10 - i, "route": "worker", "class": "accepted"} for i in range(2)]
    rows += [{"ts": since + i, "route": "worker", "class": "accepted"} for i in range(3)]
    result = oc.routes(outcomes=rows, metering_records=[], prices={}, projects_dir=tmp_path)
    assert {r["route"]: r["delegations"] for r in result} == {
        "worker/sonnet-5.5": 2, "worker/haiku-5.5": 3}
