"""Issue 109 (unit 1): the records the weekly retro reads, per role and per link failure kind. Read only.

    role_rows(root, since, until) -> [{"role", "location", "exists", "entries"}, ...]
        one row per role of the core block's "Roles (orchestration v2)" table, then `merge queue`, then
        `core push` (only when the settings name `targets`). A role with no records is
        {"exists": False, "entries": 0, "location": "no records"}.
    file_failures(root, since, until) -> [{"kind", "count", "lost_s", "pointers", "note"}, ...]
        link failures of kinds a, b, c, h only (d-g belong to another unit). a is one row per reason (with the
        numeric keys `reason`, `in_line_s`, `suite_s`; lost_s = in_line_s + suite_s); b is one row; c is one row
        per cause. A kind with no source is count 0 with the note "no source". `lost_s` is raw seconds or None;
        a note prints durations only through `_duration`. A JSON result is pointed at by its bare path, a log
        line by `path:line`.

Paths: `var/agentops` and `artifacts/runs` under `root` (named as tools/agentops/merge.py names them); the Claude
projects folder is the settings' `claude_projects_dir` under CLAUDE_PROJECTS_BASE. `since`/`until` are datetimes:
naive means local time, zoned ones are converted to local; the window is [since, until). Stdlib only.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

VAR_AGENTOPS = Path("var") / "agentops"
RESULTS = VAR_AGENTOPS / "results"
MERGE_LOG = VAR_AGENTOPS / "merge.log"
RUNS = Path("artifacts") / "runs"
DISPATCH_OUT = RUNS / "dispatch.out"
EVENTS = RUNS / "events.jsonl"
STATE = Path("docs") / "STATE.md"
RESEARCH = Path("docs") / "research"
CORE_BLOCK = Path("docs") / "agents" / "core-block.md"
SETTINGS = Path("config") / "orchestration.json"
#: the Claude Code projects folder; the installation's own folder is <base>/<settings claude_projects_dir>
CLAUDE_PROJECTS_BASE = Path.home() / ".claude" / "projects"

ROLES_HEADING = "## Roles (orchestration v2)"
#: the table rows whose records are subagent transcripts, by the agent types their meta files name
SUBAGENT_TYPES = {
    "researcher-find / researcher-how": ("researcher-find", "researcher-how"),
    "worker / worker-strong": ("worker", "worker-strong"),
    "reviewer-standards": ("reviewer-standards",),
    "reviewer-spec": ("reviewer-spec",),
}
#: did-not-land reasons: outside the branch's own code (a), needed a code change to land (b)
A_REASONS = ("target-check", "conflict", "rebase-conflict", "lock-timeout", "red-on-base")
B_REASONS = ("red-suite", "red-on-branch")
#: the core push failure causes, in the order the checks run (the first match wins)
CAUSES = ("rate limit", "uncommitted", "core update refused", "git commit failed", "merge lock held", "other")
NO_RECORDS = "no records"
NO_SOURCE = "no source"
POINTERS = 3

_RESULT_STAMP = re.compile(r"^(\d{8}T\d{6})")
_LINE_STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
_STATE_UPDATED = re.compile(r"^Updated[^:]*:\s*(\d{4}-\d\d-\d\d)(?:\s+(\d\d:\d\d))?")
_GH_CALLER = re.compile(r"gh api '?([^\s'\"]+)")


@dataclass(frozen=True)
class _Result:
    rel: str
    row: dict
    when: datetime | None


# ---- public ---------------------------------------------------------------------------------------------------------

def role_rows(root, since: datetime, until: datetime) -> list[dict]:
    root = Path(root)
    since, until = _naive(since), _naive(until)
    settings = _settings(root)
    base = _projects_dir(settings)
    subagents = _subagent_index(base)
    results = _load_results(root)
    rows = []
    for role in _roles(root):
        if role == "brain":
            location, entries = _brain(root, since, until)
        elif role == "ORCH":
            location, entries = _orch(root, since, until)
        elif role == "dispatcher":
            location, entries = _dispatcher(root, since, until)
        elif role == "external researcher":
            location, entries = _research(root, since, until)
        elif role in SUBAGENT_TYPES:
            location, entries = _subagent(SUBAGENT_TYPES[role], subagents, base, since, until)
        else:
            location, entries = None, 0
        rows.append(_row(role, location, entries))
    rows.append(_row("merge queue", *_merge_queue(root, results, since, until)))
    if _targets(settings):
        rows.append(_row("core push", *_core_push(results, since, until)))
    return rows


def file_failures(root, since: datetime, until: datetime) -> list[dict]:
    root = Path(root)
    since, until = _naive(since), _naive(until)
    results = _load_results(root)
    window = [r for r in results if _in(r.when, since, until)]
    has_results = (root / RESULTS).is_dir()
    rows = []
    rows += _kind_a_rows(window, has_results)
    rows += _kind_b_row(window, has_results)
    rows += _core_failure_rows(window, has_results)
    rows += _sampler_row(root, since, until)
    rows += _log_rate_limit_row(root, since, until)
    return rows


# ---- roles ----------------------------------------------------------------------------------------------------------

def _roles(root: Path) -> list[str]:
    path = root / CORE_BLOCK
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"no roles table: cannot read {path} ({exc})") from exc
    roles: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == ROLES_HEADING:
            inside = True
            continue
        if inside and line.startswith("## "):
            break
        if inside and line.lstrip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells[0] == "role" or set(cells[0]) <= set("-:"):
                continue
            roles.append(cells[0])
    if not roles:
        raise RuntimeError(f"no roles table in {path}")
    return roles


def _brain(root: Path, since: datetime, until: datetime) -> tuple[str | None, int]:
    """The brain's hand-off lines in docs/STATE.md (`Updated ...: <date> [<time>]`); a line with no time counts by day."""
    path = root / STATE
    if not path.is_file():
        return None, 0
    entries = 0
    for line in _lines(path):
        match = _STATE_UPDATED.match(line)
        if not match:
            continue
        try:
            day = datetime.strptime(match.group(1), "%Y-%m-%d")
            if match.group(2):
                when = datetime.strptime(f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H:%M")
                entries += _in(when, since, until)
            else:
                entries += since.date() <= day.date() <= until.date()
        except ValueError:
            continue
    return STATE.as_posix(), entries


def _orch(root: Path, since: datetime, until: datetime) -> tuple[str | None, int]:
    """ORCH sessions: the `orch_start` events of artifacts/runs/events.jsonl."""
    path = root / EVENTS
    if not path.is_file():
        return None, 0
    entries = 0
    for line in _lines(path):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("kind") == "orch_start":
            entries += _in(_parse(row.get("ts")), since, until)
    return EVENTS.as_posix(), entries


def _dispatcher(root: Path, since: datetime, until: datetime) -> tuple[str | None, int]:
    """The dispatcher's lines in artifacts/runs/dispatch.out whose leading stamp is in the window."""
    path = root / DISPATCH_OUT
    if not path.is_file():
        return None, 0
    entries = 0
    for line in _lines(path):
        match = _LINE_STAMP.match(line)
        if match:
            entries += _in(_local_stamp(match.group(1)), since, until)
    return DISPATCH_OUT.as_posix(), entries


def _research(root: Path, since: datetime, until: datetime) -> tuple[str | None, int]:
    """External research reports (docs/research/*.md), counted by their file's last write."""
    folder = root / RESEARCH
    if not folder.is_dir():
        return None, 0
    entries = 0
    for path in folder.glob("*.md"):
        entries += _in(_mtime(path), since, until)
    return RESEARCH.as_posix(), entries


def _subagent(types: tuple, index: list[tuple[str, datetime | None]], base: Path | None,
              since: datetime, until: datetime) -> tuple[str | None, int]:
    """Subagent transcripts of the given agent types (their `.meta.json` names the type), counted by the start."""
    found = [when for agent_type, when in index if agent_type in types]
    if base is None or not found:
        return None, 0
    location = f"{(base / '*' / 'subagents').as_posix()} ({', '.join(types)})"
    return location, sum(_in(when, since, until) for when in found)


def _merge_queue(root: Path, results: list[_Result], since: datetime, until: datetime) -> tuple[str | None, int]:
    """Merge results (var/agentops/results/*-merge-*.json) and the merge log."""
    places = []
    if results:
        places.append(f"{RESULTS.as_posix()}/*-merge-*.json")
    if (root / MERGE_LOG).is_file():
        places.append(MERGE_LOG.as_posix())
    if not places:
        return None, 0
    return "; ".join(places), sum(_in(r.when, since, until) for r in results)


def _core_push(results: list[_Result], since: datetime, until: datetime) -> tuple[str | None, int]:
    """The core update lines each merge result carries (`core_targets`), counted in the window."""
    if not results:
        return None, 0
    lines = 0
    for r in results:
        if _in(r.when, since, until):
            lines += sum(1 for item in r.row.get("core_targets") or [] if isinstance(item, str))
    return f"{RESULTS.as_posix()}/*-merge-*.json (core_targets)", lines


def _row(role: str, location: str | None, entries: int) -> dict:
    if location is None:
        return {"role": role, "location": NO_RECORDS, "exists": False, "entries": 0}
    return {"role": role, "location": location, "exists": True, "entries": entries}


# ---- failures -------------------------------------------------------------------------------------------------------

def _kind_a_rows(window: list[_Result], has_results: bool) -> list[dict]:
    """Kind a: one row per did-not-land reason outside the branch's code, each with its own count and time."""
    if not has_results:
        return [_failure("a", 0, None, [], NO_SOURCE) | {"reason": None, "in_line_s": None, "suite_s": None}]
    hits = [r for r in window if r.row.get("reason") in A_REASONS]
    if not hits:
        return [_failure("a", 0, 0, [], "no merge with these reasons in the window")
                | {"reason": None, "in_line_s": 0, "suite_s": 0}]
    rows = []
    for name in A_REASONS:
        own = [r for r in hits if r.row["reason"] == name]
        if not own:
            continue
        in_line = round(sum(_seconds(r.row.get("in_line_s")) for r in own))
        suite = round(sum(_seconds(r.row.get("suite_s")) for r in own))
        note = f"{name}: time in line {_duration(in_line)}, time in suite {_duration(suite)}"
        if name == "conflict":
            note += "; plain count, the records hold no 'landed in the same window' evidence"
        pointers = [r.rel for r in own[:POINTERS]]
        rows.append(_failure("a", len(own), in_line + suite, pointers, note)
                    | {"reason": name, "in_line_s": in_line, "suite_s": suite})
    return rows


def _kind_b_row(window: list[_Result], has_results: bool) -> list[dict]:
    """Kind b: one row for the did-not-land reasons that needed a code change to land."""
    if not has_results:
        return [_failure("b", 0, None, [], NO_SOURCE)]
    hits = [r for r in window if r.row.get("reason") in B_REASONS]
    if not hits:
        return [_failure("b", 0, 0, [], "no merge with these reasons in the window")]
    parts = []
    for name in B_REASONS:
        own = [r for r in hits if r.row["reason"] == name]
        in_line = round(sum(_seconds(r.row.get("in_line_s")) for r in own))
        suite = round(sum(_seconds(r.row.get("suite_s")) for r in own))
        parts.append(f"{name} {len(own)} (time in line {_duration(in_line)}, time in suite {_duration(suite)})")
    in_line = sum(_seconds(r.row.get("in_line_s")) for r in hits)
    suite = sum(_seconds(r.row.get("suite_s")) for r in hits)
    note = ("reasons counted: " + ", ".join(B_REASONS) + "; " + "; ".join(parts)
            + "; not `rework`: it counts tickets by title, not merge results")
    pointers = [r.rel for r in hits[:POINTERS]]
    return [_failure("b", len(hits), round(in_line + suite), pointers, note)]


def _core_failure_rows(window: list[_Result], has_results: bool) -> list[dict]:
    if not has_results:
        return [_failure("c", 0, None, [], NO_SOURCE)]
    found: dict[str, list] = {}
    for r in window:
        for item in r.row.get("core_targets") or []:
            if not isinstance(item, str) or ": failed: " not in item:
                continue
            bucket = found.setdefault(_cause(item.split(": failed: ", 1)[1]), [0, []])
            bucket[0] += 1
            if len(bucket[1]) < POINTERS:
                bucket[1].append(r.rel)
    if not found:
        return [_failure("c", 0, None, [], "core push failed: none in the window")]
    return [_failure("c", found[cause][0], None, found[cause][1], f"core push failed, cause: {cause}")
            for cause in CAUSES if cause in found]


def _sampler_row(root: Path, since: datetime, until: datetime) -> list[dict]:
    """The GitHub budget sampler rows (artifacts/runs/gh-budget-*.jsonl) with 0 remaining, in the window."""
    folder = root / RUNS
    files = sorted(folder.glob("gh-budget-*.jsonl")) if folder.is_dir() else []
    if not files:
        return [_failure("h", 0, None, [], "gh-budget sampler rows at 0 remaining: no source")]
    zero: list[str] = []
    total = 0
    for path in files:
        rel = _rel(path, root)
        for number, line in enumerate(_lines(path), 1):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or not _in(_parse(row.get("t")), since, until):
                continue
            total += 1
            if row.get("remaining") == 0:
                zero.append(f"{rel}:{number}")
    note = (f"gh-budget sampler rows at 0 remaining, of {total} sampler rows in the window; "
            "the sampler names no caller")
    return [_failure("h", len(zero), None, zero[:POINTERS], note)]


def _log_rate_limit_row(root: Path, since: datetime, until: datetime) -> list[dict]:
    """`rate limit exceeded` lines of the merge log and the dispatch log whose stamp is in the window."""
    logs = [root / p for p in (MERGE_LOG, DISPATCH_OUT) if (root / p).is_file()]
    if not logs:
        return [_failure("h", 0, None, [], "rate limit exceeded lines in the logs: no source")]
    hits: list[str] = []
    callers: list[str] = []
    for path in logs:
        rel = _rel(path, root)
        for number, line in enumerate(_lines(path), 1):
            if "rate limit exceeded" not in line:
                continue
            match = _LINE_STAMP.match(line)
            if not match or not _in(_local_stamp(match.group(1)), since, until):
                continue
            hits.append(f"{rel}:{number}")
            for caller in _GH_CALLER.findall(line):
                if caller not in callers:
                    callers.append(caller)
    named = ", ".join(callers[:POINTERS]) or "none"
    note = f"rate limit exceeded lines in the logs; callers named: {named}"
    return [_failure("h", len(hits), None, hits[:POINTERS], note)]


def _cause(reason: str) -> str:
    low = reason.lower()
    if "rate limit" in low:
        return "rate limit"
    if "uncommitted" in low:
        return "uncommitted"
    if "core update refused" in low:
        return "core update refused"
    if "git commit failed" in low:
        return "git commit failed"
    if "the merge lock is held" in low:
        return "merge lock held"
    return "other"


def _failure(kind: str, count: int, lost_s: int | None, pointers: list[str], note: str) -> dict:
    return {"kind": kind, "count": count, "lost_s": lost_s, "pointers": pointers, "note": note}


# ---- readers and helpers --------------------------------------------------------------------------------------------

def _load_results(root: Path) -> list[_Result]:
    """Every readable merge result, in file-name order; its moment is ended, else started, else the file's stamp."""
    folder = root / RESULTS
    if not folder.is_dir():
        return []
    out: list[_Result] = []
    for path in sorted(folder.glob("*-merge-*.json")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            row = json.loads(text)
        except (OSError, ValueError):
            continue
        if not isinstance(row, dict):
            continue
        when = _parse(row.get("ended")) or _parse(row.get("started")) or _name_stamp(path.name)
        out.append(_Result(_rel(path, root), row, when))
    return out


def _subagent_index(base: Path | None) -> list[tuple[str, datetime | None]]:
    """(agent type, start moment) of every subagent transcript under the installation's projects folder."""
    if base is None or not base.is_dir():
        return []
    index: list[tuple[str, datetime | None]] = []
    for meta in base.glob("*/subagents/agent-*.meta.json"):
        try:
            agent_type = json.loads(meta.read_text(encoding="utf-8")).get("agentType")
        except (OSError, ValueError, AttributeError):
            continue
        if not isinstance(agent_type, str):
            continue
        transcript = meta.with_name(meta.name[: -len(".meta.json")] + ".jsonl")
        index.append((agent_type, _first_stamp(transcript) or _mtime(meta)))
    return index


def _first_stamp(path: Path) -> datetime | None:
    """The first `timestamp` among the first lines of a transcript."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for _ in range(5):
                line = fh.readline()
                if not line:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                when = _parse(row.get("timestamp")) if isinstance(row, dict) else None
                if when is not None:
                    return when
    except OSError:
        return None
    return None


def _settings(root: Path) -> dict:
    try:
        data = json.loads((root / SETTINGS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _projects_dir(settings: dict) -> Path | None:
    name = settings.get("claude_projects_dir")
    return CLAUDE_PROJECTS_BASE / name if isinstance(name, str) and name else None


def _targets(settings: dict) -> bool:
    targets = settings.get("targets")
    return isinstance(targets, list) and bool(targets)


def _lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _naive(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment
    return moment.astimezone().replace(tzinfo=None)


def _parse(stamp: object) -> datetime | None:
    if not isinstance(stamp, str):
        return None
    try:
        return _naive(datetime.fromisoformat(stamp.replace("Z", "+00:00")))
    except ValueError:
        return None


def _local_stamp(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _name_stamp(name: str) -> datetime | None:
    match = _RESULT_STAMP.match(name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%S")
    except ValueError:
        return None


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def _in(when: datetime | None, since: datetime, until: datetime) -> bool:
    return when is not None and since <= when < until


def _seconds(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _duration(seconds: float) -> str:
    """The one duration formatter of every note: seconds under ~90 s, minutes up to ~90 min, then hours and minutes."""
    if seconds < 90:
        return f"{round(seconds)} s"
    minutes = round(seconds / 60)
    if minutes <= 90:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min"
