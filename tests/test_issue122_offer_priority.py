"""#122 unit 1 (offer + admission): a Priority ticket (label `priority` or `flow-fix`) heads the offer, ranked by
class then by the older mark; a failed timeline read never breaks the tick; an offer that holds a Priority
ticket is a go without the admission hook.

Seams: `tools.agentops.priority_rank` (pure, the timeline reader injected as a stub with `.timeline`, the
`TimelineReader` protocol), `Dispatcher._eligible_candidates(offering=True)` and `Dispatcher.tick()` with a
fake `gh`, a fake `claude` and an injected `timeline_reader` (no network, no `gh`)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from tests.test_agentops_dispatch import FakeClaude, FakeGh, _dispatcher
from tools.agentops import priority_rank
from tools.agentops.dispatch import IssueSummary

READY = ("ready-for-agent",)


def _at(hm: str) -> datetime:
    h, m = hm.split(":")
    return datetime(2026, 10, 17, int(h), int(m), tzinfo=timezone.utc)


def _stamp(hm: str) -> str:
    return f"2026-10-17T{hm}:00Z"


def _labeled(label: str, hm: str) -> dict:
    return {"event": "labeled", "label": {"name": label}, "created_at": _stamp(hm)}


class FakeTimeline:
    """A stub of the `TimelineReader` protocol: `timeline(issue)` returns the fixture events, raises the
    fixture error for an issue listed in `errors`, and records every call."""

    def __init__(self, events: dict[int, list[dict]] | None = None,
                 errors: dict[int, Exception] | None = None) -> None:
        self.events = dict(events or {})
        self.errors = dict(errors or {})
        self.calls: list[int] = []

    def timeline(self, issue: int) -> list[dict]:
        self.calls.append(issue)
        if issue in self.errors:
            raise self.errors[issue]
        return list(self.events.get(issue, []))


def _c(number: int, created_hm: str, extra: tuple[str, ...] = (), open_blockers: tuple[int, ...] = ()) -> IssueSummary:
    return IssueSummary(number=number, created_at=_stamp(created_hm), labels=READY + extra,
                        open_blockers=open_blockers)


# -- priority_rank: pure ordering --------------------------------------------------------------------

def test_priority_class_is_priority_over_flow_fix_when_both_labels():
    assert priority_rank.priority_class(("ready-for-agent",)) is None
    assert priority_rank.priority_class(("ready-for-agent", "flow-fix")) == "flow-fix"
    assert priority_rank.priority_class(("ready-for-agent", "priority")) == "priority"
    assert priority_rank.priority_class(("flow-fix", "priority")) == "priority"


def test_priority_class_before_flow_fix_even_when_marked_later():
    p = priority_rank.rank_key("priority", _at("12:10"))
    f = priority_rank.rank_key("flow-fix", _at("11:00"))
    assert p < f


def test_inside_a_class_the_older_mark_comes_first():
    early = priority_rank.rank_key("flow-fix", _at("11:30"))
    late = priority_rank.rank_key("flow-fix", _at("12:00"))
    assert early < late


def test_both_labels_rank_as_priority_with_the_priority_label_time():
    cls = priority_rank.priority_class(("flow-fix", "priority"))
    both_at_1210 = priority_rank.rank_key(cls, _at("12:10"))
    priority_at_1210 = priority_rank.rank_key("priority", _at("12:10"))
    flow_at_1100 = priority_rank.rank_key("flow-fix", _at("11:00"))
    assert both_at_1210 == priority_at_1210
    assert both_at_1210 < flow_at_1100


def test_a_mark_that_is_unknown_sorts_last_in_its_class():
    known = priority_rank.rank_key("flow-fix", _at("23:59"))
    unknown = priority_rank.rank_key("flow-fix", None)
    assert known < unknown


# -- priority_rank: mark_time through an injected reader -----------------------------------------

def test_mark_time_is_the_newest_labeled_event_of_that_label():
    reader = FakeTimeline({7: [
        _labeled("flow-fix", "10:00"),
        {"event": "unlabeled", "label": {"name": "flow-fix"}, "created_at": _stamp("10:30")},
        _labeled("flow-fix", "11:30"),
        _labeled("priority", "13:00"),
    ]})
    assert priority_rank.mark_time(7, "flow-fix", reader) == _at("11:30")
    assert reader.calls == [7]


def test_mark_time_is_none_without_a_labeled_event_of_that_label():
    reader = FakeTimeline({7: [_labeled("priority", "13:00")]})
    assert priority_rank.mark_time(7, "flow-fix", reader) is None


# -- dispatcher: offer order and cap (row 1) ----------------------------------------------------

def test_row1_offer_is_p1_f1_f2_o1_with_cap_four(tmp_path):
    candidates = [
        _c(1, "08:00"),                   # O1
        _c(2, "09:00"),                   # O2
        _c(3, "10:00"),                   # O3
        _c(11, "11:00", ("flow-fix",)),   # F1, marked 11:30
        _c(12, "07:00", ("flow-fix",)),   # F2, marked 12:00
        _c(21, "12:00", ("priority",)),   # P1, marked 12:10
    ]
    reader = FakeTimeline({
        11: [_labeled("flow-fix", "11:30")],
        12: [_labeled("flow-fix", "12:00")],
        21: [_labeled("priority", "12:10")],
    })
    claude = FakeClaude()
    gh = FakeGh(candidates=candidates)
    d = _dispatcher(tmp_path, claude=claude, gh=gh, orch_offer_max=4)
    d.timeline_reader = reader

    d.tick()

    assert claude.started[0][0] == [21, 11, 12, 1]
    assert sorted(reader.calls) == [11, 12, 21]  # one timeline read per Priority ticket, none for the rest


def test_row1_without_priority_the_offer_stays_oldest_first(tmp_path):
    candidates = [_c(1, "08:00"), _c(2, "09:00"), _c(3, "10:00")]
    reader = FakeTimeline()
    d = _dispatcher(tmp_path, claude=FakeClaude(), gh=FakeGh(candidates=candidates), orch_offer_max=4)
    d.timeline_reader = reader

    assert [c.number for c in d._eligible_candidates(offering=True)] == [1, 2, 3]
    assert reader.calls == []


def test_a_priority_ticket_dropped_by_a_filter_costs_no_timeline_read(tmp_path):
    candidates = [
        _c(1, "08:00"),                                   # O1, offered
        _c(31, "07:00", ("flow-fix", "needs-info")),      # dropped: needs-info
        _c(32, "07:30", ("flow-fix", "parked")),          # dropped: parked
        _c(33, "07:40", ("flow-fix",), open_blockers=(99,)),  # dropped: natively blocked
    ]
    reader = FakeTimeline()
    d = _dispatcher(tmp_path, claude=FakeClaude(), gh=FakeGh(candidates=candidates), orch_offer_max=4)
    d.timeline_reader = reader

    assert [c.number for c in d._eligible_candidates(offering=True)] == [1]
    assert reader.calls == []


# -- dispatcher: a failed timeline read never breaks the tick (finding 1) ------------------------

@pytest.mark.parametrize("error", [
    RuntimeError("gh api repos/x/issues/21/timeline failed: HTTP 502"),
    json.JSONDecodeError("Extra data", "[][]", 2),  # `--paginate` with several pages: concatenated arrays
])
def test_a_failed_timeline_read_still_offers_and_ranks_the_failed_ticket_last_in_its_class(tmp_path, error):
    candidates = [
        _c(1, "08:00"),                       # O1
        _c(11, "11:00", ("flow-fix",)),       # F1, marked 11:30
        _c(21, "12:00", ("priority",)),       # P1: its timeline read raises
        _c(22, "12:30", ("priority",)),       # P2, marked 12:00
    ]
    reader = FakeTimeline({
        11: [_labeled("flow-fix", "11:30")],
        22: [_labeled("priority", "12:00")],
    }, errors={21: error})
    messages: list[str] = []
    claude = FakeClaude()
    d = _dispatcher(tmp_path, claude=claude, gh=FakeGh(candidates=candidates), orch_offer_max=4)
    d.log = messages.append
    d.timeline_reader = reader

    d.tick()  # must not raise

    assert claude.started[0][0] == [22, 21, 11, 1]  # P2 (known mark), P1 (unknown, last in class), F1, O1
    assert len([m for m in messages if m.startswith("priority mark of #21")]) == 1


def test_a_failed_read_is_logged_once_per_ticket_and_kind(tmp_path):
    candidates = [_c(21, "12:00", ("priority",)), _c(1, "08:00")]
    messages: list[str] = []
    d = _dispatcher(tmp_path, claude=FakeClaude(), gh=FakeGh(candidates=candidates), orch_offer_max=4)
    d.log = messages.append
    d.timeline_reader = FakeTimeline(errors={21: RuntimeError("boom")})

    d._eligible_candidates(offering=True)
    d._eligible_candidates(offering=True)

    assert len([m for m in messages if m.startswith("priority mark of #21")]) == 1


# -- dispatcher: admission (row 7) --------------------------------------------------------------

class _Hold:
    go = False
    reason = "load"

    def line(self) -> str:
        return "held by load"


def _always_hold(calls: list):
    def admission(previous_wait, waited_s: float = 0.0, hold_cap_s=None):
        calls.append(previous_wait)
        return _Hold()
    return admission


def test_row7_offer_with_a_priority_ticket_starts_without_calling_the_hook(tmp_path):
    calls: list = []
    claude = FakeClaude()
    d = _dispatcher(tmp_path, claude=claude, gh=FakeGh(candidates=[_c(1, "08:00"), _c(11, "11:00", ("flow-fix",))]),
                    orch_offer_max=4)
    d.timeline_reader = FakeTimeline({11: [_labeled("flow-fix", "11:30")]})
    d.admission = _always_hold(calls)

    d.tick()

    assert len(claude.started) == 1
    assert calls == []


def test_row7_offer_without_a_priority_ticket_calls_the_hook_as_today(tmp_path):
    calls: list = []
    claude = FakeClaude()
    d = _dispatcher(tmp_path, claude=claude, gh=FakeGh(candidates=[_c(1, "08:00"), _c(2, "09:00")]),
                    orch_offer_max=4)
    d.timeline_reader = FakeTimeline()
    d.admission = _always_hold(calls)

    d.tick()

    assert claude.started == []
    assert len(calls) == 1
