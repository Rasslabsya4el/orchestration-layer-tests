"""tools/agentops/retro_comments.py (#109, unit 2): ticket-comment link failures of kinds d, e, f, g for the weekly retro.

Planted gh JSON through a fake ``run_gh``: one GraphQL search per kind, the fake routes each call by its search text.
The clock is pinned (`_now`). Every expected value is written from the spec (the dispatcher's alarm text, the takeover
texts, the one-day rule, the closer rule, the parked form), never computed the way the code computes it. The one
local-time reading (`until <date> <time>` is local naive) is pinned by a naive window, so the literal holds in any zone."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from tools.agentops import retro_comments
from tools.agentops.retro_comments import comment_failures

REPO = "acme/orchestration-layer"
SINCE = datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc)
UNTIL = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
#: naive = local wall clock: the window of the parked literal (27 h between 16.10 12:00 and 17.10 15:00 local)
LOCAL_SINCE = datetime(2026, 10, 16, 0, 0)
LOCAL_UNTIL = datetime(2026, 10, 17, 15, 0)
MERGE_LANDED = "<!-- merge result -->\nMerge of orch-204 landed on main 0123456789ab after 4 min in line and a suite of 3 min"


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    monkeypatch.setattr(retro_comments, "_now", lambda: NOW)


def _url(number: int, n: int) -> str:
    return f"https://github.com/{REPO}/issues/{number}#issuecomment-{n}"


def _comment(number: int, n: int, body: str, created: str) -> dict:
    return {"body": body, "createdAt": created, "url": _url(number, n), "author": {"login": "acme-bot"}}


def _node(number: int, *, comments=(), state="OPEN", closed_at=None, body="", labels=()) -> dict:
    return {
        "number": number,
        "url": f"https://github.com/{REPO}/issues/{number}",
        "state": state,
        "closedAt": closed_at,
        "body": body,
        "labels": {"nodes": [{"name": name} for name in labels]},
        "comments": {"nodes": list(comments)},
    }


def _search(nodes) -> dict:
    return {"data": {"search": {"issueCount": len(nodes), "nodes": nodes}}}


class FakeGh:
    """Routes each planted call by the text it carries; records every call; raises for a planted failure."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        text = " ".join(args)
        for marker, payload in self.routes.items():
            if marker in text:
                if isinstance(payload, Exception):
                    raise payload
                return json.dumps(payload)
        raise AssertionError(f"unplanted call: {args}")


def _empty_routes() -> dict:
    return {"ended without": _search([]), "taking over": _search([]), "Merge of": _search([]),
            "label:parked": _search([])}


def _planted_routes() -> dict:
    """One takeover comment, one stale parked ticket, one stale merge result, one no-checkpoint ending, each beside a
    negative that must not count."""
    d_nodes = [
        _node(201, comments=[_comment(201, 1, "ORCH session 3f2a9c1d ended without a closing or parking checkpoint",
                                      "2026-10-16T10:00:00Z")]),
        # before the window: not counted
        _node(211, comments=[_comment(211, 2, "ORCH session 00aa11bb ended without a closing or parking checkpoint",
                                      "2026-10-15T10:00:00Z")]),
    ]
    e_nodes = [
        _node(202, comments=[_comment(202, 3, "taking over #202 from b1da63f4: claimed; no worker started.",
                                      "2026-10-16T11:00:00Z")]),
        # a checkpoint of an ORCH that takes nothing over: not counted
        _node(203, comments=[_comment(203, 4, "Checkpoint 1 (ORCH 1234abcd, #203). done: read the code.",
                                      "2026-10-16T12:00:00Z")]),
    ]
    f_nodes = [
        # stale: no closer, merged 2026-10-16 09:00Z, 30 h before the window end
        _node(204, comments=[_comment(204, 5, MERGE_LANDED, "2026-10-16T09:00:00Z")]),
        # fresh: 9 h before the window end, not counted
        _node(205, comments=[_comment(205, 6, MERGE_LANDED, "2026-10-17T06:00:00Z")]),
        # ORCH's own closing comment is a closer: not counted
        _node(206, comments=[_comment(206, 7, MERGE_LANDED, "2026-10-16T08:00:00Z"),
                             _comment(206, 8, "Closing #206 (ORCH 1234abcd): done in row 1", "2026-10-16T09:30:00Z")]),
    ]
    g_nodes = [
        # stale: the event (16.10 12:00 local) passed, still parked
        _node(207, labels=["parked"], body="Parked: until 2026-10-16 12:00\nwaiting for the owner's answer"),
        # not yet: the event is in 3 days
        _node(208, labels=["parked"], body="Parked: until 2026-10-19 12:00"),
        # a human wait is never flipped: not counted
        _node(209, labels=["parked", "needs-info"], body="Parked: until 2026-10-16 12:00"),
    ]
    return {
        "ended without": _search(d_nodes),
        "taking over": _search(e_nodes),
        "Merge of": _search(f_nodes),
        "label:parked": _search(g_nodes),
    }


def _by_kind(result: list[dict]) -> dict:
    return {row["kind"]: row for row in result}


def test_planted_run_counts_each_kind_exactly():
    result = comment_failures(REPO, SINCE, UNTIL, FakeGh(_planted_routes()))

    assert [row["kind"] for row in result] == ["d", "e", "f", "g"]
    rows = _by_kind(result)
    assert rows["d"]["count"] == 1
    assert rows["e"]["count"] == 1
    assert rows["f"]["count"] == 1
    assert rows["g"]["count"] == 1
    # the merge result from 2026-10-16 09:00Z to the window end 2026-10-17 15:00Z is 30 h
    assert rows["f"]["lost_s"] == 108000
    assert rows["d"]["lost_s"] is None
    assert rows["e"]["lost_s"] is None
    assert rows["d"]["pointers"] == [_url(201, 1)]
    assert rows["e"]["pointers"] == [_url(202, 3)]
    assert rows["f"]["pointers"] == [_url(204, 5)]
    assert rows["g"]["pointers"] == [f"https://github.com/{REPO}/issues/207"]


def test_run_makes_at_most_four_gh_calls():
    gh = FakeGh(_planted_routes())
    comment_failures(REPO, SINCE, UNTIL, gh)
    assert len(gh.calls) <= 4
    assert len(gh.calls) == 4  # one GraphQL search per kind, no per-issue call


def test_writes_nothing_only_graphql_reads():
    gh = FakeGh(_planted_routes())
    comment_failures(REPO, SINCE, UNTIL, gh)
    forbidden = {"edit", "close", "comment", "--add-label", "--remove-label", "--body-file", "-X", "--method"}
    for args in gh.calls:
        assert args[:2] == ["api", "graphql"]
        assert not forbidden & set(args)


def test_kind_without_a_source_counts_zero_with_no_source_note():
    routes = _planted_routes()
    routes["taking over"] = RuntimeError("gh unreachable")
    rows = _by_kind(comment_failures(REPO, SINCE, UNTIL, FakeGh(routes)))
    assert rows["e"]["count"] == 0
    assert rows["e"]["note"] == "no source"
    assert rows["d"]["count"] == 1
    assert rows["f"]["count"] == 1
    assert rows["g"]["count"] == 1


def test_pointers_capped_at_three_while_count_is_full():
    d_nodes = [
        _node(300 + i, comments=[_comment(300 + i, 10 + i, "ORCH session 3f2a9c1d ended without a closing or "
                                                          "parking checkpoint", f"2026-10-16T1{i}:00:00Z")])
        for i in range(4)
    ]
    routes = _planted_routes()
    routes["ended without"] = _search(d_nodes)
    rows = _by_kind(comment_failures(REPO, SINCE, UNTIL, FakeGh(routes)))
    assert rows["d"]["count"] == 4
    assert len(rows["d"]["pointers"]) == 3


def test_takeover_counts_both_texts_and_not_continuing():
    e_nodes = [
        # the issue's wording
        _node(202, comments=[_comment(202, 3, "taking over #202 from b1da63f4: claimed", "2026-10-16T11:00:00Z")]),
        # the dispatcher's form (dispatch.py:2510)
        _node(215, comments=[_comment(215, 9, "ORCH 3f2a9c1d takes #215 over from b1da63f4: claimed",
                                      "2026-10-16T11:10:00Z")]),
        # the real checkpoint wording, without the number
        _node(216, comments=[_comment(216, 10, "ORCH (session taking over from 76019696's hand-off): claimed",
                                      "2026-10-16T11:20:00Z")]),
        # no source for "continuing #N": not counted
        _node(217, comments=[_comment(217, 11, "continuing #217 from b1da63f4: claimed", "2026-10-16T11:30:00Z")]),
    ]
    routes = _empty_routes()
    routes["taking over"] = _search(e_nodes)
    rows = _by_kind(comment_failures(REPO, SINCE, UNTIL, FakeGh(routes)))
    assert rows["e"]["count"] == 3
    assert rows["e"]["pointers"] == [_url(202, 3), _url(215, 9), _url(216, 10)]
    assert "takes #N over" in rows["e"]["note"]


def test_merge_result_closers_clear_it_and_a_machine_line_does_not():
    f_nodes = [
        # a person's later comment closes the loop: not counted
        _node(218, comments=[_comment(218, 12, MERGE_LANDED, "2026-10-16T08:00:00Z"),
                             _comment(218, 13, "looks good, merging the follow-up", "2026-10-16T08:30:00Z")]),
        # the issue closed on 17.10 20:00Z (after the window end, before now): a closer, not counted
        _node(219, state="CLOSED", closed_at="2026-10-17T20:00:00Z",
              comments=[_comment(219, 14, MERGE_LANDED, "2026-10-16T08:30:00Z")]),
        # a later machine line is no closer: stale, 32 h before the window end
        _node(220, comments=[_comment(220, 15, MERGE_LANDED, "2026-10-16T07:00:00Z"),
                             _comment(220, 16, "<!-- parked-on-run: a run of the heavy tool -->\nparked",
                                      "2026-10-16T07:30:00Z")]),
    ]
    routes = _empty_routes()
    routes["Merge of"] = _search(f_nodes)
    rows = _by_kind(comment_failures(REPO, SINCE, UNTIL, FakeGh(routes)))
    assert rows["f"]["count"] == 1
    assert rows["f"]["lost_s"] == 115200  # 16.10 07:00Z to 17.10 15:00Z
    assert rows["f"]["pointers"] == [_url(220, 15)]


def test_parked_literal_is_the_local_wall_clock_gap():
    routes = _empty_routes()
    routes["label:parked"] = _search([
        _node(207, labels=["parked"], body="Parked: until 2026-10-16 12:00"),
        _node(208, labels=["parked"], body="Parked: until 2026-10-19 12:00"),
    ])
    rows = _by_kind(comment_failures(REPO, LOCAL_SINCE, LOCAL_UNTIL, FakeGh(routes)))
    assert rows["g"]["count"] == 1
    assert rows["g"]["lost_s"] == 97200  # 16.10 12:00 to 17.10 15:00, 27 h


def test_mixed_parked_batch_sums_the_known_times_and_counts_the_undated():
    routes = _empty_routes()
    routes["label:parked"] = _search([
        _node(207, labels=["parked"], body="Parked: until 2026-10-16 12:00"),
        _node(301, labels=["parked"], body="Parked: until #300 closes"),
    ])
    routes["repository("] = {"data": {"r0": {"issue": {"state": "CLOSED"}}}}
    gh = FakeGh(routes)
    rows = _by_kind(comment_failures(REPO, LOCAL_SINCE, LOCAL_UNTIL, gh))
    assert rows["g"]["count"] == 2
    assert rows["g"]["lost_s"] == 97200  # the date form only
    assert "1 of 2 stale ticket(s) had no event time" in rows["g"]["note"]
    assert len(gh.calls) == 5  # four searches and one batched state read


def test_issue_form_reads_states_in_one_batch_call():
    routes = _empty_routes()
    routes["label:parked"] = _search([
        _node(401, labels=["parked"], body="Parked: until #300 closes"),
        _node(402, labels=["parked"], body="Parked: until #301 closes"),
    ])
    routes["repository("] = {"data": {"r0": {"issue": {"state": "CLOSED"}},
                                      "r1": {"issue": {"state": "CLOSED"}}}}
    gh = FakeGh(routes)
    rows = _by_kind(comment_failures(REPO, SINCE, UNTIL, gh))
    assert rows["g"]["count"] == 2
    batch_calls = [args for args in gh.calls if any("repository(" in a for a in args)]
    assert len(batch_calls) == 1
    assert len(gh.calls) == 5  # four searches and the one batched state read


def test_kinds_a_b_c_h_are_not_returned():
    result = comment_failures(REPO, SINCE, UNTIL, FakeGh(_planted_routes()))
    assert [row["kind"] for row in result] == ["d", "e", "f", "g"]
