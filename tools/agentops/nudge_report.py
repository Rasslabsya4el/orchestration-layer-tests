"""`nudge-report` (issue #311): per brain session, what the context-reset reminder did.

Reads `<root>/var/agentops/brain_nudge.log` (the rows `tools/hooks/brain_reset_nudge.py` writes) and the
session transcripts under `~/.claude/projects/<root, every non-alphanumeric char as '-'>/<session>.jsonl`,
and prints one row per session that got at least one `nudged` row in the window: how many reminders, the
context at the first one, the context at which the brain first names the clear, the context at the end.
Transcripts are streamed line by line, never loaded whole.
"""

from __future__ import annotations

import json
import re
import statistics
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NAMES_CLEAR_RE = re.compile(r"клир|/clear|hand-?off", re.IGNORECASE)
LIMIT_K = 200


def default_projects_dir(root: Path) -> Path:
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(root))


def _k(tokens: int) -> int:
    return int(tokens / 1000 + 0.5)


def _parse_bound(text: str, end: bool) -> datetime:
    """`YYYY-MM-DD` (an `until` covers that whole day) or `YYYY-MM-DDTHH:MM[:SS]`."""
    if "T" in text:
        return datetime.fromisoformat(text)
    day = datetime.fromisoformat(text)
    return day + timedelta(days=1) if end else day


def _usage_total(usage: dict) -> int:
    return ((usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0))


def _local(ts: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone().replace(tzinfo=None) if moment.tzinfo else moment


def _scan_transcript(path: Path, first_reminder: datetime) -> tuple[int | None, int | None]:
    """(context in tokens of the first assistant text naming the clear at/after the reminder, last usage)."""
    names: int | None = None
    end: int | None = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"assistant"' not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "assistant":
                continue
            message = obj.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            total = _usage_total(usage)
            end = total
            if names is not None:
                continue
            when = _local(obj.get("timestamp") or "")
            if when is None or when < first_reminder:
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if (isinstance(block, dict) and block.get("type") == "text"
                        and NAMES_CLEAR_RE.search(block.get("text") or "")):
                    names = total
                    break
    return names, end


def build_rows(root: Path, projects_dir: Path | None = None, since: datetime | None = None,
               until: datetime | None = None) -> list[dict]:
    log = root / "var" / "agentops" / "brain_nudge.log"
    projects = projects_dir if projects_dir is not None else default_projects_dir(root)
    sessions: dict[str, list[dict]] = {}
    if log.exists():
        with open(log, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    row = json.loads(line)
                    when = datetime.fromisoformat(row["ts"])
                except (ValueError, KeyError, TypeError):
                    continue
                if (row.get("action") != "nudged" or row.get("event") != "UserPromptSubmit"
                        or not row.get("session")):
                    continue
                if (since and when < since) or (until and when >= until):
                    continue
                row["_when"] = when
                sessions.setdefault(row["session"], []).append(row)
    out = []
    for session, rows in sessions.items():
        rows.sort(key=lambda r: r["_when"])
        first = rows[0]
        names = end = None
        transcript = projects / f"{session}.jsonl"
        if transcript.exists():
            names, end = _scan_transcript(transcript, first["_when"])
        out.append({"session": session[:8], "reminders": len(rows), "first_k": first.get("context_k"),
                    "names_k": _k(names) if names is not None else None,
                    "end_k": _k(end) if end is not None else None, "when": first["_when"]})
    out.sort(key=lambda r: r["when"])
    return out


def _cell(value: int | None, none: str) -> str:
    return none if value is None else f"{value}k"


def _median(values: list[int]) -> str:
    return f"{statistics.median(values):g}k" if values else "?"


def format_report(rows: list[dict]) -> str:
    if not rows:
        return "no session with a reminder in the window"
    head = ["session", "reminders", "first reminder", "names the clear", "end"]
    body = [[r["session"], str(r["reminders"]), _cell(r["first_k"], "?"), _cell(r["names_k"], "never"),
             _cell(r["end_k"], "?")] for r in rows]
    widths = [max(len(x[i]) for x in [head] + body) for i in range(len(head))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(x, widths)).rstrip() for x in [head] + body]
    firsts = [r["first_k"] for r in rows if r["first_k"] is not None]
    ends = [r["end_k"] for r in rows if r["end_k"] is not None]
    late = sum(1 for r in rows if r["names_k"] is None or r["names_k"] > LIMIT_K)
    lines.append("")
    lines.append(f"median first reminder {_median(firsts)}; median end {_median(ends)}; "
                 f"passed {LIMIT_K}k before naming the hand-off: {late} of {len(rows)}")
    return "\n".join(lines)


def add_subparser(sub) -> None:
    p = sub.add_parser("nudge-report", help="what the context-reset reminder did per brain session (issue #311)")
    p.add_argument("--root", default=str(ROOT), help="the repo whose var/agentops/brain_nudge.log is read")
    p.add_argument("--since", help="YYYY-MM-DD or YYYY-MM-DDTHH:MM")
    p.add_argument("--until", help="YYYY-MM-DD (that whole day) or YYYY-MM-DDTHH:MM (exclusive)")
    p.add_argument("--days", type=int, default=7, help="window length when no --since (default 7)")


def run_command(args, projects_dir: Path | None = None) -> int:
    root = Path(args.root)
    since = _parse_bound(args.since, False) if args.since else datetime.now() - timedelta(days=args.days)
    until = _parse_bound(args.until, True) if args.until else None
    print(format_report(build_rows(root, projects_dir, since, until)))
    return 0
