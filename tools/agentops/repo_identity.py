"""#239: a repo's GitHub name, the name the heavy-run line and the owner's repo order use for it.

    repo_name(cwd) -> str             the part after `/` of the `origin` of cwd's MAIN worktree; no origin (or no git
                                      repo) gives the main worktree's folder name, never a linked worktree's folder
    known_names(root) -> list[str]    root's own GitHub name, then the GitHub names of its config's `targets`
    matches(order_name, name) -> bool an order name matches a repo name when, case-folded, they are equal or the repo
                                      name starts with the order name and a dash

Stdlib only: the core ships it into every target like the rest of `tools/agentops`.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .util import no_window_flags

_PATH_SEPARATORS = re.compile(r"[/:\\]")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """One git call in `cwd`, output as text, no console window (#216)."""
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=no_window_flags())


def main_worktree(cwd) -> Path:
    """The main worktree of the repo holding `cwd` (cwd itself when it is not a git checkout or not a folder)."""
    path = Path(str(cwd))
    if not path.is_dir():
        return path
    proc = _git(path, "worktree", "list", "--porcelain")
    if proc.returncode != 0:
        return path
    for line in proc.stdout.splitlines():  # the first entry is always the main worktree
        if line.startswith("worktree "):
            return Path(line[len("worktree "):].strip())
    return path


def _origin_name(main: Path) -> str | None:
    if not main.is_dir():
        return None
    proc = _git(main, "remote", "get-url", "origin")
    url = proc.stdout.strip().rstrip("/\\") if proc.returncode == 0 else ""
    if not url:
        return None
    name = _PATH_SEPARATORS.split(url)[-1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    return name or None


def repo_name(cwd) -> str:
    """The GitHub repo name of the repo holding `cwd`: the main worktree's origin, else the main folder's name."""
    main = main_worktree(cwd)
    return _origin_name(main) or main.name


def known_names(root) -> list[str]:
    """The names a repo order may use: `root`'s own GitHub name, then each `targets` entry's (`owner/name` -> `name`)."""
    root = Path(str(root))
    names = [repo_name(root)]
    try:
        settings = json.loads((root / "config" / "orchestration.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    targets = settings.get("targets") if isinstance(settings, dict) else None
    for entry in targets or []:
        repo = entry.get("repo") if isinstance(entry, dict) else None
        if isinstance(repo, str) and repo.strip():
            names.append(repo.strip().rsplit("/", 1)[-1])
    return list(dict.fromkeys(names))


def matches(order_name: str, name: str) -> bool:
    """True when the order name is the repo name, case-folded, or the start of it up to a dash (an order name `alpha` matches a repo `alpha-service`)."""
    order, repo = order_name.casefold(), name.casefold()
    return repo == order or repo.startswith(order + "-")


def matched_name(order_name: str, known: list[str]) -> str | None:
    """The first of `known` that `order_name` matches, or None."""
    return next((name for name in known if matches(order_name, name)), None)
