from datetime import datetime, timezone

from tools.agentops import outcomes_report as rp

S = datetime(2026, 10, 1, tzinfo=timezone.utc)
U = datetime(2026, 10, 8, tzinfo=timezone.utc)
T = int(datetime(2026, 10, 3, tzinfo=timezone.utc).timestamp())
OLD = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())


def _r(issue, route, cls="accepted", ts=T, **kw):
    return {"ts": ts, "issue": issue, "route": route, "class": cls, "why": "x", **kw}


OUT = [
    _r(1, "worker/sonnet-5.5", unit="1/2"), _r(1, "worker/sonnet-5.5", unit="2/2"),
    _r(1, "worker-strong/sonnet-5.5"), _r(1, "researcher-find/haiku"),
    _r(2, "worker-strong/sonnet-5.5"), _r(2, "orch/sonnet-5.5", "redone", unit="1/1"),
    _r(3, "worker/sonnet-5.5"), _r(3, "orch/sonnet-5.5"),
    _r(4, "worker/sonnet-5.5"), _r(4, "worker-strong/sonnet-5.5", unit="1/1"),
    _r(5, "worker/sonnet-5.5", ts=OLD),
    _r(None, "orch/sonnet-5.5", "abandoned"),
]
LAB = {
    1: {"labels": ["area:builds"], "state": "CLOSED", "closedAt": "2026-10-04T00:00:00Z"},
    2: {"labels": ["area:runs", "area:builds"], "state": "CLOSED", "closedAt": "2026-10-05T00:00:00Z"},
    3: {"labels": [], "state": "OPEN", "closedAt": None},
    4: {"labels": ["area:builds"], "state": "CLOSED", "closedAt": "2026-10-09T00:00:00Z"},
    5: {"labels": ["area:runs"], "state": "CLOSED", "closedAt": "2026-09-02T00:00:00Z"},
}
METER = {1: {"orch_calls": 10, "cost_usd": 2.0}, 2: {"orch_calls": 30, "cost_usd": 4.0}}


def test_per_issue():
    rows = {r["issue"]: r for r in rp.per_issue(OUT, LAB, METER, S, U)}
    assert sorted(rows) == [1, 2, 3, 4]
    assert rows[1]["routes"].endswith("worker/sonnet-5.5 x2")
    assert "worker-strong/sonnet-5.5 x1" in rows[1]["routes"]
    assert "researcher-find" in rows[1]["routes"]
    assert rows[1]["area"] == "builds" and rows[1]["units"] == 2
    assert rows[1]["orch_calls"] == 10 and rows[1]["cost_usd"] == 2.0
    assert rows[2]["area"] == "runs+builds" and rows[2]["units"] == 1
    assert rows[3]["area"] == "-" and rows[3]["units"] is None
    assert rows[3]["orch_calls"] is None and rows[3]["cost_usd"] is None
    assert "$2.00" in rp.format_per_issue(list(rows.values()))


def test_pilot():
    # definitions changed in #646 unit 3: window totals (ORCH own real turns; $ of the six roles)
    # divided by the closed issues, not a mean of the closed issues' own meter rows
    totals = {"orch_calls": 40, "cost_usd": 6.0}
    p = rp.pilot(OUT, LAB, totals, {"a": 0, "b": 2, "c": 5}, S, U)
    assert p["closed"] == 2  # 1 and 2; 4 closed after the window, 5 before
    assert p["orch_calls_per_closed"] == 20 and p["cost_per_closed"] == 3.0
    assert p["own"] == {"accepted": 1, "redone": 1, "wrong-fact": 0, "abandoned": 1}
    assert p["skill_median"] == 2 and p["skill_max"] == 5
    assert "1/1/0/1" in rp.format_pilot(p)
    e = rp.pilot(OUT, LAB, None, {}, S, U)
    assert e["orch_calls_per_closed"] is None and e["skill_max"] is None
    assert "median -, max -" in rp.format_pilot(e)


def test_split_shares():
    s = rp.split_shares(OUT, LAB, S, U)
    assert (s["delegations"], s["worker_strong"], s["both"]) == (4, 3, 2)
    assert s["ws_outside_builds"] == 1  # issue 2 (runs+builds)
    assert s["open_in_window"] == 2  # 3 open, 4 closed after the window
    assert s["by_area"] == {"-": (1, 0, 0), "builds": (2, 2, 2), "runs+builds": (1, 1, 0)}
    report = rp.build_report(outcomes=OUT, labels=LAB, meter=METER, totals=None, skill_counts={}, since=S, until=U)
    assert "open in the window: 2" in report


# ------------------------------------------------------------------ end to end through the CLI ----

import json  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402


def _ts(day, hour=10):
    return f"2026-10-{day:02d}T{hour:02d}:00:00Z"


def _turn(i, ts, model, tokens, *, skill=False, uid=None):
    msg = {"id": f"m{uid or i}", "model": model, "content": [],
           "usage": {"input_tokens": tokens, "output_tokens": 0}}
    if skill:
        msg["content"] = [{"type": "tool_use", "id": f"t{i}", "name": "Skill", "input": {}}]
    return json.dumps({"type": "assistant", "timestamp": ts, "message": msg})


def _write(path: Path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture()
def cli_world(tmp_path, monkeypatch):
    import sys

    from tools.agentops import outcomes as oc

    tm = oc._token_meter()
    # prices: $10 per million input tokens for opus, $5 for sonnet; nothing else costs
    prices = tmp_path / "prices.json"
    zero = {"cache_write": 0, "cache_read": 0, "output": 0}
    prices.write_text(json.dumps({"claude-opus-5-5": {"input": 10, **zero},
                                  "claude-sonnet-5-5": {"input": 5, **zero}}), encoding="utf-8")
    agents = tmp_path / "agents"
    agents.mkdir()
    for name in ("worker", "worker-strong", "orch"):
        (agents / f"{name}.md").write_text(f"---\nname: {name}\nmodel: claude-sonnet-5-5\n---\nx\n",
                                           encoding="utf-8")
    projects = tmp_path / "projects"
    # ORCH session for #1: 3 real opus turns of 100k ($1 each), one Skill call, plus a synthetic and
    # a 0-token turn that are not calls; ORCH session for #2: 1 real turn
    _write(projects / "o1.jsonl", [
        json.dumps({"type": "custom-title", "customTitle": "orch #1"}),
        _turn(1, _ts(3), "claude-opus-5-5", 100_000, skill=True), _turn(2, _ts(3, 11), "claude-opus-5-5", 100_000),
        _turn(3, _ts(3, 12), "claude-opus-5-5", 100_000), _turn(4, _ts(3, 13), "<synthetic>", 0),
        _turn(5, _ts(3, 14), "claude-opus-5-5", 0)])
    _write(projects / "o2.jsonl", [
        json.dumps({"type": "custom-title", "customTitle": "orch #2"}),
        _turn(1, _ts(4), "claude-opus-5-5", 100_000)])
    # two worker spawns under o1: one on its role's model ($0.50 per turn, 2 turns), one drifted
    sub = projects / "o1" / "subagents"
    for sid, model, n in (("w1", "claude-sonnet-5-5", 2), ("w2", "claude-opus-5-5", 1)):
        user = json.dumps({"type": "user", "message": {"role": "user", "content": "work on #1"}})
        _write(sub / f"agent-{sid}.jsonl", [user] + [_turn(i, _ts(3, 15 + i), model, 100_000, uid=f"{sid}{i}")
                                                     for i in range(n)])
        (sub / f"agent-{sid}.meta.json").write_text(json.dumps({"agentType": "worker"}), encoding="utf-8")

    t = int(datetime(2026, 10, 3, 9, tzinfo=timezone.utc).timestamp())
    log = tmp_path / "log.jsonl"
    rows = [_r(1, "worker/sonnet-5.5", session="w1", unit="1/2"), _r(1, "worker/sonnet-5.5", session="w2", unit="2/2"),
            _r(1, "worker-strong/sonnet-5.5"), _r(2, "worker-strong/sonnet-5.5", "redone"),
            _r(2, "orch/sonnet-5.5", "redone", unit="1/1"), _r(3, "worker/sonnet-5.5"),
            _r(3, "orch/sonnet-5.5"), _r(4, "worker/sonnet-5.5"), _r(4, "worker-strong/sonnet-5.5"),
            _r(5, "worker/sonnet-5.5", ts=OLD), _r(None, "orch/sonnet-5.5", "abandoned"),
            _r(1, "orch/sonnet-5.5", "wrong-fact")]
    log.write_text("\n".join(json.dumps({**r, "ts": r["ts"] if r["ts"] == OLD else t}) for r in rows) + "\n",
                   encoding="utf-8")
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps([
        {"number": 1, "state": "CLOSED", "closedAt": "2026-10-04T00:00:00Z", "labels": [{"name": "area:builds"}]},
        {"number": 2, "state": "CLOSED", "closedAt": "2026-10-05T00:00:00Z",
         "labels": [{"name": "area:runs"}, {"name": "area:builds"}]},
        {"number": 3, "state": "OPEN", "closedAt": None, "labels": []},
        {"number": 4, "state": "CLOSED", "closedAt": "2026-10-09T00:00:00Z", "labels": [{"name": "area:builds"}]},
    ]), encoding="utf-8")

    monkeypatch.setattr(oc, "DEFAULT_OUTCOMES_LOG", log)
    monkeypatch.setattr(oc, "METERING_LOG", tmp_path / "no_metering.jsonl")
    monkeypatch.setattr(oc, "AGENTS_DIR", agents)
    monkeypatch.setattr(tm, "DEFAULT_PRICES_PATH", prices)
    dispatch_out = tmp_path / "dispatch.out"  # the sessions' held issues, as the dispatcher logs them
    dispatch_out.write_text("\n".join(["started orch (o1) offered #1", "started orch (o2) offered #2"]),
                            encoding="utf-8")
    monkeypatch.setattr(tm, "DEFAULT_DISPATCH_OUT", dispatch_out)
    monkeypatch.setattr(tm, "DEFAULT_ORCH_SESSION_JSON", tmp_path / "no.json")
    return projects, labels


def _report(cli, capsys, projects, labels):
    rc = cli.main(["outcomes", "report", "--since", "2026-10-01T00:00:00Z", "--until", "2026-10-08T00:00:00Z",
                   "--labels", str(labels), "--projects-dir", str(projects)])
    assert rc == 0
    return capsys.readouterr().out


def test_cli_outcomes_report_end_to_end(cli_world, capsys):
    from tools.agentops import cli

    out = _report(cli, capsys, *cli_world)
    routes, rest = out.split("\n\nPer issue\n", 1)
    per_issue, rest = rest.split("\n\nPilot\n", 1)
    pilot_t, split_t = rest.split("\n\nSplit shares\n", 1)
    # table 1: worker 4 delegations in the window (issues 1,1,3,4; the OLD one is outside)
    worker = next(l for l in routes.splitlines() if l.startswith("worker/sonnet-5.5"))
    assert worker.split()[1] == "4" and worker.split()[-1] == "1/2"  # 1 of 2 worker spawns on opus
    assert "drift/spawns" in routes
    orch = next(l for l in routes.splitlines() if l.startswith("orch/sonnet-5.5"))
    assert orch.split()[1] == "4" and orch.split()[-1] == "-"  # no orch subagent transcript
    # table 2: #1 has 3 ORCH calls and $3 (ORCH) + $1 (w1: 2 x $0.5) + $1 (w2) = $5.00; #2 1 call, $1.00
    l1 = next(l for l in per_issue.splitlines() if l.strip().startswith("#1 "))
    assert l1.split()[2:5] == ["2", "3", "$5.00"]
    l2 = next(l for l in per_issue.splitlines() if l.strip().startswith("#2 "))
    assert l2.split()[2:5] == ["1", "1", "$1.00"]
    # table 3: 2 closed (1, 2); ORCH 4 real turns -> 2 per closed; six roles $4 + $2 = $6 -> $3.0
    assert "closed issues: 2" in pilot_t
    assert "ORCH calls per closed issue: 2" in pilot_t
    assert "$ per closed issue: $3.0" in pilot_t
    assert "orch own units a/r/w/x: 1/1/1/1" in pilot_t
    assert "Skill per ORCH session: median 0.5, max 1" in pilot_t
    # table 4
    assert "issues with delegations: 4" in split_t
    assert "with worker-strong: 3" in split_t and "with worker and worker-strong: 2" in split_t
    assert "worker-strong outside area builds: 1" in split_t


def test_cli_routes_output_is_unchanged(monkeypatch, capsys):
    from tools.agentops import cli, outcomes as oc

    rows = [{"route": "worker/sonnet-5", "delegations": 2, "accepted_pct": 100.0, "redone_pct": 0.0,
             "wrong_fact_pct": 0.0, "median_cost_usd": 0.01, "median_tokens": 1500}]
    monkeypatch.setattr(cli, "compute_routes", lambda **kw: rows)
    assert cli.main(["routes"]) == 0
    assert capsys.readouterr().out == oc.format_routes_table(rows) + "\n"
    assert "drift" not in oc.format_routes_table(rows)


# ------------------------------------------------------------------ #1068: no --labels reads the issue list ----

import subprocess  # noqa: E402

ISSUE_LIST_ARGS = ["issue", "list", "--state", "all", "--limit", "5000", "--json", "number,labels,state,closedAt"]
# 1 and 2 closed inside the window 10-01..10-08, 4 closed after it
GH_ISSUES = [
    {"number": 1, "state": "CLOSED", "closedAt": "2026-10-04T00:00:00Z", "labels": [{"name": "area:builds"}]},
    {"number": 2, "state": "CLOSED", "closedAt": "2026-10-05T00:00:00Z", "labels": [{"name": "area:runs"}]},
    {"number": 4, "state": "CLOSED", "closedAt": "2026-10-09T00:00:00Z", "labels": [{"name": "area:builds"}]},
]


class _FakeGh:
    """Stands in for `run_gh` inside outcomes_report: records each call, returns one fixed reply."""

    def __init__(self, reply):
        self.reply = reply
        self.calls: list[list[str]] = []

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        return self.reply


def _pilot_part(out: str) -> str:
    return out.split("\n\nPilot\n", 1)[1].split("\n\nSplit shares\n", 1)[0]


def _report_without_labels(cli, capsys, projects):
    rc = cli.main(["outcomes", "report", "--since", "2026-10-01T00:00:00Z", "--until", "2026-10-08T00:00:00Z",
                   "--projects-dir", str(projects)])
    assert rc == 0
    return capsys.readouterr().out


def test_no_labels_reads_the_issue_list_through_gh(cli_world, capsys, monkeypatch):
    from tools.agentops import cli

    fake = _FakeGh(subprocess.CompletedProcess(["gh"], 0, json.dumps(GH_ISSUES), ""))
    monkeypatch.setattr(rp, "run_gh", fake)
    out = _report_without_labels(cli, capsys, cli_world[0])
    assert fake.calls == [ISSUE_LIST_ARGS]
    assert "closed issues: 2" in _pilot_part(out)  # 1 and 2; 4 closed after the window


@pytest.mark.parametrize("reply", [
    subprocess.CompletedProcess(["gh"], 1, "", "HTTP 401: Bad credentials"),
    subprocess.CompletedProcess(["gh"], 0, "not json", ""),
], ids=["gh-rc", "bad-json"])
def test_failed_issue_list_prints_dash_and_names_the_failure(cli_world, capsys, monkeypatch, reply):
    from tools.agentops import cli

    monkeypatch.setattr(rp, "run_gh", _FakeGh(reply))
    out = _report_without_labels(cli, capsys, cli_world[0])  # rc 0 asserted inside
    pilot_t = _pilot_part(out)
    assert "closed issues: -" in pilot_t
    assert "issue list: not read (" in pilot_t
    assert "closed issues: 0" not in out
    assert "ORCH calls per closed issue: -" in pilot_t and "$ per closed issue: -" in pilot_t


def test_labels_file_given_never_calls_gh(cli_world, capsys, monkeypatch):
    from tools.agentops import cli

    fake = _FakeGh(subprocess.CompletedProcess(["gh"], 1, "", "must not be called"))
    monkeypatch.setattr(rp, "run_gh", fake)
    out = _report(cli, capsys, *cli_world)
    assert fake.calls == []
    assert "closed issues: 2" in _pilot_part(out)
