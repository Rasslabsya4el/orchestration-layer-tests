"""#241: the dispatcher tells the brain chat each line of `brain_lines.log` once.

The merge writes one JSON line `{"time", "repo", "line"}` per fallback to the PC (`merge_github.append_brain_line`);
the dispatcher reads the lines past its offset on every tick and sends the new ones to the repo's brain chat (no area
label), several in one message, one line each. Driven through `Dispatcher.tick` with the fakes of the dispatcher's
own test module: no real `claude`, no real `gh`, no real chat.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import tools.agentops.dispatch as dispatch
from tools.agentops.brain_notice import MAX_ATTEMPTS
from tools.agentops.dispatch import Dispatcher

from tests._area_rule import _no_area_rule  # noqa: F401
from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh

BRAIN = "Brain: Main"
SESSION_BRAIN = "brain-1"


def _row(line: str, repo: str = "orch") -> bytes:
    return (json.dumps({"time": "2026-10-19 10:00:00", "repo": repo, "line": line}) + "\n").encode("utf-8")


def _line(n: int) -> str:
    return f"orch merge feat-{n}: GitHub not-started, suite on the PC (GitHub not-started)"


class Rig:
    """One repo's installation: the lines file, the state file, a brain chat that can be live or not."""

    def __init__(self, tmp_path: Path, monkeypatch, *, live: bool = True) -> None:
        monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(tmp_path / "machine"))
        real = dispatch._load_settings
        monkeypatch.setattr(dispatch, "_load_settings", lambda: {**real(), "brain_chat_title": BRAIN,
                                                                 "brain_chats": {}, "areas": []})
        self.root = tmp_path
        self.lines_file = tmp_path / "brain_lines.log"
        self.state_file = tmp_path / "orch_session.json"
        self.claude = FakeClaude()
        self.gh = FakeGh()
        self.clock = FakeClock()
        self.wall0 = datetime.now(timezone.utc).timestamp()
        self.log: list[str] = []
        if live:
            self.set_brain_live(True)
        self.d = self.new_dispatcher()

    def set_brain_live(self, live: bool) -> None:
        if live:
            self.claude.add_session(SESSION_BRAIN, name=BRAIN, state="working", status="busy", kind="interactive",
                                    cwd=str(self.claude.cwd))
        else:
            self.claude._sessions.pop(SESSION_BRAIN, None)

    def new_dispatcher(self, *, dry_run: bool = False) -> Dispatcher:
        """A dispatcher on the same installation: building a second one on the same state file is a restart."""
        d = Dispatcher(claude=self.claude, gh=self.gh, max_hours=40.0, events_file=self.root / "events.jsonl",
                       state_file=self.state_file, projects_dir=self.root / "projects",
                       finishing_dir=self.root / "finishing", context_line=1_000_000, clock=self.clock,
                       timestamp=lambda: datetime.fromtimestamp(self.wall0 + self.clock.value, timezone.utc),
                       merge_lock_path=self.root / "var" / "locks" / "merge.lock", orch_offer_max=99,
                       brain_lines_file=self.lines_file, dry_run=dry_run)
        d.log = self.log.append
        return d

    def append(self, data: bytes) -> None:
        with self.lines_file.open("ab") as fh:
            fh.write(data)

    def sent(self) -> list[str]:
        """The messages the brain chat was woken with, in order (the other wakes of the dispatcher are not these)."""
        return [m for target, m in self.claude.waked if target == BRAIN]

    def brain_lines_logs(self) -> list[str]:
        return [line for line in self.log if line.startswith("brain lines")]


def test_1_three_lines_one_message_then_nothing_then_a_fourth_alone(tmp_path: Path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.append(_row(_line(1)) + _row(_line(2)) + _row(_line(3)))

    rig.d.tick()
    assert rig.sent() == ["\n".join([_line(1), _line(2), _line(3)])]

    rig.d.tick()
    assert len(rig.sent()) == 1  # a second tick sends nothing

    restarted = rig.new_dispatcher()  # a restart on the same persisted state
    restarted.tick()
    assert len(rig.sent()) == 1

    rig.append(_row(_line(4)))
    restarted.tick()
    assert rig.sent() == ["\n".join([_line(1), _line(2), _line(3)]), _line(4)]


def test_2_chat_not_live_for_two_ticks_then_the_three_lines_once_on_the_third(tmp_path: Path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, live=False)
    rig.append(_row(_line(1)) + _row(_line(2)) + _row(_line(3)))

    rig.d.tick()
    rig.d.tick()
    assert rig.sent() == []  # the lines wait while the chat is not live

    rig.set_brain_live(True)
    rig.d.tick()
    assert rig.sent() == ["\n".join([_line(1), _line(2), _line(3)])]

    rig.d.tick()
    assert len(rig.sent()) == 1


def test_3_torn_last_line_is_sent_on_the_tick_after_its_newline(tmp_path: Path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    whole = _row(_line(1))
    torn = _row(_line(2))
    rig.lines_file.write_bytes(whole + torn[:-1])  # the last line has no newline yet

    rig.d.tick()
    assert rig.sent() == [_line(1)]  # only the whole line

    rig.append(torn[-1:])  # the newline arrives
    rig.d.tick()
    assert rig.sent() == [_line(1), _line(2)]


def test_4_an_unreadable_line_is_skipped_with_one_log_line_and_later_lines_go(tmp_path: Path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.append(_row(_line(1)) + b"this is not json\n" + _row(_line(2)))

    rig.d.tick()
    assert rig.sent() == ["\n".join([_line(1), _line(2)])]
    assert len(rig.brain_lines_logs()) == 1
    assert "unreadable" in rig.brain_lines_logs()[0]

    rig.d.tick()
    assert len(rig.sent()) == 1 and len(rig.brain_lines_logs()) == 1  # the skipped line is not read again


def test_5_a_failed_send_is_kept_once_then_dropped_at_max_attempts_with_one_count_line(tmp_path: Path, monkeypatch):
    assert MAX_ATTEMPTS == 2  # the two attempts below are the whole budget
    rig = Rig(tmp_path, monkeypatch)
    rig.append(_row(_line(1)) + _row(_line(2)))
    rig.claude.wake_fails = True

    rig.d.tick()  # first failure: kept for one retry
    assert len(rig.sent()) == 1
    assert any("kept for one retry" in line for line in rig.brain_lines_logs())

    rig.d.tick()  # second failure: dropped, the count is named
    assert len(rig.sent()) == 2
    drops = [line for line in rig.brain_lines_logs() if "dropped" in line]
    assert len(drops) == 1 and "2 line(s) dropped" in drops[0]

    rig.claude.wake_fails = False
    rig.d.tick()  # the dropped lines are not sent later
    assert len(rig.sent()) == 2


def test_5b_a_failed_send_then_a_good_retry_sends_the_lines_once(tmp_path: Path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.append(_row(_line(1)))
    rig.claude.wake_fails = True
    rig.d.tick()
    rig.claude.wake_fails = False

    rig.d.tick()
    rig.d.tick()
    # the fake records the failed wake too: the failure, then the one good retry; the later ticks send nothing
    assert rig.sent() == [_line(1), _line(1)]


def test_6_dry_run_sends_nothing_and_moves_no_offset(tmp_path: Path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.append(_row(_line(1)) + _row(_line(2)))
    dry = rig.new_dispatcher(dry_run=True)

    dry.tick()

    assert rig.sent() == []
    state = json.loads(rig.state_file.read_text(encoding="utf-8")) if rig.state_file.exists() else {}
    assert (state.get("brain_lines") or {}).get("offset", 0) == 0  # nothing was told, so nothing is consumed

    rig.new_dispatcher().tick()  # a real tick after the dry one still tells both lines
    assert rig.sent() == ["\n".join([_line(1), _line(2)])]


def test_7_without_a_lines_file_the_dispatcher_reads_nothing(tmp_path: Path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.append(_row(_line(1)))
    plain = Dispatcher(claude=rig.claude, gh=rig.gh, max_hours=40.0, events_file=tmp_path / "events.jsonl",
                       state_file=tmp_path / "plain_state.json", projects_dir=tmp_path / "projects",
                       finishing_dir=tmp_path / "finishing", context_line=1_000_000, clock=rig.clock,
                       merge_lock_path=tmp_path / "var" / "locks" / "merge.lock", orch_offer_max=99)
    plain.log = rig.log.append

    plain.tick()

    assert rig.sent() == []


def test_8_a_negative_stored_offset_is_read_from_the_start_and_never_raises(tmp_path: Path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.append(_row(_line(1)))
    rig.state_file.write_text(json.dumps({"brain_lines": {"offset": -5, "attempts": -1}}), encoding="utf-8")

    rig.new_dispatcher().tick()  # a corrupt offset must not stop the tick or loop on a failing seek

    assert rig.sent() == [_line(1)]
