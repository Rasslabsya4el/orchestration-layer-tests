"""#1105 unit 1: the dispatcher never wakes the chat brain for an autopilot goal. Seams: the terms
parser (`parse_autopilot_terms`), the stuck path (a bad-deadline terms block ticked through
`Dispatcher.tick`), and the quiet deadline path (a tick at the deadline). The fake `claude` counts
`wake` calls; the label, comment, feed event and log line must still happen."""

from __future__ import annotations

from datetime import datetime, timezone

from tests.test_agentops_dispatch_autopilot import (
    FakeClaude,
    FakeGh,
    FakeTimestamp,
    _dispatcher,
    _events,
    _terms_body,
)
from tools.agentops.autopilot import (
    AUTOPILOT_STUCK_LABEL,
    parse_autopilot_terms,
)


def test_a_terms_block_without_a_chat_line_parses_cleanly():
    terms = parse_autopilot_terms(_terms_body(scope="#63", deadline="2026-09-28T09:00:00+00:00"))
    assert terms.parse_error is None
    assert terms.chat is None
    assert terms.scope == "#63"


def test_a_chat_line_present_is_kept_but_ignored_by_the_parser():
    terms = parse_autopilot_terms(_terms_body(scope="#63", chat="brain"))
    assert terms.parse_error is None
    assert terms.chat == "brain"


def test_b_stuck_goal_with_a_chat_line_is_labelled_and_commented_but_never_woken(tmp_path):
    claude = FakeClaude()
    gh = FakeGh()
    gh.autopilot_goal(150)
    gh.set_terms(150, [_terms_body(deadline="tomorrow 9", chat="brain")])
    messages: list[str] = []
    d = _dispatcher(tmp_path, claude, gh)
    d.log = messages.append

    d.tick()

    assert (150, AUTOPILOT_STUCK_LABEL) in gh.added_labels
    assert gh.comment_calls and "deadline: tomorrow 9" in gh.comment_calls[-1][1]
    assert any(e["kind"] == "autopilot_stuck" for e in _events(tmp_path))
    assert any("deadline: tomorrow 9" in m for m in messages)
    assert claude.wake_calls == []
    assert not any(e["kind"] == "autopilot_wake" for e in _events(tmp_path))


def test_b_quiet_deadline_with_a_chat_line_logs_past_deadline_and_never_wakes(tmp_path):
    claude = FakeClaude()
    gh = FakeGh()
    gh.autopilot_goal(151)
    gh.set_terms(151, [_terms_body(deadline="2026-09-28T09:00:00+00:00", quiet="yes", chat="brain")])
    ts = FakeTimestamp(datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc))
    messages: list[str] = []
    d = _dispatcher(tmp_path, claude, gh, timestamp=ts)
    d.log = messages.append

    d.tick()
    d.tick()

    assert claude.wake_calls == []
    assert claude.autopilot_started == [] and 151 not in d.autopilots
    assert sum("autopilot #151 past deadline" in m for m in messages) == 1
    assert not any(e["kind"] == "autopilot_wake" for e in _events(tmp_path))
