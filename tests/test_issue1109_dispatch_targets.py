"""#1109 unit 2: the session-level messages (permission blocked, start refused) go to the brain chat of each issue's
area (`brain_chats`), each target once; a session holding no issue goes to the title.

Fakes only: the dispatcher's FakeClaude / FakeGh of test_agentops_dispatch, the sweep rows built by hand, the
settings patched on the dispatch module. No gh, no claude, no real clock.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, _dispatcher, _events, _issue
from tests.test_agentops_dispatch_permission_block import PENDING, _write
from tools.agentops.core_settings import load as load_settings
from tools.agentops.dispatch import BLOCKED_STATE, PERMISSION_WAIT, Dispatcher, IssueStatus

P = "Brain: PoB"          # brain_chat_title
O = "Оркестровый слой"    # the chat of area:orchestration
B = "Builds chat"         # the chat of area:builds
CHATS = {"area:orchestration": O, "area:builds": B}
READY = "ready-for-agent"


@pytest.fixture
def chats(monkeypatch):
    settings = dict(load_settings())
    settings.update(brain_chat_title=P, brain_chats=dict(CHATS), areas=[])  # #96: the target's `areas` must not decide
    monkeypatch.setattr("tools.agentops.dispatch._load_settings", lambda *a, **k: settings)
    return settings


def _holding_two_areas(tmp_path: Path):
    """A session started on issues #7 (area:orchestration) and #8 (area:builds), both claimed by the next tick."""
    claude, clock = FakeClaude(), FakeClock()
    gh = FakeGh(candidates=[_issue(7, labels=("area:orchestration",)), _issue(8, labels=("area:builds",))])
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)
    d.tick()
    live = claude.list_sessions()[0]["id"]
    gh.issues[7] = IssueStatus(number=7, open=True, labels=(READY, "area:orchestration"), assignees=("bot",))
    gh.issues[8] = IssueStatus(number=8, open=True, labels=(READY, "area:builds"), assignees=("bot",))
    clock.advance(30.0)
    d.tick()
    return claude, d, clock, live


def _stick(tmp_path: Path, claude: FakeClaude, live: str, clock: FakeClock, d: Dispatcher) -> None:
    """The live session's transcript ends in a pending tool call and its listing says it waits on a permission."""
    _write(tmp_path, claude._sessions[live]["sessionId"], PENDING)
    claude._sessions[live].update(state=BLOCKED_STATE, status="waiting", waitingFor=PERMISSION_WAIT)
    claude.waked.clear()
    clock.advance(30.0)
    d.tick()


def test_permission_block_holding_two_areas_one_message_to_each_chat(tmp_path, chats):
    claude, d, clock, live = _holding_two_areas(tmp_path)
    assert {7, 8} <= d.sessions[0].held

    _stick(tmp_path, claude, live, clock, d)

    reports = [target for target, message in claude.waked if PERMISSION_WAIT in message]
    assert sorted(reports) == sorted([O, B])  # one each, no duplicate, nothing to P
    blocked = [e["target"] for e in _events(tmp_path) if e["kind"] == "permission_blocked"]
    assert sorted(blocked) == sorted([O, B])


def test_permission_block_two_issues_of_one_area_is_one_message(tmp_path, chats):
    claude, clock = FakeClaude(), FakeClock()
    gh = FakeGh(candidates=[_issue(7, labels=("area:orchestration",)), _issue(9, labels=("area:orchestration",))])
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)
    d.tick()
    live = claude.list_sessions()[0]["id"]
    for n in (7, 9):
        gh.issues[n] = IssueStatus(number=n, open=True, labels=(READY, "area:orchestration"), assignees=("bot",))
    clock.advance(30.0)
    d.tick()

    _stick(tmp_path, claude, live, clock, d)

    assert [target for target, message in claude.waked if PERMISSION_WAIT in message] == [O]


def test_permission_block_holding_no_issue_goes_to_the_title(tmp_path, chats):
    claude, clock = FakeClaude(), FakeClock()
    gh = FakeGh(candidates=[_issue(7, labels=("area:orchestration",))])
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock)
    d.tick()
    live = claude.list_sessions()[0]["id"]  # offered #7, never claimed: holds nothing

    _stick(tmp_path, claude, live, clock, d)

    assert [target for target, message in claude.waked if PERMISSION_WAIT in message] == [P]


class RefusingClaude(FakeClaude):
    def start(self, offered, parked_by=None):
        raise RuntimeError("claude --bg failed: Workspace not trusted.")


def _refused(tmp_path: Path, candidates):
    claude = RefusingClaude()
    gh = FakeGh(candidates=candidates)
    d = _dispatcher(tmp_path, claude=claude, gh=gh)
    d.log = lambda text: None
    d.tick()
    return [target for target, message in claude.waked if "is refused by" in message]


def test_start_refused_with_two_mapped_areas_one_message_to_each_chat(tmp_path, chats):
    targets = _refused(tmp_path, [_issue(1, labels=("area:orchestration",)), _issue(2, labels=("area:builds",)),
                                  _issue(3, labels=("area:orchestration",))])
    assert sorted(targets) == sorted([O, B])  # #1 and #3 share O: O once


def test_start_refused_with_an_unmapped_area_goes_to_the_title(tmp_path, chats):
    targets = _refused(tmp_path, [_issue(1, labels=("area:runs",))])
    assert targets == [P]
