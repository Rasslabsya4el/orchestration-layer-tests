"""`sweep list`: the rows of the sweep over every open issue, `ready-for-agent` ones listed and never due (#155).

    python -m tools.agentops sweep list

The pure part is `build_rows` and `due_rows`: a function over issue records (the `gh issue list --json` shape:
`number`, `title`, `state`, `labels`, `updatedAt`, `blockedBy`, `subIssuesSummary`) and over the children of each
map (`number`, `state`, `updatedAt`), with the clock injected; no network. `due_rows(issues, children, now)` is the
seam the daily message imports: it returns only the rows that are due, in number order.

Kinds, checked in this order: `map` (label `wayfinder:map`), `parent` (native sub-issues, at least one), `ticket`.
Due rules, one per kind:
  ticket  -- the last update is 2 days old or more;
  parked  -- a date in its `Parked:` line still ahead holds it, whatever its blockers (#187); a date passed, or every
            native blocker closed (its event came), makes it due, its age not asked; a text event with no date, once its
            blockers are closed, falls back to the age rule of `ticket`; with no native blocker at all, and no date,
            the ticket falls back to the age rule of `ticket`;
  parent  -- every sub-issue is closed;
  map     -- no child moved for 2 days (age = the newest child's update; the map's own update when it has none),
             and the row carries its copy line `/wayfinder <number>`.
A parked ticket with an open blocker is listed as waiting on that blocker, every other not-due row as not due.
A `ready-for-agent` row is never due. Each row carries its monitor Progress group (`ticket_groups`), and the
count line is followed by one `<group> <count>` line per group.

The gateway `GhSweep` makes the only reads: one `gh issue list` for the open issues, and one paginated
`gh api .../sub_issues` per map. Standard library plus `gh_call`, `areas` and `ticket_groups` helpers only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable

from tools.agentops import core_settings
from tools.agentops.areas import _parse_pages
from tools.agentops.gh_call import run_gh
from tools.agentops.parked_return import parked_dates, parked_line, parse_parked_line
from tools.agentops.ticket_groups import group_counts, ticket_group

READY = "ready-for-agent"
MAP_LABEL = "wayfinder:map"
PARKED = "parked"
#: The state labels a row shows (the five default roles plus parked); any other label is not shown.
STATE_LABELS = (READY, "needs-triage", "needs-info", PARKED, "ready-for-human", "wontfix")
#: Days of quiet after which a ticket, or a map's newest child, is due.
QUIET_DAYS = 2
#: `gh issue list --json` fields the sweep reads (`parent`, `assignees`, `comments` for the monitor's group, #155;
#: `body` for a parked ticket's `Parked:` line, #187).
ISSUE_FIELDS = "number,title,state,labels,updatedAt,blockedBy,subIssuesSummary,parent,assignees,comments,body"


class SweepError(RuntimeError):
    """A `gh` read failed or answered garbage: one line naming the call."""


@dataclass(frozen=True)
class Row:
    number: int
    title: str
    labels: tuple[str, ...]      # state labels only; empty means no state label
    age_days: int                # whole days since the last update (a map: its newest child's)
    kind: str                    # ticket | parent | map
    progress: str                # "2/3 closed" for a parent or a map with sub-issues, else ""
    due: bool
    waiting_on: tuple[int, ...]  # open blockers of a parked ticket; empty otherwise
    copy: str                    # "/wayfinder <number>" for a map, else ""
    issue_labels: tuple[str, ...] = ()  # every label of the issue (#1109: its news routes to its area's chat)
    group: str = ""              # the monitor's Progress group of the issue (ticket_groups.ticket_group, #155)

    def due_text(self) -> str:
        if self.due:
            return "due"
        if self.waiting_on:
            return "waiting on " + ", ".join(f"#{n}" for n in self.waiting_on)
        return "not due"


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _age_days(updated_at: str, now: datetime) -> int:
    return max(0, int((now - _parse_time(updated_at)).total_seconds() // 86400))


def _labels(issue: dict) -> tuple[str, ...]:
    return tuple(str(lb.get("name", "")) if isinstance(lb, dict) else str(lb) for lb in issue.get("labels") or [])


def _open_blockers(issue: dict) -> tuple[int, ...]:
    nodes = ((issue.get("blockedBy") or {}).get("nodes")) or []
    return tuple(sorted(int(n["number"]) for n in nodes
                        if str(n.get("state") or "").upper() != "CLOSED" and "number" in n))


def _local_naive(now: datetime) -> datetime:
    """`now` as the local naive time the `Parked:` dates are written in (parked_return's `datetime.now`)."""
    return now.astimezone().replace(tzinfo=None) if now.tzinfo else now


def _parked_due(issue: dict, blockers: tuple[int, ...], own_age: int, now: datetime) -> bool:
    """The due rule of a parked ticket (#187), in order: a date in its `Parked:` line still ahead holds it, whatever
    its blockers; an open blocker holds it; a passed date is its event, due; a text event with no date falls back to
    the age rule; otherwise (no line, or a `until #N closes` blocker event) closed native blockers are due, and with
    no native blocker the age rule holds."""
    body = issue.get("body")
    line = parked_line(body)
    dates = parked_dates(line) if line else []
    if dates and max(dates) > _local_naive(now):
        return False
    if blockers:
        return False
    if dates:
        return True
    form = parse_parked_line(body)
    if line is not None and (form is None or form.kind != "issue"):
        return own_age >= QUIET_DAYS
    has_blocker_nodes = bool((issue.get("blockedBy") or {}).get("nodes"))
    return has_blocker_nodes or own_age >= QUIET_DAYS


def _row(issue: dict, children: list[dict], now: datetime, group: str) -> Row:
    """The row of one open issue: its due rule by kind; a `ready-for-agent` issue is never due (#155)."""
    row = _kind_row(issue, children, now)
    return replace(row, group=group, due=row.due and READY not in row.issue_labels)


def _kind_row(issue: dict, children: list[dict], now: datetime) -> Row:
    labels = _labels(issue)
    number = int(issue["number"])
    summary = issue.get("subIssuesSummary") or {}
    total, completed = int(summary.get("total") or 0), int(summary.get("completed") or 0)
    state_labels = tuple(lb for lb in labels if lb in STATE_LABELS)
    own_age = _age_days(issue["updatedAt"], now)
    progress = f"{completed}/{total} closed" if total else ""

    if MAP_LABEL in labels:
        stamps = [c["updatedAt"] for c in children if c.get("updatedAt")]
        age = _age_days(max(stamps), now) if stamps else own_age
        copy = f"/wayfinder {number}"
        return Row(number, issue.get("title") or "", state_labels, age, "map", progress,
                   age >= QUIET_DAYS, (), copy, labels)
    if total:
        return Row(number, issue.get("title") or "", state_labels, own_age, "parent", progress,
                   completed == total, (), "", labels)
    blockers = _open_blockers(issue)
    if PARKED in labels:
        return Row(number, issue.get("title") or "", state_labels, own_age, "ticket", "",
                   _parked_due(issue, blockers, own_age, now), blockers, "", labels)
    return Row(number, issue.get("title") or "", state_labels, own_age, "ticket", "",
               own_age >= QUIET_DAYS, (), "", labels)


def build_rows(issues: list[dict], children: dict[int, list[dict]], now: datetime) -> list[Row]:
    """Every open issue, `ready-for-agent` ones included (listed with their group, never due), one row each,
    in number order (due or not)."""
    by_number = {int(i["number"]): i for i in issues}
    rows = []
    for issue in issues:
        group = ticket_group(issue, by_number)
        if group is None:  # a closed issue: not a row
            continue
        rows.append(_row(issue, children.get(int(issue["number"]), []), now, group))
    return sorted(rows, key=lambda r: r.number)


def due_rows(issues: list[dict], children: dict[int, list[dict]], now: datetime) -> list[Row]:
    """Only the due rows of `build_rows`, in number order. The daily message imports this."""
    return [r for r in build_rows(issues, children, now) if r.due]


def _age_words(days: int) -> str:
    if days == 0:
        return "today"
    return "1 day" if days == 1 else f"{days} days"


def format_rows(rows: list[Row]) -> str:
    """One line per row, columns padded; a map's copy line after its due column; the count line, then one
    `<group> <count>` line per monitor group in the monitor's order (#155)."""
    table = []
    for r in rows:
        table.append([f"#{r.number}", ",".join(r.labels) or "(no label)", r.group, _age_words(r.age_days),
                      r.kind + (f" {r.progress}" if r.progress else ""), r.due_text(), r.copy, r.title])
    widths = [max((len(row[c]) for row in table), default=0) for c in range(7)]
    lines = ["  ".join(cell.ljust(widths[c]) for c, cell in enumerate(row[:7])).rstrip() + "  " + row[7]
             for row in table]
    due = sum(1 for r in rows if r.due)
    lines.append(f"rows {len(rows)}, due {due}")
    lines += [f"{group} {count}" for group, count in group_counts(r.group for r in rows)]
    return "\n".join(lines)


def _default_runner(args: list[str]):
    return run_gh(args)


class GhSweep:
    """The two reads behind the rows; `runner` is injected in tests (`run_gh` by default)."""

    def __init__(self, repo: str, runner: Callable[[list[str]], Any] = _default_runner):
        self.repo, self.runner = repo, runner

    def _run(self, args: list[str]) -> str:
        result = self.runner(args)
        if result.returncode != 0:
            err = (result.stderr or "").strip()
            raise SweepError(f"gh {' '.join(args[:3])} failed: {err[:200]}")
        return result.stdout or ""

    def load(self) -> tuple[list[dict], dict[int, list[dict]]]:
        out = self._run(["issue", "list", "--repo", self.repo, "--state", "open", "--limit", "1000",
                         "--json", ISSUE_FIELDS])
        try:
            issues = json.loads(out)
        except ValueError as exc:
            raise SweepError(f"gh issue list: invalid JSON ({exc})") from None
        children: dict[int, list[dict]] = {}
        for issue in issues:
            if MAP_LABEL not in _labels(issue):
                continue
            number = int(issue["number"])
            kids = _parse_pages(self._run(["api", f"repos/{self.repo}/issues/{number}/sub_issues?per_page=100",
                                           "--paginate"]))
            children[number] = [{"number": int(k["number"]), "state": str(k.get("state") or "").upper(),
                                 "updatedAt": k.get("updated_at")} for k in kids if isinstance(k, dict)]
        return issues, children


def add_subparser(sub) -> None:
    p = sub.add_parser("sweep", help="the sweep: the open tickets outside ready-for-agent")
    ssub = p.add_subparsers(dest="sweep_command", required=True)
    ssub.add_parser("list", help="read-only: one row per open issue outside ready-for-agent, due or waiting")


def run_sweep(args, gh: GhSweep | None = None, now: datetime | None = None) -> int:
    if args.sweep_command != "list":
        return 2
    try:
        gh = gh or GhSweep(core_settings.tickets_repo())  # #323: the sweep reads the issues of the tickets repo
        issues, children = gh.load()
    except (SweepError, RuntimeError) as exc:
        print(f"error: {exc}")
        return 1
    print(format_rows(build_rows(issues, children, now or datetime.now(timezone.utc))))
    return 0
