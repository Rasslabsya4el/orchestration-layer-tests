"""#74 unit 2: a merge whose test selection reads ``full suite: map holds 0 tests (map <sha8>)`` tells the
repo's brain chat ONCE per map commit: ``test map <sha8> holds 0 tests: every merge runs the full suite until the
next green full run``. A second merge on the same map says nothing; a different map commit says it again; a
selection of a non-empty map says nothing; a chat not live holds the notice until it is.

Seam: ``Dispatcher.tick`` over the merge result folder (the records ``detach.run_and_record`` writes, with the
``selection`` the merge CLI prints). Fakes only: the dispatcher's FakeClaude / FakeGh / FakeClock.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_agentops_dispatch import FakeClaude, FakeGh
from tests.test_issue28_failed_target_notice import P, STAMP, _dispatcher, results  # noqa: F401 (fixture)

EMPTY_LINE = "full suite: map holds 0 tests (map cea7af0f)"
EMPTY_LINE_B = "full suite: map holds 0 tests (map bbbb2222)"
SELECTED_LINE = "selected 1123 of 7818 tests (map 4d01d05b)"
STATE = "core_notices.json"


def _post_merge(folder: Path, branch: str, selection: str | None) -> Path:
    """A merge result as ``detach.run_and_record`` writes it; ``selection`` is the merge's suite line 1 (absent
    when None)."""
    record = {"kind": "merge", "rc": 0, "pushed_sha": "a" * 40, "reason": "pushed", "issues": [],
              "core_targets": [], "argv": ["python", "-m", "tools.agentops", "merge", branch]}
    if selection is not None:
        record["selection"] = selection
    path = folder / f"{STAMP}-merge-{branch.replace('/', '_')}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _map_wakes(claude: FakeClaude) -> list[tuple[str, str]]:
    return [(target, message) for target, message in claude.waked if "holds 0 tests" in message]


def _notice(sha8: str) -> str:
    return f"test map {sha8} holds 0 tests: every merge runs the full suite until the next green full run"


def test_one_notice_per_map_commit_none_on_the_second_merge_with_the_same_map(tmp_path, results):
    claude = FakeClaude()
    claude.add_session("chat-p", name=P, state="idle")
    d = _dispatcher(tmp_path, claude, FakeGh())

    _post_merge(results, "74-a", EMPTY_LINE)
    d.tick()
    assert _map_wakes(claude) == [(P, _notice("cea7af0f"))]

    _post_merge(results, "74-b", EMPTY_LINE)
    d.tick()
    assert _map_wakes(claude) == [(P, _notice("cea7af0f"))]  # still one


def test_a_different_map_commit_notifies_again(tmp_path, results):
    claude = FakeClaude()
    claude.add_session("chat-p", name=P, state="idle")
    d = _dispatcher(tmp_path, claude, FakeGh())

    _post_merge(results, "74-a", EMPTY_LINE)
    d.tick()
    _post_merge(results, "74-b", EMPTY_LINE_B)
    d.tick()

    assert [m for _, m in _map_wakes(claude)] == [_notice("cea7af0f"), _notice("bbbb2222")]


def test_a_selection_of_a_non_empty_map_notifies_nothing(tmp_path, results):
    claude = FakeClaude()
    claude.add_session("chat-p", name=P, state="idle")
    d = _dispatcher(tmp_path, claude, FakeGh())

    _post_merge(results, "74-a", SELECTED_LINE)
    _post_merge(results, "74-b", None)  # a merge with no selection line at all
    d.tick()

    assert _map_wakes(claude) == []


def test_chat_not_live_holds_the_notice_until_it_is_live(tmp_path, results):
    claude = FakeClaude()
    d = _dispatcher(tmp_path, claude, FakeGh())

    _post_merge(results, "74-a", EMPTY_LINE)
    d.tick()
    assert _map_wakes(claude) == []  # the chat is not listed: held, nothing sent

    claude.add_session("chat-p", name=P, state="idle")
    d.tick()
    assert _map_wakes(claude) == [(P, _notice("cea7af0f"))]


def test_the_sent_map_commit_survives_a_restart_with_the_same_state_file(tmp_path, results):
    claude = FakeClaude()
    claude.add_session("chat-p", name=P, state="idle")
    _post_merge(results, "74-a", EMPTY_LINE)
    _dispatcher(tmp_path, claude, FakeGh()).tick()
    assert len(_map_wakes(claude)) == 1

    _post_merge(results, "74-b", EMPTY_LINE)
    _dispatcher(tmp_path, claude, FakeGh()).tick()  # a fresh dispatcher on the same state file
    assert len(_map_wakes(claude)) == 1
    assert (tmp_path / STATE).is_file()


def test_the_merge_selection_line_reaches_the_result_record():
    from tools.agentops.detach import _selection_of

    out = f"merged: x\nmerge-selection: {EMPTY_LINE}\nmerge-stats: in_line_s=1.0 suite_s=- failed=-\n"
    assert _selection_of(out) == {"selection": EMPTY_LINE}
    assert _selection_of("merged: x\n") == {}
