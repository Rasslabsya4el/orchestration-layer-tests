"""Autopilot commit guard hook (issue #142, the brain's decision on #142 comment 5855352848).

A `PreToolUse` hook (matcher `Bash|PowerShell`) for a background autopilot brain session
(`claude --bg --agent brain --name "autopilot #N" --settings tools/agentops/autopilot_settings.json`,
started by `tools/agentops/dispatch.py`). It only reacts to a command that contains ``git commit``;
any other command is left alone (exit 0, no output).

On a `git commit` it collects the paths the commit would include -- staged paths
(`git diff --cached --name-only`), plus, for `-a`/`--all`/a combined short flag containing `a`
(e.g. `-am`), the tracked-modified paths (`git diff --name-only`), plus any explicit pathspecs given
on the command line after `--` or as trailing non-option arguments, plus the paths every `git add`
segment of the *same command* would stage (PreToolUse runs before the whole command, so a `git add
X && git commit ...` has nothing staged yet when the hook runs: #142 follow-up, the live check that
committed a file outside the list because `git diff --cached` was still empty). Every path outside
``ALLOWED_PATTERNS`` (fnmatch/glob, repo-relative, forward slashes) makes the hook deny the tool call
via the documented `PreToolUse` JSON shape; every path inside it, or an empty set, makes the hook
exit 0 with no output.

Why deny rather than let the commit fail on its own: the paths in question are perfectly valid git
paths, git would happily commit them -- the point is to stop an autopilot session from reaching main
with a change ORCH was meant to make (docs/agents/autopilot.md, "ORCH works the tickets, never you").

Never raises: a `git`/`gh` failure, a missing repo, or any internal error fails OPEN (exit 0, no
stdout, one line on stderr) -- this hook only ever narrows what git can do, it does not get to break
the session's ability to commit when it cannot answer the question.

A commit is read only where the shell would actually run one: the words of a `git commit` inside a
heredoc's body (stripped before anything else runs, `shell_words.strip_heredocs`) or inside a quoted
argument to another command (opaque to `shlex`, never split into bare words) are never read as a
commit (#222) -- a real `git commit` elsewhere in the same call is still judged by its own paths, as
before.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys

from shell_words import split_command, strip_heredocs

GIT_TIMEOUT_SECONDS = 5
#: `tools.agentops.util.no_window_flags`, copied rather than imported: this hook must run
#: standalone under a plain `python`, with no `tools` package on `sys.path` (#269): no console
#: window for the `git` child on Windows.
_NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

#: repo-relative, forward-slash, fnmatch/glob patterns an autopilot session may commit unescorted
#: (the brain's decision on #142 comment 5855352848, item 3). Everything else is ORCH's.
ALLOWED_PATTERNS = (
    "docs/research/*-grill.md",
    "docs/adr/**",
    "GLOSSARY.md",
    "docs/STATE.md",
)

REFUSAL_SUFFIX = (
    'this path is a ticket for ORCH: write the ticket, label it ready-for-agent '
    '(docs/agents/autopilot.md "ORCH works the tickets, never you")'
)


def _is_allowed(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    for pattern in ALLOWED_PATTERNS:
        if pattern.endswith("/**"):
            prefix = pattern[:-3]
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return True
        if fnmatch.fnmatch(normalized, pattern):
            return True
    return False


def _run_git(args: list[str], cwd: str | None) -> list[str]:
    """Lines of stdout for a `git` call, or `[]` on any failure (fail-open: callers add nothing)."""

    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS,
            creationflags=_NO_WINDOW_FLAGS,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def _has_commit(tokens: list[str]) -> bool:
    bare = [tok.strip("'\"") for tok in tokens]
    if "git" in bare and "commit" in bare:
        return True
    # `python -m tools.agentops commit ...` / `agentops commit ...` is a commit too (#597)
    return any(
        tok in ("tools.agentops", "agentops") and nxt == "commit" for tok, nxt in zip(bare, bare[1:])
    )


#: tokens that end the `git commit` invocation's own argument list: a shell separator (`&&`,
#: `||`, `;`, `|`), a PowerShell `if` starting the next statement, or a token ending a chained/
#: grouped statement (`;`, `)`, `}`). Reported by the coordinator (#142 follow-up): without this
#: cut, `git commit -m x && git push origin main` reads `git`, `push`, `origin`, `main` as paths.
_SEPARATOR_EXACT = {"&&", "||", ";", "|", "if"}
_SEPARATOR_SUFFIXES = (";", ")", "}")


def _commit_segment(tokens: list[str]) -> list[str]:
    """The tokens of the `git commit` invocation's own arguments: from just after the `commit`
    token up to (not including) the first shell/PowerShell separator. `[]` when there is no
    `commit` token."""

    try:
        commit_idx = next(i for i, tok in enumerate(tokens) if tok.strip("'\"") == "commit")
    except StopIteration:
        return []
    segment = []
    for tok in tokens[commit_idx + 1 :]:
        bare = tok.strip("'\"")
        if bare in _SEPARATOR_EXACT or bare.endswith(_SEPARATOR_SUFFIXES):
            break
        segment.append(tok)
    return segment


def _wants_all(segment: list[str]) -> bool:
    """`segment` is the `git commit` invocation's own arguments only (`_commit_segment`), so a
    later `-a` in a chained command does not count."""

    for tok in segment:
        bare = tok.strip("'\"")
        if bare in ("-a", "--all"):
            return True
        if bare.startswith("-") and not bare.startswith("--") and "a" in bare[1:]:
            return True
    return False


#: flags that take a value as a separate token (skip that token too, it is never a pathspec).
_VALUE_FLAGS = {
    "-m", "--message", "-c", "--reuse-message", "--fixup", "--squash",
    "-F", "--file", "--author", "--date", "-C", "--reedit-message", "--template",
    "-t", "--trailer", "--cleanup",
    # `python -m tools.agentops commit` (tools/agentops/merge.py add_commit_subparser) value flags (#545)
    "--state-entry", "--state-as", "--review-entry", "--cwd", "--lock", "--timeout",
}

#: a shell redirection token: `2>&1`, `>file`, `>>file`, `<file`, `>`, `2>`, `&>x` ...
_REDIRECT_RE = re.compile(r"^(\d*|&)(>>?|<)(&\d+|-)?(.*)$")


def _drop_redirections(rest: list[str]) -> list[str]:
    """`rest` without redirection tokens and the bare target token following `>`/`<` alone (#545)."""

    out: list[str] = []
    skip_next = False
    for tok in rest:
        if skip_next:
            skip_next = False
            continue
        match = _REDIRECT_RE.match(tok)
        if match:
            if not match.group(3) and not match.group(4):
                skip_next = True
            continue
        out.append(tok)
    return out


def _explicit_pathspecs(segment: list[str]) -> list[str]:
    """Paths named directly on the command line: anything after `--`, or trailing non-option
    arguments to `git commit` (conservative on purpose: when in doubt, include them, #142 item 2).
    `segment` is the `git commit` invocation's own arguments only (`_commit_segment`)."""

    rest = _drop_redirections([tok.strip("'\"") for tok in segment])
    if "--" in rest:
        return rest[rest.index("--") + 1 :]
    # trailing non-option args: skip flags and the values of flags that take one (-m/-message etc.
    # take an arg already consumed by shlex as a separate token, but conservatively we only treat a
    # bare token that does not start with "-" as a pathspec candidate). A `--flag=value` form is
    # skipped whole by the `startswith("-")` check below; only a flag taking a *separate* token
    # needs `_VALUE_FLAGS` to skip that next token.
    paths = []
    skip_next = False
    for tok in rest:
        if skip_next:
            skip_next = False
            continue
        if tok in _VALUE_FLAGS:
            skip_next = True
            continue
        if tok.startswith("-"):
            continue
        paths.append(tok)
    return paths


def _split_segments(tokens: list[str]) -> list[list[str]]:
    """`tokens` cut into separate command segments at the same separators `_commit_segment` cuts
    at (#142 follow-up: `git add X && git commit ...` is two segments of the one command)."""

    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        bare = tok.strip("'\"")
        if bare in _SEPARATOR_EXACT or bare.endswith(_SEPARATOR_SUFFIXES):
            if current:
                segments.append(current)
            current = []
            continue
        current.append(tok)
    if current:
        segments.append(current)
    return segments


def _status_paths(cwd: str | None, pathspec: str | None) -> list[str]:
    """Paths `git status --porcelain --untracked-files=all [-- <pathspec>]` reports, a rename
    line's `new` side taken instead of `old -> new`."""

    args = ["status", "--porcelain", "--untracked-files=all"]
    if pathspec is not None:
        args = args + ["--", pathspec]
    paths = []
    for line in _run_git(args, cwd):
        # porcelain v1: two status chars, a space, then the path (or "old -> new" for a rename).
        content = line[3:] if len(line) > 3 else line[2:].strip()
        if " -> " in content:
            content = content.split(" -> ", 1)[1]
        content = content.strip()
        if content:
            paths.append(content)
    return paths


#: flags meaning "every tracked+modified/deleted path" for `git add` with no explicit pathspec.
_ADD_ALL_FLAGS = {"-A", "--all", "-u", "--update"}


def _add_segment_paths(tokens: list[str], cwd: str | None) -> set[str]:
    """The paths every `git add ...` segment of the command would stage, resolved through `git
    status` (the add itself has not happened yet when `PreToolUse` runs, #142 follow-up)."""

    found: set[str] = set()
    for segment in _split_segments(tokens):
        bare = [tok.strip("'\"") for tok in segment]
        if not bare or bare[0] != "git":
            continue
        idx = 1
        while idx < len(bare) and bare[idx].startswith("-"):
            idx += 2 if bare[idx] == "-c" else 1
        if idx >= len(bare) or bare[idx] != "add":
            continue
        args = bare[idx + 1 :]
        pathspecs = [a for a in args if not a.startswith("-")]
        wants_all = any(a in _ADD_ALL_FLAGS for a in args)
        if wants_all:
            found.update(_status_paths(cwd, None))
        else:
            for spec in pathspecs:
                found.update(_status_paths(cwd, spec))
    return found


def collect_paths(command: str, cwd: str | None) -> list[str]:
    tokens = split_command(command)
    segment = _commit_segment(tokens)
    paths: set[str] = set()
    paths.update(_run_git(["diff", "--cached", "--name-only"], cwd))
    if _wants_all(segment):
        paths.update(_run_git(["diff", "--name-only"], cwd))
    paths.update(_explicit_pathspecs(segment))
    paths.update(_add_segment_paths(tokens, cwd))
    return sorted(p for p in paths if p)


# the role(s) each rule binds (#320): the refusal of a commit that touches a path outside the allowed set;
# wired only for the background autopilot brain session (agentSetting brain)
RULE_ROLES: dict[str, tuple[str, ...]] = {"commit_refusal": ("brain",)}


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def evaluate(command: str, cwd: str | None) -> dict | None:
    """`None` when the command is allowed to proceed unmodified; the deny payload otherwise.

    `command` is stripped of every heredoc body first (#222): a `git commit` a heredoc merely
    carries as text -- written to a file, echoed, piped to `grep` -- is never a command the shell
    would run, so it never counts as *the* commit `collect_paths` judges below."""

    command = strip_heredocs(command)
    tokens = split_command(command)
    if not _has_commit(tokens):
        return None
    paths = collect_paths(command, cwd)
    offending = [p for p in paths if not _is_allowed(p)]
    if not offending:
        return None
    names = ", ".join(offending)
    reason = f"{names}: {REFUSAL_SUFFIX}"
    return _deny(reason)


def run(payload: dict) -> dict | None:
    if payload.get("tool_name") not in ("Bash", "PowerShell"):
        return None
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command:
        return None
    cwd = payload.get("cwd")
    cwd = cwd if isinstance(cwd, str) and cwd else None
    return evaluate(command, cwd)


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        output = run(payload)
    except Exception as exc:  # fail open: never block a session on an internal error
        print(f"autopilot_commit_guard: internal error, failing open: {exc}", file=sys.stderr)
        return 0
    if output is not None:
        try:
            print(json.dumps(output))
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
