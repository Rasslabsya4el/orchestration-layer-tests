"""#352 unit 5: scripts/counts/test_places.py counts the test places of a window (rows 3, 4 and 8 of #352).

Seams: the pure pipeline (`parse_suite`, `parse_pc_run`, `parse_take`, `rule_of`, `report`) fed with fixture lines, and
`main()` run on a fixture repo folder (config/orchestration.json, var/agentops/merge.log) and a fixture machine folder
(pytest_runs.jsonl, heavy_runs.jsonl) with a fixed `now`. The expected numbers are written by hand from the fixture
lines, never computed the way the script computes them.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "counts" / "test_places.py"
_spec = importlib.util.spec_from_file_location("issue352_test_places", SCRIPT)
counts = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = counts  # dataclasses looks the module up while the script is executed
_spec.loader.exec_module(counts)

NOW = datetime(2026, 10, 24, 0, 0, 0)
SINCE = "2026-10-23 00:00"

# Four merge.log lines: two suites on GitHub (5 min 24 s, 45 s), one PC fallback (lost), one PC for another reason.
MERGE_LINES = [
    "2026-10-23 10:00:00 | i300 | pushed | suite 5.4 min | 0 failed | selected 1397 of 4711 tests in the map | "
    "suite on GitHub: 1 machines, parts 3.5 min/1.9 min, 7 min used, run 40034810182 (+ 11 tests on the PC) | "
    "output C:\\out\\a.out",
    "2026-10-23 11:00:00 | i301 | red-suite | suite 45 s | 1 failed | selected 12 of 4711 tests | "
    "suite on GitHub: 1 machines, parts 20 s, 1 min used, run 40034810183 | output C:\\out\\b.out",
    "2026-10-23 12:00:00 | i302 | pushed | suite 6 min | 0 failed | selected 243 of 4650 tests | "
    "suite on the PC (GitHub lost) | output C:\\out\\c.out",
    "2026-10-23 13:00:00 | i303 | pushed | suite 4 min | 0 failed | selected 254 of 4650 tests | "
    "suite on the PC (no GitHub plan: over the minutes cap) | output C:\\out\\d.out",
]

# Five PC runs: three under 2 min (40 s, 90 s, 100 s), one of 5 min (300 s), one of 3 min (180 s).
PC_ROWS = [
    ("2026-10-23 09:00:00", "2026-10-23 09:00:40", "wt-i320-u1", None),
    ("2026-10-23 09:10:00", "2026-10-23 09:11:30", "wt-i320-u2", None),
    ("2026-10-23 09:20:00", "2026-10-23 09:21:40", "wt-i320-u3", None),
    ("2026-10-23 14:00:00", "2026-10-23 14:05:00", "wt-i310", "i310"),
    ("2026-10-23 15:00:00", "2026-10-23 15:03:00", "wt-i311", "i311"),
]

# The PC line's take lines: the 5 min run waited 0 (its take 10 s before its start); a GitHub take at the same start
# with 10 min of wait must be ignored; the 3 min run waited 1 min. A free line is ignored too.
HEAVY_LINES = [
    {"time": "2026-10-23 13:59:50", "action": "take", "kind": "pc", "id": "a", "repo": "orchestration-layer",
     "run": "i310", "tests": 900, "holders": 1, "waited_min": 0.0},
    {"time": "2026-10-23 13:59:55", "action": "take", "kind": "github", "id": "b", "repo": "orchestration-layer",
     "run": "merge-suite/x", "tests": 0, "machines": 1, "holders": 1, "waited_min": 10.0},
    {"time": "2026-10-23 14:59:55", "action": "take", "kind": "pc", "id": "c", "repo": "orchestration-layer",
     "run": "i311", "tests": 900, "holders": 1, "waited_min": 1.0},
    {"time": "2026-10-23 14:59:56", "action": "free", "kind": "pc", "id": "c", "repo": "orchestration-layer",
     "run": "i311", "tests": 900, "holders": 0},
]


def settings(github_suite: bool = True) -> dict:
    return {
        "repo": "acme/repo", "brain_chat_title": "Brain: test", "context_line": 150000,
        "claude_projects_dir": "C--test", "bridge_dir": "var", "bridge_lock": "var/locks/bridge.lock",
        "local_modules": [], "github_suite": github_suite, "github_start_seconds_linux": 16,
    }


def pc_row(start: str, end: str, worktree: str, ticket: str | None) -> dict:
    return {"start": start, "end": end, "repo": "orchestration-layer", "worktree": worktree, "ticket": ticket,
            "selected": 3, "passed": 3, "failed": 0}


def pipeline_inputs():
    suites = [counts.parse_suite(line) for line in MERGE_LINES]
    pc_runs = [counts.parse_pc_run(json.dumps(pc_row(s, e, w, t))) for s, e, w, t in PC_ROWS]
    takes = [counts.parse_take(json.dumps(line)) for line in HEAVY_LINES]
    return suites, pc_runs, [t for t in takes if t is not None]


def write_fixture(tmp_path: Path, github_suite: bool = True):
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    (root / "config" / "orchestration.json").write_text(json.dumps(settings(github_suite)), encoding="utf-8")
    (root / "var" / "agentops").mkdir(parents=True)
    (root / "var" / "agentops" / "merge.log").write_text("\n".join(MERGE_LINES) + "\n", encoding="utf-8")
    machine = tmp_path / "machine"
    machine.mkdir()
    (machine / "pytest_runs.jsonl").write_text(
        "\n".join(json.dumps(pc_row(s, e, w, t)) for s, e, w, t in PC_ROWS) + "\n", encoding="utf-8")
    (machine / "heavy_runs.jsonl").write_text("\n".join(json.dumps(line) for line in HEAVY_LINES) + "\n",
                                              encoding="utf-8")
    return root, machine


def test_each_merge_line_names_its_place_and_a_fallback_keeps_its_reason():
    gh = counts.parse_suite(MERGE_LINES[0])
    assert (gh.place, gh.fallback, gh.seconds, gh.branch) == ("github", False, 324.0, "i300")
    short = counts.parse_suite(MERGE_LINES[1])
    assert (short.place, short.seconds) == ("github", 45.0)
    fallback = counts.parse_suite(MERGE_LINES[2])
    assert (fallback.place, fallback.fallback, fallback.reason, fallback.seconds) == ("pc", True, "lost", 360.0)
    other = counts.parse_suite(MERGE_LINES[3])
    assert (other.place, other.fallback, other.reason, other.seconds) == (
        "pc", False, "no GitHub plan: over the minutes cap", 240.0)
    assert counts.parse_suite("2026-10-23 10:05:00 | i300 | verdict: unclear") is None


def test_pure_report_of_the_fixture_gives_rows_3_4_and_8_one_each():
    suites, pc_runs, takes = pipeline_inputs()
    rep = counts.report(suites, pc_runs, takes, counts.rule_of(settings(True)))
    assert [s.seconds for s in rep.github] == [324.0, 45.0]
    assert len(rep.pc_suites) == 2
    assert len(rep.pc_runs) == 5
    assert [e.run.seconds for e in rep.long_runs] == [300.0, 180.0]
    assert [e.wait for e in rep.long_runs] == [0.0, 60.0]
    assert [e.run.label for e in rep.row3] == ["i311"]  # 60 + 180 = 240 s on the PC, 16 + 180 = 196 s on GitHub
    assert [s.seconds for s in rep.row4] == [45.0]  # under max(floor 120 s, start 16 s)
    assert [s.reason for s in rep.row8] == ["lost"]


def test_main_prints_row_3_row_4_and_row_8_for_the_window(tmp_path, capsys):
    root, machine = write_fixture(tmp_path)
    assert counts.main(["--since", SINCE], root=root, machine=machine, now=NOW) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "row 3: 1" in lines
    assert "row 4: 1" in lines
    assert "row 8: 1" in lines
    assert any(line.startswith("  orchestration-layer i311:") for line in lines)


def test_github_suite_off_gives_no_row_3_and_no_row_8(tmp_path, capsys):
    root, machine = write_fixture(tmp_path, github_suite=False)
    assert counts.main(["--since", SINCE], root=root, machine=machine, now=NOW) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "row 3: 0" in lines
    assert "row 8: 0" in lines


# ---- review of unit 5 (send-back): fallback forms, other reasons, merge-suite runs, GitHub suites, row 4 start ----

def merge_line(branch: str, place: str, seconds: str = "3 min", selected: str | None = "selected 90 of 900 tests") -> str:
    """One merge.log line with the given suite time, selection and place (the fields the script reads)."""
    middle = f" | {selected}" if selected else ""
    return f"2026-10-23 16:00:00 | {branch} | pushed | suite {seconds} | 0 failed{middle} | {place} | output C:\\out\\x.out"


def pc_row_of(start: str, end: str, worktree: str, ticket: str | None, selected: int) -> dict:
    row = pc_row(start, end, worktree, ticket)
    row["selected"] = selected
    return row


def test_every_fallback_form_is_a_github_fallback_and_the_rest_are_other_reasons():
    forms = {
        "suite on the PC (GitHub lost)": (True, "lost"),
        "suite on the PC (GitHub red, no test named)": (True, "red, no test named"),
        "suite on the PC (github settings unreadable)": (True, "github settings unreadable"),
        "suite on the PC (partial run, ids unreadable)": (True, "partial run, ids unreadable"),
        "suite on the PC (GitHub test copy refused: symlink tests/link.py)": (
            True, "test copy refused: symlink tests/link.py"),
        "suite on the PC (test copy refused: 2 hits, suite on the PC)": (
            True, "test copy refused: 2 hits, suite on the PC"),
        "suite on the PC (own-runners repo over free minutes: 3 min)": (
            False, "own-runners repo over free minutes: 3 min"),
        "suite on the PC": (False, ""),
    }
    for place, (fallback, reason) in forms.items():
        suite = counts.parse_suite(merge_line("i320", place))
        assert (suite.place, suite.fallback, suite.reason) == ("pc", fallback, reason), place


def test_report_counts_github_fallbacks_and_other_reasons_apart_and_github_suite_off_gives_no_row_8():
    suites = [counts.parse_suite(merge_line("i320", "suite on the PC (GitHub lost)")),
              counts.parse_suite(merge_line("i321", "suite on the PC (github settings unreadable)")),
              counts.parse_suite(merge_line("i322", "suite on the PC (own-runners repo over free minutes: 3 min)"))]
    rep = counts.report(suites, [], [], counts.rule_of(settings(True)))
    assert len(rep.row8) == 2
    assert len(rep.other_pc) == 1
    assert counts.report(suites, [], [], counts.rule_of(settings(False))).row8 == []


def test_merge_suite_pc_runs_are_listed_apart_and_stay_out_of_row_3():
    pc = [counts.parse_pc_run(json.dumps(pc_row_of("2026-10-23 15:00:00", "2026-10-23 15:03:00", "merge-suite", None, 3)))]
    takes = [counts.parse_take(json.dumps({"time": "2026-10-23 14:59:55", "action": "take", "kind": "pc",
                                           "repo": "orchestration-layer", "waited_min": 1.0}))]
    rep = counts.report([], pc, takes, counts.rule_of(settings(True)))
    assert rep.row3 == []  # 60 s wait + 180 s on the PC, 16 s + 180 s on GitHub: sooner, but a merge-suite run
    assert [e.run.seconds for e in rep.merge_suite_long] == [180.0]
    assert rep.merge_suite_long[0].sooner


def test_each_github_suite_gets_its_real_time_and_its_expected_pc_time_from_the_per_test_median():
    # one PC run of 100 s over 10 tests: 10 s per test (the per-test median of the window)
    pc = [counts.parse_pc_run(json.dumps(pc_row_of("2026-10-23 09:00:00", "2026-10-23 09:01:40", "wt-i320", "i320", 10)))]
    with_selection = counts.parse_suite(merge_line("i330", "suite on GitHub: 1 machines, parts 2 min, 3 min used, run 9",
                                                   seconds="2.5 min", selected="selected 90 of 900 tests"))
    no_selection = counts.parse_suite(merge_line("i331", "suite on GitHub: 1 machines, parts 2 min, 3 min used, run 10",
                                                 seconds="4 min", selected=None))
    rep = counts.report([with_selection, no_selection], pc, [], counts.rule_of(settings(True)))
    assert [c.suite.branch for c in rep.suite_checks] == ["i330", "i331"]
    assert rep.suite_checks[0].suite.seconds == 150.0
    assert rep.suite_checks[0].pc_expected == 900.0  # 90 tests x 10 s
    assert rep.suite_checks[1].pc_expected is None  # a full run names no selection: n/a


def test_row_4_takes_the_windows_start_only_where_the_suite_line_says_windows_part():
    rule = counts.rule_of({**settings(True), "place_pc_only_floor_seconds": 10})  # linux start 16 s, windows 26 s
    linux = counts.parse_suite(merge_line("i340", "suite on GitHub: 1 machines, parts 20 s, 1 min used, run 1",
                                          seconds="20 s", selected="selected 5 of 10 tests"))
    windows = counts.parse_suite(merge_line("i341", "suite on GitHub: 1 machines, windows part 20 s, 1 min used, run 2",
                                            seconds="20 s", selected="selected 5 of 10 tests"))
    assert counts.report([linux], [], [], rule).row4 == []  # 20 s is not under the linux start of 16 s
    assert [s.branch for s in counts.report([windows], [], [], rule).row4] == ["i341"]  # under the windows 26 s


def test_main_output_uses_the_glossary_words_and_says_row_4_reads_merge_suites_only(tmp_path, capsys):
    root, machine = write_fixture(tmp_path)
    assert counts.main(["--since", SINCE], root=root, machine=machine, now=NOW) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "PC for another reason: 1" in lines
    assert any(line.startswith("row 4 reads merge suites only") for line in lines)
    assert any(line.startswith("  GitHub fallback") or "GitHub fallback" in line for line in lines)
    assert not any("PC fallback" in line or "github_suite" in line for line in lines)
    assert any(line.startswith("  i300: GitHub suite real 5 min 24 s") for line in lines)
