"""#439: the park mark is the parking session's own checkpoint, not the newest comment at its end."""

import json

from tests.test_agentops_dispatch import FakeClaude, _dispatcher
from tests.test_agentops_dispatch_parked import RealMarkGh, _issue, _summary
from tests._area_rule import _no_area_rule  # noqa: F401

T0 = "2026-10-01T14:47:50Z"
T1 = "2026-10-01T14:48:40Z"
T2 = "2026-10-01T14:59:12Z"


def _park(tmp_path, with_answer: bool):
    claude = FakeClaude()
    gh = RealMarkGh([], T0, candidates=[_issue(1)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh)
    d.tick()
    live_id = claude.list_sessions()[0]["id"]
    comments = [{"body": f"ORCH {live_id}: needs-info checkpoint", "createdAt": T0}]
    if with_answer:
        comments.append({"body": "answer from the brain", "createdAt": T1})
    gh._view = json.dumps({"comments": comments, "updatedAt": T1 if with_answer else T0})
    gh.claim(1)
    d.tick()
    projects = tmp_path / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    (projects / f"uuid-{live_id}.jsonl").write_text(
        json.dumps({"type": "assistant", "timestamp": T2}) + "\n", encoding="utf-8")
    d.sessions[0].let_go.add(1)
    claude.set_state(live_id, state="done", status="idle")
    assert d.tick().outcomes == {1: "parked"}
    return claude, gh, d


def test_1_answer_during_the_session_tail_is_offered(tmp_path):
    claude, gh, d = _park(tmp_path, with_answer=True)
    assert d._park_marks[1].comment_at == T0  # the checkpoint, not the answer at T1
    gh._candidates = [_summary(1, comment_at=T1, updated_at=T1)]
    d.tick()
    assert claude.started[-1][0] == [1]


def test_2_nothing_after_the_checkpoint_stays_out(tmp_path):
    claude, gh, d = _park(tmp_path, with_answer=False)
    assert d._park_marks[1].comment_at == T0
    gh._candidates = [_summary(1, comment_at=T0, updated_at=T0)]
    started = len(claude.started)
    assert d._eligible_candidates() == []
    d.tick()
    assert len(claude.started) == started
