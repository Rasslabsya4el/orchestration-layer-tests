"""#6 unit 1: the monitor's pipeline rows, one per ticket, from the facts the collector already reads.

The fixture is six tickets: one ready, one held with no subagent, one held with two live workers (one of them
worker-strong, plus one finished worker-strong that must not count) and one live researcher-how, one waiting second
in the merge queue, one merging at 40 %, and one `parked` (must not appear). The gh timeline is a fake.

    python -m pytest tests/test_monitor_pipeline.py -q -p no:faulthandler
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from tools.agentops import monitor_pipeline as mp

REPO = "owner/repo"


def _facts() -> dict:
    """What collect.py's tickets() writes: per open ticket its title, labels and open blockers; `label_at` is the
    time the ready-for-agent label was added (from the timeline)."""
    return {
        "1": {"title": "ready one", "open": True, "labels": ["ready-for-agent"], "blockers": [],
              "label_at": "2026-10-15T09:40:00Z"},
        "2": {"title": "held, no subagent", "open": True, "labels": ["ready-for-agent"], "blockers": [],
              "label_at": "2026-10-15T09:20:00Z"},
        "3": {"title": "held, two workers", "open": True, "labels": ["ready-for-agent"], "blockers": [],
              "label_at": "2026-10-15T09:10:00Z"},
        "4": {"title": "waiting merge", "open": True, "labels": ["ready-for-agent"], "blockers": [],
              "label_at": "2026-10-15T09:30:00Z"},
        "5": {"title": "merging", "open": True, "labels": ["ready-for-agent"], "blockers": [],
              "label_at": "2026-10-15T09:00:00Z"},
        "6": {"title": "parked", "open": True, "labels": ["ready-for-agent", "parked"], "blockers": [],
              "label_at": "2026-10-15T08:00:00Z"},
    }


def _sessions() -> list[dict]:
    """The `agents` payload shape of collect.agents(): one ORCH session holding issues 2 and 3; `subagents` is the
    flat list of every subagent (held ones included), each with `type`, `issue`, `busy`."""
    return [{
        "sid": "orch-session",
        "held": [
            {"number": 2, "url": "https://github.com/owner/repo/issues/2", "claimed": False, "subagents": []},
            {"number": 3, "url": "https://github.com/owner/repo/issues/3", "claimed": True, "subagents": []},
        ],
        "subagents": [
            {"type": "worker", "issue": 3, "busy": True, "started": "2026-10-15T09:55:00+00:00"},
            {"type": "worker-strong", "issue": 3, "busy": True, "started": "2026-10-15T09:50:00+00:00"},
            {"type": "worker-strong", "issue": 3, "busy": False, "started": "2026-10-15T09:05:00+00:00"},
            {"type": "researcher-how", "issue": 3, "busy": True, "started": None},
        ],
    }]


def _merge() -> dict:
    """The `merge` payload shape of collect.merge_status(): issue 5 runs at 40 %, issue 4 waits second."""
    return {
        "running": {"kind": "branch", "branches": ["orch-5"], "issues": [5], "phase": "suite", "percent": 40,
                    "failed": 0, "started": "2026-10-15T12:00:00+00:00", "queued": "2026-10-15T11:50:00+00:00"},
        "waiting": [{"branch": "orch-4", "issues": [4], "position": 1, "queued": "2026-10-15T12:05:00+00:00"}],
        "landed": [],
        "full_run": None,
    }


def _by_number(rows: list[dict]) -> dict[int, dict]:
    return {r["number"]: r for r in rows}


def test_five_rows_parked_ticket_absent_and_order():
    rows = mp.pipeline_rows(_facts(), _sessions(), _merge())
    assert [r["number"] for r in rows] == [5, 4, 3, 2, 1]
    assert [r["stage"] for r in rows] == ["merge", "merge", "work", "orch", "ready"]


def test_stage_counts_and_merge_fields():
    by = _by_number(mp.pipeline_rows(_facts(), _sessions(), _merge()))
    assert by[5]["merge_position"] == 1 and by[5]["merge_percent"] == 40
    assert by[4]["merge_position"] == 2 and by[4]["merge_percent"] is None
    assert by[3]["workers"] == 2 and by[3]["strong"] == 1 and by[3]["researchers"] == 1
    assert by[3]["merge_position"] is None and by[3]["merge_percent"] is None
    assert by[2]["workers"] == 0 and by[2]["strong"] == 0 and by[2]["researchers"] == 0
    assert by[1]["workers"] == 0 and by[1]["merge_position"] is None
    assert all(r["failed"] is False for r in by.values())


def test_times_and_row_shape():
    rows = mp.pipeline_rows(_facts(), _sessions(), _merge())
    by = _by_number(rows)
    assert by[1]["started_at"] == "2026-10-15T09:40:00Z" and by[1]["stage_since"] == "2026-10-15T09:40:00Z"
    assert by[5]["stage_since"] == "2026-10-15T11:50:00+00:00"  # the running merge's enqueue time, not the suite start
    assert by[4]["stage_since"] == "2026-10-15T12:05:00+00:00"
    assert by[3]["stage_since"] == "2026-10-15T09:50:00+00:00"  # the earliest live subagent
    assert by[2]["stage_since"] is None
    keys = {"number", "title", "stage", "failed", "workers", "strong", "researchers",
            "merge_position", "merge_percent", "started_at", "stage_since", "units", "wait", "returned"}  # #171: + wait, returned
    assert all(set(r) == keys for r in rows)
    assert by[3]["title"] == "held, two workers"
    json.dumps(rows)  # plain JSON values only


def _events() -> list[dict]:
    """The dispatcher's events file (`artifacts/runs/events.jsonl`): the ORCH session starts, then takes issue 2
    (`issue_taken`, how `claimed`); issue 7 was claimed by an older session, before this one started."""
    return [
        {"ts": "2026-10-15T08:30:00", "kind": "issue_taken", "issue": 7, "how": "claimed"},
        {"ts": "2026-10-15T09:00:00", "kind": "orch_start", "session": "orch-session", "issues": [2, 3]},
        {"ts": "2026-10-15T09:21:00", "kind": "issue_taken", "issue": 2, "how": "claimed"},
        {"ts": "2026-10-15T09:22:00", "kind": "issue_taken", "issue": 3, "how": "offered"},
    ]


def test_row_six_claimed_event_and_running_merge_give_both_stages_a_time():
    # #6 row 6: a held ticket with no subagent (orch) takes the claimed event's time, and the running merge
    # (merge) takes its enqueue time, so both stages show a time
    sessions = _sessions()
    claims = mp.claim_times(_events(), ["orch-session"])
    for held in sessions[0]["held"]:
        if held["number"] in claims:
            held["claimed_at"] = claims[held["number"]]
    rows = mp.pipeline_rows(_facts(), sessions, _merge())
    by = _by_number(rows)
    assert by[2]["stage"] == "orch" and by[2]["stage_since"] == "2026-10-15T09:21:00"
    assert by[5]["stage"] == "merge" and by[5]["stage_since"] == "2026-10-15T11:50:00+00:00"
    assert by[3]["stage"] == "work" and by[3]["stage_since"] == "2026-10-15T09:50:00+00:00"  # live subagents keep theirs


def test_claim_times_takes_the_newest_claim_of_this_session_only():
    events = _events() + [{"ts": "2026-10-15T09:40:00", "kind": "issue_taken", "issue": 2, "how": "claimed"}]
    assert mp.claim_times(events, ["orch-session"]) == {2: "2026-10-15T09:40:00"}
    assert mp.claim_times(events, ["someone-else"]) == {}
    # issue 7's claim is older than this session's start: it is no claim of this session
    assert 7 not in mp.claim_times(_events(), ["orch-session"])


def test_closed_or_unknown_tickets_leave_the_pipeline():
    # #2 is held but closed (it left the pipeline when it closed); #5 holds the line but is absent from the facts:
    # neither gets a row. The line itself still counts #5 as running, so #4 keeps its second place.
    facts = _facts()
    facts["2"]["open"] = False
    del facts["5"]
    rows = mp.pipeline_rows(facts, _sessions(), _merge())
    assert [r["number"] for r in rows] == [4, 3, 1]
    assert [r["stage"] for r in rows] == ["merge", "work", "ready"]
    assert _by_number(rows)[4]["merge_position"] == 2


def test_no_merge_data_keeps_the_held_and_ready_rows():
    # without the merge line, the two merge tickets are only labelled ready: they show as ready, oldest label first
    rows = mp.pipeline_rows(_facts(), _sessions(), {"missing": True})
    assert [r["number"] for r in rows] == [3, 2, 5, 4, 1]
    assert [r["stage"] for r in rows] == ["work", "orch", "ready", "ready", "ready"]


def test_gh_timeline_called_at_most_once_per_ticket_across_two_pulls():
    calls: list[list[str]] = []
    pages = {
        # two pages, as `gh api --paginate` prints them: the label event sits on the second one
        1: json.dumps([{"event": "commented", "created_at": "2026-10-15T09:00:00Z"}]) + json.dumps([
            {"event": "labeled", "label": {"name": "needs-triage"}, "created_at": "2026-10-15T09:10:00Z"},
            {"event": "labeled", "label": {"name": "ready-for-agent"}, "created_at": "2026-10-15T09:40:00Z"}]),
        2: json.dumps([{"event": "labeled", "label": {"name": "ready-for-agent"}, "created_at": "2026-10-15T09:20:00Z"}]),
        3: json.dumps([{"event": "labeled", "label": {"name": "ready-for-agent"}, "created_at": "2026-10-15T09:10:00Z"}]),
        4: json.dumps([{"event": "labeled", "label": {"name": "ready-for-agent"}, "created_at": "2026-10-15T09:30:00Z"}]),
        5: json.dumps([{"event": "labeled", "label": {"name": "ready-for-agent"}, "created_at": "2026-10-15T09:00:00Z"}]),
    }

    def fake_gh(args: list[str]):
        calls.append(list(args))
        number = int(args[1].split("/issues/")[1].split("/")[0])
        return SimpleNamespace(returncode=0, stdout=pages[number])

    issues = {n: {"n": n, "title": f["title"], "open": True, "labels": f["labels"], "blockers": [],
                  "parked": "parked" in f["labels"]} for n, f in ((int(k), v) for k, v in _facts().items())}
    cache: dict = {}
    first = mp.pipeline_facts(issues, repo=REPO, gh=fake_gh, cache=cache)
    labelled_once = [args[1] for args in calls]
    second = mp.pipeline_facts(issues, repo=REPO, gh=fake_gh, cache=cache)

    # the parked ticket is never asked; each other ready ticket is asked once, and the second pull asks nothing
    assert sorted(labelled_once) == sorted({f"repos/{REPO}/issues/{n}/timeline" for n in (1, 2, 3, 4, 5)})
    assert len(calls) == 5
    assert all(args[0] == "api" and args[-1] == "--paginate" for args in calls)
    # the label event of the second page is read, not the first page's other events
    assert first["1"]["label_at"] == "2026-10-15T09:40:00Z"
    assert second["1"]["label_at"] == "2026-10-15T09:40:00Z"
    assert "label_at" not in first["6"]
    # a ticket whose label set changes is asked again
    issues[2]["labels"] = ["ready-for-agent", "needs-info"]
    mp.pipeline_facts(issues, repo=REPO, gh=fake_gh, cache=cache)
    assert len(calls) == 6


def test_live_orch_stage_gets_its_claim_time_from_the_events_file(tmp_path):
    # #6 wiring: the state file names the ORCH session; load_orch_state reads the events file beside it, and
    # attach_held_issues puts each held issue's claimed time on its entry, which pipeline_rows shows as the orch stage
    from tools.agentops import monitor_agents as ma

    state = tmp_path / "orch_session.json"
    state.write_text(json.dumps({"session": "orch-s8", "session_uuid": "uuid-1", "held": [2, 3], "claimed_now": [2]}),
                     encoding="utf-8")
    events = tmp_path / "events.jsonl"
    events.write_text("\n".join([
        json.dumps({"ts": "2026-10-15T08:30:00", "kind": "issue_taken", "issue": 2, "how": "claimed"}),  # an older session's
        json.dumps({"ts": "2026-10-15T09:00:00", "kind": "orch_start", "session": "orch-s8", "issues": [2, 3]}),
        "{not json",  # a garbled line is no event
        json.dumps({"ts": "2026-10-15T09:21:00", "kind": "issue_taken", "issue": 2, "how": "claimed"}),
    ]) + "\n", encoding="utf-8")

    row = {"session_id": "uuid-1", "subagents": []}
    ma.attach_held_issues(row, ma.load_orch_state(state, events_path=events))
    held = {h["number"]: h for h in row["held_issues"]}
    assert held[2]["claimed_at"] == "2026-10-15T09:21:00"
    assert held[3]["claimed_at"] == "2026-10-15T09:00:00"  # taken through `offered`: the session's start, as holding_window_from_events

    facts = {str(n): {"title": f"held {n}", "open": True, "labels": ["ready-for-agent"], "blockers": []} for n in (2, 3)}
    by = _by_number(mp.pipeline_rows(facts, [{"held": row["held_issues"], "subagents": []}], {"missing": True}))
    assert by[2]["stage"] == "orch" and by[2]["stage_since"] == "2026-10-15T09:21:00"
    assert by[3]["stage"] == "orch" and by[3]["stage_since"] == "2026-10-15T09:00:00"


def test_claim_times_skips_an_event_whose_issue_is_not_a_number():
    # a garbled `issue` (a word, a list, null) is no claim: it must not fail the agents pull
    events = _events() + [
        {"ts": "2026-10-15T09:30:00", "kind": "issue_taken", "issue": "twelve", "how": "claimed"},
        {"ts": "2026-10-15T09:31:00", "kind": "issue_taken", "issue": [4], "how": "claimed"},
        {"ts": "2026-10-15T09:32:00", "kind": "issue_taken", "issue": None, "how": "claimed"},
    ]
    assert mp.claim_times(events, ["orch-session"]) == {2: "2026-10-15T09:21:00"}


def test_running_merge_without_a_queued_time_keeps_its_suite_start():
    # no claimed ticket, so no enqueue time: the row keeps the suite start instead of losing its time
    merge = _merge()
    del merge["running"]["queued"]
    by = _by_number(mp.pipeline_rows(_facts(), _sessions(), merge))
    assert by[5]["stage_since"] == "2026-10-15T12:00:00+00:00"
