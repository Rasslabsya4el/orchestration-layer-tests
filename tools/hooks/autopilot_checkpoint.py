"""Autopilot checkpoint hook (issue #116, D108).

A `SessionStart` hook (matchers `startup`, `compact`, `clear`) for a background autopilot brain
session (`claude --bg --agent brain --name "autopilot #N" --settings
tools/agentops/autopilot_settings.json`, started by `tools/agentops/dispatch.py`). Only in a session
named exactly ``autopilot #N`` it injects, as ``additionalContext``, the latest comment on issue `#N`
that starts with ``Autopilot checkpoint``; when there is none yet it injects ``first session on #N``
instead. Any other session (the interactive brain, ORCH, a worker) gets no output.

Beside the checkpoint, it also injects the latest comment starting with ``Autopilot terms`` (#127,
GLOSSARY.md "Autopilot terms") when one exists, so a restarted session sees the owner's current terms
without re-reading every comment itself. No terms comment yet: only the checkpoint goes out, exactly
as before #127.

The session's name is read from the stdin payload's `session_title` field first (present from the
first `SessionStart`, unlike the transcript, which does not exist yet at `startup`: measured #116,
2026-09-27). When `session_title` is absent or does not match, the transcript's `agent-name` line is
the fallback, the same trick `tools/hooks/brain_reset_nudge.py` uses for `agentSetting`: Claude Code
writes one `{"type": "agent-name", "agentName": "autopilot #N", ...}` line near the top of every
transcript for a `--name`d session (measured 2026-09-27 on live `orch #N` transcripts under
`~/.claude/projects/...`).

Never raises: any error, bad stdin, or `gh` failure results in a silent exit 0.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

HEAD_CHUNK_SIZE = 64 * 1024
GH_TIMEOUT_SECONDS = 5
#: `tools.agentops.util.no_window_flags`, copied rather than imported for the same standalone
#: reason as `CHECKPOINT_PREFIX` below (#269): no console window for the `gh` child on Windows.
_NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
#: `autopilot.AUTOPILOT_CHECKPOINT_PREFIX`, copied rather than imported: this hook must run standalone
#: under a plain `python`, with no `tools` package on `sys.path` (Claude Code invokes it directly).
CHECKPOINT_PREFIX = "Autopilot checkpoint"
#: `autopilot.AUTOPILOT_TERMS_PREFIX`, copied for the same reason as `CHECKPOINT_PREFIX` above.
TERMS_PREFIX = "Autopilot terms"
#: #564: the checkpoint comment is at most this many characters; over it the hook tells the session to fold it first.
CHECKPOINT_CAP_CHARS = 10_000
_SESSION_START_SOURCES = frozenset({"startup", "compact", "clear"})
_NAME_RE = re.compile(r"^autopilot\s+#(\d+)$")
#: #323: the tickets repo is read by the core's settings reader; this hook runs under a plain `python`, so the
#: repo root goes on `sys.path` first (as `brain_reset_nudge.py` does).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _read_head(path: str, chunk_size: int = HEAD_CHUNK_SIZE) -> str:
    try:
        with open(path, "rb") as f:
            return f.read(chunk_size).decode("utf-8", errors="ignore")
    except OSError:
        return ""


def autopilot_issue_number(transcript_path: str | None) -> int | None:
    """The `N` this session is named `autopilot #N` for, read from its transcript's `agent-name`
    line; `None` for any session not named that way (including a missing/unreadable transcript)."""

    if not transcript_path or not isinstance(transcript_path, str):
        return None
    for line in _read_head(transcript_path).split("\n"):
        if '"agent-name"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "agent-name":
            continue
        match = _NAME_RE.match(str(obj.get("agentName") or ""))
        return int(match.group(1)) if match else None
    return None


def _tickets_repo_or_none() -> str | None:
    """#323: the repo the tickets live in (setting `tickets_repo`), or None when the settings cannot be read."""
    try:
        from tools.agentops.core_settings import tickets_repo
        return tickets_repo()
    except (ImportError, RuntimeError):
        return None


def _gh_comments(n: int, timeout: int = GH_TIMEOUT_SECONDS) -> list[dict] | None:
    """Comments on issue `n` as `{"body", "url"}` dicts, oldest first, or `None` on any failure. `AGENTOPS_HOOK_GH`
    overrides the `gh` command for tests (same mechanism as `brain_reset_nudge.py`'s `_gh_labels`)."""

    repo = _tickets_repo_or_none()
    if repo is None:  # the settings are unreadable: no repo to read the checkpoint from
        return None
    override = os.environ.get("AGENTOPS_HOOK_GH")
    # posix=False: on Windows, paths contain backslashes that posix-mode shlex would escape-interpret.
    base_cmd = shlex.split(override, posix=False) if override else ["gh"]
    args = base_cmd + ["issue", "view", str(n), "--repo", repo, "--json", "comments"]
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW_FLAGS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        obj = json.loads(result.stdout)
    except ValueError:
        return None
    comments = obj.get("comments") if isinstance(obj, dict) else None
    if not isinstance(comments, list):
        return None
    return [{"body": str(c.get("body") or ""), "url": str(c.get("url") or "")}
            for c in comments if isinstance(c, dict)]


_COMMENT_ID_RE = re.compile(r"#issuecomment-(\d+)$")


def latest_checkpoint_id(comments: list[dict]) -> str:
    """The NUMERIC id of the latest checkpoint comment (the one `gh api --method PATCH
    repos/<repo>/issues/comments/<id>` takes), parsed from its url; `"none"` when no checkpoint
    comment exists, `"unknown"` when it exists but its url does not parse (#562)."""

    for c in reversed(comments):
        if c["body"].startswith(CHECKPOINT_PREFIX):
            match = _COMMENT_ID_RE.search(c["url"])
            return match.group(1) if match else "unknown"
    return "none"


def latest_checkpoint(comments: list[str]) -> str | None:
    """The latest (last) comment body starting with `CHECKPOINT_PREFIX`, or `None`."""

    for body in reversed(comments):
        if body.startswith(CHECKPOINT_PREFIX):
            return body
    return None


def latest_terms(comments: list[str]) -> str | None:
    """The latest (last) comment body starting with `TERMS_PREFIX`, or `None` (#127)."""

    for body in reversed(comments):
        if body.startswith(TERMS_PREFIX):
            return body
    return None


#: #1133: a checkpoint's `questions:` entry lists the questions waiting for the owner; each listed question
#: needs a `one-way door: <what>` reason. A reversible question is a provisional decision, never a question.
_QUESTIONS_RE = re.compile(r"^[ \t]*(?:[-*][ \t]+)?\*{0,2}questions\*{0,2}[ \t]*:\*{0,2}(.*)$", re.IGNORECASE)
_BULLET_RE = re.compile(r"^[ \t]*(?:[-*]|\d+[.)])[ \t]+")
_COUNT_RE = re.compile(r"^\s*(\d+)\b")
_ONE_WAY_RE = re.compile(r"one-way door:[ \t]*\S", re.IGNORECASE)
_NEW_FIELD_RE = re.compile(r"^\*{0,2}[A-Za-z][\w /-]{0,40}\*{0,2}:")


def questions_refusal(body: str) -> str | None:
    """The refusal text when a checkpoint's `questions:` entry lists a question with no `one-way door:`
    reason (or counts more questions than it lists), else `None`. `questions: 0`, `none`, or no
    `questions:` line at all pass (#1133)."""

    lines = body.split("\n")
    start = next((i for i, line in enumerate(lines) if _QUESTIONS_RE.match(line)), None)
    if start is None:
        return None
    value = _QUESTIONS_RE.match(lines[start]).group(1).strip()
    count_match = _COUNT_RE.match(value)
    count = int(count_match.group(1)) if count_match else 0
    if count_match:
        value = value[count_match.end():].lstrip(" \t:-—.")
    items: list[str] = []
    if value and value.lower() not in ("none", "no"):
        items.append(value)
    for line in lines[start + 1:]:
        if not line.strip():
            break
        if not line[:1].isspace() and not _BULLET_RE.match(line) and _NEW_FIELD_RE.match(line):
            break
        if _BULLET_RE.match(line) or not items:
            items.append(_BULLET_RE.sub("", line, count=1).strip())
        else:
            items[-1] += " " + line.strip()
    if not items and count == 0:
        return None
    for item in items:
        if not _ONE_WAY_RE.search(item):
            sample = item if len(item) <= 120 else item[:117] + "..."
            return (f"checkpoint refused: a `questions:` entry lists a question with no `one-way door:` reason "
                    f"({sample}); name what cannot be walked back as `one-way door: <what>`. A reversible "
                    f"question is a provisional decision instead, not a question to the owner.")
    if len(items) < count:
        return (f"checkpoint refused: `questions: {count}` counts {count} questions but lists {len(items)}; "
                f"each question needs `one-way door: <what>`. A reversible question is a provisional "
                f"decision instead, not a question to the owner.")
    return None


def _goal_from_title(session_title: object) -> int | None:
    """The `N` in a `session_title` of `"autopilot #N"`, or `None` (#116: the stdin payload's own
    session name, read before the transcript exists at `startup`)."""

    match = _NAME_RE.match(str(session_title or "").strip())
    return int(match.group(1)) if match else None


# the role(s) each rule binds (#320): the SessionStart context of the background autopilot brain session
# (agentSetting brain) only
RULE_ROLES: dict[str, tuple[str, ...]] = {"session_start_context": ("brain",)}


def handle_session_start(payload: dict) -> dict | None:
    if payload.get("hook_event_name") != "SessionStart":
        return None
    if payload.get("source") not in _SESSION_START_SOURCES:
        return None
    goal = _goal_from_title(payload.get("session_title"))
    if goal is None:
        goal = autopilot_issue_number(payload.get("transcript_path"))
    if goal is None:
        return None
    comments = _gh_comments(goal)
    bodies = [c["body"] for c in comments] if comments is not None else None
    checkpoint = latest_checkpoint(bodies) if bodies is not None else None
    terms = latest_terms(bodies) if bodies is not None else None
    # a failed read is not "no checkpoint": `none` would make the session post a second one.
    checkpoint_id = latest_checkpoint_id(comments) if comments is not None else "unknown"
    if comments is None:
        # #563: a failed read is never a fresh start; say so in words, with the command to look it up.
        ref = _tickets_repo_or_none() or "{owner}/{repo}"  # #323: the tickets repo, as the read used
        checkpoint_text = (
            f"checkpoint read failed on #{goal}: run gh api "
            f"repos/{ref}/issues/{goal}/comments?per_page=100 --paginate "
            f'and take the last comment that starts with "Autopilot checkpoint"; '
            f"never start the goal fresh on a failed read"
        )
    elif checkpoint is None:
        checkpoint_text = f"first session on #{goal}"
    else:
        checkpoint_text = checkpoint
    # the latest `Autopilot terms` comment goes in beside the checkpoint (#127 numbers item 1);
    # no terms comment yet -> only the checkpoint, exactly as before #127.
    context = f"{terms}\n\n{checkpoint_text}" if terms is not None else checkpoint_text
    # #562: the session edits this one comment in place, so it is told which one (numeric id).
    context = f"{context}\n\ncheckpoint comment: {checkpoint_id}"
    if checkpoint is not None and len(checkpoint) > CHECKPOINT_CAP_CHARS:
        context = (
            f"checkpoint {len(checkpoint)} chars over the cap 10,000: "
            f"fold it in this session before any other step\n\n{context}"
        )
    # #1133: a bare question in the checkpoint is refused in words, first, so the session fixes it.
    refusal = questions_refusal(checkpoint) if checkpoint is not None else None
    if refusal is not None:
        context = f"{refusal}\n\n{context}"
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }


def run(payload: dict) -> dict | None:
    return handle_session_start(payload)


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        output = run(payload)
    except Exception:
        return 0
    if output is not None:
        try:
            print(json.dumps(output))
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
