"""Issue 1116: in a target, a merge or a commit may not change a core file (a manifest path)
unless the change is a core update's own commit (``Core update: <sha8>``). At the home nothing
changes. Tests run the real merge and commit commands on fake repos (bare origin + clones); no gh."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.agentops import merge as merge_module
from tools.agentops.merge import run_commit, run_commit_command, run_merge

CORE_FILES = [f"core/f{n:02d}.txt" for n in range(1, 13)]  # twelve manifest paths
TRACKER = "docs/agents/issue-tracker.md"  # a TARGET_OWNED_ENTRIES path, also a manifest path
TOOL_FILE = "tools/agentops/helper.py"  # a manifest path under a directory a commit may name
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


def _world(tmp_path: Path, update_rule: str) -> tuple[Path, Path, Path]:
    """A bare origin whose main holds the target's settings, its manifest and the core files.
    Returns (bare, checkout, lock_path)."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = _clone(bare, tmp_path / "seed")
    _write(seed, "config/orchestration.json", json.dumps({"update_rule": update_rule}) + "\n")
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


def _core_branch(tmp_path: Path, bare: Path, name: str = "core-edit") -> str:
    """One ordinary commit touching one manifest file (the row-1 branch)."""
    return _branch(tmp_path, bare, name, [("ordinary change", {CORE_FILES[0]: "v1\n"})])


# Row 1 -- target, a branch touching one manifest file: refused, file named, main not moved.
def test_row1_target_merge_of_a_manifest_edit_is_refused(tmp_path: Path) -> None:
    bare, checkout, lock = _world(tmp_path, "word")
    ref = _core_branch(tmp_path, bare)
    before = _main(bare)

    result = run_merge(ref, GREEN, cwd=checkout, lock_path=lock, lock_timeout=60)

    assert not result.ok, result.render()
    assert result.reason == "core-manifest", result.render()
    assert CORE_FILES[0] in result.message
    assert "write a core issue at the home" in result.message
    assert "python -m tools.agentops core issue" in result.message
    assert _main(bare) == before, "a refused merge moved main"


# Row 2 -- target, a `Core update:` commit touching twelve manifest files: passes.
def test_row2_target_core_update_commit_merges(tmp_path: Path) -> None:
    bare, checkout, lock = _world(tmp_path, "word")
    ref = _branch(tmp_path, bare, "core-update",
                  [("Core update: 0123abcd", {rel: "v1\n" for rel in CORE_FILES})])

    result = run_merge(ref, GREEN, cwd=checkout, lock_path=lock, lock_timeout=60)

    assert result.ok, result.render()
    assert result.reason != "core-manifest"


# Row 3 -- target, a branch touching only local files and a TARGET_OWNED_ENTRIES file: passes.
def test_row3_target_owned_and_local_edits_merge(tmp_path: Path) -> None:
    bare, checkout, lock = _world(tmp_path, "word")
    ref = _branch(tmp_path, bare, "local-work", [
        ("local work", {"local/notes.txt": "x\n"}),
        ("tracker of the target", {TRACKER: "tracker v1\n"}),
    ])

    result = run_merge(ref, GREEN, cwd=checkout, lock_path=lock, lock_timeout=60)

    assert result.ok, result.render()
    assert result.reason != "core-manifest"


# Row 4 -- target, `agentops commit` of a manifest file: exit 1, nothing committed.
def test_row4_target_commit_of_a_manifest_file_exits_1(tmp_path: Path, capsys) -> None:
    bare, checkout, lock = _world(tmp_path, "word")
    _write(checkout, "local/notes.txt", "x\n")
    local = run_commit(["local/notes.txt"], "local note", cwd=checkout, lock_path=lock, lock_timeout=60)
    assert local.ok, local.message  # a local file still commits

    _write(checkout, CORE_FILES[0], "v1\n")
    head_before = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    result = run_commit([CORE_FILES[0]], "edit core", cwd=checkout, lock_path=lock, lock_timeout=60)

    assert not result.ok, result.message
    assert result.reason == "core-manifest", result.message
    assert CORE_FILES[0] in result.message
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == head_before

    rc = run_commit_command(argparse.Namespace(
        paths=[CORE_FILES[0]], message="edit core", cwd=checkout, lock=lock, timeout=60,
        state_entry=None, state_as="", review_entry=None))
    assert rc == 1
    assert CORE_FILES[0] in capsys.readouterr().out


def _cli_commit(checkout: Path, lock: Path, paths: list[str]) -> int:
    """`agentops commit <paths>` through the command's own entry point; returns its exit code."""
    return run_commit_command(argparse.Namespace(
        paths=paths, message="edit", cwd=checkout, lock=lock, timeout=60,
        state_entry=None, state_as="", review_entry=None))


# Pathspec forms (issue 1116 send-back): git resolves the paths, so no written form can slip past.
def test_commit_dot_with_a_manifest_file_changed_exits_1(tmp_path: Path, capsys) -> None:
    _bare, checkout, lock = _world(tmp_path, "word")
    _write(checkout, CORE_FILES[0], "v1\n")
    head_before = _git(checkout, "rev-parse", "HEAD").stdout.strip()

    assert _cli_commit(checkout, lock, ["."]) == 1
    assert CORE_FILES[0] in capsys.readouterr().out
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == head_before


def test_commit_dot_slash_directory_with_a_manifest_file_exits_1(tmp_path: Path, capsys) -> None:
    _bare, checkout, lock = _world(tmp_path, "word")
    _write(checkout, TOOL_FILE, "helper v1\n")
    head_before = _git(checkout, "rev-parse", "HEAD").stdout.strip()

    assert _cli_commit(checkout, lock, ["./tools/agentops"]) == 1
    assert TOOL_FILE in capsys.readouterr().out
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == head_before


def test_commit_backslash_directory_with_a_manifest_file_exits_1(tmp_path: Path, capsys) -> None:
    _bare, checkout, lock = _world(tmp_path, "word")
    _write(checkout, TOOL_FILE, "helper v1\n")

    assert _cli_commit(checkout, lock, ["tools\\agentops"]) == 1
    assert TOOL_FILE in capsys.readouterr().out


def test_commit_dot_of_a_local_only_change_still_commits(tmp_path: Path) -> None:
    _bare, checkout, lock = _world(tmp_path, "word")
    _write(checkout, "local/notes.txt", "x\n")

    result = run_commit(["."], "local note", cwd=checkout, lock_path=lock, lock_timeout=60)

    assert result.ok, result.message
    assert result.reason != "core-manifest"


# Row 5 -- home (update_rule home): the row-1 branch merges as today.
def test_row5_home_merges_the_same_manifest_edit(tmp_path: Path) -> None:
    bare, checkout, lock = _world(tmp_path, "home")
    ref = _core_branch(tmp_path, bare)

    result = run_merge(ref, GREEN, cwd=checkout, lock_path=lock, lock_timeout=60)

    assert result.ok, result.render()
    assert result.reason != "core-manifest"
