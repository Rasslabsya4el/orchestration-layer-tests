"""tools/agentops/dispatch.py (#1074 unit 4): an open issue labelled `parked` waits on a named event, so
the dispatcher does not offer it, like a natively blocked one. The label is orthogonal to the role
labels: a parked ticket keeps `ready-for-agent`, and only the `parked` label keeps it out of the queue."""

from __future__ import annotations

from pathlib import Path

from tests.test_agentops_dispatch import FakeClaude, FakeGh, READY, _dispatcher, _issue
from tests._area_rule import _no_area_rule  # noqa: F401

PARKED = ("ready-for-agent", "parked")


def test_1_a_ready_issue_labelled_parked_is_not_offered(tmp_path: Path):
    gh = FakeGh(candidates=[_issue(7, labels=PARKED)])
    d = _dispatcher(tmp_path, claude=FakeClaude(), gh=gh)

    assert d._eligible_candidates(offering=True) == []


def test_2_a_ready_issue_without_parked_is_offered(tmp_path: Path):
    gh = FakeGh(candidates=[_issue(7, labels=READY), _issue(8, labels=PARKED)])
    d = _dispatcher(tmp_path, claude=FakeClaude(), gh=gh)

    assert [c.number for c in d._eligible_candidates(offering=True)] == [7]


def test_3_a_tick_with_only_a_parked_issue_starts_no_session(tmp_path: Path):
    claude = FakeClaude()
    gh = FakeGh(candidates=[_issue(7, labels=PARKED)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh)

    d.tick()

    assert claude.started == []
