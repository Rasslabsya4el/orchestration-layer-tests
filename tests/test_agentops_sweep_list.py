"""`sweep list` (issue #1102, unit 1): the pure rows of the sweep over fake issue records. No network.

Acceptance row 1: eight fake issues, A..H, numbered 1..8 in this file.
  A needs-triage, updated 3 days ago            -> due
  B no state label, 1 day                       -> not due
  C parked, every native blocker closed, 1 day  -> due (its event came; the age is not asked)
  D parked, an open blocker, 10 days            -> waiting on the blocker
  E ready-for-agent                             -> listed with its group, never due (#155)
  F parent, 3/3 children closed                 -> due
  G map, its newest child moved 3 days ago      -> due, row carries "/wayfinder 7"
  H ready-for-human, 5 days                     -> due
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from tools.agentops import sweep as S

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _issue(n, labels=(), days=0.0, blockers=(), total=0, completed=0, body=""):
    """A `gh issue list --json` record. `blockers`: (number, state) pairs of native blockers; `body`: the issue body."""
    return {
        "number": n, "title": f"title {n}", "state": "OPEN", "body": body,
        "labels": [{"name": x} for x in labels], "updatedAt": _iso(days),
        "blockedBy": {"nodes": [{"number": b, "state": s} for b, s in blockers], "totalCount": len(blockers)},
        "subIssuesSummary": {"total": total, "completed": completed},
    }


def _child(n, state, days):
    """A normalized child record (the gateway turns REST `sub_issues` items into this shape)."""
    return {"number": n, "state": state, "updatedAt": _iso(days)}


def _fixture():
    issues = [
        _issue(1, ["needs-triage"], days=3),
        _issue(2, [], days=1),
        _issue(3, ["parked"], days=1, blockers=[(90, "CLOSED")]),
        _issue(4, ["parked"], days=10, blockers=[(91, "OPEN")]),
        _issue(5, ["ready-for-agent"], days=5),
        _issue(6, [], days=1, total=3, completed=3),
        _issue(7, ["wayfinder:map"], days=0, total=2, completed=1),
        _issue(8, ["ready-for-human"], days=5),
    ]
    children = {7: [_child(71, "CLOSED", 3), _child(72, "CLOSED", 9)]}
    return issues, children


def test_acceptance_eight_fake_issues_due_waiting_and_absent():
    issues, children = _fixture()
    rows = {r.number: r for r in S.build_rows(issues, children, NOW)}
    due = sorted(r.number for r in S.due_rows(issues, children, NOW))

    assert due == [1, 3, 6, 7, 8]          # A, C, F, G, H
    assert 2 in rows and 4 in rows         # B and D are listed, not due
    assert 2 not in due and 4 not in due
    assert rows[5].labels == ("ready-for-agent",) and rows[5].due is False   # E is listed, never due (#155)
    assert rows[7].copy == "/wayfinder 7"  # G's row carries the copy line
    assert rows[4].waiting_on == (91,)     # D waits on its open blocker
    assert rows[1].age_days == 3 and rows[8].age_days == 5


def test_map_age_is_its_newest_child_and_quiet_map_is_not_due():
    quiet = [_issue(9, ["wayfinder:map"], days=10, total=1, completed=0)]
    assert [r.due for r in S.build_rows(quiet, {9: [_child(91, "OPEN", 1)]}, NOW)] == [False]
    assert S.build_rows(quiet, {}, NOW)[0].age_days == 10  # no child: the map's own update


def test_format_rows_copy_line_and_count():
    issues, children = _fixture()
    text = S.format_rows(S.build_rows(issues, children, NOW)).splitlines()
    # 8 rows (ready-for-agent listed), 5 due; then the monitor's group counts in PROGRESS_LINES order (#155)
    assert text[-7:] == ["rows 8, due 5", "needs-triage 1", "ready-for-agent 1", "ready-for-human 1",
                         "parked 2", "folder 1", "no label 2"]
    g_row = [line for line in text if line.startswith("#7 ")]
    assert g_row and "/wayfinder 7" in g_row[0] and "map 1/2 closed" in g_row[0]
    assert any("waiting on #91" in line for line in text if line.startswith("#4 "))


class FakeRunner:
    def __init__(self, issue_json, sub_json=None, code=0, err=""):
        self.issue_json, self.sub_json, self.code, self.err, self.calls = issue_json, sub_json or "", code, err, []

    def __call__(self, args):
        self.calls.append(list(args))
        if self.code:
            return SimpleNamespace(returncode=self.code, stdout="", stderr=self.err)
        out = self.issue_json if args[:2] == ["issue", "list"] else self.sub_json
        return SimpleNamespace(returncode=0, stdout=out, stderr="")


def test_gateway_reads_open_issues_and_map_children_only():
    issues, _ = _fixture()
    sub = json.dumps([{"number": 71, "state": "closed", "updated_at": _iso(3)}])
    runner = FakeRunner(json.dumps(issues), sub)
    loaded, children = S.GhSweep("o/r", runner).load()
    assert len(loaded) == 8
    assert [c[:2] for c in runner.calls] == [["issue", "list"], ["api", "repos/o/r/issues/7/sub_issues?per_page=100"]]
    assert children == {7: [{"number": 71, "state": "CLOSED", "updatedAt": _iso(3)}]}


def test_gateway_failure_raises_one_line_never_a_false_empty():
    import pytest
    with pytest.raises(S.SweepError, match="failed: HTTP 502"):
        S.GhSweep("o/r", FakeRunner("", code=1, err="HTTP 502 Bad Gateway")).load()


def test_run_sweep_prints_rows_and_returns_zero():
    import argparse
    import io
    from contextlib import redirect_stdout
    issues, _ = _fixture()
    sub = json.dumps([{"number": 71, "state": "closed", "updated_at": _iso(3)}])
    gh = S.GhSweep("o/r", FakeRunner(json.dumps(issues), sub))
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = S.run_sweep(argparse.Namespace(sweep_command="list"), gh=gh, now=NOW)
    assert code == 0
    assert buf.getvalue().splitlines()[-7:] == ["rows 8, due 5", "needs-triage 1", "ready-for-agent 1",
                                                "ready-for-human 1", "parked 2", "folder 1", "no label 2"]


def test_parked_with_no_native_blocker_follows_the_age_rule():
    """The event of such a ticket is text only: a ticket the sweep just parked is not due again the same day."""
    issues = [_issue(1, ["parked"], days=1), _issue(2, ["parked"], days=3)]
    rows = {r.number: r for r in S.build_rows(issues, {}, NOW)}
    assert not rows[1].due and rows[2].due


FUTURE = "2026-10-25 00:00"   # 10 days after NOW: not yet reached
PASSED = "2026-10-10 12:00"   # 5 days before NOW: passed
CORE_LINE = "Parked: 0 h after core ab7ea95b reaches some-owner/some-repo"   # a core form: text, no date


def test_parked_date_holds_the_ticket_until_it_passes():
    """Issue 187: a `Parked:` date ahead holds the ticket not due, whatever its blockers; a passed date with its
    blockers closed is due; a parked ticket with closed blockers and no `Parked:` line is due, as before."""
    issues = [
        _issue(1, ["parked"], days=1, blockers=[(90, "CLOSED")], body=f"Parked: until {FUTURE}"),
        _issue(2, ["parked"], days=1, blockers=[(91, "CLOSED")], body=f"Parked: until {PASSED}"),
        _issue(3, ["parked"], days=1, blockers=[(92, "CLOSED")]),
    ]
    rows = {r.number: r for r in S.build_rows(issues, {}, NOW)}
    assert not rows[1].due
    assert rows[2].due
    assert rows[3].due


def test_parked_line_forms_by_event_kind():
    """Issue 187: a text event with no date falls back to the age rule once its blockers are closed; a
    `until #N closes` line is a blocker event (closed blockers: due); a future date with no blocker is not due;
    the core form is a text event too (age rule)."""
    issues = [
        _issue(1, ["parked"], days=1, blockers=[(93, "CLOSED")], body="Parked: waiting for the owner's answer"),
        _issue(2, ["parked"], days=1, blockers=[(90, "CLOSED")], body="Parked: until #90 closes"),
        _issue(3, ["parked"], days=10, body=f"Parked: until {FUTURE}"),
        _issue(4, ["parked"], days=3, blockers=[(94, "CLOSED")], body="Parked: waiting for the owner's answer"),
        _issue(5, ["parked"], days=1, blockers=[(95, "CLOSED")], body=CORE_LINE),
        _issue(6, ["parked"], days=3, blockers=[(96, "CLOSED")], body=CORE_LINE),
    ]
    rows = {r.number: r for r in S.build_rows(issues, {}, NOW)}
    assert not rows[1].due      # free text, closed blocker, 1 day < QUIET_DAYS
    assert rows[2].due          # blocker event, closed blocker
    assert not rows[3].due      # future date, no blocker, 10 days old: still held
    assert rows[4].due          # free text, closed blocker, 3 days >= QUIET_DAYS: the age rule
    assert not rows[5].due      # core form, closed blocker, 1 day < QUIET_DAYS
    assert rows[6].due          # core form, closed blocker, 3 days >= QUIET_DAYS


def test_parked_date_after_a_text_event():
    """Issue 187, the #177 row: a date after the words of the event holds the ticket as a date alone does."""
    line = "Parked: the first full week of purpose tags ends, {}"
    issues = [
        _issue(7, ["parked"], days=1, blockers=[(97, "CLOSED")], body=line.format("2026-10-25 00:00")),
        _issue(8, ["parked"], days=1, blockers=[(98, "CLOSED")], body=line.format("2026-10-01 00:00")),
    ]
    rows = {r.number: r for r in S.build_rows(issues, {}, NOW)}
    assert not rows[7].due      # the date is ahead, closed blocker
    assert rows[8].due          # the date passed, closed blocker
