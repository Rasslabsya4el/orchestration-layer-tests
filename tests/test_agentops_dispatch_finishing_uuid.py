"""#726: a finishing mark named by the session's uuid (what `finishing` writes from the env var) is read."""

from __future__ import annotations

from pathlib import Path

from tools.agentops.dispatch import mark_finishing

from tests.test_agentops_dispatch import FakeClaude, FakeGh, _issue
from tests.test_agentops_dispatch_finishing import _dispatcher
from tests._area_rule import _no_area_rule  # noqa: F401


def _two_held(tmp_path: Path):
    claude, gh = FakeClaude(), FakeGh(candidates=[_issue(1, 1), _issue(2, 2)])
    logs: list[str] = []
    d = _dispatcher(tmp_path, claude=claude, gh=gh)
    d.log = logs.append
    d.tick()
    s1 = claude.list_sessions()[0]
    gh.claim(1)
    gh.claim(2)
    d.tick()
    return claude, gh, d, s1, logs


def test_1_uuid_named_mark_is_finishing_next_tick(tmp_path: Path):
    claude, gh, d, s1, logs = _two_held(tmp_path)
    mark_finishing(s1["sessionId"], finishing_dir=tmp_path / "finishing")
    d.tick()
    cur = next(c for c in d.sessions if c.session == s1["id"])
    assert cur.finishing
    assert any("is finishing (said so)" in line for line in logs)


def test_2_two_held_issues_released_new_session_offered_both(tmp_path: Path):
    claude, gh, d, s1, logs = _two_held(tmp_path)
    mark_finishing(s1["sessionId"], finishing_dir=tmp_path / "finishing")
    gh._candidates = [_issue(1, 1), _issue(2, 2)]  # both eligible again
    d.tick()
    assert len(claude.started) == 2
    assert sorted(claude.started[-1][0]) == [1, 2]


def test_3_short_id_mark_still_read(tmp_path: Path):
    claude, gh, d, s1, logs = _two_held(tmp_path)
    mark_finishing(s1["id"], finishing_dir=tmp_path / "finishing")
    d.tick()
    assert next(c for c in d.sessions if c.session == s1["id"]).finishing
    assert any("is finishing (said so)" in line for line in logs)
