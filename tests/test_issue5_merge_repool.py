"""#5: a ticket parked on a merge goes back into the pool once the merge landed (or failed) and the parking
session ended, whatever the order of the park comment, the `<!-- merge result -->` comment and the
finishing note; a ticket whose merge still runs is not offered; the finishing note alone parks nothing.
Fake dispatcher, claude and gh of the existing parked tests; the real `GhCli.parking_mark` reads the canned
comments."""

from __future__ import annotations

import json

from tools.agentops.dispatch import IssueSummary

from tests.test_agentops_dispatch import FakeClaude, FakeClock, READY, _dispatcher, _issue
from tests.test_agentops_dispatch_parked import RealMarkGh
from tests._area_rule import _no_area_rule  # noqa: F401

T0 = "2026-10-16T09:00:00Z"
P = "2026-10-16T10:00:00Z"  # the park comment
R = "2026-10-16T10:05:00Z"  # the merge result comment
F = "2026-10-16T10:10:00Z"  # the finishing note
LANDED = ("<!-- merge result -->\nMerge of i5-u1 landed on main 0123456789abcdef after 1 min in line and a "
          "suite of 2 min, 0 failed. Result {file}; suite output -.\n")
NOT_LANDED = ("<!-- merge result -->\nMerge of i5-u1 did not land (red-on-branch) after 1 min in line and a "
              "suite of 2 min, 3 failed. Result {file}; suite output -.\n")


class UnlabelledGh(RealMarkGh):
    """RealMarkGh that also lists open issues without `ready-for-agent` (the relabel read)."""

    unlabelled: list[dict] = []

    def unlabelled_open_issues(self) -> list[dict]:
        return list(self.unlabelled)


def _c(at: str, body: str) -> dict:
    return {"createdAt": at, "body": body}


def _finishing(sid: str) -> str:
    # the text `finishing_note` posts for the session
    return (f"ORCH {sid[:8]}, finishing: still working this issue, takes nothing new; "
            "if the session ends, the next one continues from the last checkpoint above\n"
            "<!-- parked-on-run: this session is finishing; the session that claims the issue continues "
            "from the last checkpoint -->\n"
            f"Session id: {sid}")


def _summary(number: int, comments: list[dict]) -> IssueSummary:
    last = comments[-1]["createdAt"]
    return IssueSummary(number=number, created_at="2026-10-01T00:00:00Z", labels=READY,
                        latest_comment_at=last, updated_at=last,
                        comments=tuple((c["createdAt"], c["body"]) for c in comments))


def _ended(tmp_path, build, *, pending=None, gh_cls=RealMarkGh):
    """Session S1 claims #1, parks it (comments `build(session id)` on the issue) and ends; the dispatcher
    read the end and set the park mark. Returns (claude, gh, dispatcher, log, comments, clock)."""
    claude = FakeClaude()
    gh = gh_cls([], T0, candidates=[_issue(1)])
    clock = FakeClock()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)
    d.has_pending_job = pending
    log: list[str] = []
    d.log = log.append
    d.tick()
    sid = claude.list_sessions()[0]["id"]
    comments = build(sid)
    gh._view = json.dumps({"comments": comments, "updatedAt": comments[-1]["createdAt"]})
    gh.claim(1)
    d.tick()
    d.sessions[0].let_go.add(1)
    claude.set_state(sid, state="done", status="idle")
    assert d.tick().outcomes == {1: "parked"}
    gh._candidates = [_summary(1, comments)]
    return claude, gh, d, log, comments, clock


def _park_text(sid: str, result_file) -> str:
    return f"ORCH {sid}: parked on merge {result_file}"


def _landed_lines(log: list[str], rc: str) -> list[str]:
    return [m for m in log if m == f"#1: its merge landed (rc {rc}) and its session ended; back in the pool"]


def _offered_after_a_tick(claude, d) -> None:
    started = len(claude.started)
    d.tick()
    assert len(claude.started) == started + 1
    assert claude.started[-1][0] == [1]


def test_order_a_park_then_result_then_the_session_ends(tmp_path):
    f = tmp_path / "r1.json"
    claude, gh, d, log, _, _ = _ended(tmp_path, lambda sid: [
        _c(P, _park_text(sid, f)), _c(R, LANDED.format(file=f)), _c(F, _finishing(sid))])
    _offered_after_a_tick(claude, d)
    assert _landed_lines(log, "0")


def test_order_b_result_before_the_park_comment(tmp_path):
    f = tmp_path / "r1.json"
    claude, gh, d, log, _, _ = _ended(tmp_path, lambda sid: [
        _c(P, LANDED.format(file=f)), _c(R, _park_text(sid, f)), _c(F, _finishing(sid))])
    _offered_after_a_tick(claude, d)
    assert _landed_lines(log, "0")


def test_order_c_no_result_comment_only_the_result_file_with_rc_0(tmp_path):
    f = tmp_path / "r1.json"
    f.write_text(json.dumps({"kind": "merge", "rc": 0}), encoding="utf-8")
    claude, gh, d, log, _, _ = _ended(tmp_path, lambda sid: [
        _c(P, _park_text(sid, f)), _c(F, _finishing(sid))])
    _offered_after_a_tick(claude, d)
    assert _landed_lines(log, "0")


def test_a_red_merge_rc_1_is_re_pooled_the_same_way(tmp_path):
    f = tmp_path / "r1.json"
    f.write_text(json.dumps({"kind": "merge", "rc": 1, "reason": "red-on-branch"}), encoding="utf-8")
    claude, gh, d, log, _, _ = _ended(tmp_path, lambda sid: [
        _c(P, _park_text(sid, f)), _c(R, NOT_LANDED.format(file=f)), _c(F, _finishing(sid))])
    _offered_after_a_tick(claude, d)
    assert _landed_lines(log, "1")


def test_no_pending_job_command_the_landed_ticket_is_offered_not_kept_out(tmp_path):
    """Row 3: `has_pending_job=None` (no `pending_job_command` in the repo) read every parked ticket as
    pending, so "nothing newer than its checkpoint" kept the landed one out for good."""
    f = tmp_path / "r1.json"
    claude, gh, d, log, _, _ = _ended(tmp_path, lambda sid: [
        _c(P, _park_text(sid, f)), _c(R, LANDED.format(file=f)), _c(F, _finishing(sid))], pending=None)
    assert d.has_pending_job is None
    _offered_after_a_tick(claude, d)
    assert not any("kept out of the pool" in m for m in log)


def test_ticket_73_merge_still_running_is_not_offered_until_the_result_lands(tmp_path):
    f = tmp_path / "r1.json"  # not written: the merge still runs
    claude, gh, d, log, comments, _ = _ended(tmp_path, lambda sid: [
        _c(P, _park_text(sid, f)), _c(F, _finishing(sid))], pending=lambda n: False)
    started = len(claude.started)
    d.tick()
    d.tick()
    assert len(claude.started) == started
    assert not _landed_lines(log, "0")
    comments.append(_c("2026-10-16T10:20:00Z", LANDED.format(file=f)))
    gh._candidates = [_summary(1, comments)]
    _offered_after_a_tick(claude, d)
    assert _landed_lines(log, "0")


def test_ticket_80_checkpoint_naming_no_event_then_the_finishing_note_is_offered(tmp_path):
    claude, gh, d, log, _, _ = _ended(tmp_path, lambda sid: [
        _c(P, f"Checkpoint 3 (ORCH {sid}, #1). Parked after the queued merges land."),
        _c(R, LANDED.format(file="var/agentops/results/q.json")), _c(F, _finishing(sid))])
    _offered_after_a_tick(claude, d)


def test_a_landed_merge_puts_ready_for_agent_back_when_it_is_absent(tmp_path):
    f = tmp_path / "r1.json"
    claude, gh, d, log, comments, clock = _ended(tmp_path, lambda sid: [
        _c(P, _park_text(sid, f)), _c(R, LANDED.format(file=f)), _c(F, _finishing(sid))],
        gh_cls=UnlabelledGh)
    gh._candidates = []
    gh.unlabelled = [{"number": 1, "labels": [], "comments": comments}]
    clock.advance(120)
    started = len(claude.started)
    d.tick()
    assert (1, "ready-for-agent") in gh.added_labels
    assert _landed_lines(log, "0")
    assert len(claude.started) == started  # not yet a candidate this tick
    gh._candidates = [_summary(1, comments)]
    _offered_after_a_tick(claude, d)


def test_order_b_with_no_pending_job_command(tmp_path):
    f = tmp_path / "r1.json"
    claude, gh, d, log, _, _ = _ended(tmp_path, lambda sid: [
        _c(P, LANDED.format(file=f)), _c(R, _park_text(sid, f)), _c(F, _finishing(sid))], pending=None)
    assert d.has_pending_job is None
    _offered_after_a_tick(claude, d)
    assert _landed_lines(log, "0")


def test_order_c_with_no_pending_job_command(tmp_path):
    f = tmp_path / "r1.json"
    f.write_text(json.dumps({"kind": "merge", "rc": 0}), encoding="utf-8")
    claude, gh, d, log, _, _ = _ended(tmp_path, lambda sid: [
        _c(P, _park_text(sid, f)), _c(F, _finishing(sid))], pending=None)
    assert d.has_pending_job is None
    _offered_after_a_tick(claude, d)
    assert _landed_lines(log, "0")


def test_a_park_by_session_a_then_session_b_ends_with_only_a_finishing_note(tmp_path):
    """The park reason is the ticket's newest event-naming checkpoint, whoever wrote it."""
    f = tmp_path / "r1.json"  # not written: the merge still runs
    claude, gh, d, log, comments, _ = _ended(tmp_path, lambda sid: [
        _c(P, _park_text("a1b2c3d4-other-session", f)), _c(F, _finishing(sid))], pending=lambda n: False)
    started = len(claude.started)
    d.tick()
    d.tick()
    assert len(claude.started) == started
    comments.append(_c("2026-10-16T10:20:00Z", LANDED.format(file=f)))
    gh._candidates = [_summary(1, comments)]
    _offered_after_a_tick(claude, d)
    assert _landed_lines(log, "0")


def test_a_live_session_that_parked_on_a_landed_merge_is_not_re_pooled(tmp_path):
    from types import SimpleNamespace
    f = tmp_path / "r1.json"
    f.write_text(json.dumps({"kind": "merge", "rc": 0}), encoding="utf-8")
    claude, gh, d, log, comments, _ = _ended(tmp_path, lambda sid: [
        _c(P, _park_text(sid, f)), _c(R, LANDED.format(file=f)), _c(F, _finishing(sid))])
    candidate = _summary(1, comments)
    d.sessions = [SimpleNamespace(held={1})]  # a live session still holds #1: #1072 wakes it
    assert d._drop_still_parked([candidate], offering=False) == []
    assert 1 in d._park_marks and not _landed_lines(log, "0")
    d.sessions = []  # once it is gone the same sweep re-pools it
    assert d._drop_still_parked([candidate], offering=False) == [candidate]
    assert _landed_lines(log, "0")


def test_names_event_reads_the_parked_on_run_marker_and_never_the_finishing_note():
    from tools.agentops.dispatch import is_finishing_note, names_event
    note = _finishing("sess-12345678")
    assert is_finishing_note(note) and not names_event(note)
    assert names_event("<!-- parked-on-run: merge of i372, result file var/r/a.json -->\nORCH sess-1 waits")
    assert not names_event(LANDED.format(file="x.json"))


def test_an_unreadable_result_file_falls_through_to_the_result_comment(tmp_path):
    f = tmp_path / "r1.json"
    f.write_text("{not json", encoding="utf-8")
    claude, gh, d, log, _, _ = _ended(tmp_path, lambda sid: [
        _c(P, _park_text(sid, f)), _c(R, NOT_LANDED.format(file=f)), _c(F, _finishing(sid))])
    _offered_after_a_tick(claude, d)
    assert _landed_lines(log, "1")
