"""logic-audit part 3: the "until" conditions of the tracked text (issue #191). Stdlib only, plus
``gh_call.run_gh`` for the default issue lookup.

Read-only, writes no file. The scan reads the lines of the tracked text files (``git ls-files``) that hold a
deferral marker (for now, temporary, temporarily, until #N, TODO, HACK, workaround; case-insensitive) and
sorts them into two kinds of row:

* ``until met``: the line names an issue (``#N``) and that issue is closed. The owner decides what to do.
* ``until open-ended``: the marker names no issue. Listed with its age, from ``git blame``, oldest first.

A line naming only open or unknown issues gives no row. No git repo gives one ``no source`` row.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NamedTuple

from .core_format import human_duration
from .gh_call import run_gh
from .logic_audit_rows import Row, no_source_row
from .util import no_window_flags

PART = "until"
#: The file types the scan reads.
SCANNED_SUFFIXES = (".py", ".md", ".txt", ".json", ".ps1", ".ts", ".tsx", ".js", ".yml", ".yaml", ".toml")
#: Paths (repo-relative, forward slashes) never scanned: research notes, every test (fixtures included), generated output.
SKIPPED_PREFIXES = ("docs/research/", "tests/", "artifacts/", "var/")
#: A file bigger than this is never read.
MAX_FILE_BYTES = 300_000
#: At most this many distinct issue numbers are looked up.
MAX_LOOKUPS = 40
#: At most this many ``git blame`` calls; a candidate beyond it has age ``unknown``.
MAX_BLAME_CALLS = 300
#: At most this many open-ended rows (the oldest first).
MAX_OPEN_ENDED_ROWS = 100
#: The line text in a finding is cut to this many characters.
TEXT_CHARS = 100

_MARKER_RE = re.compile(
    r"(?<!\w)(until\s+#\d+|for now|temporary|temporarily|todo|hack|workaround)(?!\w)", re.IGNORECASE)
_REF_RE = re.compile(r"#(\d+)")


class _Hit(NamedTuple):
    """One line that holds a marker: where it is, its text, the marker's label, and the issue numbers it names."""

    rel: str
    line: int
    text: str
    label: str
    refs: tuple[int, ...]


def _label(word: str) -> str:
    """The marker as a reader names it: any ``until #N`` is the ``until`` marker."""

    return "until" if word.lower().startswith("until") else word


def _git(root: Path, args: list[str]) -> subprocess.CompletedProcess | None:
    """One ``git -C root <args>``; None when git itself cannot run."""

    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              creationflags=no_window_flags())
    except OSError:
        return None


def _tracked(root: Path) -> list[str] | None:
    """The tracked paths under ``root`` (forward slashes), or None when ``root`` is not in a git repo."""

    res = _git(root, ["ls-files", "-z"])
    if res is None or res.returncode:
        return None
    return [p for p in res.stdout.decode("utf-8", errors="replace").split("\0") if p]


def _scanned(rel: str) -> bool:
    return rel.endswith(SCANNED_SUFFIXES) and not rel.startswith(SKIPPED_PREFIXES)


def _scan(root: Path, tracked: list[str]) -> list[_Hit]:
    """Every line of every scanned file that holds a marker, in file order then line order."""

    hits: list[_Hit] = []
    for rel in tracked:
        if not _scanned(rel):
            continue
        path = root / rel
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, start=1):
            found = _MARKER_RE.search(line)
            if found is None:
                continue
            refs = tuple(int(n) for n in _REF_RE.findall(line))
            hits.append(_Hit(rel, number, line.strip(), _label(found.group(1)), refs))
    return hits


def _gh_issue_state(root: Path) -> Callable[[int], str | None]:
    """The default lookup: ``gh issue view N --json state`` in ``root``; any failure reads as unknown (None)."""

    def state(number: int) -> str | None:
        try:
            res = run_gh(["issue", "view", str(number), "--json", "state"], cwd=root)
        except Exception:  # noqa: BLE001 - a failed lookup is unknown, never an error of the audit
            return None
        if res.returncode:
            return None
        try:
            return str(json.loads(res.stdout or "")["state"])
        except (ValueError, KeyError, TypeError):
            return None

    return state


def _states(numbers: list[int], lookup: Callable[[int], str | None]) -> dict[int, str | None]:
    """``open`` / ``closed`` per distinct number, in the order given; past ``MAX_LOOKUPS`` every number is unknown."""

    out: dict[int, str | None] = {}
    for index, number in enumerate(numbers):
        value = lookup(number) if index < MAX_LOOKUPS else None
        text = value.lower() if isinstance(value, str) else None
        out[number] = text if text in ("open", "closed") else None
    return out


def _author_time(root: Path, hit: _Hit) -> float | None:
    """The line's ``author-time`` (epoch seconds) from ``git blame``, None when blame cannot tell."""

    res = _git(root, ["blame", "-L", f"{hit.line},{hit.line}", "--porcelain", "--", hit.rel])
    if res is None or res.returncode:
        return None
    for raw in res.stdout.decode("utf-8", errors="replace").splitlines():
        if raw.startswith("author-time "):
            try:
                return float(raw.split(" ", 1)[1])
            except ValueError:
                return None
    return None


def _clip(text: str) -> str:
    return text[:TEXT_CHARS]


def _open_ended_rows(root: Path, hits: list[_Hit], now: datetime) -> list[Row]:
    """The marker lines that name no issue, oldest first (unknown age last), at most ``MAX_OPEN_ENDED_ROWS``."""

    now_epoch = now.timestamp()
    ages: list[tuple[float | None, int, _Hit]] = []
    for index, hit in enumerate(hits):
        stamp = _author_time(root, hit) if index < MAX_BLAME_CALLS else None
        ages.append((stamp, index, hit))
    ages.sort(key=lambda item: (item[0] is None, item[0] or 0.0, item[1]))
    rows: list[Row] = []
    for stamp, _index, hit in ages[:MAX_OPEN_ENDED_ROWS]:
        age = "unknown" if stamp is None else human_duration(now_epoch - stamp)
        rows.append(Row(kind="until open-ended",
                        finding=f"{hit.label} without a ticket, {age} old: {_clip(hit.text)}",
                        place_a=f"{hit.rel}:{hit.line}", verdict="listed"))
    return rows


def until_rows(root: Path, issue_state: Callable[[int], str | None] | None = None, *,
               now: datetime | None = None) -> list[Row]:
    """The "until" rows of the repo at ``root``: ``until met`` for a closed ``#N`` on a marker line, ``until open-ended``
    for a marker line with no ``#N`` (with its age). ``issue_state(n)`` answers ``"open"``, ``"closed"`` or None;
    the default asks ``gh``. ``now`` is the clock for the ages (default: the current time)."""

    root = Path(root)
    tracked = _tracked(root)
    if tracked is None:
        return [no_source_row(PART, f"no git repo at {root}")]
    hits = _scan(root, tracked)
    lookup = issue_state if issue_state is not None else _gh_issue_state(root)
    numbers = list(dict.fromkeys(n for hit in hits for n in hit.refs))
    states = _states(numbers, lookup)

    met: list[Row] = []
    candidates: list[_Hit] = []
    for hit in hits:
        if not hit.refs:
            candidates.append(hit)
            continue
        closed = next((n for n in hit.refs if states.get(n) == "closed"), None)
        if closed is None:
            continue
        met.append(Row(kind="until met",
                       finding=f"{hit.label} #{closed} but #{closed} is closed: {_clip(hit.text)}",
                       place_a=f"{hit.rel}:{hit.line}", verdict="owner decides"))
    moment = now if now is not None else datetime.now(timezone.utc)
    return met + _open_ended_rows(root, candidates, moment)
