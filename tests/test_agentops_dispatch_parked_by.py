"""tools/agentops/dispatch.py (#599): an issue a session parked and the brain made ready again is
offered with `parked by <session> at <date>, continue branch orch-<N>`; the memory lives in the
session file, so it survives the parking session's end and a dispatcher restart."""

from __future__ import annotations

import json
from pathlib import Path

from tools.agentops.dispatch import Dispatcher, IssueStatus, build_command, mark_finishing

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, READY, _issue
from tests._area_rule import _no_area_rule  # noqa: F401


class ParkedClaude(FakeClaude):
    """Records the offer text a real `ClaudeCli.start` would put in the prompt."""

    def start(self, offered, parked_by=None):
        session_id = f"sess-{self._next_id}"
        self._next_id += 1
        cmd = build_command(offered, "claude.exe", parked_by)
        self.started.append((list(offered), cmd))
        self._sessions[session_id] = {"id": session_id, "sessionId": f"uuid-{session_id}",
                                      "kind": "background", "state": "working", "status": "busy"}
        return session_id, cmd


def _dispatcher(tmp_path: Path, claude, gh) -> Dispatcher:
    return Dispatcher(claude=claude, gh=gh, events_file=tmp_path / "events.jsonl",
                      state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                      finishing_dir=tmp_path / "finishing", clock=FakeClock())


def _a_parks_9(tmp_path: Path):
    claude, gh = ParkedClaude(), FakeGh(candidates=[_issue(9)])
    d = _dispatcher(tmp_path, claude, gh)
    d.tick()
    a = claude.list_sessions()[0]["id"]
    gh.claim(9)
    d.tick()
    mark_finishing(a, finishing_dir=tmp_path / "finishing")
    # A parks it (needs-info, unassigned), then the brain answers: ready-for-agent back
    gh.issues[9] = IssueStatus(number=9, open=True, labels=("needs-info",), assignees=())
    gh.issues[9] = IssueStatus(number=9, open=True, labels=READY, assignees=())
    return claude, gh, d, a


def test_1_offer_names_the_parking_session_and_branch_then_b_holds_it(tmp_path: Path):
    claude, gh, d, a = _a_parks_9(tmp_path)
    gh._candidates = [_issue(9)]
    d.tick()  # A finishing, 9 let go -> B starts

    assert len(claude.started) == 2
    prompt = claude.started[1][1][-1]
    assert f"parked by {a} at " in prompt and "continue branch orch-9" in prompt
    b = next(s["id"] for s in claude.list_sessions() if s["id"] != a)

    d.tick()  # A's let-go of 9 is seen (nobody holds it yet)
    gh.claim(9)
    d.tick()
    a_cur = next(c for c in d.sessions if c.session == a)
    b_cur = next(c for c in d.sessions if c.session == b)
    assert b_cur.held == {9} and a_cur.held == set() and 9 not in a_cur.let_go  # #694: one holder

    claude.set_state(a, state="done", status="idle")
    d.tick()  # A's end
    assert gh.removed_assignees == [] and gh.comments == []
    assert gh.issues[9].assignees == ("bot",)
    assert b_cur.held == {9}


def test_2_memory_survives_a_s_end_and_a_restart(tmp_path: Path):
    claude, gh, d, a = _a_parks_9(tmp_path)
    claude.set_state(a, state="done", status="idle")
    report = d.tick()
    assert report.ended == a and report.outcomes == {9: "parked"}

    data = json.loads((tmp_path / "orch_session.json").read_text(encoding="utf-8"))
    assert data["parked_by"]["9"]["session"] == a

    # a fresh dispatcher (restart) reads the memory from the file only
    claude2 = ParkedClaude()
    gh2 = FakeGh(candidates=[_issue(9)])
    d2 = _dispatcher(tmp_path, claude2, gh2)
    d2.tick()
    prompt = claude2.started[0][1][-1]
    assert f"parked by {a} at " in prompt and "continue branch orch-9" in prompt


def test_3_orch_doc_carries_the_rule():
    root = Path(__file__).resolve().parent.parent
    text = (root / "docs" / "agents" / "orch.md").read_text(encoding="utf-8")
    section = text.split("Which issues a session holds", 1)[1].split("- **End.**", 1)[0]
    assert "never resumes an issue, not even one it parked itself" in section
    assert "continues `orch-<N>`" in section
