"""The rule-file counter: how many lines of written agent rules the repo holds, and how many of them one issue
added or removed.

    python -m tools.agentops rules [--root PATH]
    python -m tools.agentops rules --issues N,M,... [--root PATH]

A rule file is one of ``RULE_PATTERNS`` (CLAUDE.md, CODING_STANDARDS.md, the agent docs, the role files, the skill
files). With no flag the command prints the file count and the line total at HEAD. With ``--issues`` it prints one row
per issue, ``#N +added/-removed``, summed over the rule-file lines of every commit on ``origin/main`` (``main`` when
there is no origin) whose message names ``#N`` and no digit after it; a commit's other files are not counted; the last
row is ``sum``. Stdlib and git only.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
from pathlib import Path

from tools.agentops.util import no_window_flags

DEFAULT_ROOT = Path(__file__).resolve().parents[2]

RULE_PATTERNS = (
    "CLAUDE.md",
    "CODING_STANDARDS.md",
    "docs/agents/*.md",
    "docs/agents/**/*.md",
    ".claude/agents/*.md",
    ".claude/skills/**/*.md",
)


class RulesError(RuntimeError):
    """A git call failed or the base branch is missing; the command prints it and exits 2."""


def is_rule_file(path: str) -> bool:
    """fnmatch with ``*`` crossing ``/``, the same reading git gives its pathspecs."""
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in RULE_PATTERNS)


def _git(root: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          creationflags=no_window_flags())
    if proc.returncode:
        raise RulesError(f"git {' '.join(args[:2])} failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout


def _line_count(data: bytes) -> int:
    """wc -l, plus one for a last line without its newline."""
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def count_rule_files(root: Path) -> tuple[int, int]:
    """(files, lines) of the rule files at HEAD."""
    files = lines = 0
    for path in _git(root, "ls-tree", "-r", "--name-only", "HEAD").decode("utf-8", "replace").splitlines():
        if not is_rule_file(path):
            continue
        files += 1
        lines += _line_count(_git(root, "show", f"HEAD:{path}"))
    return files, lines


def base_ref(root: Path) -> str:
    """``origin/main`` when it exists, else ``main``."""
    for ref in ("origin/main", "main"):
        probe = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"],
                               capture_output=True, creationflags=no_window_flags())
        if probe.returncode == 0:
            return ref
    raise RulesError("neither origin/main nor main exists")


def issue_rule_lines(root: Path, issues: list[int]) -> dict[int, tuple[int, int]]:
    """{issue: (added, removed)} over the rule-file lines of the base branch's commits naming each issue."""
    patterns = {issue: re.compile(rf"#{issue}(?!\d)") for issue in issues}
    totals = {issue: [0, 0] for issue in issues}
    log = _git(root, "-c", "core.quotepath=off", "log", base_ref(root), "--no-renames", "--numstat",
               "--format=%x1e%B%x1f").decode("utf-8", "replace")
    for chunk in log.split("\x1e")[1:]:
        message, _, numstat = chunk.partition("\x1f")
        named = [issue for issue, pattern in patterns.items() if pattern.search(message)]
        if not named:
            continue
        added = removed = 0
        for line in numstat.splitlines():
            fields = line.split("\t")
            if len(fields) != 3 or not is_rule_file(fields[2]):
                continue
            added += int(fields[0]) if fields[0].isdigit() else 0
            removed += int(fields[1]) if fields[1].isdigit() else 0
        for issue in named:
            totals[issue][0] += added
            totals[issue][1] += removed
    return {issue: (a, r) for issue, (a, r) in totals.items()}


def parse_issues(raw: str) -> list[int]:
    issues: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise RulesError(f"--issues takes comma-separated numbers, got {part!r}")
        if int(part) not in issues:
            issues.append(int(part))
    if not issues:
        raise RulesError("--issues names no issue")
    return issues


def add_subparser(sub: argparse._SubParsersAction) -> None:
    rules = sub.add_parser("rules", help="rule files' lines at HEAD, or the rule lines each issue changed")
    rules.add_argument("--issues", help="comma-separated issue numbers: one row each, rule-file lines only")
    rules.add_argument("--root", default=str(DEFAULT_ROOT), help="the repo to count (default: this installation)")


def run_rules_command(args: argparse.Namespace) -> int:
    root = Path(args.root)
    try:
        if args.issues is None:
            files, lines = count_rule_files(root)
            print(f"{files} files, {lines:,} lines")
            return 0
        issues = parse_issues(args.issues)
        rows = issue_rule_lines(root, issues)
    except RulesError as exc:
        print(f"error: {exc}")
        return 2
    for issue in issues:
        added, removed = rows[issue]
        print(f"#{issue} +{added}/-{removed}")
    print(f"sum +{sum(a for a, _ in rows.values())}/-{sum(r for _, r in rows.values())}")
    return 0
