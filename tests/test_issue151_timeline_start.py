"""#151 unit 1: the Pipeline row's "с начала" (``started_at``) is the ticket's FIRST ``ready-for-agent`` label event.

A ticket missing from the label cache (never seen eligible, e.g. already held by ORCH) gets that time from its issue
timeline once, then it is cached. A ticket with no such event falls back to its claim time. Only tickets that appear
as Pipeline rows are asked; a failed call is not cached. The gh timeline is a fake, as in tests/test_monitor_pipeline.py.

    python -m pytest tests/test_issue151_timeline_start.py -q -p no:faulthandler
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from tools.agentops import monitor_pipeline as mp

REPO = "owner/repo"
T = "2026-10-17T19:49:25Z"
T_LATER = "2026-10-17T20:10:00Z"
CLAIM = "2026-10-18T09:00:00Z"
NO_MERGE = {"missing": True}


def _issue(number: int, labels: list[str], *, open: bool = True, blockers: list[str] | None = None) -> dict:
    """One entry of the collector's `issues` map (what pipeline_facts reads)."""
    return {"title": f"ticket {number}", "open": open, "labels": list(labels), "blockers": list(blockers or [])}


def _timeline(*events: tuple[str, str, str]) -> str:
    """A one-page `gh api --paginate` answer: (event, label name, created_at) per timeline event."""
    return json.dumps([{"event": kind, "label": {"name": name}, "created_at": at} for kind, name, at in events])


def _gh(pages: dict[int, str], failing: tuple[int, ...] = ()):
    """A fake gh: answers each ticket's timeline from `pages` (a failing ticket answers returncode 1), records calls."""
    calls: list[list[str]] = []

    def fake_gh(args: list[str]):
        calls.append(list(args))
        number = int(args[1].split("/issues/")[1].split("/")[0])
        if number in failing:
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(returncode=0, stdout=pages[number])

    return fake_gh, calls


def _asked(calls: list[list[str]]) -> list[int]:
    return [int(args[1].split("/issues/")[1].split("/")[0]) for args in calls]


def _held(number: int, claimed_at: str | None = None) -> dict:
    entry: dict = {"number": number, "claimed": True, "subagents": []}
    if claimed_at:
        entry["claimed_at"] = claimed_at
    return {"held": [entry], "subagents": []}


def test_an_absent_ticket_gets_its_first_ready_time_from_the_timeline_and_is_cached():
    fake, calls = _gh({7: _timeline(
        ("labeled", "ready-for-agent", T),
        ("unlabeled", "ready-for-agent", T_LATER),
        ("labeled", "orch", T_LATER),
    )})
    cache: dict = {}
    facts = mp.pipeline_facts({7: _issue(7, ["orch"])}, repo=REPO, gh=fake, cache=cache)
    assert calls == []  # not eligible: pipeline_facts asks nothing for it

    sessions = [_held(7)]
    mp.fill_row_times(facts, mp.row_numbers(facts, sessions, NO_MERGE), repo=REPO, gh=fake, cache=cache)
    by = {r["number"]: r for r in mp.pipeline_rows(facts, sessions, NO_MERGE)}
    assert by[7]["stage"] == "orch"
    assert by[7]["started_at"] == T
    assert cache["7"]["at"] == T


def test_the_same_call_twice_makes_one_timeline_fetch():
    fake, calls = _gh({7: _timeline(("labeled", "ready-for-agent", T))})
    cache: dict = {}
    sessions = [_held(7)]
    for _ in range(2):
        facts = mp.pipeline_facts({7: _issue(7, ["orch"])}, repo=REPO, gh=fake, cache=cache)
        mp.fill_row_times(facts, mp.row_numbers(facts, sessions, NO_MERGE), repo=REPO, gh=fake, cache=cache)
        by = {r["number"]: r for r in mp.pipeline_rows(facts, sessions, NO_MERGE)}
        assert by[7]["started_at"] == T
    assert _asked(calls) == [7]


def test_a_ticket_with_no_ready_event_falls_back_to_its_claim_time():
    fake, _ = _gh({8: _timeline(("labeled", "orch", T))})
    cache: dict = {}
    facts = mp.pipeline_facts({8: _issue(8, ["orch"])}, repo=REPO, gh=fake, cache=cache)
    sessions = [_held(8, claimed_at=CLAIM)]
    mp.fill_row_times(facts, mp.row_numbers(facts, sessions, NO_MERGE), repo=REPO, gh=fake, cache=cache)
    by = {r["number"]: r for r in mp.pipeline_rows(facts, sessions, NO_MERGE)}
    assert by[8]["started_at"] == CLAIM
    assert by[8]["stage_since"] == CLAIM

    # no ready event and no claim time: no start at all
    facts = mp.pipeline_facts({8: _issue(8, ["orch"])}, repo=REPO, gh=fake, cache=cache)
    mp.fill_row_times(facts, mp.row_numbers(facts, [_held(8)], NO_MERGE), repo=REPO, gh=fake, cache=cache)
    by = {r["number"]: r for r in mp.pipeline_rows(facts, [_held(8)], NO_MERGE)}
    assert by[8]["started_at"] is None


def test_a_ready_label_added_at_t_and_removed_later_keeps_t():
    fake, _ = _gh({9: _timeline(
        ("labeled", "ready-for-agent", T),
        ("unlabeled", "ready-for-agent", T_LATER),
    )})
    cache: dict = {}
    facts = mp.pipeline_facts({9: _issue(9, [])}, repo=REPO, gh=fake, cache=cache)
    sessions = [_held(9, claimed_at=CLAIM)]
    mp.fill_row_times(facts, mp.row_numbers(facts, sessions, NO_MERGE), repo=REPO, gh=fake, cache=cache)
    by = {r["number"]: r for r in mp.pipeline_rows(facts, sessions, NO_MERGE)}
    assert by[9]["started_at"] == T  # the first event, not the claim and not the removal


def test_only_pipeline_rows_are_asked_and_a_merge_line_ticket_counts_as_a_row():
    fake, calls = _gh({7: _timeline(("labeled", "ready-for-agent", T)),
                       8: _timeline(("labeled", "ready-for-agent", T)),
                       9: _timeline(("labeled", "ready-for-agent", T))})
    cache: dict = {}
    facts = mp.pipeline_facts({7: _issue(7, ["orch"]), 8: _issue(8, ["needs-triage"]),
                               9: _issue(9, ["orch"]), 10: _issue(10, ["orch"], open=False)},
                              repo=REPO, gh=fake, cache=cache)
    sessions = [_held(7)]
    merge = {"running": {"issues": [9], "percent": 40, "failed": False, "queued": CLAIM}}
    mp.fill_row_times(facts, mp.row_numbers(facts, sessions, merge), repo=REPO, gh=fake, cache=cache)
    assert sorted(_asked(calls)) == [7, 9]  # 8 is no row, 10 is closed
    assert "8" not in cache and "10" not in cache


def test_a_failed_fetch_is_not_cached_and_is_asked_again_next_pull():
    fake, calls = _gh({7: _timeline(("labeled", "ready-for-agent", T))}, failing=(7,))
    cache: dict = {}
    sessions = [_held(7, claimed_at=CLAIM)]
    facts = mp.pipeline_facts({7: _issue(7, ["orch"])}, repo=REPO, gh=fake, cache=cache)
    mp.fill_row_times(facts, mp.row_numbers(facts, sessions, NO_MERGE), repo=REPO, gh=fake, cache=cache)
    assert cache == {}
    by = {r["number"]: r for r in mp.pipeline_rows(facts, sessions, NO_MERGE)}
    assert by[7]["started_at"] == CLAIM  # the claim time while the fetch keeps failing

    # the next pull asks again, and this time the answer is good
    fake2, calls2 = _gh({7: _timeline(("labeled", "ready-for-agent", T))})
    facts = mp.pipeline_facts({7: _issue(7, ["orch"])}, repo=REPO, gh=fake2, cache=cache)
    mp.fill_row_times(facts, mp.row_numbers(facts, sessions, NO_MERGE), repo=REPO, gh=fake2, cache=cache)
    assert _asked(calls2) == [7]
    assert cache["7"]["at"] == T
