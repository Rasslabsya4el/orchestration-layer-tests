"""#946 rework (autopilot #780, session 201): the brain chat is the one the listing names, and a notice
kept while the chat is away is sent on the first tick the chat is live.

The name is read from this repo's own `config/orchestration.json` as it is; the listing entry is a copy of
the shape `claude agents --json --all` really prints for the brain chat (`kind` interactive, `name`,
`status`, NO `state` key). Fakes only: no real `claude`, no real `gh`, no real message.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from tests.test_agentops_dispatch import FakeClaude, FakeClock
from tests.test_agentops_dispatch_brain_notice import NoticeGh
from tools.agentops import core_settings
from tools.agentops.dispatch import Dispatcher, build_wake_command

ROOT = Path(__file__).resolve().parents[1]
SESSION = "orch1"

def _configured_title() -> str:
    """The brain chat name as this repo's config file states it (read as it is, no override)."""
    return str(core_settings.load(ROOT / "config" / "orchestration.json")["brain_chat_title"])


#: a copy of the listing entry of this repo's brain chat (`claude agents --json --all`): the name is the one the
#: config states, so the entry is the shape the listing really prints for this repo's brain chat.
REAL_BRAIN_ENTRY = {
    "kind": "interactive", "name": _configured_title(), "status": "idle", "pid": 24388,
    "cwd": "C:\\repos\\target-repo", "sessionId": "5f0c9a1e-2d7b-4c83-9e61-0b8a3f7d2c44",
    "startedAt": 1760363000000,
}
#: the brain chat of ANOTHER repo (other-repo), listed beside or instead of this one.
OTHER_BRAIN_ENTRY = {
    "kind": "interactive", "name": "Brain: other-repo", "status": "idle", "pid": 31175,
    "cwd": "C:\\repos\\other-repo", "sessionId": "a13d7e90-44c1-4f2b-8d5e-7c6b9e0f1a22",
    "startedAt": 1760363100000,
}


class ListingClaude(FakeClaude):
    """The fake `claude`: one ORCH session (adopted, claims held) plus whatever brain entries the test lists."""

    def __init__(self, *brain_entries: dict) -> None:
        super().__init__()
        self.add_session(SESSION, name="orch #1 #2", state="working", status="busy",
                         startedAt=int(time.time() * 1000) - 60_000)
        self.set_brain(*brain_entries)

    def set_brain(self, *entries: dict) -> None:
        """Replace the brain entries of the listing with `entries`, each as printed (no added keys)."""
        self._sessions = {k: v for k, v in self._sessions.items() if not k.startswith("brain-entry")}
        for i, entry in enumerate(entries):
            self._sessions[f"brain-entry-{i}"] = dict(entry)

    def notices_to(self, target: str) -> list[tuple[str, str]]:
        return [(t, m) for t, m in self.waked if t == target and m.startswith("#")]


def _dispatcher(tmp_path: Path, claude: ListingClaude, gh: NoticeGh) -> Dispatcher:
    """No `brain_title`: the dispatcher reads the name from the config, as production does."""
    d = Dispatcher(claude=claude, gh=gh, max_hours=4.0, events_file=tmp_path / "events.jsonl",
                   state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                   clock=FakeClock(), brain_notice_file=tmp_path / "brain_notices.json")
    d.log_lines = []
    d.log = d.log_lines.append
    return d


def _holding(tmp_path: Path, *brain_entries: dict) -> tuple[ListingClaude, NoticeGh, Dispatcher]:
    """A live ORCH session that holds #1 (adopted on tick 1, claim read on tick 2)."""
    claude = ListingClaude(*brain_entries)
    gh = NoticeGh()
    gh.titles = {1: "Fix the first thing"}
    d = _dispatcher(tmp_path, claude, gh)
    d.tick()
    gh.claim(1)
    d.tick()
    assert d.current is not None and d.current.held == {1}
    return claude, gh, d


def test_1_the_listing_entry_of_this_repos_brain_chat_is_live(tmp_path: Path):
    claude = ListingClaude(REAL_BRAIN_ENTRY)
    d = _dispatcher(tmp_path, claude, NoticeGh())

    assert _configured_title().startswith("Brain: ")
    assert d._brain_is_live(_configured_title()) is True


def test_2_another_repos_brain_chat_alone_is_never_this_one(tmp_path: Path):
    claude = ListingClaude(OTHER_BRAIN_ENTRY)
    d = _dispatcher(tmp_path, claude, NoticeGh())

    assert d._brain_is_live(_configured_title()) is False


def test_2_with_both_listed_only_this_repos_chat_counts(tmp_path: Path):
    claude = ListingClaude(OTHER_BRAIN_ENTRY, REAL_BRAIN_ENTRY)
    d = _dispatcher(tmp_path, claude, NoticeGh())

    assert d._brain_is_live(_configured_title()) is True
    claude.set_brain(OTHER_BRAIN_ENTRY)
    assert d._brain_is_live(_configured_title()) is False


def test_2_a_parked_issue_with_only_the_other_repos_chat_listed_is_sent_to_no_one(tmp_path: Path):
    claude, gh, d = _holding(tmp_path, OTHER_BRAIN_ENTRY)
    gh.newest = {1: "ORCH s: parked"}
    gh.park(1)

    d.tick()

    assert claude.notices_to(OTHER_BRAIN_ENTRY["name"]) == [] and claude.waked == []
    assert len([line for line in d.log_lines if "not live" in line]) == 1


def test_3_the_name_the_notice_checks_is_the_name_the_wake_sends_to(tmp_path: Path):
    claude, gh, d = _holding(tmp_path, REAL_BRAIN_ENTRY)
    gh.newest = {1: "ORCH s: parked on the owner's answer"}
    gh.park(1)

    d.tick()

    sent = claude.notices_to(REAL_BRAIN_ENTRY["name"])
    assert len(sent) == 1
    target, message = sent[0]
    assert target == _configured_title() == REAL_BRAIN_ENTRY["name"]
    # the wake command the dispatcher builds for that target names the same chat
    assert f'to={json.dumps(target, ensure_ascii=False)}' in build_wake_command(target, message)[-1]


def test_4_a_park_while_the_chat_is_away_is_sent_once_on_the_first_live_tick(tmp_path: Path):
    claude, gh, d = _holding(tmp_path)
    gh.newest = {1: "ORCH s: parked on the owner's answer"}
    gh.park(1)

    d.tick()  # the chat is not listed: nothing sent, the notice is kept
    d.tick()  # still not listed
    assert claude.waked == []

    claude.set_brain(REAL_BRAIN_ENTRY)  # the chat comes up
    d.tick()  # the first tick the chat is live
    assert len(claude.notices_to(REAL_BRAIN_ENTRY["name"])) == 1

    for _ in range(3):
        d.tick()
    assert len(claude.notices_to(REAL_BRAIN_ENTRY["name"])) == 1
    assert len(claude.waked) == 1
