"""The monitor's Progress-tab group of each open ticket, in Python (issue #155, unit 1).

The monitor draws its lines in `progressLines` (the monitor plugin hooks (kit.tsx), rule at :1199-1201, body at :1202-1224):
each open ticket is counted once, on the first line that fits, in this order: `parked` (the label, checked first: a
parked ticket keeps its role label); its triage label; `autopilot`; `folder` (label wayfinder:map or core-epic, or an
open sub-ticket); `blocked` (an open blocker on it or on an ancestor); `parked-on-run`; else `no label`.

This module is the Python port of that rule, for `sweep list`: the same order and the same first-match rule. The
TypeScript stays the monitor's own; this file imports nothing from the monitor.

An issue is a `gh issue list --json` record: `number`, `state`, `labels`, `parent` (`{number}` or null),
`blockedBy` (`{nodes: [{number, state}]}`), `assignees` (a list) and `comments` (a list of `{body}`).
Stdlib only.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping

PARKED = "parked"
#: the five default triage labels, in the monitor's order (kit.tsx TRIAGE_LABELS)
TRIAGE_LABELS = ("needs-triage", "needs-info", "ready-for-agent", "ready-for-human", "wontfix")
#: every group, in the order the Progress tab draws them (kit.tsx PROGRESS_LINES)
GROUPS = (*TRIAGE_LABELS, PARKED, "autopilot", "folder", "blocked", "parked-on-run", "no label")
#: the groups drawn even with no ticket (kit.tsx progressLines keeps them at 0)
ALWAYS_SHOWN = ("needs-triage", "no label")
#: labels that make an issue a folder (a map or a core epic, not a ticket with a state)
FOLDER_LABELS = ("wayfinder:map", "core-epic")
#: the collector's marker (the monitor collector (collect.py) `_parked_on_run`): a comment whose first line opens with it
PARKED_ON_RUN_MARKER = "<!-- parked-on-run:"
#: how many of the newest comments the marker is looked for in (collect.py `_parked_on_run`)
PARKED_ON_RUN_COMMENTS = 30


def _is_open(issue: Mapping) -> bool:
    return str(issue.get("state") or "").upper() == "OPEN"


def _names(issue: Mapping) -> tuple[str, ...]:
    return tuple(str(lb.get("name", "")) if isinstance(lb, dict) else str(lb) for lb in issue.get("labels") or [])


def _parent_number(issue: Mapping) -> int | None:
    parent = issue.get("parent")
    if isinstance(parent, dict):
        parent = parent.get("number")
    return None if parent is None else int(parent)


def _open_blockers(issue: Mapping) -> tuple[int, ...]:
    nodes = (issue.get("blockedBy") or {}).get("nodes") or []
    return tuple(int(n["number"]) for n in nodes if str(n.get("state") or "").upper() == "OPEN" and "number" in n)


def _blocked_up(issue: Mapping, issues_by_number: Mapping[int, Mapping]) -> bool:
    """An open blocker on the issue or on an ancestor (kit.tsx `blockedUp`: the walk stops at a repeat or a missing parent)."""
    seen: set[int] = set()
    current: Mapping | None = issue
    while current is not None and int(current["number"]) not in seen:
        if _open_blockers(current):
            return True
        seen.add(int(current["number"]))
        parent = _parent_number(current)
        current = issues_by_number.get(parent) if parent is not None else None
    return False


def _has_open_kid(number: int, issues_by_number: Mapping[int, Mapping]) -> bool:
    return any(_parent_number(k) == number and _is_open(k) for k in issues_by_number.values())


def _parked_on_run(issue: Mapping, labels: tuple[str, ...]) -> bool:
    """Open, no ready-for-agent / needs-info, unassigned, and one of the last 30 comments opens with the marker."""
    if {"ready-for-agent", "needs-info"} & set(labels):
        return False
    if issue.get("assignees"):
        return False
    for comment in (issue.get("comments") or [])[-PARKED_ON_RUN_COMMENTS:]:
        first = ((comment.get("body") or "").splitlines()[:1] or [""])[0]
        if first.startswith(PARKED_ON_RUN_MARKER):
            return True
    return False


def ticket_group(issue: Mapping, issues_by_number: Mapping[int, Mapping]) -> str | None:
    """The Progress group of one issue, or None for a closed one (the monitor skips it).

    `issues_by_number` holds the issues the kid and ancestor reads look into, keyed by number; an ancestor or kid
    missing from it is taken as absent.
    """
    if not _is_open(issue):
        return None
    labels = _names(issue)
    if PARKED in labels:
        return PARKED
    triage = next((lb for lb in TRIAGE_LABELS if lb in labels), None)
    if triage is not None:
        return triage
    if "autopilot" in labels:
        return "autopilot"
    if any(lb in labels for lb in FOLDER_LABELS) or _has_open_kid(int(issue["number"]), issues_by_number):
        return "folder"
    if _blocked_up(issue, issues_by_number):
        return "blocked"
    if _parked_on_run(issue, labels):
        return "parked-on-run"
    return "no label"


def group_counts(groups: Iterable[str | None]) -> list[tuple[str, int]]:
    """`[(group, count)]` in GROUPS order. A group with 0 is left out except needs-triage and no label;
    a None (a closed ticket) counts nothing."""
    counts = Counter(g for g in groups if g is not None)
    return [(g, counts.get(g, 0)) for g in GROUPS if counts.get(g, 0) or g in ALWAYS_SHOWN]
