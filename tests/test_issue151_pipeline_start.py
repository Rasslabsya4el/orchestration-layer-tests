"""#151 unit 1: the pipeline's "с начала" (``started_at``) survives ORCH removing the ``ready-for-agent`` label on a take.

A ticket that was pipeline-eligible keeps its first ``ready-for-agent`` time in the cache while it stays open; a
closed or absent ticket leaves the cache; a ticket with no label time falls back to its claim time. The gh timeline
is a fake, as in tests/test_monitor_pipeline.py.

    python -m pytest tests/test_issue151_pipeline_start.py tests/test_monitor_pipeline.py -q -p no:faulthandler
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from tools.agentops import monitor_pipeline as mp

REPO = "owner/repo"
T = "2026-10-16T08:00:00Z"


def _issue(number: int, labels: list[str], *, open: bool = True, blockers: list[str] | None = None) -> dict:
    """One entry of the collector's `issues` map (what pipeline_facts reads)."""
    return {"title": f"ticket {number}", "open": open, "labels": list(labels), "blockers": list(blockers or [])}


def _timeline(*events: tuple[str, str, str]) -> str:
    """A one-page `gh api --paginate` answer: (event, label name, created_at) per timeline event."""
    return json.dumps([{"event": kind, "label": {"name": name}, "created_at": at} for kind, name, at in events])


def _gh(pages: dict[int, str]):
    """A fake gh: answers each ticket's timeline from `pages`, and records every call."""
    calls: list[list[str]] = []

    def fake_gh(args: list[str]):
        calls.append(list(args))
        number = int(args[1].split("/issues/")[1].split("/")[0])
        return SimpleNamespace(returncode=0, stdout=pages[number])

    return fake_gh, calls


def test_label_removed_on_take_keeps_the_first_ready_time_while_open():
    fake, calls = _gh({7: _timeline(("labeled", "ready-for-agent", T))})
    cache: dict = {}
    first = mp.pipeline_facts({7: _issue(7, ["ready-for-agent"])}, repo=REPO, gh=fake, cache=cache)
    assert first["7"]["label_at"] == T

    # ORCH takes the ticket: the label is gone, the ticket is still open and held
    second = mp.pipeline_facts({7: _issue(7, ["orch"])}, repo=REPO, gh=fake, cache=cache)
    assert second["7"]["label_at"] == T
    assert len(calls) == 1  # the taken ticket is not asked again

    sessions = [{"held": [{"number": 7, "claimed": True, "subagents": []}], "subagents": []}]
    by = {r["number"]: r for r in mp.pipeline_rows(second, sessions, {"missing": True})}
    assert by[7]["stage"] == "orch"
    assert by[7]["started_at"] == T


def test_the_first_ready_event_of_the_timeline_wins():
    fake, _ = _gh({7: _timeline(
        ("labeled", "ready-for-agent", "2026-10-16T08:00:00Z"),
        ("unlabeled", "ready-for-agent", "2026-10-16T08:30:00Z"),
        ("labeled", "ready-for-agent", "2026-10-16T09:00:00Z"),
    )})
    facts = mp.pipeline_facts({7: _issue(7, ["ready-for-agent"])}, repo=REPO, gh=fake, cache={})
    assert facts["7"]["label_at"] == "2026-10-16T08:00:00Z"


def test_a_relabelled_ticket_with_changed_labels_is_asked_again_and_takes_the_first_event():
    fake, calls = _gh({7: _timeline(
        ("labeled", "ready-for-agent", "2026-10-16T08:00:00Z"),
        ("labeled", "ready-for-agent", "2026-10-16T09:00:00Z"),
    )})
    cache: dict = {}
    mp.pipeline_facts({7: _issue(7, ["ready-for-agent"])}, repo=REPO, gh=fake, cache=cache)
    again = mp.pipeline_facts({7: _issue(7, ["ready-for-agent", "needs-info"])}, repo=REPO, gh=fake, cache=cache)
    assert len(calls) == 2
    assert again["7"]["label_at"] == "2026-10-16T08:00:00Z"


def test_no_label_time_falls_back_to_the_claim_time_then_none():
    facts = {"8": {"title": "ticket 8", "open": True, "labels": ["orch"], "blockers": []}}
    claimed = [{"held": [{"number": 8, "claimed": True, "claimed_at": "2026-10-16T08:45:00Z", "subagents": []}],
                "subagents": []}]
    by = {r["number"]: r for r in mp.pipeline_rows(facts, claimed, {"missing": True})}
    assert by[8]["started_at"] == "2026-10-16T08:45:00Z"
    assert by[8]["stage_since"] == "2026-10-16T08:45:00Z"

    unclaimed = [{"held": [{"number": 8, "subagents": []}], "subagents": []}]
    by = {r["number"]: r for r in mp.pipeline_rows(facts, unclaimed, {"missing": True})}
    assert by[8]["started_at"] is None


def test_a_closed_or_absent_ticket_leaves_the_cache():
    fake, _ = _gh({7: _timeline(("labeled", "ready-for-agent", T)),
                   9: _timeline(("labeled", "ready-for-agent", T))})
    cache: dict = {}
    mp.pipeline_facts({7: _issue(7, ["ready-for-agent"]), 9: _issue(9, ["ready-for-agent"])},
                      repo=REPO, gh=fake, cache=cache)
    assert set(cache) == {"7", "9"}

    # 7 is closed (still in the map, not open); 9 is absent from the map
    after = mp.pipeline_facts({7: _issue(7, ["ready-for-agent"], open=False)}, repo=REPO, gh=fake, cache=cache)
    assert cache == {}
    assert "7" not in after and "9" not in after
