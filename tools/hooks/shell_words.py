"""Shell/PowerShell command text shared by the autopilot commit guard (#142) and the worker
git-stash guard (#215): finding a heredoc's body and splitting a command line into words the way
`shlex` does, so a hook that reads a command for words like `git commit` or `git stash` never
mistakes a heredoc body, or a quoted argument to another command, for a command the shell itself
would run (#222).

Neither guard reads this module as a definition of "which command runs" beyond finding the
words: separator/segment handling specific to one guard's own rule (which tokens end a `git
commit`'s own arguments, which end a `git stash` segment) stays in that guard's own file.
"""

from __future__ import annotations

import re
import shlex

#: a heredoc opener: `<<`/`<<-`, an optional quote, a delimiter word.
HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")


def strip_heredocs(command: str) -> str:
    """`command` with every heredoc body (between the opener line and the delimiter line)
    removed, so words inside a heredoc's body are never read as a shell command."""

    lines = command.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        match = HEREDOC_RE.search(line)
        if match:
            delim = match.group(2)
            i += 1
            while i < len(lines) and lines[i].strip() != delim:
                i += 1
            if i < len(lines):
                i += 1  # skip the delimiter line itself too
            continue
        i += 1
    return "\n".join(out)


def split_command(command: str) -> list[str]:
    """`command`'s words à la `shlex.split(command, posix=False)` -- a quoted argument stays one
    opaque token (never split into bare words), which is what keeps a phrase like `"git commit"`
    inside a quoted argument to another command from ever being read as `git` and `commit`.
    Falls back to a bare whitespace split on anything `shlex` cannot parse (e.g. an unbalanced
    quote): never raises."""

    try:
        return shlex.split(command, posix=False)
    except ValueError:
        return command.split()
