"""Part 4 of `logic-audit` (issue #191): the guards and the automatic repairs, 14 days. Stdlib only, read-only.

Sources, read under `root` (a missing file is skipped; when none exists the part has no source):
- `var/agentops/permission_guard.log` (jsonl ts, session, rule, text): guard `permission: <rule>`, blocking.
- `var/agentops/worker_git_guard.log` (jsonl ts, agent_id, command): `worker git guard`, blocking.
- `var/logs/tracker_guard.jsonl` (jsonl time, session, kind, command): `tracker guard: <kind>`, warning.
- `var/agentops/merge.log` (`YYYY-MM-DD HH:MM:SS | branch | rest`): `ejected: <reason>` is a fire of
  `merge eject: <first word of reason>`, blocking; `N failed after re-check of M` with M >= 1 is a fire of the
  repair `merge suite re-check`.
- `artifacts/runs/events.jsonl` (jsonl ts, kind, session): kind `permission_refused` is a fire of
  `dispatch permission_refused`, blocking.

Stopped time of a fire: only a blocking guard stops work. For a blocking fire it is the seconds to the next record of
the same session (the agent_id, or the branch in merge.log) within 30 min, else 0; a merge eject stops until the next
later `pushed` line of its branch, capped at 6 h, else 0. A warning's stop time is 0 (it does not stop work), and a
repair's is 0 too (it runs inside the merge).

False, per fire (checked first): (i) a merge eject whose reason names `<path>:<line> <word>` while `<word>` is
still on that line of the file (the same text went through unchanged); (ii) `rule_changed(guard, fire time)`: a
commit to the guard's source files since the fire has a subject saying it stopped refusing.

Real, per fire, without reading any transcript: a merge eject (any reason) whose branch is later `pushed` in
merge.log, where a `batch of N: a, b | pushed` line pushes each member a and b; any other permission, worker,
tracker or dispatch fire whose session's next fire of the same guard within 30 min has a different text (the call
was changed and went on). Everything else is unknown. A repair has no real / false / unknown count.

The default `rule_changed` runs one `git log` per guard over its source files (commit dates are then compared
with each fire time in Python, which is what `--since` would do per fire, without one git call per fire). When that
git call fails, or the permission guard's `RULES` cannot be read, the rule is read as not changed and every stat
carries the flag `rule check unavailable`. A known guard whose log is missing is listed with 0 fires while another
log exists; with no log at all the part is `no source`.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .core_format import human_duration
from .logic_audit_rows import Row, no_source_row
from .util import no_window_flags

PERMISSION_LOG = "var/agentops/permission_guard.log"
WORKER_LOG = "var/agentops/worker_git_guard.log"
TRACKER_LOG = "var/logs/tracker_guard.jsonl"
MERGE_LOG = "var/agentops/merge.log"
EVENTS_LOG = "artifacts/runs/events.jsonl"
PERMISSION_SOURCE = "tools/hooks/permission_guard.py"
TRACKER_SOURCE = "tools/hooks/tracker_guard.py"
MERGE_SOURCES = ("tools/agentops/target_check.py", "tools/agentops/merge.py")
#: the known guards that are not permission rules, with their kinds (the defaults of `known_guards`)
_FIXED_KNOWN_GUARDS = {
    "tracker guard: issue": "warning",
    "worker git guard": "blocking",
    "merge eject: target-check": "blocking",
    "merge eject: red-suite": "blocking",
    "merge suite re-check": "repair",
    "dispatch permission_refused": "blocking",
}
#: the flag every stat carries when the default rule check could not run (git failed, or RULES unreadable)
RULE_CHECK_FLAG = "rule check unavailable"

SESSION_WINDOW = timedelta(minutes=30)
PUSH_CAP = timedelta(hours=6)
WARNING_FALSE_SHARE = 0.10
REPAIR_DAYS = 5

_REPAIR = re.compile(r"(\d+) failed after re-check of (\d+)")
_BATCH = re.compile(r"^batch of \d+: (.*)$")
_REASON_REF = re.compile(r"([\w./\\-]+):(\d+) (\S+)")
_FALSE_SUBJECT = re.compile(r"(?i)false (fire|alarm|positive)|no longer (refus|block)|let through|stop refusing")


@dataclass(frozen=True)
class GuardStat:
    """One guard or repair over the window. For a guard, real + false + unknown == fires; a repair has 0 of each."""

    guard: str
    kind: str
    source: str
    fires: int
    real: int
    false: int
    unknown: int
    stopped_s: float
    days_fired: int
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Fire:
    guard: str
    kind: str
    source: str
    when: datetime
    key: str | None
    text: str | None
    index: int
    reason: str | None = None  # a merge eject's reason text


@dataclass(frozen=True)
class _Push:
    branch: str
    when: datetime
    index: int


@dataclass
class _Parsed:
    fires: list[_Fire]
    pushes: list[_Push]


def _members(branch: str) -> list[str]:
    """The branches a merge.log branch field names: `batch of N: a, b` names a and b; anything else names itself."""

    match = _BATCH.match(branch)
    if match is None:
        return [branch]
    return [name.strip() for name in match.group(1).split(",") if name.strip()]


def _when(value) -> datetime | None:
    """A timestamp as a naive local datetime (an aware one is converted to local time)."""

    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if when.tzinfo is not None:
        when = when.astimezone().replace(tzinfo=None)
    return when


def _lines(path: Path) -> list[str] | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


def _jsonl(root: Path, rel: str) -> list[dict] | None:
    lines = _lines(root / rel)
    if lines is None:
        return None
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _json_parsed(records: list[dict], source: str, until: datetime, *, guard_of, kind: str,
                 when_key: str, key_key: str) -> _Parsed:
    fires = []
    for index, record in enumerate(records):
        when = _when(record.get(when_key))
        guard = guard_of(record)
        if when is None or guard is None or when > until:
            continue
        key = record.get(key_key)
        text = next((record[name] for name in ("text", "command", "call") if isinstance(record.get(name), str)), None)
        fires.append(_Fire(guard, kind, source, when, str(key) if key else None, text, index))
    return _Parsed(fires, [])


def _merge_parsed(lines: list[str], until: datetime) -> _Parsed:
    fires, pushes = [], []
    for index, line in enumerate(lines):
        parts = line.split(" | ", 2)
        if len(parts) < 3:
            continue
        when = _when(parts[0])
        if when is None or when > until:
            continue
        branch, rest = parts[1], parts[2]
        if rest.startswith("ejected:"):
            reason = rest[len("ejected:"):].strip()
            words = reason.split()
            if words:
                fires.append(_Fire(f"merge eject: {words[0]}", "blocking", MERGE_LOG, when, branch, None, index,
                                   reason=reason))
        if rest.startswith("pushed"):
            pushes.extend(_Push(member, when, index) for member in _members(branch))
        repair = _REPAIR.search(rest)
        if repair and int(repair.group(2)) >= 1:
            fires.append(_Fire("merge suite re-check", "repair", MERGE_LOG, when, None, None, index))
    return _Parsed(fires, pushes)


def _load(root: Path, until: datetime) -> list[_Parsed]:
    loaded = []
    permission = _jsonl(root, PERMISSION_LOG)
    if permission is not None:
        loaded.append(_json_parsed(permission, PERMISSION_LOG, until, guard_of=_permission_guard,
                                   kind="blocking", when_key="ts", key_key="session"))
    worker = _jsonl(root, WORKER_LOG)
    if worker is not None:
        loaded.append(_json_parsed(worker, WORKER_LOG, until, guard_of=lambda r: "worker git guard",
                                   kind="blocking", when_key="ts", key_key="agent_id"))
    tracker = _jsonl(root, TRACKER_LOG)
    if tracker is not None:
        loaded.append(_json_parsed(tracker, TRACKER_LOG, until, guard_of=_tracker_guard,
                                   kind="warning", when_key="time", key_key="session"))
    merge = _lines(root / MERGE_LOG)
    if merge is not None:
        loaded.append(_merge_parsed(merge, until))
    events = _jsonl(root, EVENTS_LOG)
    if events is not None:
        loaded.append(_json_parsed(events, EVENTS_LOG, until, guard_of=_event_guard,
                                   kind="blocking", when_key="ts", key_key="session"))
    return loaded


def _permission_guard(record: dict) -> str | None:
    rule = record.get("rule")
    return f"permission: {rule}" if isinstance(rule, str) and rule else None


def _tracker_guard(record: dict) -> str | None:
    kind = record.get("kind")
    return f"tracker guard: {kind}" if isinstance(kind, str) and kind else None


def _event_guard(record: dict) -> str | None:
    return "dispatch permission_refused" if record.get("kind") == "permission_refused" else None


def _next_fire(fires: list[_Fire], position: int, *, same_guard: bool) -> _Fire | None:
    """The next fire of the same session (and of the same guard when `same_guard`) within 30 min."""

    fire = fires[position]
    if fire.key is None:
        return None
    limit = fire.when + SESSION_WINDOW
    for other in fires[position + 1:]:
        if other.when > limit:
            break
        if other.key != fire.key or (same_guard and other.guard != fire.guard):
            continue
        return other
    return None


def _is_false(fire: _Fire, file_text: Callable[[str], str | None],
              rule_changed: Callable[[str, datetime], bool]) -> bool:
    if fire.reason is not None:
        ref = _REASON_REF.search(fire.reason)
        if ref:
            text = file_text(ref.group(1))
            if text is not None:
                lines = text.splitlines()
                number = int(ref.group(2))
                if 1 <= number <= len(lines) and ref.group(3) in lines[number - 1]:
                    return True
    return rule_changed(fire.guard, fire.when)


def _score(fire: _Fire, position: int, parsed: _Parsed, file_text, rule_changed) -> tuple[float, str]:
    """``(stopped seconds, verdict)``. Only a blocking guard stops work: a warning's stop is 0 here, a repair's is 0 in
    `_judge`. Verdict is ``real`` | ``false`` | ``unknown`` | ``none`` (a repair)."""

    stopped, verdict = _judge(fire, position, parsed, file_text, rule_changed)
    return (0.0 if fire.kind == "warning" else stopped), verdict


def _judge(fire: _Fire, position: int, parsed: _Parsed, file_text, rule_changed) -> tuple[float, str]:
    """The stop time and the verdict of one fire, before the warning rule of `_score`."""

    if fire.kind == "repair":
        return 0.0, "none"
    if fire.reason is not None:
        push = next((p for p in parsed.pushes if p.branch == fire.key and p.index > fire.index), None)
        stopped = 0.0 if push is None else min(max((push.when - fire.when).total_seconds(), 0.0),
                                               PUSH_CAP.total_seconds())
        if _is_false(fire, file_text, rule_changed):
            return stopped, "false"
        if push is not None:
            return stopped, "real"
        return stopped, "unknown"
    nxt = _next_fire(parsed.fires, position, same_guard=False)
    stopped = (nxt.when - fire.when).total_seconds() if nxt is not None else 0.0
    if _is_false(fire, file_text, rule_changed):
        return stopped, "false"
    nxt_same = _next_fire(parsed.fires, position, same_guard=True)
    if nxt_same is not None and nxt_same.text is not None and fire.text is not None and nxt_same.text != fire.text:
        return stopped, "real"
    return stopped, "unknown"


def _source_of(guard: str) -> str:
    """The log a guard's fires come from; "" for a name this part does not know (listed without a source)."""

    if guard.startswith("permission: "):
        return PERMISSION_LOG
    if guard == "worker git guard":
        return WORKER_LOG
    if guard.startswith("tracker guard: "):
        return TRACKER_LOG
    if guard.startswith("merge eject: ") or guard == "merge suite re-check":
        return MERGE_LOG
    if guard.startswith("dispatch "):
        return EVENTS_LOG
    return ""


def _permission_rule_names(root: Path) -> list[str]:
    """The red-line short names of `tools/hooks/permission_guard.py` (its plain `RULES` dict); [] when the file is
    absent. A file that exists but whose `RULES` is not a plain dict of names raises RuntimeError."""

    path = root / PERMISSION_SOURCE
    if not path.exists():
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError) as exc:
        raise RuntimeError(f"{PERMISSION_SOURCE} unreadable: {exc}") from exc
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "RULES" for t in node.targets):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError) as exc:
                raise RuntimeError(f"{PERMISSION_SOURCE} RULES is not a plain dict: {exc}") from exc
            if isinstance(value, dict) and all(isinstance(name, str) for name in value.values()):
                return list(value.values())
            raise RuntimeError(f"{PERMISSION_SOURCE} RULES is not a dict of names")
    return []


def _default_known_guards(root: Path) -> dict[str, str]:
    known = {f"permission: {name}": "blocking" for name in _permission_rule_names(root)}
    known.update(_FIXED_KNOWN_GUARDS)
    return known


def _default_file_text(root: Path) -> Callable[[str], str | None]:
    def read(path: str) -> str | None:
        try:
            return (root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    return read


def _guard_sources(root: Path, guard: str) -> list[str]:
    if guard.startswith("permission: "):
        return [PERMISSION_SOURCE]
    if guard == "worker git guard":
        return sorted(p.relative_to(root).as_posix() for p in (root / "tools" / "hooks").glob("*worker_git*")
                      if p.is_file())
    if guard.startswith("merge eject: "):
        return list(MERGE_SOURCES)
    if guard.startswith("tracker guard: "):
        return [TRACKER_SOURCE]
    return []


def _commits(root: Path, paths: list[str]) -> list[tuple[datetime, str]]:
    """``(commit time, subject)`` for the commits that touched `paths` (the existing ones); [] when none exists.
    A git failure raises RuntimeError."""

    existing = [path for path in paths if (root / path).exists()]
    if not existing:
        return []
    try:
        done = subprocess.run(["git", "log", "--format=%cI\t%s", "--", *existing], cwd=root, capture_output=True,
                              text=True, timeout=60, creationflags=no_window_flags())
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"git log failed in {root}: {exc}") from exc
    if done.returncode != 0:
        raise RuntimeError(f"git log failed in {root} (rc {done.returncode}): {done.stderr.strip()}")
    commits = []
    for line in done.stdout.splitlines():
        stamp, _, subject = line.partition("\t")
        when = _when(stamp)
        if when is not None:
            commits.append((when, subject))
    return commits


class _GitRuleCheck:
    """The default `rule_changed`: one `git log` per guard over its source files, cached per guard. A git failure
    is kept in `failure`; the rule then reads as not changed, and `guard_stats` flags every stat."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.cache: dict[str, list[tuple[datetime, str]]] = {}
        self.failure: str | None = None

    def __call__(self, guard: str, when: datetime) -> bool:
        if guard not in self.cache:
            try:
                self.cache[guard] = _commits(self.root, _guard_sources(self.root, guard))
            except RuntimeError as exc:
                self.failure = str(exc)
                self.cache[guard] = []
        return any(commit >= when and _FALSE_SUBJECT.search(subject) for commit, subject in self.cache[guard])


def _flags(kind: str, fires: int, false: int, days_fired: int) -> tuple[str, ...]:
    flags = []
    if kind == "blocking" and false > 0:
        flags.append("blocking: false fire")
    if kind == "warning" and fires and false / fires > WARNING_FALSE_SHARE:
        flags.append("warning: false share > 10%")
    if kind == "repair" and days_fired >= REPAIR_DAYS:
        flags.append("repair fired on 5+ days")
    return tuple(flags)


def _stats(root: Path, until: datetime, days: int, file_text, rule_changed, known_guards) -> tuple[bool, list[GuardStat]]:
    """``(any source exists, stats)``: the rows need the first to tell "no source" from "no fire"."""

    loaded = _load(root, until)
    if not loaded:
        return False, []
    unavailable = False
    checker = None
    if rule_changed is None:
        checker = _GitRuleCheck(root)
        rule_changed = checker
    file_text = file_text or _default_file_text(root)
    if known_guards is not None:
        known = known_guards
    else:
        try:
            known = _default_known_guards(root)
        except RuntimeError:
            unavailable = True
            known = dict(_FIXED_KNOWN_GUARDS)
    start = until - timedelta(days=days)

    scored: dict[str, list[tuple[_Fire, float, str]]] = {}
    for parsed in loaded:
        for position, fire in enumerate(parsed.fires):
            if fire.when < start:
                continue
            stopped, verdict = _score(fire, position, parsed, file_text, rule_changed)
            scored.setdefault(fire.guard, []).append((fire, stopped, verdict))

    stats = []
    for guard, items in scored.items():
        fires = [fire for fire, _, _ in items]
        false = sum(1 for _, _, verdict in items if verdict == "false")
        days_fired = len({fire.when.date() for fire in fires})
        stats.append(GuardStat(
            guard=guard, kind=fires[0].kind, source=fires[0].source, fires=len(fires),
            real=sum(1 for _, _, v in items if v == "real"), false=false,
            unknown=sum(1 for _, _, v in items if v == "unknown"),
            stopped_s=sum(stopped for _, stopped, _ in items), days_fired=days_fired,
            flags=_flags(fires[0].kind, len(fires), false, days_fired)))

    present = {stat.guard for stat in stats}
    for guard, kind in known.items():
        if guard in present:
            continue
        flags = (f"0 fires in {days} days (listed, not removed)",)
        stats.append(GuardStat(guard=guard, kind=kind, source=_source_of(guard), fires=0, real=0, false=0,
                               unknown=0, stopped_s=0.0, days_fired=0, flags=flags))
    if checker is not None and checker.failure is not None:
        unavailable = True
    if unavailable:
        stats = [replace(stat, flags=stat.flags + (RULE_CHECK_FLAG,)) for stat in stats]
    return True, stats


def guard_stats(root: Path, until: datetime | None = None, days: int = 14, *,
                file_text: Callable[[str], str | None] | None = None,
                rule_changed: Callable[[str, datetime], bool] | None = None,
                known_guards: dict[str, str] | None = None) -> list[GuardStat]:
    """Every guard and repair fired in ``[until - days, until]`` (``until`` None = now), plus the listed known
    guards with 0 fires. A repo with none of the logs gives []."""

    _, stats = _stats(Path(root), datetime.now() if until is None else until, days,
                      file_text, rule_changed, known_guards)
    return stats


def guard_rows(root: Path, until: datetime | None = None, days: int = 14, *,
               file_text: Callable[[str], str | None] | None = None,
               rule_changed: Callable[[str, datetime], bool] | None = None,
               known_guards: dict[str, str] | None = None) -> list[Row]:
    """One row per GuardStat: flagged first, then by fires descending. No log at all: one `no source` row."""

    root = Path(root)
    has_source, stats = _stats(root, datetime.now() if until is None else until, days,
                               file_text, rule_changed, known_guards)
    if not has_source:
        return [no_source_row("guards", "none of the guard, merge or dispatch logs exists under the repo root")]
    rows = []
    for stat in sorted(stats, key=lambda s: (not s.flags, -s.fires)):
        finding = (f"{stat.guard}: {stat.fires} fires (real {stat.real}, false {stat.false}, unknown {stat.unknown}), "
                   f"stopped work {human_duration(stat.stopped_s)}, {stat.kind}")
        if stat.flags:
            finding += "; " + "; ".join(stat.flags)
        rows.append(Row(kind="repair" if stat.kind == "repair" else "guard", finding=finding,
                        place_a=stat.source, place_b="", count=stat.fires,
                        verdict="review" if stat.flags else "ok"))
    return rows
