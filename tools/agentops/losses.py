"""The weekly loss table of the orchestration core: ``python -m tools.agentops losses``.

One table per repo that runs the core, this week against the week before: closed tickets, cost per closed
ticket (mean, median, p90), cost no ticket owns, extra merge attempts by class, tickets with more than one
merge attempt, hours a start was held, hours lost to rate limits, hours parked, hours worked per closed
ticket. The numbers come from the readers ``losses_cost`` and ``losses_ops``; this module reads the
settings, lists the closed tickets through the REST API (one ``gh api`` call per repo) and prints.

Window: an ISO week in the machine's local time zone, Monday 00:00 to the next Monday 00:00. ``bucket_values``
takes any window, so a caller may compare any two spans the same way.

Stdlib plus the core's own modules only.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable

from .core_format import human_duration
from .core_settings import DEFAULTS, ROOT, load as load_settings
from .gh_call import run_gh
from .losses_cost import cost_buckets
from .losses_ops import ops_buckets
from .util import no_window_flags

ISO_WEEK = re.compile(r"^(\d{4})-W(\d{2})$")
#: the owner and name of a GitHub remote, over https or ssh
GITHUB_REMOTE = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")
EVENTS_PATH = "artifacts/runs/events.jsonl"
ORCH_KINDS = ("issue_taken", "orch_end")
NAN = float("nan")
GIT_TIMEOUT_SECONDS = 30.0


class NotReadable(Exception):
    """A repo whose table cannot be read; the reason is printed after the repo's name."""


@dataclass(frozen=True)
class Window:
    """A span of time: ``since`` inclusive, ``until`` exclusive, both tz-aware. ``label`` names an ISO week."""

    since: datetime
    until: datetime
    label: str = ""


@dataclass(frozen=True)
class Repo:
    """One repo of the table: its name in the table, its root folder and its session transcripts folder."""

    name: str
    root: Path
    projects_dir: Path


def _monday(day: date) -> datetime:
    """Midnight at the start of ``day``, in the machine's local time zone."""
    return datetime.combine(day, time()).astimezone()


def window_for(week: str | None, now: datetime) -> tuple[Window, Window]:
    """(this week, last week) as windows. ``week`` is ``YYYY-Www``; None means the ISO week that contains
    ``now``. Raises ValueError for a text that is not an existing ISO week."""
    if week is None:
        year, number, _ = now.astimezone().isocalendar()
    else:
        match = ISO_WEEK.match(week.strip())
        if not match:
            raise ValueError(f"not an ISO week (YYYY-Www): {week!r}")
        year, number = int(match.group(1)), int(match.group(2))
    try:
        monday = date.fromisocalendar(year, number, 1)
    except ValueError as exc:
        raise ValueError(f"no such ISO week: {year}-W{number:02d}") from exc
    this = Window(_monday(monday), _monday(monday + timedelta(days=7)), f"{year}-W{number:02d}")
    before = monday - timedelta(days=7)
    before_year, before_number, _ = before.isocalendar()
    last = Window(_monday(before), _monday(monday), f"{before_year}-W{before_number:02d}")
    return this, last


def _parse_utc(text: object) -> datetime | None:
    """An ISO stamp as an aware moment (a ``Z`` suffix allowed), or None."""
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.astimezone()


def slug_of_remote(url: str) -> str | None:
    """``owner/name`` of a GitHub remote URL (https or ssh form), or None for any other remote."""
    match = GITHUB_REMOTE.search(url.strip())
    return f"{match.group(1)}/{match.group(2)}" if match else None


def parse_pages(text: str) -> list[dict]:
    """The objects of a paginated ``gh api`` output: one JSON array per page, printed one after another.
    Raises ValueError when the text is not JSON."""
    decoder = json.JSONDecoder()
    rows: list[dict] = []
    text = text.strip()
    position = 0
    while position < len(text):
        value, position = decoder.raw_decode(text, position)
        while position < len(text) and text[position].isspace():
            position += 1
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            rows.append(value)
    return rows


def closed_issues_rest(slug: str, window: Window, gh: Callable = run_gh) -> list[dict]:
    """The closed tickets of ``slug`` whose closing time lies in [since, until): rows of
    ``{"number", "closedAt"}``. Pull requests are dropped. ``gh`` runs the one REST call; a failure is
    ``NotReadable``, never a crash."""
    since = window.since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    endpoint = f"repos/{slug}/issues?state=closed&since={since}&per_page=100"
    try:
        result = gh(["api", "--paginate", endpoint])
    except (OSError, subprocess.SubprocessError) as exc:
        raise NotReadable(f"gh api: {exc}") from exc
    if result.returncode != 0:
        first = (result.stderr or "").strip().splitlines()
        raise NotReadable(f"gh api failed: {first[0] if first else 'no message'}")
    try:
        pages = parse_pages(result.stdout or "")
    except ValueError as exc:
        raise NotReadable("gh api printed no JSON") from exc
    rows = []
    for row in pages:
        number = row.get("number")
        closed = row.get("closed_at")
        if "pull_request" in row or not isinstance(number, int):
            continue
        moment = _parse_utc(closed)
        if moment is None or not window.since <= moment < window.until:
            continue
        rows.append({"number": number, "closedAt": closed})
    return rows


def _remote_slug(root: Path) -> str:
    """The GitHub slug of the repo's origin remote, read at run time; NotReadable when there is none."""
    try:
        done = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True,
                              text=True, timeout=GIT_TIMEOUT_SECONDS, creationflags=no_window_flags())
    except (OSError, subprocess.SubprocessError) as exc:
        raise NotReadable(f"git: {exc}") from exc
    if done.returncode != 0:
        raise NotReadable("no origin remote")
    slug = slug_of_remote(done.stdout)
    if slug is None:
        raise NotReadable("origin is not a GitHub remote")
    return slug


def default_closed_lister(repo: Repo, window: Window) -> list[dict]:
    """The closed tickets of a repo's window, listed through the REST API for the repo's own remote."""
    return closed_issues_rest(_remote_slug(repo.root), window)


def _orch_events(root: Path) -> list[dict]:
    """The orchestrator's taking and end events of a repo, each stamp made tz-aware. Other kinds and lines
    with no readable stamp are skipped; a missing file is no events."""
    path = root / EVENTS_PATH
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("kind") not in ORCH_KINDS:
            continue
        moment = _parse_utc(row.get("ts"))
        if moment is None:
            continue
        events.append({**row, "ts": moment.isoformat()})
    return events


def bucket_values(repo: Repo, window: Window, now: datetime, closed_lister: Callable,
                  prices: dict | None = None) -> dict[str, float]:
    """Every bucket of the table for one repo over one window: bucket name -> number, money in USD,
    durations in seconds. ``hours_worked_median_seconds`` is NaN when no closed ticket has a held span.
    ``closed_lister(repo, window)`` gives the closed tickets (rows of ``number`` and ``closedAt``)."""
    closed_rows = closed_lister(repo, window)
    costs = cost_buckets(repo.projects_dir, closed_rows, _orch_events(repo.root), window.since, window.until,
                         prices)
    closed_at = {}
    for row in closed_rows:
        moment = _parse_utc(row.get("closedAt"))
        if isinstance(row.get("number"), int) and moment is not None:
            closed_at[row["number"]] = moment
    ops = ops_buckets(repo.root, window.since, window.until, now, closed_at=closed_at)
    worked = ops.hours_worked_median_seconds
    return {
        "closed": float(costs.closed),
        "spent_usd": costs.spent_usd,
        "unowned_usd": costs.unowned_usd,
        "unowned_open_usd": costs.unowned_by_source["ticket still open"],
        "unowned_brain_usd": costs.unowned_by_source["brain chat"],
        "unowned_unnamed_usd": costs.unowned_by_source["no ticket named"],
        "unowned_share": costs.unowned_share,
        "mean_usd": costs.mean_usd,
        "median_usd": costs.median_usd,
        "p90_usd": costs.p90_usd,
        "tickets_with_extra_attempts": float(ops.tickets_with_extra_attempts),
        "extra_attempts": float(ops.extra_attempts),
        "extra_red_suite": float(ops.extra_red_suite),
        "extra_conflict": float(ops.extra_conflict),
        "extra_other": float(ops.extra_other),
        "held_start_seconds": ops.held_start_seconds,
        "rate_limit_seconds": ops.rate_limit_seconds,
        "parked_seconds": ops.parked_seconds,
        "hours_worked_median_seconds": NAN if worked is None else worked,
    }


# ----------------------------------------------------------------- settings and repos

def this_repo(settings: dict) -> Repo:
    """This repo: its root folder, and the session transcripts folder named by the settings."""
    projects = Path(os.path.expanduser("~/.claude/projects")) / str(settings["claude_projects_dir"])
    return Repo(name=ROOT.name, root=ROOT, projects_dir=projects)


def configured_repos(settings: dict) -> list[Repo]:
    """The repos of ``loss_repos``; with none configured, only this repo."""
    entries = settings.get("loss_repos") or []
    if not entries:
        return [this_repo(settings)]
    return [Repo(name=str(entry["name"]),
                 root=Path(os.path.expanduser(str(entry["root"]))),
                 projects_dir=Path(os.path.expanduser(str(entry["projects_dir"]))))
            for entry in entries]


def _same_path(first: Path, second: Path) -> bool:
    try:
        return Path(first).resolve() == Path(second).resolve()
    except OSError:
        return False


def home_repo(repos: list[Repo], settings: dict) -> Repo:
    """The repo the table shows by default: the entry whose root is this repo, else the one with this repo's
    folder name, else this repo itself."""
    for repo in repos:
        if _same_path(repo.root, ROOT):
            return repo
    for repo in repos:
        if repo.name == ROOT.name:
            return repo
    return this_repo(settings)


# ----------------------------------------------------------------------- the table

@dataclass(frozen=True)
class _Row:
    label: str
    key: str
    kind: str


ROWS = (
    _Row("closed tickets", "closed", "count"),
    _Row("$ spent", "spent_usd", "money"),
    _Row("$ per closed ticket (mean)", "mean_usd", "money"),
    _Row("$ per closed ticket (median)", "median_usd", "money"),
    _Row("$ per closed ticket (p90)", "p90_usd", "money"),
    _Row("$ no ticket owns", "unowned_usd", "unowned"),
    _Row("  of which ticket not closed in the week", "unowned_open_usd", "money"),
    _Row("  of which brain chat", "unowned_brain_usd", "money"),
    _Row("  of which no ticket named", "unowned_unnamed_usd", "money"),
    _Row("extra merge attempts", "extra_attempts", "count"),
    _Row("extra merge attempts, of which red suite", "extra_red_suite", "count"),
    _Row("extra merge attempts, of which conflict", "extra_conflict", "count"),
    _Row("extra merge attempts, of which other", "extra_other", "count"),
    _Row("tickets with more than one merge attempt", "tickets_with_extra_attempts", "count"),
    _Row("hours a start was held", "held_start_seconds", "duration"),
    _Row("hours lost to rate limits", "rate_limit_seconds", "duration"),
    _Row("hours parked", "parked_seconds", "duration"),
    _Row("hours worked per closed ticket (median)", "hours_worked_median_seconds", "duration"),
)


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def _money(value: float) -> str:
    """``$5.3`` below $100, ``$2,757`` from $100 up; never a sign on zero."""
    if not _finite(value):
        return "-"
    if abs(value) < 0.05:
        value = 0.0
    if abs(value) >= 100:
        return f"${value:,.0f}"
    return f"${value:.1f}"


def _cell(row: _Row, values: dict[str, float]) -> str:
    value = values.get(row.key, NAN)
    if row.kind == "count":
        return f"{round(value):,}" if _finite(value) else "-"
    if row.kind == "money":
        return _money(value)
    if row.kind == "duration":
        return human_duration(value)
    share = values.get("unowned_share", NAN)
    if not (_finite(value) and _finite(share)):
        return "-"
    return f"{_money(value)} ({share * 100:.0f}%)"


def _change(this: float, last: float) -> str:
    """This week against last: ``x1.4``; ``new`` when last is 0 and this is not; ``-`` when both are 0."""
    if not (_finite(this) and _finite(last)):
        return "-"
    if this == 0 and last == 0:
        return "-"
    if last == 0:
        return "new"
    return f"x{this / last:.1f}"


def render_block(name: str, this: Window, last: Window, this_values: dict[str, float],
                 last_values: dict[str, float], ceiling: float) -> list[str]:
    """One repo's table as text lines: a heading, then the columns bucket, this week, last week, change.
    The mean cost row is marked `` OVER $<ceiling>`` when this week's value is above the ceiling."""
    table = [["bucket", "this week", "last week", "change"]]
    for row in ROWS:
        label = row.label
        this_mean = this_values.get(row.key, NAN)
        if row.key == "mean_usd" and _finite(this_mean) and this_mean > ceiling:
            label = f"{row.label} OVER {_money(ceiling)}"
        change_key = row.key if row.key != "unowned_usd" else "unowned_usd"
        table.append([label, _cell(row, this_values), _cell(row, last_values),
                      _change(this_values.get(change_key, NAN), last_values.get(change_key, NAN))])
    widths = [max(len(line[column]) for line in table) for column in range(4)]
    lines = [f"{name} {this.label} (last week {last.label})"]
    for line in table:
        lines.append(" | ".join(cell.ljust(widths[column]) for column, cell in enumerate(line)).rstrip())
    return lines


def run_losses(args: argparse.Namespace, *, settings: dict | None = None, now: datetime | None = None,
               closed_lister: Callable | None = None, prices: dict | None = None) -> int:
    """Print the loss table(s) for ``args`` (``--week``, ``--repo`` or ``--all``); returns the exit code.
    The keyword arguments are for tests: the settings file, the clock, the closed-ticket lister, the prices."""
    cfg = load_settings() if settings is None else {**DEFAULTS, **settings}
    moment = now if now is not None else datetime.now().astimezone()
    try:
        this, last = window_for(args.week, moment)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    repos = configured_repos(cfg)
    if args.all:
        chosen = repos
    elif args.repo is not None:
        chosen = [repo for repo in repos if repo.name == args.repo]
        if not chosen:
            print(f"error: no repo named {args.repo!r} in loss_repos")
            return 2
    else:
        chosen = [home_repo(repos, cfg)]
    lister = closed_lister or default_closed_lister
    ceiling = float(cfg["loss_ceiling_usd_per_closed"])

    blocks: list[list[str]] = []
    for repo in chosen:
        try:
            if not repo.root.is_dir():
                raise NotReadable("root folder does not exist")
            this_values = bucket_values(repo, this, moment, lister, prices)
            last_values = bucket_values(repo, last, moment, lister, prices)
        except NotReadable as exc:
            blocks.append([f"{repo.name}: not readable ({exc})"])
            continue
        blocks.append(render_block(repo.name, this, last, this_values, last_values, ceiling))
    print("\n\n".join("\n".join(block) for block in blocks))
    return 0


def add_subparser(sub) -> argparse.ArgumentParser:
    """Register the ``losses`` command on the CLI's subparsers."""
    parser = sub.add_parser("losses", help="the weekly loss table per repo: this week against last week")
    parser.add_argument("--week", help="an ISO week such as 2026-W42 (default: the week that contains now)")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--repo", help="one repo of loss_repos, by name (default: this repo)")
    scope.add_argument("--all", action="store_true", help="every repo of loss_repos")
    return parser
