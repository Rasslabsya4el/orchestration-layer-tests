"""Unit 2 of 2 of issue 235: the `pytest-runs` report in tools/agentops/pytest_runs_report.py.

Seam: `run_pytest_runs(args, now=...)` on a hand-written log file in tmp_path, and `main([...])` for the CLI wiring.
Every expected value below is written out by hand from the fixture (never recomputed with the code's formula).

Hand-computed fixture, now = 2026-10-19 12:00:00, --days 7, so the window starts 2026-10-12 12:00:00:
  repo A, 10 runs in the window (+1 run of 2026-10-10 that must be excluded):
    durations s: 60 120 180 240 300 360 420 480 1200 1800 -> sorted, median (300+360)/2 = 330 s = 5.5 min,
    p90 nearest-rank: ceil(0.9*10) = 9th = 1200 s = 20.0 min; runs per day 10/7 = 1.4;
    selected > 500: the 600 and 900 runs (the 500 run is not above 500): 2 of 10 = 20.0 %,
    minutes (1200+1800)/60 = 50.0 min.
  repo B, 5 runs: durations s 45 75 600 1500 4000 -> median 600 s = 10.0 min, p90 ceil(4.5) = 5th = 4000 s = 66.7 min,
    runs per day 5/7 = 0.7; selected > 500: 1500 s and 4000 s runs, 2 of 5 = 40.0 %,
    minutes (1500+4000)/60 = 91.67 min -> past 90 min: "1 h 32 min".
  repo C, 2 runs: durations s 30 100 -> median 65 s (under 90 s, in seconds), p90 ceil(1.8) = 2nd = 100 s = 1.7 min,
    runs per day 2/7 = 0.3; selected 800 and 900 both > 500: 100.0 %, minutes 130/60 = 2.2 min.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

from tools.agentops.cli import main
from tools.agentops.pytest_runs_report import run_pytest_runs

NOW = datetime(2026, 10, 19, 12, 0, 0)


def run_line(repo: str, start: str, seconds: int, selected: int) -> str:
    """One log line in the checkpoint's fixed format (start and end are naive local time)."""
    begin = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
    end = (begin + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")
    return (f'{{"start": "{start}", "end": "{end}", "repo": "{repo}", "worktree": "wt-x", '
            f'"ticket": 235, "selected": {selected}, "passed": 1, "failed": 0}}')


def fixture_lines() -> list[str]:
    a = [
        ("2026-10-13 09:00:00", 60, 500),
        ("2026-10-13 10:00:00", 120, 40),
        ("2026-10-14 09:00:00", 180, 80),
        ("2026-10-14 10:00:00", 240, 120),
        ("2026-10-15 09:00:00", 300, 160),
        ("2026-10-15 10:00:00", 360, 200),
        ("2026-10-16 09:00:00", 420, 240),
        ("2026-10-16 10:00:00", 480, 280),
        ("2026-10-17 09:00:00", 1200, 600),
        ("2026-10-18 09:00:00", 1800, 900),
    ]
    b = [
        ("2026-10-13 08:00:00", 45, 10),
        ("2026-10-14 08:00:00", 75, 20),
        ("2026-10-15 08:00:00", 600, 30),
        ("2026-10-16 08:00:00", 1500, 501),
        ("2026-10-17 08:00:00", 4000, 1200),
    ]
    c = [
        ("2026-10-13 12:00:00", 30, 800),
        ("2026-10-14 12:00:00", 100, 900),
    ]
    lines = [run_line("A", s, d, n) for s, d, n in a] + [run_line("B", s, d, n) for s, d, n in b]
    lines += [run_line("C", s, d, n) for s, d, n in c]
    lines.append(run_line("A", "2026-10-10 09:00:00", 9999, 9999))  # outside the 7-day window
    return lines


def report(tmp_path: Path, lines: list[str], capsys) -> tuple[int, list[str]]:
    log = tmp_path / "pytest_runs.jsonl"
    log.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    code = run_pytest_runs(argparse.Namespace(days=7, log=log), now=NOW)
    return code, capsys.readouterr().out.splitlines()


def test_report_numbers_per_repo(tmp_path: Path, capsys):
    code, out = report(tmp_path, fixture_lines(), capsys)
    assert code == 0
    assert out == [
        "pytest calls, last 7 days",
        "A: 10 runs, 1.4 per day | median 5.5 min, p90 20.0 min | over 500 tests: 20.0% of runs, 50.0 min total",
        "B: 5 runs, 0.7 per day | median 10.0 min, p90 66.7 min | over 500 tests: 40.0% of runs, 1 h 32 min total",
        "C: 2 runs, 0.3 per day | median 65 s, p90 1.7 min | over 500 tests: 100.0% of runs, 2.2 min total",
    ]


def test_empty_log_prints_no_runs_logged(tmp_path: Path, capsys):
    code, out = report(tmp_path, [], capsys)
    assert code == 0
    assert out == ["no runs logged"]


def test_missing_log_prints_no_runs_logged(tmp_path: Path, capsys):
    code = run_pytest_runs(argparse.Namespace(days=7, log=tmp_path / "absent.jsonl"), now=NOW)
    assert code == 0
    assert capsys.readouterr().out.splitlines() == ["no runs logged"]


def test_unreadable_lines_are_skipped_and_counted(tmp_path: Path, capsys):
    bad = [
        "not json",                                                       # 1: not JSON
        "[1, 2]",                                                         # 2: JSON but not an object
        '{"start": "2026-10-13 09:00:00", "end": "2026-10-13 08:00:00", "repo": "C", "selected": 5}',  # 3: end first
        '{"start": "2026-10-13 09:00:00", "end": "2026-10-13 09:01:00", "repo": "C"}',                 # 4: no selected
        '{"start": "13.10.2026", "end": "13.10.2026", "repo": "C", "selected": 5}',                    # 5: bad time
    ]
    lines = [run_line("C", "2026-10-13 12:00:00", 30, 800), "", *bad]  # a blank line is not counted
    code, out = report(tmp_path, lines, capsys)
    assert code == 0
    assert out == [
        "pytest calls, last 7 days",
        "C: 1 run, 0.1 per day | median 30 s, p90 30 s | over 500 tests: 100.0% of runs, 30 s total",
        "skipped 5 unreadable lines",
    ]


def test_cli_missing_log_exits_zero(tmp_path: Path, capsys):
    code = main(["pytest-runs", "--log", str(tmp_path / "absent.jsonl")])
    assert code == 0
    assert "no runs logged" in capsys.readouterr().out
