"""#232 unit 4c (D119): the merge runs its pytest suite on GitHub, off by default (rows 2, 3 and 4 of the issue).

Real merges in temp repos (a bare origin, a checkout, a worker branch) as in test_issue212_merge_recheck; the PC suite is
a fake (`_run_merge_suite`), GitHub is a fake injected into `run_github_suite`'s deps (`merge_github.DEPS`). Seams:
`merge_github.plan_for` and `run_merge` (the result, merge.log, brain_lines.log)."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tools.agentops import core_settings, github_minutes, github_suite, merge as merge_module, merge_github
from tools.agentops import test_select
from tools.agentops.merge import MergeResult, SuiteRun, run_merge
from tools.agentops.test_select import Selection
from tools.agentops.test_copy_send import SendResult
from tools.agentops.util import no_window_flags

SUITE = f'"{sys.executable}" -m pytest tests -q'
COPY = "https://github.com/owner/copy-repo.git"
BAD = "tests/test_a.py::test_x"
GREEN = {"run_id": 79, "parts": [{"part": 1, "seconds": 20}], "minutes_used": 2, "outcome": "green", "failed_ids": [],
         "full_recheck": False}


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True, timeout=60,
                          creationflags=no_window_flags()).stdout.strip()


class Fakes:
    """The deps of `run_github_suite`: a GitHub that answers with ``poll_result`` (or raises)."""

    def __init__(self, poll_result=None, machines=3, later=()):
        self.poll_result = poll_result or {"run_id": 77, "parts": [{"part": 1, "seconds": 61}, {"part": 2, "seconds": 600}],
                                           "minutes_used": 12, "outcome": "green", "failed_ids": [],
                                           "full_recheck": False}
        self.machines, self.sends, self.dispatches, self.polls, self.records, self.pushes = machines, [], [], [], [], []
        self.later = list(later)  # #247: the answers of the runs after the first (a re-check of the failed ids)

    def send(self, root, branch, remote):
        self.sends.append((root, branch, remote))
        return SendResult(True, "abc")

    def acquire_machines(self, repo, run, want=None, **kw):
        fakes = self

        class Berth:
            machines = fakes.machines

            def release(self):
                pass

        return Berth()

    def dispatch(self, repo, branch, parts, *a, **kw):
        self.dispatches.append((repo, branch, parts))
        return 77

    def poll(self, repo, run_id, *a, **kw):
        self.polls.append((repo, run_id))
        return self.later.pop(0) if self.polls[1:] and self.later else self.poll_result

    def push(self, root, branch, repo_slug):
        self.pushes.append((root, branch, repo_slug))

    def deps(self):
        return github_suite.Deps(send=self.send, acquire_machines=self.acquire_machines, dispatch=self.dispatch,
                                 poll=self.poll, record=self.records.append, push=self.push, clock=lambda: 1.0)


class World:
    def __init__(self, tmp_path: Path, monkeypatch, *, github=True, copy=COPY, own_runners=False, fakes=None):
        self.tmp = tmp_path
        self.bare = tmp_path / "origin.git"
        _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.bare))
        seed = tmp_path / "seed"
        _git(tmp_path, "clone", "-q", str(self.bare), str(seed))
        (seed / "pkg").mkdir()
        (seed / "pkg" / "a.py").write_text("A = 1\n")
        (seed / "pytest.ini").write_text("[pytest]\n")
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
        self.pc_runs: list[str] = []
        self.rechecks: list[list[str]] = []
        self.recheck_still: list[str] = []  # what the PC re-check reports as still failing
        self.fakes = fakes or Fakes()
        self.suite_calls = 0
        settings = {**core_settings.DEFAULTS, "repo": "me/orch", "brain_chat_title": "Brain",
                    "github_suite": github, "test_copy_remote": copy, "github_own_runners": own_runners}
        monkeypatch.setattr(core_settings, "load", lambda path=None: dict(settings))
        monkeypatch.setattr(merge_github, "DEPS", self.fakes.deps())
        self.calls: list[dict] = []
        real = github_suite.run_github_suite

        def spy(**kw):
            self.calls.append(kw)
            return real(**kw)
        monkeypatch.setattr(github_suite, "run_github_suite", spy)
        monkeypatch.setattr(merge_module, "SUITE_OUTPUT_DIR", tmp_path / "suite_out")
        monkeypatch.setenv(merge_module.ENV_MERGE_LOG, str(self.log))
        monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: False)
        monkeypatch.setattr(merge_module, "machine_holds", lambda: False)
        monkeypatch.setattr(merge_module, "_with_workers", lambda cmd, is_pytest, suite_dir: (cmd, 0, ""))
        monkeypatch.setattr(merge_module, "_run_merge_suite", self._pc_suite)
        self.suite_dirs = []
        real_ensure = merge_module.ensure_suite_worktree

        def ensure(*a, **kw):
            found = real_ensure(*a, **kw)
            self.suite_dirs.append(found)
            return found
        monkeypatch.setattr(merge_module, "ensure_suite_worktree", ensure)
        monkeypatch.setattr(merge_module, "_dropped_for_not_planned", lambda *a, **kw: None)
        monkeypatch.setattr(merge_module, "_recheck_failed", self._recheck)

    def _pc_suite(self, run_cmd, *a, **kw):
        self.pc_runs.append(run_cmd)
        return SuiteRun(True, "ok", 0)

    def _recheck(self, suite_cmd, ids, suite_dir, run_env, output_path):
        self.rechecks.append(list(ids))
        return list(self.recheck_still), "re-check output\n"

    def select(self, monkeypatch, n):
        monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: True)
        ids = [f"tests/t.py::t{i}" for i in range(n)]
        monkeypatch.setattr(test_select, "select_for_diff", lambda root, base, head, mdir: Selection(
            False, "", ids, 0, 4000, "m" * 40, f"selected {n} of 4000 (map mmmmmmmm)"))

    def merge(self) -> MergeResult:
        return run_merge("origin/feat", SUITE, cwd=self.checkout, lock_path=self.lock, lock_timeout=60)

    def merge_with(self, suite: str) -> MergeResult:
        return run_merge("origin/feat", suite, cwd=self.checkout, lock_path=self.lock, lock_timeout=60)

    def log_lines(self):
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def brain_lines(self):
        path = self.tmp / "brain_lines.log"
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def red(ids=(BAD,), full_recheck=False):
    return {"run_id": 78, "parts": [{"part": 1, "seconds": 30}], "minutes_used": 3, "outcome": "red-test",
            "failed_ids": list(ids), "full_recheck": full_recheck}


# -- the plan: off by default, public copy or own runners ------------------------------------------------------

def test_plan_is_none_when_github_suite_is_false(tmp_path):
    settings = {**core_settings.DEFAULTS, "repo": "me/orch", "test_copy_remote": COPY}
    assert merge_github.plan_for(settings, tmp_path) == (None, "")


def test_plan_is_none_without_a_copy_or_own_runners(tmp_path):
    settings = {**core_settings.DEFAULTS, "repo": "me/orch", "github_suite": True}
    assert merge_github.plan_for(settings, tmp_path) == (None, "")


def test_plan_of_a_repo_with_a_copy(tmp_path):
    settings = {**core_settings.DEFAULTS, "repo": "me/orch", "github_suite": True, "test_copy_remote": COPY}
    plan, note = merge_github.plan_for(settings, tmp_path / "orchrepo")
    assert note == "" and (plan.remote, plan.own_repo, plan.repo_slug, plan.repo_name) == (COPY, False, "me/orch", "orchrepo")


def test_plan_of_own_runners_asks_the_free_minutes(tmp_path, monkeypatch):
    settings = {**core_settings.DEFAULTS, "repo": "me/priv", "github_suite": True, "github_own_runners": True}
    asked = []
    monkeypatch.setattr(github_minutes, "over_budget", lambda repo, now=None, **kw: asked.append(repo) or (False, ""))
    plan, note = merge_github.plan_for(settings, tmp_path / "privrepo", now=datetime(2026, 10, 19, tzinfo=timezone.utc))
    assert asked == ["privrepo"] and note == "" and plan.own_repo is True and plan.remote == ""
    monkeypatch.setattr(github_minutes, "over_budget", lambda repo, now=None, **kw: (True, "1990 used + median 15 > 2000"))
    plan, note = merge_github.plan_for(settings, tmp_path / "privrepo")
    assert plan is None and note == "own-runners repo over free minutes: 1990 used + median 15 > 2000"


# -- default: nothing changes ----------------------------------------------------------------------------------

def test_github_suite_false_never_calls_github_and_the_log_line_is_todays(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, github=False)
    result = w.merge()
    assert result.ok and result.pushed and len(w.pc_runs) == 1
    assert w.calls == [] and w.brain_lines() == []
    assert result.suite_place == "" and result.suite_machines == 0 and result.suite_part_seconds == []
    assert result.suite_minutes == 0
    assert "suite on" not in "\n".join(w.log_lines())


# -- row 3: green, red, lost ---------------------------------------------------------------------------------

def test_green_on_github_skips_the_pc_and_records_the_place(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    result = w.merge()
    assert result.ok and result.pushed and w.pc_runs == [] and len(w.calls) == 1
    assert w.calls[0]["remote"] == COPY and w.calls[0]["own_repo"] is False and w.calls[0]["repo_slug"] == "me/orch"
    assert (result.suite_machines, result.suite_part_seconds, result.suite_minutes) == (3, [61, 600], 12)
    assert result.suite_place == "suite on GitHub: 3 machines, parts 61 s/10 min, 12 min used, run 77"
    landed = [x for x in w.log_lines() if "| pushed |" in x]
    assert len(landed) == 1 and result.suite_place in landed[0]
    assert w.brain_lines() == []


def test_a_failed_id_that_passes_the_github_recheck_lands_green(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, fakes=Fakes(red(), later=[GREEN]))
    result = w.merge()
    assert result.ok and result.pushed and w.pc_runs == [] and w.rechecks == []  # #247: the re-check ran on GitHub
    assert [c["ids"] for c in w.calls] == [None, [BAD]] and w.calls[1]["tests"] == 1
    assert result.rechecked == [BAD] and result.suite_place.startswith("suite on GitHub: 3 machines")
    landed = [x for x in w.log_lines() if "| pushed |" in x]
    assert f"0 failed after re-check of 1" in landed[0] and BAD in landed[0]


def test_a_failed_id_that_still_fails_on_github_is_red_naming_it(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, fakes=Fakes(red(), later=[red()]))
    result = w.merge()
    assert not result.ok and result.reason == "red-suite" and result.failed_tests == [BAD] and BAD in result.message
    assert w.pc_runs == [] and w.rechecks == [] and len(w.calls) == 2
    assert w.brain_lines() == []
    assert _git(w.bare, "log", "-1", "--format=%s", "main") == "initial"


def test_a_lost_part_is_a_fallback_not_red(tmp_path, monkeypatch):
    lost = {"run_id": 79, "parts": [], "minutes_used": 1, "outcome": "lost", "failed_ids": [], "full_recheck": False}
    w = World(tmp_path, monkeypatch, fakes=Fakes(lost))
    result = w.merge()
    assert result.ok and result.pushed and len(w.pc_runs) == 1 and w.rechecks == []
    assert result.suite_place == "suite on the PC (GitHub lost)"
    assert len(w.brain_lines()) == 1


def test_a_red_with_no_named_test_runs_the_whole_suite_on_the_pc(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, fakes=Fakes(red(ids=(), full_recheck=True)))
    result = w.merge()
    assert result.ok and len(w.pc_runs) == 1 and w.rechecks == []
    assert result.suite_place.startswith("suite on the PC (GitHub ")
    assert len(w.brain_lines()) == 1


# -- row 2: not started -> the PC, one brain line, the next merge asks again ------------------------------------

def test_not_started_runs_on_the_pc_with_one_brain_line_and_the_next_merge_asks_again(tmp_path, monkeypatch):
    gone = {"run_id": 80, "parts": [], "minutes_used": 0, "outcome": "not-started", "failed_ids": [],
            "full_recheck": False}
    w = World(tmp_path, monkeypatch, fakes=Fakes(gone))
    result = w.merge()
    assert result.ok and result.pushed and len(w.pc_runs) == 1
    assert result.suite_place == "suite on the PC (GitHub not-started)"
    assert any("suite on the PC (GitHub not-started)" in x for x in w.log_lines())
    lines = w.brain_lines()
    assert len(lines) == 1 and lines[0]["line"].endswith(": GitHub not-started, suite on the PC")
    assert lines[0]["line"].startswith("checkout merge ")
    # a second merge: a new branch on a moved main asks GitHub again
    worker = w.tmp / "worker"
    _git(worker, "checkout", "-q", "-b", "feat2", "origin/main")
    (worker / "pkg" / "b.py").write_text("B = 3\n")
    _git(worker, "add", "-A")
    _git(worker, "commit", "-q", "-m", "add b")
    _git(worker, "push", "-q", "origin", "feat2")
    again = run_merge("origin/feat2", SUITE, cwd=w.checkout, lock_path=w.lock, lock_timeout=60)
    assert again.ok and len(w.calls) == 2 and len(w.pc_runs) == 2 and len(w.brain_lines()) == 2


# -- the light run stays on the PC; a skipped suite asks nobody ---------------------------------------------------

def test_a_selected_run_of_50_tests_goes_to_github_with_its_ids(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.select(monkeypatch, 50)
    result = w.merge()  # #247: a selected run goes to GitHub like a full one
    assert result.ok and w.pc_runs == [] and len(w.calls) == 1 and w.calls[0]["tests"] == 50
    assert len(w.calls[0]["ids"]) == 50 and result.suite_place.startswith("suite on GitHub: 3 machines")


def test_a_selected_run_over_500_tests_goes_to_github_too(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.select(monkeypatch, 600)
    result = w.merge()
    assert result.ok and w.pc_runs == [] and w.calls[0]["tests"] == 600 and len(w.calls[0]["ids"]) == 600


def test_an_empty_selection_skips_the_suite_and_asks_nobody(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.select(monkeypatch, 0)
    monkeypatch.setattr(test_select, "select_for_diff", lambda root, base, head, mdir: Selection(
        False, "", [], 0, 4000, "m" * 40, "selected 0 of 4000 (map mmmmmmmm)"))
    result = w.merge()
    assert result.ok and w.pc_runs == [] and w.calls == []


# -- row 4: a repo on its own runners ----------------------------------------------------------------------------

def test_own_runners_over_budget_runs_on_the_pc_with_the_reason_in_the_log(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, copy="", own_runners=True)
    monkeypatch.setattr(github_minutes, "over_budget", lambda repo, now=None, **kw: (True, "1990 used + median 15 > 2000"))
    result = w.merge()
    assert result.ok and len(w.pc_runs) == 1 and w.calls == []
    assert result.suite_place == "suite on the PC (own-runners repo over free minutes: 1990 used + median 15 > 2000)"
    assert any(result.suite_place in x for x in w.log_lines())
    assert w.brain_lines() == []


def test_own_runners_under_budget_goes_to_github_with_own_repo(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, copy="", own_runners=True)
    monkeypatch.setattr(github_minutes, "over_budget", lambda repo, now=None, **kw: (False, ""))
    result = w.merge()
    assert result.ok and w.pc_runs == [] and len(w.calls) == 1
    assert w.calls[0]["own_repo"] is True and w.calls[0]["remote"] == "" and w.calls[0]["repo_slug"] == "me/orch"
    assert w.fakes.pushes and w.fakes.sends == []


# -- the merge lock stays alive while GitHub works; GitHub never raises into the merge ----------------------------

def test_the_lock_is_kept_alive_while_github_works_and_a_crash_is_a_fallback(tmp_path, monkeypatch):
    import time as _time
    plan = merge_github.Plan(remote=COPY, own_repo=False, repo_slug="me/orch", repo_name="orch")
    beats = []

    def slow(**kw):
        _time.sleep(0.35)
        return github_suite.GithubSuiteResult(status="green", machines=1, run_id=1)
    monkeypatch.setattr(github_suite, "run_github_suite", slow)
    out = tmp_path / "out.txt"
    res = merge_github.run(plan, cwd=tmp_path, suite_dir=tmp_path, sha="x", branch="origin/feat", priority=False,
                           settings={}, alive=lambda: beats.append(1), output_path=out, every=0.1)
    assert res.status == "green" and len(beats) >= 2 and out.exists()

    def boom(**kw):
        raise RuntimeError("gh exploded")
    monkeypatch.setattr(github_suite, "run_github_suite", boom)
    res = merge_github.run(plan, cwd=tmp_path, suite_dir=tmp_path, sha="x", branch="origin/feat", priority=False,
                           settings={}, alive=lambda: None, output_path=out, every=0.1)
    assert res.status == "fallback" and res.reason == "error"
    assert res.brain_line == "orch merge feat: GitHub error, suite on the PC"


def test_the_test_copy_is_sent_from_the_suite_worktree_not_the_checkout(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    result = w.merge()
    assert result.ok and len(w.fakes.sends) == 1 and w.suite_dirs
    root = w.fakes.sends[0][0]
    assert Path(root) == Path(w.suite_dirs[0]) and Path(root).resolve() != w.checkout.resolve()


def test_the_brain_line_names_the_first_issue_else_the_branch(tmp_path, monkeypatch):
    gone = {"run_id": 80, "parts": [], "minutes_used": 0, "outcome": "not-started", "failed_ids": [],
            "full_recheck": False}
    w = World(tmp_path, monkeypatch, fakes=Fakes(gone))
    result = run_merge("origin/feat", SUITE, cwd=w.checkout, lock_path=w.lock, lock_timeout=60, issues=(232, 7))
    assert result.ok and w.brain_lines()[0]["line"] == "checkout merge #232: GitHub not-started, suite on the PC"
    assert merge_github.ticket_of("origin/feat", ()) == "origin/feat"


# -- a heartbeat that raises never ends the wait or starts the PC suite beside GitHub ------------------------------

def test_a_raising_heartbeat_keeps_waiting_for_github_and_the_pc_suite_never_starts(tmp_path, monkeypatch):
    import time as _time
    fakes = Fakes()
    polling = {"on": False, "raised": 0}
    real_poll = fakes.poll

    def slow_poll(*a, **kw):
        polling["on"] = True
        _time.sleep(0.5)
        polling["on"] = False
        return real_poll(*a, **kw)
    fakes.poll = slow_poll
    w = World(tmp_path, monkeypatch, fakes=fakes)
    monkeypatch.setattr(merge_github, "ALIVE_SECONDS", 0.05)
    real_beat = merge_module.MergeLock.heartbeat

    def beat(self, *a, **kw):
        if polling["on"] and not polling["raised"]:
            polling["raised"] = 1
            raise OSError("lock file busy")
        return real_beat(self, *a, **kw)
    monkeypatch.setattr(merge_module.MergeLock, "heartbeat", beat)
    result = w.merge()
    assert polling["raised"] == 1
    assert result.ok and w.pc_runs == [] and result.suite_place.startswith("suite on GitHub: 3 machines")


# -- an own-repo run deletes its pushed ref afterwards, a public copy's run never does ---------------------------

@pytest.mark.parametrize("outcome", ["green", "red", "fallback"])
def test_an_own_repo_run_deletes_its_pushed_ref_once_per_run_whatever_the_outcome(tmp_path, monkeypatch, outcome):
    poll = {"green": None, "red": red(), "fallback": {"run_id": 5, "parts": [], "minutes_used": 0,
                                                       "outcome": "lost", "failed_ids": [], "full_recheck": False}}[outcome]
    w = World(tmp_path, monkeypatch, copy="", own_runners=True, fakes=Fakes(poll, later=[GREEN]))
    monkeypatch.setattr(github_minutes, "over_budget", lambda repo, now=None, **kw: (False, ""))
    git_calls = []
    monkeypatch.setattr(merge_github, "_git", lambda root, *args: git_calls.append(args))
    w.recheck_still = []
    w.merge()
    deletes = [a for a in git_calls if a[:3] == ("push", "origin", "--delete")]
    runs = 2 if outcome == "red" else 1  # a red run's failed ids are run again on GitHub: a second run, its own ref
    assert deletes == [("push", "origin", "--delete", "merge-suite/feat")] * runs


def test_a_public_copy_run_never_deletes_a_ref(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    git_calls = []
    monkeypatch.setattr(merge_github, "_git", lambda root, *args: git_calls.append(args))
    w.merge()
    assert git_calls == []
