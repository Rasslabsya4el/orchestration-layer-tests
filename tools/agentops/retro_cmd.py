"""Issue 109 (unit 3): `python -m tools.agentops retro-sources --since <date> [--until <date>]`. Read only.

Prints table 1 (`retro_sources.role_rows`: one row per role of the core block's table, then `merge queue`, then
`core push` when the settings name targets) and table 2 (`retro_sources.file_failures` kinds a, b, c, h and
`retro_comments.comment_failures` kinds d, e, f, g: one row per kind, per reason for a, per cause for c), then one
line with the GitHub calls this run made. Writes no file.

The window is [since, until) in local time; `--since` and `--until` take YYYY-MM-DD or YYYY-MM-DDTHH:MM; `--until`
defaults to now. The repo is the `repo` key of the installation's config; the gh seam is `gh_call.run_gh` (check=True),
counted, so a failed call counts too.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Callable

from tools.agentops import retro_comments, retro_sources
from tools.agentops.core_check import DEFAULT_ROOT
from tools.agentops.gh_call import run_gh
from tools.agentops.retro_sources import _duration

DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%dT%H:%M")
DATE_HELP = "YYYY-MM-DD or YYYY-MM-DDTHH:MM, local time"
KIND_ORDER = "abcdefgh"
_CAUSE = re.compile(r"cause: (.+)$")
_NONE = "-"


def moment(raw: str) -> datetime:
    """A `--since` / `--until` value as a naive local datetime; ValueError for any other form."""
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
    raise ValueError(f"not a date: {raw!r} (use {DATE_HELP})")


class _Counting:
    """The gh seam with a counter: every call the run makes, failed ones included."""

    def __init__(self, inner: Callable[[list[str]], str]):
        self._inner = inner
        self.calls = 0

    def __call__(self, args: list[str]) -> str:
        self.calls += 1
        return self._inner(args)


def _gh(args: list[str]) -> str:
    return run_gh(args, check=True).stdout


def _repo(root: Path) -> str | None:
    try:
        repo = json.loads((root / retro_sources.SETTINGS).read_text(encoding="utf-8")).get("repo")
    except (OSError, ValueError, AttributeError):
        return None
    return repo if isinstance(repo, str) and repo else None


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    columns = list(zip(headers, *rows))
    widths = [max(len(str(cell)) for cell in column) for column in columns]

    def line(cells) -> str:
        return " | ".join(str(cell).ljust(width) for cell, width in zip(cells, widths)).rstrip()

    return [line(headers), "-+-".join("-" * width for width in widths), *(line(row) for row in rows)]


def _reason_or_cause(failure: dict) -> str:
    if failure.get("reason"):
        return str(failure["reason"])
    if failure["kind"] == "c":
        match = _CAUSE.search(failure.get("note") or "")
        return match.group(1) if match else _NONE
    return _NONE


def _role_table(rows: list[dict]) -> list[str]:
    body = [[row["role"], row["location"], "yes" if row["exists"] else "no", str(row["entries"])] for row in rows]
    return _table(["role", "location", "exists", "entries"], body)


def _failure_table(failures: list[dict]) -> list[str]:
    ordered = sorted(failures, key=lambda f: KIND_ORDER.index(f["kind"]))
    body = []
    for failure in ordered:
        lost = failure.get("lost_s")
        body.append([failure["kind"], _reason_or_cause(failure), str(failure["count"]),
                     _duration(lost) if lost is not None else _NONE,
                     ", ".join(failure.get("pointers") or []) or _NONE, failure.get("note") or ""])
    return _table(["kind", "reason or cause", "count", "time lost", "pointers (up to 3)", "note"], body)


def run_retro_sources(args: argparse.Namespace, *, root: Path = DEFAULT_ROOT,
                      runner: Callable[[list[str]], str] | None = None, out: Callable[[str], None] = print) -> int:
    """Print the two tables and the call count for ``args`` (``since``, ``until``); returns the exit code.
    ``runner`` is the gh seam (tests inject a fake); ``out`` receives the whole text in one call."""
    root = Path(root)
    repo = _repo(root)
    if repo is None:
        out(f"error: no repo in {retro_sources.SETTINGS.as_posix()}")
        return 2
    try:
        since = moment(args.since)
        until = moment(args.until) if args.until else datetime.now()
    except ValueError as exc:
        out(f"error: {exc}")
        return 2
    if since >= until:
        out("error: --since is not before --until")
        return 2
    counter = _Counting(runner or _gh)
    roles = retro_sources.role_rows(root, since, until)
    failures = (retro_sources.file_failures(root, since, until)
                + retro_comments.comment_failures(repo, since, until, counter))
    lines = [
        f"retro sources {since:%Y-%m-%d %H:%M} .. {until:%Y-%m-%d %H:%M} (local), repo {repo}",
        "",
        "Table 1: records per role and step",
        *_role_table(roles),
        "",
        "Table 2: link failures per kind (a-h), time lost where the records give it",
        *_failure_table(failures),
        "",
        f"GitHub calls this run: {counter.calls}",
    ]
    out("\n".join(lines))
    return 0


def add_retro_subparser(sub) -> argparse.ArgumentParser:
    """Register the `retro-sources` command on the CLI's subparsers."""
    parser = sub.add_parser("retro-sources",
                            help="the weekly retro's records per role and its link failures per kind (issue #109); read only")
    parser.add_argument("--since", required=True, help=f"window start, {DATE_HELP}")
    parser.add_argument("--until", default=None, help=f"window end, {DATE_HELP} (default: now)")
    return parser
