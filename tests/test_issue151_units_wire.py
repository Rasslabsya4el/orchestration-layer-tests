"""#151 unit 3 (units-wire): the parts count reaches every ticket that has data, whoever holds it.

    python -m pytest -q -p no:faulthandler tests/test_issue151_units_wire.py

ORCH session rows carry `ticket_units` ({"<n>": {done,total}}) for every issue with `k/n` outcome rows, and for each
held ticket with no rows a cut-table total gives {0,n}. Pipeline rows carry `units` from the same sources.
"""
import json
from types import SimpleNamespace

from tools.agentops import monitor_agents as ma
from tools.agentops import monitor_pipeline as mp

REPO = "o/r"
TIMELINE = json.dumps([{"event": "labeled", "label": {"name": "ready-for-agent"}, "created_at": "2026-10-01T00:00:00Z"}])


def _row(ts: int, issue: int, unit: str, cls: str) -> dict:
    return {"ts": ts, "issue": issue, "unit": unit, "class": cls, "route": "worker", "why": "", "session": "s"}


def _log(tmp_path, rows: list[dict]):
    path = tmp_path / "outcomes.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def _gh():
    return lambda argv: SimpleNamespace(returncode=0, stdout=TIMELINE)


def _issue(number: int, labels: list[str]) -> dict:
    return {"title": f"ticket {number}", "open": True, "labels": labels, "blockers": []}


def _units(log) -> dict[str, dict]:
    return {str(n): u for n, u in ma.units_by_issue(ma._load_outcome_rows(log)).items()}


def _panel(tmp_path, log, held=(), cut_totals=None) -> dict:
    sessions = [{"sessionId": "orchsess", "name": "orch", "cwd": None, "kind": "background", "status": "working"}]
    orch_state = {"session": "orchsess", "held": list(held), "claimed_now": [], "claimed_at": {}}
    return ma.panel_agents(sessions, projects_dir=tmp_path, prices={}, repo_cwd=tmp_path,
                           orch_state=orch_state, outcomes_path=log, cut_totals=cut_totals)


def _by(rows: list[dict]) -> dict[int, dict]:
    return {r["number"]: r for r in rows}


def test_accepted_rows_reach_unheld_ticket_on_pipeline_and_orch_row(tmp_path):
    # rule 1: #136 is held by no session, yet its 3 of 3 accepted units show on its row and on the ORCH row
    log = _log(tmp_path, [_row(1, 136, "1/3", "accepted"), _row(2, 136, "2/3", "accepted"),
                          _row(3, 136, "3/3", "accepted")])
    panel = _panel(tmp_path, log, held=[])
    assert panel["sessions"][0]["ticket_units"] == {"136": {"done": 3, "total": 3}}
    facts = mp.pipeline_facts({136: _issue(136, ["ready-for-agent"])}, repo=REPO, gh=_gh(), cache={})
    by = _by(mp.pipeline_rows(facts, [], {"missing": True}, units=_units(log)))
    assert by[136]["units"] == {"done": 3, "total": 3}


def test_redone_rows_give_two_done_of_four(tmp_path):
    # #160: redone counts as done (was 0/4)
    log = _log(tmp_path, [_row(1, 148, "1/4", "redone"), _row(2, 148, "2/4", "redone")])
    panel = _panel(tmp_path, log, held=[])
    assert panel["sessions"][0]["ticket_units"] == {"148": {"done": 2, "total": 4}}
    facts = mp.pipeline_facts({148: _issue(148, ["ready-for-agent"])}, repo=REPO, gh=_gh(), cache={})
    by = _by(mp.pipeline_rows(facts, [], {"missing": True}, units=_units(log)))
    assert by[148]["units"] == {"done": 2, "total": 4}


def test_held_ticket_with_cut_total_and_no_rows_gets_zero_of_n(tmp_path):
    # rule 2: #149 is held, has no k/n rows, and its ORCH cut table says 4 units
    missing = tmp_path / "no-such-log.jsonl"
    panel = _panel(tmp_path, missing, held=[149], cut_totals={149: 4})
    assert panel["sessions"][0]["ticket_units"] == {"149": {"done": 0, "total": 4}}
    facts = mp.pipeline_facts({149: _issue(149, ["orch"])}, repo=REPO, gh=_gh(), cache={}, cut_totals={149: 4})
    sessions = [{"held": panel["sessions"][0]["held_issues"], "subagents": []}]
    by = _by(mp.pipeline_rows(facts, sessions, {"missing": True}, units=_units(missing)))
    assert by[149]["units"] == {"done": 0, "total": 4}


def test_no_data_gives_empty_ticket_units_and_null_units(tmp_path):
    missing = tmp_path / "no-such-log.jsonl"
    panel = _panel(tmp_path, missing, held=[151])
    assert panel["sessions"][0]["ticket_units"] == {}
    assert panel["sessions"][0]["held_issues"][0]["units"] is None
    facts = mp.pipeline_facts({150: _issue(150, ["ready-for-agent"])}, repo=REPO, gh=_gh(), cache={})
    by = _by(mp.pipeline_rows(facts, [], {"missing": True}, units=_units(missing)))
    assert by[150]["units"] is None


def test_cut_total_of_unheld_ticket_without_rows_gives_null_units(tmp_path):
    # rule 2 applies to a held ticket only: a cut total alone on a ready ticket gives no units
    missing = tmp_path / "no-such-log.jsonl"
    facts = mp.pipeline_facts({137: _issue(137, ["ready-for-agent"])}, repo=REPO, gh=_gh(), cache={},
                              cut_totals={137: 3})
    by = _by(mp.pipeline_rows(facts, [], {"missing": True}, units=_units(missing)))
    assert by[137]["units"] is None


def test_pipeline_facts_writes_cut_total_when_known(tmp_path):
    facts = mp.pipeline_facts({136: _issue(136, ["ready-for-agent"]), 137: _issue(137, ["ready-for-agent"])},
                              repo=REPO, gh=_gh(), cache={}, cut_totals={136: 3})
    assert facts["136"]["cut_total"] == 3
    assert "cut_total" not in facts["137"]
    assert mp.cut_totals_of(facts) == {136: 3}


def test_collect_agents_passes_ticket_units_through(tmp_path, monkeypatch):
    """The plugin reads the ORCH row of `collect.agents()`: `ticket_units` must survive its rebuild of each session."""
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "mods" / "pob-slots" / "bin"))
    import collect

    units = {"136": {"done": 3, "total": 3}}
    row = {"session_id": "abc", "name": "orch", "kind": "background", "state": "busy", "cwd": str(root),
           "held_issues": [], "subagents": [], "ticket_units": units}
    monkeypatch.setattr(ma, "_token_meter", lambda: SimpleNamespace(load_prices=lambda p: {}))
    monkeypatch.setattr(ma, "panel_agents", lambda *a, **k: {"sessions": [row]})
    monkeypatch.setattr(ma, "load_orch_state", lambda *a, **k: None)
    monkeypatch.setattr(ma, "live_sessions_raw", lambda *a, **k: [{"session_id": "abc", "kind": "background", "state": "busy", "cwd": str(root)}])
    monkeypatch.setattr(collect, "CACHE_DIR", tmp_path / ".cache")
    sessions = collect.agents()
    assert sessions[0]["ticket_units"] == units
