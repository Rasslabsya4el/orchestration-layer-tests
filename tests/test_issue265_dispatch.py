"""#265 unit 2: the dispatcher acts on an ORCH session's silence.

A session whose own transcript (and every still-running subagent's) wrote no timed record for `silent_orch_minutes`
(30) while a background shell has no end record is: ended at that poll when it is finishing, woken once when it is not
and ended `silent_orch_minutes` after the wake when it stayed silent. A wake or an end writes one dispatcher log line
and one brain notice.

Fake clock, fake `claude`, fake `gh`, transcript fixtures only. The cases are 20.10's two silent ORCH sessions.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests._area_rule import _no_area_rule  # noqa: F401
from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, _events, _issue
from tests.test_agentops_dispatch_finishing import FakeTimestamp, _dispatcher
from tests.test_issue209_unblocker_line import AREA, CHAT, TITLE, chats  # noqa: F401
from tools.agentops.brain_notice import IssueRead
from tools.agentops.dispatch import mark_finishing

DAY = datetime(2026, 10, 20, tzinfo=timezone.utc)
LOG_RE = re.compile(r"^(?P<name>orch .+?) silent (?P<n>\d+) min \(shells: (?P<shells>[^)]*)\); (?P<what>woken|ended)$")


def at(hour: int, minute: int, second: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute, second=second)


def _stamp(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


class _Gh(FakeGh):
    def parent_issue(self, number: int):
        return None

    def issue_title_and_last_comment(self, number: int):
        return IssueRead(f"Title {number}", "", None, None, (AREA,))


class Rig:
    """One ORCH session holding #1 and #2, its transcript written by the test, a brain chat live."""

    def __init__(self, tmp_path: Path, *, finishing: bool, last_record: datetime, shells: dict[str, datetime],
                 listing: tuple[str, str] = ("working", "busy")) -> None:
        self.tmp = tmp_path
        self.projects = tmp_path / "projects"
        self.claude = FakeClaude()
        self.claude.add_session("area-chat", name=CHAT, kind="interactive", status="idle")
        self.claude.add_session("title-chat", name=TITLE, kind="interactive", status="idle")
        self.gh = _Gh(candidates=[_issue(1, 1), _issue(2, 2)])
        self.ts = FakeTimestamp(last_record - timedelta(minutes=20))
        self.clock = FakeClock()
        self.base = self.ts.value
        self.d = _dispatcher(tmp_path, claude=self.claude, gh=self.gh, clock=self.clock, timestamp=self.ts)
        self.d.brain_notice_file = None
        self.d._brain_notifier = self.d._build_brain_notifier(tmp_path / "brain_notices.json", TITLE)
        self.log: list[str] = []
        self.d.log = self.log.append
        self.d.tick()
        self.sid = next(s["id"] for s in self.claude.list_sessions() if s.get("kind") == "background")
        self.uuid = next(s["sessionId"] for s in self.claude.list_sessions() if s["id"] == self.sid)
        self.gh.claim(1)
        self.gh.claim(2)
        self.gh._candidates = []
        self.d.tick()
        self.last_record = last_record
        self.shells = shells
        self._write_transcript()
        if finishing:
            mark_finishing(self.sid, finishing_dir=tmp_path / "finishing")
        self.claude.set_state(self.sid, state=listing[0], status=listing[1])

    def _append(self, record: dict, path: Path | None = None) -> None:
        path = path or self.projects / f"{self.uuid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def _write_transcript(self) -> None:
        for n, (task, started) in enumerate(sorted(self.shells.items())):
            self._append({"type": "assistant", "timestamp": _stamp(started), "message": {"content": [{
                "type": "tool_use", "id": f"toolu_{n}", "name": "Bash",
                "input": {"command": "prequeue", "run_in_background": True}}]}})
            self._append({"type": "user", "timestamp": _stamp(started), "message": {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": f"toolu_{n}", "is_error": False,
                "content": f"Command running in background with ID: {task}. Output is being written to: x"}]}})
        self.record_at(self.last_record)

    def record_at(self, when: datetime) -> None:
        self._append({"type": "system", "subtype": "turn_duration", "timestamp": _stamp(when)})

    def subagent_record_at(self, when: datetime) -> None:
        self._append({"type": "assistant", "timestamp": _stamp(when)},
                     self.projects / self.uuid / "subagents" / "agent-a0.jsonl")

    def go(self, when: datetime) -> None:
        self.clock.value = (when - self.base).total_seconds()
        self.ts.value = when
        self.d.tick()

    @property
    def ended(self) -> bool:
        return self.sid in self.d.ended_sessions

    def silent_lines(self) -> list[str]:
        return [x for x in self.log if " silent " in x and LOG_RE.match(x)]

    def orch_wakes(self) -> list[str]:
        return [m for t, m in self.claude.waked if t not in (CHAT, TITLE) and "silent" in m]

    def notices(self) -> list[str]:
        return [m for t, m in self.claude.waked if t == CHAT and m.startswith("#")]


LISTINGS = [("working", "busy"), ("idle", "idle")]
SHELLS_1 = {"bqnh181kv": at(1, 38, 57), "bjwtauppz": at(1, 39, 0), "bn12fs8xo": at(3, 24, 54)}


@pytest.mark.parametrize("listing", LISTINGS)
def test_row1_finishing_with_three_never_ended_shells_is_ended_at_30_min_of_silence(tmp_path: Path, listing):
    rig = Rig(tmp_path, finishing=True, last_record=at(3, 24, 57), shells=SHELLS_1, listing=listing)

    rig.go(at(3, 24, 58))
    rig.go(at(3, 40))
    rig.go(at(3, 54, 56))
    assert not rig.ended and rig.claude.stopped == []

    rig.go(at(3, 54, 57))

    assert rig.ended and rig.claude.stopped == [rig.sid]
    lines = rig.silent_lines()
    assert len(lines) == 1
    parsed = LOG_RE.match(lines[0])
    assert parsed["what"] == "ended" and parsed["n"] == "30"
    assert lines[0] == "orch orchestration-layer #1 #2 silent 30 min (shells: bjwtauppz, bn12fs8xo, bqnh181kv); ended"
    assert set(parsed["shells"].split(", ")) == {"bqnh181kv", "bjwtauppz", "bn12fs8xo"}
    assert len(rig.notices()) == 1 and "ended" in rig.notices()[0]
    assert rig.orch_wakes() == []


@pytest.mark.parametrize("listing", LISTINGS)
def test_row2_not_finishing_is_woken_once_then_ended_30_min_later(tmp_path: Path, listing):
    shells = {"bqnh181kv": at(1, 38, 57), "bjwtauppz": at(1, 39, 0)}
    rig = Rig(tmp_path, finishing=False, last_record=at(1, 39, 3), shells=shells, listing=listing)

    rig.go(at(2, 9, 2))
    assert rig.orch_wakes() == [] and not rig.ended

    rig.go(at(2, 9, 3))
    assert len(rig.orch_wakes()) == 1
    message = rig.orch_wakes()[0]
    assert "30 min" in message and "bqnh181kv" in message and "bjwtauppz" in message
    assert "#1" in message and "#2" in message
    assert not rig.ended
    assert [LOG_RE.match(x)["what"] for x in rig.silent_lines()] == ["woken"]
    assert len(rig.notices()) == 1 and "woken" in rig.notices()[0]

    for minute in range(10, 40):  # every minute until just before the second mark: no second wake, no end
        rig.go(at(2, minute) if minute < 39 else at(2, 39, 2))
    assert len(rig.orch_wakes()) == 1 and not rig.ended

    rig.go(at(2, 39, 3))

    assert rig.ended and rig.claude.stopped == [rig.sid]
    assert [LOG_RE.match(x)["what"] for x in rig.silent_lines()] == ["woken", "ended"]
    assert len(rig.orch_wakes()) == 1
    assert len(rig.notices()) == 2 and "ended" in rig.notices()[1]


def test_row2_a_record_after_the_wake_means_it_is_not_ended(tmp_path: Path):
    shells = {"bqnh181kv": at(1, 38, 57), "bjwtauppz": at(1, 39, 0)}
    rig = Rig(tmp_path, finishing=False, last_record=at(1, 39, 3), shells=shells)
    rig.go(at(2, 9, 3))
    assert len(rig.orch_wakes()) == 1

    rig.record_at(at(2, 12))  # it answered
    rig.go(at(2, 39, 3))
    rig.go(at(2, 41, 59))

    assert not rig.ended and rig.claude.stopped == []
    assert [LOG_RE.match(x)["what"] for x in rig.silent_lines()] == ["woken"]


def test_row2_a_restarted_dispatcher_does_not_wake_twice(tmp_path: Path):
    shells = {"bqnh181kv": at(1, 38, 57)}
    rig = Rig(tmp_path, finishing=False, last_record=at(1, 39, 3), shells=shells)
    rig.go(at(2, 9, 3))
    assert len(rig.orch_wakes()) == 1

    # a new dispatcher over the same stores: the live session is adopted by its name
    rig.claude.set_state(rig.sid, state="working", status="busy")
    rig.claude._sessions[rig.sid]["name"] = "orch #1 #2"
    d2 = _dispatcher(tmp_path, claude=rig.claude, gh=rig.gh, clock=rig.clock, timestamp=rig.ts)
    d2.log = rig.log.append
    rig.d = d2
    rig.go(at(2, 12))
    rig.go(at(2, 20))

    assert len(rig.orch_wakes()) == 1 and not rig.ended and any("adopted live" in x for x in rig.log)
    rig.go(at(2, 39, 3))
    assert rig.ended


@pytest.mark.parametrize("listing", LISTINGS)
def test_row3_finishing_with_one_shell_is_ended_at_30_min_of_silence(tmp_path: Path, listing):
    rig = Rig(tmp_path, finishing=True, last_record=at(6, 29, 43), shells={"bve9hgrbg": at(5, 56, 24)},
              listing=listing)

    rig.go(at(6, 59, 42))
    assert not rig.ended
    rig.go(at(6, 59, 43))

    assert rig.ended and rig.claude.stopped == [rig.sid]
    lines = rig.silent_lines()
    assert len(lines) == 1 and LOG_RE.match(lines[0])["shells"] == "bve9hgrbg"
    assert len(rig.notices()) == 1


@pytest.mark.parametrize("finishing", [True, False])
def test_row4_a_running_subagent_with_a_recent_record_means_no_wake_and_no_end(tmp_path: Path, finishing):
    rig = Rig(tmp_path, finishing=finishing, last_record=at(3, 0), shells={"bqnh181kv": at(2, 0)})
    rig.subagent_record_at(at(3, 55))  # 5 min old at 04:00

    rig.go(at(4, 0))
    rig.go(at(4, 20))
    rig.subagent_record_at(at(4, 20))
    rig.go(at(4, 40))

    assert not rig.ended and rig.claude.stopped == []
    assert rig.orch_wakes() == [] and rig.silent_lines() == [] and rig.notices() == []


@pytest.mark.parametrize("finishing", [True, False])
def test_row5_silent_25_min_with_a_shell_running_is_neither_woken_nor_ended(tmp_path: Path, finishing):
    rig = Rig(tmp_path, finishing=finishing, last_record=at(3, 0), shells={"bqnh181kv": at(2, 0)})

    rig.go(at(3, 25))

    assert not rig.ended and rig.claude.stopped == []
    assert rig.orch_wakes() == [] and rig.silent_lines() == [] and rig.notices() == []


def test_a_session_with_no_shell_is_left_to_the_older_rules(tmp_path: Path):
    rig = Rig(tmp_path, finishing=False, last_record=at(3, 0), shells={})

    rig.go(at(3, 40))

    assert rig.orch_wakes() == [] and rig.silent_lines() == [] and not rig.ended


def test_a_shell_with_an_end_notice_does_not_count(tmp_path: Path):
    rig = Rig(tmp_path, finishing=False, last_record=at(3, 0), shells={"bqnh181kv": at(2, 0)})
    rig._append({"type": "queue-operation", "operation": "enqueue", "timestamp": _stamp(at(2, 30)), "content": (
        "<task-notification>\n<task-id>bqnh181kv</task-id>\n<status>completed</status>\n</task-notification>")})

    rig.go(at(3, 40))

    assert rig.orch_wakes() == [] and rig.silent_lines() == []


def test_the_notice_is_held_while_no_brain_chat_is_live_and_sent_once_when_one_is(tmp_path: Path):
    rig = Rig(tmp_path, finishing=True, last_record=at(6, 29, 43), shells={"bve9hgrbg": at(5, 56, 24)})
    for sid in ("area-chat", "title-chat"):
        rig.claude._sessions.pop(sid)

    rig.go(at(6, 59, 43))
    rig.go(at(7, 0))
    assert rig.ended and rig.notices() == [] and len(rig.silent_lines()) == 1

    rig.claude.add_session("area-chat", name=CHAT, kind="interactive", status="idle")
    rig.go(at(7, 1))
    rig.go(at(7, 2))

    assert len(rig.notices()) == 1 and "ended" in rig.notices()[0]
    assert len(rig.silent_lines()) == 1


def _delivering(rig: Rig, *, ok: bool = True) -> list[datetime]:
    """The real delivery takes a second and Claude Code writes the delivered message into the session's transcript
    as a timed record: this fake claude does both, and returns the times of the deliveries."""
    real_wake = rig.claude.wake
    times: list[datetime] = []

    def wake(target: str, message: str):
        result = real_wake(target, message)
        if "silent" in message and target not in (CHAT, TITLE):
            rig.ts.value += timedelta(seconds=1)
            times.append(rig.ts.value)
            if ok:
                rig.record_at(rig.ts.value)
            return (True, "{}") if ok else (False, "boom")
        return result

    rig.claude.wake = wake
    return times


def test_the_delivered_wake_record_is_not_the_sessions_answer_so_it_is_ended_30_min_after_the_wake(tmp_path: Path):
    shells = {"bqnh181kv": at(1, 38, 57)}
    rig = Rig(tmp_path, finishing=False, last_record=at(1, 39, 3), shells=shells)
    times = _delivering(rig)

    rig.go(at(2, 9, 3))
    assert len(rig.orch_wakes()) == 1 and times == [at(2, 9, 4)]

    rig.go(at(2, 20))
    rig.go(at(2, 39, 3))
    assert not rig.ended and len(rig.orch_wakes()) == 1
    rig.go(at(2, 39, 4))

    assert rig.ended and len(rig.orch_wakes()) == 1
    assert [LOG_RE.match(x)["what"] for x in rig.silent_lines()] == ["woken", "ended"]


def test_a_failed_delivery_leaves_no_wake_record_and_the_next_poll_tries_again(tmp_path: Path):
    shells = {"bqnh181kv": at(1, 38, 57)}
    rig = Rig(tmp_path, finishing=False, last_record=at(1, 39, 3), shells=shells)
    rig.claude.wake_fails = True

    rig.go(at(2, 9, 3))
    rig.go(at(2, 9, 33))

    assert len(rig.orch_wakes()) == 2 and rig.d.sessions[0].silent_woken is None
    assert not [e for e in _events(tmp_path) if e["kind"] == "orch_silent_wake"]
    assert rig.silent_lines() == [] and rig.notices() == []

    rig.claude.wake_fails = False
    rig.go(at(2, 10, 3))
    rig.go(at(2, 10, 33))

    assert len(rig.orch_wakes()) == 3 and not rig.ended
    assert len([e for e in _events(tmp_path) if e["kind"] == "orch_silent_wake"]) == 1
    assert [LOG_RE.match(x)["what"] for x in rig.silent_lines()] == ["woken"] and len(rig.notices()) == 1


def test_the_wake_message_and_the_notice_name_the_same_claims(tmp_path: Path):
    shells = {"bqnh181kv": at(1, 38, 57)}
    rig = Rig(tmp_path, finishing=False, last_record=at(1, 39, 3), shells=shells)

    rig.go(at(2, 9, 3))

    assert "you claim #1, #2" in rig.orch_wakes()[0]
    assert "(claims #1, #2)" in rig.notices()[0]
