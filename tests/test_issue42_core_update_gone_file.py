"""Issue 42 unit 2: a path `core update` meant to remove that is already gone is removed, not a failure
(rows 3-4), and a failed `git commit` / `add` / `push` always names why (row 5). Real git on a tmp target
(bare origin + clone); the home is a fake `gh` runner, no network. Reuses the helpers of the #28 family."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests import test_issue28_core_update_failed_commit as base
from tools.agentops import core_update
from tools.agentops.core_update import CoreUpdateError, run_core_update

GONE = "docs/agents/gone.md"
# the home dropped GONE (and still has one.md / two.md / three.md); the target's manifest still names it
HOME_FILES = base.NEW_HOME_FILES


def _target_with_gone(tmp_path: Path, *, tracked: bool) -> Path:
    """A target whose own manifest names GONE; `tracked`: committed and present, else never there."""

    root = base._target(tmp_path)
    base._write(root, base.MANIFEST, base._manifest_text({**base.OLD_FILES, GONE: ""}))
    if tracked:
        base._write(root, GONE, "gone v1\n")
    assert base._git(root, "add", "-A").returncode == 0
    assert base._git(root, "commit", "-m", "manifest names GONE").returncode == 0
    assert base._git(root, "push", "origin", "main").returncode == 0
    return root


def _update(root: Path):
    return run_core_update(root, gh_runner=base._fake_gh(HOME_FILES), live_check=lambda: False,
                           step_runner=lambda _root: [])


# Row 3 -- the manifest drops a path the tmp target does not have: counted in `removed`, nothing raised,
# and the commit (which cannot `git add` a path that does not exist) still lands.
def test_row3_path_the_target_never_had_is_removed(tmp_path: Path) -> None:
    root = _target_with_gone(tmp_path, tracked=False)

    result = _update(root)

    assert GONE in result.removed
    assert base._git(root, "status", "--porcelain").stdout.strip() == ""
    assert base._git(root, "log", "-1", "--format=%s").stdout.strip().startswith("Core update:")


# Row 4 -- the WinError case. `core_update.py:766` (`(root / entry).unlink()`) ran on a path that
# `pending_removed` (:723-726) had seen as a file, but another `core update` of the same target (two home
# merges 8 s apart) deleted it in between: `[WinError 2] ... effort-high.md`. Reproduced by the file
# vanishing between phase 1 and phase 2 (inside the dirty-path preflight that sits between them).
def test_row4_file_gone_between_the_plan_and_the_unlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _target_with_gone(tmp_path, tracked=True)
    real = core_update._dirty_paths

    def vanishing(root_: Path, paths: set[str]) -> list[str]:
        found = real(root_, paths)
        (root_ / GONE).unlink()  # the concurrent update's unlink
        return found

    monkeypatch.setattr(core_update, "_dirty_paths", vanishing)

    result = _update(root)

    assert GONE in result.removed
    assert base._git(root, "status", "--porcelain").stdout.strip() == ""


# Row 4b -- the same line for a path the failed-step `_restore` (:662-670) deletes: gone is fine.
def test_row4b_restore_tolerates_a_gone_path(tmp_path: Path) -> None:
    core_update._restore(tmp_path, {"docs/not-there.md": None})


def _stdout_hook(root: Path, name: str) -> None:
    path = root / ".git" / "hooks" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"#!/bin/sh\necho refused-on-stdout-by-test-hook\nexit 1\n")
    path.chmod(path.stat().st_mode | 0o100)


# Row 5 -- a commit hook that prints its refusal to stdout and exits 1: the text holds that line.
def test_row5_commit_refusal_on_stdout_is_named(tmp_path: Path) -> None:
    root = base._target(tmp_path)
    _stdout_hook(root, "pre-commit")

    error = base._run_update(root)

    assert "refused-on-stdout-by-test-hook" in str(error)
    assert "failed:;" not in str(error) and "failed: ;" not in str(error)


def _fake_git_rc(monkeypatch: pytest.MonkeyPatch, subcommand: str, out: str, err: str) -> None:
    real = core_update._git

    def fake(cwd: Path, *args: str, env=None):
        if args and args[0] == subcommand:
            return subprocess.CompletedProcess(["git", *args], 7, out, err)
        return real(cwd, *args, env=env)

    monkeypatch.setattr(core_update, "_git", fake)


@pytest.mark.parametrize("subcommand,out,err,expected", [
    ("commit", "", "", "rc 7"),
    ("commit", "\n  stdout reason\nmore\n", "\n", "stdout reason"),
    ("commit", "stdout reason", "\nstderr reason\nmore", "stderr reason"),
    ("add", "", "", "rc 7"),
    ("add", "add said this\n", "", "add said this"),
])
def test_row5_reason_falls_back_stderr_stdout_rc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                  subcommand: str, out: str, err: str, expected: str) -> None:
    root = base._target(tmp_path)
    _fake_git_rc(monkeypatch, subcommand, out, err)

    error = base._run_update(root)

    assert f"git {subcommand} failed: {expected};" in str(error)


@pytest.mark.parametrize("out,err,expected", [("", "", "rc 7"), ("push said this\n", "", "push said this")])
def test_row5_push_reason_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                      out: str, err: str, expected: str) -> None:
    root = base._target(tmp_path)
    _fake_git_rc(monkeypatch, "push", out, err)

    error = base._run_update(root)

    assert f"push refused ({expected})" in str(error)
