"""#345: the GitHub result reader takes all of what a run said.

Row 1 reads a real log: `tests/fixtures/poe_39984304018_windows_log.txt` is TRIMMED. It is lines 1430-1722 of the full
`test (windows)` job log of Poe-Junkroan run 39984304018 (1722 lines in the full log; the outer pytest summary header
is its line 1477, the 40 outer `FAILED` lines follow it, a nested pytest prints an indented second summary header at
line 1691, and the outer `ERROR` lines are at 1698-1703). Lines 1-1429 are the job's set-up and the first test steps,
which the reader never needs; they are cut to keep the fixture small.

Row 2: a job seen `completed` with an empty conclusion is read again (at most 3 further reads, one poll interval
apart); a conclusion that appears is taken; still empty after the bound it is `lost`, as before.

Every test uses a fake `gh(args) -> str` and a simulated clock whose `sleep` only advances the clock, so no test waits
real time.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.agentops import github_run

REPO = "owner/copy"
RUN_ID = 39984304018
BASE = datetime(2026, 10, 23, 16, 8, 0, tzinfo=timezone.utc)
ZERO = "0001-01-01T00:00:00Z"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "poe_39984304018_windows_log.txt"

ERROR_IDS = [
    "tests/test_issue749_early_warning.py::test_row1_same_rows_no_drop",
    "tests/test_issue749_early_warning.py::test_row2_lowered_by_threshold_plus_one",
    "tests/test_issue749_early_warning.py::test_row3_lowered_by_threshold_minus_one",
    "tests/test_issue749_early_warning.py::test_row4_one_seed_only",
    "tests/test_issue749_early_warning.py::test_row5_failed_row",
    "tests/test_issue749_early_warning.py::test_row6_post_twice",
]


def iso(seconds: float) -> str:
    return (BASE + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Clock:
    """A simulated monotonic clock: `sleep` advances it and records the sleeps."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class FakeGh:
    """Replays `run view --json` by the simulated time (`view(t)` -> dict) and `run view --log-failed` by a string."""

    def __init__(self, clock: Clock, view=None, log: str = "") -> None:
        self.clock, self.view, self.log = clock, view, log
        self.view_times: list[float] = []

    def __call__(self, args: list[str], input: str | None = None) -> str:
        if args[:2] == ["run", "view"] and "--log-failed" in args:
            return self.log
        if args[:2] == ["run", "view"]:
            self.view_times.append(self.clock.t)
            return json.dumps(self.view(self.clock.t))
        raise AssertionError(f"unexpected gh call {args}")


def job(name: str, start: float | None, end: float | None, conclusion: str,
        steps: list[tuple[str, str]] | None = None) -> dict:
    """One job of `gh run view --json jobs`; start/end are seconds after BASE; `conclusion` is taken as it is."""
    return {
        "name": name,
        "status": "completed" if end is not None else ("in_progress" if start is not None else "queued"),
        "conclusion": conclusion,
        "startedAt": iso(start) if start is not None else ZERO,
        "completedAt": iso(end) if end is not None else ZERO,
        "steps": [{"name": n, "status": "completed", "conclusion": c} for n, c in (steps or [])],
    }


PLAN_OK = job("plan", 1, 5, "success", [("plan", "success")])
PART_STEPS = [("Set up job", "success"), ("pytest part", "success")]
RED_PART_STEPS = [("Set up job", "success"), ("pytest part", "failure")]


def one_part_view(conclusion: str, run_status: str = "completed", steps=PART_STEPS):
    """A run with the plan job and one part `test (1)` whose conclusion is `conclusion`."""
    return {"status": run_status, "conclusion": "",
            "jobs": [PLAN_OK, job("test (1)", 10, 110, conclusion, steps)]}


def log_line(text: str, job_name: str = "test (1)", step: str = "pytest part") -> str:
    return f"{job_name}\t{step}\t2026-10-23T16:22:00.9888711Z {text}"


def poll_one(view, **kwargs):
    clock = Clock()
    gh = FakeGh(clock, view, log=kwargs.pop("log", ""))
    result = github_run.poll(REPO, 77, gh, parts=1, clock=clock, sleep=clock.sleep,
                             wall=lambda: BASE + timedelta(seconds=clock.t), **kwargs)
    return result, gh, clock


# -- row 1: the real log of run 39984304018 ----------------------------------------------------------------------

def test_row1_the_windows_log_gives_the_40_failed_and_6_error_ids_of_its_outer_summary():
    log = FIXTURE.read_text(encoding="utf-8")
    ids = github_run.failed_test_ids(REPO, RUN_ID, FakeGh(Clock(), log=log))

    assert len(ids) == 46
    assert len(set(ids)) == 46
    assert ids[0] == "tests/test_benchmark_sets.py::test_sets_name_fixtures_that_exist_and_partition_them"
    assert ids[1] == "tests/test_benchmark_sets.py::test_a_slug_is_looked_up_in_the_tuadh_folder_first"
    assert ids[39] == "tests/test_issue973_mixed_selection.py::test_mixed_selection_collects_without_errors"
    assert ids[40:] == ERROR_IDS
    assert not any("test_cost_cut" in test_id for test_id in ids)
    assert all("::" in test_id and " - " not in test_id for test_id in ids)


# -- row 2: a job completed with no conclusion yet ------------------------------------------------------------------

def test_row2_a_job_completed_with_empty_conclusion_is_read_again_and_success_is_taken():
    result, gh, clock = poll_one(lambda t: one_part_view("" if t < 30 else "success"), interval_s=30.0)

    assert result["outcome"] == "green"
    assert [part["state"] for part in result["parts"]] == ["success"]
    assert len(gh.view_times) == 2 and clock.sleeps == [30.0]


def test_row2_a_job_still_completed_with_empty_conclusion_after_the_bound_is_lost():
    result, gh, clock = poll_one(lambda t: one_part_view(""), interval_s=30.0)

    assert result["outcome"] == "lost"
    assert len(gh.view_times) == 4  # the first read and 3 further reads, then lost
    assert clock.sleeps == [30.0, 30.0, 30.0]


def test_a_conclusion_set_but_not_success_or_failure_is_still_lost_at_the_first_read():
    result, gh, _ = poll_one(lambda t: one_part_view("cancelled"), interval_s=30.0)

    assert result["outcome"] == "lost"
    assert len(gh.view_times) == 1


# -- row 3: what the reader saw, per job, as plain data ----------------------------------------------------------

def test_a_column_0_collection_error_without_double_colon_counts_as_its_path_id():
    # #345 review: a collection error prints `ERROR <path>.py - ImportError`, no `::`; the path is its id. A FAILED
    # line still needs its `::`.
    log = "\n".join([
        log_line("ERROR tests/x.py - ImportError: No module named 'x'"),
        log_line("FAILED tests/y.py - AssertionError"),
    ])
    assert github_run.failed_test_ids(REPO, RUN_ID, FakeGh(Clock(), log=log)) == ["tests/x.py"]


def test_a_red_result_names_each_job_with_its_conclusion_and_the_ids_found_in_it():
    log = "\n".join([
        log_line("=========================== short test summary info ==========================="),
        log_line("FAILED tests/test_a.py::test_one - AssertionError"),
        log_line("ERROR tests/test_b.py::test_two - FileNotFoundError"),
    ])
    result, _, _ = poll_one(lambda t: one_part_view("failure", steps=RED_PART_STEPS), log=log)

    assert result["outcome"] == "red-test"
    assert result["failed_ids"] == ["tests/test_a.py::test_one", "tests/test_b.py::test_two"]
    assert result["full_recheck"] is False
    assert result["jobs"] == [
        {"name": "plan", "conclusion": "success", "failed_ids": []},
        {"name": "test (1)", "conclusion": "failure",
         "failed_ids": ["tests/test_a.py::test_one", "tests/test_b.py::test_two"]},
    ]
