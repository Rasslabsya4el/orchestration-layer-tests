"""Issue 270: a mod the branch deleted no longer runs in the merge's tests.

A suite worktree reused across commits keeps a mod's ignored ``hooks/draw.ts`` after a branch deletes the mod, so the
folder stays on disk; ``_mod_dirs`` counts a folder only when git tracks a file under it. Seam: ``_mod_dirs`` on a
temporary git repo and on a plain folder."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tools.agentops.merge import _mod_dirs
from tools.agentops.util import no_window_flags


def _git(cwd: Path, *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", creationflags=no_window_flags())
    assert result.returncode == 0, f"git {args} failed in {cwd}: {result.stderr}"


def _write(root: Path, rel: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("export {};\n", encoding="utf-8")


def test_untracked_mod_folder_is_not_a_mod(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "agentops-test@example.invalid")
    _git(tmp_path, "config", "user.name", "agentops test")
    for rel in ("mods/a/hooks/x.ts", "mods/shared/draw.ts"):
        _write(tmp_path, rel)
    _git(tmp_path, "add", "mods")
    _git(tmp_path, "commit", "-q", "-m", "mods")
    _write(tmp_path, "mods/b/hooks/draw.ts")  # left behind by a deleted mod, never tracked

    assert _mod_dirs(tmp_path) == ["a"]


def test_plain_folder_keeps_the_file_system_reading(tmp_path: Path) -> None:
    for rel in ("mods/a/hooks/x.ts", "mods/b/hooks/draw.ts", "mods/shared/draw.ts"):
        _write(tmp_path, rel)

    assert _mod_dirs(tmp_path) == ["a", "b"]
