"""Install and check the repo's git hooks (#379): core.hooksPath -> tools/githooks.

The path is relative, so every worktree resolves its own copy of the hooks.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .util import no_window_flags

HOOKS_PATH = "tools/githooks"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", creationflags=no_window_flags())


def _current(root: Path) -> str:
    return _git(root, "config", "--get", "core.hooksPath").stdout.strip()


def install(root: Path) -> str:
    """Set core.hooksPath to tools/githooks; return the previous value ("" when unset)."""

    previous = _current(root)
    result = _git(root, "config", "core.hooksPath", HOOKS_PATH)
    if result.returncode != 0:
        raise RuntimeError(f"githooks install failed: {(result.stderr or '').strip()[:200]}")
    return previous


def check(root: Path) -> bool:
    """True when core.hooksPath names the same folder as root/tools/githooks (#1069).

    The stored value may be relative (install writes it so) or absolute (git may store the full
    path); both are resolved before comparing, so the spelling does not matter, only the folder.
    An unset value is red.
    """

    configured = _current(root)
    if not configured:
        return False
    configured_path = Path(configured)
    if not configured_path.is_absolute():
        configured_path = root / configured_path
    return configured_path.resolve() == (root / HOOKS_PATH).resolve()


def add_subparser(sub) -> None:
    gh = sub.add_parser("githooks", help="install or check the repo's git hooks (#379)")
    gh.add_argument("action", choices=["install", "check"])


def run_githooks_command(args) -> int:
    root = Path.cwd()
    if args.action == "install":
        previous = install(root)
        print(f"githooks: core.hooksPath = {HOOKS_PATH} (was {previous or 'unset'})")
        return 0
    if check(root):
        print(f"githooks: core.hooksPath is {_current(root)} (expected {HOOKS_PATH})")
        return 0
    print(f"githooks: core.hooksPath is {_current(root) or 'unset'}, expected {HOOKS_PATH}; "
          "run python -m tools.agentops githooks install")
    return 1
