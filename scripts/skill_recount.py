"""Recount for #108: do the subagents that report a skill call it? (repeat of the #102 count)

Read-only, stdlib only. For each repo it reads ``<projects>/<PROJECT_FOLDER>/*/subagents/*.jsonl``
(exactly that folder name, never a prefix match) with the sibling ``.meta.json`` (``agentType``).
Only ``worker`` and ``worker-strong`` units count. Method and limits: the research file
``docs/research/2026-10-17-subagent-skill-calls.md`` at commit 3cbdead (#102). The claim, no-claim,
fence, quote, code-edit and scratch rules are copied from ``tools/hooks/subagent_skill_check.py``.

    python scripts/skill_recount.py --repo home=C--Coding-orchestration-layer@2026-10-20T00:00:00Z
    python scripts/skill_recount.py --repo home=C--Coding-orchestration-layer --repo other=C--Coding-other --json

Options: --since ISO (default 2026-10-17T12:18:01Z), --until ISO (default 2026-10-24T12:18:00Z),
--projects-dir DIR (default ~/.claude/projects; the tests point it at a temp folder).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROLES = ("worker", "worker-strong")
DEFAULT_SINCE = "2026-10-17T12:18:01Z"
DEFAULT_UNTIL = "2026-10-24T12:18:00Z"
TAIL_CHARS = 3000

# as in tools/hooks/subagent_skill_check.py
CODE_SUFFIXES = (".py", ".js", ".ts", ".tsx", ".ps1", ".sh", ".html", ".css", ".json")
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit"})
SKILL_OF_LABEL = {
    "tdd": "tdd",
    "diagnosis": "diagnosing-bugs",
    "writing-for-agents": "writing-for-agents",
    "domain-modeling": "domain-modeling",
    "codebase-design": "codebase-design",
}
LINE_RE = re.compile(
    r"(?im)^\W*(tdd|diagnosis|writing-for-agents|domain-modeling|codebase-design)\s*:[ \t]*(.*)$")
TDD_PRESENT_RE = re.compile(r"(?i)\btdd\W*(?:called\b|skipped\b)")
NO_CLAIM_RE = re.compile(
    r"^(skipped|n/a|na|none|not a defect|no defect|not applicable|not run|not needed"
    r"|not called|not used|not invoked)\b",
    re.IGNORECASE)
LEADING_MARKUP_RE = re.compile(r"^\W+")
FENCES = ("```", "~~~")


@dataclass
class Unit:
    role: str
    first_ts: datetime | None
    skill_calls: set[str]
    edits_code: bool
    final_text: str


@dataclass
class RepoSpec:
    name: str
    folder: str
    start: datetime | None


@dataclass
class Counts:
    units: int = 0
    code_edit_units: int = 0
    false_claims: int = 0
    silent: int = 0
    left_out: int = 0
    claims_by_skill: dict[str, dict[str, int]] = field(default_factory=dict)
    false_by_skill: dict[str, dict[str, int]] = field(default_factory=dict)


def parse_ts(raw: object) -> datetime | None:
    """ISO time as an aware UTC datetime; a naive one is taken as UTC, anything else is None."""

    if not isinstance(raw, str) or not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _iso(ts: datetime | None) -> str | None:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def _normalize_skill(raw: str) -> str:
    return raw.strip().rsplit(":", 1)[-1].strip().lower()


def _is_code_path(path: str) -> bool:
    """A code file, not under a scratchpad, tmp or temp part of its path (the hook's rule)."""

    lowered = path.lower().replace("\\", "/")
    if not lowered.endswith(CODE_SUFFIXES):
        return False
    return not any("scratchpad" in part or part in ("tmp", "temp") for part in lowered.split("/"))


def _report_text(tail: str) -> str:
    """The lines that can be report lines: not inside a fenced code block, not a blockquote line."""

    kept: list[str] = []
    fence: str | None = None
    for line in tail.splitlines():
        stripped = line.lstrip()
        marker = stripped[:3]
        if fence is None and marker in FENCES:
            fence = marker
            continue
        if fence is not None:
            if marker == fence:
                fence = None
            continue
        if stripped.startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept)


def read_unit(path: Path, role: str) -> Unit:
    """Stream one subagent transcript: first timestamp, Skill calls, code edits, final text."""

    first_ts: datetime | None = None
    skill_calls: set[str] = set()
    edits_code = False
    final_id: object = None
    final_parts: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if first_ts is None:
                first_ts = parse_ts(obj.get("timestamp"))
            if obj.get("type") != "assistant":
                continue
            message = obj.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("content"), list):
                continue
            msg_id = message.get("id")
            text_here: list[str] = []
            for block in message["content"]:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    text_here.append(block["text"])
                elif block.get("type") == "tool_use":
                    name = block.get("name")
                    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                    if name == "Skill" and isinstance(tool_input.get("skill"), str):
                        skill_calls.add(_normalize_skill(tool_input["skill"]))
                    elif name in EDIT_TOOLS:
                        path_arg = tool_input.get("file_path")
                        if isinstance(path_arg, str) and _is_code_path(path_arg):
                            edits_code = True
            if text_here:
                if msg_id is None or msg_id != final_id:
                    final_id = msg_id
                    final_parts = []
                final_parts.extend(text_here)
    return Unit(role, first_ts, skill_calls, edits_code, "\n".join(final_parts))


def _judge(unit: Unit) -> tuple[set[str], set[str], bool]:
    """(claimed skills, false claimed skills, silent) from the report lines of the last 3000 characters.

    A label line claims its skill unless its value starts with a no-claim word (skipped, n/a, not a defect ...).
    A skill is counted once per unit, however many lines claim it.
    """

    report = _report_text(unit.final_text[-TAIL_CHARS:])
    claimed: set[str] = set()
    for match in LINE_RE.finditer(report):
        value = LEADING_MARKUP_RE.sub("", match.group(2).strip())
        if NO_CLAIM_RE.match(value):
            continue
        claimed.add(SKILL_OF_LABEL[match.group(1).lower()])
    false = claimed - unit.skill_calls
    silent = unit.edits_code and "tdd" not in unit.skill_calls and not TDD_PRESENT_RE.search(report)
    return claimed, false, silent


def _read_role(jsonl: Path) -> str | None:
    meta_path = jsonl.with_name(jsonl.name[: -len(".jsonl")] + ".meta.json")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    role = meta.get("agentType") if isinstance(meta, dict) else None
    return role if isinstance(role, str) else None


def _bump(table: dict[str, dict[str, int]], skill: str, role: str) -> None:
    per = table.setdefault(skill, {r: 0 for r in ROLES})
    per[role] = per.get(role, 0) + 1


def count_repo(projects: Path, spec: RepoSpec, since: datetime, until: datetime) -> Counts:
    """Counts for one repo. A unit counts when since <= first timestamp < until and not before START."""

    counts = Counts()
    for jsonl in sorted((projects / spec.folder).glob("*/subagents/*.jsonl")):
        role = _read_role(jsonl)
        if role not in ROLES:
            continue
        unit = read_unit(jsonl, role)
        ts = unit.first_ts
        if ts is None or not (since <= ts < until):
            continue
        if spec.start is not None and ts < spec.start:
            counts.left_out += 1
            continue
        counts.units += 1
        if unit.edits_code:
            counts.code_edit_units += 1
        claimed, false, silent = _judge(unit)
        for skill in claimed:
            _bump(counts.claims_by_skill, skill, role)
        for skill in false:
            _bump(counts.false_by_skill, skill, role)
        counts.false_claims += len(false)
        if silent:
            counts.silent += 1
    return counts


def _merge_tables(dst: dict[str, dict[str, int]], src: dict[str, dict[str, int]]) -> None:
    for skill, per in src.items():
        out = dst.setdefault(skill, {r: 0 for r in ROLES})
        for role, n in per.items():
            out[role] = out.get(role, 0) + n


def _merge(a: Counts, b: Counts) -> Counts:
    out = Counts(
        units=a.units + b.units,
        code_edit_units=a.code_edit_units + b.code_edit_units,
        false_claims=a.false_claims + b.false_claims,
        silent=a.silent + b.silent,
        left_out=a.left_out + b.left_out,
    )
    for src in (a, b):
        _merge_tables(out.claims_by_skill, src.claims_by_skill)
        _merge_tables(out.false_by_skill, src.false_by_skill)
    return out


def _silent_pct(c: Counts) -> float | None:
    return round(c.silent / c.code_edit_units * 100, 1) if c.code_edit_units else None


def _counts_dict(c: Counts) -> dict:
    return {
        "units": c.units,
        "code_edit_units": c.code_edit_units,
        "false_claims": c.false_claims,
        "silent": c.silent,
        "silent_pct": _silent_pct(c),
        "left_out": c.left_out,
        "claims_by_skill": c.claims_by_skill,
        "false_claims_by_skill": c.false_by_skill,
    }


def _format(rows: list[list[str]]) -> str:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    out = []
    for row in rows:
        cells = [cell.ljust(w) if i == 0 else cell.rjust(w) for i, (cell, w) in enumerate(zip(row, widths))]
        out.append("  ".join(cells).rstrip())
    return "\n".join(out)


def _table(rows: list[tuple[str, Counts]]) -> str:
    header = ["repo", "units", "code_edit_units", "false_claims", "silent", "silent_pct", "left_out"]
    body = []
    for name, c in rows:
        pct = _silent_pct(c)
        body.append([
            name, str(c.units), str(c.code_edit_units), str(c.false_claims), str(c.silent),
            f"{pct:.1f}%" if pct is not None else "n/a", str(c.left_out),
        ])
    return _format([header, *body])


def _split(name: str, c: Counts) -> str:
    """Per skill and role: false claims of the units that claim the skill, as 'false of claims'."""

    skills = sorted(set(c.claims_by_skill) | set(c.false_by_skill))
    if not skills:
        return f"{name}: no skill claims"
    rows = [["false of claims", *ROLES, "total"]]
    tot_false = {r: 0 for r in ROLES}
    tot_claims = {r: 0 for r in ROLES}
    for skill in skills:
        false = c.false_by_skill.get(skill, {})
        claims = c.claims_by_skill.get(skill, {})
        cells = []
        for r in ROLES:
            tot_false[r] += false.get(r, 0)
            tot_claims[r] += claims.get(r, 0)
            cells.append(f"{false.get(r, 0)} of {claims.get(r, 0)}")
        all_false = sum(false.values())
        all_claims = sum(claims.values())
        rows.append([skill, *cells, f"{all_false} of {all_claims}"])
    rows.append([
        "total",
        *(f"{tot_false[r]} of {tot_claims[r]}" for r in ROLES),
        f"{sum(tot_false.values())} of {sum(tot_claims.values())}",
    ])
    return f"{name}\n" + _format(rows)


def _repo_arg(raw: str) -> RepoSpec:
    name, sep, rest = raw.partition("=")
    if not sep or not name.strip():
        raise argparse.ArgumentTypeError("expected NAME=PROJECT_FOLDER[@START_ISO]")
    folder, at, start_raw = rest.partition("@")
    folder = folder.strip()
    if not folder or "/" in folder or "\\" in folder:
        raise argparse.ArgumentTypeError("PROJECT_FOLDER must be one folder name under the projects dir")
    start = None
    if at:
        start = parse_ts(start_raw.strip())
        if start is None:
            raise argparse.ArgumentTypeError(f"START is not an ISO time: {start_raw!r}")
    return RepoSpec(name.strip(), folder, start)


def _cli_ts(raw: str) -> datetime:
    ts = parse_ts(raw.strip())
    if ts is None:
        raise argparse.ArgumentTypeError(f"not an ISO time: {raw!r}")
    return ts


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recount subagent skill claims (#108).")
    parser.add_argument("--repo", action="append", type=_repo_arg, required=True,
                        help="NAME=PROJECT_FOLDER[@START_ISO]; repeatable")
    parser.add_argument("--since", type=_cli_ts, default=parse_ts(DEFAULT_SINCE),
                        help=f"window start, inclusive, ISO UTC (default {DEFAULT_SINCE})")
    parser.add_argument("--until", type=_cli_ts, default=parse_ts(DEFAULT_UNTIL),
                        help=f"window end, exclusive, ISO UTC (default {DEFAULT_UNTIL})")
    parser.add_argument("--json", action="store_true", help="machine output")
    parser.add_argument("--projects-dir", default=None,
                        help="default ~/.claude/projects; the tests point it at a temp folder")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.since >= args.until:
        parser.error("--since must be before --until")
    projects = Path(args.projects_dir).expanduser() if args.projects_dir else Path.home() / ".claude" / "projects"
    for spec in args.repo:
        if not (projects / spec.folder).is_dir():
            parser.error(f"no project folder {spec.folder!r} under {projects}")

    results = [(spec, count_repo(projects, spec, args.since, args.until)) for spec in args.repo]
    total = Counts()
    for _, c in results:
        total = _merge(total, c)

    if args.json:
        doc = {
            "since": _iso(args.since),
            "until": _iso(args.until),
            "repos": [
                {"repo": spec.name, "folder": spec.folder, "start": _iso(spec.start), **_counts_dict(c)}
                for spec, c in results
            ],
            "total": _counts_dict(total),
        }
        print(json.dumps(doc, indent=2))
        return 0

    print(f"window: since {_iso(args.since)} until {_iso(args.until)} (UTC); "
          f"roles {', '.join(ROLES)}; claims read from the last {TAIL_CHARS} characters of the final message")
    rows = [(spec.name, c) for spec, c in results] + [("TOTAL", total)]
    print(_table(rows))
    print()
    for spec, c in results:
        print(_split(f"{spec.name} ({spec.folder})", c))
        print()
    print(_split("TOTAL", total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
