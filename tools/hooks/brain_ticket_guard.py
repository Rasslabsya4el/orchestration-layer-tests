"""PreToolUse refusal: past the context line the brain opens, rewrites and closes no ticket (map #355).

Once the brain chat's context is at or over ``context_line`` (``config/orchestration.json``, read through
``brain_reset_nudge._sizes``), a Bash or PowerShell command that writes a GitHub issue is denied. The brain may still
comment, read, label and do everything else; a fresh chat takes the ticket.

Refused, wherever the gh call sits in the command text:

* ``gh issue create`` / ``close`` / ``reopen`` / ``delete`` / ``transfer``;
* ``gh issue edit`` with ``--title``/``-t``/``--body``/``-b``/``--body-file``/``-F``;
* ``gh api`` on ``repos/<o>/<r>/issues`` with POST (explicit, or implicit through a field/``--input``);
* ``gh api`` on ``repos/<o>/<r>/issues/<n>`` that sets ``title``, ``body`` or ``state``, or sends ``--input`` (a query
  string on the path changes nothing);
* ``gh api graphql`` carrying a create/update/close/reopen/delete/transferIssue mutation.

Not refused: ``gh issue comment|view|list``, ``gh issue edit`` with labels alone, ``gh api .../comments``, any read,
and gh words that are only text (a quoted ``--body``, a heredoc, an ``echo`` argument).

"Wherever it sits": ``_commands`` lexes the text into commands (split at ``; | & newline``, parentheses and braces
that open a group, backticks, and ``$( )`` inside double quotes); a command's prefixes (``do``, ``then``, ``env``,
``sudo``, ``timeout 9``, ``xargs``, ``VAR=x``, ...) are stripped to find its head; a head that is a shell
(``bash -c``, ``pwsh -Command``, ``cmd /c``) or ``eval`` has its body lexed again.

Only the brain: ``brain_reset_nudge._not_brain`` skips background sessions, other ``--agent`` launches and subagents,
and the rule names its role (``RULE_ROLES``, #320). Every refusal is one line with action ``refuse`` in
``var/agentops/brain_nudge.log`` (``AGENTOPS_HOOK_LOG``). Any error of the hook itself lets the command through.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from tools.hooks.brain_reset_nudge import (  # noqa: E402
    _brain_titles, _last_assistant_usage_total, _log, _not_brain, _sizes,
)
from tools.hooks.session_role import fires  # noqa: E402
from tools.hooks.shell_words import strip_heredocs  # noqa: E402
from tools.hooks.tracker_guard import _api_method, _gh_args, _positionals  # noqa: E402

RULE_ROLES: dict[str, tuple[str, ...]] = {
    "ticket-write-refusal": ("brain",),
}

_ENDPOINT = r"(?:https://api\.github\.com)?/?repos/[^/\s]+/[^/\s]+/issues"
_ISSUES_PATH = re.compile(rf"^{_ENDPOINT}/?$")
_ISSUE_PATH = re.compile(rf"^{_ENDPOINT}/\d+/?$")
_FIELD_OPTS = ("-f", "-F", "--field", "--raw-field")
_ISSUE_FIELDS = ("title", "body", "state")
_ISSUE_MUTATIONS = ("createIssue", "updateIssue", "closeIssue", "reopenIssue", "deleteIssue", "transferIssue")
_EDIT_FLAGS = ("--title", "--body", "--body-file")
_EDIT_SHORT = ("-t", "-b", "-F")

_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "pwsh", "powershell", "cmd"})
_SHELL_FLAGS = frozenset({"-c", "-lc", "-ic", "-cl", "-command", "/c", "/k"})
_EVALS = frozenset({"eval", "iex", "invoke-expression"})
#: words that only introduce the command after them
_PREFIX_WORDS = frozenset({"do", "then", "else", "elif", "if", "while", "until", "!", "time", "%", "foreach-object",
                           "foreach", "command", "exec", "nohup", "builtin"})
#: prefix commands with options; the value is the set of options that take the next word as their value
_OPTION_PREFIXES = {
    "env": frozenset({"-u", "-C", "-S"}),
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U"}),
    "xargs": frozenset({"-n", "-I", "-P", "-L", "-s", "-d", "-E", "-a"}),
    "timeout": frozenset({"-s", "-k"}),
}
_PAREN_KEYWORDS = frozenset({"foreach", "if", "while", "for", "switch", "elseif", "until"})
_ASSIGN = re.compile(r"\w+=.*")
_DURATION = re.compile(r"\d+(?:\.\d+)?[smhd]?")
_MAX_DEPTH = 4


def _close_paren(text: str, start: int) -> int:
    """Index of the ``)`` that closes the ``(`` just before ``start``; the end of the text when it never closes."""
    depth = 1
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return len(text)


def _commands(text: str) -> list[list[str]]:
    """The commands of a shell/PowerShell text, each as its words (quotes removed, a quoted word one word).

    A command ends at an unquoted ``; | & newline``, a backtick, a group's ``( )`` / ``{ }`` (a ``(`` or ``{`` that
    starts a command, follows ``$``/``%`` or ``foreach``/``if``...; any other is a plain character, so ``--title (x)``
    stays one command), and a ``$( )`` or backtick pair inside double quotes is lexed as commands of its own. Single
    quotes are literal; a ``#`` at the start of a word comments out the line. Backslashes stay as written (Windows
    paths), except ``\\"`` in double quotes.
    """
    out: list[list[str]] = []
    cur: list[str] = []
    word: list[str] = []
    have = False
    parens = braces = 0

    def flush() -> None:
        nonlocal have
        if have:
            cur.append("".join(word))
            word.clear()
            have = False

    def end() -> None:
        flush()
        if cur:
            out.append(cur[:])
            cur.clear()

    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "'":
            j = text.find("'", i + 1)
            j = n if j < 0 else j
            word.append(text[i + 1:j])
            have = True
            i = j + 1
        elif c == '"':
            i += 1
            while i < n and text[i] != '"':
                ch = text[i]
                if ch == "\\" and text[i + 1:i + 2] == '"':
                    word.append('"')
                    i += 2
                elif ch == "$" and text[i + 1:i + 2] == "(":
                    k = _close_paren(text, i + 2)
                    out.extend(_commands(text[i + 2:k]))
                    word.append(text[i:k + 1])
                    i = k + 1
                elif ch == "`":
                    k = text.find("`", i + 1)
                    k = n - 1 if k < 0 else k
                    out.extend(_commands(text[i + 1:k]))
                    word.append(text[i:k + 1])
                    i = k + 1
                else:
                    word.append(ch)
                    i += 1
            have = True
            i += 1
        elif c in " \t\r":
            flush()
            i += 1
        elif c == "#" and not have:
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c in ";|&\n`":
            end()
            i += 1
        elif c == "(":
            joined = "".join(word)
            if (not have and not cur) or joined.endswith("$") or (not have and cur and cur[-1].lower() in _PAREN_KEYWORDS):
                end()
                parens += 1
            else:
                word.append(c)
                have = True
            i += 1
        elif c == ")":
            if parens > 0:
                end()
                parens -= 1
            else:
                word.append(c)
                have = True
            i += 1
        elif c == "{":
            joined = "".join(word)
            if joined.endswith("$"):  # ${name}: part of the word
                j = text.find("}", i)
                j = n - 1 if j < 0 else j
                word.append(text[i:j + 1])
                have = True
                i = j + 1
                continue
            if not have or joined == "%":
                end()
                braces += 1
            else:
                word.append(c)
                have = True
            i += 1
        elif c == "}":
            if braces > 0:
                end()
                braces -= 1
            else:
                word.append(c)
                have = True
            i += 1
        else:
            word.append(c)
            have = True
            i += 1
    end()
    return out


def _base(word: str) -> str:
    """A command word's file name, lower case, without ``.exe``."""
    name = re.split(r"[\\/]", word)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _head(tokens: list[str]) -> list[str]:
    """``tokens`` from the word that is the command itself: ``VAR=x``, ``do``/``then``, ``env``, ``sudo``,
    ``timeout 9``, ``xargs -n 1``... in front of it are dropped."""
    i = 0
    while i < len(tokens):
        word = tokens[i]
        base = _base(word)
        if _ASSIGN.fullmatch(word) or base in _PREFIX_WORDS:
            i += 1
        elif base in _OPTION_PREFIXES:
            values = _OPTION_PREFIXES[base]
            i += 1
            while i < len(tokens) and tokens[i].startswith("-") and len(tokens[i]) > 1:
                i += 2 if tokens[i] in values else 1
            if base == "timeout" and i < len(tokens) and _DURATION.fullmatch(tokens[i]):
                i += 1
        else:
            break
    return tokens[i:]


def _edit_rewrites(args: list[str]) -> bool:
    """True when ``gh issue edit``'s arguments carry a title or body flag, in any spelling."""
    for a in args:
        if a in _EDIT_FLAGS or a.split("=", 1)[0] in _EDIT_FLAGS:
            return True
        if a.startswith(_EDIT_SHORT) and not a.startswith("--"):
            return True
    return False


def _set_fields(args: list[str]) -> set[str]:
    """Names of the ``gh api`` fields given by ``-f/-F/--field/--raw-field`` (``-fstate=x`` and ``--field=state=x`` too)."""
    names: set[str] = set()
    for i, a in enumerate(args):
        value = None
        if a in _FIELD_OPTS:
            value = args[i + 1] if i + 1 < len(args) else ""
        elif a.startswith(("--field=", "--raw-field=")):
            value = a.split("=", 1)[1]
        elif a.startswith(("-f", "-F")) and not a.startswith("--") and len(a) > 2:
            value = a[2:]
        if value is not None:
            names.add(value.split("=", 1)[0])
    return names


def _gh_writes(args: list[str]) -> bool:
    """True when the arguments after ``gh`` write an issue."""
    pos = _positionals(args)
    if len(pos) >= 2 and pos[0] == "issue":
        if pos[1] in ("create", "close", "reopen", "delete", "transfer"):
            return True
        return pos[1] == "edit" and _edit_rewrites(args)
    if pos and pos[0] == "api":
        rest = args[args.index("api") + 1:]
        endpoint = _positionals(rest)[:1]
        if not endpoint:
            return False
        path = endpoint[0].split("?", 1)[0].split("#", 1)[0]
        if path == "graphql":
            return any(m in " ".join(rest) for m in _ISSUE_MUTATIONS)
        method = _api_method(rest)
        if _ISSUES_PATH.match(path):
            return method == "POST"
        if _ISSUE_PATH.match(path) and method in ("PATCH", "POST"):
            has_input = any(a == "--input" or a.startswith("--input=") for a in rest)
            return has_input or bool(_set_fields(rest) & set(_ISSUE_FIELDS))
    return False


def _scan(text: str, depth: int) -> bool:
    for tokens in _commands(text):
        head = _head(tokens)
        if not head:
            continue
        base = _base(head[0])
        body = None
        if base in _SHELLS:
            for i, word in enumerate(head[1:], start=1):
                if word.lower() in _SHELL_FLAGS:
                    body = " ".join(head[i + 1:])
                    break
        elif base in _EVALS:
            body = " ".join(head[1:])
        if body is not None:
            if depth < _MAX_DEPTH and _scan(body, depth + 1):
                return True
            continue
        args = _gh_args(head)
        if args is not None and _gh_writes(args):
            return True
    return False


def command_writes_issue(command: str) -> bool:
    """True when ``command`` holds, at any command position, a gh call that writes a GitHub issue."""
    return _scan(strip_heredocs(command), 0)


def _reason(n: int, line: int) -> str:
    return (
        f"Context is {round(n / 1000)}k, over the context line ({round(line / 1000)}k). Past the line the brain "
        "answers the owner and records his words as a comment, but opens, rewrites and closes no ticket; that waits "
        'for a fresh chat (docs/agents/brain-reference.md, The clear and the hand-off).'
    )


def run(payload: dict) -> dict | None:
    if payload.get("tool_name") not in ("Bash", "PowerShell"):
        return None
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command or not command_writes_issue(command):
        return None
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return None
    if _not_brain(payload) is not None or not fires(RULE_ROLES["ticket-write-refusal"], payload,
                                                     titles=tuple(_brain_titles())):
        return None
    n = _last_assistant_usage_total(transcript)
    line, _ceiling = _sizes()
    if n is None or n < line:
        return None
    _log(payload, "PreToolUse", "refuse", context_k=round(n / 1000), command=command[:200])
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": _reason(n, line)}}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            return 0
        output = run(payload)
    except Exception as exc:
        print(f"brain_ticket_guard: internal error, failing open: {exc}", file=sys.stderr)
        return 0
    if output is not None:
        print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
