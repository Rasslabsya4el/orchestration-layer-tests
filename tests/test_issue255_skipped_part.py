"""#255: a job whose conclusion is `skipped` is not a part of the run; the windows part counts only when `windows` was
requested. Seam: `github_run.poll` through the fake `gh` of the #232 tests (no network, no real waiting)."""
from __future__ import annotations

from tests.test_issue232_github_run import PLAN_OK, ZERO, good_job, iso, job, poll, steps


def skipped_windows() -> dict:
    """The `test (windows)` job of a run dispatched without `windows`: skipped, no steps, never started."""
    return {"name": "test (windows)", "status": "completed", "conclusion": "skipped",
            "startedAt": ZERO, "completedAt": iso(1), "steps": []}


def still_running(part: int) -> dict:
    return job(f"test ({part})", 10, None)


# -- row 1: a skipped windows job is not a part; no `lost` on the first read, `green` on the second ---------------

def test_row1_skipped_windows_job_is_not_a_part_and_the_run_is_green_after_the_first_read():
    def view(t):
        if t < 60:
            return {"status": "in_progress", "conclusion": "", "jobs": [PLAN_OK, skipped_windows(), still_running(1)]}
        return {"status": "completed", "conclusion": "success",
                "jobs": [PLAN_OK, skipped_windows(), good_job(1, 10, 100)]}

    result, gh, _ = poll(view, parts=1, windows=False)
    assert gh.view_times == [0.0, 60.0]  # the first read did not settle the outcome
    assert result["outcome"] == "green"
    assert [p["part"] for p in result["parts"]] == [1]


# -- row 2: the same, with `test (1)` failing in its pytest step: red with the failed ids, not lost ---------------

def test_row2_failed_pytest_step_with_a_skipped_windows_job_is_red_not_lost():
    failed = job("test (1)", 10, 110, "failure", steps(fail="pytest part"))
    log = "test (1)\tpytest part\t2026-10-19T12:03:00Z FAILED tests/test_x.py::test_a - boom\n"

    def view(t):
        if t < 60:
            return {"status": "in_progress", "conclusion": "", "jobs": [PLAN_OK, skipped_windows(), still_running(1)]}
        return {"status": "completed", "conclusion": "failure", "jobs": [PLAN_OK, skipped_windows(), failed]}

    result, gh, _ = poll(view, log=log, parts=1, windows=False)
    assert gh.view_times == [0.0, 60.0]
    assert result["outcome"] == "red-test"
    assert result["failed_ids"] == ["tests/test_x.py::test_a"]
    assert result["full_recheck"] is False


# -- row 3: windows requested and its job skipped: the part is missing, so `lost` once the run completes ----------

def test_row3_skipped_windows_job_with_windows_requested_is_a_missing_part_lost_once_completed():
    def view(t):
        if t < 60:
            return {"status": "in_progress", "conclusion": "", "jobs": [PLAN_OK, skipped_windows(), still_running(1)]}
        return {"status": "completed", "conclusion": "success",
                "jobs": [PLAN_OK, skipped_windows(), good_job(1, 10, 100)]}

    result, gh, _ = poll(view, parts=1, windows=True)
    assert gh.view_times == [0.0, 60.0]  # not lost while the run is still going
    assert result["outcome"] == "lost"


# -- rule 1, second clause: the windows part is not a part when `windows` was not requested --------------------

def test_a_windows_job_that_ran_without_windows_requested_is_not_a_part():
    ran = job("test (windows)", 10, 200, "success", steps())

    def view(t):
        return {"status": "completed", "conclusion": "success", "jobs": [PLAN_OK, good_job(1, 10, 100), ran]}

    result, _, _ = poll(view, parts=1, windows=False)
    assert result["outcome"] == "green"
    assert [p["part"] for p in result["parts"]] == [1]
