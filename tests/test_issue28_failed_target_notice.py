"""#28 unit 3: a merge result whose `core_targets` holds a `failed:` line reaches the brain chat of the merged
issue's area (`brain_chats`), one line per failed target; `refused: ORCH live` and `taken` send nothing.

The result file is written by `detach.run_and_record` (the detached runner); the dispatcher reads the results
folder on its tick. Fakes only: the dispatcher's FakeClaude / FakeGh / FakeClock of test_agentops_dispatch.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh
from tools.agentops.core_settings import load as load_settings
from tools.agentops.dispatch import Dispatcher, IssueStatus

P = "Brain: Main"          # brain_chat_title
O = "Оркестровый слой"    # the chat of area:orchestration
CHATS = {"area:orchestration": O}
STATE = "core_notices.json"
STAMP = "20261016T120000"
HOUR = 3600.0


@pytest.fixture
def results(tmp_path, monkeypatch):
    folder = tmp_path / "results"
    folder.mkdir()
    monkeypatch.setenv("AGENTOPS_DETACH_DIR", str(folder))
    settings = dict(load_settings())
    settings.update(brain_chat_title=P, brain_chats=dict(CHATS))
    monkeypatch.setattr("tools.agentops.dispatch._load_settings", lambda *a, **k: settings)
    return folder


def _dispatcher(tmp_path: Path, claude: FakeClaude, gh: FakeGh) -> Dispatcher:
    return Dispatcher(claude=claude, gh=gh, max_hours=4.0, events_file=tmp_path / "events.jsonl",
                      state_file=tmp_path / "orch_session.json", projects_dir=tmp_path / "projects",
                      clock=FakeClock(), core_notice_file=tmp_path / STATE)


def _gh_with_issue_7() -> FakeGh:
    return FakeGh(issues={7: IssueStatus(number=7, open=True, labels=("area:orchestration",), assignees=())})


def _post(folder: Path, issues: list[int], core_targets: list[str], branch: str = "28-u3", *,
          age_seconds: float = 0.0) -> Path:
    """A merge result as `detach.run_and_record` writes it: the argv of the merge, the file named after the
    sanitized branch (`/` becomes `_`), its mtime `age_seconds` in the past."""
    argv = ["python", "-m", "tools.agentops", "merge", branch,
            *[word for n in issues for word in ("--issue", str(n))]]
    record = {"kind": "merge", "rc": 0, "pushed_sha": "a" * 40, "reason": "pushed", "issues": issues,
              "core_targets": core_targets, "argv": argv}
    path = folder / f"{STAMP}-merge-{branch.replace('/', '_')}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    if age_seconds:
        when = time.time() - age_seconds
        os.utime(path, (when, when))
    return path


def _core_wakes(claude: FakeClaude) -> list[tuple[str, str]]:
    return [(target, message) for target, message in claude.waked if "core update" in message]


def _state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / STATE).read_text(encoding="utf-8"))


def test_failed_target_wakes_the_area_chat_once_with_repo_branch_and_reason(tmp_path, results):
    claude = FakeClaude()
    claude.add_session("chat-o", name=O, state="idle")
    d = _dispatcher(tmp_path, claude, _gh_with_issue_7())

    _post(results, [7], ["orch-repo: failed: dirty tree"])
    d.tick()

    wakes = _core_wakes(claude)
    assert [target for target, _ in wakes] == [O]  # one wake, the area's chat
    assert wakes[0][1].splitlines() == ["core update failed in orch-repo after 1 merge(s) (28-u3): dirty tree"]


def test_branch_with_a_slash_is_named_from_the_merge_argv(tmp_path, results):
    claude = FakeClaude()
    claude.add_session("chat-o", name=O, state="idle")
    d = _dispatcher(tmp_path, claude, _gh_with_issue_7())

    _post(results, [7], ["orch-repo: failed: dirty tree"], branch="feature/x")
    d.tick()

    assert [m.splitlines() for _, m in _core_wakes(claude)] == [
        ["core update failed in orch-repo after 1 merge(s) (feature/x): dirty tree"]]


def test_refused_and_taken_targets_send_nothing(tmp_path, results):
    claude = FakeClaude()
    claude.add_session("chat-o", name=O, state="idle")
    d = _dispatcher(tmp_path, claude, _gh_with_issue_7())

    _post(results, [7], ["orch-repo: refused: ORCH live", "other-repo: taken 1a2b3c4d"])
    d.tick()

    assert _core_wakes(claude) == []


def test_failed_target_of_a_merge_with_no_issue_goes_to_the_title(tmp_path, results):
    claude = FakeClaude()
    claude.add_session("chat-p", name=P, state="idle")
    d = _dispatcher(tmp_path, claude, FakeGh())

    _post(results, [], ["orch-repo: failed: dirty tree"])
    d.tick()

    assert [target for target, _ in _core_wakes(claude)] == [P]


def test_chat_not_live_keeps_the_line_until_it_is_live(tmp_path, results):
    claude = FakeClaude()
    d = _dispatcher(tmp_path, claude, _gh_with_issue_7())

    _post(results, [7], ["orch-repo: failed: dirty tree"])
    d.tick()
    assert _core_wakes(claude) == []  # the area's chat is not listed: held, nothing sent

    claude.add_session("chat-o", name=O, state="idle")
    d.tick()
    assert [target for target, _ in _core_wakes(claude)] == [O]  # sent once the chat is live
    d.tick()
    assert len(_core_wakes(claude)) == 1  # not repeated


def test_pending_line_survives_a_new_dispatcher_on_the_same_state_file(tmp_path, results):
    claude = FakeClaude()
    gh = _gh_with_issue_7()
    _post(results, [7], ["orch-repo: failed: dirty tree"])
    _dispatcher(tmp_path, claude, gh).tick()  # chat not live: the line is held in the state file
    assert _core_wakes(claude) == []
    assert _state(tmp_path)["pending"], "the held line is on disk"

    claude.add_session("chat-o", name=O, state="idle")
    restarted = _dispatcher(tmp_path, claude, gh)  # a new instance over the same state file
    restarted.tick()
    assert [target for target, _ in _core_wakes(claude)] == [O]
    restarted.tick()
    assert len(_core_wakes(claude)) == 1


def test_first_start_announces_a_result_written_just_before_it(tmp_path, results):
    claude = FakeClaude()
    claude.add_session("chat-o", name=O, state="idle")
    _post(results, [7], ["orch-repo: failed: dirty tree"])  # written before the dispatcher's first start

    _dispatcher(tmp_path, claude, _gh_with_issue_7()).tick()

    assert [target for target, _ in _core_wakes(claude)] == [O]


def test_first_start_skips_results_older_than_six_hours(tmp_path, results):
    claude = FakeClaude()
    claude.add_session("chat-o", name=O, state="idle")
    _post(results, [7], ["orch-repo: failed: old"], branch="old-one", age_seconds=7 * HOUR)
    _post(results, [7], ["orch-repo: failed: fresh"], branch="fresh-one")

    _dispatcher(tmp_path, claude, _gh_with_issue_7()).tick()

    assert [m.splitlines() for _, m in _core_wakes(claude)] == [
        ["core update failed in orch-repo after 1 merge(s) (fresh-one): fresh"]]


def test_unreadable_state_file_skips_results_older_than_six_hours(tmp_path, results):
    (tmp_path / STATE).write_text("not json", encoding="utf-8")
    claude = FakeClaude()
    claude.add_session("chat-o", name=O, state="idle")
    _post(results, [7], ["orch-repo: failed: old"], branch="old-one", age_seconds=7 * HOUR)
    _post(results, [7], ["orch-repo: failed: fresh"], branch="fresh-one")

    _dispatcher(tmp_path, claude, _gh_with_issue_7()).tick()

    assert [m.splitlines() for _, m in _core_wakes(claude)] == [
        ["core update failed in orch-repo after 1 merge(s) (fresh-one): fresh"]]


def test_seen_is_pruned_to_the_results_still_in_the_folder(tmp_path, results):
    claude = FakeClaude()
    claude.add_session("chat-o", name=O, state="idle")
    d = _dispatcher(tmp_path, claude, _gh_with_issue_7())
    gone = _post(results, [7], ["orch-repo: refused: ORCH live"], branch="gone")
    d.tick()
    assert f"{STAMP}-merge-gone.json" in _state(tmp_path)["seen"]

    gone.unlink()
    _post(results, [7], ["orch-repo: failed: dirty tree"], branch="new")
    d.tick()  # this tick saves the state: `seen` keeps only the names still in the folder

    assert _state(tmp_path)["seen"] == [f"{STAMP}-merge-new.json"]
