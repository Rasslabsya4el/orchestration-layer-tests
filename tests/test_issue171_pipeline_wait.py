"""#171 unit 1: a landed ticket that is parked is not at `accept`; it carries `wait` (its reason and its date); a ticket
sent back after its landing is `returned` and sits at `ready` (or `orch`/`work` when held), not at `accept`.

Seams: `monitor_pipeline.pipeline_rows` over facts (with `parked_line`, `parked_run`, `send_back_at`) and a merge payload
shaped like the collector's; `pipeline_facts` carrying those three from the collector's issues dict; the three pure
readers of a ticket's body and comments (`parked_line`, `parked_run_line`, `send_back_at`).

    python -m pytest tests/test_issue171_pipeline_wait.py -q -p no:faulthandler
"""
from __future__ import annotations

from tools.agentops import monitor_pipeline as mp

LANDED_AT = "2026-10-18T00:29:42"
READY_AT = "2026-10-17T20:00:00"
# a send-back far after / far before the landing, whatever the machine's time zone (the landing is local time)
SEND_BACK_NEWER = "2026-10-19T12:00:00Z"
SEND_BACK_OLDER = "2026-10-16T12:00:00Z"


def _facts(**extra) -> dict:
    """Ticket 135: open, ready-for-agent, not parked, no blocker; its label time is before the landing."""
    fact = {"title": "landed, still open", "open": True, "labels": ["ready-for-agent"], "blockers": [],
            "label_at": READY_AT}
    fact.update(extra)
    return {"135": fact}


def _parked(**extra) -> dict:
    """Ticket 135 with the `parked` label, not ready-for-agent."""
    fact = {"title": "landed, parked", "open": True, "labels": ["parked"], "blockers": [], "label_at": READY_AT}
    fact.update(extra)
    return {"135": fact}


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


def test_row1_landed_parked_ticket_waits_with_its_reason_and_date_not_accept():
    facts = _parked(parked_line="Parked: until 2026-10-24 19:30 (the owner's go for the other repo's ORCH)")
    row = _one(mp.pipeline_rows(facts, [], _merge(_row(0))))
    assert row["number"] == 135
    assert row["stage"] != "accept"
    assert row["stage"] == "ready"
    assert row["wait"]["until"] == "2026-10-24T19:30"
    assert row["wait"]["reason"] and "2026-10-24 19:30" in row["wait"]["reason"]
    assert row["returned"] is False


def test_wait_reason_is_cut_to_about_sixty_characters():
    long_line = "Parked: " + "waits for the owner's word on a long reason " * 3
    row = _one(mp.pipeline_rows(_parked(parked_line=long_line), [], _merge(_row(0))))
    assert len(row["wait"]["reason"]) <= 60
    assert row["wait"]["until"] is None


def test_parked_without_a_parked_line_falls_back_to_the_parked_on_run_first_line():
    facts = _parked(parked_run="<!-- parked-on-run: waits for the run 2026-10-24 20:30 -->")
    row = _one(mp.pipeline_rows(facts, [], _merge(_row(0))))
    assert row["stage"] == "ready"
    assert row["wait"]["until"] == "2026-10-24T20:30"
    assert row["wait"]["reason"]


def test_parked_with_no_reason_in_the_body_still_waits_with_a_reason():
    row = _one(mp.pipeline_rows(_parked(), [], _merge(_row(0))))
    assert row["stage"] == "ready"
    assert row["wait"] == {"reason": "parked", "until": None}


def test_row2_send_back_after_landing_returns_the_ticket_to_ready_not_accept():
    facts = _facts(send_back_at=SEND_BACK_NEWER)
    row = _one(mp.pipeline_rows(facts, [], _merge(_row(0))))
    assert row["stage"] == "ready"
    assert row["returned"] is True
    assert row["wait"] is None


def test_returned_ticket_held_by_orch_is_orch_not_accept():
    facts = _facts(send_back_at=SEND_BACK_NEWER)
    row = _one(mp.pipeline_rows(facts, _held_by_orch(), _merge(_row(0))))
    assert row["stage"] == "orch"
    assert row["returned"] is True


def test_send_back_after_a_failed_merge_row_is_not_returned():
    """Landed means rc 0: a ticket whose newest merge row did not land has no landing for a send-back to be newer than."""
    facts = _facts(send_back_at=SEND_BACK_NEWER)
    row = _one(mp.pipeline_rows(facts, [], _merge(_row(1, landed="2026-10-18T00:40:00"))))
    assert row["returned"] is False
    assert row["stage"] == "ready"


def test_unreadable_date_in_the_parked_line_keeps_the_reason_and_until_is_none():
    facts = _parked(parked_line="Parked: until 2026-13-45 19:30 (the owner's go)")
    row = _one(mp.pipeline_rows(facts, [], _merge(_row(0))))
    assert row["wait"] == {"reason": "until 2026-13-45 19:30 (the owner's go)", "until": None}
    assert row["stage"] == "ready"


def test_row3_landed_open_not_parked_no_send_back_after_is_accept():
    row = _one(mp.pipeline_rows(_facts(), [], _merge(_row(0))))
    assert row["stage"] == "accept"
    assert row["stage_since"] == LANDED_AT
    assert row["wait"] is None
    assert row["returned"] is False


def test_send_back_older_than_the_landing_is_accept():
    facts = _facts(send_back_at=SEND_BACK_OLDER)
    row = _one(mp.pipeline_rows(facts, [], _merge(_row(0))))
    assert row["stage"] == "accept"
    assert row["returned"] is False


def test_parked_line_is_the_first_parked_line_of_the_body():
    body = "## For the human\n- **What:** x\n\nParked: until 2026-10-24 19:30 (x)\nParked: later\n"
    assert mp.parked_line(body) == "Parked: until 2026-10-24 19:30 (x)"
    assert mp.parked_line("no marker here") is None
    assert mp.parked_line("") is None


def test_parked_run_line_is_the_first_line_of_the_newest_parked_on_run_comment():
    comments = [
        {"body": "<!-- parked-on-run: old 2026-10-20 10:00 -->\nmore", "createdAt": "2026-10-20T07:00:00Z"},
        {"body": "<!-- parked-on-run: new 2026-10-24 20:30 -->", "createdAt": "2026-10-21T07:00:00Z"},
        {"body": "Checkpoint 1 (ORCH abc)", "createdAt": "2026-10-22T07:00:00Z"},
    ]
    assert mp.parked_run_line(comments) == "<!-- parked-on-run: new 2026-10-24 20:30 -->"
    assert mp.parked_run_line([]) is None


def test_send_back_at_is_the_newest_send_back_comments_createdAt():
    comments = [
        {"body": "<!-- send-back -->\nfix x", "createdAt": "2026-10-18T09:19:00Z"},
        {"body": "a person's words", "createdAt": "2026-10-19T09:00:00Z"},
        {"body": "<!-- send-back -->\nfix y", "createdAt": "2026-10-18T09:40:00Z"},
    ]
    assert mp.send_back_at(comments) == "2026-10-18T09:40:00Z"
    assert mp.send_back_at([{"body": "words", "createdAt": "2026-10-18T09:00:00Z"}]) is None


def test_pipeline_facts_carries_the_parked_line_the_run_line_and_the_send_back_time():
    issues = {135: {"title": "t", "open": True, "labels": ["parked"], "blockers": [],
                    "parked_line": "Parked: until 2026-10-24 19:30 (x)",
                    "parked_run": None, "send_back_at": SEND_BACK_NEWER}}
    facts = mp.pipeline_facts(issues, repo="owner/repo", gh=lambda args: 1 / 0, cache={})
    assert facts["135"]["parked_line"] == "Parked: until 2026-10-24 19:30 (x)"
    assert facts["135"]["send_back_at"] == SEND_BACK_NEWER
    assert "parked_run" not in facts["135"]
