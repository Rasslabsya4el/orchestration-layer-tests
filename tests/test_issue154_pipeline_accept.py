"""#154 unit 1: a ticket that landed on main and is still open is at stage `accept` (after `merge`), whether or not an
ORCH holds it; a ticket whose newest merge-line row did not land keeps today's stage.

Seams: `monitor_pipeline.pipeline_rows` over a merge payload shaped like the collector's (`landed` rows with
`issues`, `rc`, `landed`), and the collector's own `merge_shape` (it must pass `landed` through).

    python -m pytest tests/test_issue154_pipeline_accept.py -q -p no:faulthandler
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from tools.agentops import monitor_pipeline as mp

LANDED_AT = "2026-10-18T00:29:42"
READY_AT = "2026-10-17T20:00:00"


def _facts() -> dict:
    """Ticket 135: open, ready-for-agent, not parked, no blocker; its label time is before the landing."""
    return {"135": {"title": "landed, still open", "open": True, "labels": ["ready-for-agent"], "blockers": [],
                    "label_at": READY_AT}}


def _merge(*landed: dict) -> dict:
    """The `merge` payload shape of collect.merge_status(): no suite running, no waiters, the given landed rows."""
    return {"running": None, "waiting": [], "landed": list(landed), "full_run": None}


def _row(rc: int, landed: str = LANDED_AT, issues: tuple = (135,)) -> dict:
    return {"branch": "orch-135", "issues": list(issues), "queued": "2026-10-18T00:20:00", "landed": landed,
            "queued_to_land_s": 582, "rc": rc, "reason": None, "failed": 0, "attempts": 1, "failed_before": 0}


def _held_by_orch() -> list[dict]:
    """One ORCH session holding ticket 135, claimed before the landing."""
    return [{"sid": "orch-session", "held": [
        {"number": 135, "url": "https://github.com/owner/repo/issues/135", "claimed": True,
         "claimed_at": "2026-10-17T21:00:00Z", "subagents": []},
    ], "subagents": []}]


def _one(rows: list[dict]) -> dict:
    assert len(rows) == 1, rows
    return rows[0]


def test_row1_landed_open_ticket_not_held_is_accept_at_its_landed_time():
    row = _one(mp.pipeline_rows(_facts(), [], _merge(_row(0))))
    assert row["number"] == 135
    assert row["stage"] == "accept"
    assert row["stage_since"] == LANDED_AT


def test_row2_landed_open_ticket_held_by_orch_is_accept():
    row = _one(mp.pipeline_rows(_facts(), _held_by_orch(), _merge(_row(0))))
    assert row["stage"] == "accept"
    assert row["stage_since"] == LANDED_AT


def test_row3_newest_row_did_not_land_keeps_today_stage_ready():
    row = _one(mp.pipeline_rows(_facts(), [], _merge(_row(1))))
    assert row["stage"] == "ready"
    assert row["stage_since"] == READY_AT


def test_newest_landed_time_decides_not_the_list_order():
    """The list is newest first from the collector, but the rule is the landed time: an older did-not-land row
    listed first does not hide a newer landing, and a newer did-not-land row beats an older landing."""
    older_fail = _row(1, landed="2026-10-18T00:10:00")
    newer_ok = _row(0, landed="2026-10-18T00:29:42")
    assert _one(mp.pipeline_rows(_facts(), [], _merge(older_fail, newer_ok)))["stage"] == "accept"
    assert _one(mp.pipeline_rows(_facts(), [], _merge(newer_ok, older_fail)))["stage"] == "accept"
    newer_fail = _row(1, landed="2026-10-18T00:40:00")
    assert _one(mp.pipeline_rows(_facts(), [], _merge(newer_ok, newer_fail)))["stage"] == "ready"


def test_running_merge_of_the_ticket_still_wins_over_accept():
    """A live suite for the ticket is the stage it is at now; accept is the state after a landing with no newer run."""
    merge = _merge(_row(0))
    merge["running"] = {"kind": "branch", "branches": ["orch-135"], "issues": [135], "phase": "suite",
                        "percent": 10, "failed": 0, "started": "2026-10-18T01:00:00+00:00",
                        "queued": "2026-10-18T00:58:00+00:00"}
    row = _one(mp.pipeline_rows(_facts(), [], merge))
    assert row["stage"] == "merge"


def test_accept_sorts_after_merge_before_work_orch_ready():
    facts = _facts()
    facts["136"] = {"title": "ready only", "open": True, "labels": ["ready-for-agent"], "blockers": [],
                    "label_at": "2026-10-17T10:00:00"}
    rows = mp.pipeline_rows(facts, _held_by_orch(), _merge(_row(0)))
    assert [r["stage"] for r in rows] == ["accept", "ready"]
    assert mp.STAGE_RANK["merge"] < mp.STAGE_RANK["accept"] < mp.STAGE_RANK["work"] < mp.STAGE_RANK["orch"] \
        < mp.STAGE_RANK["ready"]


def test_collector_merge_shape_passes_landed_rows_through_to_the_pipeline():
    """The collector's merge payload (merge_shape) keeps `landed`, so the pipeline sees the landing."""
    spec = importlib.util.spec_from_file_location(
        "collect154", sorted((Path(__file__).resolve().parents[1] / "mods").glob("*/bin/collect.py"))[0])
    collect = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collect)
    data = {"at": "2026-10-18T00:40:00", "running": None, "waiting": [], "full_run": None,
            "landed": [_row(0)]}
    shaped = collect.merge_shape(data)
    assert shaped["landed"][0]["issues"] == [135] and shaped["landed"][0]["rc"] == 0
    assert _one(mp.pipeline_rows(_facts(), [], shaped))["stage"] == "accept"
