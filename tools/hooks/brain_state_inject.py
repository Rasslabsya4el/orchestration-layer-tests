"""Brain state inject hook (issue #316).

A `SessionStart` hook (sources `startup`, `clear`, `compact`) for the brain's own chat. A cleared brain
chat starts blind: `docs/agents/brain.md` (Reset) tells it to read its hand-off entry in `docs/STATE.md`
first, but nothing makes it do so. This hook puts that entry in front of it.

The chat's title is the stdin payload's `session_title` field, the same field
`tools/hooks/autopilot_checkpoint.py` reads (measured on #116, 2026-09-27: present from the first
`SessionStart`, before any transcript exists). The title is a brain title when it equals
`brain_chat_title` or any `brain_chats` value of `config/orchestration.json`, read from the project dir
(env `CLAUDE_PROJECT_DIR`, else the payload's `cwd`). A title that is not a brain title, and a background
session (`sessionKind` == `bg` in the payload), get no output.

For the brain it runs `git fetch origin` (at most 10 s, no console window) and reads `docs/STATE.md` from
`origin/<default branch>` (the branch from `origin/HEAD`, else `main`). When the fetch fails it reads the
working copy and the injected text says so. It injects the whole first line starting `Updated (brain)` as
`hookSpecificOutput.additionalContext`, after one line that says this chat is the brain.

Open point, measured live in #316 row 6: if a real clear sends no `session_title`, the fallback is the
first `UserPromptSubmit` of the session, which `tools/hooks/brain_reset_nudge.py` already covers for the
title check (#221). This hook does not build that fallback.

Never raises: any error exits 0 silently and appends one line to ``var/agentops/brain_nudge.log``
(``AGENTOPS_HOOK_LOG`` overrides). Every injection is logged as one line too, for the live check.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

FETCH_TIMEOUT_SECONDS = 10
#: fetch 10 s + two local git calls of 2 s stay under the 15 s hook timeout in the settings files.
GIT_TIMEOUT_SECONDS = 2
#: `tools.agentops.util.no_window_flags`, copied rather than imported: this hook runs standalone under a plain
#: `python` with no `tools` package on `sys.path` (Claude Code invokes it directly). No console window for `git`.
_NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_SESSION_START_SOURCES = frozenset({"startup", "clear", "compact"})
BRAIN_LINE_PREFIX = "Updated (brain)"
STATE_PATH = "docs/STATE.md"
HEAD_TEXT = "This chat is the brain; its hand-off entry from docs/STATE.md follows (docs/agents/brain.md, Reset)"
NO_ENTRY_TEXT = "docs/STATE.md has no brain entry"
FETCH_FAILED_NOTE = "git fetch failed, read from the working copy"
LOG_PATH = Path(__file__).resolve().parents[2] / "var" / "agentops" / "brain_nudge.log"
_ORIGIN_PREFIX = "refs/remotes/origin/"


def _log(payload: dict, action: str, **fields) -> None:
    """Append one JSON line (the same row shape as `brain_reset_nudge.py`'s log writer); never raises."""
    try:
        path = Path(os.environ.get("AGENTOPS_HOOK_LOG") or LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": payload.get("session_id"),
               "event": "SessionStart", "action": action, **fields}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _project_dir(payload: dict) -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env)
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return Path(cwd)
    return Path(__file__).resolve().parents[2]


def _brain_titles(project: Path) -> set[str]:
    """`brain_chat_title` and every `brain_chats` value of `config/orchestration.json`; empty when the file is
    missing or not valid JSON (then no chat is a brain chat)."""
    try:
        with open(project / "config" / "orchestration.json", encoding="utf-8") as fh:
            settings = json.load(fh)
    except (OSError, ValueError):
        return set()
    if not isinstance(settings, dict):
        return set()
    titles: set[str] = set()
    title = settings.get("brain_chat_title")
    if isinstance(title, str) and title:
        titles.add(title)
    chats = settings.get("brain_chats")
    if isinstance(chats, dict):
        titles.update(value for value in chats.values() if isinstance(value, str) and value)
    return titles


def _git(project: Path, *args: str, timeout: int = GIT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess | None:
    """The finished `git` call, or `None` when it could not run or ran past `timeout`."""
    try:
        return subprocess.run(["git", *args], cwd=project, capture_output=True, timeout=timeout,
                              creationflags=_NO_WINDOW_FLAGS)
    except (OSError, subprocess.SubprocessError):
        return None


def _default_branch(project: Path) -> str:
    """The branch `origin/HEAD` points at (`refs/remotes/origin/<branch>`), else `main`."""
    result = _git(project, "symbolic-ref", f"{_ORIGIN_PREFIX}HEAD")
    if result is not None and result.returncode == 0:
        ref = result.stdout.decode("utf-8", errors="replace").strip()
        if ref.startswith(_ORIGIN_PREFIX) and len(ref) > len(_ORIGIN_PREFIX):
            return ref[len(_ORIGIN_PREFIX):]
    return "main"


def _origin_text(project: Path, branch: str) -> str | None:
    result = _git(project, "show", f"origin/{branch}:{STATE_PATH}")
    if result is None or result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def _working_copy_text(project: Path) -> str | None:
    try:
        return (project / STATE_PATH).read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None


def _brain_line(text: str | None) -> str | None:
    """The whole first line of `text` that starts with `Updated (brain)`, or `None`."""
    if text is None:
        return None
    for line in text.splitlines():
        if line.startswith(BRAIN_LINE_PREFIX):
            return line
    return None


def handle_session_start(payload: dict) -> dict | None:
    if payload.get("hook_event_name") != "SessionStart":
        return None
    if payload.get("source") not in _SESSION_START_SOURCES:
        return None
    if payload.get("sessionKind") == "bg":
        return None
    project = _project_dir(payload)
    title = payload.get("session_title")
    if not isinstance(title, str) or title not in _brain_titles(project):
        return None

    fetch = _git(project, "fetch", "origin", timeout=FETCH_TIMEOUT_SECONDS)
    fetched = fetch is not None and fetch.returncode == 0
    notes: list[str] = []
    if fetched:
        branch = _default_branch(project)
        text = _origin_text(project, branch)
        if text is None:
            notes.append(f"origin/{branch} has no {STATE_PATH}, read from the working copy")
            text = _working_copy_text(project)
    else:
        notes.append(FETCH_FAILED_NOTE)
        text = _working_copy_text(project)

    entry = _brain_line(text)
    context = f"{HEAD_TEXT}\n{entry}" if entry is not None else NO_ENTRY_TEXT
    for note in notes:
        context = f"{context}\n{note}"
    _log(payload, "injected", entry=entry is not None, fetch="ok" if fetched else "failed")
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }


def run(payload: dict) -> dict | None:
    return handle_session_start(payload)


def main() -> int:
    payload: dict = {}
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            raise ValueError("stdin payload is not a JSON object")
        payload = loaded
        output = run(payload)
        if output is not None:
            print(json.dumps(output))
    except Exception as exc:
        _log(payload, "error", why=f"{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
