"""`python -m tools.agentops guard install | check` (#354, U4): the user-level half of the permission guard.

`install` puts `permissions.defaultMode = "bypassPermissions"` into the user settings file and touches
nothing else. `check` states five facts, one line each, and exits 1 when any fails.
"""

from __future__ import annotations

import json
from pathlib import Path

from .util import walk_paths

DENY_LINES = (
    "mcp__ccd_session_mgmt__set_session_permission_mode",
    "Bash(git push --force*)",
    "Bash(git push -f*)",
    "Bash(git clean*)",
    "Bash(git worktree remove*)",
    "Bash(gh repo delete*)",
    "Bash(gh issue delete*)",
    "Bash(gh auth token*)",
    "Bash(format *)",
    "Bash(diskpart*)",
    "Bash(shutdown*)",
    "Bash(bcdedit*)",
)
LAUNCH_DIRS = ("src", "tools", "scripts", "docs/agents", ".claude/agents")
NEEDLE = "permission-mode" + " auto"  # split, so this file is not its own hit
MODE = "bypassPermissions"


def default_user_settings() -> Path:
    return Path.home() / ".claude" / "settings.json"


def default_repo() -> Path:
    return Path(__file__).resolve().parents[2]


def install(path: Path) -> list[str]:
    """Write the default mode; return what was added or changed (empty on a rerun)."""
    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    data = json.loads(raw) if raw.strip() else {}
    changes: list[str] = []
    perms = data.get("permissions")
    if not isinstance(perms, dict):
        perms = {}
        data["permissions"] = perms
        changes.append("permissions (added)")
    if perms.get("defaultMode") != MODE:
        changes.append(f"permissions.defaultMode = {MODE}")
        perms["defaultMode"] = MODE
    if changes:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return changes


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def launch_hits(repo: Path) -> list[str]:
    hits = []
    for d in LAUNCH_DIRS:
        root = repo / d
        for f in sorted(walk_paths(root)) if root.is_dir() else []:
            if not f.is_file() or "__pycache__" in f.parts:
                continue
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            hits += [f"{f.relative_to(repo).as_posix()}:{i}" for i, ln in enumerate(lines, 1) if NEEDLE in ln]
    return hits


def default_claude_json() -> Path:
    return Path.home() / ".claude.json"


DISCLAIMER_CMD = "claude --dangerously-skip-permissions"


def disclaimer_accepted(claude_json: Path) -> bool:
    return _load(claude_json).get("bypassPermissionsModeAccepted") is True


def check(user_path: Path, repo: Path, claude_json: Path | None = None) -> list[tuple[bool, str]]:
    user = _load(user_path)
    user_perms = user.get("permissions")
    mode = user_perms.get("defaultMode") if isinstance(user_perms, dict) else None
    facts = [(mode == MODE, f"user settings permissions.defaultMode is {mode!r} (want {MODE!r})")]
    repo_settings = _load(repo / ".claude" / "settings.json")
    hooks_cfg = repo_settings.get("hooks")
    entries = hooks_cfg.get("PreToolUse", []) if isinstance(hooks_cfg, dict) else []
    has_hook = any(
        "permission_guard.py" in str(h.get("command", ""))
        for entry in entries if isinstance(entry, dict)
        for h in entry.get("hooks", []) if isinstance(h, dict))
    facts.append((has_hook, "repo .claude/settings.json has a PreToolUse hook running permission_guard.py"))
    perms = repo_settings.get("permissions")
    deny = perms.get("deny", []) if isinstance(perms, dict) else []
    missing = [d for d in DENY_LINES if d not in deny]
    facts.append((not missing, f"repo permissions.deny holds all {len(DENY_LINES)} lines"
                  + (f"; missing: {missing}" if missing else "")))
    hits = launch_hits(repo)
    facts.append((not hits, f"{len(hits)} hits of {NEEDLE!r} in {', '.join(LAUNCH_DIRS)}"
                  + (f": {hits}" if hits else "")))
    accepted = disclaimer_accepted(claude_json or default_claude_json())
    facts.append((accepted, "the bypassPermissions disclaimer is accepted for this user"
                  + ("" if accepted else f"; run once in a terminal and accept: {DISCLAIMER_CMD}")))
    return facts


def add_subparser(sub) -> None:
    parser = sub.add_parser("guard", help="the permission guard's user-level install and check (#354)")
    parser.add_argument("action", choices=("install", "check"))
    parser.add_argument("--path", type=Path, default=None, help="user settings file (default ~/.claude/settings.json)")
    parser.add_argument("--repo", type=Path, default=None, help="repo root for check (default: this checkout)")
    parser.add_argument("--claude-json", type=Path, default=None, help="user config file (default ~/.claude.json)")


def run_guard_command(args) -> int:
    path = args.path or default_user_settings()
    cj = args.claude_json or default_claude_json()
    if args.action == "install":
        changes = install(path)
        print("\n".join(changes) if changes else "0 changes")
        if not disclaimer_accepted(cj):
            print(f"bypassPermissions disclaimer not accepted yet: run once in a terminal and accept: {DISCLAIMER_CMD}")
        return 0
    facts = check(path, args.repo or default_repo(), cj)
    for ok, text in facts:
        print(("OK   " if ok else "FAIL ") + text)
    return 0 if all(ok for ok, _ in facts) else 1
