"""The exception-list part of `logic-audit` (issue #191, part 2). Stdlib only, read-only: it writes no file.

The lists are the allow / ignore / exclude / ... lists of the repo's config (``config/*.json``) and of its guard modules
(``.py`` files named ``*guard*`` or under ``tools/hooks/``). Two findings:

- ``dead exception``: an entry that matches no tracked file (a path glob) and occurs in no other tracked file.
- ``list only grew``: a list whose entries at the window's start are all still there, with some added.

``until`` is the window's end: the growth compares the last commit at or before ``since`` with the last commit at or
before ``until`` (``HEAD`` when ``until`` is None). The dead check always reads ``HEAD``.
"""

from __future__ import annotations

import ast
import fnmatch
import json
import re
import subprocess
from datetime import date, datetime
from pathlib import Path, PurePosixPath

from tools.agentops.logic_audit_rows import Row, no_source_row
from tools.agentops.util import no_window_flags

#: A list key or a module-level name matches this (case-insensitive) when it is an exception list.
_LIST_NAME = re.compile(r"allow|ignore|exclude|exempt|except|skip|whitelist|special|waiv", re.IGNORECASE)
#: An entry is checked only when its length is in this range (a short word or a long line is not a file name).
_ENTRY_MIN, _ENTRY_MAX = 3, 200
#: At most this many entries are checked per run.
_CHECK_CAP = 200
#: A growth row names at most this many added entries.
_ADDED_SHOWN = 5

_Lists = dict[str, list[tuple[str, int]]]  # list id -> [(entry, line)]


#: git returned no answer at all (could not start, or timed out): not "no match", not "no repo".
_NO_ANSWER = -1


class _GitUnavailable(RuntimeError):
    """A git call the part needs gave no answer; the part returns one no-source row instead."""


def exception_rows(root: Path, since: date | datetime, until: date | datetime | None = None) -> list[Row]:
    """The rows of the exception-list part for the git repo at ``root``: dead entries, then lists that only grew."""

    root = Path(root)
    try:
        return _rows(root, since, until)
    except _GitUnavailable as exc:
        return [no_source_row("exceptions", f"git did not answer: {exc}")]


def _rows(root: Path, since: date | datetime, until: date | datetime | None) -> list[Row]:
    rc, tracked = _tracked(root)
    if rc == _NO_ANSWER:
        raise _GitUnavailable(f"ls-files in {root} could not run or timed out")
    if rc != 0:
        return [no_source_row("exceptions", f"no git repo at {root}")]
    files = _config_files(tracked) + _guard_files(tracked)
    if not files:
        return [no_source_row("exceptions", "no config/*.json and no guard .py file tracked")]

    lists_head = _all_lists(root, files, "HEAD")
    lists_then = _lists_at(root, files, since)
    lists_now = lists_head if until is None else _lists_at(root, files, until)

    rows = _dead_rows(root, tracked, lists_head)
    rows += _growth_rows(lists_now, lists_then)
    return rows


# --- which files --------------------------------------------------------------------------------------------------

def _git(root: Path, *args: str) -> tuple[int, str]:
    """``(returncode, stdout)`` of one git call on ``root``; ``(_NO_ANSWER, "")`` when git cannot be started or
    times out. A caller never reads that as git's own exit code 1 ("no match" for grep)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=120, creationflags=no_window_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return _NO_ANSWER, ""
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace")


def _tracked(root: Path) -> tuple[int, list[str]]:
    """``(rc, tracked paths)`` with forward slashes; ``rc`` is git's own (128 when ``root`` is not a git repo)."""

    rc, out = _git(root, "ls-files", "-z")
    return rc, [path for path in out.split("\0") if path]


def _config_files(tracked: list[str]) -> list[str]:
    """``config/*.json``: the json files directly in the repo's ``config`` folder."""

    return [p for p in tracked if p.startswith("config/") and p.count("/") == 1 and p.endswith(".json")]


def _guard_files(tracked: list[str]) -> list[str]:
    """Python files with ``guard`` in their name, and every python file under ``tools/hooks/``."""

    return [
        p for p in tracked
        if p.endswith(".py") and ("guard" in PurePosixPath(p).name.lower() or p.startswith("tools/hooks/"))
    ]


# --- reading the lists ----------------------------------------------------------------------------------------------

def _show(root: Path, ref: str, path: str) -> tuple[int, str | None]:
    """``(rc, text)``; the text is None when the file does not exist at ``ref`` (git's rc 128)."""

    rc, out = _git(root, "show", f"{ref}:{path}")
    return rc, (out if rc == 0 else None)


def _last_commit_before(root: Path, when: date | datetime) -> str | None:
    """The last commit at or before ``when``; a plain date means its local midnight (git would otherwise fill
    in the current time of day, and the commits of that day would count as before it). None when no commit is that old."""

    stamp = when.isoformat() if isinstance(when, datetime) else f"{when.isoformat()}T00:00:00"
    rc, out = _git(root, "rev-list", "-1", f"--before={stamp}", "HEAD")
    if rc == _NO_ANSWER:
        raise _GitUnavailable(f"rev-list before {stamp} could not run or timed out")
    if rc != 0:
        raise _GitUnavailable(f"rev-list before {stamp} failed with rc {rc}")
    sha = out.strip()
    return sha or None


def _lists_at(root: Path, files: list[str], when: date | datetime) -> _Lists:
    """The lists of ``files`` at the last commit at or before ``when`` (none when no commit is that old)."""

    sha = _last_commit_before(root, when)
    return _all_lists(root, files, sha) if sha else {}


def _all_lists(root: Path, files: list[str], ref: str) -> _Lists:
    """Every list of ``files`` as ``ref`` has them; a file that does not exist at ``ref`` has none."""

    lists: _Lists = {}
    for path in files:
        rc, text = _show(root, ref, path)
        if rc == _NO_ANSWER:
            raise _GitUnavailable(f"show {ref}:{path} could not run or timed out")
        if text is not None:
            lists.update(_lists_of(path, text))
    return lists


def _lists_of(path: str, text: str) -> _Lists:
    """The exception lists one file holds: ``<file>::<dotted key path>`` for json, ``<file>::<NAME>`` for python."""

    if path.endswith(".json"):
        try:
            data = json.loads(text)
        except ValueError:
            return {}
        found: dict[str, list[str]] = {}
        _walk_json(data, "", found)
        lines = text.splitlines()
        return {
            f"{path}::{key}": [(entry, _json_line(lines, entry)) for entry in entries]
            for key, entries in found.items()
        }
    return {f"{path}::{name}": items for name, items in _python_lists(text).items()}


def _walk_json(node: object, prefix: str, found: dict[str, list[str]]) -> None:
    """Collect the matching keys of a json value at any depth: a list gives its string items, an object its keys."""

    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if _LIST_NAME.search(str(key)):
                if isinstance(value, list):
                    found[path] = [item for item in value if isinstance(item, str)]
                elif isinstance(value, dict):
                    found[path] = [str(k) for k in value]
            _walk_json(value, path, found)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _walk_json(item, f"{prefix}[{index}]", found)


def _json_line(lines: list[str], entry: str) -> int:
    """The line of the quoted entry in the file's text, or 1 when no line holds it."""

    needle = json.dumps(entry, ensure_ascii=False)
    for number, line in enumerate(lines, 1):
        if needle in line:
            return number
    return 1


def _python_lists(text: str) -> dict[str, list[tuple[str, int]]]:
    """Module-level assignments of a list, tuple, set or frozenset literal of strings to a matching name."""

    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return {}
    found: dict[str, list[tuple[str, int]]] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            names = [target.id for target in stmt.targets if isinstance(target, ast.Name)]
            value = stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            names = [stmt.target.id]
            value = stmt.value
        else:
            continue
        constants = _string_literal_items(value)
        if constants is None:
            continue
        for name in names:
            if _LIST_NAME.search(name):
                found[name] = [(node.value, node.lineno) for node in constants]
    return found


def _string_literal_items(node: ast.expr) -> list[ast.Constant] | None:
    """The string constants of a list, tuple, set or frozenset({...}) literal; ``None`` for anything else."""

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "frozenset" \
            and len(node.args) == 1 and not node.keywords:
        node = node.args[0]
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [e for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return None


# --- the two findings -----------------------------------------------------------------------------------------------

def _matches_path(entry: str, tracked: list[str]) -> bool:
    """True when ``entry`` is a path glob matching a tracked file, on the whole path or on its basename. Case is
    compared exactly (fnmatch would fold case on Windows)."""

    return any(
        fnmatch.fnmatchcase(path, entry) or fnmatch.fnmatchcase(PurePosixPath(path).name, entry)
        for path in tracked
    )


def _occurs_elsewhere(root: Path, entry: str, holder: str) -> bool:
    """True when ``entry`` occurs literally in a tracked file other than ``holder``. Only git's exit 1 (no match) lets
    an entry count as dead; a git call that could not run or timed out, or any other error, counts as occurring."""

    rc, out = _git(root, "grep", "-F", "-l", "-e", entry, "--")
    if rc == 1:
        return False
    if rc == 0:
        return any(line and line != holder for line in out.splitlines())
    return True


def _dead_rows(root: Path, tracked: list[str], lists: _Lists) -> list[Row]:
    rows: list[Row] = []
    checked = 0
    for list_id, items in sorted(lists.items()):
        holder = list_id.split("::", 1)[0]
        for entry, line in items:
            if not _ENTRY_MIN <= len(entry) <= _ENTRY_MAX:
                continue
            if checked >= _CHECK_CAP:
                return rows
            checked += 1
            if _matches_path(entry, tracked) or _occurs_elsewhere(root, entry, holder):
                continue
            rows.append(Row(
                kind="dead exception",
                finding=f"entry {entry} of {list_id} matches nothing",
                place_a=f"{holder}:{line}",
                verdict="owner decides",
            ))
    return rows


def _unique(entries: list[str]) -> list[str]:
    return list(dict.fromkeys(entries))


def _growth_rows(now: _Lists, then: _Lists) -> list[Row]:
    """One row per list whose entries at ``then`` are all still in ``now``, with at least one added. A list absent at
    ``then`` counts as grown from 0 only when it has two or more entries."""

    rows: list[Row] = []
    for list_id in sorted(now):
        new = _unique([entry for entry, _ in now[list_id]])
        if list_id in then:
            base = _unique([entry for entry, _ in then[list_id]])
        elif len(new) >= 2:
            base = []
        else:
            continue
        if any(entry not in new for entry in base):
            continue
        added = [entry for entry in new if entry not in base]
        if not added:
            continue
        rows.append(Row(
            kind="list only grew",
            finding=(f"{list_id} grew {len(base)} -> {len(new)} entries: "
                     f"added {', '.join(added[:_ADDED_SHOWN])}"),
            place_a=list_id.split("::", 1)[0],
            count=len(added),
            verdict="owner decides",
        ))
    return rows
