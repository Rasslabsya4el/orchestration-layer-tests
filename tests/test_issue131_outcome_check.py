"""Issue #131 unit 3: `outcome --check agree|disagree` is stored in the outcome row, and `routes`
shows each route's disagree share (`disagree_pct`, a column of the table).

Fixtures only, tmp dirs for every write -- never the real log. The routes tests pass their rows
in directly (`outcomes=`), so no transcript is read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.agentops import outcomes as oc

PROJECTS_DIR = Path(__file__).resolve().parent / "fixtures" / "agentops_outcomes" / "projects"


# --------------------------------------------------------------------------- record_outcome ----


@pytest.mark.parametrize("value", ["agree", "disagree"])
def test_record_outcome_stores_check_in_the_row(value: str, tmp_path: Path):
    log = tmp_path / "agentops_outcomes.jsonl"
    record = oc.record_outcome(issue=131, route="researcher-find/haiku", cls="accepted", why="ok",
                               check=value, log_path=log)
    assert record["check"] == value
    stored = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert stored["check"] == value


def test_record_outcome_row_without_check_has_no_check_key(tmp_path: Path):
    log = tmp_path / "agentops_outcomes.jsonl"
    record = oc.record_outcome(issue=131, route="researcher-find/haiku", cls="accepted", why="ok",
                               log_path=log)
    assert "check" not in record
    assert "check" not in json.loads(log.read_text(encoding="utf-8").splitlines()[0])


def test_record_outcome_rejects_bad_check_and_writes_nothing(tmp_path: Path):
    log = tmp_path / "agentops_outcomes.jsonl"
    with pytest.raises(oc.UnknownOutcomeCheck):
        oc.record_outcome(issue=131, route="researcher-find/haiku", cls="accepted", why="ok",
                          check="maybe", log_path=log)
    assert not log.exists()


# ------------------------------------------------------------------------------------ routes ----


def _row(route: str, *, check: str | None, ts: int = 1) -> dict:
    row = {"ts": ts, "issue": 131, "route": route, "class": "accepted", "why": "ok"}
    if check is not None:
        row["check"] = check
    return row


def test_routes_disagree_pct_is_share_of_checked_rows():
    """4 checked rows on one route, 1 of them disagree -> 25.0; an unchecked row is not counted."""
    lines = [
        _row("worker/sonnet-5", check="agree", ts=1),
        _row("worker/sonnet-5", check="agree", ts=2),
        _row("worker/sonnet-5", check="agree", ts=3),
        _row("worker/sonnet-5", check="disagree", ts=4),
        _row("worker/sonnet-5", check=None, ts=5),
    ]
    rows = oc.routes(outcomes=lines, metering_records=[], projects_dir=PROJECTS_DIR, prices={})
    by_route = {r["route"]: r for r in rows}
    assert by_route["worker/sonnet-5"]["disagree_pct"] == 25.0


def test_routes_disagree_pct_is_none_without_checked_rows():
    lines = [_row("researcher-how/sonnet-5", check=None)]
    rows = oc.routes(outcomes=lines, metering_records=[], projects_dir=PROJECTS_DIR, prices={})
    assert rows[0]["disagree_pct"] is None


# ------------------------------------------------------------------- format_routes_table ----


# -------------------------------------------------------------------------------------- CLI ----


def test_cli_outcome_check_is_written_into_the_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from tools.agentops import cli

    log = tmp_path / "agentops_outcomes.jsonl"
    monkeypatch.setattr(cli, "record_outcome", lambda **kw: oc.record_outcome(log_path=log, **kw))
    rc = cli.main(["outcome", "--issue", "131", "--route", "researcher-find/haiku", "--class", "accepted",
                   "--why", "second check agreed", "--check", "disagree"])
    assert rc == 0
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[0])["check"] == "disagree"


def test_cli_outcome_without_check_writes_no_check_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from tools.agentops import cli

    log = tmp_path / "agentops_outcomes.jsonl"
    monkeypatch.setattr(cli, "record_outcome", lambda **kw: oc.record_outcome(log_path=log, **kw))
    rc = cli.main(["outcome", "--issue", "131", "--route", "researcher-find/haiku", "--class", "accepted",
                   "--why", "ok"])
    assert rc == 0
    assert "check" not in json.loads(log.read_text(encoding="utf-8").splitlines()[0])


def test_cli_outcome_rejects_bad_check_and_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                          capsys):
    from tools.agentops import cli

    log = tmp_path / "agentops_outcomes.jsonl"
    monkeypatch.setattr(cli, "record_outcome", lambda **kw: oc.record_outcome(log_path=log, **kw))
    with pytest.raises(SystemExit) as exc:  # argparse choices=OUTCOME_CHECKS refuses it before any write
        cli.main(["outcome", "--issue", "131", "--route", "researcher-find/haiku", "--class", "accepted",
                  "--why", "ok", "--check", "maybe"])
    assert exc.value.code != 0
    assert not log.exists()


def test_cli_routes_prints_disagree_column(monkeypatch: pytest.MonkeyPatch, capsys):
    from tools.agentops import cli

    fixed_rows = [_table_row("researcher-find/haiku", 25.0), _table_row("researcher-how/sonnet-5", None)]
    monkeypatch.setattr(cli, "compute_routes", lambda **kw: fixed_rows)
    rc = cli.main(["routes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "disagree %" in out
    assert "25%" in out


def _table_row(route: str, disagree_pct: float | None) -> dict:
    return {"route": route, "delegations": 4, "accepted_pct": 100.0, "redone_pct": 0.0,
            "wrong_fact_pct": 0.0, "disagree_pct": disagree_pct,
            "median_cost_usd": None, "median_tokens": None}


def test_format_routes_table_prints_disagree_column_after_wrong_fact():
    table = oc.format_routes_table([_table_row("researcher-find/haiku", 25.0),
                                    _table_row("researcher-how/sonnet-5", None)])
    header, _rule, first, second = table.splitlines()
    assert "disagree %" in header
    assert header.index("wrong-fact %") < header.index("disagree %") < header.index("median $")
    # fields: route, delegations, accepted, redone, wrong-fact, disagree, median $, median tokens
    assert first.split()[5] == "25%"
    assert second.split()[5] == "-"
