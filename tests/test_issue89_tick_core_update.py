"""#89 unit 2: a target's dispatcher takes a pending core update on its first tick without an ORCH.

Row 2 of the issue: the mark present, no session listed, the admission answering `wait` and 0 eligible
candidates calls the core update exactly once; with a session listed it calls it 0 times; after a `taken`
result the mark is gone and the next tick calls it 0 times; a refusal keeps the mark.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from tools.agentops.core_pending import MARK_REL, read_mark, write_mark
from tools.agentops.dispatch import Dispatcher, TickReport, _Current

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, _issue

TAKEN = "core update (auto): updated: 3 file(s) changed; core_version -> abc12345"
REFUSED = "core update (auto) refused: refusing: an ORCH session is live in this target"
FAILED = "core update (auto) failed: RuntimeError('home unreadable')"


class Update:
    """The `core_update` callable: counts its calls, answers the next message of `answers`."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _build(tmp_path: Path, update, *, candidates=(), admission_go=False, dry_run=False):
    gh = FakeGh(candidates=list(candidates))
    d = Dispatcher(claude=FakeClaude(), gh=gh, max_hours=4.0, events_file=tmp_path / "events.jsonl",
                   state_file=tmp_path / "state" / "orch_session.json", projects_dir=tmp_path / "projects",
                   finishing_dir=tmp_path / "finishing", context_line=1_000_000, clock=FakeClock(),
                   core_update=update, merge_lock_path=tmp_path / "var" / "locks" / "merge.lock",
                   core_update_wait_s=5.0, core_root=tmp_path, dry_run=dry_run)
    d.admission = lambda previous_wait: SimpleNamespace(go=admission_go, reason="busy", line=lambda: "PC busy")
    log: list[str] = []
    d.log = log.append
    return d, log


def _list_session_in_target(tmp_path: Path) -> None:
    path = tmp_path / "artifacts" / "runs" / "orch_session.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sessions": [{"session": "sess-9"}]}), encoding="utf-8")


def test_a_mark_no_session_admission_wait_no_candidates_calls_the_update_once(tmp_path: Path):
    write_mark(tmp_path, "5ebb24f0")
    update = Update(TAKEN)
    d, _ = _build(tmp_path, update, admission_go=False)

    d.tick()

    assert update.calls == 1


def test_a_mark_with_a_candidate_held_by_admission_still_calls_the_update_once(tmp_path: Path):
    write_mark(tmp_path, "5ebb24f0")
    update = Update(TAKEN)
    d, _ = _build(tmp_path, update, candidates=[_issue(1)], admission_go=False)

    d.tick()

    assert update.calls == 1
    assert d.claude.started == []


def test_b_a_session_in_memory_calls_the_update_zero_times(tmp_path: Path):
    write_mark(tmp_path, "5ebb24f0")
    update = Update(TAKEN)
    d, _ = _build(tmp_path, update)
    d.sessions.append(_Current(session="sess-1", started_at=0.0, offered=(1,), name="",
                               preexisting=frozenset()))

    d._maybe_start(TickReport())

    assert update.calls == 0
    assert read_mark(tmp_path) is not None


def test_b_a_session_listed_in_the_targets_state_file_calls_the_update_zero_times(tmp_path: Path):
    write_mark(tmp_path, "5ebb24f0")
    _list_session_in_target(tmp_path)
    update = Update(TAKEN)
    d, _ = _build(tmp_path, update)

    d.tick()
    d.tick()

    assert update.calls == 0
    assert read_mark(tmp_path) is not None


def test_c_after_a_taken_result_the_mark_is_gone_and_the_next_tick_calls_zero_times(tmp_path: Path):
    write_mark(tmp_path, "5ebb24f0")
    update = Update(TAKEN)
    d, _ = _build(tmp_path, update)

    d.tick()
    assert update.calls == 1
    assert read_mark(tmp_path) is None and not (tmp_path / MARK_REL).exists()
    d.tick()

    assert update.calls == 1


def test_d_a_refusal_keeps_the_mark_and_the_next_tick_calls_again(tmp_path: Path):
    write_mark(tmp_path, "5ebb24f0")
    update = Update(REFUSED, TAKEN)
    d, _ = _build(tmp_path, update)

    d.tick()
    assert update.calls == 1 and read_mark(tmp_path)["sha"] == "5ebb24f0"
    d.tick()

    assert update.calls == 2
    assert read_mark(tmp_path) is None


def test_d_a_failure_keeps_the_mark_and_is_logged_once_per_cause(tmp_path: Path):
    write_mark(tmp_path, "5ebb24f0")
    update = Update(RuntimeError("home unreadable"))
    d, log = _build(tmp_path, update)

    d.tick()
    d.tick()

    assert update.calls == 2 and read_mark(tmp_path) is not None
    assert len([m for m in log if "pending core update failed" in m]) == 1


def test_e_no_mark_calls_the_update_zero_times(tmp_path: Path):
    update = Update(TAKEN)
    d, _ = _build(tmp_path, update)

    d.tick()

    assert update.calls == 0


def test_a_dry_run_dispatcher_does_not_run_it(tmp_path: Path):
    write_mark(tmp_path, "5ebb24f0")
    update = Update(TAKEN)
    d, _ = _build(tmp_path, update, dry_run=True)

    d.tick()

    assert update.calls == 0
    assert read_mark(tmp_path) is not None


def test_f_a_repo_whose_update_rule_is_not_auto_clears_the_mark_and_stops_calling(tmp_path: Path):
    write_mark(tmp_path, "5ebb24f0")
    update = Update(None)
    d, _ = _build(tmp_path, update)

    d.tick()
    d.tick()

    assert update.calls == 1
    assert read_mark(tmp_path) is None
