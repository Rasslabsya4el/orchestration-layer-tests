"""#1072: an ORCH session idle with nothing held whose parked issue waits on a live merge is kept (not
ended), logged once as `idle, waiting on merge of #N`, and woken when the merge result makes the issue a
candidate again. Past the ceiling, finishing, or a result file with no wake within one tick -> ended as today.
Rows 1-4 of the issue, fake clock and fake gh; row 5 is a live reading.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.agentops.dispatch import MergeWait, checkpoint_merge_wait, mark_finishing

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, _issue
from tests.test_agentops_dispatch_finishing import FakeTimestamp, _dispatcher
from tests._area_rule import _no_area_rule  # noqa: F401

NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)


class MergeGh(FakeGh):
    """A fake gh that knows the merge-parking checkpoint of an issue (`waits`) and, like the real
    #918 rule, names the session as the holder of its own newest checkpoint."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.waits: dict[int, MergeWait] = {}
        #: issues whose newest ORCH checkpoint is the session's own (the #918 skip applies to them)
        self.own_checkpoint: set[int] = set()

    def merge_wait(self, number: int, session_ids: tuple = ()):
        return self.waits.get(number)

    def checkpoint_holder(self, number: int, session_ids: tuple = ()):
        return (session_ids[0], False) if number in self.waits or number in self.own_checkpoint else (None, False)


def _park_on_merge(tmp_path: Path, *, checkpoint_age_min: float, state: str = "working",
                   finishing: bool = False, result_exists: bool = False):
    """S1 claimed #1 and parked it on a merge; its turn ended with nothing held."""
    claude, gh = FakeClaude(), MergeGh(candidates=[_issue(1, 1)])
    clock, stamp = FakeClock(), FakeTimestamp(NOW)
    d = _dispatcher(tmp_path, claude=claude, gh=gh, clock=clock, timestamp=stamp)
    log: list[str] = []
    d.log = log.append
    d.tick()
    sid = claude.list_sessions()[0]["id"]
    gh.claim(1)
    d.tick()  # S1 holds #1
    clock.advance(1200)  # the work took a while: past the wake grace of an offered issue
    stamp.advance(seconds=1200)
    gh.park(1)
    gh._candidates = []
    result = tmp_path / "merge" / "r1.json"
    if result_exists:
        result.parent.mkdir(parents=True)
        result.write_text("{}", encoding="utf-8")
    gh.waits[1] = MergeWait(comment_at=stamp.value - timedelta(minutes=checkpoint_age_min),
                            result_file=str(result), result_posted=False)
    claude.set_state(sid, state=state, status="idle")
    if finishing:
        mark_finishing(sid, finishing_dir=tmp_path / "finishing")

    def tick():
        clock.advance(30)
        stamp.advance(seconds=30)
        return d.tick()

    return claude, gh, d, sid, log, tick


def _waiting_lines(log: list[str]) -> list[str]:
    return [m for m in log if "idle, waiting on merge of #1" in m]


def test_row1_idle_session_parked_on_a_live_merge_is_kept_and_logged_once(tmp_path: Path):
    claude, gh, d, sid, log, tick = _park_on_merge(tmp_path, checkpoint_age_min=10)

    tick()
    tick()

    assert sid not in d.ended_sessions and claude.stopped == []
    assert len(_waiting_lines(log)) == 1


def test_row1b_a_done_session_is_kept_the_same_way(tmp_path: Path):
    claude, gh, d, sid, log, tick = _park_on_merge(tmp_path, checkpoint_age_min=10, state="done")

    tick()

    assert sid not in d.ended_sessions and claude.stopped == []
    assert len(_waiting_lines(log)) == 1


def test_row2_merge_result_wakes_the_same_session_for_that_issue_in_the_same_tick(tmp_path: Path):
    claude, gh, d, sid, log, tick = _park_on_merge(tmp_path, checkpoint_age_min=10)
    tick()

    gh.waits[1] = MergeWait(comment_at=gh.waits[1].comment_at, result_file=gh.waits[1].result_file,
                            result_posted=True)
    gh._candidates = [_issue(1, 1)]  # the runner re-added ready-for-agent
    report = tick()

    assert report.woke == (1,) and report.ended is None
    assert len(claude.waked) == 1 and "#1" in claude.waked[0][1]
    assert sid not in d.ended_sessions


def test_after_the_wake_the_issue_is_back_under_the_own_checkpoint_skip(tmp_path: Path):
    claude, gh, d, sid, log, tick = _park_on_merge(tmp_path, checkpoint_age_min=10)
    tick()
    gh.waits[1] = MergeWait(comment_at=gh.waits[1].comment_at, result_file=gh.waits[1].result_file,
                            result_posted=True)
    gh._candidates = [_issue(1, 1)]
    assert tick().woke == (1,)

    # the session answered the wake, parked the issue again with its own `needs-info` checkpoint
    gh.waits.clear()
    gh.own_checkpoint.add(1)
    claude.set_state(sid, state="working", status="busy")
    d.clock.advance(700)  # past the wake cooldown
    d.timestamp.advance(seconds=700)
    report = tick()

    assert report.woke == () and len(claude.waked) == 1


def test_a_checkpoint_that_is_no_longer_parked_on_merge_drops_the_skip_bypass(tmp_path: Path):
    claude, gh, d, sid, log, tick = _park_on_merge(tmp_path, checkpoint_age_min=10)
    tick()  # kept for #1
    gh.waits.clear()  # its newest checkpoint is now something else (needs-info)
    gh.own_checkpoint.add(1)
    gh._candidates = [_issue(1, 1)]
    claude.set_state(sid, state="working", status="busy")

    report = tick()

    assert report.woke == () and claude.waked == []


def test_row2b_a_result_file_with_no_wake_ends_the_session_one_tick_later(tmp_path: Path):
    claude, gh, d, sid, log, tick = _park_on_merge(tmp_path, checkpoint_age_min=10, result_exists=True)

    tick()  # file seen: kept for this tick
    assert sid not in d.ended_sessions
    tick()  # no candidate, no wake -> ended as today

    assert sid in d.ended_sessions


def test_row3_checkpoint_older_than_the_ceiling_is_ended_as_today(tmp_path: Path):
    claude, gh, d, sid, log, tick = _park_on_merge(tmp_path, checkpoint_age_min=40)

    tick()

    assert sid in d.ended_sessions and claude.stopped == [sid]
    assert _waiting_lines(log) == []


def test_row4_finishing_session_is_ended_as_today(tmp_path: Path):
    claude, gh, d, sid, log, tick = _park_on_merge(tmp_path, checkpoint_age_min=10, finishing=True)

    tick()

    assert sid in d.ended_sessions and claude.stopped == [sid]
    assert _waiting_lines(log) == []


# -- the checkpoint reader --------------------------------------------------------------------------

def _c(at: str, body: str) -> dict:
    return {"createdAt": at, "body": body}


def test_checkpoint_merge_wait_reads_result_file_and_age_of_the_own_parking_checkpoint():
    comments = [
        _c("2026-10-15T11:00:00Z", "ORCH abc12345: claimed"),
        _c("2026-10-15T11:50:00Z", "ORCH abc12345\nparked on merge artifacts/merge/r1.json (numbers on main)"),
    ]

    wait = checkpoint_merge_wait(comments, ("abc12345", "uuid-x"))

    assert wait is not None and wait.result_file == "artifacts/merge/r1.json" and not wait.result_posted
    assert wait.comment_at == datetime(2026, 10, 15, 11, 50, tzinfo=timezone.utc)


def test_checkpoint_merge_wait_sees_a_result_comment_after_it_and_ignores_other_sessions():
    comments = [
        _c("2026-10-15T11:50:00Z", "ORCH abc12345\nparked on merge r1.json"),
        _c("2026-10-15T11:55:00Z", "<!-- merge result -->\nMerge of b landed on main"),
    ]

    assert checkpoint_merge_wait(comments, ("abc12345",)).result_posted is True
    assert checkpoint_merge_wait(comments, ("zzzzzzzz",)) is None


def test_checkpoint_merge_wait_is_none_once_the_session_closed_the_issue():
    comments = [
        _c("2026-10-15T11:50:00Z", "ORCH abc12345\nparked on merge r1.json"),
        _c("2026-10-15T11:55:00Z", "<!-- merge result -->\nlanded"),
        _c("2026-10-15T11:58:00Z", "ORCH abc12345\nclosing: acceptance rows answered"),
    ]

    assert checkpoint_merge_wait(comments, ("abc12345",)) is None
