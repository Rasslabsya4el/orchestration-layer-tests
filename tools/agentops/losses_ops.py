"""The operations buckets of the weekly loss table: counts and seconds read from the outcome log,
the merge results and the dispatch log, each windowed to [since, until).

One public function, ``ops_buckets(root, since, until, now)``. It reads three files under ``root``:
the outcome log (one JSON object per line), the merge results (one JSON file each, in the results
folder) and the dispatch log (text). A missing file counts as nothing; a malformed line or file is
skipped. The pure rules (spans, attribution, merge counting) are in ``losses_calc``; this module only
reads, windows and adds up. Stdlib only.

Time: the outcome log carries zoned stamps, the merge results and the dispatch log carry naive local
stamps. Every comparison is made in naive local time, so ``since``, ``until`` and ``now`` may be zoned
(converted to local) or naive (already local).

Extras rule: a ticket's extra merge attempts are its attempts minus one (``extra_attempts``). Its class
counts count every not-landed attempt by class, the first attempt included (a failed attempt is what
caused the retry), but only for a ticket with at least two attempts, and capped so that its three
classes add up to at most its extra attempts: a ticket that never landed has more not-landed attempts
than extras, and the last ones are dropped first. Attempts with no ticket owner are counted only in
``unattributed_attempts``.

Parked time: a park runs from its ``orch_end`` with outcome ``parked`` to the earliest of: the next
``issue_taken`` of the same issue, the next ``orch_end`` of that issue with outcome ``closed``, the issue's
REST closing time (``closed_at``, when given), ``until`` and ``now``; each park counts only the seconds inside
[since, until).
"""

from __future__ import annotations

import bisect
import json
import re
import statistics
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tools.agentops.losses_calc import (
    LINE_TIME,
    LogSpan,
    Span,
    held_spans,
    log_spans,
    merge_attempts,
)

EVENTS_PATH = "artifacts/runs/events.jsonl"
RESULTS_DIR = "var/agentops/results"
DISPATCH_PATH = "artifacts/runs/dispatch.out"
HELD_NEEDLE = "start held"
RATE_LIMIT_NEEDLE = "rate limit"
ORCH_KINDS = ("issue_taken", "orch_end")
LANDED = "landed"
RED = "red"
CONFLICT = "conflict"
OTHER = "other"
#: a result file's name starts with its stamp, ``YYYYMMDDTHHMMSS``
RESULT_STAMP = re.compile(r"^(\d{8}T\d{6})")


@dataclass(frozen=True)
class OpsBuckets:
    tickets_with_extra_attempts: int
    extra_attempts: int
    extra_red_suite: int
    extra_conflict: int
    extra_other: int
    unattributed_attempts: int
    held_start_seconds: float
    rate_limit_seconds: float
    held_start_by_issue: dict[int, float]
    rate_limit_by_issue: dict[int, float]
    parked_seconds: float
    hours_worked_median_seconds: float | None
    no_ticket_events_note: str


def _local_naive(moment: datetime) -> datetime:
    """A moment as naive local time (a naive input is already local)."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone().replace(tzinfo=None)


def _aware(moment: datetime) -> datetime:
    """A moment as tz-aware (a naive input is local time)."""
    return moment.astimezone() if moment.tzinfo is None else moment


def _in_window(when: datetime, since: datetime, until: datetime) -> bool:
    return since <= when < until


def _read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _timed_orch_events(path: Path) -> list[dict]:
    """The orchestrator's taking and end events, with each stamp made tz-aware. Other kinds, and
    lines that are not a JSON object or carry no readable stamp, are skipped."""
    events = []
    for line in _read_lines(path):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("kind") not in ORCH_KINDS:
            continue
        stamp = row.get("ts")
        if not isinstance(stamp, str):
            continue
        try:
            when = _aware(datetime.fromisoformat(stamp.replace("Z", "+00:00")))
        except ValueError:
            continue
        events.append({**row, "ts": when.isoformat()})
    return events


def _result_stamp(row: dict, name: str) -> datetime | None:
    """A merge result's moment, as naive local time: ``ended``, else ``started``, else the file name."""
    for key in ("ended", "started"):
        value = row.get(key)
        if isinstance(value, str):
            try:
                return _local_naive(datetime.fromisoformat(value.replace("Z", "+00:00")))
            except ValueError:
                pass
    match = RESULT_STAMP.match(name)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y%m%dT%H%M%S")
        except ValueError:
            return None
    return None


def _results(root: Path) -> list[tuple[datetime, dict]]:
    """Every readable merge result with its moment, in file-name order (a stable base for the sort)."""
    folder = root / RESULTS_DIR
    if not folder.is_dir():
        return []
    timed = []
    for path in sorted(folder.glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(row, dict):
            continue
        when = _result_stamp(row, path.name)
        if when is not None:
            timed.append((when, row))
    return timed


def _dispatch_lines(path: Path) -> list[str]:
    """The dispatch log's lines whose leading stamp is a real local time (a bad date is skipped)."""
    kept = []
    for line in _read_lines(path):
        match = LINE_TIME.match(line)
        if not match:
            continue
        try:
            datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        kept.append(line)
    return kept


def _clipped_seconds(span: LogSpan, since: datetime, until: datetime) -> float:
    start = max(span.start, since)
    end = min(span.end, until)
    return max((end - start).total_seconds(), 0.0)


def _log_total(spans: list[LogSpan], since: datetime, until: datetime) -> tuple[float, dict[int, float]]:
    """The clipped seconds of the spans, and each named issue's equal share of its spans' seconds.
    A span without issues stays in the total only."""
    total = 0.0
    by_issue: dict[int, float] = {}
    for span in spans:
        seconds = _clipped_seconds(span, since, until)
        total += seconds
        if seconds > 0 and span.issues:
            share = seconds / len(span.issues)
            for issue in span.issues:
                by_issue[issue] = by_issue.get(issue, 0.0) + share
    return total, by_issue


def _result_classes(row: dict) -> dict[int | None, str]:
    """Each owner of one merge result, with the result's class: red, conflict, other or landed."""
    classes: dict[int | None, str] = {}
    for owner, one in merge_attempts([row]).items():
        if one.red_suite:
            classes[owner] = RED
        elif one.conflict:
            classes[owner] = CONFLICT
        elif one.other:
            classes[owner] = OTHER
        else:
            classes[owner] = LANDED
    return classes


def _merge_extras(rows: list[dict]) -> tuple[int, int, int, int, int, int]:
    """(tickets with extra attempts, extra attempts, extra red suite, extra conflict, extra other,
    unattributed attempts) over the merge results of the window, in time order (``rows`` sorted).
    The class counts follow the module's extras rule: the not-landed attempts in time order, cut
    after the ticket's extra attempts (attempts minus one)."""
    sequences: dict[int | None, list[str]] = {}
    for row in rows:
        for owner, cls in _result_classes(row).items():
            sequences.setdefault(owner, []).append(cls)

    tickets = extra = red = conflict = other = 0
    for owner, sequence in sequences.items():
        if owner is None:
            continue
        extras = len(sequence) - 1
        if extras > 0:
            tickets += 1
            extra += extras
        kept = [cls for cls in sequence if cls != LANDED][:max(extras, 0)]
        red += kept.count(RED)
        conflict += kept.count(CONFLICT)
        other += kept.count(OTHER)
    unattributed = len(sequences.get(None, []))
    return tickets, extra, red, conflict, other, unattributed


def _next_after(times: list[datetime], when: datetime) -> datetime | None:
    index = bisect.bisect_right(times, when)
    return times[index] if index < len(times) else None


def _parked_in_window(spans: list[Span], events: list[dict], since: datetime, until: datetime,
                      now: datetime, closed_at: dict[int, datetime]) -> float:
    """Seconds parked inside [since, until): each park from its end to the earliest of the same
    issue's next taking, its next closed end, the issue's REST closing time, ``until`` and ``now``."""
    takings: dict[int, list[datetime]] = {}
    closes: dict[int, list[datetime]] = {}
    for event in events:
        issue = event.get("issue")
        if not isinstance(issue, int):
            continue
        when = datetime.fromisoformat(event["ts"])
        if event.get("kind") == "issue_taken":
            takings.setdefault(issue, []).append(when)
        elif event.get("kind") == "orch_end" and event.get("outcome") == "closed":
            closes.setdefault(issue, []).append(when)
    for times in (*takings.values(), *closes.values()):
        times.sort()

    total = 0.0
    for span in spans:
        if span.outcome != "parked" or span.end is None:
            continue
        candidates = [until, now]
        for times in (takings.get(span.issue, []), closes.get(span.issue, [])):
            following = _next_after(times, span.end)
            if following is not None:
                candidates.append(following)
        rest_close = closed_at.get(span.issue)
        if rest_close is not None and rest_close > span.end:
            candidates.append(rest_close)
        stop = min(candidates)
        inside_start = max(span.end, since)
        inside_end = min(stop, until)
        total += max((inside_end - inside_start).total_seconds(), 0.0)
    return total


def ops_buckets(root: Path, since: datetime, until: datetime, now: datetime,
                closed_at: dict[int, datetime] | None = None) -> OpsBuckets:
    """The operations buckets of ``root`` over [since, until). ``now`` closes a park that has no
    resume yet; it may be zoned or naive (local). ``closed_at`` maps an issue to its closing time as
    the REST listing gives it: a park of that issue ends there too."""
    since_local = _local_naive(since)
    until_local = _local_naive(until)
    since_aware = _aware(since)
    until_aware = _aware(until)
    now_aware = _aware(now)
    rest_closed = {issue: _aware(moment) for issue, moment in (closed_at or {}).items()}

    # merges, by the moment of each result
    results = [(when, row) for when, row in _results(root) if _in_window(when, since_local, until_local)]
    results.sort(key=lambda pair: pair[0])
    tickets, extra, red, conflict, other, unattributed = _merge_extras([row for _, row in results])

    # dispatch log: start held and rate limit
    lines = _dispatch_lines(root / DISPATCH_PATH)
    held_total, held_by = _log_total(log_spans(lines, HELD_NEEDLE), since_local, until_local)
    rate_total, rate_by = _log_total(log_spans(lines, RATE_LIMIT_NEEDLE), since_local, until_local)

    # outcome log: parked seconds inside the window, and hours worked per closed ticket
    events = _timed_orch_events(root / EVENTS_PATH)
    spans: list[Span] = held_spans(events)
    parked_total = _parked_in_window(spans, events, since_aware, until_aware, now_aware, rest_closed)

    closed = {
        int(event["issue"])
        for event in events
        if event.get("kind") == "orch_end" and event.get("outcome") == "closed"
        and isinstance(event.get("issue"), int)
        and _in_window(datetime.fromisoformat(event["ts"]), since_aware, until_aware)
    }
    life: dict[int, float] = {}
    for span in spans:
        if span.end is not None:
            life[span.issue] = life.get(span.issue, 0.0) + (span.end - span.start).total_seconds()
    worked = [life[issue] for issue in closed if issue in life]
    median = float(statistics.median(worked)) if worked else None

    return OpsBuckets(
        tickets_with_extra_attempts=tickets,
        extra_attempts=extra,
        extra_red_suite=red,
        extra_conflict=conflict,
        extra_other=other,
        unattributed_attempts=unattributed,
        held_start_seconds=held_total,
        rate_limit_seconds=rate_total,
        held_start_by_issue=held_by,
        rate_limit_by_issue=rate_by,
        parked_seconds=parked_total,
        hours_worked_median_seconds=median,
        no_ticket_events_note="",
    )
