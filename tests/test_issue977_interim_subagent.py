"""#977: (a) a subagent whose LATEST task notification is interim (status `completed` plus the harness
note that it stopped with background work of its own still running) counts as running, for at most
`INTERIM_SUBAGENT_CAP_SECONDS` from that notification (rows 1-4); (b) at a session's end a comment that
names no ORCH session does not hide the session's own progress checkpoint (rows 5-6).

The notification text of row 4 is the real one: ORCH 74765d0c's transcript lines 851 and 861 (the
`a8a8e0bee17ceca19` worker of #970), cut to the record fields the matcher reads. Rows 5-6 replay the real
comment thread of #970 through the real `GhCli` readers, with a fake `gh` runner."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.agentops.dispatch import (
    FINISHING_IDLE_END_SECONDS, GhCli, INTERIM_SUBAGENT_CAP_SECONDS, running_subagent_ids,
)
from tools.agentops.dispatch import mark_finishing

from tests.test_agentops_dispatch import FakeClaude, FakeGh, _events, _issue
from tests.test_agentops_dispatch_core_issue import FakeCoreGh
from tests.test_agentops_dispatch_core_issue import _dispatcher as _core_dispatcher
from tests.test_agentops_dispatch_finishing import FakeTimestamp, _dispatcher
from tests._area_rule import _no_area_rule  # noqa: F401

#: the real note, parent line 851 / 861 (the same words in both).
REAL_NOTE = ("This agent stopped with background work of its own still running. It may resume on its own "
             "when that work completes or reports, and the same task-id notifies again if it does; the "
             "result below may be interim.")
REAL_TASK = "a8a8e0bee17ceca19"
NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)


def _stamp(at: datetime) -> str:
    return at.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _notification(task: str, *, interim: bool, at: datetime, result: str = "still running") -> str:
    """One parent-transcript record line in the shape of the real lines 851 / 861."""

    note = f"<note>{REAL_NOTE}</note>\n" if interim else ""
    text = (f"<task-notification>\n<task-id>{task}</task-id>\n"
            f"<output-file>C:\\Temp\\{task}.output</output-file>\n<status>completed</status>\n"
            f"<summary>Agent \"#970 merge drops not-planned ticket\" finished</summary>\n{note}"
            f"<result>{result}</result>\n<usage><tool_uses>42</tool_uses></usage>\n</task-notification>")
    return json.dumps({"type": "user", "message": {"role": "user", "content": text},
                       "timestamp": _stamp(at), "sessionKind": "bg"})


def _parent(projects: Path, session_uuid: str, lines: list[str], *, agents: tuple[str, ...] = (REAL_TASK,)) -> None:
    projects.mkdir(parents=True, exist_ok=True)
    (projects / f"{session_uuid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    sub = projects / session_uuid / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    for agent in agents:
        (sub / f"agent-{agent}.jsonl").write_text("{}\n", encoding="utf-8")


# -- rows 1-4: running_subagent_ids ------------------------------------------------------------

def test_row1_interim_notification_counts_as_running_10_min_later(tmp_path: Path):
    t = NOW - timedelta(minutes=10)
    _parent(tmp_path, "u", [_notification(REAL_TASK, interim=True, at=t)])

    assert running_subagent_ids(tmp_path, "u", now=t + timedelta(minutes=10)) == {REAL_TASK}


def _finishing_session(tmp_path: Path, parent_lines: list[str]):
    claude, gh = FakeClaude(), FakeGh(candidates=[_issue(1, 1)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh, timestamp=FakeTimestamp(NOW))
    d.tick()
    s1 = claude.list_sessions()[0]
    gh.claim(1)
    d.tick()  # S1 holds #1, working / busy
    _parent(tmp_path / "projects", s1["sessionId"], parent_lines)
    mark_finishing(s1["id"], finishing_dir=tmp_path / "finishing")
    gh._candidates = []
    return claude, d, s1["id"]


def test_row1_the_finishing_idle_end_does_not_fire_with_an_interim_subagent(tmp_path: Path):
    # the last record is the notification, 11 min old: past FINISHING_IDLE_END_SECONDS (10 min), inside the cap
    t = NOW - timedelta(minutes=11)
    assert 11 * 60 > FINISHING_IDLE_END_SECONDS
    claude, d, sid = _finishing_session(tmp_path, [_notification(REAL_TASK, interim=True, at=t)])

    d.tick()

    assert sid not in d.ended_sessions and claude.stopped == []
    assert not [e for e in _events(tmp_path) if e["kind"] == "orch_session_end" and e["session"] == sid]


def test_row2_a_later_notification_without_the_note_ends_it(tmp_path: Path):
    first = NOW - timedelta(minutes=13)
    final = NOW - timedelta(minutes=11)
    lines = [_notification(REAL_TASK, interim=True, at=first),
             _notification(REAL_TASK, interim=False, at=final, result="done")]
    _parent(tmp_path / "p", "u", lines)
    assert running_subagent_ids(tmp_path / "p", "u", now=final + timedelta(minutes=1)) == set()

    claude, d, sid = _finishing_session(tmp_path / "d", lines)
    d.tick()

    assert sid in d.ended_sessions and claude.stopped == [sid]
    assert [e for e in _events(tmp_path / "d") if e["kind"] == "orch_session_end" and e["session"] == sid]


def test_row3_past_the_30_min_cap_the_interim_subagent_is_over(tmp_path: Path):
    assert INTERIM_SUBAGENT_CAP_SECONDS == 30 * 60
    t = NOW - timedelta(minutes=40)
    _parent(tmp_path, "u", [_notification(REAL_TASK, interim=True, at=t)])

    assert running_subagent_ids(tmp_path, "u", now=t + timedelta(minutes=29)) == {REAL_TASK}
    assert running_subagent_ids(tmp_path, "u", now=t + timedelta(minutes=31)) == set()


def test_row3_the_cap_ends_the_session_through_the_dispatcher(tmp_path: Path):
    t = NOW - timedelta(minutes=31)
    claude, d, sid = _finishing_session(tmp_path, [_notification(REAL_TASK, interim=True, at=t)])

    d.tick()

    assert sid in d.ended_sessions and claude.stopped == [sid]


def test_row4_the_real_case_one_running_subagent(tmp_path: Path):
    # parent lines 851 (18:52:15.874Z) and 861 (18:54:41.707Z); the dispatcher ended the session at 19:05:14Z
    lines = [
        _notification(REAL_TASK, interim=True, at=datetime(2026, 10, 13, 18, 52, 15, 874000, tzinfo=timezone.utc),
                      result="The baseline is still running and I haven't touched merge.py."),
        _notification(REAL_TASK, interim=True, at=datetime(2026, 10, 13, 18, 54, 41, 707000, tzinfo=timezone.utc),
                      result="The after-run is still going."),
    ]
    _parent(tmp_path, "74765d0c-1219-44e2-a293-faab9ea564a9", lines)

    running = running_subagent_ids(tmp_path, "74765d0c-1219-44e2-a293-faab9ea564a9",
                                   now=datetime(2026, 10, 13, 19, 5, 14, tzinfo=timezone.utc))

    assert running == {REAL_TASK}


# -- rows 5-6: the comment walk at a session's end ---------------------------------------------

class ThreadGh(FakeGh):
    """The two real readers over a canned comment list (`GhCli` with a fake runner)."""

    def __init__(self) -> None:
        super().__init__()
        self.thread: dict[int, list[dict]] = {}

    def _cli(self, number: int) -> GhCli:
        def runner(args, stdin=None):
            out = json.dumps({"comments": self.thread.get(number, [])})
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=out, stderr="")
        return GhCli(runner=runner)

    def last_comment_names(self, number, session_ids=()):
        return self._cli(number).last_comment_names(number, session_ids)

    def own_checkpoint_parks(self, number, session_ids=(), held_from="", held_until="", other_ids=None):
        return self._cli(number).own_checkpoint_parks(number, session_ids, held_from, held_until, other_ids)


def _end(tmp_path: Path, thread: list[tuple[str, str]]):
    claude, gh, core_gh = FakeClaude(), ThreadGh(), FakeCoreGh()
    d = _core_dispatcher(tmp_path, claude=claude, gh=gh, core_gh=core_gh)
    gh._candidates = [_issue(9, 9)]
    d.tick()
    live_id = claude.list_sessions()[0]["id"]
    gh.claim(9)
    gh.thread[9] = [{"createdAt": at, "body": body.replace("<id>", live_id)} for at, body in thread]
    claude.set_state(live_id, state="done", status="idle")
    outcome = d.tick().outcomes[9]
    return outcome, gh, core_gh, d


#: #970's real comments, cut to their first words (created-at times as `gh` printed them).
REAL_970 = [
    ("2026-10-13T18:13:23Z", "Checkpoint (ORCH <id>, #970): took the issue over. Unit 1 of 1 (merge.py + one "
                             "sentence in orch.md): a researcher-find lists the places; then a worker."),
    ("2026-10-13T18:13:56Z", "Checkpoint (ORCH <id>, #970): the issue is one unit (`merge.py` + one sentence of "
                             "`orch.md`). A researcher-find listed the places (lock at `merge.py:2062`)."),
    ("2026-10-13T18:30:11Z", "From the autopilot (#780, session 212), a new fact for this ticket, read from "
                             "`var/agentops/merge.log`: line 2888, 20:21:21, `batch of 2: orch-968, orch-969 -> "
                             "red-suite (suite 24 min, 0 failed)`."),
]


def test_row5_the_real_970_sequence_is_released(tmp_path: Path):
    outcome, gh, core_gh, d = _end(tmp_path, REAL_970)

    assert outcome == "released"
    assert gh.comments == [] and core_gh.calls == []
    assert gh.removed_assignees == [9] and gh.issues[9].labels == ("ready-for-agent",)
    assert d._consecutive_no_checkpoint(9) == 0
    assert [(e["issue"], e["outcome"]) for e in _events(tmp_path) if e["kind"] == "orch_end"] == [(9, "released")]


def test_row5_the_dispatchers_own_alarm_and_a_machine_comment_do_not_stop_the_walk(tmp_path: Path):
    thread = REAL_970[:2] + [("2026-10-13T18:40:00Z", "<!-- merge result -->\nmerged wt-1 into main"),
                             ("2026-10-13T18:41:00Z", "ORCH session 0badf00d ended without a closing or parking checkpoint")]
    outcome, gh, core_gh, _ = _end(tmp_path, thread)

    assert outcome == "released" and gh.comments == [] and core_gh.calls == []


def test_row5_another_orch_session_writing_after_the_checkpoint_keeps_no_checkpoint(tmp_path: Path):
    thread = REAL_970 + [("2026-10-13T18:50:00Z", "Checkpoint (ORCH f50e4a1b, #970): taking it over.")]
    outcome, gh, core_gh, _ = _end(tmp_path, thread)

    assert outcome == "no_checkpoint"
    assert [n for n, _ in gh.comments] == [9]


def test_row6_no_own_comment_stays_no_checkpoint(tmp_path: Path):
    outcome, gh, core_gh, _ = _end(tmp_path, [REAL_970[2]])

    assert outcome == "no_checkpoint"
    assert [n for n, _ in gh.comments] == [9]
    assert any(c[:2] == ["issue", "create"] for c in core_gh.calls)
