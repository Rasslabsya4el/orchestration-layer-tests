"""#138 unit 2: `Dispatcher._brain_is_live` holds the news while the resolved brain chat awaits its owner.

Through `Dispatcher` with its fakes (no real `claude`, no real `gh`, no real message). The transcript is a
written `projects_dir/<sessionId>.jsonl`; the reading is `brain_fresh.awaiting_owner_message` (unit 1).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh
from tools.agentops.dispatch import Dispatcher

TITLE = "привет"
FOLDER = "D:\\work\\Target repo"
REPO = "owner/target"
HOME = "owner/home"
WALL_START = datetime(2026, 10, 16, 18, 7, tzinfo=timezone.utc)
CLOCK_START = 1000.0
SESSION = "S"

CLEAR = {
    "type": "user",
    "message": {
        "role": "user",
        "content": "<command-name>/clear</command-name>\n"
        "            <command-message>clear</command-message>\n"
        "            <command-args></command-args>",
    },
}

CROSS_SESSION = {
    "type": "user",
    "message": {
        "role": "user",
        "content": "Another Claude session sent a message:\n"
        '<cross-session-message from="uds:\\\\.\\pipe\\LOCAL\\cc-msg-68aa" from-name="dispatcher" from-mode="bypass">\n'
        "#133 A fresh worker copy can run the monitor tests without a manu: parked\n"
        "</cross-session-message>\n\nThis came from another Claude session.",
    },
}

OWNER_HELLO = {
    "type": "user",
    "message": {"role": "user", "content": "hello"},
}


class Wall:
    """The wall clock of the fake clock: the same seconds, as a datetime."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock

    def __call__(self) -> datetime:
        return WALL_START + timedelta(seconds=self.clock() - CLOCK_START)


@pytest.fixture
def values(monkeypatch):
    """The settings the dispatcher reads for the brain chat lookup."""
    values = {"repo": REPO, "home": HOME, "brain_chat_title": TITLE, "brain_chats": {},
              "brain_chat_title_aliases": [], "loss_note_area": ""}
    monkeypatch.setattr("tools.agentops.dispatch._load_settings", lambda *a, **k: values)
    return values


def _write(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    return path


def _dispatcher(tmp_path: Path, claude: FakeClaude) -> Dispatcher:
    clock = FakeClock(CLOCK_START)
    d = Dispatcher(claude=claude, gh=FakeGh(), max_hours=4.0, events_file=tmp_path / "events.jsonl",
                   state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                   clock=clock, timestamp=Wall(clock),
                   brain_notice_file=tmp_path / "brain_notices.json", brain_title=TITLE)
    d.log_lines = []
    d.log = d.log_lines.append
    return d


def _chat(claude: FakeClaude, session_id: str | None = SESSION) -> None:
    """A live interactive chat named by the title, in this repo's folder; `sessionId` only when given."""
    fields = {"name": TITLE, "kind": "interactive", "cwd": FOLDER, "status": "idle"}
    if session_id is not None:
        fields["sessionId"] = session_id
    claude.add_session("chat-1", **fields)


def _held(d: Dispatcher) -> list[str]:
    return [line for line in d.log_lines if "news held" in line]


def test_a_cleared_chat_with_no_owner_message_holds_the_news(tmp_path: Path, values):
    claude = FakeClaude(cwd=Path(FOLDER))
    _chat(claude)
    d = _dispatcher(tmp_path, claude)
    _write(d.projects_dir / f"{SESSION}.jsonl", [CLEAR, CROSS_SESSION])

    assert d._brain_is_live(TITLE) is False
    # the hold does not touch the away clock, its HH:MM, or the batch's sources
    assert d.away.held_since(TITLE) is None
    assert TITLE not in d._since_text
    assert TITLE not in d._batch_sources
    assert _held(d) == [f"brain chat {TITLE} just cleared, no owner message yet: news held"]

    assert d._brain_is_live(TITLE) is False  # the next tick: still held, no second line
    assert len(_held(d)) == 1


def test_b_the_owner_message_after_the_clear_releases_the_hold(tmp_path: Path, values):
    claude = FakeClaude(cwd=Path(FOLDER))
    _chat(claude)
    d = _dispatcher(tmp_path, claude)
    _write(d.projects_dir / f"{SESSION}.jsonl", [CLEAR, CROSS_SESSION, OWNER_HELLO])

    assert d._brain_is_live(TITLE) is True
    assert _held(d) == []


def test_c_a_row_without_session_id_is_live_as_today(tmp_path: Path, values):
    claude = FakeClaude(cwd=Path(FOLDER))
    _chat(claude, session_id=None)
    d = _dispatcher(tmp_path, claude)
    _write(d.projects_dir / f"{SESSION}.jsonl", [CLEAR, CROSS_SESSION])  # would hold if the row named it

    assert d._brain_is_live(TITLE) is True
    assert _held(d) == []


def test_d_a_new_hold_after_a_release_logs_once_more(tmp_path: Path, values):
    claude = FakeClaude(cwd=Path(FOLDER))
    _chat(claude)
    d = _dispatcher(tmp_path, claude)
    path = _write(d.projects_dir / f"{SESSION}.jsonl", [CLEAR, CROSS_SESSION])

    assert d._brain_is_live(TITLE) is False
    _write(path, [CLEAR, CROSS_SESSION, OWNER_HELLO])
    assert d._brain_is_live(TITLE) is True
    _write(path, [CLEAR, CROSS_SESSION])  # a new /clear: a new hold start, one more line
    assert d._brain_is_live(TITLE) is False
    assert len(_held(d)) == 2
