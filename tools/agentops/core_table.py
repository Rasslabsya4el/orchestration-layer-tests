"""Issue 153: `python -m tools.agentops core table --repos <path> [<path> ...]`, run at the home.

One row per repo (#4 of the issue), read from that repo's own token meter and outcome log, its
`artifacts/runs/events.jsonl` (the dispatcher's own record of what ORCH closed) and its
`config/orchestration.json`. The cost column calls `scripts/token_meter.cost_per_closed_issue`
itself -- never a reimplementation of its arithmetic -- so the number matches the meter's own
report to the cent.

Stdlib only; the token meter (a manifest file, shipped to every target the same way) is imported
by putting `scripts/` on `sys.path`, the way the core's own scripts already do.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median

from . import core_settings
from .core_check import DEFAULT_ROOT as HOME_ROOT
from .core_check import patterns_file_exists, run_check
from .core_format import human_duration
from .core_update import load_settings

_SCRIPTS_DIR = HOME_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import token_meter  # noqa: E402  (issue #153: the home's own scripts/token_meter.py)


@dataclass
class RepoRow:
    repo: str
    issues_closed: int
    closed_no_brain_hand: int
    cost_per_closed_issue: float | None
    label_to_merge_median: float | None
    sent_back: int
    leaks: str
    core_version: str
    leaks_checked: bool = True  # False: the repo folder has no config/core_leak_patterns.json (issue 93)


#: printed under the table for a repo whose folder has no leak-words file (issue 93, row 6)
NOT_CHECKED_LEAKS = "not checked: no leak words"


# -- one column at a time --------------------------------------------------------------------

def _events(root: Path) -> list[dict]:
    path = root / "artifacts" / "runs" / "events.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _closed_issue_numbers(events: list[dict]) -> set[int]:
    return {
        e["issue"] for e in events
        if e.get("kind") == "orch_end" and e.get("outcome") == "closed" and e.get("issue") is not None
    }


def _sent_back(root: Path) -> int:
    """The outcome log's `redone` class (`tools/agentops/outcomes.py`, `OUTCOME_CLASSES`)."""

    path = root / "artifacts" / "runs" / "agentops_outcomes.jsonl"
    if not path.is_file():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("class") == "redone":
            count += 1
    return count


def _parse_gh_timestamp(raw: str | None):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


class GhIssueReader:
    """Reads one issue's timeline (`labeled`/`unlabeled`/`reopened`/`closed` events) and a
    commit's committer date, through `gh api`. `gh_runner` matches
    `scripts/token_meter.GhRunner` (an argv list in, the raw stdout text out), so one runner
    serves both this reader and `cost_per_closed_issue`'s own `fetch_closed_issues` call; tests
    inject a fixture, never the network.

    #323: ``repo`` is the repo whose issue timelines are read (the tickets repo); ``code_repo`` is the code repo
    whose commits are read (default: ``repo``)."""

    def __init__(self, repo: str, gh_runner=None, *, code_repo: str | None = None) -> None:
        self.repo = repo
        self.code_repo = code_repo or repo
        self.runner = gh_runner or token_meter.run_gh

    def timeline(self, issue: int) -> list[dict]:
        out = self.runner(["api", f"repos/{self.repo}/issues/{issue}/timeline", "--paginate"])
        text = out.strip()
        return json.loads(text) if text else []

    def commit_committer_date(self, sha: str) -> str | None:
        out = self.runner(["api", f"repos/{self.code_repo}/commits/{sha}", "--jq", ".commit.committer.date"])
        text = out.strip()
        return text or None


def _label_name(event: dict) -> str | None:
    return (event.get("label") or {}).get("name")


def _has_no_brain_hand(timeline: list[dict]) -> bool:
    """No hand of the brain: no `labeled` `needs-info` event, and the issue was never reopened."""

    for event in timeline:
        kind = event.get("event")
        if kind == "reopened":
            return False
        if kind == "labeled" and _label_name(event) == "needs-info":
            return False
    return True


def _label_to_merge_seconds(timeline: list[dict], reader: GhIssueReader) -> float | None:
    """From the last `labeled` `ready-for-agent` event before the final `closed` event, to that
    `closed` event: its `commit_id`'s committer date when there is one, else the `closed`
    event's own `created_at`."""

    closed_indices = [i for i, e in enumerate(timeline) if e.get("event") == "closed"]
    if not closed_indices:
        return None
    closed_index = closed_indices[-1]
    closed = timeline[closed_index]

    ready_events = [
        e for e in timeline[:closed_index]
        if e.get("event") == "labeled" and _label_name(e) == "ready-for-agent"
    ]
    if not ready_events:
        return None
    ready_time = _parse_gh_timestamp(ready_events[-1].get("created_at"))
    if ready_time is None:
        return None

    closed_time = None
    commit_id = closed.get("commit_id")
    if commit_id:
        closed_time = _parse_gh_timestamp(reader.commit_committer_date(commit_id))
    if closed_time is None:
        closed_time = _parse_gh_timestamp(closed.get("created_at"))
    if closed_time is None:
        return None
    return (closed_time - ready_time).total_seconds()


def _brain_hand_and_merge_columns(
    root: Path, closed_issue_numbers: set[int], *, gh_runner=None,
) -> tuple[int, float | None]:
    """(closed with no hand of the brain, label-to-merge median in seconds), from that repo's own
    GitHub issue timelines (the tickets repo of `config/orchestration.json`, #323); the closing commit's date
    comes from the code repo (`repo` there)."""

    settings = load_settings(root)
    reader = GhIssueReader(core_settings.tickets_repo(root / "config" / "orchestration.json"),
                           gh_runner=gh_runner, code_repo=settings["repo"])

    no_hand = 0
    deltas: list[float] = []
    for number in sorted(closed_issue_numbers):
        timeline = reader.timeline(number)
        if _has_no_brain_hand(timeline):
            no_hand += 1
        delta = _label_to_merge_seconds(timeline, reader)
        if delta is not None:
            deltas.append(delta)
    return no_hand, (median(deltas) if deltas else None)


def _leak_patterns_present(root: Path) -> bool:
    """Whether the repo's own folder holds config/core_leak_patterns.json (issue 93)."""
    return patterns_file_exists(root / "config" / "core_leak_patterns.json")


def _leaks(root: Path) -> str:
    """`core check --root <repo>` with the repo's own leak-words file; the four totals (L1/L2/L3/H).
    Without that file, L1-L3 are not judged: they show '-', never a 0 (issue 93)."""

    rows, _ok = run_check(root=root, patterns_path=root / "config" / "core_leak_patterns.json")
    totals = {"L1": 0, "L2": 0, "L3": 0, "H": 0}
    for row in rows:
        if row.missing:
            continue
        for key in totals:
            totals[key] += row.counts.get(key, 0) if key != "H" else row.h
    if not _leak_patterns_present(root):
        return f"-/-/-/{totals['H']}"
    return f"{totals['L1']}/{totals['L2']}/{totals['L3']}/{totals['H']}"


def cost_per_closed_issue(
    root: Path,
    *,
    turns=None,
    closed_issues: list[dict] | None = None,
    prices: dict | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    gh_runner=None,
) -> float | None:
    """The mean of `token_meter.cost_per_closed_issue`'s own per-issue `cost_usd`, over this
    repo's own transcripts/prices/closed-issue list -- never a reimplementation. Every input may
    be injected (tests); with none given, it reads the repo's own files and GitHub issues."""

    if since is None or until is None:
        since, until = token_meter.default_window()
    if prices is None:
        prices_path = root / "config" / "model_prices.json"
        prices = token_meter.load_prices(prices_path) if prices_path.is_file() else {}
    if closed_issues is None:
        try:
            repo_slug = core_settings.tickets_repo(root / "config" / "orchestration.json")
        except Exception:
            repo_slug = token_meter.DEFAULT_REPO
        runner = gh_runner or token_meter.run_gh
        closed_issues = token_meter.fetch_closed_issues(repo=repo_slug, runner=runner)
    if turns is None:
        settings = load_settings(root)
        projects_dir = Path.home() / ".claude" / "projects" / settings["claude_projects_dir"]
        result = token_meter.scan_projects_dir(projects_dir)
        turns = result.turns

    costs = token_meter.cost_per_closed_issue(turns, closed_issues, prices, since=since, until=until)
    if not costs:
        return None
    return sum(c.cost_usd for c in costs) / len(costs)


# -- the row ------------------------------------------------------------------------------------

def compute_repo_row(
    root: Path,
    *,
    turns=None,
    closed_issues: list[dict] | None = None,
    prices: dict | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    gh_runner=None,
) -> RepoRow:
    root = Path(root)
    events = _events(root)
    settings = load_settings(root)
    closed_numbers = _closed_issue_numbers(events)
    no_hand, merge_median = _brain_hand_and_merge_columns(root, closed_numbers, gh_runner=gh_runner)
    return RepoRow(
        repo=settings.get("repo", str(root)),
        issues_closed=len(closed_numbers),
        closed_no_brain_hand=no_hand,
        cost_per_closed_issue=cost_per_closed_issue(
            root, turns=turns, closed_issues=closed_issues, prices=prices,
            since=since, until=until, gh_runner=gh_runner,
        ),
        label_to_merge_median=merge_median,
        sent_back=_sent_back(root),
        leaks=_leaks(root),
        core_version=settings.get("core_version", "-"),
        leaks_checked=_leak_patterns_present(root),
    )


def format_table(rows: list[RepoRow]) -> str:
    header = (
        f"{'repo':<32}{'closed':>7}{'no brain hand':>14}{'cost/issue':>12}"
        f"{'label->merge':>13}{'sent back':>10}{'leaks':>10}{'core_version':>14}"
    )
    lines = [header]
    for row in rows:
        cost = "-" if row.cost_per_closed_issue is None else f"${row.cost_per_closed_issue:.2f}"
        merge = human_duration(row.label_to_merge_median)
        version = (row.core_version or "-")[:12]
        lines.append(
            f"{row.repo:<32}{row.issues_closed:>7}{row.closed_no_brain_hand:>14}{cost:>12}"
            f"{merge:>13}{row.sent_back:>10}{row.leaks:>10}{version:>14}"
        )
    for row in rows:
        if not row.leaks_checked:
            lines.append(f"{row.repo}: {NOT_CHECKED_LEAKS}")
    return "\n".join(lines)


# -- CLI ------------------------------------------------------------------------------------------

def add_table_subparser(core_sub) -> None:
    table = core_sub.add_parser("table", help="one row per repo, cost to the cent (issue #153)")
    table.add_argument("--repos", nargs="+", required=True, type=Path)


def run_table_command(args) -> int:
    rows = [compute_repo_row(repo) for repo in args.repos]
    print(format_table(rows))
    return 0
