"""Issue 355 (unit 2): `python -m tools.agentops rework --since <date> [--until <date>]`.

How much of the tracker's work is rework. Per window it counts, over the repo's issues:
`closed` (closed in the window), `reopened` (a `reopened` event in the window), and the
rework tickets created in the window, found by TITLE (core-issue titles read
`<repo>: <kind>: <what>`): `broke` = title has `broke:`, `looks off` = title has `looks off:`,
`Fix` = title starts with `Fix:` or `Fix `. A ticket is counted once per kind it matches.
The rate is rework tickets / closed. `caused by: #N` and `caught by: <who>` are read from the
ticket body. Tokens / $ come from `scripts/token_meter.cost_per_closed_issue` over the rework
tickets; when the meter cannot attribute them the line says `not measured (<reason>)`.

gh calls: one `issue list --state all` (titles, bodies, dates) and one paginated
`repos/{repo}/issues/events` call for the `reopened` events (cheaper than a timeline per issue).
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

from . import core_settings
from .core_check import DEFAULT_ROOT as HOME_ROOT
from .core_format import human_duration
from .core_table import token_meter
from .core_update import load_settings

DEFAULT_UNTIL = "2026-10-10"
CAUGHT_BY = ("suite", "orch-diff", "brain-review", "owner", "later-run", "unknown")

_CAUSED_RE = re.compile(r"^[\s>*_\-]*caused[ -]by\b[\s*_:]*#(\d+)", re.IGNORECASE | re.MULTILINE)
_CAUGHT_RE = re.compile(r"^[\s>*_\-]*caught[ -]by\b[\s*_:]*([a-z][a-z-]*)", re.IGNORECASE | re.MULTILINE)


def _dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _in(dt: datetime | None, since: datetime, until: datetime) -> bool:
    return dt is not None and since <= dt < until


def title_kinds(title: str) -> list[str]:
    kinds = []
    low = title.lower()
    if "broke:" in low:
        kinds.append("broke")
    if "looks off:" in low:
        kinds.append("looks off")
    if title.startswith(("Fix:", "Fix ")):
        kinds.append("Fix")
    return kinds


def caused_by(body: str | None) -> int | None:
    m = _CAUSED_RE.search(body or "")
    return int(m.group(1)) if m else None


def caught_by(body: str | None) -> str:
    m = _CAUGHT_RE.search(body or "")
    value = m.group(1).lower() if m else "unknown"
    return value if value in CAUGHT_BY else "unknown"


def fetch_issues(repo: str, runner) -> list[dict]:
    raw = runner(["issue", "list", "--repo", repo, "--state", "all", "--json",
                  "number,title,body,createdAt,closedAt,state", "--limit", "5000"])
    return json.loads(raw) if raw.strip() else []


def fetch_reopened(repo: str, runner) -> list[tuple[int, datetime]]:
    raw = runner(["api", f"repos/{repo}/issues/events?per_page=100", "--paginate", "--jq",
                  '.[] | select(.event=="reopened") | {n: .issue.number, at: .created_at}'])
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if line:
            row = json.loads(line)
            out.append((row["n"], _dt(row["at"])))
    return out


def measure_tokens(rework: list[dict], since: datetime, until: datetime, *, root: Path,
                   turns=None, prices=None) -> str:
    """The `tokens: ...` line. Never a made-up number."""
    closed = [{"number": r["number"], "closedAt": r.get("closedAt")} for r in rework if r.get("closedAt")]
    if not closed:
        return "tokens: not measured (no closed rework ticket)"
    try:
        if prices is None:
            path = root / "config" / "model_prices.json"
            prices = token_meter.load_prices(path) if path.is_file() else {}
        if turns is None:
            settings = load_settings(root)
            projects = Path.home() / ".claude" / "projects" / settings["claude_projects_dir"]
            turns = token_meter.scan_projects_dir(projects).turns
        costs = token_meter.cost_per_closed_issue(turns, closed, prices, since=since, until=until)
    except Exception as exc:  # the meter's own inputs missing is a reason, not a crash
        return f"tokens: not measured ({type(exc).__name__}: {exc})"
    if not costs:
        return "tokens: not measured (the meter attributes no turn to these tickets)"
    tokens = sum(sum(c.tokens.values()) for c in costs)
    usd = sum(c.cost_usd for c in costs)
    unpriced = sum(c.unpriced_turns for c in costs)
    note = f", {unpriced} unpriced turns" if unpriced else ""
    return f"tokens: {tokens:,} / ${usd:.2f} over {len(costs)} of {len(closed)} closed rework tickets{note}"


def compute(issues: list[dict], reopened: list[tuple[int, datetime]], since: datetime, until: datetime) -> dict:
    closed = [i for i in issues if _in(_dt(i.get("closedAt")), since, until)]
    reopened_n = sorted({n for n, at in reopened if _in(at, since, until)})
    kinds: dict[str, list[dict]] = {"broke": [], "looks off": [], "Fix": []}
    rework: dict[int, dict] = {}
    for i in issues:
        if not _in(_dt(i.get("createdAt")), since, until):
            continue
        for k in title_kinds(i.get("title", "")):
            kinds[k].append(i)
            rework[i["number"]] = i
    tickets = sorted(rework.values(), key=lambda i: i["number"])
    split = {k: 0 for k in CAUGHT_BY}
    known = 0
    for t in tickets:
        split[caught_by(t.get("body"))] += 1
        if caused_by(t.get("body")) is not None:
            known += 1
    return {"closed": len(closed), "reopened": reopened_n, "kinds": kinds, "tickets": tickets,
            "known": known, "split": split}


def format_report(res: dict, since: datetime, until: datetime, tokens_line: str) -> str:
    n_closed = res["closed"]
    n_rework = len(res["tickets"])
    rate = f"{100 * n_rework / n_closed:.1f} %" if n_closed else "n/a (nothing closed)"

    def nums(rows):
        return ", ".join(f"#{r['number']}" for r in rows) or "-"

    span = human_duration((until - since).total_seconds())
    reopened = ", ".join(f"#{n}" for n in res["reopened"]) or "-"
    lines = [
        f"rework window {since.isoformat(timespec='minutes')} .. {until.isoformat(timespec='minutes')} ({span})",
        f"closed: {n_closed}",
        f"reopened: {len(res['reopened'])} ({reopened})",
        f"broke: {len(res['kinds']['broke'])} ({nums(res['kinds']['broke'])})",
        f"looks off: {len(res['kinds']['looks off'])} ({nums(res['kinds']['looks off'])})",
        f"Fix: {len(res['kinds']['Fix'])} ({nums(res['kinds']['Fix'])})",
        f"rework tickets: {n_rework}; rate: {rate} of closed",
        f"caused-by known: {res['known']} of {n_rework}",
        "caught by: " + ", ".join(f"{k} {v}" for k, v in res["split"].items()),
        tokens_line,
    ]
    return "\n".join(lines)


def run_rework(since_raw: str, until_raw: str | None = None, *, repo: str | None = None, runner=None,
               root: Path = HOME_ROOT, turns=None, prices=None) -> str:
    runner = runner or token_meter.run_gh
    repo = repo or core_settings.tickets_repo()
    since = token_meter.parse_date_arg(since_raw)
    until = token_meter.parse_date_arg(until_raw or DEFAULT_UNTIL)
    res = compute(fetch_issues(repo, runner), fetch_reopened(repo, runner), since, until)
    tokens_line = measure_tokens(res["tickets"], since, until, root=root, turns=turns, prices=prices)
    return format_report(res, since, until, tokens_line)


def add_rework_subparser(sub) -> None:
    p = sub.add_parser("rework", help="how much of the tracker's work is rework (issue #355)")
    p.add_argument("--since", required=True, help="a date (2026-09-26) or ISO timestamp")
    p.add_argument("--until", default=DEFAULT_UNTIL, help=f"end of the window (default {DEFAULT_UNTIL}, #70's)")


def run_rework_command(args: argparse.Namespace) -> int:
    print(run_rework(args.since, args.until))
    return 0
