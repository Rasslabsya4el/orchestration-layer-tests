"""#850: a session that ended holding an issue whose newest own comment is its checkpoint, followed
only by machine comments (merge result, runs-daemon result), releases it: no alarm comment, no core
issue, no streak. Anyone else's comment after the checkpoint keeps `no_checkpoint`. The comment
list goes through the real `GhCli.last_comment_names` with a fake `gh` runner."""

from __future__ import annotations

import json
import subprocess

from tools.agentops.dispatch import GhCli
from tests.test_agentops_dispatch import FakeClaude, FakeGh, _events, _issue
from tests.test_agentops_dispatch_core_issue import FakeCoreGh, _dispatcher
from tests._area_rule import _no_area_rule  # noqa: F401

MERGE = "<!-- merge result -->\nmerged wt-1 into main"
JOB = "<!-- runs-daemon result -->\njob 12 finished"


class ListGh(FakeGh):
    def __init__(self) -> None:
        super().__init__()
        self.thread: dict[int, list[str]] = {}

    def last_comment_names(self, number, session_ids=()):
        def runner(args, stdin=None):
            out = json.dumps({"comments": [{"body": b} for b in self.thread.get(number, [])]})
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=out, stderr="")
        return GhCli(runner=runner).last_comment_names(number, session_ids)


def _end(tmp_path, thread: list[str]):
    claude, gh, core_gh = FakeClaude(), ListGh(), FakeCoreGh()
    d = _dispatcher(tmp_path, claude=claude, gh=gh, core_gh=core_gh)
    gh._candidates = [_issue(9, 9)]
    d.tick()
    live_id = claude.list_sessions()[0]["id"]
    gh.claim(9)
    gh.thread[9] = [b.replace("<id>", live_id) for b in thread]
    claude.set_state(live_id, state="done", status="idle")
    outcome = d.tick().outcomes[9]
    return outcome, gh, core_gh, d


def _assert_released(tmp_path, outcome, gh, core_gh, d):
    assert outcome == "released"
    assert gh.comments == [] and core_gh.calls == []
    assert gh.removed_assignees == [9] and gh.issues[9].labels == ("ready-for-agent",)
    assert d._consecutive_no_checkpoint(9) == 0
    assert [(e["issue"], e["outcome"]) for e in _events(tmp_path) if e["kind"] == "orch_end"] == [(9, "released")]


def test_row2_own_parking_then_merge_result_is_released(tmp_path):
    out = _end(tmp_path, ["ORCH <id>\nparked on merge", MERGE])
    _assert_released(tmp_path, *out)


def test_row3_own_parking_then_job_result_is_released(tmp_path):
    out = _end(tmp_path, ["ORCH <id>\nparked on job", JOB])
    _assert_released(tmp_path, *out)


def test_row4_own_checkpoint_then_brain_comment_stays_no_checkpoint(tmp_path):
    outcome, gh, core_gh, _ = _end(tmp_path, ["ORCH <id>\nparked", MERGE, "brain: please redo"])
    assert outcome == "no_checkpoint"
    assert [n for n, _ in gh.comments] == [9]
    assert any(c[:2] == ["issue", "create"] for c in core_gh.calls)


def test_row4_no_own_comment_stays_no_checkpoint(tmp_path):
    outcome, gh, core_gh, _ = _end(tmp_path, [MERGE, JOB])
    assert outcome == "no_checkpoint"
    assert [n for n, _ in gh.comments] == [9]
    assert any(c[:2] == ["issue", "create"] for c in core_gh.calls)
