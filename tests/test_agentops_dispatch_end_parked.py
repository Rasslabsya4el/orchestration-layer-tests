"""#975 unit 2: a session this dispatcher marked ended (`ended_sessions`) whose claude.exe is still
listed is ended through `end_session` once no issue it held is still open and assigned to it, no
subagent runs and no watch is live. Row 2 is the ending (one end call, one log line); rows 3-4 are the
never-touched cases (0 end calls each). Fake `claude.exe` / `gh` / clock, no real call."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools.agentops import dispatch as dispatch_mod
from tools.agentops.dispatch import IssueStatus

from tests.test_agentops_dispatch import FakeClaude, FakeGh
from tests.test_agentops_dispatch_finishing import FakeTimestamp, _dispatcher

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
PID = 4321


class _StuckAssigneeGh(FakeGh):
    """GitHub keeps the assignee: `remove_assignee` is recorded and changes nothing."""

    def remove_assignee(self, number: int, login: str = "@me") -> bool:
        self.removed_assignees.append(number)
        return False


@pytest.fixture
def ends(monkeypatch) -> list[str]:
    """Every `end_session` call the dispatcher makes; the real function still runs underneath."""
    calls: list[str] = []
    real = dispatch_mod.end_session

    def spy(claude, session_id, **kwargs):
        calls.append(session_id)
        return real(claude, session_id, **kwargs)

    monkeypatch.setattr(dispatch_mod, "end_session", spy)
    return calls


def _row(claude: FakeClaude, sid: str, *, name: str = "orch Target-Repo #1", cwd: Path | None = None,
         kind: str = "background", state: str = "stopped", status: str = "idle", hours_old: float = 3.0,
         pid: int = PID) -> str:
    """One `claude agents --json` row; returns its transcript uuid."""
    uuid = f"uuid-{sid}"
    claude.add_session(sid, name=name, cwd=str(cwd or claude.cwd), kind=kind, state=state, status=status,
                       pid=pid, sessionId=uuid,
                       startedAt=int((NOW - timedelta(hours=hours_old)).timestamp() * 1000))
    return uuid


def _state_file(tmp_path: Path, entries: list[tuple[str, str, list[int]]]) -> None:
    """The dispatcher's own state file naming sessions that held issues (read on the first tick)."""
    sessions = [{"session": sid, "session_uuid": uuid, "offered": list(held), "held": list(held),
                 "preexisting": [], "claimed_now": None, "finishing": False,
                 "started_at_wall": (NOW - timedelta(hours=3)).timestamp()}
                for sid, uuid, held in entries]
    (tmp_path / "orch_session.json").write_text(
        json.dumps({"updated": NOW.isoformat(), "session": None, "sessions": sessions}), encoding="utf-8")


def _tick(tmp_path: Path, claude: FakeClaude, gh: FakeGh):
    d = _dispatcher(tmp_path, claude=claude, gh=gh, timestamp=FakeTimestamp(NOW))
    msgs: list[str] = []
    d.log = msgs.append
    d.tick()
    return d, msgs


def test_row2_parked_session_with_no_open_held_issue_is_ended_with_one_log_line(tmp_path: Path, ends):
    claude = FakeClaude()
    claude.stop_leaves_process = True  # the real #86 behaviour: `claude stop` keeps the pid listed
    gh = FakeGh(issues={1: IssueStatus(number=1, open=False, labels=(), assignees=())})
    uuid = _row(claude, "dead-1")
    _state_file(tmp_path, [("dead-1", uuid, [1])])

    d, msgs = _tick(tmp_path, claude, gh)

    assert ends == ["dead-1"]
    assert claude.ended_pids == [PID]
    own = [m for m in msgs if "dead-1" in m and "marked ended" in m]
    assert len(own) == 1, own
    assert "#1" in own[0] and "age" in own[0]


def test_row3_held_issue_still_open_and_assigned_is_never_ended(tmp_path: Path, ends):
    claude = FakeClaude()
    claude.stop_leaves_process = True
    gh = _StuckAssigneeGh(issues={1: IssueStatus(number=1, open=True, labels=("ready-for-agent",),
                                                  assignees=("bot",))})
    uuid = _row(claude, "dead-2")
    _state_file(tmp_path, [("dead-2", uuid, [1])])

    d, _ = _tick(tmp_path, claude, gh)

    assert ends == []
    assert [s["id"] for s in claude.list_sessions()] == ["dead-2"]
    assert gh.issues[1].assignees == ("bot",)


def test_row4_other_repo_orch_session_is_never_ended(tmp_path: Path, ends):
    claude = FakeClaude()
    claude.stop_leaves_process = True
    uuid = _row(claude, "other-1", name="orch Other #7", cwd=tmp_path / "Other")
    _state_file(tmp_path, [("other-1", uuid, [])])

    _tick(tmp_path, claude, FakeGh())

    assert ends == []
    assert [s["id"] for s in claude.list_sessions()] == ["other-1"]


def test_row4_interactive_chat_is_never_ended(tmp_path: Path, ends):
    claude = FakeClaude()
    claude.stop_leaves_process = True
    uuid = _row(claude, "chat-1", name="Target-Repo chat", kind="interactive")
    _state_file(tmp_path, [("chat-1", uuid, [])])

    _tick(tmp_path, claude, FakeGh())

    assert ends == []
    assert [s["id"] for s in claude.list_sessions()] == ["chat-1"]


def test_row4_autopilot_session_is_never_ended(tmp_path: Path, ends):
    claude = FakeClaude()
    claude.stop_leaves_process = True
    uuid = _row(claude, "auto-1", name="autopilot #975")
    _state_file(tmp_path, [("auto-1", uuid, [])])

    _tick(tmp_path, claude, FakeGh())

    assert ends == []
    assert [s["id"] for s in claude.list_sessions()] == ["auto-1"]


def test_row4_session_with_a_live_subagent_is_never_ended(tmp_path: Path, ends):
    claude = FakeClaude()
    claude.stop_leaves_process = True
    uuid = _row(claude, "dead-3")
    sub = tmp_path / "projects" / uuid / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a1.jsonl").write_text("{}\n", encoding="utf-8")
    _state_file(tmp_path, [("dead-3", uuid, [])])

    _tick(tmp_path, claude, FakeGh())

    assert ends == []
    assert [s["id"] for s in claude.list_sessions()] == ["dead-3"]


def test_row4_live_session_not_marked_ended_is_never_ended(tmp_path: Path, ends):
    claude = FakeClaude()
    claude.stop_leaves_process = True
    _row(claude, "live-3", name="orch Target-Repo #3", state="working", status="busy", hours_old=0.5)

    d, _ = _tick(tmp_path, claude, FakeGh())
    d.tick()

    assert ends == []
    assert "live-3" not in d.ended_sessions
    assert [s["id"] for s in claude.list_sessions()] == ["live-3"]


def test_row4_marked_ended_but_still_working_busy_is_never_ended(tmp_path: Path, ends):
    claude = FakeClaude()
    claude.stop_leaves_process = True
    _row(claude, "live-4", name="orch Target-Repo #4", state="working", status="busy", hours_old=0.5)
    d = _dispatcher(tmp_path, claude=claude, gh=FakeGh(), timestamp=FakeTimestamp(NOW))
    d.ended_sessions.add("live-4")

    d.tick()

    assert ends == []
    assert [s["id"] for s in claude.list_sessions()] == ["live-4"]


# -- #975 unit 2: a session an EARLIER dispatcher process parked (not in `ended_sessions`) ---------

CLOSED_837 = IssueStatus(number=837, open=False, labels=(), assignees=())
CLOSED_935 = IssueStatus(number=935, open=False, labels=(), assignees=())


def _direct(tmp_path: Path, claude: FakeClaude, gh: FakeGh):
    """The end step alone (no tick: no adoption, no polling), so only its own rule can end a row."""
    d = _dispatcher(tmp_path, claude=claude, gh=gh, timestamp=FakeTimestamp(NOW))
    msgs: list[str] = []
    d.log = msgs.append
    d._end_listed_ended_sessions()
    return d, msgs


def _event(tmp_path: Path, session: str, issue: int) -> None:
    """One `orch_end` event of an earlier process, as `_finish` writes it (`issue` per held issue)."""
    line = {"ts": NOW.isoformat(timespec="seconds"), "kind": "orch_end", "issue": issue,
            "session": session, "outcome": "parked", "why": "finished"}
    with (tmp_path / "events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def test_unit2_idle_orch_rows_parked_by_an_earlier_process_are_ended_one_each(tmp_path: Path, ends):
    claude = FakeClaude()
    claude.stop_leaves_process = True
    gh = FakeGh(issues={837: CLOSED_837, 935: CLOSED_935})
    _row(claude, "f0cd04a2", name="orch Target-Repo #837 ~b87c", state="done", pid=4321, hours_old=24.0)
    _row(claude, "0768339a", name="orch Target-Repo #935 ~41e0", state="done", pid=4322, hours_old=24.0)

    _, msgs = _tick(tmp_path, claude, gh)

    assert ends == ["f0cd04a2", "0768339a"]
    assert claude.ended_pids == [4321, 4322]
    own = [m for m in msgs if "listed after a restart" in m]
    assert len(own) == 2, own
    assert "f0cd04a2" in own[0] and "#837" in own[0] and "age" in own[0]
    assert "0768339a" in own[1] and "#935" in own[1] and "age" in own[1]


def test_unit2_held_issue_known_only_from_the_events_log_still_ends_the_row(tmp_path: Path, ends):
    claude = FakeClaude()
    claude.stop_leaves_process = True
    gh = FakeGh(issues={837: CLOSED_837})
    _row(claude, "evt-1", name="orch Target-Repo ~x1", state="done", hours_old=24.0)
    _event(tmp_path, "evt-1", 837)

    _, msgs = _direct(tmp_path, claude, gh)

    assert ends == ["evt-1"]
    assert any("evt-1" in m and "#837" in m and "listed after a restart" in m for m in msgs), msgs


def test_unit2_restart_row_whose_named_issue_is_still_open_and_assigned_is_never_ended(tmp_path: Path, ends):
    claude = FakeClaude()
    claude.stop_leaves_process = True
    gh = _StuckAssigneeGh(issues={935: IssueStatus(number=935, open=True, labels=("ready-for-agent",),
                                                    assignees=("bot",))})
    _row(claude, "open-935", name="orch Target-Repo #935 ~41e0", state="done", hours_old=24.0)

    _direct(tmp_path, claude, gh)

    assert ends == []
    assert [s["id"] for s in claude.list_sessions()] == ["open-935"]


def test_unit2_other_repo_orch_session_is_never_ended(tmp_path: Path, ends):
    claude = FakeClaude()
    claude.stop_leaves_process = True
    gh = FakeGh(issues={7: IssueStatus(number=7, open=False, labels=(), assignees=())})
    _row(claude, "other-7", name="orch other-repo #7", cwd=tmp_path / "Other", state="done",
         hours_old=24.0)

    _direct(tmp_path, claude, gh)

    assert ends == []
    assert [s["id"] for s in claude.list_sessions()] == ["other-7"]


def test_unit2_interactive_chat_with_an_orch_name_is_never_ended(tmp_path: Path, ends):
    claude = FakeClaude()
    claude.stop_leaves_process = True
    gh = FakeGh(issues={837: CLOSED_837})
    _row(claude, "chat-837", name="orch Target-Repo #837 ~b87c", kind="interactive", state="done",
         hours_old=24.0)

    _direct(tmp_path, claude, gh)

    assert ends == []
    assert [s["id"] for s in claude.list_sessions()] == ["chat-837"]


def test_unit2_autopilot_session_is_never_ended(tmp_path: Path, ends):
    claude = FakeClaude()
    claude.stop_leaves_process = True
    gh = FakeGh(issues={837: CLOSED_837})
    _row(claude, "auto-837", name="autopilot #837", state="done", hours_old=24.0)

    _direct(tmp_path, claude, gh)

    assert ends == []
    assert [s["id"] for s in claude.list_sessions()] == ["auto-837"]


def test_unit2_restart_row_with_a_live_subagent_is_never_ended(tmp_path: Path, ends):
    claude = FakeClaude()
    claude.stop_leaves_process = True
    gh = FakeGh(issues={837: CLOSED_837})
    uuid = _row(claude, "sub-837", name="orch Target-Repo #837 ~b87c", state="done", hours_old=24.0)
    sub = tmp_path / "projects" / uuid / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a2.jsonl").write_text("{}\n", encoding="utf-8")

    _direct(tmp_path, claude, gh)

    assert ends == []
    assert [s["id"] for s in claude.list_sessions()] == ["sub-837"]


def test_unit2_working_busy_row_is_never_ended(tmp_path: Path, ends):
    claude = FakeClaude()
    claude.stop_leaves_process = True
    gh = FakeGh(issues={837: CLOSED_837})
    _row(claude, "busy-837", name="orch Target-Repo #837 ~b87c", state="working", status="busy",
         hours_old=0.5)

    _direct(tmp_path, claude, gh)

    assert ends == []
    assert [s["id"] for s in claude.list_sessions()] == ["busy-837"]


def test_unit2_session_this_dispatcher_tracks_live_is_never_ended_by_this_step(tmp_path: Path, ends):
    claude = FakeClaude()
    claude.stop_leaves_process = True
    gh = FakeGh(issues={837: CLOSED_837})
    _row(claude, "tracked-837", name="orch Target-Repo #837 ~b87c", state="working", status="busy",
         hours_old=0.5)
    d = _dispatcher(tmp_path, claude=claude, gh=gh, timestamp=FakeTimestamp(NOW))
    d.log = lambda _msg: None
    assert d._adopt_live() == ["tracked-837"]  # tracked by this process
    claude.set_state("tracked-837", state="done", status="idle")  # now idle, still tracked

    d._end_listed_ended_sessions()

    assert ends == []
    assert [s["id"] for s in claude.list_sessions()] == ["tracked-837"]


def test_unit2_restart_row_with_no_issue_number_is_never_ended(tmp_path: Path, ends):
    claude = FakeClaude()
    claude.stop_leaves_process = True
    _row(claude, "bare-1", name="orch Target-Repo", state="done", hours_old=24.0)

    _direct(tmp_path, claude, FakeGh())

    assert ends == []
    assert [s["id"] for s in claude.list_sessions()] == ["bare-1"]


def test_unit2_one_end_attempt_per_session_per_process(tmp_path: Path, ends):
    claude = FakeClaude()
    claude.stop_leaves_process = True
    claude.end_process_fails = {4321}  # the process survives: the row stays listed
    gh = FakeGh(issues={837: CLOSED_837})
    _row(claude, "once-837", name="orch Target-Repo #837 ~b87c", state="done", hours_old=24.0)

    d, _ = _direct(tmp_path, claude, gh)
    d._end_listed_ended_sessions()

    assert ends == ["once-837"]
