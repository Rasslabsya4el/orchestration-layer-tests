"""#212: a green merge suite is not redone in full when main moves during it.

Real git (a bare origin, a checkout, a worker clone), a fake suite (`_run_merge_suite` is replaced: it records the
command it was given and, on its first call, moves local main like another writer would), a fake map for the
selection. No real gh, no real pytest run. Tested here: rows 1-5 of the issue and its rule 5 (a changed batch
runs the whole suite). Rows 6 and 7 (a replay of a real range, `core check`) are run by hand, not by this file."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from tools.agentops import detach, merge as merge_module
from tools.agentops import test_select
from tools.agentops.merge import (DEFAULT_MAIN, DEFAULT_REMOTE, MergeLock, MergeResult, SuiteRun, Waiter, run_merge,
                                  run_merge_command)
from tools.agentops.test_select import Selection

SUITE = f'"{sys.executable}" -m pytest tests -q'
IDS = ["tests/test_b.py::test_b1", "tests/test_b.py::test_b2"]


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


class World:
    """A bare origin, the checkout the merge runs in, a pushed branch ``feat``, and the fake suite."""

    def __init__(self, tmp_path: Path, monkeypatch, *, outcomes=None):
        self.tmp = tmp_path
        self.bare = tmp_path / "origin.git"
        _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.bare))
        seed = tmp_path / "seed"
        _git(tmp_path, "clone", "-q", str(self.bare), str(seed))
        for rel, text in (("pkg/a.py", "A = 1\n"), ("pkg/b.py", "B = 1\n"), ("pytest.ini", "[pytest]\n"),
                          ("docs/notes.md", "n\n")):
            (seed / rel).parent.mkdir(parents=True, exist_ok=True)
            (seed / rel).write_text(text)
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", "initial")
        _git(seed, "push", "-q", "origin", "main")
        self.checkout = tmp_path / "checkout"
        _git(tmp_path, "clone", "-q", str(self.bare), str(self.checkout))
        self.lock = tmp_path / "var" / "locks" / "merge.lock"
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        worker = tmp_path / "worker"
        _git(tmp_path, "clone", "-q", str(self.bare), str(worker))
        _git(worker, "checkout", "-q", "-b", "feat")
        (worker / "pkg" / "a.py").write_text("A = 2\n")
        _git(worker, "commit", "-q", "-am", "change a")
        _git(worker, "push", "-q", "origin", "feat")
        self.log = tmp_path / "merge.log"
        self.calls: list[str] = []  # the command of each suite run
        self.arg_ids: list[list[str]] = []  # the ids each selected run was given
        self.selects: list[tuple[str, str]] = []
        self.move_with: tuple[str, str] | None = None  # (path, text) the first run's moving commit writes
        self.outcomes = list(outcomes or [])  # SuiteRun per run, in order; green when exhausted
        monkeypatch.setattr(merge_module, "SUITE_OUTPUT_DIR", tmp_path / "suite_out")
        monkeypatch.setenv(merge_module.ENV_MERGE_LOG, str(self.log))
        monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: False)
        monkeypatch.setattr(merge_module, "machine_holds", lambda: False)
        monkeypatch.setattr(merge_module, "_with_workers", lambda cmd, is_pytest, suite_dir: (cmd, 0, ""))
        monkeypatch.setattr(merge_module, "_run_merge_suite", self._run)

    def _run(self, run_cmd, suite_cmd, workers, head_line, suite_dir, env, output_path, **kw):
        self.calls.append(run_cmd)
        found = re.search(r'"@([^"]+)"', run_cmd)
        self.arg_ids.append(Path(found.group(1)).read_text(encoding="utf-8").split() if found else [])
        if len(self.calls) == 1 and self.move_with:  # another writer lands a commit on local main meanwhile
            path, text = self.move_with
            (self.checkout / path).parent.mkdir(parents=True, exist_ok=True)
            (self.checkout / path).write_text(text)
            _git(self.checkout, "add", path)
            _git(self.checkout, "commit", "-q", "-m", f"move {path}")
            _git(self.checkout, "push", "-q", "origin", "main")
        if self.outcomes:
            return self.outcomes.pop(0)
        return SuiteRun(True, "ok", 0)

    def fake_map(self, monkeypatch, ids):
        def select(root, base, head, mdir):
            self.selects.append((base, head))
            return Selection(False, "", list(ids), 0, 100, "m" * 40, f"selected {len(ids)} of 100 (map mmmmmmmm)")
        monkeypatch.setattr(test_select, "select_for_diff", select)

    def merge(self) -> MergeResult:
        return run_merge("origin/feat", SUITE, cwd=self.checkout, lock_path=self.lock, lock_timeout=60)

    def log_lines(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []


# -- row 1: main moves by one code file whose map lists K tests -------------------------------------------------

def test_row1_a_moved_main_reruns_only_the_tests_the_new_commit_reaches(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.fake_map(monkeypatch, IDS)
    w.move_with = ("pkg/b.py", "B = 2\n")
    old_head = _git(w.checkout, "rev-parse", "HEAD")
    result = w.merge()
    assert result.ok and result.pushed, result
    assert result.suite_runs == 2 and result.rebases == 1
    assert len(w.calls) == 2
    assert "@" not in w.calls[0] and w.arg_ids[0] == []  # the first run is the whole suite
    assert w.arg_ids[1] == IDS  # exactly the K ids the map lists
    assert len(w.selects) == 1 and w.selects[0][0] == old_head  # old base -> new base
    assert w.selects[0][1] == _git(w.bare, "rev-parse", "main~1")
    assert re.fullmatch(r"suite .+ \+ re-check after main moved: 2 tests, .+", result.selection), result.selection


def test_row1_the_log_line_names_both_runs_when_it_lands(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.fake_map(monkeypatch, IDS)
    w.move_with = ("pkg/b.py", "B = 2\n")
    w.merge()  # run_merge writes the landing line itself
    landed = [x for x in w.log_lines() if "| pushed |" in x]
    assert len(landed) == 1 and re.search(r"suite .+ \+ re-check after main moved: 2 tests, ", landed[0]), landed


# -- row 2: the moving commit touches a file every test imports -------------------------------------------------

def test_row2_a_shared_import_in_the_moving_commit_runs_the_whole_suite_again(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)  # the real selection: pytest.ini is a SHARED_IMPORTS file, no map is read
    w.move_with = ("pytest.ini", "[pytest]\naddopts = -q\n")
    result = w.merge()
    assert result.ok and result.suite_runs == 2 and result.rebases == 1, result
    assert len(w.calls) == 2 and "@" not in w.calls[1]  # the second run is the whole suite
    thrown = [x for x in w.log_lines() if "green suite thrown away" in x]
    assert len(thrown) == 1 and "full: change touches a file every test imports" in thrown[0], thrown


def test_row2_a_failed_selection_runs_the_whole_suite_again(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)

    def boom(root, base, head, mdir):
        raise RuntimeError("map unreadable")
    monkeypatch.setattr(test_select, "select_for_diff", boom)
    w.move_with = ("pkg/b.py", "B = 2\n")
    result = w.merge()
    assert result.ok and result.suite_runs == 2 and "@" not in w.calls[1]
    thrown = [x for x in w.log_lines() if "green suite thrown away" in x]
    assert len(thrown) == 1 and "full: selection failed: map unreadable" in thrown[0], thrown


# -- row 3: suite-free commits only ---------------------------------------------------------------------------

def test_row3_a_suite_free_move_carries_the_green_over_with_no_second_run(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.fake_map(monkeypatch, IDS)
    w.move_with = ("docs/notes.md", "changed\n")
    result = w.merge()
    assert result.ok and result.suite_runs == 1 and result.rebases == 1, result
    assert len(w.calls) == 1 and w.selects == []
    assert not [x for x in w.log_lines() if "thrown away" in x]


# -- row 4: the selected re-run is red ------------------------------------------------------------------------

def test_row4_a_red_recheck_does_not_land_and_names_the_failed_ids(tmp_path, monkeypatch):
    bad = IDS[1]
    w = World(tmp_path, monkeypatch, outcomes=[
        SuiteRun(True, "ok", 0),
        SuiteRun(False, f"FAILED {bad} - assert 0", 0, rechecked_done=True, red_ids=[bad], still=[bad])])
    w.fake_map(monkeypatch, IDS)
    w.move_with = ("pkg/b.py", "B = 2\n")
    result = w.merge()
    assert not result.ok and not result.pushed and result.reason == "red-suite", result
    assert result.failed_tests == [bad] and bad in result.message
    assert result.suite_runs == 2 and result.rebases == 1
    assert _git(w.bare, "log", "-1", "--format=%s", "main") == "move pkg/b.py"  # main holds only the moving commit


# -- row 5: one merge.log line per thrown-away green; rebases and suite_runs reach the results JSON ---------------

def test_row5_one_merge_log_line_per_thrown_away_green(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.fake_map(monkeypatch, IDS)
    w.move_with = ("pkg/b.py", "B = 2\n")
    old_head = _git(w.checkout, "rev-parse", "HEAD")
    w.merge()
    thrown = [x for x in w.log_lines() if "green suite thrown away" in x]
    assert len(thrown) == 1, w.log_lines()
    new_head = _git(w.bare, "rev-parse", "main~1")
    assert f"| origin/feat | " in thrown[0]
    assert f"{old_head[:12]} -> {new_head[:12]}" in thrown[0]
    assert "re-check: 2 tests" in thrown[0]


def test_row5_rebases_and_suite_runs_reach_the_results_json(tmp_path, monkeypatch, capsys):
    result = MergeResult(ok=True, reason="pushed", message="feat -> main", suite_runs=2, rebases=1,
                         head_after="a" * 40, suite_duration=12.0)
    monkeypatch.setattr(merge_module, "run_merge", lambda *a, **k: result)
    monkeypatch.setattr(merge_module, "_preload_repo_modules", lambda: None)
    monkeypatch.setattr(merge_module, "worktree_refusal", lambda cwd: None)

    class Args:
        branch, cwd, suite, lock, timeout, queue_checked, issue = "feat", tmp_path, "x", tmp_path / "l", 1, True, []
    assert run_merge_command(Args) == 0
    out = capsys.readouterr().out
    assert detach._runs_of(out) == {"rebases": 1, "suite_runs": 2}
    assert detach._runs_of("no such line") == {"rebases": None, "suite_runs": None}
    assert detach._stats_of(out)["suite_s"] == 12.0  # the stats line stays the last one
    assert out.rstrip().splitlines()[-1].startswith("merge-stats: ")


# -- rule 5: a batch whose members changed runs the whole suite --------------------------------------------------

def test_rule5_a_member_that_leaves_the_batch_after_the_move_runs_the_whole_suite(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.fake_map(monkeypatch, IDS)
    other = tmp_path / "worker-other"  # a waiter whose branch edits the line the moving commit edits
    _git(tmp_path, "clone", "-q", str(w.bare), str(other))
    _git(other, "checkout", "-q", "-b", "other")
    (other / "pkg" / "b.py").write_text("B = 9\n")
    _git(other, "commit", "-q", "-am", "other edits b")
    _git(other, "push", "-q", "origin", "other")
    waiter = Waiter(ticket="t-other", pid=0, info={
        "suite": SUITE, "remote": DEFAULT_REMOTE, "main": DEFAULT_MAIN,
        "cwd": str(w.checkout.resolve()), "branch": "origin/other"})
    unclaimed: list[str] = []

    def line(self):
        self.line_stop = None
        return [waiter]
    monkeypatch.setattr(MergeLock, "waiting_line", line)
    monkeypatch.setattr(MergeLock, "claim", lambda self, ws: list(ws))
    monkeypatch.setattr(MergeLock, "unclaim", lambda self, ws: unclaimed.extend(x.info["branch"] for x in ws))
    monkeypatch.setattr(MergeLock, "post_result", lambda self, x, result: None)
    w.move_with = ("pkg/b.py", "B = 3\n")  # the waiter now conflicts: it leaves the line, the batch is the holder alone
    result = w.merge()
    assert result.ok and result.suite_runs == 2 and result.rebases == 1, result
    assert unclaimed == ["origin/other"]
    assert len(w.calls) == 2 and "@" not in w.calls[1] and w.arg_ids[1] == []  # the whole suite, not the re-check
    assert w.selects, "the plan was made at the move"
    assert "re-check after main moved" not in result.selection


# -- an empty selection: the green is kept -----------------------------------------------------------------------

def test_an_empty_selection_keeps_the_green_and_logs_zero_tests(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.fake_map(monkeypatch, [])
    w.move_with = ("pkg/b.py", "B = 2\n")
    result = w.merge()
    assert result.ok and result.suite_runs == 1 and result.rebases == 1, result
    assert len(w.calls) == 1
    kept = [x for x in w.log_lines() if "green suite kept" in x]
    assert len(kept) == 1 and "re-check: 0 tests" in kept[0], w.log_lines()
    assert not [x for x in w.log_lines() if "thrown away" in x]
