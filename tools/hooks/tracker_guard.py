"""PreToolUse guard for the tracker (issue #319, unit 1).

The rules on every Bash/PowerShell command, each segment of a compound command judged alone:

* label guard: a session that is not the brain never creates, edits, deletes or clones a GitHub
  label (`gh label ...`, `gh api` on repos/<o>/<r>/labels, a graphql label mutation).
* issue guard: `gh issue create` (any session) names exactly one label of the `areas` setting, judged against
  the tickets repo (`tickets_repo`, #323): a create aimed at another repo is not judged.
* shared temp (#612): `gh issue create|edit|comment --body-file` whose file sits directly in the user
  temp root is refused; a subfolder of it (a session's scratchpad) passes.
* parked title (#1074): `gh issue create|edit --title` whose title contains "parked" (any case) is
  refused; the label `parked` and a `Parked: <event>` body line are the way.

A refusal is one JSON line in var/logs/tracker_guard.jsonl (env TRACKER_GUARD_LOG).
The test seam: env TRACKER_GUARD_SETTINGS names the settings file `areas` is read from.
Any error of the hook itself lets the command through.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

LOG_PATH = _ROOT / "var" / "logs" / "tracker_guard.jsonl"

LABEL_REFUSAL = (
    "A label is created, edited or deleted by the brain alone. Do not touch labels from this session: "
    "put the wish for a label (name, meaning) into the ticket and the brain will decide."
)
_VALUE_OPTS = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "-H", "--header", "--input",
               "-q", "--jq", "-t", "--template", "--hostname", "--cache", "-p", "--preview"}
_BODY_OPTS = {"-f", "-F", "--field", "--raw-field", "--input"}
_LABEL_PATH = re.compile(r"^/?repos/[^/\s]+/[^/\s]+/labels(/[^/\s]*)?/?$")
_MUTATIONS = ("createLabel", "updateLabel", "deleteLabel")
_ENV_ASSIGN = re.compile(r"\w+=.*")


def _segments(command: str) -> list[str]:
    """Split on unquoted && || ; | & and newlines."""
    out: list[str] = []
    cur: list[str] = []
    quote = None
    for ch in command:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch in ";|&\n":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [s.strip() for s in out if s.strip()]


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _gh_args(tokens: list[str]) -> list[str] | None:
    """The tokens after `gh`, or None when the segment is not a gh call."""
    for i, tok in enumerate(tokens):
        if _ENV_ASSIGN.fullmatch(tok):
            continue
        name = re.split(r"[\\/]", tok)[-1].lower()
        return tokens[i + 1:] if name in ("gh", "gh.exe") else None
    return None


def _positionals(args: list[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
        elif a in ("-R", "--repo") or a in _VALUE_OPTS:
            skip = True
        elif a.startswith("-"):
            continue
        else:
            out.append(a)
    return out


def _api_method(args: list[str]) -> str:
    for i, a in enumerate(args):
        if a in ("-X", "--method") and i + 1 < len(args):
            return args[i + 1].upper()
        if a.startswith("--method="):
            return a.split("=", 1)[1].upper()
        if a.startswith("-X") and len(a) > 2:
            return a[2:].upper()
    for a in args:
        if a in _BODY_OPTS or a.split("=", 1)[0] in _BODY_OPTS:
            return "POST"
    return "GET"


def _is_label_command(segment: str) -> bool:
    args = _gh_args(_tokens(segment))
    if args is None:
        return False
    pos = _positionals(args)
    if len(pos) >= 2 and pos[0] == "label" and pos[1] in ("create", "edit", "delete", "clone"):
        return True
    if pos and pos[0] == "api":
        rest = args[args.index("api") + 1:]
        endpoint = _positionals(rest)[:1]
        if not endpoint:
            return False
        if endpoint[0] == "graphql":
            return any(m in segment for m in _MUTATIONS)
        return bool(_LABEL_PATH.match(endpoint[0])) and _api_method(rest) in ("POST", "PATCH", "DELETE")
    return False


def _issue_labels(args: list[str]) -> list[str]:
    labels: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        val = None
        if a in ("-l", "--label") and i + 1 < len(args):
            i += 1
            val = args[i]
        elif a.startswith("--label="):
            val = a.split("=", 1)[1]
        elif a.startswith("-l") and len(a) > 2 and not a.startswith("--"):
            val = a[2:]
        if val is not None:
            labels += [x.strip() for x in val.split(",") if x.strip()]
        i += 1
    return labels


def _issue_create_problem(segment: str, areas: list[str], repo: str | None = None) -> str | None:
    args = _gh_args(_tokens(segment))
    if args is None or not areas:
        return None
    pos = _positionals(args)
    if not (len(pos) >= 2 and pos[0] == "issue" and pos[1] == "create"):
        return None
    target = _flag_value(args, "--repo", "-R")
    if target and repo and target.strip().lower() != repo.strip().lower():
        return None  # another repo: this repo's area labels do not exist there
    labels = _issue_labels(args)
    named = [x for x in labels if x in areas]
    stray = [x for x in labels if x.lower().startswith("area:") and x not in areas]
    if len(named) == 1 and not stray:
        return None
    return ("`gh issue create` must carry exactly one area label, one of: " + ", ".join(areas)
            + f" (got {len(named) + len(stray)}: {', '.join(named + stray) or 'none'}).")


def _flag_value(args: list[str], long: str, short: str) -> str | None:
    val = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in (long, short) and i + 1 < len(args):
            i += 1
            val = args[i]
        elif a.startswith(long + "="):
            val = a.split("=", 1)[1]
        elif a.startswith(short) and len(a) > 2 and not a.startswith("--"):
            val = a[2:]
        i += 1
    return val


SHAPE_REFUSAL = "Call the Skill tool with `write-ticket`, then write the ticket again: "

PARKED_REFUSAL = (
    "The word \"parked\" is not put in a ticket title: a ticket that waits on a named event takes the label "
    "`parked` and a `Parked: <event>` line at the top of its body. Call the Skill tool with `write-ticket` "
    "before rewriting the title."
)


def _parked_title_problem(segment: str) -> str | None:
    """`gh issue create|edit` with a --title containing "parked" (any case) is refused (#1074)."""
    args = _gh_args(_tokens(segment))
    if args is None:
        return None
    pos = _positionals(args)
    if not (len(pos) >= 2 and pos[0] == "issue" and pos[1] in ("create", "edit")):
        return None
    title = _flag_value(args, "--title", "-t")
    if title is None or "parked" not in title.lower():
        return None
    return PARKED_REFUSAL


SHARED_TEMP_REFUSAL = (
    "Write the body into your session's own folder (the scratchpad, or a file named with your session id), "
    "not the shared temp folder: another session may overwrite it."
)
# The temp root as a command names it before the shell expands it (#612).
_TEMP_WORDS = {"$temp", "${temp}", "$tmp", "${tmp}", "$tmpdir", "${tmpdir}", "%temp%", "%tmp%",
               "$env:temp", "$env:tmp", "/tmp"}
_MSYS_DRIVE = re.compile(r"^/([a-zA-Z])(/.*)?$")


def _temp_roots() -> set[str]:
    import tempfile
    roots = {tempfile.gettempdir()}
    roots.update(os.environ[k] for k in ("TEMP", "TMP", "TMPDIR") if os.environ.get(k))
    out = set()
    for r in roots:
        try:
            out.add(os.path.normcase(str(Path(r).resolve())))
        except Exception:
            out.add(os.path.normcase(r))
    return out


def _in_shared_temp(body_file: str, cwd: str | None) -> bool:
    """True when the body file sits directly in the user temp root (a subfolder of it passes)."""
    text = body_file.replace("\\", "/")
    parent_text, _, _ = text.rpartition("/")
    if parent_text.lower().rstrip("/") in _TEMP_WORDS:
        return True
    m = _MSYS_DRIVE.match(text)
    if m:  # Git Bash's /c/Users/... form
        text = f"{m.group(1)}:{m.group(2) or '/'}"
    path = Path(text)
    if not path.is_absolute():
        path = Path(cwd or os.getcwd()) / path
    try:
        parent = path.resolve().parent
    except Exception:
        parent = path.parent
    return os.path.normcase(str(parent)) in _temp_roots()


def _shared_temp_problem(segment: str, cwd: str | None) -> str | None:
    """`gh issue create|edit|comment --body-file <temp root>/x` is refused (#612).

    Split without POSIX escapes, so a Windows path keeps its backslashes.
    """
    try:
        raw = shlex.split(segment, posix=False)
    except ValueError:
        raw = segment.split()
    raw = [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t for t in raw]
    args = _gh_args(raw)
    if args is None:
        return None
    pos = _positionals(args)
    if not (len(pos) >= 2 and pos[0] == "issue" and pos[1] in ("create", "edit", "comment")):
        return None
    body_file = _flag_value(args, "--body-file", "-F")
    if body_file is None or body_file == "-":
        return None
    return SHARED_TEMP_REFUSAL if _in_shared_temp(body_file, cwd) else None


_SHELL_VAR = re.compile(r"\$|%[^%]+%|`")


def _cd_target(segment: str) -> str | None:
    """The literal target of a `cd <dir>` segment, else None."""
    toks = _tokens(segment)
    if not toks or toks[0] != "cd":
        return None
    rest = [t for t in toks[1:] if t.lower() != "/d"]
    return rest[0] if len(rest) == 1 else None


def _shape_problem(segment: str, cwd: str | None, cd_var: bool = False) -> tuple[str | None, bool]:
    """(problem, body_from_stdin) of the ticket shape for `gh issue create` / `gh issue edit`."""
    args = _gh_args(_tokens(segment))
    if args is None:
        return None, False
    pos = _positionals(args)
    if not (len(pos) >= 2 and pos[0] == "issue" and pos[1] in ("create", "edit")):
        return None, False
    create = pos[1] == "create"
    title = _flag_value(args, "--title", "-t")
    body = _flag_value(args, "--body", "-b")
    body_file = _flag_value(args, "--body-file", "-F")
    if not create and title is None and body is None and body_file is None:
        return None, False
    stdin = False
    if body_file is not None:
        if body_file == "-":
            stdin = True
            body = None
        else:
            if _SHELL_VAR.search(body_file) or cd_var:
                return f"The --body-file {body_file} holds a shell variable: write a literal absolute path.", False
            path = Path(body_file)
            if not path.is_absolute():
                path = Path(cwd or os.getcwd()) / path
            try:
                body = path.read_text(encoding="utf-8")
            except Exception:
                return f"The --body-file {body_file} cannot be read: write the ticket body to a readable file.", False
    if create:
        if body is None and not stdin:
            body = ""
        if title is None:
            title = None  # gh asks interactively; only the body is judged
    from tools.agentops.ticket_shape import check, flow_fix_problems
    problems = check(title, None if stdin else body)
    labels = _issue_labels(args) if create else []
    if "core-issue" in labels and body is not None:  # #199: body unknown from stdin
        problems += flow_fix_problems(body, labels)
    if not problems:
        return None, stdin
    return SHAPE_REFUSAL + "; ".join(problems), stdin


def _settings() -> dict:
    from tools.agentops.core_settings import load
    path = os.environ.get("TRACKER_GUARD_SETTINGS")
    return load(Path(path) if path else None)


def _tickets_repo() -> str:
    """#323: the repo the tickets live in (setting `tickets_repo`; the repo itself when it is not set)."""
    from tools.agentops.core_settings import tickets_repo
    path = os.environ.get("TRACKER_GUARD_SETTINGS")
    return tickets_repo(Path(path) if path else None)


def _areas() -> list[str]:
    return list(_settings().get("areas") or [])


def _log(session: str, kind: str, command: str) -> None:
    try:
        path = Path(os.environ.get("TRACKER_GUARD_LOG") or LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": session, "kind": kind,
               "command": command[:200]}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:
        pass


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def run(payload: dict) -> dict | None:
    if payload.get("tool_name") not in ("Bash", "PowerShell"):
        return None
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command:
        return None
    from tools.hooks.brain_reset_nudge import _not_brain
    from tools.hooks.shell_words import strip_heredocs
    other = _not_brain(payload)
    segments = _segments(strip_heredocs(command))
    if other is not None and any(_is_label_command(s) for s in segments):
        _log(other, "label", command)
        return _deny(LABEL_REFUSAL)
    settings = _settings()
    areas = list(settings.get("areas") or [])
    repo = _tickets_repo()
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    cd_var = False
    for s in segments:
        target = _cd_target(s)
        if target is not None:
            if _SHELL_VAR.search(target):
                cd_var = True
            else:
                cd_var = False
                t = Path(target)
                cwd = str(t if t.is_absolute() else Path(cwd or os.getcwd()) / t)
            continue
        problems = [_issue_create_problem(s, areas, repo), _shared_temp_problem(s, cwd),
                    _parked_title_problem(s)]
        shape, stdin = _shape_problem(s, cwd, cd_var)
        problems.append(shape)
        if stdin:
            _log(other or "brain", "issue_stdin", command)
        problems = [p for p in problems if p]
        if problems:
            _log(other or "brain", "issue", command)
            return _deny(" ".join(problems))
    return None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            return 0
        output = run(payload)
    except Exception as exc:
        print(f"tracker_guard: internal error, failing open: {exc}", file=sys.stderr)
        return 0
    if output is not None:
        print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
