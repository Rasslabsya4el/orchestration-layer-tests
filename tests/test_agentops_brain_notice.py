"""Brain notice module: one message per observed batch, each (issue, state) once. Fakes only, no gh, no real send."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from tools.agentops.brain_notice import BrainNotifier, first_line, format_line

BRAIN = "Brain"


class FakeSender:
    def __init__(self, fail_times: int = 0) -> None:
        self.messages: list[tuple[str, str]] = []
        self.fail_times = fail_times
        self.calls = 0

    def __call__(self, title: str, message: str) -> None:
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("chat window gone")
        self.messages.append((title, message))


class Fakes:
    def __init__(self) -> None:
        self.infos: dict[int, tuple[str, str] | None] = {}
        self.autopilot: dict[int, str | None] = {}
        self.live = True
        self.logs: list[str] = []
        self.sender = FakeSender()

    def notifier(self, path) -> BrainNotifier:
        return BrainNotifier(
            state_path=path,
            brain_title=BRAIN,
            send=self.sender,
            issue_info=lambda n: self.infos.get(n),
            autopilot_live_for=lambda n: self.autopilot.get(n),
            brain_live=lambda: self.live,
            log=self.logs.append,
            now=lambda: datetime(2026, 10, 13, 9, 0, 0, tzinfo=timezone.utc),
        )


def _fakes_with_two(tmp_path_fakes: Fakes) -> None:
    tmp_path_fakes.infos[101] = ("Monitor card layout", "<!-- parked-on-run: x -->\nmore text")
    tmp_path_fakes.infos[102] = ("Tree export fix", "Closed by merge of the branch.\nsecond line")


def test_batch_of_two_is_one_message_with_one_line_each(tmp_path):
    f = Fakes()
    _fakes_with_two(f)
    notifier = f.notifier(tmp_path / "state.json")

    sent = notifier.observe({102: "closed", 101: "parked"})

    assert len(f.sender.messages) == 1
    title, message = f.sender.messages[0]
    assert title == BRAIN
    lines = message.split("\n")
    assert lines == [
        "#101 Monitor card layout: parked: parked-on-run: x",
        "#102 Tree export fix: closed: Closed by merge of the branch.",
    ]
    assert sent == lines
    assert f.logs == []


def test_same_state_is_not_sent_again_not_in_later_calls_nor_after_restart(tmp_path):
    f = Fakes()
    _fakes_with_two(f)
    path = tmp_path / "state.json"
    notifier = f.notifier(path)
    notifier.observe({101: "parked", 102: "closed"})
    assert len(f.sender.messages) == 1

    for _ in range(3):
        assert notifier.observe({101: "parked", 102: "closed"}) == []
    assert len(f.sender.messages) == 1

    restarted = f.notifier(path)
    assert restarted.observe({101: "parked", 102: "closed"}) == []
    assert len(f.sender.messages) == 1
    assert f.logs == []


def test_autopilot_live_skips_with_one_log_line_naming_it(tmp_path):
    f = Fakes()
    _fakes_with_two(f)
    f.autopilot[101] = "autopilot-780"
    notifier = f.notifier(tmp_path / "state.json")

    assert notifier.observe({101: "parked"}) == []
    assert f.sender.messages == []
    assert len(f.logs) == 1
    assert "autopilot-780" in f.logs[0] and "#101" in f.logs[0]
    # recorded as handled: the same state is not logged again
    assert notifier.observe({101: "parked"}) == []
    assert len(f.logs) == 1


def test_brain_not_live_sends_nothing_raises_nothing_logs_one_line(tmp_path):
    f = Fakes()
    _fakes_with_two(f)
    f.live = False
    notifier = f.notifier(tmp_path / "state.json")

    assert notifier.observe({101: "parked", 102: "closed"}) == []
    assert f.sender.messages == []
    assert f.sender.calls == 0
    assert len(f.logs) == 1

    # nothing was recorded, so once the brain is back the batch goes out once
    f.live = True
    assert len(notifier.observe({101: "parked", 102: "closed"})) == 2
    assert len(f.sender.messages) == 1


def test_parked_while_the_chat_is_away_is_kept_and_sent_on_the_first_live_call(tmp_path):
    """#946 row 4 at notifier level: a batch seen while the chat is away is not dropped; one log line per away period."""
    f = Fakes()
    _fakes_with_two(f)
    f.live = False
    notifier = f.notifier(tmp_path / "state.json")

    assert notifier.observe({101: "parked"}) == []
    assert notifier.observe({}) == []  # a later tick, the park unchanged, the chat still away
    assert f.sender.calls == 0
    assert len(f.logs) == 1 and "not live" in f.logs[0]

    f.live = True
    assert notifier.observe({}) == ["#101 Monitor card layout: parked: parked-on-run: x"]
    assert len(f.sender.messages) == 1
    assert notifier.observe({}) == []
    assert len(f.sender.messages) == 1


def test_failed_send_is_retried_once_then_dropped_never_a_third_time(tmp_path):
    f = Fakes()
    _fakes_with_two(f)
    f.sender = FakeSender(fail_times=2)
    notifier = f.notifier(tmp_path / "state.json")

    # first failure: observe returns normally, one log line
    assert notifier.observe({101: "parked"}) == []
    assert f.sender.calls == 1
    assert len(f.logs) == 1

    # the retry on a later call fails again: dropped with its own log line
    assert notifier.observe({101: "parked"}) == []
    assert f.sender.calls == 2
    assert len(f.logs) == 2
    assert "dropped" in f.logs[1]

    # a third call does not send it again
    assert notifier.observe({101: "parked"}) == []
    assert notifier.observe({}) == []
    assert f.sender.calls == 2
    assert f.sender.messages == []


def test_failed_send_then_successful_retry_sends_once(tmp_path):
    f = Fakes()
    _fakes_with_two(f)
    f.sender = FakeSender(fail_times=1)
    notifier = f.notifier(tmp_path / "state.json")

    assert notifier.observe({101: "parked"}) == []
    assert len(f.logs) == 1
    assert len(notifier.observe({})) == 1  # retry comes from the pending record, no outcome needed
    assert len(f.sender.messages) == 1
    assert notifier.observe({101: "parked"}) == []
    assert len(f.sender.messages) == 1


def test_state_change_parked_to_closed_is_notified_once_more(tmp_path):
    f = Fakes()
    _fakes_with_two(f)
    notifier = f.notifier(tmp_path / "state.json")

    assert notifier.observe({101: "parked"}) == ["#101 Monitor card layout: parked: parked-on-run: x"]
    assert notifier.observe({101: "closed"}) == ["#101 Monitor card layout: closed: parked-on-run: x"]
    assert len(f.sender.messages) == 2
    for _ in range(2):
        assert notifier.observe({101: "closed"}) == []
    assert len(f.sender.messages) == 2


def test_issue_info_none_leaves_issue_unrecorded_for_next_call(tmp_path):
    f = Fakes()
    notifier = f.notifier(tmp_path / "state.json")

    assert notifier.observe({103: "closed"}) == []
    assert f.sender.messages == []
    f.infos[103] = ("Late title", "Closed now")
    assert notifier.observe({103: "closed"}) == ["#103 Late title: closed: Closed now"]


def test_state_file_is_json_keyed_by_issue_number_and_written_atomically(tmp_path):
    f = Fakes()
    _fakes_with_two(f)
    path = tmp_path / "state.json"
    f.notifier(path).observe({101: "parked"})

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["101"]["state"] == "parked"
    assert data["101"]["done"] is True
    assert not (tmp_path / "state.json.tmp").exists()


def test_title_cut_at_60_and_first_line_cut_at_160():
    long_title = "T" * 80
    long_line = "x" * 300
    line = format_line(7, long_title, "closed", long_line)
    assert line == f"#7 {'T' * 60}: closed: {'x' * 160}"


def test_marker_brackets_stripped_and_marker_only_line_falls_to_next():
    assert first_line("<!-- parked-on-run: x -->\nnext") == "parked-on-run: x"
    assert first_line("<!-- -->\n\n  real first line\nmore") == "real first line"
    assert first_line("<!--   -->") == ""
    assert first_line("") == ""
    assert first_line(None) == ""
