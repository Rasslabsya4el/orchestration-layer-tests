"""#209 unit 6: after an unblocker's `<!-- brain-answer -->` the area's brain chat gets exactly ONE line.

Through `Dispatcher` with its fakes (`FakeClaude`, a `FakeGh` that serves the thread of a ticket): the notifier of
`brain_notice.py` is the real one, with its real store; no real `claude`, no real `gh`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.test_agentops_brain_notice import Fakes
from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh
from tests._area_rule import _no_area_rule  # noqa: F401
from tools.agentops.brain_notice import IssueRead
from tools.agentops.core_settings import load as load_settings
from tools.agentops.dispatch import Dispatcher, IssueStatus, IssueSummary, READY_LABEL

T0 = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
SB, BA, MR = "<!-- send-back -->", "<!-- brain-answer -->", "<!-- merge result -->"
RED = f"{MR}\nMerge of orch-195 did not land (red-suite) after 40 min in line and a suite of 36 min, 1 failed."
AREA = "area:orchestration"
TITLE = "Brain: Main"
CHAT = "Orchestration chat"
WAY = "The suite fails on test_foo because the fixture is stale; rebase onto main and regenerate it."
GOING_ON = f"{BA}\n## Loop stop\nthree attempts, one reason: red suite.\n\n## The way\n{WAY}\n2. then run the suite.\n"
ASKING = (f"{BA}\n## Loop stop\nthree attempts.\n\n## The way\nfor the owner: keep the old export format "
          f"(breaks two readers) or drop it (frees the module)?\n")


def _c(body: str, hour: int, minute: int = 0) -> dict:
    return {"id": f"IC_{hour:02d}{minute:02d}", "createdAt": f"2026-10-19T{hour:02d}:{minute:02d}:00Z", "body": body}


FAILED = [_c(f"{SB}\nnot accepted", 6), _c(RED, 7), _c(f"{SB}\nnot accepted", 8)]


class TalkGh(FakeGh):
    """The thread of each ticket is served whole; `reads` lists every thread read."""

    def __init__(self) -> None:
        super().__init__()
        self.threads: dict[int, list[dict]] = {}
        self.ready: set[int] = set()
        self.reads: list[int] = []

    def ready_for_agent_issues(self) -> list[IssueSummary]:
        return [IssueSummary(number=n, created_at=f"2026-10-01T00:00:0{n % 10}Z", labels=(READY_LABEL, AREA),
                             comments=tuple((c["createdAt"], c["body"]) for c in self.threads[n]))
                for n in sorted(self.ready)]

    def issue_status(self, number: int):
        return IssueStatus(number=number, open=True, labels=(AREA,), assignees=())

    def parent_issue(self, number: int):
        return None

    def issue_comments(self, number: int):
        self.reads.append(number)
        return list(self.threads.get(number, []))

    def issue_title_and_last_comment(self, number: int):
        thread = self.threads.get(number, [])
        last = thread[-1] if thread else {}
        return IssueRead(f"Title {number}", str(last.get("body") or ""), last.get("id"), None, (AREA,))


@pytest.fixture(autouse=True)
def chats(monkeypatch):
    settings = dict(load_settings())
    settings.update(brain_chat_title=TITLE, brain_chats={AREA: CHAT}, areas=[])
    monkeypatch.setattr("tools.agentops.dispatch._load_settings", lambda *a, **k: settings)


class World:
    """One dispatcher over a state dir; `restart()` builds another over the same stores."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.now = T0
        self.clock = FakeClock()
        self.claude = FakeClaude()
        self.claude.add_session("area-chat", name=CHAT, kind="interactive", status="idle")
        self.claude.add_session("title-chat", name=TITLE, kind="interactive", status="idle")
        self.gh = TalkGh()
        self.d = self.restart()

    def restart(self) -> Dispatcher:
        d = Dispatcher(claude=self.claude, gh=self.gh, max_hours=4.0, events_file=self.tmp / "events.jsonl",
                       orch_offer_max=99, state_file=self.tmp / "orch_session.json", projects_dir=self.tmp / "p",
                       clock=self.clock, unblocker_start_file=self.tmp / "unblocker_starts.json",
                       brain_notice_file=self.tmp / "brain_notices.json", brain_title=TITLE)
        d.timestamp = lambda: self.now
        d.log_lines = []
        d.log = d.log_lines.append
        self.d = d
        return d

    def tick(self, minutes: float = 1.0) -> None:
        self.clock.advance(minutes * 60)
        self.now += timedelta(minutes=minutes)
        self.d.tick()

    def start_unblocker_on(self, number: int = 195) -> None:
        self.gh.threads[number] = list(FAILED)
        self.gh.ready = {number}
        self.tick(0)
        assert [n for n, _ in self.claude.unblockers] == [number]
        self.gh.ready = set()  # the unblocker has the ticket now

    def told(self) -> list[tuple[str, str]]:
        return [(t, m) for t, m in self.claude.waked if m.startswith("#")]


def test_the_answer_that_goes_on_is_one_line_to_the_area_chat_then_nothing_more(tmp_path: Path):
    w = World(tmp_path)
    w.start_unblocker_on()
    w.tick(1)
    assert w.told() == []  # no answer yet

    w.gh.threads[195].append(_c(GOING_ON, 12, 20))
    w.tick(20)
    for _ in range(3):
        w.tick(1)

    assert w.told() == [(CHAT, f"#195: {WAY}")]
    assert len(w.told()[0][1].splitlines()) == 1


def test_a_restarted_dispatcher_over_the_same_stores_sends_nothing_more(tmp_path: Path):
    w = World(tmp_path)
    w.start_unblocker_on()
    w.gh.threads[195].append(_c(GOING_ON, 12, 20))
    w.tick(20)
    assert len(w.told()) == 1

    w.restart()
    for _ in range(3):
        w.tick(1)

    assert len(w.told()) == 1


def test_a_for_the_owner_answer_says_the_unblocker_asks_the_owner(tmp_path: Path):
    w = World(tmp_path)
    w.start_unblocker_on()
    w.gh.threads[195].append(_c(ASKING, 12, 20))
    w.tick(20)
    w.tick(1)

    assert w.told() == [(CHAT, "#195: asks the owner")]


def test_a_long_way_is_cut_at_160_characters(tmp_path: Path):
    w = World(tmp_path)
    w.start_unblocker_on()
    w.gh.threads[195].append(_c(f"{BA}\n## Loop stop\nx\n\n## The way\n{'word ' * 80}\n", 12, 20))
    w.tick(20)

    (_, line), = w.told()
    assert line.startswith("#195: word word") and len(line) <= len("#195: ") + 160


def test_a_brain_answer_on_a_ticket_with_no_start_record_tells_nothing_and_reads_nothing(tmp_path: Path):
    w = World(tmp_path)
    w.gh.threads[77] = [_c(GOING_ON, 12, 20)]
    w.gh.ready = {77}
    for _ in range(3):
        w.tick(1)

    assert w.told() == [] and w.gh.reads == []


def test_only_the_ticket_with_a_start_record_is_read(tmp_path: Path):
    w = World(tmp_path)
    w.gh.threads[40] = [_c(GOING_ON, 12, 20)]
    w.start_unblocker_on(195)
    w.gh.threads[195].append(_c(GOING_ON, 12, 20))
    w.tick(20)
    w.tick(1)

    assert set(w.gh.reads) == {195} and len(w.told()) == 1


def test_a_brain_answer_older_than_the_start_is_not_this_triggers_answer(tmp_path: Path):
    w = World(tmp_path)
    w.gh.threads[195] = [_c(GOING_ON, 5)] + FAILED  # an old answer, then the failures that start a new unblocker
    w.gh.ready = {195}
    w.tick(0)
    w.gh.ready = set()
    for _ in range(3):
        w.tick(1)

    assert w.told() == []


def test_a_later_second_answer_after_a_new_trigger_is_a_new_line(tmp_path: Path):
    w = World(tmp_path)
    w.start_unblocker_on()
    w.gh.threads[195].append(_c(GOING_ON, 12, 20))
    w.tick(20)
    # three more failed attempts, a second unblocker, a second answer
    w.gh.threads[195] += [_c(f"{SB}\nagain", 13), _c(f"{SB}\nagain", 13, 5), _c(f"{SB}\nagain", 13, 10)]
    w.gh.ready = {195}
    w.now = T0 + timedelta(hours=1, minutes=15)
    w.tick(0)
    assert len(w.claude.unblockers) == 2
    w.gh.ready = set()
    w.gh.threads[195].append(_c(ASKING, 13, 30))
    w.tick(15)
    w.tick(1)

    assert [m for _, m in w.told()] == [f"#195: {WAY}", "#195: asks the owner"]


def test_a_start_with_no_answer_after_30_minutes_logs_one_limbo_line_and_never_restarts(tmp_path: Path):
    w = World(tmp_path)
    w.start_unblocker_on()
    for _ in range(25):
        w.tick(1)
    assert not [x for x in w.d.log_lines if "ended without a brain-answer" in x]

    for _ in range(10):
        w.tick(1)
    assert len([x for x in w.d.log_lines if "ended without a brain-answer" in x]) == 1
    w.restart()
    for _ in range(3):
        w.tick(1)

    # the restarted dispatcher's log: the record keeps that the line was written once
    assert [x for x in w.d.log_lines if "ended without a brain-answer" in x] == []
    assert len(w.claude.unblockers) == 1


def test_the_limbo_line_text_and_count_in_one_dispatcher(tmp_path: Path):
    w = World(tmp_path)
    w.start_unblocker_on()
    for _ in range(40):
        w.tick(1)

    assert [x for x in w.d.log_lines if "ended without a brain-answer" in x] == [
        "unblocker for #195 ended without a brain-answer"]
    assert len(w.claude.unblockers) == 1


def test_an_answer_after_the_limbo_line_is_neither_read_nor_told(tmp_path: Path):
    """#222: the limbo line closes the record: no comment read after it, so a later brain-answer tells nothing."""
    w = World(tmp_path)
    w.start_unblocker_on()
    for _ in range(40):
        w.tick(1)
    reads = w.gh.reads.count(195)
    w.gh.threads[195].append(_c(GOING_ON, 12, 50))
    for _ in range(12):
        w.tick(1)

    assert w.gh.reads.count(195) == reads
    assert [m for _, m in w.told()] == ["#195: the unblocker wrote no way in 30 min; the ticket stays stopped "
                                        "until a brain-answer"]


def test_the_notifier_holds_the_line_while_its_chat_is_away_and_sends_it_once_when_back(tmp_path: Path):
    f = Fakes()
    f.infos[9] = IssueRead("T", "x", "c1", None, ())
    f.live = False
    n = f.notifier(tmp_path / "n.json")
    n.observe({9: "unblocked"}, lines={9: ("k1", "#9: the way")})
    assert f.sender.messages == []

    f.live = True
    restarted = f.notifier(tmp_path / "n.json")
    assert restarted.observe({}) == ["#9: the way"]
    assert restarted.observe({9: "unblocked"}, lines={9: ("k1", "#9: the way")}) == []
    assert restarted.observe({9: "unblocked"}, lines={9: ("k2", "#9: asks the owner")}) == ["#9: asks the owner"]
    assert [m for _, m in f.sender.messages] == ["#9: the way", "#9: asks the owner"]
    assert json.loads((tmp_path / "n.json").read_text())["9"]["key"] == "k2"
