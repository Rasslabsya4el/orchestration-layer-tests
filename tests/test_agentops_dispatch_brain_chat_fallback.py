"""#69 unit 2: the dispatcher finds its repo's brain chat when the owner renamed it.

Through `Dispatcher` with its fakes (no real `claude`, no real `gh`, no real message): the notifier of
`brain_notice.py` is the real one, its seams are the dispatcher's own. Rows 1-4 of the issue's acceptance,
the alias, and the repo == home case.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh
from tools.agentops.dispatch import Dispatcher, IssueStatus

#: the title the repo's settings name (the chat was called this at port time; the owner renamed it)
TITLE = "привет"
#: this repo's folder as the dispatcher's `claude` runs in it
FOLDER = "C:\\Coding\\Target repo"
REPO = "owner/target"
HOME = "owner/home"
HOME_TITLE = "Brain: orch layer"
HERMES = "Brain: Hermes"
#: the wall clock at the fake clock's start (1000.0); `_hhmm` converts a fake clock value the way the dispatcher does
WALL_START = datetime(2026, 10, 16, 18, 7, tzinfo=timezone.utc)
CLOCK_START = 1000.0
HOLD = 30 * 60


def _hhmm(clock_value: float) -> str:
    """The human clock of a fake clock value, as the dispatcher writes it (local time, HH:MM)."""
    return (WALL_START + timedelta(seconds=clock_value - CLOCK_START)).astimezone().strftime("%H:%M")


class ChatClaude(FakeClaude):
    """The fake `claude` with interactive chats in a folder (no `state` key, the shape of the real entry)."""

    def __init__(self, folder: str = FOLDER) -> None:
        super().__init__(cwd=Path(folder))

    def chat(self, session_id: str, name: str, *, cwd: str = FOLDER, kind: str = "interactive") -> None:
        self.add_session(session_id, name=name, kind=kind, cwd=cwd, status="idle")

    def remove(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def notices(self, target: str) -> list[str]:
        return [message for chat, message in self.waked if chat == target]


class ChatGh(FakeGh):
    """The fake `gh`: the two issue reads of the notifier, and the home repo's config read (`gh api`)."""

    def __init__(self) -> None:
        super().__init__()
        self.titles: dict[int, str] = {}
        self.newest: dict[int, str] = {}
        self.parents: dict[int, int] = {}
        self.home_title: str | None = HOME_TITLE
        self.runner_calls: list[list[str]] = []

    def issue_title_and_last_comment(self, number: int):
        return self.titles.get(number, f"Title {number}"), self.newest.get(number, "")

    def parent_issue(self, number: int):
        return self.parents.get(number)

    def issue_status(self, number: int) -> IssueStatus:
        """Every issue is open and unlabelled (the autopilot look of the notifier reads it)."""
        return IssueStatus(number=number, open=True, labels=(), assignees=())

    def home_runner(self, args: list[str]):
        """The `gh` runner the home title is read through: one call, the raw config JSON."""
        self.runner_calls.append(list(args))
        body = {"brain_chat_title": self.home_title} if self.home_title is not None else {}
        return SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr="")


class Wall:
    """The wall clock of the fake clock: the same seconds, as a datetime."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock

    def __call__(self) -> datetime:
        return WALL_START + timedelta(seconds=self.clock() - CLOCK_START)


@pytest.fixture
def values(monkeypatch):
    """The settings the dispatcher reads (repo, home, title, aliases); a test changes the dict to change them."""
    values = {"repo": REPO, "home": HOME, "brain_chat_title": TITLE, "brain_chats": {},
              "brain_chat_title_aliases": [], "loss_note_area": ""}
    monkeypatch.setattr("tools.agentops.dispatch._load_settings", lambda *a, **k: values)
    return values


def _dispatcher(tmp_path: Path, claude: ChatClaude, gh: ChatGh) -> Dispatcher:
    clock = FakeClock(CLOCK_START)
    d = Dispatcher(claude=claude, gh=gh, max_hours=4.0, events_file=tmp_path / "events.jsonl",
                   state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                   clock=clock, timestamp=Wall(clock),
                   brain_notice_file=tmp_path / "brain_notices.json", brain_title=TITLE,
                   home_gh_runner=gh.home_runner)
    d.log_lines = []
    d.log = d.log_lines.append
    return d


def _lines(d: Dispatcher, text: str) -> list[str]:
    return [line for line in d.log_lines if text in line]


def test_row1_a_renamed_chat_in_the_folder_gets_the_news_and_one_fallback_line(tmp_path: Path, values):
    claude = ChatClaude()
    claude.chat("hermes", HERMES, cwd="c:/coding/target repo/")  # the same folder, other slashes and case
    gh = ChatGh()
    d = _dispatcher(tmp_path, claude, gh)
    notifier = d._build_brain_notifier(tmp_path / "brain_notices.json", TITLE)

    assert d._brain_is_live(TITLE) is True
    notifier.observe({1: "parked"})
    notifier.observe({2: "parked"})

    assert claude.notices(TITLE) == []
    assert [m.splitlines()[0][:3] for m in claude.notices(HERMES)] == ["#1 ", "#2 "]
    fallback = _lines(d, "brain chat: no chat named")
    assert fallback == [f"brain chat: no chat named {TITLE}; using {HERMES} (the one chat in the repo folder)"]
    assert gh.runner_calls == []


def test_row2_two_chats_in_the_folder_hold_the_news_then_one_home_alert_per_batch(tmp_path: Path, values):
    claude = ChatClaude()
    claude.chat("alpha", "Brain: Alpha")
    claude.chat("beta", "Brain: Beta")
    gh = ChatGh()
    d = _dispatcher(tmp_path, claude, gh)
    notifier = d._build_brain_notifier(tmp_path / "brain_notices.json", TITLE)
    home_text = f"{REPO}: brain chat {TITLE} not found (2 chats in the folder); 1 news held since {_hhmm(CLOCK_START)}"

    notifier.observe({1: "parked"})
    assert claude.waked == [] and _lines(d, "not live")

    d.clock.advance(HOLD - 1)
    notifier.observe({})
    assert claude.waked == []  # 29 min 59 s: not yet

    d.clock.advance(1)
    notifier.observe({})
    assert claude.waked == [(HOME_TITLE, home_text)]
    assert gh.runner_calls == [["api", f"repos/{HOME}/contents/config/orchestration.json",
                                "-H", "Accept: application/vnd.github.raw"]]

    d.clock.advance(HOLD)
    notifier.observe({})
    assert len(claude.waked) == 1  # the same batch is alerted once

    claude.chat("exact", TITLE)  # the chat is back under its title: the held news goes there
    notifier.observe({})
    assert [m.splitlines()[0][:3] for m in claude.notices(TITLE)] == ["#1 "]
    assert claude.notices("Brain: Alpha") == [] and claude.notices(HOME_TITLE) == [home_text]

    claude.remove("exact")  # missing again: a new batch, its own alert after its own 30 minutes
    notifier.observe({2: "parked"})
    d.clock.advance(HOLD)
    notifier.observe({})
    second = f"{REPO}: brain chat {TITLE} not found (2 chats in the folder); 1 news held since {_hhmm(CLOCK_START + 2 * HOLD)}"
    assert claude.notices(HOME_TITLE) == [home_text, second]


def test_row3_the_exact_title_live_is_todays_behaviour_with_no_fallback_line(tmp_path: Path, values):
    claude = ChatClaude()
    claude.chat("exact", TITLE)
    claude.chat("hermes", HERMES)  # a second chat in the folder does not matter: the exact name wins
    d = _dispatcher(tmp_path, claude, ChatGh())

    assert d._brain_is_live(TITLE) is True
    assert d._brain_wake_name(TITLE) == TITLE
    assert _lines(d, "brain chat: no chat named") == []


def test_row4_a_chat_in_another_repos_folder_never_counts(tmp_path: Path, values):
    claude = ChatClaude()
    claude.chat("other", "Brain: Other", cwd="C:\\Coding\\Some other repo")
    d = _dispatcher(tmp_path, claude, ChatGh())

    assert d._brain_is_live(TITLE) is False
    assert d._brain_wake_name(TITLE) == TITLE
    assert _lines(d, "brain chat: no chat named") == []


def test_alias_live_is_woken_by_the_alias(tmp_path: Path, values):
    values["brain_chat_title_aliases"] = ["Brain: Alias"]
    claude = ChatClaude()
    claude.chat("alias", "Brain: Alias", cwd="C:\\Coding\\Some other repo")  # the alias needs no folder
    claude.chat("hermes", HERMES)  # the folder chat is the later fallback
    d = _dispatcher(tmp_path, claude, ChatGh())

    assert d._brain_is_live(TITLE) is True
    d._send_to_brain(TITLE, "#1 Some title: closed: done\n")

    assert claude.notices("Brain: Alias") == ["#1 Some title: closed: done\n"]
    assert claude.notices(HERMES) == [] and claude.notices(TITLE) == []
    assert _lines(d, "brain chat: no chat named") == []


def test_repo_is_home_the_alert_is_logged_and_nobody_is_woken(tmp_path: Path, values):
    values["home"] = values["repo"]
    claude = ChatClaude()
    gh = ChatGh()
    d = _dispatcher(tmp_path, claude, gh)
    text = f"{REPO}: brain chat {TITLE} not found (0 chats in the folder); 1 news held since {_hhmm(CLOCK_START)}"

    assert d._brain_is_live(TITLE, "issue notice") is False
    d.clock.advance(HOLD)
    assert d._brain_is_live(TITLE, "issue notice") is False

    assert claude.waked == [] and gh.runner_calls == []
    assert _lines(d, text) == [text]


def test_an_area_chat_that_is_not_live_falls_back_to_the_repos_own_chat(tmp_path: Path, values):
    """Rule 3: the area title has no chat; the repo's own title is live next to a second chat in the folder."""
    area = "Область"
    claude = ChatClaude()
    claude.chat("own", TITLE)
    claude.chat("other", "Another chat")
    d = _dispatcher(tmp_path, claude, ChatGh())
    assert d._brain_is_live(area) is True
    assert d._brain_wake_name(area) == TITLE
    assert d._brain_wake_name(TITLE) == TITLE
