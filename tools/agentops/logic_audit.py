"""The `logic-audit` command (issue #191): the four parts of the weekly "project logic" step, one table each.

    python -m tools.agentops logic-audit --since YYYY-MM-DD [--until YYYY-MM-DD|YYYY-MM-DDTHH:MM] [--root PATH]

Read-only, writes no file. Each part runs in its own try/except: a part that raises prints one row
`part N failed: <error>` and the others still run. Parts: 1 baselines, 2 exception lists, 3 until conditions,
4 guards and repairs (14 days ending --until). Rows print as a plain pipe table
`kind | finding | place A | place B | count | proposed verdict`, a missing record source as a `no source` row.
Stdlib only.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import date, datetime, time
from pathlib import Path

from tools.agentops.logic_audit_baselines import baseline_rows
from tools.agentops.logic_audit_exceptions import exception_rows
from tools.agentops.logic_audit_guards import guard_rows
from tools.agentops.logic_audit_rows import NO_SOURCE, Row
from tools.agentops.logic_audit_until import until_rows

#: The repo root: this file lives at tools/agentops/logic_audit.py. `--root` picks another one.
REPO_ROOT = Path(__file__).resolve().parents[2]
#: The guard part's window in days, ending at --until.
GUARD_DAYS = 14
#: The owner's table header; every part prints under it.
TABLE_HEADER = ("kind", "finding", "place A", "place B", "count", "proposed verdict")
#: The kind of the row a failed part prints.
FAILED = "failed"


def add_subparser(sub: argparse._SubParsersAction) -> None:
    audit = sub.add_parser("logic-audit", help="the four parts of the project logic step (read-only)")
    audit.add_argument("--since", required=True, help="the first day of the window, YYYY-MM-DD")
    audit.add_argument("--until", default=None,
                       help="the end of the window, YYYY-MM-DD or YYYY-MM-DDTHH:MM (default: now)")
    audit.add_argument("--root", default=str(REPO_ROOT), help="the repo to audit (default: this repo)")


def _parse_since(text: str) -> date:
    return datetime.strptime(text, "%Y-%m-%d").date()


def _parse_until(text: str) -> date | datetime:
    if "T" in text:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M")
    return datetime.strptime(text, "%Y-%m-%d").date()


def _as_datetime(value: date | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.combine(value, time())


def _sections(root: Path, since: date, until: date | datetime | None) -> list[tuple[int, str, Callable[[], list[Row]]]]:
    """The four parts in order: number, heading name, and a thunk that returns the part's rows."""
    until_at = None if until is None else _as_datetime(until)
    return [
        (1, "baselines", lambda: baseline_rows(root, since, until)),
        (2, "exception lists", lambda: exception_rows(root, since, until)),
        (3, "until conditions", lambda: until_rows(root, now=until_at)),
        (4, "guards and repairs", lambda: guard_rows(root, until=until_at, days=GUARD_DAYS)),
    ]


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _table(rows: list[Row]) -> list[str]:
    lines = [
        "| " + " | ".join(TABLE_HEADER) + " |",
        "|" + "---|" * len(TABLE_HEADER),
    ]
    for row in rows:
        cells = (row.kind, row.finding, row.place_a, row.place_b, row.count, row.verdict)
        lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    return lines


def run_logic_audit_command(args: argparse.Namespace) -> int:
    try:
        since = _parse_since(args.since)
    except ValueError:
        print(f"error: --since takes a date YYYY-MM-DD, got {args.since!r}")
        return 2
    until = None
    if args.until is not None:
        try:
            until = _parse_until(args.until)
        except ValueError:
            print(f"error: --until takes YYYY-MM-DD or YYYY-MM-DDTHH:MM, got {args.until!r}")
            return 2

    root = Path(args.root)
    out: list[str] = []
    total = 0
    no_source = 0
    for number, name, part in _sections(root, since, until):
        try:
            rows = part()
        except Exception as exc:  # one part's failure costs its own rows only
            rows = [Row(kind=FAILED, finding=f"part {number} failed: {exc}", count=0)]
        total += len(rows)
        no_source += sum(1 for row in rows if row.kind == NO_SOURCE)
        out.append(f"## {number} {name}")
        out.extend(_table(rows))
        out.append("")
    out.append(f"rows: {total} (no source: {no_source})")
    print("\n".join(out))
    return 0
