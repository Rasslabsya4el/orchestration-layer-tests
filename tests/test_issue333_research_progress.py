"""#333: a research run shows its progress while it runs, and stops a stalled session.

Seams: `research.run_research` (the run, with `ocserver.ensure`, `register`/`unregister`,
`read_opencode_config` patched to a fake client; the report path, the status file and the
partial report land in the test's tmp dir), `ocserver.session_progress` (the reader),
`ocserver.wait_for_idle` (its sleep, monotonic clock and wall clock injected), and `cli.main`
(the one printed status line and the partial-report path). The client is a real `ocserver.Client`
subclass whose message read comes from the session fixtures tests/fixtures/issue333_*.json: the
shape of opencode's `GET /session/{id}/message` list, with text and URLs invented. No sleeping,
no network, no opencode.

Fixture counts (known from the fixtures themselves):
- stalled session: 3 steps, last step at base + 2 min 5 s, 2 completed webfetch sources, no reddit.
- healthy session: 5 steps, last step at base + 3 min 5 s, 3 distinct completed webfetch sources, no reddit.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

from tools.agentops import cli, ocserver, research
from tools.agentops.research import ResearchResult, run_research

FIXTURES = Path(__file__).parent / "fixtures"
SESSION = "ses_fake_333"
BASE_S = 1_800_000_000  # the fixtures' base time, epoch seconds (their times are epoch ms)
REPORT_REL = Path("docs") / "research" / "2026-10-23-invented.md"
PARTIAL_REL = Path("docs") / "research" / "2026-10-23-invented.partial.md"

STALLED_STEPS = 3
STALLED_LAST_STEP_S = BASE_S + 125
HEALTHY_STEPS = 5
HEALTHY_LAST_STEP_S = BASE_S + 185


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeClock:
    """Sleep advances the one fake timeline; the monotonic and wall clocks read it."""

    def __init__(self, start_s: float):
        self.start_s = start_s
        self.t = 0.0

    def sleep(self, seconds: float) -> None:
        self.t += seconds

    def mono(self) -> float:
        return self.t

    def wall(self) -> float:
        return self.start_s + self.t


class FakeClient(ocserver.Client):
    """A real Client (its usage and last_error read `messages`) with the HTTP reads replaced."""

    def __init__(self, messages, *, busy: bool, abort_error: bool = False):
        super().__init__({"port": 0, "password": ""})
        self._messages = messages
        self._busy = busy
        self._abort_error = abort_error
        self.aborted = False

    def create_session(self, directory, title):
        return {"id": SESSION}

    def prompt_async(self, directory, session_id, **kwargs):
        return None

    def statuses(self, directories):
        return {SESSION: "busy"} if self._busy else {}

    def messages(self, directory, session_id, timeout=60):
        return self._messages

    def abort(self, directory, session_id):
        self.aborted = True
        if self._abort_error:
            raise ocserver.AgentOpsError("invented abort refusal")


class CountingClient(FakeClient):
    """Counts the status polls and the full message reads; the first message read can be made to fail."""

    def __init__(self, messages, *, busy: bool, fail_first_read: bool = False):
        super().__init__(messages, busy=busy)
        self.status_polls = 0
        self.message_reads = 0
        self._fail_first_read = fail_first_read

    def statuses(self, directories):
        self.status_polls += 1
        return super().statuses(directories)

    def messages(self, directory, session_id, timeout=60):
        self.message_reads += 1
        if self._fail_first_read and self.message_reads == 1:
            raise ocserver.AgentOpsError("invented transient read error")
        return super().messages(directory, session_id, timeout)


def _spy_progress_lines(monkeypatch) -> list[str]:
    """Records every line built by ocserver.progress_line, in order."""
    written: list[str] = []
    real = ocserver.progress_line

    def spy(progress, *, now):
        line = real(progress, now=now)
        written.append(line)
        return line

    monkeypatch.setattr(ocserver, "progress_line", spy)
    return written


def _run(tmp_path, monkeypatch, messages, *, busy: bool, clock: FakeClock, minutes: float = 20.0,
         abort_error: bool = False) -> tuple[ResearchResult, FakeClient]:
    client = FakeClient(messages, busy=busy, abort_error=abort_error)
    real_wait = ocserver.wait_for_idle
    # the report path, the status file and the partial report resolve under the tmp dir, never the repo
    monkeypatch.setattr(research, "ROOT", tmp_path)
    monkeypatch.setattr(research, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(ocserver, "ensure", lambda **kwargs: client)
    monkeypatch.setattr(ocserver, "register", lambda *args, **kwargs: None)
    monkeypatch.setattr(ocserver, "unregister", lambda *args, **kwargs: None)
    monkeypatch.setattr(ocserver, "read_opencode_config",
                        lambda config_path=None: {"provider": {"invented-provider": {}}})
    monkeypatch.setattr(ocserver, "wait_for_idle",
                        functools.partial(real_wait, sleep=clock.sleep, clock=clock.mono))
    brief = tmp_path / "brief.md"
    brief.write_text("Invented question.\n\nReport: `docs/research/2026-10-23-invented.md`\n", encoding="utf-8")
    result = run_research(brief, model="luna", directory=tmp_path, minutes=minutes, wall=clock.wall)
    return result, client


def test_stalled_session_stops_within_ten_minutes_and_keeps_the_partial_report(tmp_path, monkeypatch):
    # The session took its last step at STALLED_LAST_STEP_S and then sat silent (still busy)
    # for 15 min unless the run stops it. The run must stop at 10 min of no new step.
    clock = FakeClock(start_s=STALLED_LAST_STEP_S)
    result, client = _run(tmp_path, monkeypatch, _fixture("issue333_stalled_session.json"), busy=True, clock=clock)

    assert result.reason == "stalled: no new step for 10 min"
    assert client.aborted
    silent_s = clock.t  # the time from the last step to the stop, on the fake timeline
    assert silent_s <= 10 * 60, f"stopped after {silent_s / 60:g} min without a step"
    assert silent_s < 15 * 60
    assert not result.accepted  # the stalled fixture cites no reddit.com URL
    assert not (tmp_path / REPORT_REL).exists()
    # the partial report is on disk, beside the report path, with the text parts of every step
    partial = tmp_path / PARTIAL_REL
    text = partial.read_text(encoding="utf-8")
    assert "Partial report chunk one" in text
    assert "Partial report chunk two" in text
    assert result.partial_path == str(partial)


def test_stalled_partial_that_passes_the_check_stays_an_accepted_report(tmp_path, monkeypatch):
    # An invented stalled run whose text passes report_is_good: it stays an accepted report.
    passage = "Invented passage https://www.reddit.com/r/invented/comments/1/a quoted verbatim. "
    messages = [
        {"info": {"id": "msg_long", "sessionID": SESSION, "role": "assistant",
                  "time": {"created": STALLED_LAST_STEP_S * 1000}, "tokens": {"input": 10, "output": 20}},
         "parts": [{"type": "step-start", "snapshot": "x"},
                   {"type": "text", "text": passage * 60}]},
    ]
    clock = FakeClock(start_s=STALLED_LAST_STEP_S)
    result, client = _run(tmp_path, monkeypatch, messages, busy=True, clock=clock)

    assert client.aborted
    assert result.accepted
    assert result.report_path == str(tmp_path / REPORT_REL)
    assert (tmp_path / REPORT_REL).read_text(encoding="utf-8") == (passage * 60).strip()
    assert (tmp_path / PARTIAL_REL).is_file()


def test_healthy_session_status_steps_equal_the_fixture_count(tmp_path, monkeypatch):
    messages = _fixture("issue333_healthy_session.json")
    assert ocserver.session_progress(messages).steps == HEALTHY_STEPS

    clock = FakeClock(start_s=HEALTHY_LAST_STEP_S)
    result, _ = _run(tmp_path, monkeypatch, messages, busy=False, clock=clock)

    assert result.status.startswith(f"steps {HEALTHY_STEPS};")


def test_status_file_is_overwritten_on_each_progress_read_and_ends_with_the_last_line(tmp_path, monkeypatch):
    messages = _fixture("issue333_healthy_session.json")
    written = _spy_progress_lines(monkeypatch)
    clock = FakeClock(start_s=HEALTHY_LAST_STEP_S)
    _run(tmp_path, monkeypatch, messages, busy=True, clock=clock, minutes=1.0)

    status_file = tmp_path / "runs" / "gpt-research-brief.status"
    last_poll_line = written[-2]  # written[-1] is the run's own result line, built after the wait
    assert len(written) >= 4  # a 1 min run with the progress read every 15 s: several polls
    assert status_file.read_text(encoding="utf-8").strip() == last_poll_line
    assert last_poll_line.startswith(f"steps {HEALTHY_STEPS};")


def test_session_that_never_opened_reddit_says_reddit_reached_no(tmp_path, monkeypatch):
    messages = _fixture("issue333_healthy_session.json")
    assert ocserver.session_progress(messages).reddit is False

    clock = FakeClock(start_s=HEALTHY_LAST_STEP_S)
    result, _ = _run(tmp_path, monkeypatch, messages, busy=False, clock=clock)

    assert result.status.endswith("reddit reached: no")


def test_completed_reddit_fetch_says_reddit_reached_yes_and_failed_one_does_not_count():
    messages = [
        {"info": {"id": "msg_x", "role": "assistant", "time": {"created": 1800000000000}},
         "parts": [
             {"type": "step-start", "snapshot": "x"},
             {"type": "tool", "tool": "webfetch",
              "state": {"status": "error", "input": {"url": "https://www.reddit.com/r/invented/"}}},
             {"type": "tool", "tool": "webfetch",
              "state": {"status": "completed", "input": {"url": "https://old.reddit.com/r/invented/comments/1/x"}}},
         ]},
    ]
    progress = ocserver.session_progress(messages)
    assert progress.reddit is True
    assert progress.sources == ("https://old.reddit.com/r/invented/comments/1/x",)


def test_stall_reason_wins_when_the_last_message_carries_an_error(tmp_path, monkeypatch):
    # opencode records the abort as an error on the last message: the stall is still the reason.
    messages = _fixture("issue333_stalled_session.json")
    messages[-1]["info"]["error"] = {"name": "MessageAbortedError", "data": {"message": "invented abort"}}
    clock = FakeClock(start_s=STALLED_LAST_STEP_S)
    result, client = _run(tmp_path, monkeypatch, messages, busy=True, clock=clock)

    assert client.last_error(str(tmp_path), SESSION)  # the fixture does carry the error
    assert result.reason == "stalled: no new step for 10 min"
    assert not result.accepted


def test_failed_abort_is_named_in_the_stall_reason_and_the_partial_is_kept(tmp_path, monkeypatch):
    clock = FakeClock(start_s=STALLED_LAST_STEP_S)
    result, client = _run(tmp_path, monkeypatch, _fixture("issue333_stalled_session.json"), busy=True,
                          clock=clock, abort_error=True)

    assert client.aborted
    assert result.reason == "stalled: no new step for 10 min (abort failed)"
    assert "Partial report chunk two" in (tmp_path / PARTIAL_REL).read_text(encoding="utf-8")


def test_full_progress_read_runs_at_most_every_fifteen_seconds_while_status_polls_every_two():
    clock = FakeClock(start_s=HEALTHY_LAST_STEP_S)
    client = CountingClient(_fixture("issue333_healthy_session.json"), busy=True)

    outcome = ocserver.wait_for_idle(client, "invented-dir", SESSION, minutes=1.0,
                                     sleep=clock.sleep, clock=clock.mono, wall=clock.wall)

    assert outcome == "time limit"
    assert client.status_polls >= 25  # the 2 s status poll stays: about 30 polls in 1 min
    assert client.message_reads <= 5  # the full read runs at most every 15 s: 4 in 1 min


def test_transient_message_error_skips_that_progress_read_and_is_not_fatal():
    clock = FakeClock(start_s=HEALTHY_LAST_STEP_S)
    client = CountingClient(_fixture("issue333_healthy_session.json"), busy=True, fail_first_read=True)
    seen = []

    outcome = ocserver.wait_for_idle(client, "invented-dir", SESSION, minutes=1.0,
                                     sleep=clock.sleep, clock=clock.mono, wall=clock.wall,
                                     on_progress=seen.append)

    assert outcome == "time limit"
    assert len(seen) >= 2  # the reads after the failed one still reported progress


def test_cli_prints_the_status_line_when_the_run_ends(tmp_path, monkeypatch, capsys):
    result = ResearchResult(slug="brief", report_path=None, accepted=False,
                            reason="stalled: no new step for 10 min", text="", elapsed_s=600.0,
                            status="steps 3; last step 10 min ago; sources 2; reddit reached: no")
    monkeypatch.setattr(cli, "run_research", lambda *args, **kwargs: result)
    brief = tmp_path / "brief.md"
    brief.write_text("Invented question.\n", encoding="utf-8")

    cli.main(["research", str(brief)])

    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].startswith("brief: stalled: no new step for 10 min")
    assert lines[1] == "steps 3; last step 10 min ago; sources 2; reddit reached: no"


def test_cli_names_the_partial_report_path_on_the_summary_line(tmp_path, monkeypatch, capsys):
    partial = str(tmp_path / PARTIAL_REL)
    result = ResearchResult(slug="brief", report_path=None, accepted=False,
                            reason="stalled: no new step for 10 min", text="", elapsed_s=600.0,
                            status="steps 3; last step 10 min ago; sources 2; reddit reached: no",
                            partial_path=partial)
    monkeypatch.setattr(cli, "run_research", lambda *args, **kwargs: result)
    brief = tmp_path / "brief.md"
    brief.write_text("Invented question.\n", encoding="utf-8")

    cli.main(["research", str(brief)])

    lines = capsys.readouterr().out.strip().splitlines()
    assert partial in lines[0]
