"""Issue 54: the home ignores compiled Python caches, and a merge refuses a branch whose tree tracks
them (``__pycache__`` component or ``.pyc`` name) before the merge lock is taken. Tests run the real
merge on fake repos (bare origin + clones); no gh."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.agentops import detach
from tools.agentops import merge as merge_module
from tools.agentops.merge import run_merge

REPO = Path(__file__).resolve().parents[1]
CORE_FILES = [f"core/f{n:02d}.txt" for n in range(1, 13)]
TRACKER = "docs/agents/issue-tracker.md"
TOOL_FILE = "tools/agentops/helper.py"
GREEN = f'"{sys.executable}" -c "pass"'


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(merge_module, "SUITE_OUTPUT_DIR", tmp_path / "suite_out")
    monkeypatch.setattr(merge_module, "MERGE_LOG_PATH", tmp_path / "merge.log")
    monkeypatch.setattr(merge_module, "resolve_suite_cmd", lambda explicit=None: explicit or GREEN)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    assert result.returncode == 0, f"git {args} failed in {cwd}: {result.stderr}"
    return result


def _clone(bare: Path, dest: Path) -> Path:
    _git(dest.parent, "clone", "-q", str(bare), str(dest))
    _git(dest, "config", "user.email", "agentops-test@example.invalid")
    _git(dest, "config", "user.name", "agentops test")
    return dest


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _world(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A bare origin whose main holds a target's settings and the core files.
    Returns (bare, checkout, lock_path)."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = _clone(bare, tmp_path / "seed")
    _write(seed, "config/orchestration.json", json.dumps({"update_rule": "word"}) + "\n")
    _write(seed, "tools/agentops/core_manifest.txt", "\n".join(CORE_FILES + [TRACKER, TOOL_FILE]) + "\n")
    for rel in CORE_FILES:
        _write(seed, rel, "v0\n")
    _write(seed, TRACKER, "tracker v0\n")
    _write(seed, TOOL_FILE, "helper v0\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "initial")
    _git(seed, "push", "-q", "origin", "main")
    checkout = _clone(bare, tmp_path / "checkout")
    lock = tmp_path / "var" / "locks" / "merge.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    return bare, checkout, lock


def _branch(tmp_path: Path, bare: Path, name: str,
            commits: list[tuple[str, dict[str, str]]]) -> str:
    """A branch off origin/main with one commit per (subject, {path: text}); pushed to origin."""
    worker = _clone(bare, tmp_path / f"worker-{name}")
    _git(worker, "checkout", "-q", "-b", name)
    for subject, files in commits:
        for rel, text in files.items():
            _write(worker, rel, text)
        _git(worker, "add", "-A")
        _git(worker, "commit", "-q", "-m", subject)
    _git(worker, "push", "-q", "origin", name)
    return f"origin/{name}"


def _main(bare: Path) -> str:
    return _git(bare, "rev-parse", "main").stdout.strip()


def _local_ref(checkout: Path, name: str) -> str:
    """A worker's branch as the checkout sees it in the real repo: a local ref (the shared repo's
    refs/heads), not only origin/<name>."""
    _git(checkout, "fetch", "-q", "origin", f"{name}:{name}")
    return name


# Row 2 -- a branch tracking one cache file is refused before the lock: rc-1 result, count named,
# no lock taken, main not moved.
def test_branch_tracking_a_cache_file_is_refused_before_the_lock(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    bare, checkout, lock = _world(tmp_path)
    _branch(tmp_path, bare, "tracked-cache",
            [("tracks a cache", {"a/__pycache__/b.pyc": "bytecode\n"})])
    ref = _local_ref(checkout, "tracked-cache")
    before = _main(bare)

    def _no_lock(*args, **kwargs):
        raise AssertionError("the merge lock was taken for a branch that tracks caches")

    monkeypatch.setattr(merge_module, "acquire_merge_line", _no_lock)

    result = run_merge(ref, GREEN, cwd=checkout, lock_path=lock, lock_timeout=60)

    assert not result.ok, result.render()
    assert result.reason == "tracked-caches", result.render()
    assert ref in result.message, result.render()
    assert "1 cache file(s)" in result.message, result.render()
    line = result.render()
    assert line.startswith("not merged (tracked-caches): "), line
    assert line == (
        f"not merged (tracked-caches): {ref} tracks 1 cache file(s) (__pycache__ or *.pyc); "
        "git rm -r --cached them on the branch"
    ), line
    assert detach.classify(1, line)[0] == "tracked-caches", line
    assert not lock.exists(), "the refused merge created the merge lock"
    assert _main(bare) == before, "a refused merge moved main"


# Row 2c -- the CLI (run_merge_command, the path ``python -m tools.agentops merge`` takes) exits 1.
def test_merge_command_refuses_a_cache_branch_with_rc_1(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch,
                                                        capsys) -> None:
    bare, checkout, lock = _world(tmp_path)
    _branch(tmp_path, bare, "cli-cache", [("tracks a cache", {"a/__pycache__/b.pyc": "bytecode\n"})])
    ref = _local_ref(checkout, "cli-cache")
    before = _main(bare)

    def _no_lock(*args, **kwargs):
        raise AssertionError("the merge lock was taken for a branch that tracks caches")

    monkeypatch.setattr(merge_module, "acquire_merge_line", _no_lock)
    args = argparse.Namespace(branch=ref, suite=GREEN, cwd=checkout, lock=lock, timeout=60)

    rc = merge_module.run_merge_command(args)

    out = capsys.readouterr().out
    assert rc == 1, out
    assert f"not merged (tracked-caches): {ref} tracks 1 cache file(s)" in out, out
    assert _main(bare) == before, "a refused merge moved main"


# Row 2b -- a branch tracking no cache file gets past the check and merges as before.
def test_branch_without_cache_files_merges(tmp_path: Path) -> None:
    bare, checkout, lock = _world(tmp_path)
    _branch(tmp_path, bare, "plain-work", [("local work", {"local/notes.txt": "x\n"})])
    ref = _local_ref(checkout, "plain-work")

    result = run_merge(ref, GREEN, cwd=checkout, lock_path=lock, lock_timeout=60)

    assert result.ok, result.render()
    assert result.reason != "tracked-caches"


# Row 3 -- the home's ignore file covers the cache paths of the home (check-ignore on the worktree root).
def test_home_gitignore_ignores_cache_files() -> None:
    result = subprocess.run(["git", "check-ignore", "-q", "tests/__pycache__/x.pyc"],
                            cwd=str(REPO), capture_output=True, text=True)
    assert result.returncode == 0, "tests/__pycache__/x.pyc is not ignored at the home root"
    rules = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    #: #59: the owner's endpoint file `opencode.json` is not committed at the home, so it is ignored too
    assert rules == ["__pycache__/", "*.pyc", ".pytest_cache/", ".venv/", "var/", "opencode.json"], rules
