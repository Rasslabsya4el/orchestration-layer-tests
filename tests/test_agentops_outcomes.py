"""Tests for tools/agentops/outcomes.py (#66): one outcome line per delegation, and the routing
table recomputed from it. Fixtures only, tmp dirs for every write -- never the real log.

Fixture layout (tests/fixtures/agentops_outcomes/):
  projects/sessA/subagents/agent-w1.jsonl(+.meta.json)   a Claude subagent transcript, 1 turn:
    input 1000, cache_write 0, cache_read 0, output 500 tokens on claude-sonnet-5.
  prices.json    claude-sonnet-5 only: input $2, cache_write $2.5, cache_read $0.2, output $10 /
    million tokens -- so agent-w1's one turn costs 1000*2/1e6 + 500*10/1e6 = $0.007, 1500 tokens.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.agentops import outcomes as oc

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agentops_outcomes"
PROJECTS_DIR = FIXTURES / "projects"
PRICES_PATH = FIXTURES / "prices.json"


@pytest.fixture()
def prices() -> dict:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import token_meter as tm

    return tm.load_prices(PRICES_PATH)


# --------------------------------------------------------------------------- record_outcome ----


def test_record_outcome_appends_one_jsonl_line(tmp_path: Path):
    log = tmp_path / "agentops_outcomes.jsonl"
    record = oc.record_outcome(issue=66, route="worker/sonnet-5", cls="accepted",
                               why="numbers matched", session="w1", log_path=log)
    assert record["issue"] == 66
    assert record["route"] == "worker/sonnet-5"
    assert record["class"] == "accepted"
    assert record["why"] == "numbers matched"
    assert record["session"] == "w1"
    assert "ts" in record and isinstance(record["ts"], int)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == record


def test_record_outcome_rejects_unknown_class(tmp_path: Path):
    log = tmp_path / "agentops_outcomes.jsonl"
    with pytest.raises(oc.UnknownOutcomeClass):
        oc.record_outcome(issue=66, route="worker/sonnet-5", cls="not-a-class", why="x", log_path=log)
    assert not log.exists()


def test_record_outcome_no_session_is_optional(tmp_path: Path):
    log = tmp_path / "agentops_outcomes.jsonl"
    record = oc.record_outcome(issue=1, route="researcher-how/sonnet-5", cls="redone", why="sent back",
                               log_path=log)
    assert record["session"] is None


def test_record_outcome_no_unit_stays_exactly_as_today(tmp_path: Path):
    """Issue #254: a row with no `unit` given carries no `unit` field at all."""
    log = tmp_path / "agentops_outcomes.jsonl"
    record = oc.record_outcome(issue=66, route="worker/sonnet-5", cls="accepted", why="ok", log_path=log)
    assert "unit" not in record
    assert "unit" not in json.loads(log.read_text(encoding="utf-8").splitlines()[0])


def test_record_outcome_unit_is_written_when_given(tmp_path: Path):
    log = tmp_path / "agentops_outcomes.jsonl"
    record = oc.record_outcome(issue=254, route="worker/sonnet-5", cls="accepted", why="ok",
                               unit="2/3", log_path=log)
    assert record["unit"] == "2/3"
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[0])["unit"] == "2/3"


# -------------------------------------------------------------------------------- load_jsonl ----


def test_load_jsonl_missing_file_is_empty(tmp_path: Path):
    assert oc.load_jsonl(tmp_path / "nope.jsonl") == []


def test_load_jsonl_skips_broken_lines(tmp_path: Path):
    path = tmp_path / "log.jsonl"
    path.write_text('{"a": 1}\nnot json\n{"b": 2}\n', encoding="utf-8")
    assert oc.load_jsonl(path) == [{"a": 1}, {"b": 2}]


# ------------------------------------------------------------------------------------ routes ----


def _outcomes(*rows: dict) -> list[dict]:
    return list(rows)


def test_routes_computes_percentages_per_route(prices):
    lines = _outcomes(
        {"ts": 1, "issue": 66, "route": "worker/sonnet-5", "class": "accepted", "why": "ok"},
        {"ts": 2, "issue": 66, "route": "worker/sonnet-5", "class": "accepted", "why": "ok"},
        {"ts": 3, "issue": 66, "route": "worker/sonnet-5", "class": "redone", "why": "wrong scope"},
        {"ts": 4, "issue": 46, "route": "gpt-5.6-terra", "class": "wrong-fact", "why": "bad url"},
    )
    rows = oc.routes(outcomes=lines, metering_records=[], projects_dir=PROJECTS_DIR, prices=prices)
    by_route = {r["route"]: r for r in rows}

    sonnet = by_route["worker/sonnet-5"]
    assert sonnet["delegations"] == 3
    assert sonnet["accepted_pct"] == pytest.approx(200 / 3)
    assert sonnet["redone_pct"] == pytest.approx(100 / 3)
    assert sonnet["wrong_fact_pct"] == 0.0

    terra = by_route["gpt-5.6-terra"]
    assert terra["delegations"] == 1
    assert terra["wrong_fact_pct"] == 100.0


def test_routes_folds_rows_with_and_without_unit(prices):
    """Issue #254: `unit` is not a grouping key -- a row without it and one with it fold into the
    same route."""
    lines = _outcomes(
        {"ts": 1, "issue": 254, "route": "worker/sonnet-5", "class": "accepted", "why": "ok"},
        {"ts": 2, "issue": 254, "route": "worker/sonnet-5", "class": "accepted", "why": "ok",
         "unit": "2/3"},
    )
    rows = oc.routes(outcomes=lines, metering_records=[], projects_dir=PROJECTS_DIR, prices=prices)
    by_route = {r["route"]: r for r in rows}
    assert by_route["worker/sonnet-5"]["delegations"] == 2


def test_routes_joins_claude_subagent_transcript_by_session(prices):
    lines = _outcomes(
        {"ts": 1, "issue": 66, "route": "worker/sonnet-5", "class": "accepted", "why": "ok",
         "session": "w1"},
    )
    rows = oc.routes(outcomes=lines, metering_records=[], projects_dir=PROJECTS_DIR, prices=prices)
    row = rows[0]
    assert row["median_cost_usd"] == pytest.approx(1000 * 2.0 / 1e6 + 500 * 10.0 / 1e6)
    assert row["median_tokens"] == 1500


def test_routes_joins_metering_log_by_session_tokens_only_no_usd(prices):
    """The opencode bridge's own runs (metering.py's log) carry codex.sale credits, not USD: a
    route joined only there gets tokens but "-" for $ (the gap this issue allows noting)."""

    lines = _outcomes(
        {"ts": 1, "issue": 46, "route": "gpt-5.6-terra", "class": "accepted", "why": "good report",
         "session": "oc-session-1"},
    )
    metering = [
        {"ts": 1, "kind": "research", "model": "gpt-5.6-terra", "tokens_in": 1000, "tokens_out": 300,
         "session": "oc-session-1"},
    ]
    rows = oc.routes(outcomes=lines, metering_records=metering, projects_dir=PROJECTS_DIR, prices=prices)
    row = rows[0]
    assert row["median_tokens"] == 1300
    assert row["median_cost_usd"] is None


def test_routes_since_filters_older_lines(prices):
    lines = _outcomes(
        {"ts": 100, "issue": 1, "route": "worker/sonnet-5", "class": "accepted", "why": "old"},
        {"ts": 5_000_000_000, "issue": 2, "route": "worker/sonnet-5", "class": "redone", "why": "new"},
    )
    rows = oc.routes(outcomes=lines, metering_records=[], since=1_000_000_000,
                     projects_dir=PROJECTS_DIR, prices=prices)
    assert rows[0]["delegations"] == 1
    assert rows[0]["redone_pct"] == 100.0


def test_routes_no_session_at_all_gives_dash(prices):
    lines = _outcomes({"ts": 1, "issue": 1, "route": "researcher-find/haiku", "class": "abandoned",
                       "why": "gave up"})
    rows = oc.routes(outcomes=lines, metering_records=[], projects_dir=PROJECTS_DIR, prices=prices)
    assert rows[0]["median_cost_usd"] is None
    assert rows[0]["median_tokens"] is None


def test_parse_since_bare_date_is_local_midnight():
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=2))
    ts = oc.parse_since("2026-09-20", tz=tz)
    assert datetime.fromtimestamp(ts, tz=timezone.utc) == datetime(2026, 9, 19, 22, tzinfo=timezone.utc)


def test_parse_since_agrees_with_token_meter_and_cost_levers(monkeypatch):
    """#304: the three `--since` parsers read `2026-09-29T00:00` as the same instant."""
    import sys
    from datetime import datetime, timedelta, timezone

    scripts = str(Path(__file__).resolve().parent.parent / "scripts")
    monkeypatch.syspath_prepend(scripts)
    import token_meter as tm

    tz = timezone(timedelta(hours=2))
    naive = "2026-09-29T00:00"
    expected = datetime(2026, 9, 28, 22, tzinfo=timezone.utc)
    assert datetime.fromtimestamp(oc.parse_since(naive, tz=tz), tz=timezone.utc) == expected
    # cost_levers calls tm.parse_date_arg
    assert tm.parse_date_arg(naive, tz=tz) == expected
    utc_midnight = datetime(2026, 9, 29, tzinfo=timezone.utc)
    for explicit in ("2026-09-29T00:00+00:00", "2026-09-29T00:00Z"):
        assert datetime.fromtimestamp(oc.parse_since(explicit, tz=tz), tz=timezone.utc) == utc_midnight
        assert tm.parse_date_arg(explicit, tz=tz) == utc_midnight


# ------------------------------------------------------------------------ format_routes_table ----


def test_format_routes_table_renders_dashes_for_missing_join():
    rows = [{"route": "researcher-find/haiku", "delegations": 1, "accepted_pct": 0.0,
             "redone_pct": 0.0, "wrong_fact_pct": 0.0, "median_cost_usd": None, "median_tokens": None}]
    table = oc.format_routes_table(rows)
    assert "researcher-find/haiku" in table
    assert " - " in table or table.rstrip().endswith("-")


def test_format_routes_table_empty_is_a_message():
    assert oc.format_routes_table([]) == "no outcome lines"


# -------------------------------------------------------------------------------------- CLI ----


def test_cli_outcome_writes_and_rejects_unknown_class(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from tools.agentops import cli

    log = tmp_path / "agentops_outcomes.jsonl"
    monkeypatch.setattr(cli, "record_outcome", lambda **kw: oc.record_outcome(log_path=log, **kw))
    rc = cli.main(["outcome", "--issue", "66", "--route", "worker/sonnet-5", "--class", "accepted",
                  "--why", "numbers matched"])
    assert rc == 0
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[0])["route"] == "worker/sonnet-5"

    with pytest.raises(SystemExit):  # argparse itself rejects an unknown --class (choices=OUTCOME_CLASSES)
        cli.main(["outcome", "--issue", "66", "--route", "worker/sonnet-5", "--class", "bogus",
                 "--why", "x"])


def test_cli_outcome_unit_is_written_into_the_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Issue #254: `outcome --unit 2/3` writes `"unit": "2/3"` into the row."""
    from tools.agentops import cli

    log = tmp_path / "agentops_outcomes.jsonl"
    monkeypatch.setattr(cli, "record_outcome", lambda **kw: oc.record_outcome(log_path=log, **kw))
    rc = cli.main(["outcome", "--issue", "254", "--route", "worker/sonnet-5", "--class", "accepted",
                  "--why", "ok", "--unit", "2/3"])
    assert rc == 0
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[0])["unit"] == "2/3"


@pytest.mark.parametrize("bad_unit", ["4/3", "x", "0/3", "1/0", "2", "2/3/4", "-1/3"])
def test_cli_outcome_rejects_a_bad_unit(bad_unit: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from tools.agentops import cli

    log = tmp_path / "agentops_outcomes.jsonl"
    monkeypatch.setattr(cli, "record_outcome", lambda **kw: oc.record_outcome(log_path=log, **kw))
    with pytest.raises(SystemExit):  # argparse rejects an invalid --unit before record_outcome runs
        cli.main(["outcome", "--issue", "254", "--route", "worker/sonnet-5", "--class", "accepted",
                 "--why", "ok", "--unit", bad_unit])
    assert not log.exists()


def test_cli_routes_prints_table(monkeypatch: pytest.MonkeyPatch, capsys, prices):
    from tools.agentops import cli

    fixed_rows = [{"route": "worker/sonnet-5", "delegations": 2, "accepted_pct": 100.0,
                  "redone_pct": 0.0, "wrong_fact_pct": 0.0, "median_cost_usd": 0.01,
                  "median_tokens": 1500}]
    monkeypatch.setattr(cli, "compute_routes", lambda **kw: fixed_rows)
    rc = cli.main(["routes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "worker/sonnet-5" in out
    assert "$0.01" in out
