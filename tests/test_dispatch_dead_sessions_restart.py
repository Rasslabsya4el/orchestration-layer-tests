"""#442: a dispatcher that starts after a reboot resolves the issues the state file's dead
sessions held, so they are offered to the ORCH it starts. Fakes only: no PoB, no real gh."""

from __future__ import annotations

import json
import time
from pathlib import Path

from tests.test_agentops_dispatch import FakeClaude, FakeGh, _dispatcher, _issue
from tests._area_rule import _no_area_rule  # noqa: F401


class CheckpointGh(FakeGh):
    """FakeGh plus the last comment per issue (`last_comment_body`)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.last: dict[int, str] = {}

    def last_comment_body(self, number: int) -> str | None:
        return self.last.get(number)


def _state(tmp_path: Path, sessions: dict[str, list[int]]) -> None:
    entries = [{"session": sid, "session_uuid": None, "offered": [], "held": held, "preexisting": [],
                "claimed_now": held, "finishing": False,
                "started_at_wall": time.time() - 3600} for sid, held in sessions.items()]
    (tmp_path / "orch_session.json").write_text(
        json.dumps({"updated": "2026-10-01T17:27:00", "session": None, "sessions": entries}),
        encoding="utf-8")


def _held_gh(held: dict[str, list[int]]) -> CheckpointGh:
    numbers = [n for ns in held.values() for n in ns]
    gh = CheckpointGh(candidates=[_issue(n) for n in numbers])
    for sid, ns in held.items():
        for n in ns:
            gh.claim(n)
            gh.last[n] = f"**Checkpoint 1 (ORCH {sid}, claimed)**\n\ndone: claimed"
    return gh


def test_row1_two_dead_sessions_ten_checkpointed_issues_are_unassigned_and_offered(tmp_path: Path):
    held = {"aaaa1111": [1, 2, 3, 4, 5], "bbbb2222": [6, 7, 8, 9, 10]}
    _state(tmp_path, held)
    gh = _held_gh(held)
    claude = FakeClaude()
    d = _dispatcher(tmp_path, claude=claude, gh=gh)

    d.tick()

    assert sorted(gh.removed_assignees) == list(range(1, 11))
    assert gh.comments == []
    assert [sorted(offered) for offered, _ in claude.started] == [list(range(1, 11))]
    assert d.current is not None and d.current.preexisting == frozenset()


def test_row2_a_held_issue_without_a_checkpoint_takes_the_no_checkpoint_path_then_is_offered(tmp_path: Path):
    held = {"aaaa1111": [1, 2, 3, 4, 5], "bbbb2222": [6, 7, 8, 9, 10]}
    _state(tmp_path, held)
    gh = _held_gh(held)
    gh.last[7] = "worked on it, no checkpoint"
    claude = FakeClaude()
    d = _dispatcher(tmp_path, claude=claude, gh=gh)
    d._write_no_checkpoint_core_issue = lambda *a, **k: None

    d.tick()

    assert [n for n, _ in gh.comments] == [7]
    assert "bbbb2222 ended without a closing or parking checkpoint" in gh.comments[0][1]
    assert sorted(gh.removed_assignees) == list(range(1, 11))
    assert [sorted(offered) for offered, _ in claude.started] == [list(range(1, 11))]


def test_row3_a_held_issue_of_a_session_live_after_the_restart_is_adopted_not_unassigned(tmp_path: Path):
    held = {"aaaa1111": [1, 2], "bbbb2222": [6, 7]}
    _state(tmp_path, held)
    gh = _held_gh(held)
    claude = FakeClaude()
    claude.add_session("bbbb2222", name="orch #6 #7", state="working", status="busy",
                       startedAt=int(time.time() * 1000) - 60_000)
    d = _dispatcher(tmp_path, claude=claude, gh=gh)

    d.tick()

    assert sorted(gh.removed_assignees) == [1, 2]
    assert d.current is not None and d.current.session == "bbbb2222"
    assert d.current.held == {6, 7}
    assert gh.issues[6].assignees and gh.issues[7].assignees
    assert claude.started == []


def test_row4_a_returning_session_leaves_issues_the_current_holder_has_and_is_woken_once(tmp_path: Path):
    # aaaa1111 was counted dead and its issues re-offered; cccc3333 claimed #1 #2; aaaa1111 is live again.
    gh = _held_gh({"cccc3333": [1, 2]})
    claude = FakeClaude()
    now_ms = int(time.time() * 1000)
    claude.add_session("cccc3333", name="orch #1 #2", state="working", status="busy", startedAt=now_ms - 600_000)
    d = _dispatcher(tmp_path, claude=claude, gh=gh)
    d.tick()  # cccc3333 is tracked and holds #1 #2; the state file now lists only it
    claude.add_session("aaaa1111", name="orch #1 #2", state="working", status="busy",
                       startedAt=now_ms - 3_600_000)

    d.tick()
    d.tick()

    by_session = {c.session: c for c in d.sessions}
    assert by_session["cccc3333"].held == {1, 2}
    assert by_session["aaaa1111"].held == set()
    entries = json.loads((tmp_path / "orch_session.json").read_text(encoding="utf-8"))["sessions"]
    assert sorted(n for e in entries for n in e["held"]) == [1, 2]
    taken_wakes = [(t, m) for t, m in claude.waked if "leave it" in m]  # FakeGh also lists assigned issues as wake candidates
    assert len(taken_wakes) == 1
    target, message = taken_wakes[0]
    assert target == "orch #1 #2"
    assert "#1 is held by cccc3333 since " in message and "#2 is held by cccc3333 since " in message
    assert message.count(": leave it") == 2
