"""#367: the live-state guard. A test not marked `live` or `slow` fails when it reads this PC's live state.

Seam: `sys.addaudithook` on `open` (a read of a path under the real repo's `var/` or the real `~/.claude`) and on
`subprocess.Popen` (a `git` or `gh` run with cwd at the real repo root). An audit hook cannot be removed, so it is
installed once per session and does nothing unless the autouse fixture in tests/conftest.py has switched it on for the
current test (`start`); each violation is recorded for that test, and the fixture's teardown asserts the list empty
(`stop`). The hook never raises: the code under test could swallow the exception.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path


def _both(path: Path) -> frozenset[Path]:
    """The path and its resolved target (a junction's target is the real folder)."""
    return frozenset({path, path.resolve()})


ROOT = Path(__file__).resolve().parents[1]
VAR_ROOTS = _both(ROOT / "var")
CLAUDE_ROOTS = _both(Path.home() / ".claude")  # computed once, at import: a test that moves HOME does not move this
REPO_ROOT = ROOT.resolve()
GIT_NAMES = frozenset({"git", "git.exe"})  # `-C` applies to git only; gh has no -C, its directory is its cwd
GUARDED_NAMES = GIT_NAMES | {"gh", "gh.exe"}
GIT_READS_NO_LIVE_STATE = frozenset({"init", "clone", "cat-file", "ls-files", "check-ignore", "merge-base"})  # #367 cause B
# global options that take their value as the next word when written without `=` (`--git-dir X`)
GIT_VALUE_OPTIONS = frozenset({"--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix", "--config-env"})
# `git clone` options that take their value as the next word (`-b main`); the first other word is the source
CLONE_VALUE_OPTIONS = frozenset({
    "-b", "--branch", "-o", "--origin", "-u", "--upload-pack", "--template", "--reference", "--reference-if-able",
    "--depth", "--shallow-since", "--shallow-exclude", "--separate-git-dir", "-c", "--config", "-j", "--jobs",
    "--filter", "--server-option", "--bundle-uri", "--ref-format",
})
COMMIT_FILE = re.compile(r"[0-9a-f]{7,40}:\S+")  # a git object name of a file in a commit named by its hash
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
ADVICE = "mark the test `live` or give it its own state"


class _State:
    active = False
    busy = False
    violations: list[str] = []


STATE = _State()
_installed = False


def install() -> None:
    """Add the audit hook once per process."""
    global _installed
    if not _installed:
        sys.addaudithook(_on_audit)
        _installed = True


def start() -> None:
    """Switch the guard on for the current test."""
    STATE.violations = []
    STATE.active = True


def stop() -> list[str]:
    """Switch the guard off; return the violations recorded since `start`, one line per distinct read."""
    STATE.active = False
    found, STATE.violations = list(dict.fromkeys(STATE.violations)), []
    return found


def _on_audit(event: str, args: tuple) -> None:
    if not STATE.active or STATE.busy:
        return
    STATE.busy = True
    try:
        if event == "open":
            _check_open(args)
        elif event == "subprocess.Popen":
            _check_git(args)
    finally:
        STATE.busy = False


def _unpack(args: tuple, count: int) -> tuple:
    """The first `count` items of an audit event's args; None where the event gave fewer."""
    return (tuple(args) + (None,) * count)[:count]


def _text(value) -> str | None:
    """A path or an argument as text (str, bytes or PathLike); None when it is none of those."""
    try:
        value = os.fspath(value)
    except TypeError:
        return None
    return os.fsdecode(value)


def _resolved(path: str | Path) -> Path | None:
    try:
        return Path(path).resolve()
    except (OSError, ValueError):
        return None


def _writes(mode, flags) -> bool:
    if isinstance(mode, str):
        return any(c in mode for c in "wax+")
    return isinstance(flags, int) and bool(flags & WRITE_FLAGS)


def _check_open(args: tuple) -> None:
    path, mode, flags = _unpack(args, 3)
    if isinstance(path, int) or _writes(mode, flags):
        return
    shown = _text(path)
    real = _resolved(shown) if shown is not None else None
    if real is not None and any(real.is_relative_to(root) for root in VAR_ROOTS | CLAUDE_ROOTS):
        STATE.violations.append(
            f"reads {shown}, this PC's live state (the real repo's var/ or the real ~/.claude): {ADVICE}")


def _argv_words(argv) -> list[str]:
    if argv is None:
        return []
    if isinstance(argv, (str, bytes, os.PathLike)):
        return (_text(argv) or "").split()
    return [_text(item) or "" for item in argv]


def _git_position(words: list[str], start: Path) -> tuple[Path, int]:
    """The directory git works in and the index of its subcommand in `words` (`len(words)` when there is none): the
    process cwd, moved by each `-C <path>` of the global options in turn (a relative path joins onto the one before
    it, an absolute one replaces it). `-c <name=value>` and the value options of GIT_VALUE_OPTIONS skip their value
    word; any other option is one word; the subcommand ends the options."""
    where, i = start, 1
    while i < len(words):
        word = words[i]
        if word == "-C" and i + 1 < len(words):
            where, i = where / words[i + 1], i + 2
        elif word in ("-c", *GIT_VALUE_OPTIONS) and i + 1 < len(words):
            i += 2
        elif word.startswith("-"):
            i += 1
        else:
            break
    return where, i


def _reads_no_live_state(words: list[str], at: int) -> bool:
    """True for a git subcommand in GIT_READS_NO_LIVE_STATE, or `rev-parse --is-shallow-repository` with nothing
    after it. The set's subcommands read no live state or only the checkout's committed content (deterministic for
    the clean checkout the merge suite runs in). `ls-files` and `check-ignore` read the index and .gitignore: they
    are exempt on purpose, because the tracked-content checks (line endings, tracked caches) need them in the merge
    gate. `clone` is in the set, but `_git_target` judges a clone by its source before this is asked."""
    subcommand = words[at] if at < len(words) else ""
    if subcommand in GIT_READS_NO_LIVE_STATE:
        return True
    if subcommand == "show":  # `show <sha>:<path>`: the content of one commit, the same on every run
        return any(COMMIT_FILE.fullmatch(word) for word in words[at + 1:])
    if subcommand == "diff":  # `diff <a>..<b>` compares two commits, not the working tree or the index
        return any(".." in word and not word.startswith("-") for word in words[at + 1:])
    return subcommand == "rev-parse" and words[at + 1:] == ["--is-shallow-repository"]


def _clone_source(words: list[str], at: int) -> str | None:
    """The source word of `git clone` (the first word that is not an option or an option's value, or the word after
    `--`); None when there is none."""
    i = at + 1
    while i < len(words):
        word = words[i]
        if word == "--":
            return words[i + 1] if i + 1 < len(words) else None
        if word in CLONE_VALUE_OPTIONS and i + 1 < len(words):
            i += 2
        elif word.startswith("-"):
            i += 1
        else:
            return word
    return None


def _git_target(words: list[str], where: Path) -> Path | None:
    """The directory whose live state a git run reads, resolved; None when the run reads none. A clone reads its
    source only, so it is judged by that (relative to the cwd moved by `-C`); any other run is judged by its
    subcommand (`_reads_no_live_state`) and then by its directory."""
    moved, at = _git_position(words, where)
    if at < len(words) and words[at] == "clone":
        source = _clone_source(words, at)
        return None if source is None else _resolved(moved / source)
    if _reads_no_live_state(words, at):
        return None
    return _resolved(moved)


def _check_git(args: tuple) -> None:
    executable, argv, cwd = _unpack(args, 3)
    words = _argv_words(argv)
    name = _text(executable) if executable is not None else (words[0] if words else "")
    name = os.path.basename((name or "").strip('"').replace("\\", "/")).lower()
    if name not in GUARDED_NAMES:
        return
    start = os.getcwd() if cwd is None else _text(cwd)
    where = _resolved(start) if start is not None else None
    if where is None:
        return
    if name in GIT_NAMES:
        where = _git_target(words, where)
    if where != REPO_ROOT:  # None (unresolvable or read-free) also falls here
        return
    STATE.violations.append(f"runs `{' '.join(words) or name}` in {where}, the real repo root: {ADVICE}")
