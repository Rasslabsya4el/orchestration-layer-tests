"""#951: the brain chat is told of every park of a ticket (its parking checkpoint), not only the first, and the
wake prompt carries the message so that no character of it can end it. Fakes only: no real `gh`, no real send."""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_agentops_brain_notice import Fakes
from tests.test_agentops_dispatch import FakeClock
from tests.test_agentops_dispatch_brain_notice import BRAIN, NoticeClaude, NoticeGh
from tools.agentops.dispatch import build_wake_command, build_wake_prompt

# ----- the notifier alone (rows 1-4) ----------------------------------------------------------------------------


def _info(f: Fakes, number: int, text: str, park: str | None, close: str | None = None) -> None:
    f.infos[number] = ("Long ticket", text, park, close)


def test_row1_parked_twice_with_two_checkpoints_is_two_messages_the_second_with_its_own_line(tmp_path):
    f = Fakes()
    notifier = f.notifier(tmp_path / "state.json")
    _info(f, 885, "ORCH s1: parked on a run", "IC_11")
    assert notifier.observe({885: "parked"}) == ["#885 Long ticket: parked: ORCH s1: parked on a run"]

    _info(f, 885, "ORCH s2: parked on the owner", "IC_13")
    assert notifier.observe({885: "parked"}) == ["#885 Long ticket: parked: ORCH s2: parked on the owner"]

    assert len(f.sender.messages) == 2
    assert json.loads((tmp_path / "state.json").read_text())["885"]["key"] == "IC_13"


def test_row2_the_same_park_over_three_ticks_and_a_restart_is_no_further_message(tmp_path):
    f = Fakes()
    path = tmp_path / "state.json"
    notifier = f.notifier(path)
    _info(f, 885, "ORCH s1: parked", "IC_11")
    notifier.observe({885: "parked"})
    for _ in range(3):
        assert notifier.observe({885: "parked"}) == []
    assert notifier.observe({885: "parked"}) == []
    assert f.notifier(path).observe({885: "parked"}) == []  # state re-read from disk
    assert len(f.sender.messages) == 1


def test_row3_parked_closed_reopened_parked_again_is_three_messages(tmp_path):
    f = Fakes()
    notifier = f.notifier(tmp_path / "state.json")
    _info(f, 7, "ORCH s1: parked", "IC_1")
    notifier.observe({7: "parked"})
    _info(f, 7, "Closed: merged", "IC_2", "2026-10-13T10:00:00Z")
    notifier.observe({7: "closed"})
    _info(f, 7, "ORCH s3: parked again", "IC_3")
    notifier.observe({7: "parked"})
    assert len(f.sender.messages) == 3


def test_closed_reopened_closed_again_is_two_messages_one_close_stays_one(tmp_path):
    f = Fakes()
    notifier = f.notifier(tmp_path / "state.json")
    _info(f, 7, "Closed: merged", "IC_2", "2026-10-13T10:00:00Z")
    notifier.observe({7: "closed"})
    assert notifier.observe({7: "closed"}) == []
    _info(f, 7, "Closed: merged again", "IC_5", "2026-10-13T12:00:00Z")
    assert len(notifier.observe({7: "closed"})) == 1
    assert len(f.sender.messages) == 2


def test_row4_an_old_shape_record_on_the_same_park_sends_nothing_after_the_restart(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"885": {"state": "parked", "done": True}}), encoding="utf-8")
    f = Fakes()
    notifier = f.notifier(path)
    _info(f, 885, "ORCH s1: parked", "IC_11")

    assert notifier.observe({885: "parked"}) == []
    assert notifier.observe({885: "parked"}) == []
    assert f.sender.messages == []
    # the park it read is now its key, so the NEXT park of that ticket is told
    _info(f, 885, "ORCH s2: parked", "IC_13")
    assert len(notifier.observe({885: "parked"})) == 1


def test_a_failed_read_of_a_done_issue_sends_nothing_and_a_two_tuple_info_keeps_the_old_rule(tmp_path):
    f = Fakes()
    notifier = f.notifier(tmp_path / "state.json")
    f.infos[1] = ("Plain", "ORCH: parked")  # no ids: the word alone
    notifier.observe({1: "parked"})
    assert notifier.observe({1: "parked"}) == []
    f.infos[1] = None
    assert notifier.observe({1: "parked"}) == []
    assert len(f.sender.messages) == 1


def test_a_pending_retry_after_a_failed_send_still_sends_once_with_its_key(tmp_path):
    f = Fakes()
    f.sender.fail_times = 1
    notifier = f.notifier(tmp_path / "state.json")
    _info(f, 9, "ORCH s: parked", "IC_9")
    notifier.observe({9: "parked"})
    assert len(notifier.observe({})) == 1
    assert notifier.observe({9: "parked"}) == []
    assert len(f.sender.messages) == 1


# ----- through the dispatcher (row 1 at tick level) -------------------------------------------------------------


class KeyedGh(NoticeGh):
    """The fake `gh` whose newest comment has an id: what `GhCli.issue_title_and_last_comment` now returns."""

    def __init__(self) -> None:
        super().__init__()
        self.park_ids: dict[int, str] = {}

    def issue_title_and_last_comment(self, number: int):
        return (self.titles.get(number, f"Title {number}"), self.newest.get(number, ""),
                self.park_ids.get(number), None)


def _keyed_holding(tmp_path: Path):
    from tools.agentops.dispatch import Dispatcher

    claude = NoticeClaude()
    gh = KeyedGh()
    gh.titles = {1: "Fix the first thing", 2: "Add the second thing"}
    d = Dispatcher(claude=claude, gh=gh, max_hours=4.0, events_file=tmp_path / "events.jsonl",
                   state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                   clock=FakeClock(), brain_notice_file=tmp_path / "brain_notices.json", brain_title=BRAIN)
    d.log_lines = []
    d.log = d.log_lines.append
    d.tick()
    gh.claim(1)
    gh.claim(2)
    d.tick()
    assert d.current is not None and d.current.held == {1, 2}
    return claude, gh, d


def test_row1_through_the_dispatcher_the_second_park_is_told_in_its_own_tick(tmp_path):
    claude, gh, d = _keyed_holding(tmp_path)
    gh.newest = {1: "ORCH s1: parked on a run"}
    gh.park_ids = {1: "IC_11"}
    gh.park(1)
    d.tick()
    assert claude.notices() == ["#1 Fix the first thing: parked: ORCH s1: parked on a run"]

    d.tick()
    d.tick()
    assert len(claude.notices()) == 1

    gh.claim(1)  # taken again
    d.tick()
    gh.newest = {1: "ORCH s2: parked on the owner"}
    gh.park_ids = {1: "IC_13"}
    gh.park(1)
    d.tick()

    assert claude.notices() == ["#1 Fix the first thing: parked: ORCH s1: parked on a run",
                                "#1 Fix the first thing: parked: ORCH s2: parked on the owner"]
    for _ in range(3):
        d.tick()
    assert len(claude.notices()) == 2


# ----- the wake prompt (row 5) ----------------------------------------------------------------------------------

_NASTY_TITLE = 'Fix "quoted" `tick` 100% it\'s'
_NASTY_LINE = 'ORCH s: parked "x" `y` %z% \'w\' \\ back\\"slash'


def _parse_back(prompt: str, name: str) -> str:
    """The form's own rule: a line `name=<JSON string>`; the value is the JSON string, decoded."""
    lines = [ln for ln in prompt.split("\n") if ln.startswith(name + "=")]
    assert len(lines) == 1
    return json.loads(lines[0][len(name) + 1:])


def test_row5_a_message_with_quotes_backticks_percent_and_apostrophes_parses_back_byte_for_byte():
    message = f"#951 {_NASTY_TITLE}: parked: {_NASTY_LINE}\n#952 Второй: closed: готово \"да\""
    prompt = build_wake_prompt('Brain "chat"', message)

    assert _parse_back(prompt, "message") == message
    assert _parse_back(prompt, "to") == 'Brain "chat"'
    assert build_wake_command("Brain", message)[-1] == build_wake_prompt("Brain", message)


def test_row5_a_notice_built_from_such_a_title_and_comment_reaches_the_prompt_whole():
    from tools.agentops.brain_notice import format_line

    line = format_line(951, _NASTY_TITLE, "parked", _NASTY_LINE)
    assert _parse_back(build_wake_prompt("Brain", line), "message") == line


def test_the_real_read_returns_the_newest_comments_id_and_the_close_time_in_the_one_gh_call():
    import subprocess

    from tools.agentops.dispatch import GhCli

    calls: list[list[str]] = []
    payload = {"title": "T", "closedAt": "2026-10-13T10:00:00Z",
               "comments": [{"id": "IC_1", "body": "old"}, {"id": "IC_2", "body": "<!-- parked -->\nx"}]}

    def runner(args, stdin=None):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    gh = GhCli(repo="o/r", runner=runner)
    assert gh.issue_title_and_last_comment(5) == ("T", "<!-- parked -->\nx", "IC_2", "2026-10-13T10:00:00Z")
    assert len(calls) == 1
    payload.update(comments=[], closedAt=None)
    assert gh.issue_title_and_last_comment(5) == ("T", "", None, None)
