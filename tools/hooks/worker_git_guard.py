"""Worker git-stash guard hook (issue #215).

A `PreToolUse` hook (matcher `Bash|PowerShell`) in `.claude/agents/worker.md` /
`worker-strong.md`. It refuses (`permissionDecision: deny`) a command that runs
`git stash` in any form that changes the stash list or the working tree: bare
`git stash`, `push`, `save`, `pop`, `apply`, `drop`, `clear`, `branch`,
`create`/`store`; also through `git -C <path> stash ...` and inside a chain
(`&&`, `;`, `|`, a new line). `git stash list` and `git stash show` pass. The
words `git stash` inside a quoted argument (a commit message, an `echo`, a
`grep` pattern) or inside a heredoc's body pass -- both are naturally opaque
to the tokenizer below (a quoted phrase is one token, never two words `git`
and `stash`) or stripped before it runs (a heredoc body).

Why: #215, a worker followed the harness's own reminder about a *safe* stash
procedure to the letter, dropping another session's stash entry by index. A
line of prose in the role files lost to a longer, more detailed text in the
same context; this hook is what makes the refusal load-bearing instead of a
sentence a later instruction can out-argue.

Never raises: any error, bad stdin, unparsable input, or an internal failure
exits 0 with no output -- this hook only ever narrows what a worker's shell
can do, it never gets to break the shell call itself. Every refusal appends
one line to `var/agentops/worker_git_guard.log` (`AGENTOPS_HOOK_LOG`
overrides); a passed command writes nothing.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from shell_words import split_command, strip_heredocs

LOG_PATH = Path(__file__).resolve().parents[2] / "var" / "agentops" / "worker_git_guard.log"

#: `git stash` subcommands that leave the stash list and working tree unchanged.
_SAFE_SUBCOMMANDS = {"list", "show"}

_OP_RE = re.compile(r"(&&|\|\||;|\|)")

REFUSAL_REASON = (
    "git stash changes state shared with every other worktree of this repo and is refused here "
    "(#215). Instead: commit what you hold on your own branch, run `git checkout --detach <base>` "
    "to check the base commit out, run the test, then return with `git checkout <your branch>`."
)


def _tokenize(command: str) -> list[str]:
    """`command`'s words, with shell/PowerShell separators (`&&`, `||`, `;`, `|`) split out
    into their own tokens even when they sit glued to a word (`status;`). A quoted token (one
    that starts with `'` or `"`) is never split further -- it is one opaque word, so `"never git
    stash"` never contributes a bare `git` or `stash` token."""

    tokens: list[str] = []
    for raw in split_command(command):
        if raw and raw[0] in ("'", '"'):
            tokens.append(raw)
            continue
        parts = [p for p in _OP_RE.split(raw) if p != ""]
        tokens.extend(parts)
    return tokens


_SEPARATORS = {"&&", "||", ";", "|"}


def _segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _SEPARATORS:
            if current:
                segments.append(current)
            current = []
            continue
        current.append(tok)
    if current:
        segments.append(current)
    return segments


def _mutating_stash_segment(segment: list[str]) -> bool:
    """`True` when `segment` runs a `git stash` subcommand other than `list`/`show`."""

    try:
        git_idx = segment.index("git")
    except ValueError:
        return False
    try:
        stash_idx = segment.index("stash", git_idx + 1)
    except ValueError:
        return False
    subcommand = None
    for tok in segment[stash_idx + 1 :]:
        if tok.startswith("-"):
            continue
        subcommand = tok
        break
    if subcommand is None:
        return True  # bare `git stash` behaves like `git stash push`
    return subcommand not in _SAFE_SUBCOMMANDS


def is_mutating_stash(command: str) -> bool:
    stripped = strip_heredocs(command)
    tokens = _tokenize(stripped)
    for segment in _segments(tokens):
        if _mutating_stash_segment(segment):
            return True
    return False


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _log(payload: dict, command: str) -> None:
    try:
        path = Path(os.environ.get("AGENTOPS_HOOK_LOG") or LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "agent_id": payload.get("agent_id"),
            "agent_type": payload.get("agent_type"),
            "command": command[:200],
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def run(payload: dict) -> dict | None:
    if payload.get("tool_name") not in ("Bash", "PowerShell"):
        return None
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command:
        return None
    if not is_mutating_stash(command):
        return None
    _log(payload, command)
    return _deny(REFUSAL_REASON)


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        output = run(payload)
    except Exception as exc:  # fail open: never block a shell call on an internal error
        print(f"worker_git_guard: internal error, failing open: {exc}", file=sys.stderr)
        return 0
    if output is not None:
        try:
            print(json.dumps(output))
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
