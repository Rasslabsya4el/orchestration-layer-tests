"""#124 unit 1: ``prequeue`` runs the merge's own selection on the issue branch merged with origin/main,
before the branch joins the merge line.

Seams: ``run_prequeue`` on a tmp repo with an origin and a test map (rows 1-3 of #124), and the pure
median/threshold functions on a fake merge log."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from tests.test_select_build_map import SUITE, _git, _repo
from tools.agentops import merge as merge_module
from tools.agentops.prequeue import full_suite_median_seconds, full_suite_runs, run_prequeue
from tools.agentops.test_select import build_map, map_dir

GREEN_A = "def a():\n    return 1  # changed\n"
RED_A = "def a():\n    return 2  # changed\n"


def _fixture(tmp_path, *, with_map: bool):
    """A repo on branch ``main`` with an origin, its test map (when asked), and a checked-out ``issue`` branch."""
    repo = tmp_path / "repo"
    _repo(repo)
    _git(repo, "branch", "-M", "main")
    (repo / ".gitignore").write_text("var/\n.claude/\n__pycache__/\n.pytest_cache/\n*.pyc\n.testmondata\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "main")
    base = _git(repo, "rev-parse", "HEAD")
    if with_map:
        build_map(repo, base, map_dir(repo), SUITE)
    _git(repo, "checkout", "-q", "-b", "issue")
    return repo, base


def _change_a(repo, text: str) -> str:
    (repo / "pkg" / "a.py").write_text(text)
    _git(repo, "commit", "-q", "-am", "issue change")
    return _git(repo, "rev-parse", "HEAD")


def _flaky(repo, tmp_path) -> None:
    """Commit a test that fails on its first run and passes on a re-run (the marker file is its memory)."""
    marker = tmp_path / "flaky.marker"
    (repo / "tests" / "test_flaky.py").write_text(
        "import pathlib\n\n\ndef test_flaky():\n"
        f"    marker = pathlib.Path({str(marker)!r})\n"
        "    if not marker.exists():\n"
        "        marker.write_text('first run')\n"
        "        assert False, 'first run fails'\n")
    _git(repo, "add", "tests/test_flaky.py")
    _git(repo, "commit", "-q", "-m", "flaky test")


def _run(repo, tmp_path, capsys) -> tuple[int, list[str]]:
    rc = run_prequeue(repo, suite_cmd=SUITE, log_path=tmp_path / "merge.log", output_dir=tmp_path / "out",
                      suite_shared=())
    return rc, capsys.readouterr().out.splitlines()


def _log(path, duration: str, n: int = 20) -> None:
    path.write_text("".join(
        f"2026-10-17 10:00:00 | b{i} | pushed | suite {duration} | 0 failed | output x\n" for i in range(n)))


def test_red_branch_exits_1_names_the_failing_test_and_line1_is_plan_selections(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: True)
    repo, base = _fixture(tmp_path, with_map=True)
    head = _change_a(repo, RED_A)
    expected = merge_module._plan_selection(repo, base, head, merge_module._augmented_suite_cmd(SUITE),
                                            tmp_path / "plan.out")[1]
    assert expected == f"selected 1 of 4 tests (map {base[:8]})"
    rc, lines = _run(repo, tmp_path, capsys)
    assert rc == 1
    assert lines[0] == expected
    assert any("tests/test_a.py::test_a" in line for line in lines)


def test_green_branch_exits_0_and_the_worktree_head_and_status_stay(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: True)
    repo, base = _fixture(tmp_path, with_map=True)
    _change_a(repo, GREEN_A)
    before = (_git(repo, "rev-parse", "HEAD"), _git(repo, "status", "--porcelain"))
    rc, lines = _run(repo, tmp_path, capsys)
    assert rc == 0
    assert lines[0] == f"selected 1 of 4 tests (map {base[:8]})"
    assert (_git(repo, "rev-parse", "HEAD"), _git(repo, "status", "--porcelain")) == before


def _worktree_paths(root) -> list[str]:
    """The worktree paths git lists for ``root``, each as its normcased real path."""
    listed = _git(root, "worktree", "list", "--porcelain").splitlines()
    return [os.path.normcase(os.path.realpath(line[9:])) for line in listed if line.startswith("worktree ")]


def test_relative_cwd_places_the_suite_worktree_under_the_real_root(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: True)
    repo, _base = _fixture(tmp_path, with_map=True)
    _change_a(repo, GREEN_A)
    monkeypatch.chdir(tmp_path)  # the relative path is relative to this cwd, as a shell call's is
    rc = run_prequeue(Path("repo"), suite_cmd=SUITE, log_path=tmp_path / "merge.log",
                      output_dir=tmp_path / "out", suite_shared=())
    capsys.readouterr()
    assert rc == 0
    check = os.path.normcase(os.path.realpath(repo / ".claude" / "worktrees" / "check"))
    listed = _worktree_paths(repo)
    assert listed.count(check) == 1 and len(listed) == 2
    assert not (repo / "repo").exists(), "git resolved the relative path against the root a second time"


def test_no_map_runs_the_full_suite_when_the_median_is_short(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: True)
    repo, _base = _fixture(tmp_path, with_map=False)
    _change_a(repo, GREEN_A)
    _log(tmp_path / "merge.log", "4 min")
    rc, lines = _run(repo, tmp_path, capsys)
    assert rc == 0
    assert lines[0] == "full suite: no map"
    assert not any(line.startswith("prequeue: skipped") for line in lines)
    assert list((tmp_path / "out").glob("*.out")), "the full suite must have run (an output file)"


def test_no_map_skips_the_full_suite_when_the_median_is_long(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: True)
    repo, _base = _fixture(tmp_path, with_map=False)
    _change_a(repo, GREEN_A)
    _log(tmp_path / "merge.log", "15 min")
    rc, lines = _run(repo, tmp_path, capsys)
    assert rc == 0
    assert lines == ["full suite: no map", "prequeue: skipped, full suite median 15 min"]
    assert not list((tmp_path / "out").glob("*.out")), "a skipped full suite runs no pytest"


def test_no_map_skip_message_states_the_median_in_minutes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: True)
    repo, _base = _fixture(tmp_path, with_map=False)
    _change_a(repo, GREEN_A)
    _log(tmp_path / "merge.log", "100 min")
    rc, lines = _run(repo, tmp_path, capsys)
    assert rc == 0
    assert lines == ["full suite: no map", "prequeue: skipped, full suite median 100 min"]


def test_red_first_run_green_on_recheck_exits_0(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: True)
    repo, _base = _fixture(tmp_path, with_map=False)
    _flaky(repo, tmp_path)
    _change_a(repo, GREEN_A)
    _log(tmp_path / "merge.log", "4 min")
    rc, lines = _run(repo, tmp_path, capsys)
    assert rc == 0
    assert lines[0] == "full suite: no map"
    assert "prequeue: green after re-check of 1: tests/test_flaky.py::test_flaky" in lines


def test_selection_off_runs_the_full_suite_when_the_median_is_short(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: False)
    repo, _base = _fixture(tmp_path, with_map=True)
    _change_a(repo, GREEN_A)
    _log(tmp_path / "merge.log", "4 min")
    rc, lines = _run(repo, tmp_path, capsys)
    assert rc == 0
    assert lines[0] == "full suite: selection off (merge_select_tests)"
    assert list((tmp_path / "out").glob("*.out")), "the full suite must have run (an output file)"


def test_selection_off_skips_the_full_suite_when_the_median_is_long(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: False)
    repo, _base = _fixture(tmp_path, with_map=True)
    _change_a(repo, GREEN_A)
    _log(tmp_path / "merge.log", "15 min")
    rc, lines = _run(repo, tmp_path, capsys)
    assert rc == 0
    assert lines == ["full suite: selection off (merge_select_tests)",
                     "prequeue: skipped, full suite median 15 min"]
    assert not list((tmp_path / "out").glob("*.out")), "a skipped full suite runs no pytest"


# -- the median and the threshold, on a fake merge log ---------------------------------------------

def _line(suite: str, extra: str = "") -> str:
    return f"2026-10-17 10:00:00 | b | pushed | suite {suite} | 0 failed | {extra}output x"


def test_median_is_over_the_last_20_full_runs_only():
    old = [_line("60 min")] * 5
    new = [_line("4 min")] * 20
    assert full_suite_median_seconds(old + new) == 240


def test_median_ignores_selected_runs_zero_runs_and_other_lines():
    lines = [_line("4 min")] * 3 + [
        _line("40 min", "selected 1 of 4 tests (map 12345678) | "),  # a selected run is not a full suite
        _line("0 s"),                                                # no suite ran
        "2026-10-17 10:00:00 | not-planned | no suite field here",
    ]
    assert full_suite_median_seconds(lines) == 240


def test_median_ignores_red_batch_lines():
    red = "2026-10-17 10:00:00 | batch of 2: b1, b2 | red-suite | suite 40 min | 3 failed | output x"
    assert full_suite_median_seconds([_line("4 min")] + [red] * 2) == 240


def test_median_ignores_selected_lines():
    selected = _line("40 min", "selected 1 of 4 tests (map 12345678) | ")
    assert full_suite_median_seconds([_line("4 min")] + [selected] * 2) == 240


def test_median_reads_hours_minutes_and_seconds():
    assert full_suite_median_seconds([_line("1 h 30 min")]) == 5400
    assert full_suite_median_seconds([_line("45 s")]) == 45


def test_no_full_run_in_the_log_gives_no_median():
    assert full_suite_median_seconds([]) is None


def test_full_suite_runs_up_to_ten_minutes_and_when_unknown():
    assert full_suite_runs(None) is True
    assert full_suite_runs(240) is True
    assert full_suite_runs(600) is True
    assert full_suite_runs(601) is False
    assert full_suite_runs(900) is False


def test_a_prequeue_that_returns_early_frees_its_machine_berth(tmp_path, monkeypatch, capsys):
    """#224: the full-suite check takes a machine berth while it prepares; the early return (median too long)
    leaves no holder and the berth log shows the free."""
    from tools.agentops import machine_berths as ms
    monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: True)
    repo, _base = _fixture(tmp_path, with_map=False)
    _change_a(repo, GREEN_A)
    _log(tmp_path / "merge.log", "15 min")
    rc, lines = _run(repo, tmp_path, capsys)
    assert rc == 0 and lines[-1].startswith("prequeue: skipped")
    assert ms.holders() == []
    actions = [json.loads(x)["action"] for x in ms.log_path().read_text(encoding="utf-8").splitlines()]
    assert actions == ["take", "free"]


def test_a_prequeue_that_raises_frees_its_machine_berth(tmp_path, monkeypatch, capsys):
    import pytest
    from tools.agentops import machine_berths as ms
    monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: True)
    repo, _base = _fixture(tmp_path, with_map=False)
    _change_a(repo, GREEN_A)
    _log(tmp_path / "merge.log", "4 min")

    def boom(*a, **k):
        raise RuntimeError("suite crashed")

    monkeypatch.setattr(merge_module, "_run_merge_suite", boom)
    with pytest.raises(RuntimeError):
        _run(repo, tmp_path, capsys)
    assert ms.holders() == []
    assert ms.max_simultaneous_holders() == 1
