"""Pure functions behind the weekly loss table of the orchestration core.

Each function takes what its caller has already read (the outcome log's event dicts, the merge result
dicts, the dispatch log's lines) and returns numbers or plain records. None of them reads a file, a
network or the clock: a moment the function needs is a parameter. Stdlib only; formatting of
durations belongs to the caller (core_format.human_duration).
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

#: the ``[issues #N #M]`` suffix a dispatch line may carry, naming the issues it holds back
ISSUE_SUFFIX = re.compile(r"\[issues((?: #\d+)+)\]")
#: the local stamp at the head of a dispatch line
LINE_TIME = re.compile(r"^\s*(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
#: a merge branch named after its issue: ``o<issue>`` followed by anything
BRANCH_ISSUE = re.compile(r"^(?:o|orch-)(\d+)")

LANDED_REASON = "pushed"
CONFLICT_REASON = "conflict"
#: merge reasons that mean the suite was red (on the branch, or already on the base it merges into)
RED_SUITE_REASONS = frozenset({"red-suite", "red-on-branch", "red-on-base"})


@dataclass(frozen=True)
class Span:
    """One hold of an issue by one orchestrator session: from its last taking to its end.

    ``end`` and ``outcome`` are None for a span still open; ``session`` is None when no end was seen.
    """

    issue: int
    session: str | None
    start: datetime
    end: datetime | None
    outcome: str | None


@dataclass(frozen=True)
class LogSpan:
    """A run of matching dispatch lines. ``end`` is the last matching line plus one tick, so
    ``end - start`` is the span's seconds; ``issues`` is the sorted union of the lines' suffixes."""

    start: datetime
    end: datetime
    issues: tuple[int, ...]


@dataclass
class MergeCounts:
    """Merge attempts of one ticket: every merge result, and the ones that did not land by reason."""

    attempts: int = 0
    red_suite: int = 0
    conflict: int = 0
    other: int = 0


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def held_spans(events: list[dict]) -> list[Span]:
    """The holds an orchestrator session made, from the outcome log's events.

    A span runs from the latest ``issue_taken`` of an issue to the next ``orch_end`` of that issue
    (its session and outcome come from that ``orch_end``). An ``orch_end`` without a taking before it
    (or with no issue, as ``nothing_claimed``) makes no span. A taking with no ``orch_end`` after it
    is an open span, listed after the closed ones.
    """
    # only the two kinds read here are parsed: other kinds carry naive or no stamps in the real log
    timed = []
    for event in events:
        stamp = event.get("ts")
        if stamp and event.get("kind") in ("issue_taken", "orch_end"):
            timed.append((_parse(stamp), event))
    timed.sort(key=lambda pair: pair[0])

    open_since: dict[int, datetime] = {}
    spans: list[Span] = []
    for when, event in timed:
        kind = event.get("kind")
        issue = event.get("issue")
        if not isinstance(issue, int):
            continue
        if kind == "issue_taken":
            open_since[issue] = when
        elif kind == "orch_end":
            start = open_since.pop(issue, None)
            if start is not None:
                spans.append(Span(issue=issue, session=event.get("session"), start=start, end=when,
                                  outcome=event.get("outcome")))
    spans.extend(Span(issue=issue, session=None, start=start, end=None, outcome=None)
                 for issue, start in open_since.items())
    return spans


def held_weights(spans_seconds: dict[int, float]) -> dict[int, float]:
    """Each issue's share of the seconds held. A total of 0 gives every issue an equal share."""
    if not spans_seconds:
        return {}
    total = sum(spans_seconds.values())
    if total <= 0:
        share = 1.0 / len(spans_seconds)
        return {issue: share for issue in spans_seconds}
    return {issue: seconds / total for issue, seconds in spans_seconds.items()}


def session_issue_weights(spans: list[Span]) -> dict[str, dict[int, float]]:
    """Per session, the held_weights of its issues' seconds. Only closed spans with a session count;
    an issue held twice in one session sums both spans."""
    seconds: dict[str, dict[int, float]] = {}
    for span in spans:
        if span.end is None or span.session is None:
            continue
        per_issue = seconds.setdefault(span.session, {})
        per_issue[span.issue] = per_issue.get(span.issue, 0.0) + (span.end - span.start).total_seconds()
    return {session: held_weights(per_issue) for session, per_issue in seconds.items()}


def parked_seconds(spans: list[Span], events: list[dict], now: datetime) -> dict[int, float]:
    """Per issue, the seconds its parked spans have waited: from each parked span's end to the next
    ``issue_taken`` of the same issue after that end, or to ``now`` when there is none yet."""
    takings: dict[int, list[datetime]] = {}
    for event in events:
        issue = event.get("issue")
        if event.get("kind") == "issue_taken" and isinstance(issue, int) and event.get("ts"):
            takings.setdefault(issue, []).append(_parse(event["ts"]))
    for times in takings.values():
        times.sort()

    totals: dict[int, float] = {}
    for span in spans:
        if span.outcome != "parked" or span.end is None:
            continue
        times = takings.get(span.issue, [])
        index = bisect.bisect_right(times, span.end)
        until = times[index] if index < len(times) else now
        waited = max((until - span.end).total_seconds(), 0.0)
        totals[span.issue] = totals.get(span.issue, 0.0) + waited
    return totals


def _merge_branch_issue(argv: object) -> int | None:
    """The issue a merge's branch names: the token after ``merge`` (``origin/`` dropped), ``o<N>...``."""
    if not isinstance(argv, list) or "merge" not in argv:
        return None
    index = argv.index("merge")
    if index + 1 >= len(argv) or not isinstance(argv[index + 1], str):
        return None
    branch = argv[index + 1]
    if branch.startswith("origin/"):
        branch = branch[len("origin/"):]
    match = BRANCH_ISSUE.match(branch)
    return int(match.group(1)) if match else None


def merge_attempts(results: list[dict]) -> dict[int | None, MergeCounts]:
    """Merge attempts per ticket, from the merge results (``kind`` ``merge``; other kinds are skipped).

    A result counts once for each issue of its ``issues`` list; with an empty list it counts for the
    issue its branch names, else for None (no ticket owns it). Every result is one attempt; the not
    landed ones (any reason but ``pushed``) are classed as red suite, conflict or other by ``reason``.
    The extra attempts of a ticket are its attempts minus one, left to the caller.
    """
    counts: dict[int | None, MergeCounts] = {}
    for result in results:
        if result.get("kind") != "merge":
            continue
        reason = result.get("reason") or ""
        issues = sorted({issue for issue in (result.get("issues") or []) if isinstance(issue, int)})
        owners: list[int | None] = list(issues) if issues else [_merge_branch_issue(result.get("argv"))]
        for owner in owners:
            row = counts.setdefault(owner, MergeCounts())
            row.attempts += 1
            if reason == LANDED_REASON:
                continue
            if reason in RED_SUITE_REASONS:
                row.red_suite += 1
            elif reason == CONFLICT_REASON:
                row.conflict += 1
            else:
                row.other += 1
    return counts


def log_spans(lines: list[str], needle: str, gap_s: float = 120.0, tick_s: float = 30.0) -> list[LogSpan]:
    """Spans of the dispatch log's lines that contain ``needle`` (case-insensitive).

    Consecutive matching lines whose gap is at most ``gap_s`` seconds form one span. A line without a
    local stamp is skipped. Each span's issues are the union of the ``[issues #N #M]`` suffixes of its
    own lines.
    """
    wanted = needle.lower()
    hits: list[tuple[datetime, str]] = []
    for line in lines:
        match = LINE_TIME.match(line)
        if not match or wanted not in line.lower():
            continue
        hits.append((datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S"), line))

    spans: list[LogSpan] = []
    group: list[tuple[datetime, str]] = []
    for when, line in hits:
        if group and (when - group[-1][0]).total_seconds() > gap_s:
            spans.append(_close_group(group, tick_s))
            group = []
        group.append((when, line))
    if group:
        spans.append(_close_group(group, tick_s))
    return spans


def _close_group(group: list[tuple[datetime, str]], tick_s: float) -> LogSpan:
    issues: set[int] = set()
    for _, line in group:
        for suffix in ISSUE_SUFFIX.finditer(line):
            issues.update(int(number) for number in re.findall(r"#(\d+)", suffix.group(1)))
    return LogSpan(start=group[0][0], end=group[-1][0] + timedelta(seconds=tick_s),
                   issues=tuple(sorted(issues)))


def doubled(this_week: dict[str, float], last_week: dict[str, float]) -> list[str]:
    """The bucket names whose this-week value is at least twice their last-week value, sorted.

    A bucket with nothing last week (absent or 0) is never doubled: there is no ratio to take.
    """
    names = []
    for name, value in this_week.items():
        before = last_week.get(name, 0.0)
        if before > 0 and value >= 2 * before:
            names.append(name)
    return sorted(names)
