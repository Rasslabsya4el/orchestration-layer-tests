"""#852: an end is `released` when the session's newest OWN comment is a parking or closing checkpoint,
whatever brain / watcher / other-ORCH / machine comments follow it; a checkpoint without the session id
created while it held the issue is its own; a claim-only note, no note, or a note from before the claim stays
`no_checkpoint`. Comments (with `createdAt`) go through the real `GhCli.own_checkpoint_parks`."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from tools.agentops.dispatch import GhCli, _parks_or_closes
from tests.test_agentops_dispatch import FakeClaude, FakeGh, _events, _issue
from tests.test_agentops_dispatch_core_issue import FakeCoreGh, _dispatcher
from tests._area_rule import _no_area_rule  # noqa: F401

T0 = datetime(2026, 10, 11, 8, 0, 0, tzinfo=timezone.utc)
MERGE = "<!-- merge result -->\nmerged wt-1 into main"
BRAIN = "**Brain (autopilot #780, session 41): answer.** go on"
WATCHER = "Watcher, anomaly `job_failed:j-1` (PC half): the job waited"
PARK = "**Checkpoint 2 (ORCH <id>, finishing):** parked `needs-info`, waits on a run"
CLAIM = "Checkpoint 1 (ORCH <id>, claimed). One unit, none needs."


class ListGh(FakeGh):
    def __init__(self) -> None:
        super().__init__()
        self.thread: dict[int, list[tuple[float, str]]] = {}

    def own_checkpoint_parks(self, number, session_ids=(), held_from="", held_until="", other_ids=None):
        def runner(args, stdin=None):
            out = json.dumps({"comments": [
                {"body": b, "createdAt": (T0 + timedelta(minutes=m)).isoformat().replace("+00:00", "Z")}
                for m, b in self.thread.get(number, [])]})
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=out, stderr="")
        return GhCli(runner=runner).own_checkpoint_parks(number, session_ids, held_from, held_until, other_ids)


def _end(tmp_path, thread: list[tuple[float, str]]):
    """Session starts at T0, claims the issue (event at T0+1 min), ends at T0+30 min; `thread` is
    (minutes after T0, body) with `<id>` the live session id."""
    now = [T0]
    claude, gh, core_gh = FakeClaude(), ListGh(), FakeCoreGh()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, core_gh=core_gh)
    d.timestamp = lambda: now[0]
    gh._candidates = [_issue(9, 9)]
    d.tick()
    live_id = claude.list_sessions()[0]["id"]
    gh.claim(9)
    now[0] = T0 + timedelta(minutes=1)
    d.tick()
    gh.thread[9] = [(m, b.replace("<id>", live_id)) for m, b in thread]
    now[0] = T0 + timedelta(minutes=30)
    claude.set_state(live_id, state="done", status="idle")
    outcome = d.tick().outcomes[9]
    return outcome, gh, core_gh, d


def _assert_released(tmp_path, outcome, gh, core_gh, d):
    assert outcome == "released"
    assert gh.comments == [] and core_gh.calls == []
    assert gh.removed_assignees == [9] and gh.issues[9].labels == ("ready-for-agent",)
    assert d._consecutive_no_checkpoint(9) == 0
    assert [(e["issue"], e["outcome"]) for e in _events(tmp_path) if e["kind"] == "orch_end"] == [(9, "released")]


def _assert_alarm(outcome, gh, core_gh):
    assert outcome == "no_checkpoint"
    assert [n for n, _ in gh.comments] == [9]
    assert any(c[:2] == ["issue", "create"] for c in core_gh.calls)


def test_row2_own_parking_then_brain_comment_is_released(tmp_path):
    _assert_released(tmp_path, *_end(tmp_path, [(5, PARK), (6, BRAIN)]))


def test_row3_parking_without_id_inside_the_holding_window_is_released(tmp_path):
    _assert_released(tmp_path, *_end(tmp_path, [(5, "Checkpoint 2 (ORCH, parked `needs-info`): waits on the brain")]))


def test_row4_claim_only_then_brain_comment_stays_no_checkpoint(tmp_path):
    outcome, gh, core_gh, _ = _end(tmp_path, [(2, CLAIM), (6, BRAIN)])
    _assert_alarm(outcome, gh, core_gh)


def test_row4_no_comment_in_the_holding_window_stays_no_checkpoint(tmp_path):
    outcome, gh, core_gh, _ = _end(tmp_path, [(-10, "an old question"), (6, MERGE)])
    _assert_alarm(outcome, gh, core_gh)


def test_row5_parking_without_id_from_before_the_claim_is_not_own(tmp_path):
    outcome, gh, core_gh, _ = _end(tmp_path, [(-5, "Checkpoint 4 (ORCH, parked `needs-info`): another session's")])
    _assert_alarm(outcome, gh, core_gh)


def test_other_orch_checkpoint_after_own_parking_keeps_released(tmp_path):
    other = "Checkpoint (ORCH 5deb1234 takes this over): done: nothing new. Cut: unit 1 of 2"
    _assert_released(tmp_path, *_end(tmp_path, [(5, PARK), (8, other)]))


def test_watcher_comment_after_own_parking_keeps_released(tmp_path):
    _assert_released(tmp_path, *_end(tmp_path, [(5, PARK), (7, WATCHER), (8, MERGE)]))


def test_the_dispatchers_own_alarm_comment_is_nobodys_checkpoint():
    alarm = "ORCH session 06001236 ended without a closing or parking checkpoint"
    runner = lambda args, stdin=None: subprocess.CompletedProcess(
        args=args, returncode=0, stderr="",
        stdout=json.dumps({"comments": [{"body": alarm, "createdAt": "2026-10-11T08:05:00Z"}]}))
    assert GhCli(runner=runner).own_checkpoint_parks(9, ("06001236",), "2026-10-11T08:00:00Z", "") is False


@pytest.mark.parametrize("headline,expected", [
    ("**Checkpoint 2 (ORCH 06001236, finishing):** four jobs still queued/running (ids in checkpoint 1); next", True),   # #747
    ("**Checkpoint 5 (ORCH be507183 takes #816 over from 8b999e49): row 5 MET, row 3 NOT MET -> `needs-info`, branch NOT merged.", True),   # #816
    ("**Checkpoint 2 (ORCH a631eb4e): unit 1 done, parked `needs-info` on the brain's answer.**", True),   # #821 (first line)
    ("Checkpoint 1 (ORCH, claimed). The issue is already one unit (one test file, one fix), so no cut table", False),   # #736
    ("Checkpoint (ORCH 5.5 session, took over #768 at its reopen; the first ORCH f50e4a1b parked it). `orch-768` (97e35c5) is already on `main`", False),   # #768
    ("ORCH note: `orch-772` made (a + b merged, clean, tests only differ in 5 files); unit 3 waits", False),   # #772
    ("Checkpoint (ORCH 5.5 session, claimed #775). The standing run (pid 37116) still stands at 0 %", False),   # #775
])
def test_the_word_list_on_the_seven_real_checkpoints(headline, expected):
    assert _parks_or_closes(headline) is expected
