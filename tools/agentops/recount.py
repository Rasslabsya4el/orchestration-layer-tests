"""The recount lister: closed issues whose `## Recount` block still waits for its recount.

    python -m tools.agentops recount --since DATE [--run]

One `gh issue list` call, through `gh_call.run_gh`, reads the closed issues closed on or after DATE (the UTC day of
`closedAt`) with their body and comments. An issue is listed when its latest `## Recount` block (in the body or in a
comment; a body counts as the earliest text of the issue) has no `## Recount result` comment posted after it. A result
comment posted before the latest block does not mark the issue done.

Each listed issue prints as `#N title (closed YYYY-MM-DD)`, then the block's first three lines indented: the heading
and the next two non-blank lines, stopping at the next heading. Then a `script:` line for the block's `- Count:` line:
`ok scripts/counts/<file>` when it names `python -I scripts/counts/<file> ...` and the file exists under the repo root,
else `missing <what it names>`. With `--run`, each ok script runs from the repo root (no shell, `python -I`, at most
COUNT_TIMEOUT seconds) and its last non-empty stdout line prints after the block, or `failed: rc N`, or `timed out`.
Oldest first by `closedAt`. Stdlib and `gh_call` only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from tools.agentops.gh_call import run_gh
from tools.agentops.util import no_window_flags

#: The repo root: this file lives at tools/agentops/recount.py. A test passes another root.
REPO_ROOT = Path(__file__).resolve().parents[2]
#: Count scripts live here, under the repo root; a Count line may name nothing else.
COUNTS_DIR = "scripts/counts"
#: Each count script runs at most this many seconds.
COUNT_TIMEOUT = 600
#: A prose Count line is shown by its first this many characters.
PROSE_CHARS = 60
#: The block's heading line, and a result comment's heading line. `## Recount result` is not a block heading.
BLOCK_HEADING_RE = re.compile(r"^##\s+Recount\s*$")
RESULT_HEADING_RE = re.compile(r"^##\s+Recount result\b")
#: A result line that says `waits` (no incident could occur in the window yet): not final the first time.
WAITS_RE = re.compile(r",\s*waits\s*$", re.IGNORECASE)
ANY_HEADING_RE = re.compile(r"^#{1,6}\s")
#: The block's `- Count: <text>` line; the text is the value after the colon.
COUNT_LINE_RE = re.compile(r"^-\s*Count:\s*(.*?)\s*$")
#: A Count value that names a count script: `python -I <path> <args>`, bare or inside one pair of backticks.
COUNT_CMD_RE = re.compile(r"^`?python -I ([^\s`]+)(.*?)`?$")
#: How many lines of a block the listing prints.
BLOCK_LINES = 3
#: The one list call reads at most this many closed issues.
LIST_LIMIT = "200"


class RecountError(RuntimeError):
    """The `gh` list call failed or answered something that is not a JSON list; the command prints it, exit 2."""


@dataclass(frozen=True)
class Candidate:
    number: int
    title: str
    closed: str
    lines: list[str]
    count: str | None = None


def block_body(text: str | None) -> list[str] | None:
    """All non-blank lines of the first `## Recount` block in `text`, heading first, up to the next heading;
    None when there is no block."""

    lines = (text or "").splitlines()
    for index, line in enumerate(lines):
        if not BLOCK_HEADING_RE.match(line.rstrip()):
            continue
        out = [line.rstrip()]
        for nxt in lines[index + 1:]:
            if ANY_HEADING_RE.match(nxt):
                break
            if nxt.strip():
                out.append(nxt.rstrip())
        return out
    return None


def count_value(body: list[str]) -> str | None:
    """The value of the block's `- Count:` line, or None when the block has none."""

    for line in body:
        match = COUNT_LINE_RE.match(line)
        if match:
            return match.group(1)
    return None


def latest_block(issue: dict) -> tuple[str, list[str]] | None:
    """(posted-at, body lines) of the latest block of the issue; the body's posted-at is "", earlier than any comment."""

    found: list[tuple[str, list[str]]] = []
    body = block_body(issue.get("body"))
    if body:
        found.append(("", body))
    for comment in issue.get("comments") or []:
        lines = block_body(comment.get("body"))
        if lines:
            found.append((comment.get("createdAt") or "", lines))
    if not found:
        return None
    return max(found, key=lambda pair: pair[0])


def recounted_after(issue: dict, posted_at: str) -> bool:
    """True when the block has been recounted: a `## Recount result` comment posted strictly after
    `posted_at` that is not `waits`, or a second one (rule: "waits" is allowed only once)."""

    results = []
    for comment in issue.get("comments") or []:
        if (comment.get("createdAt") or "") <= posted_at:
            continue
        for line in (comment.get("body") or "").splitlines():
            if RESULT_HEADING_RE.match(line):
                results.append(line)
                break
    if len(results) >= 2:
        return True
    return bool(results) and not WAITS_RE.search(results[0])


def list_args(since: date) -> list[str]:
    """The one `gh issue list` call: closed issues closed on or after `since`, with body and comments."""

    return ["issue", "list", "--state", "closed", "--search", f"closed:>={since.isoformat()}",
            "--json", "number,title,closedAt,body,comments", "--limit", LIST_LIMIT]


def read_closed_issues(since: date, runner=None) -> list[dict]:
    """The parsed JSON list of the one `gh` call; a nonzero return code or bad JSON raises RecountError."""

    call = runner or run_gh
    res = call(list_args(since))
    if res.returncode != 0:
        first = next((ln.strip() for ln in str(res.stderr or "").splitlines() if ln.strip()), "no message")
        raise RecountError(f"gh exit {res.returncode}: {first}")
    try:
        data = json.loads(res.stdout or "")
    except ValueError as exc:
        raise RecountError(f"bad JSON from gh: {exc}") from None
    if not isinstance(data, list):
        raise RecountError("gh did not answer a JSON list")
    return data


def find_candidates(since: date, runner=None) -> list[Candidate]:
    """The issues whose latest `## Recount` block has no `## Recount result` after it, oldest first."""

    issues = read_closed_issues(since, runner=runner)
    issues.sort(key=lambda issue: (str(issue.get("closedAt") or ""), int(issue.get("number") or 0)))
    out: list[Candidate] = []
    for issue in issues:
        closed_at = str(issue.get("closedAt") or "")
        if closed_at[:10] < since.isoformat():
            continue
        found = latest_block(issue)
        if found is None:
            continue
        posted_at, body = found
        if recounted_after(issue, posted_at):
            continue
        out.append(Candidate(number=int(issue["number"]), title=str(issue.get("title") or ""),
                             closed=closed_at[:10], lines=body[:BLOCK_LINES], count=count_value(body)))
    return out


def _inside_counts(named: str, root: Path) -> bool:
    """True when `named` is a relative path under scripts/counts/ with no `..` segment, and it is an existing file
    whose real location (symlinks resolved) lies under the real scripts/counts/ of `root`."""

    if not named.startswith(COUNTS_DIR + "/") or ".." in named.split("/"):
        return False
    target = (root / named).resolve()
    return target.is_file() and target.is_relative_to((root / COUNTS_DIR).resolve())


def script_check(count: str | None, root: Path) -> tuple[str, Path | None, list[str]]:
    """The text after `script: `, the script to run (None unless ok) and its arguments, for one Count value."""

    if count is None:
        return "missing no Count line in the block", None, []
    match = COUNT_CMD_RE.match(count.strip())
    if match is None:
        return f"missing {count[:PROSE_CHARS]}", None, []
    named, args = match.group(1), match.group(2).split()
    if not _inside_counts(named, root):
        return f"missing {named}", None, []
    return f"ok {named}", root / named, args


def run_script(script: Path, args: list[str], root: Path, timeout: float) -> str:
    """Runs one count script from `root` with `python -I`, no shell; its last non-empty stdout line, or
    `failed: rc N`, or `timed out`."""

    argv = [sys.executable, "-I", str(script), *args]
    try:
        res = subprocess.run(argv, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=timeout, creationflags=no_window_flags())
    except subprocess.TimeoutExpired:
        return "timed out"
    if res.returncode != 0:
        return f"failed: rc {res.returncode}"
    return next((ln.strip() for ln in reversed(res.stdout.splitlines()) if ln.strip()), "no output")


def format_candidates(candidates: list[Candidate], since: date, *, root: Path | None = None, run: bool = False,
                      timeout: float | None = None) -> str:
    if not candidates:
        return f"no closed issue with a Recount block to recount since {since.isoformat()}"
    root = REPO_ROOT if root is None else root
    timeout = COUNT_TIMEOUT if timeout is None else timeout
    out: list[str] = []
    for c in candidates:
        out.append(f"#{c.number} {c.title} (closed {c.closed})")
        out.extend(f"    {line}" for line in c.lines)
        status, script, args = script_check(c.count, root)
        out.append(f"    script: {status}")
        if run and script is not None:
            out.append(f"    {run_script(script, args, root, timeout)}")
    return "\n".join(out)


def add_subparser(sub: argparse._SubParsersAction) -> None:
    recount = sub.add_parser("recount", help="closed issues whose ## Recount block waits for its recount")
    recount.add_argument("--since", required=True, help="the first closed day to list, YYYY-MM-DD")
    recount.add_argument("--run", action="store_true", help="run each ok count script and print its last line")


def run_recount_command(args: argparse.Namespace) -> int:
    try:
        since = date.fromisoformat(args.since)
    except ValueError:
        print(f"error: --since takes a date YYYY-MM-DD, got {args.since!r}")
        return 2
    try:
        candidates = find_candidates(since)
    except (RecountError, OSError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}")
        return 2
    print(format_candidates(candidates, since, run=args.run))
    return 0
