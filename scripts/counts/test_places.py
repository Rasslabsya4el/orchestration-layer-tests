"""#352 unit 5: where a window's pytest sessions went, GitHub or the PC: the counts of #352's rows 3, 4 and 8.

Usage: python -I scripts/counts/test_places.py [--since YYYY-MM-DD HH:MM] [--hours 24]

Read-only; a missing file counts 0. Reads:
- <repo>/var/agentops/merge.log: a merge's suite line names its place. `suite on GitHub` is a suite sent to GitHub, with
  its real time (`suite N min` / `suite N s`), its selection (`selected N`) and `windows part` when it ran one.
  `suite on the PC (<reason>)` is a PC suite whose reason is the parenthesised text: a GitHub fallback when the reason
  starts `GitHub ` (any case), is `partial run, ids unreadable`, or contains `test copy refused`; any other reason, and
  a plain `suite on the PC`, is a PC suite for another reason.
- machine_dir()/pytest_runs.jsonl: the PC runs (real time = end - start). A merge's PC suite is among them (worktree
  `merge-suite`), and its merge.log line is the same run seen from the merge: the two are shown apart, never added.
- machine_dir()/heavy_runs.jsonl: the take lines of the PC line (not kind github). A take's `waited_min` is the wait of
  its run; the run's take is the one on the same repo within 120 s of the run's start (the take-start gap), else the
  run's wait is 0.
- <repo>/config/orchestration.json: github_suite, github_start_seconds_linux and _windows,
  place_pc_only_floor_seconds and github_speed_ratio (place_decision's defaults where unset).

Rows (#352 acceptance, expected 0 after the change):
- row 3: a PC run of 2 min or more (not a merge-suite run) that place_decision's arithmetic would have sent to GitHub:
  GitHub suite on, the run >= max(floor, start), and start + run x ratio < wait + run (the wait as above).
- row 4: a suite sent to GitHub shorter than max(floor, start); the start is the windows start where the suite ran a
  windows part, the linux start otherwise. Reads merge suites only: heavy_runs GitHub take lines carry no duration.
- row 8: a whole-suite GitHub fallback in the window, while GitHub suite is on.
Listed, not counted: the merge-suite PC runs of 2 min or more (row 3's arithmetic on them), and each GitHub suite's real
time with its expected PC time (its selection x the window's per-test median of PC runs; n/a without a selection).

Human text is minutes and seconds (principle 7); the rule's settings are seconds.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.agentops import core_settings, place_decision, pytest_runs_report  # noqa: E402
from tools.agentops.machine_mode import machine_dir  # noqa: E402

STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
LONG_RUN_SECONDS = 120
TAKE_MATCH_SECONDS = 120
MERGE_SUITE_WORKTREE = "merge-suite"   # suite_worktree.SUITE_NAME
MERGE_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| (.*)$")
SUITE_TIME = re.compile(r"\| suite ([\d.]+) (min|s)\b")
SELECTED = re.compile(r"\| selected (\d+)\b")
PC_REASON = re.compile(r"suite on the PC \((.*?)\)(?= \||$)")
GITHUB_REASON_PREFIX = "GitHub "
PARTIAL_UNREADABLE = "partial run, ids unreadable"
TEST_COPY_REFUSED = "test copy refused"
GITHUB_PLACE = "suite on GitHub"
PC_PLACE = "suite on the PC"
WINDOWS_PART = "windows part"
human = place_decision.human


@dataclass(frozen=True)
class Suite:
    when: datetime
    branch: str
    place: str             # "github" or "pc"
    fallback: bool         # a PC suite of a merge that was meant for GitHub: a GitHub fallback
    reason: str            # the PC suite's reason in its parentheses ("GitHub " dropped), "" when it has none
    seconds: float | None  # the suite's real time, None when the line has none
    selected: int | None   # the tests of `selected N`, None for a full suite or a line without it
    windows: bool          # the GitHub suite ran a windows part


@dataclass(frozen=True)
class PcRun:
    start: datetime
    seconds: float
    repo: str
    label: str             # the ticket, else the worktree
    selected: int          # the tests the run selected (0 when none)
    merge_suite: bool      # the merge-suite worktree: a merge's own PC suite


@dataclass(frozen=True)
class Take:
    when: datetime
    repo: str
    waited: float | None   # seconds the run waited for its place, None when the take has no waited_min


@dataclass(frozen=True)
class Rule:
    github_suite: bool
    start: float           # github_start_seconds_linux
    start_windows: float   # github_start_seconds_windows
    floor: float           # place_pc_only_floor_seconds
    ratio: float           # github_speed_ratio


@dataclass(frozen=True)
class Expected:
    run: PcRun
    wait: float            # seconds in the PC line before the run (0 when unknown)
    pc_total: float        # wait + the real run
    github_total: float    # start + the real run x ratio
    sooner: bool           # the place rule would send it to GitHub, and GitHub finishes first


@dataclass(frozen=True)
class SuiteCheck:
    suite: Suite
    pc_expected: float | None  # selected x the window's per-test median of PC runs; None without a selection


@dataclass(frozen=True)
class Report:
    rule: Rule
    github: list[Suite]
    pc_suites: list[Suite]
    pc_runs: list[PcRun]
    long_runs: list[Expected]          # PC runs of 2 min or more, not merge-suite runs
    merge_suite_long: list[Expected]   # merge-suite PC runs of 2 min or more: listed apart, not in row 3
    row3: list[Expected]
    row4: list[Suite]
    row8: list[Suite]
    other_pc: list[Suite]              # PC suites for another reason
    suite_checks: list[SuiteCheck]     # each GitHub suite: real time and expected PC time


# ---- pure readers ----

def is_github_fallback(reason: str) -> bool:
    """A PC suite's reason (the text in its parentheses) that makes it a whole-suite GitHub fallback."""
    return reason.lower().startswith(GITHUB_REASON_PREFIX.lower()) or reason == PARTIAL_UNREADABLE \
        or TEST_COPY_REFUSED in reason


def parse_suite(line: str) -> Suite | None:
    """One merge.log line as a suite with its place; None for a line that names no suite place."""
    match = MERGE_LINE.match(line.rstrip("\r\n"))
    if match is None:
        return None
    when = datetime.strptime(match.group(1), STAMP_FORMAT)
    text = match.group(2)
    branch = text.split(" | ", 1)[0]
    time_match = SUITE_TIME.search(text)
    seconds = None if time_match is None else float(time_match.group(1)) * (60 if time_match.group(2) == "min" else 1)
    selected_match = SELECTED.search(text)
    selected = None if selected_match is None else int(selected_match.group(1))
    windows = WINDOWS_PART in text
    if GITHUB_PLACE in text:
        return Suite(when, branch, "github", False, "", seconds, selected, windows)
    if PC_PLACE in text:
        reason_match = PC_REASON.search(text)
        raw = "" if reason_match is None else reason_match.group(1)
        reason = raw[len(GITHUB_REASON_PREFIX):] if raw.startswith(GITHUB_REASON_PREFIX) else raw
        return Suite(when, branch, "pc", is_github_fallback(raw), reason, seconds, selected, windows)
    return None


def parse_pc_run(line: str) -> PcRun | None:
    """One pytest_runs.jsonl line as a PC run; None when the reader of the report does not read it as a run."""
    run = pytest_runs_report.parse_line(line.encode("utf-8"))
    if run is None:
        return None
    row = json.loads(line)
    worktree = str(row.get("worktree") or "")
    label = str(row.get("ticket") or worktree or "-")
    return PcRun(run.start, run.seconds, run.repo, label, run.selected, worktree == MERGE_SUITE_WORKTREE)


def parse_take(line: str) -> Take | None:
    """One heavy_runs.jsonl take line of the PC line (kind not github); None for any other line."""
    try:
        row = json.loads(line)
        if not isinstance(row, dict) or row.get("action") != "take" or row.get("kind") == "github":
            return None
        when = datetime.strptime(row["time"], STAMP_FORMAT)
        repo = row["repo"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(repo, str):
        return None
    waited = row.get("waited_min")
    numeric = isinstance(waited, (int, float)) and not isinstance(waited, bool)
    return Take(when, repo, float(waited) * 60 if numeric else None)


def _number(settings: dict, key: str, default: float) -> float:
    value = settings.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return float(default)
    return float(value)


def rule_of(settings: dict) -> Rule:
    """The rule's settings as place_decision reads them (its keys and defaults)."""
    return Rule(
        github_suite=settings.get("github_suite") is True,
        start=_number(settings, place_decision.START_KEY.format("linux"), place_decision.START_DEFAULTS["linux"]),
        start_windows=_number(settings, place_decision.START_KEY.format("windows"),
                              place_decision.START_DEFAULTS["windows"]),
        floor=_number(settings, place_decision.FLOOR_KEY, place_decision.FLOOR_DEFAULT),
        ratio=_number(settings, place_decision.RATIO_KEY, place_decision.RATIO_DEFAULT),
    )


# ---- pure counts ----

def take_wait(run: PcRun, takes: list[Take]) -> float:
    """The run's wait: the waited time of the take on its repo nearest to its start, within 120 s; 0 when none."""
    near = [t for t in takes if t.repo == run.repo and abs((t.when - run.start).total_seconds()) <= TAKE_MATCH_SECONDS]
    if not near:
        return 0.0
    best = min(near, key=lambda t: abs((t.when - run.start).total_seconds()))
    return best.waited or 0.0


def expect(run: PcRun, wait: float, rule: Rule) -> Expected:
    """place_decision's arithmetic (rules 4 and 5) on the run's real PC time, with the wait given."""
    github_total = rule.start + run.seconds * rule.ratio
    pc_total = wait + run.seconds
    eligible = rule.github_suite and run.seconds >= max(rule.floor, rule.start)
    return Expected(run, wait, pc_total, github_total, eligible and github_total < pc_total)


def start_for(suite: Suite, rule: Rule) -> float:
    """The GitHub start a suite is judged against: the windows start where it ran a windows part, else the linux one."""
    return rule.start_windows if suite.windows else rule.start


def threshold(suite: Suite, rule: Rule) -> float:
    """The shortest real time row 4 accepts for a suite sent to GitHub: max(floor, its start)."""
    return max(rule.floor, start_for(suite, rule))


def per_test_median(runs: list[PcRun]) -> float | None:
    """The median of seconds per selected test over the PC runs that selected some; None when none did."""
    per_test = [r.seconds / r.selected for r in runs if r.selected > 0]
    return statistics.median(per_test) if per_test else None


def suite_check(suite: Suite, median: float | None) -> SuiteCheck:
    expected = None if suite.selected is None or median is None else suite.selected * median
    return SuiteCheck(suite, expected)


def report(suites: list[Suite], pc_runs: list[PcRun], takes: list[Take], rule: Rule) -> Report:
    github = [s for s in suites if s.place == "github"]
    pc_suites = [s for s in suites if s.place == "pc"]
    long = [r for r in pc_runs if r.seconds >= LONG_RUN_SECONDS]
    long_runs = [expect(r, take_wait(r, takes), rule) for r in long if not r.merge_suite]
    merge_suite_long = [expect(r, take_wait(r, takes), rule) for r in long if r.merge_suite]
    median = per_test_median(pc_runs)
    return Report(
        rule=rule,
        github=github,
        pc_suites=pc_suites,
        pc_runs=pc_runs,
        long_runs=long_runs,
        merge_suite_long=merge_suite_long,
        row3=[e for e in long_runs if e.sooner],
        row4=[s for s in github if s.seconds is not None and s.seconds < threshold(s, rule)],
        row8=[s for s in pc_suites if s.fallback] if rule.github_suite else [],
        other_pc=[s for s in pc_suites if not s.fallback],
        suite_checks=[suite_check(s, median) for s in github],
    )


def count_text(n: int, noun: str, seconds: list[float | None]) -> str:
    known = [s for s in seconds if s is not None]
    text = f"{n} {noun}{'' if n == 1 else 's'}, total {human(sum(known))}"
    unknown = len(seconds) - len(known)
    return text + (f" ({unknown} without a time)" if unknown else "")


def time_text(seconds: float | None) -> str:
    return "time unknown" if seconds is None else human(seconds)


def run_line(e: Expected) -> str:
    return (f"  {e.run.repo} {e.run.label}: real {human(e.run.seconds)}, expected GitHub "
            f"{human(e.github_total)} (PC {human(e.pc_total)} = wait {human(e.wait)} + run)")


def suite_line(check: SuiteCheck) -> str:
    expected = "n/a" if check.pc_expected is None else human(check.pc_expected)
    return f"  {check.suite.branch}: GitHub suite real {time_text(check.suite.seconds)}, expected PC {expected}"


def render(rep: Report, since: datetime, until: datetime) -> list[str]:
    rule = rep.rule
    lines = [
        f"test places, {since:%Y-%m-%d %H:%M} to {until:%Y-%m-%d %H:%M} (local time); GitHub suite "
        f"{'on' if rule.github_suite else 'off'}; GitHub start {human(rule.start)} linux, "
        f"{human(rule.start_windows)} windows; floor {human(rule.floor)}, speed ratio {rule.ratio:g}",
        "kept on the PC and sent to GitHub (real time):",
        f"  GitHub, merge suites: {count_text(len(rep.github), 'suite', [s.seconds for s in rep.github])}",
        f"  PC, merge suites: {count_text(len(rep.pc_suites), 'suite', [s.seconds for s in rep.pc_suites])}, "
        f"of them {len(rep.row8)} GitHub fallback{'' if len(rep.row8) == 1 else 's'}",
        f"  PC, runs (pytest_runs): {count_text(len(rep.pc_runs), 'run', [r.seconds for r in rep.pc_runs])}",
        f"PC for another reason: {len(rep.other_pc)}",
        f"PC runs of 2 min or more: {len(rep.long_runs)}",
        f"merge-suite PC runs: {len(rep.merge_suite_long)} (of 2 min or more; listed, not counted in row 3)",
    ]
    lines += [run_line(e) for e in rep.merge_suite_long]
    lines.append(f"row 3: {len(rep.row3)}")
    lines += [run_line(e) for e in rep.row3]
    lines.append("GitHub suites (listed, not counted in row 3):")
    lines += [suite_line(c) for c in rep.suite_checks]
    lines.append(f"row 4: {len(rep.row4)}")
    lines.append("row 4 reads merge suites only (heavy_runs GitHub take lines carry no duration)")
    for s in rep.row4:
        lines.append(f"  {s.branch} at {s.when:%Y-%m-%d %H:%M:%S}: GitHub suite {time_text(s.seconds)}, "
                     f"under {human(threshold(s, rule))}")
    lines.append(f"row 8: {len(rep.row8)}")
    for s in rep.row8:
        lines.append(f"  {s.branch} at {s.when:%Y-%m-%d %H:%M:%S}: GitHub fallback, reason: {s.reason}")
    return lines


# ---- I/O and command line ----

def parse_since(text: str) -> datetime:
    for form in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, form)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"not a time: {text!r} (YYYY-MM-DD HH:MM)")


def read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def main(argv: list[str] | None = None, *, root: Path = ROOT, machine: Path | None = None,
         now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--since", type=parse_since, help="window start, local time YYYY-MM-DD HH:MM")
    parser.add_argument("--hours", type=float, default=24.0,
                        help="window length in hours when --since is not given (default 24)")
    args = parser.parse_args(argv)
    now = now or datetime.now()
    since = args.since or now - timedelta(hours=args.hours)
    try:
        settings = core_settings.load(root / "config" / "orchestration.json")
    except RuntimeError as exc:
        print(f"no settings: {exc}")
        return 1
    folder = machine if machine is not None else machine_dir()
    suites = [s for s in (parse_suite(x) for x in read_lines(root / "var" / "agentops" / "merge.log"))
              if s is not None and since <= s.when <= now]
    pc_runs = [r for r in (parse_pc_run(x) for x in read_lines(folder / pytest_runs_report.LOG_NAME))
               if r is not None and since <= r.start <= now]
    takes = [t for t in (parse_take(x) for x in read_lines(folder / "heavy_runs.jsonl")) if t is not None]
    for line in render(report(suites, pc_runs, takes, rule_of(settings)), since, now):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
