"""#340 unit 7: tools/agentops/monitor_github.py, the merges tab's pane-free data.

The real-file checks read this checkout's merge.log, results folder, machine folder and settings, read-only. The
synthetic checks write their own small log and results under tmp_path. The place forms and the status lines below are
copied from the writers' real lines in var/agentops/merge.log (merge.py, merge_github.py).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools.agentops import core_settings, github_minutes, machine_berths, monitor_github

ROOT = core_settings.ROOT
LOG = ROOT / "var" / "agentops" / "merge.log"
RESULTS = ROOT / "var" / "agentops" / "results"
TS = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \| ")
# where the tests ran: GitHub k/n parts (+ machines, + a PC re-check or the PC tests), or the PC with its reason
PLACE = re.compile(
    r"^(GitHub \d+/\d+ parts done(, \d+ machines)?(; re-check of \d+ on the PC \(.+?\))?( \(\+ \d+ tests? on the PC\))?"
    r"|PC \(.+\))$"
)
STAMP = "2026-10-23 10:00:00"


def _pushed_stamps(limit: int) -> list[str]:
    """The stamps of the last `limit` merge.log landings, read here with no code of the module under test."""
    stamps = []
    for line in LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        if not TS.match(line):
            continue
        fields = [f.strip() for f in line.split(" | ")]
        if len(fields) > 3 and fields[2] == "pushed" and fields[3].startswith("suite "):
            stamps.append(fields[0])
    return stamps[-limit:]


def _log_line(stamp: str, branch: str, reason: str, place: str | None, output: str = "output x") -> str:
    """A merge.log line in the writers' shape: stamp | branch | reason | suite … | failed | [place] | output …"""
    fields = [stamp, branch, reason, "suite 4 min", "0 failed"]
    if place is not None:
        fields.append(place)
    fields.append(output)
    return " | ".join(fields)


def _write_log(tmp_path: Path, lines: list[str]) -> Path:
    agentops = tmp_path / "var" / "agentops"
    (agentops / "results").mkdir(parents=True, exist_ok=True)
    (agentops / "merge.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return agentops


def _write_result(agentops: Path, n: int, branch: str, started: str, ended: str, rc: int) -> None:
    """A result file as merge.py writes it: `<stamp>-merge-<branch>.json`, ISO started/ended."""
    (agentops / "results" / f"202610230000{n:02d}-merge-{branch}.json").write_text(json.dumps({
        "argv": ["py", "-m", "tools.agentops", "merge", branch], "rc": rc,
        "reason": "pushed" if rc == 0 else "red-suite", "started": started, "ended": ended, "issues": [],
    }), encoding="utf-8")


# --- the real place forms (one line each, the place field as the writers print it) ----------------------------------

REAL_PLACE_FORMS = [
    # GitHub run, one part list
    ("suite on GitHub: 1 machines, parts 1.5 min/21 s, 4 min used, run 39384283521",
     "GitHub 2/2 parts done, 1 machines"),
    # GitHub run with a re-check on GitHub (its parts count too)
    ("suite on GitHub: 5 machines, parts 3.1 min/54 s/48 s/42 s/49 s/78 s, 11 min used, run 39340695474; "
     "re-check of 18 on GitHub: 1 machines, parts 38 s/13 s, 3 min used, run 39341050263",
     "GitHub 8/8 parts done, 5 machines"),
    # GitHub run with the PC tests beside it, and a re-check on GitHub
    ("suite on GitHub: 5 machines, parts 5.4 min/5.5 min/5.9 min/5.6 min/5.6 min/56 s, 32 min used, run 39442772835; "
     "re-check of 10 on GitHub: 1 machines, parts 11 min, 12 min used, run 39443772214 (+ 17 tests on the PC)",
     "GitHub 7/7 parts done, 5 machines (+ 17 tests on the PC)"),
    # batch line: the PC tests suffix after the run id
    ("suite on GitHub: 5 machines, parts 7.2 min/63 s/35 s/54 s/61 s/88 s, 17 min used, run 39976733583 "
     "(+ 17 tests on the PC)",
     "GitHub 6/6 parts done, 5 machines (+ 17 tests on the PC)"),
    # GitHub red, a re-check on the PC (the writer's form for a re-check GitHub could not judge)
    ("suite on GitHub: 1 machines, parts 1.9 min/21 min, 24 min used, run 39407253725; "
     "re-check of 12 on the PC (red, no test named)",
     "GitHub 2/2 parts done, 1 machines; re-check of 12 on the PC (red, no test named)"),
    # GitHub fallback to the PC (merge_github.pc_place_text)
    ("suite on the PC (GitHub lost)", "PC (GitHub lost)"),
    ("suite on the PC (GitHub dispatch-failed)", "PC (GitHub dispatch-failed)"),
    ("suite on the PC (GitHub refused: 3 hits: tests/test_issue281_minutes_windows_x2.py:78 product, "
     "tests/test_issue281_minutes_windows_x2.py:122 product, tests/test_issue281_minutes_windows_x2.py:126 product)",
     "PC (GitHub refused: 3 hits: tests/test_issue281_minutes_windows_x2.py:78 product, "
     "tests/test_issue281_minutes_windows_x2.py:122 product, tests/test_issue281_minutes_windows_x2.py:126 product)"),
    ("suite on the PC (GitHub red, no test named)", "PC (GitHub red, no test named)"),
    # a PC route with no GitHub plan: the reason of merge.py (on_pc_because / plan note)
    ("suite on the PC (partial run, ids unreadable)", "PC (partial run, ids unreadable)"),
    ("suite on the PC (own-runners repo over free minutes: over budget (412 of 400 min))",
     "PC (own-runners repo over free minutes: over budget (412 of 400 min))"),
    # no place field at all: GitHub is off for this repo
    (None, "PC (GitHub off)"),
]


@pytest.mark.parametrize("place_field, expected", REAL_PLACE_FORMS,
                         ids=[str(i) for i in range(len(REAL_PLACE_FORMS))])
def test_each_real_place_form_keeps_its_reason(tmp_path, place_field, expected):
    agentops = _write_log(tmp_path, [_log_line(STAMP, "i7", "pushed", place_field)])
    rows = monitor_github.merge_rows(agentops / "merge.log", agentops / "results", limit=None)
    assert len(rows) == 1
    assert rows[0]["place"] == expected
    assert PLACE.match(rows[0]["place"]), rows[0]["place"]


def test_a_pc_place_never_comes_out_bare(tmp_path):
    agentops = _write_log(tmp_path, [_log_line(STAMP, "i8", "pushed", "suite on the PC (GitHub lost)"),
                                     _log_line(STAMP, "i9", "pushed", None)])
    for row in monitor_github.merge_rows(agentops / "merge.log", agentops / "results", limit=None):
        assert row["place"] != "PC", row
        assert PLACE.match(row["place"]), row["place"]


def test_a_batch_line_takes_the_suite_of_its_place_field(tmp_path):
    batch = _log_line(STAMP, "batch of 2: i319-fix, i333", "pushed",
                      REAL_PLACE_FORMS[3][0])
    agentops = _write_log(tmp_path, [batch])
    rows = monitor_github.merge_rows(agentops / "merge.log", agentops / "results", limit=None)
    assert rows[0]["place"] == "GitHub 6/6 parts done, 5 machines (+ 17 tests on the PC)"
    assert rows[0]["issues"] == [319, 333]


# --- status lines that are not merges (real forms from merge.log: output `-`) ---------------------------------------

REAL_NOT_MERGE_LINES = [
    "2026-10-16 14:24:42 | 27-docs | conflict | suite 0 s | 0 failed | output - | suite dir -",
    "2026-10-21 11:36:10 | status | conflict | suite 0 s | 0 failed | output - | suite dir -",
    "2026-10-23 18:43:04 | i325-m | dirty | suite 0 s | 0 failed | output - | suite dir -",
    "2026-10-16 21:41:15 | i80 | rebase-conflict | suite 0 s | 0 failed | output - | suite dir -",
    "2026-10-16 14:24:37 | 35-guard-ast | nothing-to-merge | suite 0 s | 0 failed | output - | suite dir -",
]

# real suite runs: a row each, whatever the reason (output is a file)
REAL_SUITE_LINES = [
    "2026-10-17 16:46:58 | orch-115 | red-on-branch | suite 0 s | 1 failed | output C:\\Coding\\orchestration-layer\\var\\agentops\\merge_suite\\20261017T161002-000000-check-orch-115.out | suite dir -",
    "2026-10-17 00:42:42 | orch-89 | target-check | suite 28 min | 0 failed | full suite: data change | output C:\\Coding\\orchestration-layer\\var\\agentops\\merge_suite\\20261017T001431-000001-orch-89.out | suite dir C:\\Coding\\orchestration-layer\\.claude\\worktrees\\merge-suite",
    "2026-10-16 23:19:53 | i81 | red-on-base | suite 25 min | 3 failed | output C:\\Coding\\orchestration-layer\\var\\agentops\\merge_suite\\20261016T225318-000000-i81.out | suite dir C:\\Coding\\orchestration-layer\\.claude\\worktrees\\merge-suite",
    "2026-10-16 13:57:36 | commit | suite-green | suite 13 min | 0 failed | full suite: no map | output C:\\Coding\\orchestration-layer\\var\\agentops\\merge_suite\\20261016T134438-000000-commit.out | suite dir C:\\Coding\\orchestration-layer\\.claude\\worktrees\\merge-suite",
    "2026-10-16 12:18:47 | orch-13 | red-suite | suite 8.5 min | 1 failed | full suite: no map | output C:\\Coding\\orchestration-layer\\var\\agentops\\merge_suite\\20261016T121017-000000-orch-13.out | suite dir C:\\Coding\\orchestration-layer\\.claude\\worktrees\\merge-suite",
]


@pytest.mark.parametrize("line", REAL_NOT_MERGE_LINES, ids=["conflict", "status-conflict", "dirty",
                                                           "rebase-conflict", "nothing-to-merge"])
def test_a_line_with_no_suite_run_is_not_a_merge_row(tmp_path, line):
    agentops = _write_log(tmp_path, [line])
    assert monitor_github.merge_rows(agentops / "merge.log", agentops / "results", limit=None) == []


@pytest.mark.parametrize("line", REAL_SUITE_LINES, ids=["red-on-branch", "target-check", "red-on-base",
                                                       "suite-green", "red-suite"])
def test_a_real_suite_status_is_a_merge_row(tmp_path, line):
    agentops = _write_log(tmp_path, [line])
    rows = monitor_github.merge_rows(agentops / "merge.log", agentops / "results", limit=None)
    assert len(rows) == 1
    assert rows[0]["reason"] == line.split(" | ")[2]


def test_no_suite_status_eats_the_twenty_shown(tmp_path):
    """30 conflict / dirty lines, newer than 3 pushed landings: the 3 landings are the only rows, none is cut."""
    lines = [_log_line(f"2026-10-16 14:{n:02d}:00", f"x{n}", "pushed", None) for n in range(3)]
    lines += [REAL_NOT_MERGE_LINES[0].replace("14:24:42", f"15:{n % 60:02d}:00") for n in range(30)]
    agentops = _write_log(tmp_path, lines)
    rows = monitor_github.merge_rows(agentops / "merge.log", agentops / "results", limit=monitor_github.MERGES_SHOWN)
    assert [r["branch"] for r in rows] == ["x2", "x1", "x0"]


# --- outcome: started <= log stamp <= ended, with a few seconds of tolerance; batch per member -------------------

def test_batch_lands_when_every_member_landed(tmp_path):
    """Real batch line: its stamp is 1 s before the result's ended (17:24:27 / 17:24:28)."""
    agentops = _write_log(tmp_path, [_log_line("2026-10-23 17:24:27", "batch of 2: i319-fix, i333", "pushed",
                                               None)])
    _write_result(agentops, 1, "i319-fix", "2026-10-23T17:13:48", "2026-10-23T17:24:28", 0)
    _write_result(agentops, 2, "i333", "2026-10-23T17:13:50", "2026-10-23T17:24:28", 0)
    rows = monitor_github.merge_rows(agentops / "merge.log", agentops / "results", limit=None)
    assert rows[0]["outcome"] == "landed"


def test_batch_fails_when_one_member_failed(tmp_path):
    agentops = _write_log(tmp_path, [_log_line("2026-10-23 17:24:27", "batch of 2: i319-fix, i333", "pushed", None)])
    _write_result(agentops, 1, "i319-fix", "2026-10-23T17:13:48", "2026-10-23T17:24:28", 0)
    _write_result(agentops, 2, "i333", "2026-10-23T17:13:50", "2026-10-23T17:24:28", 1)
    rows = monitor_github.merge_rows(agentops / "merge.log", agentops / "results", limit=None)
    assert rows[0]["outcome"] == "failed"


def test_batch_is_lost_when_one_member_has_no_result(tmp_path):
    agentops = _write_log(tmp_path, [_log_line("2026-10-23 17:24:27", "batch of 2: i319-fix, i333", "pushed", None)])
    _write_result(agentops, 1, "i319-fix", "2026-10-23T17:13:48", "2026-10-23T17:24:28", 0)
    rows = monitor_github.merge_rows(agentops / "merge.log", agentops / "results", limit=None)
    assert rows[0]["outcome"] == "lost"


def test_a_log_stamp_a_few_seconds_after_ended_still_matches(tmp_path):
    agentops = _write_log(tmp_path, [_log_line("2026-10-23 17:24:31", "i5", "pushed", None)])
    _write_result(agentops, 1, "i5", "2026-10-23T17:13:48", "2026-10-23T17:24:28", 0)
    rows = monitor_github.merge_rows(agentops / "merge.log", agentops / "results", limit=None)
    assert rows[0]["outcome"] == "landed"


def test_a_log_stamp_long_after_ended_is_lost(tmp_path):
    agentops = _write_log(tmp_path, [_log_line("2026-10-23 17:25:28", "i5", "pushed", None)])
    _write_result(agentops, 1, "i5", "2026-10-23T17:13:48", "2026-10-23T17:24:28", 0)
    rows = monitor_github.merge_rows(agentops / "merge.log", agentops / "results", limit=None)
    assert rows[0]["outcome"] == "lost"


def test_a_log_stamp_a_few_seconds_before_started_still_matches(tmp_path):
    agentops = _write_log(tmp_path, [_log_line("2026-10-23 17:13:46", "i5", "pushed", None)])
    _write_result(agentops, 1, "i5", "2026-10-23T17:13:48", "2026-10-23T17:24:28", 0)
    rows = monitor_github.merge_rows(agentops / "merge.log", agentops / "results", limit=None)
    assert rows[0]["outcome"] == "landed"


def test_a_log_stamp_long_before_started_is_lost(tmp_path):
    agentops = _write_log(tmp_path, [_log_line("2026-10-23 17:00:00", "i5", "pushed", None)])
    _write_result(agentops, 1, "i5", "2026-10-23T17:13:48", "2026-10-23T17:24:28", 0)
    rows = monitor_github.merge_rows(agentops / "merge.log", agentops / "results", limit=None)
    assert rows[0]["outcome"] == "lost"


# --- the real files and the source functions -----------------------------------------------------------------------

needs_real_log = pytest.mark.skipif(not LOG.is_file(), reason="no var/agentops/merge.log here (a test copy or a fresh checkout): these read the real log")


@needs_real_log
def test_last_ten_landings_each_get_a_place_and_land():
    """Acceptance 2: the place of 10 of 10 consecutive landings of the real merge.log is shown."""
    stamps = _pushed_stamps(10)
    assert len(stamps) == 10, "this checkout's merge.log holds fewer than 10 landings"
    rows = {r["at"]: r for r in monitor_github.merge_rows(LOG, RESULTS, limit=None)}
    shown = 0
    for stamp in stamps:
        row = rows.get(stamp)
        if row is not None and PLACE.match(row["place"]) and row["outcome"] == "landed":
            shown += 1
    print(f"\nlandings with a place: {shown} of {len(stamps)}")
    assert shown == len(stamps)


@needs_real_log
def test_collect_on_the_real_files_has_its_keys_and_no_error():
    doc = monitor_github.collect()
    assert {"at", "merges", "machines", "minutes"} <= set(doc)
    assert not [k for k in doc if k.endswith("_error")], [doc.get(k) for k in doc if k.endswith("_error")]
    assert len(doc["merges"]) == monitor_github.MERGES_SHOWN
    for row in doc["merges"]:
        assert PLACE.match(row["place"]), row["place"]
        assert row["outcome"] in ("landed", "failed", "lost")


def test_machines_equal_the_source_functions():
    doc = monitor_github.collect()
    total = core_settings.load()["github_machines_total"]
    assert doc["machines"]["total"] == int(total)
    assert doc["machines"]["used"] == machine_berths.machines_in_use()


def test_minutes_equal_github_minutes_per_target():
    settings = core_settings.load()
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    doc = monitor_github.collect()
    targets = settings["targets"]
    assert [m["slug"] for m in doc["minutes"]] == [t["repo"] for t in targets]
    for entry, target in zip(doc["minutes"], targets):
        name = Path(target["folder"]).name
        assert entry["name"] == name
        assert entry["cap"] == settings["github_free_minutes"]
        assert entry["month"] == month
        assert entry["used"] == github_minutes.minutes_used(name, month)


def test_unreadable_log_gives_merges_error_and_keeps_the_other_parts(tmp_path):
    log = tmp_path / "var" / "agentops" / "merge.log"
    log.mkdir(parents=True)  # a folder where the log file should be: unreadable as a file
    doc = monitor_github.collect(root=tmp_path)
    assert doc["merges"] is None
    assert doc["merges_error"]
    assert doc["machines"] is not None and doc["minutes"] is not None
    assert "machines_error" not in doc and "minutes_error" not in doc


def test_place_and_outcome_of_three_forms_and_a_red(tmp_path):
    """Synthetic: a GitHub place, a PC place after a GitHub fallback, a PC place with no GitHub plan, and a red suite."""
    agentops = tmp_path / "var" / "agentops"
    results = agentops / "results"
    results.mkdir(parents=True)
    lines = [
        "2026-10-23 10:00:00 | i1 | pushed | suite 4 min | 0 failed | suite on GitHub: 2 machines, parts 3 min/1.5 min, 5 min used, run 1 | output x",
        "2026-10-23 10:05:00 | i2 | pushed | suite 3 min | 0 failed | suite on the PC (GitHub lost) | output x",
        "2026-10-23 10:10:00 | i3 | pushed | suite 2 min | 0 failed | output x",
        "2026-10-23 10:15:00 | i4 | red-suite | suite 1 min | 1 failed | output x",
    ]
    (agentops / "merge.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for n, (branch, stamp, rc) in enumerate(
        [("i1", "10:00:01", 0), ("i2", "10:05:01", 0), ("i3", "10:10:01", 0), ("i4", "10:15:01", 1)]
    ):
        (results / f"20261023T1{n:02d}000-merge-{branch}.json").write_text(json.dumps({
            "argv": ["py", "-m", "tools.agentops", "merge", branch, "--issue", str(n + 1)],
            "rc": rc, "reason": "pushed" if rc == 0 else "red-suite", "started": "2026-10-23T10:00:00",
            "ended": f"2026-10-23T{stamp}", "issues": [n + 1],
        }), encoding="utf-8")
    rows = monitor_github.merge_rows(agentops / "merge.log", results, limit=None)
    by_branch = {r["branch"]: r for r in rows}
    assert by_branch["i1"]["place"] == "GitHub 2/2 parts done, 2 machines"
    assert by_branch["i2"]["place"] == "PC (GitHub lost)"
    assert by_branch["i3"]["place"] == "PC (GitHub off)"
    assert by_branch["i4"]["place"] == "PC (GitHub off)"
    assert by_branch["i1"]["outcome"] == "landed"
    assert by_branch["i4"]["outcome"] == "failed"
    assert by_branch["i4"]["issues"] == [4]


def test_a_merge_without_a_result_file_is_lost(tmp_path):
    agentops = tmp_path / "var" / "agentops"
    (agentops / "results").mkdir(parents=True)
    (agentops / "merge.log").write_text(
        "2026-10-23 11:00:00 | i9 | pushed | suite 2 min | 0 failed | output x\n", encoding="utf-8")
    rows = monitor_github.merge_rows(agentops / "merge.log", agentops / "results", limit=None)
    assert rows[0]["outcome"] == "lost"
    assert rows[0]["issues"] == [9]  # no result file: the branch's own number
