"""#209 unit 5: a ticket with 3 failed attempts in a row gets ONE background `unblocker` session, never an ORCH offer.

Through `Dispatcher` with its fakes (`FakeClaude.start_unblocker`, `FakeGh`): no real `claude`, no real `gh`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh
from tests._area_rule import _no_area_rule  # noqa: F401
from tools.agentops.dispatch import REPO, REPO_NAME, Dispatcher, IssueSummary, READY_LABEL
from tools.agentops.loop_stop import loop_status

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
SB, OA, BA, MR = "<!-- send-back -->", "<!-- owner-answer -->", "<!-- brain-answer -->", "<!-- merge result -->"
RED = f"{MR}\nMerge of orch-195 did not land (red-suite) after 40 min in line and a suite of 36 min, 1 failed."
LANDED = f"{MR}\nMerge of orch-195 landed on main d0b7d25c6afc after 77 min in line and a suite of 47 min, 0 failed."
STOP = "## Loop stop\nthree failed attempts."


def _c(body: str, hour: int, minute: int = 0) -> dict:
    return {"createdAt": f"2026-10-19T{hour:02d}:{minute:02d}:00Z", "body": body}


def _sb(hour: int, minute: int = 0) -> dict:
    return _c(f"{SB}\nnot accepted", hour, minute)


def _issue(number: int, comments: list[dict]) -> IssueSummary:
    return IssueSummary(number=number, created_at=f"2026-10-01T00:00:0{number % 10}Z", labels=(READY_LABEL,),
                        comments=tuple((c["createdAt"], c["body"]) for c in comments))


def _new_dispatcher(tmp_path: Path, claude: FakeClaude, gh: FakeGh, *, store: bool = True) -> Dispatcher:
    d = Dispatcher(claude=claude, gh=gh, max_hours=4.0, events_file=tmp_path / "events.jsonl", orch_offer_max=99,
                   state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects", clock=FakeClock(),
                   unblocker_start_file=tmp_path / "unblocker_starts.json" if store else None)
    d.timestamp = lambda: NOW
    d.log_lines = []
    d.log = d.log_lines.append
    return d


def _dispatch(tmp_path: Path, by_issue: dict[int, list[dict]]):
    claude = FakeClaude()
    gh = FakeGh(candidates=[_issue(n, c) for n, c in by_issue.items()])
    return claude, gh, _new_dispatcher(tmp_path, claude, gh)


# -- row 1 ------------------------------------------------------------------------------------------

def test_three_failed_attempts_mixed_start_exactly_one_unblocker_with_its_argv(tmp_path: Path):
    claude, _, d = _dispatch(tmp_path, {195: [_sb(6), _c(RED, 7), _sb(8)]})
    d.tick()
    assert [n for n, _ in claude.unblockers] == [195]
    argv = claude.unblockers[0][1]
    assert argv[argv.index("--agent") + 1] == "unblocker"
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--name") + 1] == f"unblocker {REPO_NAME} #195"
    prompt = argv[-1]
    assert "#195" in prompt and f"{REPO}#195" in prompt and ".claude/agents/unblocker.md" in prompt
    assert claude.started == []  # a stopped ticket is never offered to ORCH
    assert "started unblocker for #195 (3 failed attempts)" in d.log_lines


def test_two_in_a_row_or_three_with_a_landed_merge_between_start_nothing(tmp_path: Path):
    claude, _, d = _dispatch(tmp_path, {1: [_sb(6), _c(RED, 7)], 2: [_sb(6), _sb(7), _c(LANDED, 8), _sb(9), _sb(10)]})
    d.tick()
    assert claude.unblockers == []
    assert [offered for offered, _ in claude.started] == [[1, 2]]  # both are ordinary tickets for ORCH


def test_one_old_send_back_starts_nothing_and_the_ticket_is_offered_normally(tmp_path: Path):
    claude, _, d = _dispatch(tmp_path, {5: [_sb(1)]})  # 11 h old at NOW
    d.tick()
    assert claude.unblockers == []
    assert [offered for offered, _ in claude.started] == [[5]]
    assert "loop-stopped" not in " ".join(claude.started[0][1])


def test_second_pass_and_a_new_dispatcher_over_the_same_store_start_none(tmp_path: Path):
    by_issue = {195: [_sb(6), _c(RED, 7), _sb(8)]}
    claude, gh, d = _dispatch(tmp_path, by_issue)
    d.tick()
    d.tick()
    assert len(claude.unblockers) == 1
    again = _new_dispatcher(tmp_path, claude, gh)  # a restart: a new object over the same store file
    again.tick()
    again.tick()
    assert len(claude.unblockers) == 1
    assert (tmp_path / "unblocker_starts.json").is_file()


def test_a_newer_failed_attempt_after_a_brain_answer_is_a_new_trigger(tmp_path: Path):
    old = [_sb(6), _c(RED, 7), _sb(8)]
    claude, gh, d = _dispatch(tmp_path, {195: old})
    d.tick()
    gh._candidates = [_issue(195, old + [_c(f"{BA}\nthe way", 9), _sb(10), _sb(11), _c(RED, 11, 30)])]
    d.tick()
    assert [n for n, _ in claude.unblockers] == [195, 195]  # the second streak: its newest failed attempt is another key


def test_the_orch_start_gates_closed_do_not_hold_the_unblocker(tmp_path: Path):
    claude, gh, d = _dispatch(tmp_path, {1: [], 195: [_sb(6), _c(RED, 7), _sb(8)]})
    d.admission = lambda wait, **kw: SimpleNamespace(go=False, reason="busy", line=lambda: "busy")
    d.tick()
    assert claude.started == []  # ORCH is held ...
    assert [n for n, _ in claude.unblockers] == [195]  # ... the unblocker is not


def test_a_live_orch_that_is_not_finishing_does_not_hold_the_unblocker(tmp_path: Path):
    claude, gh, d = _dispatch(tmp_path, {1: []})
    d.tick()  # ORCH started on #1, not finishing
    gh.claim(1)
    gh._candidates = [_issue(195, [_sb(6), _c(RED, 7), _sb(8)])]
    d.tick()
    assert [n for n, _ in claude.unblockers] == [195]
    assert [offered for offered, _ in claude.started] == [[1]]


def test_a_refused_unblocker_start_is_tried_again_next_pass_and_recorded_only_once_it_goes_through(tmp_path: Path):
    claude, _, d = _dispatch(tmp_path, {195: [_sb(6), _c(RED, 7), _sb(8)]})
    real = claude.start_unblocker
    calls: list[int] = []

    def refuse_once(number: int):
        calls.append(number)
        if len(calls) == 1:
            raise RuntimeError("claude --bg failed: boom")
        return real(number)

    claude.start_unblocker = refuse_once
    d.tick()
    assert claude.unblockers == []
    d.tick()
    assert [n for n, _ in claude.unblockers] == [195]
    d.tick()
    assert len(claude.unblockers) == 1


def test_the_unblocker_ending_is_no_orch_ending(tmp_path: Path):
    claude, gh, d = _dispatch(tmp_path, {195: [_sb(6), _c(RED, 7), _sb(8)]})
    d.tick()
    assert d.sessions == []  # kept out of the ORCH list
    session = claude.list_sessions()[0]["id"]
    claude.set_state(session, state="stopped", status="idle")  # the unblocker ends
    report = d.tick()
    assert report.ended is None and d.sessions == []
    assert not [m for m in d.log_lines if "ended" in m or "checkpoint" in m or "orphan" in m]
    assert gh.removed_assignees == [] and claude.stopped == []


# -- row 3: after the unblocker's answer the ticket is an ordinary ready ticket ----------------------

def test_after_the_brain_answer_the_ready_ticket_is_offered_to_orch(tmp_path: Path):
    three = [_sb(6), _sb(7), _sb(8)]
    assert loop_status(three).stopped
    after = three + [_c(f"{BA}\n## Loop stop\nthe way", 9)]
    assert loop_status(after).stopped is False
    claude, _, d = _dispatch(tmp_path, {195: after})
    d.tick()
    assert claude.unblockers == []
    assert [offered for offered, _ in claude.started] == [[195]]
    assert "loop-stopped" not in " ".join(claude.started[0][1])


# -- row 6: the replay of #195's comment shape ------------------------------------------------------

def test_replay_of_195_prints_the_count_each_pass_and_starts_at_the_third_failed_attempt(tmp_path: Path):
    # the real shape: a send-back (T), the `## Loop stop` comment, a red merge result, the brain's answer
    story = [("send-back", _sb(1, 14)), ("loop stop comment", _c(STOP, 1, 15)),
             ("red merge result", _c(RED, 1, 37)), ("a 3rd failed attempt (what the new rule waits for)", _sb(2, 10)),
             ("brain-answer", _c(f"{BA}\nthe way", 8, 19))]
    claude, gh, d = _dispatch(tmp_path, {195: []})
    seen: list[tuple[str, int, int]] = []
    comments: list[dict] = []
    for label, comment in story:
        comments.append(comment)
        gh._candidates = [_issue(195, list(comments))]
        d.tick()
        seen.append((label, loop_status(comments).count, len(claude.unblockers)))
    for row in seen:
        print(row)  # the replay prints the count either way
    assert seen == [("send-back", 1, 0), ("loop stop comment", 1, 0), ("red merge result", 2, 0),
                    ("a 3rd failed attempt (what the new rule waits for)", 3, 1), ("brain-answer", 0, 1)]
