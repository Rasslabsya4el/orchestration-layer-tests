"""#42 unit 1 (once per cause): a target that refuses every home merge with the same reason logs that reason once in
its `dispatch.out`, and the brain notice groups the pending failure lines by (repo, reason) and stays silent for a
reason already announced in the last 6 h.

Row 1 drives `core_targets.run_core_targets` on a tmp target folder (`run_core_update` is faked to refuse). Row 2 reuses
the dispatcher fakes of tests/test_issue28_failed_target_notice.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_agentops_dispatch import FakeClaude
from tests.test_issue28_failed_target_notice import (  # noqa: F401 -- `results` is the fixture, imported with its fakes
    HOUR,
    O,
    STATE,
    _core_wakes,
    _dispatcher,
    _gh_with_issue_7,
    _post,
    _state,
    results,
)
from tools.agentops import core_targets

LOG_MARK = "core update (home merge, r) failed:"


def _refusing(monkeypatch, reason: str) -> None:
    def refuse(folder, gh_runner=None, lock_wait_s=None, home_count=None, monitor_runner=None):
        raise RuntimeError(f"core update refused: {reason}")

    monkeypatch.setattr(core_targets, "run_core_update", refuse)


def _failure_lines(folder: Path) -> list[str]:
    path = folder / core_targets.DISPATCH_OUT
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if LOG_MARK in line]


def _target(tmp_path: Path) -> tuple[Path, list[dict]]:
    folder = tmp_path / "target"
    folder.mkdir()
    return folder, [{"repo": "r", "folder": str(folder)}]


# -- row 1: the target's own dispatch.out ----------------------------------------------------------------------------

def test_same_reason_on_a_second_merge_logs_one_line_and_each_merge_keeps_its_line(tmp_path, monkeypatch):
    folder, entry = _target(tmp_path)
    _refusing(monkeypatch, "uncommitted change(s) at a.md")

    first = core_targets.run_core_targets(entry, home_root=tmp_path)
    second = core_targets.run_core_targets(entry, home_root=tmp_path)

    assert first == second == ["r: failed: core update refused: uncommitted change(s) at a.md"]
    assert len(_failure_lines(folder)) == 1


def test_a_different_reason_adds_its_own_line(tmp_path, monkeypatch):
    folder, entry = _target(tmp_path)
    _refusing(monkeypatch, "uncommitted change(s) at a.md")
    core_targets.run_core_targets(entry, home_root=tmp_path)
    _refusing(monkeypatch, "uncommitted change(s) at b.md")
    core_targets.run_core_targets(entry, home_root=tmp_path)
    core_targets.run_core_targets(entry, home_root=tmp_path)  # the same second reason again: no third line

    lines = _failure_lines(folder)
    assert len(lines) == 2
    assert lines[0].endswith("core update refused: uncommitted change(s) at a.md")
    assert lines[1].endswith("core update refused: uncommitted change(s) at b.md")


# -- row 2: the brain notice (dispatcher fakes of test_issue28) ------------------------------------------------------

def test_four_merges_with_one_reason_wake_once_with_the_count_and_branches(tmp_path, results):
    claude = FakeClaude()
    claude.add_session("chat-o", name=O, state="idle")
    d = _dispatcher(tmp_path, claude, _gh_with_issue_7())

    for branch in ("m1", "m2", "m3", "m4"):
        _post(results, [7], ["orch-repo: failed: dirty tree"], branch=branch)
    d.tick()

    assert [m.splitlines() for _, m in _core_wakes(claude)] == [
        ["core update failed in orch-repo after 4 merge(s) (m1, m2, m3, m4): dirty tree"]]


def test_a_new_reason_is_one_more_line_and_a_repeat_within_six_hours_is_silent(tmp_path, results):
    claude = FakeClaude()
    claude.add_session("chat-o", name=O, state="idle")
    d = _dispatcher(tmp_path, claude, _gh_with_issue_7())
    for branch in ("m1", "m2", "m3", "m4"):
        _post(results, [7], ["orch-repo: failed: dirty tree"], branch=branch)
    d.tick()

    _post(results, [7], ["orch-repo: failed: dirty tree"], branch="m5")  # same reason, inside 6 h: nothing
    d.tick()
    assert len(_core_wakes(claude)) == 1

    _post(results, [7], ["orch-repo: failed: other"], branch="m6")  # a new reason: one more line
    d.tick()
    messages = [m for _, m in _core_wakes(claude)]
    assert len(messages) == 2
    assert messages[1].splitlines() == ["core update failed in orch-repo after 1 merge(s) (m6): other"]


def test_a_reason_announced_more_than_six_hours_ago_is_sent_again(tmp_path, results):
    claude = FakeClaude()
    claude.add_session("chat-o", name=O, state="idle")
    gh = _gh_with_issue_7()
    _post(results, [7], ["orch-repo: failed: dirty tree"], branch="m1")
    _dispatcher(tmp_path, claude, gh).tick()
    assert len(_core_wakes(claude)) == 1

    state = _state(tmp_path)  # the announcement is remembered in the same state file, with its time
    for row in state["announced"]:
        row["at"] -= 7 * HOUR
    (tmp_path / STATE).write_text(json.dumps(state), encoding="utf-8")

    _post(results, [7], ["orch-repo: failed: dirty tree"], branch="m2")
    _dispatcher(tmp_path, claude, gh).tick()  # a restarted dispatcher reads the aged state
    messages = [m for _, m in _core_wakes(claude)]
    assert len(messages) == 2
    assert messages[1].splitlines() == ["core update failed in orch-repo after 1 merge(s) (m2): dirty tree"]


def test_a_line_held_before_this_unit_is_sent_in_the_grouped_form(tmp_path, results):
    claude = FakeClaude()
    claude.add_session("chat-o", name=O, state="idle")
    held = {"target": O, "line": "core update failed in orch-repo after merge m1: dirty tree", "attempts": 0}
    (tmp_path / STATE).write_text(json.dumps({"seen": [], "pending": [held]}), encoding="utf-8")

    _dispatcher(tmp_path, claude, _gh_with_issue_7()).tick()

    assert [m.splitlines() for _, m in _core_wakes(claude)] == [
        ["core update failed in orch-repo after 1 merge(s) (m1): dirty tree"]]


def test_the_same_reason_in_two_repos_is_two_lines(tmp_path, results):
    claude = FakeClaude()
    claude.add_session("chat-o", name=O, state="idle")
    d = _dispatcher(tmp_path, claude, _gh_with_issue_7())

    _post(results, [7], ["orch-repo: failed: dirty tree", "other-repo: failed: dirty tree"], branch="m1")
    d.tick()

    assert [m.splitlines() for _, m in _core_wakes(claude)] == [[
        "core update failed in orch-repo after 1 merge(s) (m1): dirty tree",
        "core update failed in other-repo after 1 merge(s) (m1): dirty tree",
    ]]
