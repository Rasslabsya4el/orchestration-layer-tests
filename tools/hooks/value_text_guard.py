"""The value check on every text for another reader (#415 unit 3), judged on each call by its input.

orch-it's `python -m orch_it guards check-text --leaves-machine|--stays-on-machine` reads a text on stdin and answers one
JSON line `{"ok", "findings": [{"kind", "rule"}], "not_checked"}` (exit 0 clean, 1 a finding, 2 cannot run). This module
picks the texts of a call and runs that one check on them, by SUBPROCESS in the orch-it clone (`orch_it_clone`), never
by a copy of it.

Which call is judged is picked by its INPUT, not by a tool name, so a tool added later is covered:

* a tool's input with a `file_path` inside the repo is a write that leaves the machine (STATE.md, a grill record, an ADR,
  GLOSSARY.md): every string leaf of the input but the structural keys (an Edit's `old_string` is what is found, not
  what is written); a source file outside `docs/` gets the value kinds only (its pattern kinds are legitimate code);
* a `file_path` outside the repo, a path `git check-ignore` names (a scratch or `var/` file never leaves the machine)
  and `config/orchestration.json` (the settings hold repo and account values as settings, by the standard): the
  file's own content is not judged, its other string fields are, as text that stays on this machine;
* any other input with a string leaf (a helper's `prompt`, a session's `message`, a tool nobody listed) stays on this
  machine: the run-time locations the harness gives a session and paths inside the repo pass;
* a Bash/PowerShell call is judged only in `tracker_guard`, by the text FLAGS its `gh`, `git commit` or
  `python -m tools.agentops` segment carries, not by a verb: `--title`, `--body`/`-b`, `--comment`, `--message`/`-m`,
  `--state-entry`, `--review-entry`, the files `--body-file`/`-F`, `--comment-file`, a `gh api` write's fields, and a
  heredoc's body. That text leaves the machine, and the refusal is one of the tracker guard's messages of the same
  call. A named text file that cannot be read refuses the call (naming the kind of file, never its path). This
  hook's own `main` leaves shell tools to it.

One subprocess per call and mode, however many fields: the leaves are joined. A call with no text starts nothing. Only the
brain's and ORCH's sessions are bound (`RULE_ROLES`). A check that cannot run (the clone absent, the subcommand missing,
exit 2, past `TIMEOUT_SECONDS`, an answer that is not the JSON line) refuses the call and names the install step; a
silent pass is never a result. `main` exits 0 whatever happens (a deny is JSON); the settings entry adds `|| exit 2`,
so a process that cannot even start blocks the call too.

Test seams: env `VALUE_TEXT_GUARD_CHECK` (a JSON list: the command that stands for the check, the mode flag appended)
and env `VALUE_TEXT_GUARD_CLONE` (the clone folder).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from tools.agentops.util import no_window_flags  # noqa: E402 - after the path line above

#: the role(s) each rule binds (#320)
RULE_ROLES: dict[str, tuple[str, ...]] = {"value_in_text_for_another_reader": ("brain", "orch")}

SHELL_TOOLS = ("Bash", "PowerShell")
#: keys whose strings name a place, a kind of agent or what a search finds, never text for a reader
STRUCTURAL_KEYS = ("file_path", "path", "subagent_type", "model", "pattern", "glob", "notebook_path", "cell_id",
                   "edit_mode", "old_string")
FILE_KEYS = ("file_path", "notebook_path")
#: a source file outside `docs/` is judged for the value kinds only
SOURCE_SUFFIXES = (".py", ".sh", ".ps1", ".js", ".ts", ".yml", ".yaml", ".toml")
#: the kinds orch-it's check calls pattern kinds (code shapes); the others are value kinds
PATTERN_KINDS = ("bare_python", "one_os_call", "check_name_literal")
TIMEOUT_SECONDS = 20
DEFAULT_RULE = "no value that differs per repo, account, machine or OS; such a value is a setting with its default"
ENV_CHECK = "VALUE_TEXT_GUARD_CHECK"
ENV_CLONE = "VALUE_TEXT_GUARD_CLONE"

#: the file whose content a file write leaves out of the judgement: the settings hold repo and account values
SETTINGS_FILE = "config/orchestration.json"
#: keys that carry what a file write puts INTO the file (the rest of its strings are text for a reader)
CONTENT_KEYS = ("content", "new_string", "new_source", "edits")

#: the text flags per command: (long flags, short flags) of an inline text, and flag -> kind of a file holding one
_GH_TEXT = (("--title", "--body", "--comment"), ("-t", "-b"))
_GH_FILES = {"--body-file": "body file", "--comment-file": "comment file", "-F": "body file"}
_GIT_TEXT = (("--message",), ("-m",))
_GIT_FILES = {"--file": "message file", "-F": "message file"}
_AGENTOPS_TEXT = (("--message", "--state-entry", "--review-entry", "--comment", "--body", "--title"), ("-m",))
_AGENTOPS_FILES = {"--comment-file": "comment file", "--body-file": "body file"}
_API_WRITES = ("POST", "PATCH", "PUT")
_ENV_WORD = re.compile(r"\w+=.*")


@dataclass(frozen=True)
class Judged:
    """One text of a call and how it is judged: `leaves` the machine (else stays on it), `values_only` (no pattern kinds)."""

    text: str
    leaves: bool
    values_only: bool = False


class CheckUnavailable(Exception):
    """The check could not run; the call is refused with the install step."""


# ---- the texts of a call ----

def _leaves(node: object, key: str | None = None, skip: tuple[str, ...] = STRUCTURAL_KEYS) -> Iterator[str]:
    """Every non-empty string leaf of `node` at any depth, except under a key of `skip` (a list takes its key)."""
    if isinstance(node, str):
        if node and key not in skip:
            yield node
    elif isinstance(node, dict):
        for name, value in node.items():
            yield from _leaves(value, name, skip)
    elif isinstance(node, list):
        for item in node:
            yield from _leaves(item, key, skip)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _repo_roots() -> list[str]:
    """This checkout and its main checkout (a worktree sits inside the main one). A lookup that fails raises
    `CheckUnavailable`: a root left out would make a file of that repo look outside it, a silent pass."""
    try:
        from tools.agentops.worktree import main_checkout_root
        roots = [_ROOT, main_checkout_root(_ROOT)]
    except Exception as e:
        raise CheckUnavailable(f"the repo's main checkout cannot be found ({type(e).__name__}: {e})") from e
    return [_norm(str(r)) for r in roots]


def _absolute(file: str, cwd: str | None) -> str:
    return os.path.normpath(file if os.path.isabs(file) else os.path.join(cwd or os.getcwd(), file))


def _inside_repo(file: str, cwd: str | None) -> str | None:
    """The repo root (normalised) that holds `file` (relative to `cwd`), the longest when roots nest; None outside."""
    path = _norm(_absolute(file, cwd))
    held = [r for r in _repo_roots() if path == r or path.startswith(r.rstrip(os.sep) + os.sep)]
    return max(held, key=len) if held else None


def _is_source(file: str, cwd: str | None, root: str) -> bool:
    if os.path.splitext(file)[1].lower() not in SOURCE_SUFFIXES:
        return False
    path = _norm(_absolute(file, cwd))
    return path[len(root):].lstrip(os.sep).split(os.sep, 1)[0] != "docs"


def _git_ignores(path: str) -> bool:
    """True when `git check-ignore` names `path` (asked from the file's nearest existing folder, so the repo that
    holds the file, a worktree included, answers). A git that cannot answer is not an ignore: the file is judged."""
    folder = Path(path).parent
    while not folder.is_dir() and folder != folder.parent:
        folder = folder.parent
    try:
        done = subprocess.run(["git", "check-ignore", "-q", "--", path], cwd=str(folder), capture_output=True,
                              timeout=TIMEOUT_SECONDS, creationflags=no_window_flags())
    except Exception:
        return False
    return done.returncode == 0


def _leaves_the_machine(file: str, cwd: str | None, root: str) -> bool:
    """Whether a file write in the repo is judged: not the settings file, not a path git ignores."""
    if _norm(_absolute(file, cwd)) == _norm(os.path.join(root, *SETTINGS_FILE.split("/"))):
        return False
    return not _git_ignores(_absolute(file, cwd))


def _bound(payload: dict) -> bool:
    from tools.hooks.session_role import fires
    return fires(RULE_ROLES["value_in_text_for_another_reader"], payload)


def judged_texts(payload: dict) -> list[Judged]:
    """The texts of a non-shell call, picked by its input; `[]` when it carries none for another reader."""
    tool_input = payload.get("tool_input")
    if payload.get("tool_name") in SHELL_TOOLS or not isinstance(tool_input, dict):
        return []
    texts = list(_leaves(tool_input))
    if not texts:
        return []
    file = next((tool_input[k] for k in FILE_KEYS if isinstance(tool_input.get(k), str)), None)
    if file is None:
        return [Judged("\n".join(texts), leaves=False)]
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    root = _inside_repo(file, cwd)  # a failed root lookup refuses whoever the session is: it stays before the next line
    # a session this rule does not bind is never judged (`refusal` returns None for it), so it skips the `git
    # check-ignore` subprocess of `_leaves_the_machine` (~40 ms on every file write of every other session, #461)
    if root is not None and _bound(payload) and _leaves_the_machine(file, cwd, root):
        return [Judged("\n".join(texts), leaves=True, values_only=_is_source(file, cwd, root))]
    # only the path-based judgement is skipped: the fields that are not the file's own content stay judged
    others = list(_leaves(tool_input, skip=STRUCTURAL_KEYS + CONTENT_KEYS))
    return [Judged("\n".join(others), leaves=False)] if others else []


class UnreadableText(Exception):
    """A text file a flag names cannot be read; `kind` says which (never its path, which may hold a value)."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def _flag_values(args: list[str], longs: tuple[str, ...], shorts: tuple[str, ...]) -> list[str]:
    """Every value of the flags (`--long v`, `--long=v`, `-s v`, `-sv`, and a cluster that ends in the short flag:
    `-am v`)."""
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        cluster = re.fullmatch(r"-[A-Za-z]{2,}", a) and any(a.endswith(s[1]) for s in shorts)
        if (a in longs + shorts or cluster) and i + 1 < len(args):
            i += 1
            out.append(args[i])
        elif any(a.startswith(long + "=") for long in longs):
            out.append(a.split("=", 1)[1])
        elif any(a.startswith(s) and len(a) > 2 and not a.startswith("--") for s in shorts):
            out.append(a[2:])
        i += 1
    return out


def _file_names(args: list[str], files: dict[str, str]) -> list[tuple[str, str]]:
    """(kind, name) of every file the flags of `files` (flag -> kind) name."""
    return [(kind, name) for flag, kind in files.items()
            for name in _flag_values(args, (flag,) if flag.startswith("--") else (), () if flag.startswith("--") else (flag,))]


def _read(kind: str, name: str, cwd: str | None) -> str | None:
    """A named file's text; None for stdin (`-`) or a shell variable (the tracker guard says so). A file that cannot
    be read raises `UnreadableText`: a text never seen is never a pass."""
    if name == "-" or re.search(r"\$|%[^%]+%|`", name):
        return None
    path = Path(name)
    if not path.is_absolute():
        path = Path(cwd or os.getcwd()) / path
    try:
        return path.read_text(encoding="utf-8")
    except Exception as e:
        raise UnreadableText(kind) from e


def heredoc_bodies(command: str) -> list[str]:
    """The bodies of the heredocs in `command` (the inverse of `shell_words.strip_heredocs`)."""
    from tools.hooks.shell_words import HEREDOC_RE

    lines = command.split("\n")
    bodies: list[str] = []
    i = 0
    while i < len(lines):
        match = HEREDOC_RE.search(lines[i])
        i += 1
        if match:
            start = i
            while i < len(lines) and lines[i].strip() != match.group(2):
                i += 1
            bodies.append("\n".join(lines[start:i]))
            i += 1
    return bodies


def _command_args(tokens: list[str]) -> tuple[str, list[str]] | None:
    """(`gh`, `git` or `agentops`, the words after it) of a segment that is one of the commands whose text flags are
    judged; None for any other. `git` counts only for `commit`; `agentops` is `python -m tools.agentops`."""
    for i, tok in enumerate(tokens):
        if tok == "-m" and tokens[i + 1:i + 2] == ["tools.agentops"]:
            return "agentops", tokens[i + 2:]
    for i, tok in enumerate(tokens):
        if _ENV_WORD.fullmatch(tok):
            continue
        name = re.split(r"[\\/]", tok)[-1].lower()
        rest = tokens[i + 1:]
        if name in ("gh", "gh.exe"):
            return "gh", rest
        if name in ("git", "git.exe") and "commit" in rest:
            return "git", rest[rest.index("commit") + 1:]
        return None
    return None


def shell_flags(segment: str) -> tuple[list[str], list[tuple[str, str]]]:
    """(inline texts, (kind, name) of the text files) the text flags of one shell segment carry; ([], []) for a
    segment with none. Nothing is read or started."""
    from tools.hooks.tracker_guard import _api_method, _positionals, _tokens

    found = _command_args(_tokens(segment))
    if found is None:
        return [], []
    command, args = found
    if command == "gh" and (pos := _positionals(args)) and pos[0] == "api":
        if _api_method(args) not in _API_WRITES:
            return [], []
        inline: list[str] = []
        files: list[tuple[str, str]] = []
        for field in _flag_values(args, ("--field", "--raw-field"), ("-f", "-F")):
            value = field.split("=", 1)[1] if "=" in field else ""
            if value.startswith("@"):
                files.append(("field file", value[1:]))
            elif value:
                inline.append(value)
        return inline, files + [("input file", n) for n in _flag_values(args, ("--input",), ())]
    longs, shorts = {"gh": _GH_TEXT, "git": _GIT_TEXT, "agentops": _AGENTOPS_TEXT}[command]
    table = {"gh": _GH_FILES, "git": _GIT_FILES, "agentops": _AGENTOPS_FILES}[command]
    return _flag_values(args, longs, shorts), _file_names(args, table)


def shell_texts(segment: str, cwd: str | None, command: str, flags: tuple[list[str], list[tuple[str, str]]]) -> list[Judged]:
    """The text a segment sends out through the text flags `shell_flags` found in it (`flags`); it leaves the machine.
    `command` is the whole shell text, whose heredoc bodies join a segment that opens one or reads stdin. Raises
    `UnreadableText` for a named file that cannot be read."""
    from tools.hooks.shell_words import HEREDOC_RE

    inline, files = flags
    texts = list(inline)
    texts += [t for kind, name in files if (t := _read(kind, name, cwd)) is not None]
    if HEREDOC_RE.search(segment) or any(name == "-" for _, name in files):
        texts += heredoc_bodies(command)
    texts = [t for t in texts if t.strip()]
    return [Judged("\n".join(texts), leaves=True)] if texts else []


# ---- the check ----

def clone_folder() -> Path:
    """The orch-it clone: the test seam, else the setting `orch_it_clone` resolved at run time."""
    override = os.environ.get(ENV_CLONE)
    if override:
        return Path(override)
    from tools.agentops.core_settings import orch_it_clone
    return orch_it_clone()


def check_invocation(leaves: bool) -> tuple[list[str], str | None, dict[str, str]]:
    """(command, cwd, env) of one check call. THIS repo is its cwd, so the repo and account kinds read this layer's own
    origin, never the clone's; the clone's `src` (the clone itself when it has none) is on PYTHONPATH."""
    flags = ["--leaves-machine" if leaves else "--stays-on-machine", "--work-tree", str(_ROOT)]
    env = {**os.environ, "PYTHONUTF8": "1"}
    override = os.environ.get(ENV_CHECK)
    if override:
        command = json.loads(override)
        if not (isinstance(command, list) and command and all(isinstance(c, str) for c in command)):
            raise ValueError(f"{ENV_CHECK} is not a JSON list of words")
        return command + flags, None, env
    clone = clone_folder()
    if not clone.is_dir():
        raise FileNotFoundError(f"the orch-it clone {clone} is absent")
    src = clone / "src"
    path = [str(src if src.is_dir() else clone)]
    if env.get("PYTHONPATH"):
        path.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(path)
    return [sys.executable, "-m", "orch_it", "guards", "check-text", *flags], str(_ROOT), env


def findings_of(judged: Judged) -> list[dict]:
    """The findings of the check on one text. Raises `CheckUnavailable` when it cannot run."""
    try:
        command, cwd, env = check_invocation(judged.leaves)
        done = subprocess.run(command, input=judged.text, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=cwd, env=env, timeout=TIMEOUT_SECONDS,
                              creationflags=no_window_flags())
    except subprocess.TimeoutExpired as e:
        raise CheckUnavailable(f"no answer in {TIMEOUT_SECONDS} s") from e
    except Exception as e:  # the clone absent, a bad seam, a missing Python
        raise CheckUnavailable(f"{type(e).__name__}: {e}") from e
    if done.returncode not in (0, 1):
        raise CheckUnavailable(f"exit {done.returncode}: {(done.stderr or '').strip()[-200:]}")
    try:
        answer = json.loads([line for line in done.stdout.splitlines() if line.strip()][-1])
        found = answer["findings"]
        if not isinstance(found, list):
            raise ValueError("findings is not a list")
    except Exception as e:
        raise CheckUnavailable(f"the answer is not the check's JSON line ({type(e).__name__}: {e})") from e
    if done.returncode == 1 and not found:
        raise CheckUnavailable("exit 1 with no finding")
    return [f for f in found if isinstance(f, dict)
            and not (judged.values_only and f.get("kind") in PATTERN_KINDS)]


def unavailable_message(reason: str) -> str:
    return (f"The value check cannot run ({reason}), so the call is refused, not passed. Install step: clone orch-it into "
            "the folder the setting `orch_it_clone` names (default `../orch-it-orch`, beside the main checkout) and install "
            "it (`python -m pip install -e <that folder>`); the folder is "
            "`python -c \"from tools.agentops import core_settings; print(core_settings.orch_it_clone())\"`. Then send the "
            "call again.")


def refusal(judged: list[Judged], payload: dict) -> str | None:
    """The refusal of the texts: None when the session is not bound or every text is clean."""
    if not judged:
        return None
    from tools.hooks.session_role import fires
    if not fires(RULE_ROLES["value_in_text_for_another_reader"], payload):
        return None
    rules: list[str] = []
    kinds: list[str] = []
    where: list[str] = []
    for item in dict.fromkeys(judged):
        try:
            found = findings_of(item)
        except CheckUnavailable as e:
            return unavailable_message(str(e))
        if found:
            where.append("text that leaves this machine" if item.leaves else "text that stays on this machine")
        for f in found:
            kinds.append(str(f.get("kind")))
            rules.append(str(f.get("rule") or DEFAULT_RULE))
    if not kinds:
        return None
    return (f"{' '.join(dict.fromkeys(rules))} Value kinds found, in {' and '.join(dict.fromkeys(where))}: "
            f"{', '.join(dict.fromkeys(kinds))}. Rewrite it with the setting that holds the value, or the name that is "
            "resolved at run time, then send the call again.")


def unreadable_message(kind: str) -> str:
    return (f"The {kind} the call names cannot be read, so the call is refused, not passed: a text that is not seen is "
            "not checked. Write the text to a file that exists, named by a literal path, then send the call again.")


def shell_problem(segment: str, cwd: str | None, payload: dict) -> str | None:
    """For `tracker_guard`: the refusal of one shell segment's text flags, None when it has none or is clean. A
    segment with no text flag costs nothing; a session that is not bound reads no file."""
    flags = shell_flags(segment)
    if not flags[0] and not flags[1]:
        return None
    from tools.hooks.session_role import fires
    if not fires(RULE_ROLES["value_in_text_for_another_reader"], payload):
        return None
    command = (payload.get("tool_input") or {}).get("command")
    try:
        texts = shell_texts(segment, cwd, command if isinstance(command, str) else "", flags)
    except UnreadableText as e:
        return unreadable_message(e.kind)
    return refusal(texts, payload)


def run(payload: dict) -> dict | None:
    """The deny for a non-shell call, else None. Any failure is a deny naming the install step."""
    try:
        message = refusal(judged_texts(payload), payload)
    except Exception as e:  # noqa: BLE001 - a check that cannot run never passes
        message = unavailable_message(f"{type(e).__name__}: {e}")
    if message is None:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": message}}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        return 0  # bad stdin carries no call to judge
    if not isinstance(payload, dict):
        return 0
    output = run(payload)
    if output is not None:
        print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
