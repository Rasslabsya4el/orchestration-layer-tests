"""#454 rule 2: `run_merge` refuses a branch whose commit message holds a closing keyword before a reference.

A tmp checkout and a bare tmp origin (never the real origin); the suite command writes a marker file, so a suite
that ran is seen. The check runs before the merge lock and the suite."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tools.agentops import merge as merge_module
from tools.agentops.merge import MergeResult, run_merge, run_merge_command


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


class World:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp = tmp_path
        bare = tmp_path / "origin.git"
        _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
        seed = tmp_path / "seed"
        _git(tmp_path, "clone", "-q", str(bare), str(seed))
        (seed / "a.txt").write_text("A\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", "initial")
        _git(seed, "push", "-q", "origin", "main")
        self.checkout = tmp_path / "checkout"
        _git(tmp_path, "clone", "-q", str(bare), str(self.checkout))
        self.worker = tmp_path / "worker"
        _git(tmp_path, "clone", "-q", str(bare), str(self.worker))
        self.lock = tmp_path / "var" / "locks" / "merge.lock"
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.log = tmp_path / "merge.log"
        self.marker = tmp_path / "suite_ran.txt"
        self.suite = f'"{sys.executable}" -c "open(r\'{self.marker}\', \'w\').write(\'x\')"'
        monkeypatch.setattr(merge_module, "SUITE_OUTPUT_DIR", tmp_path / "suite_out")
        monkeypatch.setenv(merge_module.ENV_MERGE_LOG, str(self.log))

    def branch(self, name: str, subject: str, body: str) -> str:
        _git(self.worker, "checkout", "-q", "-b", name)
        (self.worker / f"{name}.txt").write_text("x\n")
        _git(self.worker, "add", "-A")
        _git(self.worker, "commit", "-q", "-m", subject, "-m", body)
        _git(self.worker, "push", "-q", "origin", name)
        return _git(self.worker, "rev-parse", "--short=7", "HEAD")

    def merge(self, name: str) -> MergeResult:
        return run_merge(f"origin/{name}", self.suite, cwd=self.checkout, lock_path=self.lock, lock_timeout=60,
                         suite_output_dir=self.tmp / "suite_out", merge_log_path=self.log)

    def heads(self) -> tuple[str, str]:
        return _git(self.checkout, "rev-parse", "main"), _git(self.tmp / "origin.git", "rev-parse", "main")


def test_closing_keyword_in_a_commit_body_is_refused_before_the_suite(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    sha7 = world.branch("unit", "unit work", "fixes #415")
    assert not world.lock.exists()  # nothing has taken the lock yet
    before = world.heads()
    result = world.merge("unit")
    assert not result.ok and result.reason == "closing-keyword"
    assert f"commit {sha7}" in result.message and '"fixes"' in result.message and '"#415"' in result.message
    assert "Refs <URL>" in result.message
    lines = world.log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and sha7 in lines[0] and "fixes" in lines[0] and "closing-keyword" in lines[0]
    assert not world.marker.exists()  # the suite never ran
    assert not world.lock.exists()  # absent before (asserted above), absent after: no lock taken
    assert world.heads() == before


def test_the_same_branch_reworded_goes_on_to_the_merge(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    world.branch("unit", "unit work", "fixes #415")
    sentinel = MergeResult(ok=True, reason="sentinel", message="reached the locked merge")
    monkeypatch.setattr(merge_module, "_run_merge_locked", lambda *a, **k: sentinel)
    assert world.merge("unit").reason == "closing-keyword"  # as written: refused
    _git(world.worker, "commit", "-q", "--amend", "-m", "unit work", "-m", "finished #415")  # the same branch, reworded
    _git(world.worker, "push", "-q", "--force", "origin", "unit")
    assert world.merge("unit") is sentinel  # no longer refused: on to the locked merge


def test_a_failing_check_refuses_the_branch(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    world.branch("unit", "unit work", "finished #415")

    def boom(*a, **k):
        raise ValueError("git log exploded")
    monkeypatch.setattr(merge_module.closing_keywords, "check_range", boom)
    monkeypatch.setattr(merge_module, "_run_merge_locked", lambda *a, **k: pytest.fail("went on"))
    result = world.merge("unit")
    assert not result.ok and result.reason == "closing-keyword" and "git log exploded" in result.message


def test_a_remote_that_cannot_be_fetched_refuses_the_branch(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    world.branch("unit", "unit work", "finished #415")
    assert not world.lock.exists()
    _git(world.checkout, "remote", "set-url", "origin", str(tmp_path / "no-such-origin.git"))
    monkeypatch.setattr(merge_module, "_run_merge_locked", lambda *a, **k: pytest.fail("went on"))
    result = world.merge("unit")
    assert not result.ok and result.reason == "closing-keyword"
    assert "could not fetch origin" in result.message and "retry the merge" in result.message
    lines = world.log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and "closing-keyword" in lines[0] and "could not fetch origin" in lines[0]
    assert not world.marker.exists()
    assert not world.lock.exists()


def test_a_fetch_that_times_out_twice_refuses_the_branch(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    world.branch("unit", "unit work", "finished #415")
    calls = []

    def slow(*a, **k):
        calls.append(a)
        raise subprocess.TimeoutExpired("git fetch", 60)
    monkeypatch.setattr(merge_module.subprocess, "run", slow)
    monkeypatch.setattr(merge_module, "_run_merge_locked", lambda *a, **k: pytest.fail("went on"))
    lines = merge_module._closing_keyword_lines("origin/unit", cwd=world.checkout, remote="origin", main="main")
    assert len(calls) == 2 and len(lines) == 1 and "could not fetch origin" in lines[0]


def test_an_unresolved_branch_is_left_to_the_merge(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    sentinel = MergeResult(ok=False, reason="sync", message="the merge reports it")
    monkeypatch.setattr(merge_module, "_run_merge_locked", lambda *a, **k: sentinel)
    result = world.merge("nonexistent")
    assert result is sentinel


def test_run_merge_command_prints_the_refusal_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    world = World(tmp_path, monkeypatch)
    sha7 = world.branch("unit", "unit work", "fixes #415")
    monkeypatch.setattr(merge_module, "_preload_repo_modules", lambda: None)

    class Args:
        branch, cwd, suite, lock, timeout, queue_checked, issue = "origin/unit", world.checkout, world.suite, world.lock, 1, True, []
    assert run_merge_command(Args) != 0
    out = capsys.readouterr().out
    assert "closing-keyword" in out and sha7 in out
    assert not world.marker.exists()
