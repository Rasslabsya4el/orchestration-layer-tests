"""Worker process-start guard hook (beside the git-stash guard, same shape).

A `PreToolUse` hook (matcher `Bash|PowerShell`) in `.claude/agents/worker.md` /
`worker-strong.md`. It refuses (`permissionDecision: deny`) a command that itself starts a process
through WMI or the task scheduler, or starts the real thing: `Invoke-CimMethod` / `Invoke-WmiMethod`
/ `[wmiclass]` on `Win32_Process` with `Create`; `wmic process call create`; `schtasks` with `/run`
or `/create`; `Register-ScheduledTask`; `Start-ScheduledTask`; the repo's hidden-launch script
(`scripts/launch_hidden.ps1`); `keepalive install`; the dispatcher's launch: an agentops entry token
(`-m tools.agentops`, `-m agentops`, or a path ending `agentops/__main__.py`) followed by `dispatch`
as its first positional, with no `--help` after it.

Why: a console program started through WMI or the task scheduler gets its window on the owner's
desktop whatever desktop is asked for, even a hidden one -- measured three times independently. A
rule in a prompt did not stop a worker from doing it once already; this hook is what makes the
refusal load-bearing instead of a sentence a later instruction can out-argue.

The words of any of these forms inside a quoted argument (a commit message, an `echo`, a `grep`
pattern) or inside a heredoc's body pass -- both are naturally opaque to the tokenizer below (a
quoted phrase is one token, its content never searched) or stripped before it runs (a heredoc
body). No per-segment split on `&&`/`;`/`|` is needed here (unlike the git-stash guard): a command
either carries the words of one of these forms somewhere in its unquoted tokens or it does not, and
a chain only ever adds more unquoted tokens to look at, never removes the ones that matter.

Never raises: any error, bad stdin, unparsable input, or an internal failure exits 0 with no
output -- this hook only ever narrows what a worker's shell can do, it never gets to break the
shell call itself. Every refusal appends one line to `var/agentops/worker_git_guard.log`
(`AGENTOPS_HOOK_LOG` overrides, same file as the git-stash guard) -- a passed command writes
nothing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# the hooks run as bare scripts: their own directory is the import root of their shared helpers
sys.path.insert(0, str(Path(__file__).resolve().parent))
from shell_words import strip_heredocs  # noqa: E402
from worker_git_guard import _log, _tokenize  # noqa: E402

REFUSAL_REASON = (
    "starting a process through WMI or the task scheduler is refused here: a console program "
    "started this way gets its window on the owner's desktop whatever desktop is asked for, even a "
    "hidden one. Your step: do not start it. Write the exact command into your report; the detached "
    "launch is ORCH's. Report a number that would need the owner's desktop as \"not measured\"."
)

#: groups of unquoted-token keywords (upper-cased); a command is refused when every keyword of any
#: one group appears somewhere among its unquoted tokens (order and adjacency do not matter).
_REQUIRED_KEYWORD_GROUPS: tuple[tuple[str, ...], ...] = (
    ("INVOKE-CIMMETHOD", "WIN32_PROCESS", "CREATE"),
    ("INVOKE-WMIMETHOD", "WIN32_PROCESS", "CREATE"),
    ("[WMICLASS]", "WIN32_PROCESS"),
    ("WMIC", "PROCESS", "CALL", "CREATE"),
    ("REGISTER-SCHEDULEDTASK",),
    ("START-SCHEDULEDTASK",),
    ("LAUNCH_HIDDEN.PS1",),
    ("AGENTOPS", "KEEPALIVE", "INSTALL"),
)


def _unquoted_upper_tokens(command: str) -> list[str]:
    """`command`'s unquoted tokens (heredoc bodies stripped, quoted tokens dropped), upper-cased."""

    stripped = strip_heredocs(command)
    tokens = _tokenize(stripped)
    return [tok.upper() for tok in tokens if not (tok and tok[0] in ("'", '"'))]


#: the agentops entry point's file suffix, upper-cased, with either path separator.
_AGENTOPS_MAIN_SUFFIXES = ("AGENTOPS/__MAIN__.PY", "AGENTOPS\\__MAIN__.PY")


def _is_agentops_entry(tokens: list[str], index: int) -> bool:
    """`tokens[index]` names the agentops entry point: `-m tools.agentops`, `-m agentops`, or a path
    ending `agentops/__main__.py`."""

    if tokens[index].endswith(_AGENTOPS_MAIN_SUFFIXES):
        return True
    return index > 0 and tokens[index - 1] == "-M" and tokens[index] in ("TOOLS.AGENTOPS", "AGENTOPS")


def _launches_dispatch(tokens: list[str]) -> bool:
    """True when the unquoted tokens start the dispatcher: after an agentops entry token, the first
    token not starting with `-` is `dispatch`, and no `--help` follows the entry token anywhere."""

    for index in range(len(tokens)):
        if not _is_agentops_entry(tokens, index):
            continue
        following = tokens[index + 1:]
        if "--HELP" in following:
            continue
        word = next((tok for tok in following if not tok.startswith("-")), None)
        if word == "DISPATCH":
            return True
    return False


def is_process_start(command: str) -> bool:
    tokens = _unquoted_upper_tokens(command)
    text = " ".join(tokens)

    for group in _REQUIRED_KEYWORD_GROUPS:
        if all(keyword in text for keyword in group):
            return True

    if "SCHTASKS" in tokens and ("/RUN" in tokens or "/CREATE" in tokens):
        return True

    if _launches_dispatch(tokens):
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


def run(payload: dict) -> dict | None:
    if payload.get("tool_name") not in ("Bash", "PowerShell"):
        return None
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command:
        return None
    if not is_process_start(command):
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
        print(f"worker_process_guard: internal error, failing open: {exc}", file=sys.stderr)
        return 0
    if output is not None:
        try:
            print(json.dumps(output))
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
