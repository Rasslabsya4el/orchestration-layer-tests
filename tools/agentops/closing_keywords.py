"""GitHub closing keywords before a ticket reference (#422).

GitHub closes an issue when a commit message, a pull request title or body carries a closing keyword (close,
fix, resolve and their forms) before a reference to it, even across a line break. `find(text)` returns each such
hit with its keyword, its reference and the line of the keyword. The helpers `require_clean`, `require_clean_built` (a code-built message whose slug
names a branch) and `check_range` (the messages of a commit range, with the caller's cwd) are what the agentops
commit, merge and built-message steps call before they write (#454). The command

    python -m tools.agentops closing-check [--range A..B | --stdin]

runs it over the commit messages of `git log A..B` (`--range`) or over stdin (`--stdin`, a pull request's title
and body). A hit exits 1; no hit exits 0.
"""
from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tools.agentops.util import no_window_flags

# A GitHub closing keyword, an optional colon, spaces or line breaks, then a reference: `#N`, `owner/repo#N`,
# `GH-N` or an issue URL. Modelled on orch-it's `merge/marks.py` `_CLOSING`.
_CLOSING = re.compile(
    r"(?P<keyword>\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b):?\s*"
    r"(?P<reference>[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+#\d+|#\d+|GH-\d+|https://github\.com/[^\s/]+/[^\s/]+/issues/\d+)",
    re.IGNORECASE)

_HOW_TO_WRITE = "write `Refs <URL>` or `Ticket: <URL>` instead"


@dataclass(frozen=True)
class Hit:
    keyword: str
    reference: str
    line: int  # 1-based line of the keyword
    line_text: str


def _scan(text: str) -> list[tuple[Hit, tuple[int, int]]]:
    """Each hit of `text` with the span of its reference in `text` (#454)."""
    found: list[tuple[Hit, tuple[int, int]]] = []
    for m in _CLOSING.finditer(text):
        start = m.start("keyword")
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", start)
        if line_end == -1:
            line_end = len(text)
        hit = Hit(
            keyword=m.group("keyword"),
            reference=m.group("reference"),
            line=text.count("\n", 0, start) + 1,
            line_text=text[line_start:line_end].rstrip("\r"),
        )
        found.append((hit, m.span("reference")))
    return found


def find(text: str) -> list[Hit]:
    """Every closing keyword followed by a reference in `text`, in order."""
    return [hit for hit, _span in _scan(text)]


def _commits(rng: str, cwd: str | Path | None = None) -> list[tuple[str, str]]:
    """(sha, message) of each commit in `git log A..B`, the messages read as UTF-8."""
    proc = subprocess.run(["git", "log", "--format=%H%x1f%B%x00", rng], capture_output=True, timeout=60,
                          cwd=cwd, creationflags=no_window_flags())
    if proc.returncode != 0:
        raise ValueError(proc.stderr.decode("utf-8", "replace").strip() or f"git log {rng} failed")
    commits: list[tuple[str, str]] = []
    for record in proc.stdout.decode("utf-8", "replace").split("\x00"):
        record = record.lstrip("\n")  # git puts a newline between two records
        if not record.strip():
            continue
        sha, _, message = record.partition("\x1f")
        commits.append((sha, message))
    return commits


def add_subparser(sub) -> None:
    parser = sub.add_parser(
        "closing-check",
        help="refuse a GitHub closing keyword before a ticket reference in commit messages or stdin (#422)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--range", dest="rng", metavar="A..B",
                       help="the commit messages of git log A..B (a new ref: the caller's base)")
    group.add_argument("--stdin", action="store_true",
                       help="the text on stdin, e.g. a pull request's title and body")


def _report(hit: Hit, where: str) -> str:
    return (f"closing-check: {where}, line {hit.line}: \"{hit.keyword}\" before \"{hit.reference}\" "
            f"closes the ticket on GitHub; the line is \"{hit.line_text}\"; {_HOW_TO_WRITE}")


class ClosingKeywordError(Exception):
    """The check's refusal; its text is one line per hit, the same lines `closing-check` prints."""


# A code-built message carries a branch or a slug: its separators are read as spaces before the check (#454).
_SLUG_SEPARATORS = str.maketrans({"-": " ", "_": " ", "/": " "})


def _check(scan: Callable[[str], list[Hit]], message: str, where: str) -> None:
    """Raise ClosingKeywordError with one line per hit of `scan(message)`; a `scan` that raises refuses the
    message too, it never lets it through (#454)."""
    try:
        hits = scan(message)
    except Exception as exc:
        raise ClosingKeywordError(
            f"closing-check: the closing-keyword check failed on {where} ({type(exc).__name__}: {exc}); "
            "it refuses the message") from exc
    if hits:
        raise ClosingKeywordError("\n".join(_report(hit, where) for hit in hits))


def require_clean(message: str, where: str = "this message") -> None:
    """Raise ClosingKeywordError when `message` holds a closing keyword before a reference, or when the check
    itself fails (#454)."""
    _check(find, message, where)


def _built_hits(message: str) -> list[Hit]:
    """The hits of `message` and of its copy whose `-`, `_` and `/` are spaces (so `fix-#7` reads as `fix #7`).
    A hit only the copy holds quotes the original message's line and reference; the copy keeps every line break,
    so the line number is the same (#454)."""
    hits = find(message)
    known = {(hit.line, hit.keyword, hit.reference) for hit in hits}
    lines = message.split("\n")
    for copy_hit, (start, end) in _scan(message.translate(_SLUG_SEPARATORS)):
        original = Hit(keyword=copy_hit.keyword, reference=message[start:end], line=copy_hit.line,
                       line_text=lines[copy_hit.line - 1].rstrip("\r"))
        key = (original.line, original.keyword, original.reference)
        if key not in known:
            known.add(key)
            hits.append(original)
    return hits


def require_clean_built(message: str, where: str = "this message") -> None:
    """`require_clean` over the message and over its slug-translated copy, so a reference hidden in a branch
    or slug name (`fix-#7`, `closes-GH-7`) is refused too (#454)."""
    _check(_built_hits, message, where)


def check_range(cwd: str | Path, base: str, tip: str) -> list[tuple[str, Hit]]:
    """(sha7, hit) for every closing keyword in the messages of `git log base..tip`, run in `cwd` (#454).

    Raises ValueError on any git failure: a non-zero exit, a timeout, or git that cannot start."""
    try:
        commits = _commits(f"{base}..{tip}", cwd=cwd)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ValueError(f"git log {base}..{tip} failed: {exc}") from exc
    return [(sha[:7], hit) for sha, message in commits for hit in find(message)]


def run_closing_check_command(args) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if args.rng is not None:
        base, sep, tip = args.rng.partition("..")
        if not sep or not base or not tip:
            print(f"closing-check: --range must be A..B, got {args.rng!r}", file=sys.stderr)
            return 2
        try:
            commits = _commits(args.rng)
        except ValueError as exc:
            print(f"closing-check: {exc}", file=sys.stderr)
            return 2
        bad = [(sha, hit) for sha, message in commits for hit in find(message)]
        for sha, hit in bad:
            print(_report(hit, f"commit {sha[:7]}"), file=sys.stderr)
        if bad:
            return 1
        print(f"closing-check: {len(commits)} commits, no closing keyword before a reference")
        return 0
    raw = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read()
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    hits = find(text)
    for hit in hits:
        print(_report(hit, "text"), file=sys.stderr)
    if hits:
        return 1
    print("closing-check: no closing keyword before a reference")
    return 0
