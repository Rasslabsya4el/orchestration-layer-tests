"""#1121 unit 6: the doubling line of the daily note, with fakes only (no gh, no claude, no real thread, no real clock).

Seams under test: `doubling_lines` (pure), `LossesDaily.check()` over its injected `compute`, `send`, `brain_live`,
`log`, `now`, a `runner` (synchronous, or deferred so a test starts the jobs by hand) and the state file path (a new
object over the same file is the restart); the dispatcher's construction of the object (only when the state file is
passed) and the chat it sends to (`loss_note_area`, else the title chat).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh
from tools.agentops.dispatch import Dispatcher
from tools.agentops.losses_daily import LossesDaily, doubling_lines

BRAIN = "Brain: Main"
ORCH = "Orchestration chat"


def _local(day: int, hour: int) -> datetime:
    """An aware instant on the host's own calendar day, so the day key never leans on the UTC offset."""
    return datetime(2026, 10, day, hour, 0).astimezone()


def _money_pair(this: float, last: float, this_closed: float = 2.0, last_closed: float = 2.0) -> dict:
    """One repo whose `$ spent` window total is `this` / `last`, with the tickets closed in each window."""
    return {"alpha": ({"spent_usd": this, "closed": this_closed}, {"spent_usd": last, "closed": last_closed})}


class Fakes:
    def __init__(self, per_repo: dict, deferred: bool = False) -> None:
        self.per_repo = per_repo
        self.deferred = deferred
        self.jobs: list = []
        self.compute_calls = 0
        self.fail_compute = False
        self.fail_sends = 0
        self.sent: list[tuple[str, str]] = []
        self.live = True
        self.logs: list[str] = []
        self.clock = _local(16, 9)

    def compute(self) -> dict:
        self.compute_calls += 1
        if self.fail_compute:
            raise RuntimeError("transcripts unreadable")
        return dict(self.per_repo)

    def run(self, job) -> None:
        if self.deferred:
            self.jobs.append(job)
        else:
            job()

    def send(self, title: str, message: str) -> None:
        if self.fail_sends > 0:
            self.fail_sends -= 1
            raise RuntimeError("wake failed")
        self.sent.append((title, message))

    def daily(self, path: Path) -> LossesDaily:
        return LossesDaily(
            state_path=path, brain_title=BRAIN, send=self.send, compute=self.compute,
            brain_live=lambda: self.live, log=self.logs.append, now=lambda: self.clock,
            runner=self.run)


# ----- the pure rule ----------------------------------------------------------------------------------------------

def test_ten_then_twenty_gives_one_line_with_repo_bucket_and_both_values():
    # $10 and $20 over 2 closed tickets each: $5.0 per closed ticket, then $10.0
    assert doubling_lines(_money_pair(20.0, 10.0)) == [
        "alpha: $ spent per closed ticket doubled in 7 days: $5.0 -> $10.0"]


def test_ten_then_nineteen_gives_no_line():
    assert doubling_lines(_money_pair(19.0, 10.0)) == []


def test_acceptance_1_hundred_over_eight_then_628_over_102_closed_gives_no_line():
    # the Zorblax incident: $628 spread over 102 closed tickets is $6.2 each, not a doubling of $12.5
    assert doubling_lines(_money_pair(628.0, 100.0, this_closed=102.0, last_closed=8.0)) == []


def test_acceptance_2_hundred_over_eight_then_300_over_eight_gives_one_per_closed_line():
    assert doubling_lines(_money_pair(300.0, 100.0, this_closed=8.0, last_closed=8.0)) == [
        "alpha: $ spent per closed ticket doubled in 7 days: $12.5 -> $37.5"]


def test_acceptance_3_hundred_over_eight_then_50_with_no_closed_gives_the_zero_closed_line():
    assert doubling_lines(_money_pair(50.0, 100.0, this_closed=0.0, last_closed=8.0)) == [
        "alpha: $ spent with 0 closed tickets: $12.5 -> $50.0"]


def test_acceptance_4_a_window_before_with_no_closed_ticket_gives_no_money_line():
    assert doubling_lines(_money_pair(300.0, 100.0, this_closed=8.0, last_closed=0.0)) == []
    assert doubling_lines(_money_pair(50.0, 100.0, this_closed=0.0, last_closed=0.0)) == []


def test_no_closed_ticket_this_window_and_no_money_this_window_gives_no_line():
    assert doubling_lines(_money_pair(0.0, 100.0, this_closed=0.0, last_closed=8.0)) == []


def test_an_unowned_row_is_compared_per_closed_ticket_too():
    per_repo = {"alpha": ({"closed": 4.0, "unowned_usd": 40.0}, {"closed": 4.0, "unowned_usd": 10.0})}
    assert doubling_lines(per_repo) == [
        "alpha: $ no ticket owns per closed ticket doubled in 7 days: $2.5 -> $10.0"]


def test_the_per_closed_mean_is_already_per_ticket_and_keeps_the_plain_rule():
    per_repo = {"alpha": ({"closed": 8.0, "mean_usd": 20.0}, {"closed": 2.0, "mean_usd": 10.0})}
    assert doubling_lines(per_repo) == ["alpha: $ per closed ticket (mean) doubled in 7 days: $10.0 -> $20.0"]


def test_closed_ten_then_twentyfive_gives_no_line():
    assert doubling_lines({"alpha": ({"closed": 25.0}, {"closed": 10.0})}) == []


def test_a_duration_bucket_is_written_through_human_duration():
    per_repo = {"alpha": ({"parked_seconds": 3600.0}, {"parked_seconds": 1800.0})}
    assert doubling_lines(per_repo) == ["alpha: hours parked doubled in 7 days: 30 min -> 60 min"]


def test_a_bucket_with_nothing_last_week_never_doubles():
    assert doubling_lines(_money_pair(20.0, 0.0)) == []


# ----- the daily check --------------------------------------------------------------------------------------------

def test_ten_then_twenty_sends_one_message_on_the_check_after_the_computation(tmp_path):
    f = Fakes(_money_pair(20.0, 10.0))
    d = f.daily(tmp_path / "losses_daily.json")
    assert d.check() == []                      # first check of the day starts the computation
    assert f.compute_calls == 1 and f.sent == []
    assert d.check() == ["Losses (2026-10-16): 1 bucket(s) doubled in 7 days against the 7 before:",
                         "alpha: $ spent per closed ticket doubled in 7 days: $5.0 -> $10.0"]
    assert len(f.sent) == 1
    title, message = f.sent[0]
    assert title == BRAIN
    lines = message.splitlines()
    assert lines[0].startswith("Losses (2026-10-16):")
    assert [line for line in lines if line.startswith("alpha:")] == [
        "alpha: $ spent per closed ticket doubled in 7 days: $5.0 -> $10.0"]


def test_ten_then_nineteen_sends_nothing_and_records_the_day(tmp_path):
    f = Fakes(_money_pair(19.0, 10.0))
    path = tmp_path / "losses_daily.json"
    d = f.daily(path)
    d.check()
    assert d.check() == []
    assert f.sent == []
    assert json.loads(path.read_text(encoding="utf-8"))["checked_on"] == "2026-10-16"
    d.check()
    assert f.compute_calls == 1                 # the day is never recomputed


def test_closed_ten_then_twentyfive_sends_nothing(tmp_path):
    f = Fakes({"alpha": ({"closed": 25.0}, {"closed": 10.0})})
    d = f.daily(tmp_path / "losses_daily.json")
    d.check()
    d.check()
    assert f.sent == []


def test_a_checked_day_sends_nothing_twice_across_a_restart(tmp_path):
    path = tmp_path / "losses_daily.json"
    f = Fakes(_money_pair(20.0, 10.0))
    d = f.daily(path)
    d.check()
    d.check()
    assert len(f.sent) == 1
    d.check()
    restarted = f.daily(path)
    restarted.check()
    restarted.check()
    assert len(f.sent) == 1


def test_a_failed_send_records_nothing_and_the_next_check_retries_without_recompute(tmp_path):
    path = tmp_path / "losses_daily.json"
    f = Fakes(_money_pair(20.0, 10.0))
    f.fail_sends = 1
    d = f.daily(path)
    assert d.check() == []
    assert d.check() == []                      # the send raises: nothing sent, nothing recorded
    assert f.sent == [] and not path.exists()
    assert any("send" in line and "failed" in line for line in f.logs)
    assert d.check() != []                      # the next check retries the send
    assert len(f.sent) == 1
    assert f.compute_calls == 1


def test_a_failed_compute_logs_one_line_and_the_next_day_retries(tmp_path):
    f = Fakes(_money_pair(20.0, 10.0))
    f.fail_compute = True
    d = f.daily(tmp_path / "losses_daily.json")
    d.check()
    d.check()
    d.check()
    assert f.compute_calls == 1 and f.sent == []
    assert len([line for line in f.logs if "compute" in line]) == 1
    f.fail_compute = False
    f.clock = _local(17, 9)
    d.check()
    d.check()
    assert f.compute_calls == 2 and len(f.sent) == 1


def test_a_chat_that_is_not_live_gets_nothing_and_the_next_check_retries(tmp_path):
    f = Fakes(_money_pair(20.0, 10.0))
    f.live = False
    d = f.daily(tmp_path / "losses_daily.json")
    d.check()
    assert d.check() == [] and f.sent == []
    f.live = True
    assert d.check() != [] and len(f.sent) == 1


def test_a_result_of_an_earlier_day_is_dropped_and_sends_nothing(tmp_path):
    """Day D starts a computation that finishes late, after day D+1 started its own: the late result is tagged D,
    so day D+1 sends nothing from it and sends only the lines of its own computation."""
    f = Fakes(_money_pair(20.0, 10.0), deferred=True)      # day D's values
    d = f.daily(tmp_path / "losses_daily.json")
    d.check()                                              # day D: the computation is queued
    assert len(f.jobs) == 1
    f.clock = _local(17, 9)                                # day D+1
    d.check()                                              # day D+1: its own computation is queued
    f.jobs.pop(0)()                                        # day D's computation finishes late
    assert d.check() == []                                 # its result is dropped, a new computation starts
    assert f.sent == []
    f.per_repo = {"alpha": ({"spent_usd": 30.0, "closed": 2.0}, {"spent_usd": 10.0, "closed": 2.0})}
    while f.jobs:
        f.jobs.pop(0)()
    d.check()
    assert len(f.sent) == 1
    message = f.sent[0][1]
    # day D+1's own values: $30 / 2 = $15.0 per closed ticket against $5.0; day D's $10.0 never appears
    assert "Losses (2026-10-17)" in message and "$15.0" in message and "$10.0" not in message


# ----- the dispatcher: construction, and the chat the note goes to -----------------------------------------------

@pytest.fixture
def routed(monkeypatch, tmp_path):
    """A dispatcher whose note runs synchronously on a fixed computation; the sends and the liveness are recorded."""
    sent: list[tuple[str, str]] = []
    live_titles = {BRAIN, ORCH}
    monkeypatch.setattr("tools.agentops.losses_daily._start_thread", lambda job: job())
    monkeypatch.setattr("tools.agentops.dispatch.default_compute",
                        lambda settings, now: _money_pair(20.0, 10.0))
    monkeypatch.setattr(Dispatcher, "_send_to_brain", lambda self, title, message: sent.append((title, message)))
    monkeypatch.setattr(Dispatcher, "_brain_is_live", lambda self, title, source="", **kw: title in live_titles)

    def build(settings: dict) -> Dispatcher:
        values = {"brain_chat_title": BRAIN, "brain_chats": {}, "repo": "owner/name", "loss_note_area": ""}
        values.update(settings)
        monkeypatch.setattr("tools.agentops.dispatch._load_settings", lambda *a, **k: values)
        return _dispatcher(tmp_path, tmp_path / "losses_daily.json")

    return build, sent, live_titles


def _dispatcher(tmp_path: Path, losses_file: Path | None) -> Dispatcher:
    kwargs = {"losses_daily_file": losses_file} if losses_file is not None else {}
    return Dispatcher(claude=FakeClaude(), gh=FakeGh(), max_hours=4.0, events_file=tmp_path / "events.jsonl",
                      state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                      clock=FakeClock(), **kwargs)


def test_the_dispatcher_builds_the_losses_daily_only_when_the_file_is_passed(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.agentops.dispatch._load_settings",
                        lambda *a, **k: {"brain_chat_title": BRAIN, "brain_chats": {}, "repo": "owner/name"})
    assert _dispatcher(tmp_path, None)._losses_daily is None
    assert isinstance(_dispatcher(tmp_path, tmp_path / "losses_daily.json")._losses_daily, LossesDaily)


def test_with_loss_note_area_set_the_note_goes_to_that_areas_chat(routed, tmp_path):
    build, sent, _ = routed
    d = build({"brain_chats": {"area:orchestration": ORCH}, "loss_note_area": "area:orchestration"})
    d._losses_daily.check()
    d._losses_daily.check()
    assert [title for title, _ in sent] == [ORCH]
    assert "alpha: $ spent per closed ticket doubled in 7 days: $5.0 -> $10.0" in sent[0][1]


def test_with_loss_note_area_empty_the_note_goes_to_the_title_chat(routed, tmp_path):
    build, sent, _ = routed
    d = build({"brain_chats": {"area:orchestration": ORCH}, "loss_note_area": ""})
    d._losses_daily.check()
    d._losses_daily.check()
    assert [title for title, _ in sent] == [BRAIN]


def test_a_not_live_area_chat_gets_nothing_and_records_nothing(routed, tmp_path, monkeypatch):
    build, sent, live_titles = routed
    live_titles.discard(ORCH)
    d = build({"brain_chats": {"area:orchestration": ORCH}, "loss_note_area": "area:orchestration"})
    d._losses_daily.check()
    assert d._losses_daily.check() == []
    assert sent == [] and not (tmp_path / "losses_daily.json").exists()
