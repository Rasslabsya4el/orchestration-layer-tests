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

The pytest rule (#352 unit 4, same wiring, the worker's RULE_ROLES entry is unchanged): a command that starts
`pytest` / `python -m pytest` (or `uv run pytest`) with a selection that `tools.agentops.test_run.route_selection`
sends to GitHub is refused, the reason naming `python -m tools.agentops test run <the same args>`. The selection's ids
are the stored per-test durations the words stand for (no collect); a bare pytest, options only, a word that matches
no stored id, or a selection the PC keeps is allowed. Shell redirections are dropped before the decision. The decision
is bounded by `DECIDE_BOUND_S` (5 s; the route's own reads by `GUARD_COLLECT_S`): a timeout, an error, or a PC route
allows the call.

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
import re
import shlex
import sys
import threading
from pathlib import Path

# the hooks run as bare scripts: their own directory is the import root of their shared helpers
sys.path.insert(0, str(Path(__file__).resolve().parent))
# the repo root: ``tools.agentops`` (the pytest rule's decision) is imported from it, only when a pytest call is read
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from shell_words import strip_heredocs  # noqa: E402
from worker_git_guard import _log, _segments, _tokenize  # noqa: E402

#: the pytest rule (#352 unit 4): the decision of one selection is bounded by this many seconds; past it, or on any
#: error, the call is allowed (a guard never blocks for its own failure). The collect inside gets the smaller bound.
DECIDE_BOUND_S = 5.0
GUARD_COLLECT_S = 4.0

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


# -- the pytest rule (#352 unit 4): a direct pytest of a selection the rule sends to GitHub is refused with the form ----

_PYTEST_WORDS = ("pytest", "py.test")
_PYTHON_NAMES = ("python", "python3", "py")


def _unquote(token: str) -> str:
    return token[1:-1] if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"') else token


def _is_python_word(token: str) -> bool:
    name = re.split(r"[\\/]", token.lower())[-1]
    return name.removesuffix(".exe") in _PYTHON_NAMES


def _is_pytest_head(segment: list[str], index: int) -> bool:
    """``segment[index]`` starts pytest: the first word of the segment, ``python -m pytest``, or ``uv run pytest``."""
    if segment[index].lower() not in _PYTEST_WORDS:
        return False
    if index == 0:
        return True
    previous = segment[index - 1].lower()
    if previous == "-m" and index >= 2 and _is_python_word(segment[index - 2]):
        return True
    return previous == "run" and index >= 2 and segment[index - 2].lower() == "uv"


#: a shell redirection word: an optional fd digit or ``&``, then ``>``, ``>>`` or ``<`` (``2>&1``, ``>out``, ``&>f``)
_REDIRECT = re.compile(r"^(\d*|&)(>>?|<)")
#: pytest options whose next word is their value, not a selection word (``-k "a and b"``)
_VALUE_OPTIONS = frozenset({"-k", "-m", "-p", "-c", "-o", "-n", "-W", "--deselect", "--rootdir", "--ignore",
                            "--ignore-glob", "--maxfail", "--basetemp", "--junitxml", "--durations"})


def _without_redirections(words: list[str]) -> list[str]:
    """``words`` with each shell redirection dropped; an operator alone (``>``, ``2>``) takes its target word too."""
    kept: list[str] = []
    skip_target = False
    for word in words:
        if skip_target:
            skip_target = False
            continue
        match = _REDIRECT.match(word) if word[:1] not in ("'", '"') else None
        if match:
            skip_target = match.end() == len(word)
            continue
        kept.append(word)
    return kept


def pytest_selection(command: str) -> list[str] | None:
    """The pytest words (after the pytest word, quotes off, shell redirections dropped) of the first segment that
    starts pytest; None when no segment does. A pipe's tail is another segment, so it is never read. A mention
    (``grep pytest``, ``echo pytest``) is not a start."""
    tokens = _tokenize(strip_heredocs(command))
    for segment in _segments(tokens):
        for index in range(len(segment)):
            if _is_pytest_head(segment, index):
                return [_unquote(token) for token in _without_redirections(segment[index + 1:])]
    return None


def _matches(word: str, key: str) -> bool:
    """``key`` (a stored node id) is what the selection word ``word`` stands for: the node id itself, a parametrized
    variant of it, or any test under a file or folder word."""
    prefix = word.replace("\\", "/").rstrip("/")
    return key == prefix or key.startswith((prefix + "::", prefix + "/", prefix + "["))


def _selection_ids(args: list[str], cwd: str) -> list[str] | None:
    """The stored node ids the selection words stand for (the stored per-test durations of this repo's datafile);
    None when there is no selection word (bare pytest, options only) or a word matches no stored id (unknown)."""
    from tools.agentops import suite_plan_plugin  # imports pytest: only a pytest call pays for it

    stored = suite_plan_plugin.stored_durations(suite_plan_plugin.durations_source())
    if not stored:
        stored = suite_plan_plugin.stored_durations(str(Path(cwd) / ".testmondata"))
    words: list[str] = []
    skip_value = False
    for word in args:
        if skip_value:
            skip_value = False
            continue
        if word in _VALUE_OPTIONS:
            skip_value = True
            continue
        if not word.startswith("-"):
            words.append(word)
    if not words:
        return None
    ids: list[str] = []
    for word in words:
        found = [key for key in stored if _matches(word, key)]
        if not found:
            return None
        ids.extend(key for key in found if key not in ids)
    return ids


#: the seams of the real decision (``test_run.Seams``): None in production; a test injects its settings, clock and Deps
GUARD_SEAMS = None


def _real_decide(args: list[str], cwd: str):
    """The route of the selection (``test_run.route_selection``): the same decision ``test run`` makes, its ids read
    from the stored durations (no collect). None (allowed) when the ids are not known."""
    from tools.agentops import test_run  # imported here: only a pytest call pays for it

    ids = _selection_ids(args, cwd)
    if ids is None:
        return None
    return test_run.route_selection(list(args), Path(cwd), GUARD_SEAMS, timeout_s=GUARD_COLLECT_S, ids=ids)


#: the decision the rule makes; a test replaces it (the one seam of the rule)
_decide = _real_decide


def _decide_bounded(args: list[str], cwd: str):
    """The route of ``args`` within ``DECIDE_BOUND_S``; None (allowed) on a timeout or any error."""
    box: dict = {}

    def target() -> None:
        try:
            box["route"] = _decide(args, cwd)
        except Exception as exc:  # noqa: BLE001 -- a guard never blocks for its own failure
            box["error"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(DECIDE_BOUND_S)
    if worker.is_alive() or "error" in box:
        return None
    return box.get("route")


def pytest_refusal_reason(args: list[str], route) -> str:
    form = shlex.join(args)
    spelled = f"pytest {form}" if form else "pytest"
    return (f"process guard: a long pytest selection runs through the decision point; "
            f"use python -m tools.agentops test run {form}".rstrip()
            + f" ({spelled} goes to GitHub: {route.reason})")


def _payload_cwd(payload: dict) -> str:
    cwd = payload.get("cwd")
    return cwd if isinstance(cwd, str) and Path(cwd).is_dir() else str(Path.cwd())


def _pytest_refusal(payload: dict, command: str) -> dict | None:
    args = pytest_selection(command)
    if args is None:
        return None
    route = _decide_bounded(args, _payload_cwd(payload))
    if route is None or getattr(route, "where", "") != "github":
        return None
    _log(payload, command)
    return _deny(pytest_refusal_reason(args, route))


# the role(s) each rule binds (#320): bound by wiring in the worker frontmatter (.claude/agents/worker.md and
# worker-strong.md), so no fires() gate here: a payload without agent_type must not turn the guard off
RULE_ROLES: dict[str, tuple[str, ...]] = {"process_start_refusal": ("worker",)}


def run(payload: dict) -> dict | None:
    if payload.get("tool_name") not in ("Bash", "PowerShell"):
        return None
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command:
        return None
    if not is_process_start(command):
        return _pytest_refusal(payload, command)
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
