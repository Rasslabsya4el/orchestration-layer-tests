"""Issue 188 unit 1 (rule 2): a finishing ORCH session does not block a core update. The update's live refusal
counts only sessions that are not finishing; the merge-then-checkout lock still guards the checkout, so an update
never runs during a merge. Real git on a tmp target (bare origin + clone) under pytest's tmp_path; the live state
is a state file, `artifacts/runs/orch_session.json` under that tmp target root, written by the test and read by
the default live check (no `live_check` override); the home is a fake `gh` runner, and the lock holder is a real
other process (the helpers of `test_issue49_core_update`). Tests (d) cover the home push path (`core_targets`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_issue49_core_update import (
    NEW_HOME_FILES,
    NEW_VERSION,
    OLD_VERSION,
    _fake_gh,
    _git,
    _hold_lock,
    _stop,
    _target,
    _tree,
    _version,
)
from tools.agentops import core_targets
from tools.agentops.core_update import ORCH_STATE_ENTRY, CoreUpdateBusy, CoreUpdateRefused, run_core_update


def _state(root: Path, sessions: list[dict] | None, session: str | None = None) -> None:
    """The dispatcher's state file: `sessions` (every live session, with its finishing mark) and the legacy
    top-level `session` (mirrors only the sole non-finishing one)."""
    path = root / ORCH_STATE_ENTRY
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"session": session}
    if sessions is not None:
        data["sessions"] = sessions
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _run(root: Path, gh=None, **kwargs):
    return run_core_update(root, gh_runner=gh or _fake_gh(NEW_HOME_FILES), step_runner=lambda _root: [], **kwargs)


def _clean(root: Path) -> bool:
    return _git(root, "status", "--porcelain").stdout.strip() == ""


# (a) only finishing sessions are live and the merge lock is free: the update is taken.
@pytest.mark.parametrize("sessions", [
    [{"session": "aaaa1111", "finishing": True}],
    [{"session": "aaaa1111", "finishing": True}, {"session": "bbbb2222", "finishing": True}],
])
def test_a_only_finishing_sessions_live_and_lock_free_the_update_is_taken(tmp_path: Path, sessions) -> None:
    root = _target(tmp_path)
    _state(root, sessions, session=None)

    result = _run(root, lock_wait_s=5.0)

    assert result.home_head == NEW_VERSION
    assert _version(root) == NEW_VERSION
    assert _clean(root)


# (b) a non-finishing session is live: refused as today, nothing written.
@pytest.mark.parametrize("sessions, session", [
    ([{"session": "cccc3333", "finishing": False}], "cccc3333"),
    ([{"session": "aaaa1111", "finishing": True}, {"session": "cccc3333", "finishing": False}], "cccc3333"),
    (None, "cccc3333"),  # an older state file with no `sessions` key: the top-level session still counts
])
def test_b_a_non_finishing_session_live_refuses_as_today_and_writes_nothing(tmp_path: Path, sessions, session) -> None:
    root = _target(tmp_path)
    _state(root, sessions, session)
    before = _tree(root)

    with pytest.raises(CoreUpdateRefused, match="ORCH session is live"):
        _run(root, lock_wait_s=5.0)

    assert _tree(root) == before
    assert _version(root) == OLD_VERSION
    assert _clean(root)


# (c) the merge lock is held (a finishing session's merge): the update is not tried; the first call after the
# release takes it (acceptance row 5).
def test_c_lock_held_by_a_finishing_sessions_merge_is_not_tried_then_taken_on_the_first_call_after_release(
        tmp_path: Path) -> None:
    root = _target(tmp_path)
    _state(root, [{"session": "cccc3333", "finishing": True}], session=None)
    before = _tree(root)
    home_fake = _fake_gh(NEW_HOME_FILES)
    asked: list[str] = []

    def gh(args: list[str]):
        asked.append(args[1])
        return home_fake(args)

    holder = _hold_lock(root, 60.0)
    try:
        with pytest.raises(CoreUpdateBusy, match="merge running"):
            _run(root, gh=gh, lock_wait_s=0.5)
    finally:
        _stop(holder)

    assert not any("/contents/" in path or "/git/trees/" in path for path in asked)  # the home was not read
    assert _tree(root) == before
    assert _version(root) == OLD_VERSION
    assert _clean(root)

    result = _run(root, lock_wait_s=5.0)  # the first call after the release

    assert result.home_head == NEW_VERSION
    assert _version(root) == NEW_VERSION
    assert _clean(root)


# (d) the home push path (`core_targets`, the pre-check before `run_core_update`): only a finishing session is live,
# so the update is attempted and taken; a non-finishing session is refused "ORCH live" as today, nothing written.
def test_d_home_push_with_only_a_finishing_session_live_attempts_the_update_and_takes_it(tmp_path: Path) -> None:
    root = _target(tmp_path)
    _state(root, [{"session": "aaaa1111", "finishing": True}], session=None)

    lines = core_targets.run_core_targets([{"repo": "t", "folder": str(root)}], home_root=tmp_path,
                                          gh_runner=_fake_gh(NEW_HOME_FILES))

    assert len(lines) == 1 and lines[0].startswith(f"t: taken {NEW_VERSION[:8]}")
    assert _version(root) == NEW_VERSION
    assert _clean(root)


def test_d_home_push_with_a_non_finishing_session_live_is_refused_orch_live_and_writes_nothing(
        tmp_path: Path, monkeypatch) -> None:
    root = _target(tmp_path)
    _state(root, [{"session": "cccc3333", "finishing": False}], session="cccc3333")
    before = _tree(root)
    monkeypatch.setattr(core_targets, "_home_head", lambda home_root: NEW_VERSION)

    lines = core_targets.run_core_targets([{"repo": "t", "folder": str(root)}], home_root=tmp_path,
                                          gh_runner=_fake_gh(NEW_HOME_FILES))

    assert lines == ["t: refused: ORCH live"]
    assert _tree(root) == before
    assert _version(root) == OLD_VERSION
    assert _clean(root)
