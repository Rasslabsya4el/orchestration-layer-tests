"""#232 unit 2: the PC drives one run of the test copy's `tests.yml` through `gh` (dispatch, bounded poll, failed ids).

Every test uses a fake `gh(args) -> str` replaying the JSON shape of `gh run list|view --json ...` and a simulated
clock whose `sleep` only advances the clock, so no test waits real time.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tools.agentops import github_run
from tools.agentops.util import AgentOpsError

REPO = "owner/copy"
BASE = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
ZERO = "0001-01-01T00:00:00Z"


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


def job(name: str, start: float | None, end: float | None, conclusion: str | None = None,
        steps: list[tuple[str, str]] | None = None) -> dict:
    """One job of `gh run view --json jobs`; start/end are seconds after BASE."""
    return {
        "name": name,
        "status": "completed" if end is not None else ("in_progress" if start is not None else "queued"),
        "conclusion": conclusion or "",
        "startedAt": iso(start) if start is not None else ZERO,
        "completedAt": iso(end) if end is not None else ZERO,
        "steps": [{"name": n, "status": "completed", "conclusion": c} for n, c in (steps or [])],
    }


def good_job(part: int, start: float, seconds: float) -> dict:
    return job(f"test ({part})", start, start + seconds, "success", steps())


REAL_STEPS = ["Set up job", "Run actions/checkout@v4", "Run actions/setup-python@v5",
              "Run pip install pytest pytest-xdist psutil", "pytest part", "Post Run actions/checkout@v4",
              "Complete job"]


def steps(fail: str | None = None, conclusion: str = "failure") -> list[tuple[str, str]]:
    """The step list `gh run view --json jobs` reports for the real workflow's test job: the unnamed `uses:` /
    `run:` steps get gh's own names; a failed step skips the ones after it, but `Post` and `Complete job` still run."""
    out, failed = [], False
    for name in REAL_STEPS:
        if name == fail:
            out.append((name, conclusion))
            failed = True
        elif failed and not name.startswith("Post") and name != "Complete job":
            out.append((name, "skipped"))
        else:
            out.append((name, "success"))
    return out


PLAN_STEPS = [("Set up job", "success"), ("Run actions/checkout@v4", "success"), ("plan", "success"),
              ("Post Run actions/checkout@v4", "success"), ("Complete job", "success")]
PLAN_OK = job("plan", 1, 5, "success", PLAN_STEPS)  # 4 s -> 1 minute


class FakeGh:
    """Replays `run view` by the simulated time; `run list` / `workflow run` / `--log-failed` by script."""

    def __init__(self, clock: Clock, view, list_runs=None, log: str = "") -> None:
        self.clock, self.view, self.list_runs, self.log = clock, view, list_runs or [], log
        self.calls: list[list[str]] = []
        self.view_times: list[float] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        if args[:2] == ["run", "view"] and "--log-failed" in args:
            return self.log
        if args[:2] == ["run", "view"]:
            self.view_times.append(self.clock.t)
            return json.dumps(self.view(self.clock.t))
        if args[:2] == ["run", "list"]:
            return json.dumps(self.list_runs)
        if args[:2] == ["workflow", "run"]:
            return ""
        raise AssertionError(f"unexpected gh call {args}")


def poll(view, *, log: str = "", **kwargs):
    clock = Clock()
    gh = FakeGh(clock, view, log=log)
    result = github_run.poll(REPO, 77, gh, clock=clock, sleep=clock.sleep,
                             wall=lambda: BASE + timedelta(seconds=clock.t), **kwargs)
    return result, gh, clock


# -- row 2: no part started -> not-started at the bound -------------------------------------------------------

def test_no_part_started_in_ten_minutes_is_not_started_and_never_sleeps_real_time():
    def view(t):
        return {"status": "queued", "conclusion": "", "jobs": [PLAN_OK]}

    result, gh, clock = poll(view, parts=5)
    assert result["outcome"] == "not-started"
    assert result["run_id"] == 77
    assert result["parts"] == []
    assert result["minutes_used"] == 1  # the plan job alone: 4 s, one minute
    assert 600 <= clock.t < 660  # gave up at the 10 minute bound
    assert all(s <= 60 for s in clock.sleeps)


def test_queued_parts_with_zero_started_at_still_count_as_not_started():
    def view(t):
        return {"status": "queued", "conclusion": "",
                "jobs": [PLAN_OK] + [job(f"test ({n})", None, None) for n in (1, 2, 3)]}

    result, _, _ = poll(view, parts=3)
    assert result["outcome"] == "not-started"
    assert [p["state"] for p in result["parts"]] == ["queued"] * 3


def test_a_part_that_starts_before_the_bound_keeps_the_poll_going():
    def view(t):
        if t < 300:
            return {"status": "queued", "conclusion": "", "jobs": [PLAN_OK]}
        done = t >= 420
        return {"status": "completed" if done else "in_progress", "conclusion": "success" if done else "",
                "jobs": [PLAN_OK, job("test (1)", 300, 400 if done else None, "success" if done else None,
                                      steps() if done else None)]}

    result, _, _ = poll(view, parts=1)
    assert result["outcome"] == "green"


# -- row 3: red test vs lost ---------------------------------------------------------------------------------

def red_view(t):
    return {"status": "completed", "conclusion": "failure", "jobs": [
        PLAN_OK, good_job(1, 10, 100),
        job("test (2)", 10, 130, "failure", steps("pytest part")),
    ]}


def test_a_part_whose_pytest_step_failed_is_red_test():
    result, _, _ = poll(red_view, parts=2)
    assert result["outcome"] == "red-test"
    assert [(p["part"], p["state"]) for p in result["parts"]] == [(1, "success"), (2, "failure")]


@pytest.mark.parametrize("conclusion,failing", [
    ("failure", steps("Set up job")),
    ("failure", steps("Run actions/checkout@v4")),
    ("failure", steps("Run actions/setup-python@v5")),
    ("failure", steps("Run pip install pytest pytest-xdist psutil")),  # "pytest" is in the name, still setup
    ("cancelled", steps("pytest part", "cancelled")),
    ("timed_out", steps("pytest part", "cancelled")),
])
def test_a_part_lost_by_the_runner_or_setup_is_lost_not_red_test(conclusion, failing):
    def view(t):
        return {"status": "completed", "conclusion": "failure", "jobs": [
            PLAN_OK, good_job(1, 10, 100), job("test (2)", 10, 40, conclusion, failing)]}

    result, _, _ = poll(view, parts=2)
    assert result["outcome"] == "lost"
    assert result["parts"][1]["state"] == "lost"


def test_a_cancelled_run_is_lost():
    def view(t):
        return {"status": "completed", "conclusion": "cancelled",
                "jobs": [PLAN_OK, job("test (1)", None, 30, "cancelled")]}

    result, _, _ = poll(view, parts=1)
    assert result["outcome"] == "lost"


def test_lost_wins_over_red_test_because_the_pc_runs_the_whole_suite_then():
    def view(t):
        return {"status": "completed", "conclusion": "failure", "jobs": [
            PLAN_OK,
            job("test (1)", 10, 130, "failure", steps("pytest part")),
            job("test (2)", 10, 30, "failure", steps("Set up job"))]}

    result, _, _ = poll(view, parts=2)
    assert result["outcome"] == "lost"


def test_a_failed_plan_job_with_no_parts_is_lost():
    def view(t):
        return {"status": "completed", "conclusion": "failure",
                "jobs": [job("plan", 1, 20, "failure", [("Set up job", "success"), ("plan", "failure")])]}

    result, _, _ = poll(view, parts=5)
    assert result["outcome"] == "lost"


def test_fewer_green_parts_than_asked_is_lost():
    def view(t):
        return {"status": "completed", "conclusion": "success", "jobs": [PLAN_OK, good_job(1, 10, 100)]}

    result, _, _ = poll(view, parts=5)
    assert result["outcome"] == "lost"


def log_line(text: str, step: str = "pytest part") -> str:
    return f"test (2)\t{step}\t2026-10-19T12:02:00.1234567Z {text}"


def test_failed_test_ids_are_parsed_from_the_failed_log_up_to_200():
    log = "\n".join([
        log_line("=================== short test summary info ==================="),
        log_line("FAILED tests/test_a.py::test_one - AssertionError: 1 != 2"),
        log_line("FAILED tests/test_b.py::TestK::test_two[x-1] - boom"),
        log_line("FAILED tests/test_a.py::test_one - AssertionError: 1 != 2"),
        log_line("FAILED tests/test_d.py::test_end"),
        log_line("ERROR tests/test_c.py::test_err"),
        log_line("2 failed, 40 passed in 100s"),
    ])
    clock = Clock()
    gh = FakeGh(clock, red_view, log=log)
    assert github_run.failed_test_ids(REPO, 77, gh) == [
        "tests/test_a.py::test_one", "tests/test_b.py::TestK::test_two[x-1]", "tests/test_d.py::test_end"]
    assert gh.calls == [["run", "view", "77", "-R", REPO, "--log-failed"]]

    many = "\n".join(log_line(f"FAILED tests/test_m.py::test_{n} - e") for n in range(250))
    ids = github_run.failed_test_ids(REPO, 77, FakeGh(clock, red_view, log=many))
    assert len(ids) == 200 and ids[0] == "tests/test_m.py::test_0" and ids[-1] == "tests/test_m.py::test_199"


def test_a_failed_id_with_a_space_inside_brackets_is_kept_whole():
    log = "\n".join([
        log_line("FAILED tests/test_x.py::test_x[a b] - AssertionError: assert 'a b' == 'c'"),
        log_line("FAILED tests/test_x.py::test_y[c d e]"),
        "FAILED tests/test_z.py::test_bare - boom\r",  # a log line with no job/step/timestamp prefix
    ])
    assert github_run.failed_test_ids(REPO, 77, FakeGh(Clock(), red_view, log=log)) == [
        "tests/test_x.py::test_x[a b]", "tests/test_x.py::test_y[c d e]", "tests/test_z.py::test_bare"]


def test_a_red_run_carries_the_failed_ids_read_from_its_log():
    log = log_line("FAILED tests/test_a.py::test_one - AssertionError")
    result, gh, _ = poll(red_view, parts=2, log=log)
    assert result["outcome"] == "red-test"
    assert result["failed_ids"] == ["tests/test_a.py::test_one"]
    assert result["full_recheck"] is False
    assert ["run", "view", "77", "-R", REPO, "--log-failed"] in gh.calls


def test_a_red_part_without_failed_ids_is_a_full_recheck_on_the_pc():
    # a collection error: the pytest step failed, the log has no `FAILED` line
    log = log_line("ERROR collecting tests/test_a.py") + "\n" + log_line("1 error in 3.2s")
    result, _, _ = poll(red_view, parts=2, log=log)
    assert result["outcome"] == "red-test"
    assert result["failed_ids"] == []
    assert result["full_recheck"] is True


def test_an_unreadable_failed_log_is_a_full_recheck_too():
    clock = Clock()

    def gh(args):
        if "--log-failed" in args:
            raise RuntimeError("HTTP 502")
        return json.dumps(red_view(0))

    result = github_run.poll(REPO, 77, gh, parts=2, clock=clock, sleep=clock.sleep)
    assert result["outcome"] == "red-test" and result["failed_ids"] == [] and result["full_recheck"] is True


def test_other_outcomes_carry_no_failed_ids_and_no_full_recheck():
    result, _, _ = poll(green_view_factory([100]), parts=1)
    assert result["outcome"] == "green" and result["failed_ids"] == [] and result["full_recheck"] is False


# -- green, minutes, rate ----------------------------------------------------------------------------------

def green_view_factory(durations: list[float], finish_at: float = 400.0):
    def view(t):
        done = t >= finish_at
        jobs = [PLAN_OK]
        for n, seconds in enumerate(durations, start=1):
            if done:
                jobs.append(good_job(n, 10, seconds))
            else:
                jobs.append(job(f"test ({n})", 10, None))
        return {"status": "completed" if done else "in_progress", "conclusion": "success" if done else "",
                "jobs": jobs}

    return view


def test_green_run_of_five_parts_counts_ceil_minutes_per_part():
    durations = [61, 59, 120, 1, 301]  # ceil minutes 2 + 1 + 2 + 1 + 6 = 12, plus the plan job's 1 = 13
    result, _, _ = poll(green_view_factory(durations), parts=5)
    assert result["outcome"] == "green"
    assert [p["seconds"] for p in result["parts"]] == [61, 59, 120, 1, 301]
    assert [p["state"] for p in result["parts"]] == ["success"] * 5
    assert result["minutes_used"] == 13
    first = result["parts"][0]
    assert first["part"] == 1 and first["started"] == iso(10) and first["ended"] == iso(71)


def test_at_most_one_run_view_per_simulated_minute():
    result, gh, clock = poll(green_view_factory([100, 100], finish_at=400), parts=2)
    assert result["outcome"] == "green"
    assert len(gh.view_times) >= 5
    gaps = [b - a for a, b in zip(gh.view_times, gh.view_times[1:])]
    assert min(gaps) >= 60
    view_call = next(c for c in gh.calls if c[:2] == ["run", "view"])
    assert view_call == ["run", "view", "77", "-R", REPO, "--json", "status,conclusion,jobs"]


def test_minutes_used_counts_every_started_part_up_to_the_last_read_and_the_plan_job():
    # read at BASE: part 1 still running since 170 s ago (3 min), part 2 lost after 30 s (1 min), plan 20 s (1 min)
    def view(t):
        return {"status": "in_progress", "conclusion": "", "jobs": [
            job("plan", -200, -180, "success", PLAN_STEPS),
            job("test (1)", -170, None),
            job("test (2)", -170, -140, "failure", steps("Run actions/checkout@v4")),
            job("test (3)", None, None)]}

    result, _, _ = poll(view, parts=3)
    assert result["outcome"] == "lost"
    assert [(p["part"], p["state"], p["seconds"]) for p in result["parts"]] == [
        (1, "running", 170), (2, "lost", 30), (3, "queued", 0)]
    assert result["minutes_used"] == 3 + 1 + 0 + 1


def test_since_counts_the_start_bound_from_the_dispatch_not_from_the_first_read():
    def view(t):
        return {"status": "queued", "conclusion": "", "jobs": [PLAN_OK]}

    result, _, clock = poll(view, parts=5, since=-300.0)  # dispatched 5 minutes before the poll began
    assert result["outcome"] == "not-started"
    assert clock.t == 300.0


def test_interval_s_sets_the_gap_between_reads():
    def view(t):
        return {"status": "queued", "conclusion": "", "jobs": [PLAN_OK]}

    result, gh, clock = poll(view, parts=5, interval_s=30.0)
    assert result["outcome"] == "not-started"
    assert set(clock.sleeps) == {30.0}
    assert gh.view_times == [30.0 * n for n in range(21)]


def test_a_flaky_view_is_retried_next_minute_and_a_dead_one_raises():
    clock = Clock()
    seen = []

    def gh(args):
        seen.append(clock.t)
        if len(seen) == 1:
            raise RuntimeError("HTTP 502")
        return json.dumps(green_view_factory([100], finish_at=0)(clock.t))

    result = github_run.poll(REPO, 77, gh, parts=1, clock=clock, sleep=clock.sleep)
    assert result["outcome"] == "green" and seen == [0.0, 60.0]

    def dead(args):
        raise RuntimeError("HTTP 502")

    with pytest.raises(AgentOpsError):
        github_run.poll(REPO, 77, dead, clock=Clock(), sleep=Clock().sleep)


# -- dispatch -----------------------------------------------------------------------------------------------

def run_row(run_id: int, created: float, event: str = "workflow_dispatch") -> dict:
    return {"databaseId": run_id, "status": "queued", "createdAt": iso(created), "event": event}


def test_dispatch_runs_the_workflow_and_returns_the_run_created_after_it():
    clock = Clock()
    runs = [
        run_row(11, -600),  # an older run of the same branch
        run_row(13, 2),
        run_row(12, 1),
    ]
    gh = FakeGh(clock, None, list_runs=runs)
    run_id = github_run.dispatch(REPO, "i232", 5, gh, wall=lambda: BASE, sleep=clock.sleep)
    assert run_id == 12
    assert gh.calls[0] == ["workflow", "run", "tests.yml", "-R", REPO, "--ref", "i232",
                           "-f", "ref=i232", "-f", "parts=5"]
    assert gh.calls[1] == ["run", "list", "-R", REPO, "--workflow", "tests.yml", "--branch", "i232",
                           "--json", "databaseId,status,createdAt,event", "--limit", "5"]


def test_dispatch_with_two_runs_after_it_takes_the_workflow_dispatch_one_and_the_oldest():
    runs = [
        run_row(31, 9),                      # a later dispatch run
        run_row(32, 1, event="push"),        # a push run of the branch, created first
        run_row(30, 4),                      # the oldest workflow_dispatch run after the dispatch time - 5 s
        run_row(29, -6),                     # just before the 5 s skew window: an older run
    ]
    gh = FakeGh(Clock(), None, list_runs=runs)
    assert github_run.dispatch(REPO, "i232", 3, gh, wall=lambda: BASE, sleep=Clock().sleep) == 30
    runs = [run_row(40, -4), run_row(41, 3)]  # inside the skew window counts, and it is the oldest
    gh = FakeGh(Clock(), None, list_runs=runs)
    assert github_run.dispatch(REPO, "i232", 3, gh, wall=lambda: BASE, sleep=Clock().sleep) == 40


def test_dispatch_waits_for_the_run_to_appear_and_raises_when_it_never_does():
    clock = Clock()
    appeared = {"n": 0}

    def gh(args):
        if args[:2] == ["run", "list"]:
            appeared["n"] += 1
            if appeared["n"] < 3:
                return "[]"
            return json.dumps([run_row(5, 3)])
        return ""

    assert github_run.dispatch(REPO, "b", 1, gh, wall=lambda: BASE, sleep=clock.sleep) == 5
    assert len(clock.sleeps) == 2

    with pytest.raises(AgentOpsError):
        github_run.dispatch(REPO, "b", 1, lambda a: "[]", wall=lambda: BASE, sleep=Clock().sleep)

    only_push = lambda a: json.dumps([run_row(6, 2, event="push")]) if a[:2] == ["run", "list"] else ""
    with pytest.raises(AgentOpsError):
        github_run.dispatch(REPO, "b", 1, only_push, wall=lambda: BASE, sleep=Clock().sleep)


# -- the poll settles when every job is done, not when GitHub marks the run completed (#232 4f) -----------------

def jobs_done_view(parts_done: int = 5, failing: int | None = None, running: int | None = None, plan=PLAN_OK):
    """A run whose own status stays `in_progress` while its jobs are (all or some) done."""
    jobs = [plan]
    for n in range(1, parts_done + 1):
        if n == failing:
            jobs.append(job(f"test ({n})", 10, 130, "failure", steps("pytest part")))
        elif n == running:
            jobs.append(job(f"test ({n})", 10, None))
        else:
            jobs.append(good_job(n, 10, 100))
    return {"status": "in_progress", "conclusion": "", "jobs": jobs}


def test_all_jobs_done_but_run_in_progress_is_green_at_the_first_read():
    result, gh, clock = poll(lambda t: jobs_done_view(), parts=5)
    assert result["outcome"] == "green"
    assert len(gh.view_times) == 1 and clock.t == 0
    assert [p["state"] for p in result["parts"]] == ["success"] * 5


def test_all_jobs_done_with_one_failed_part_but_run_in_progress_is_red_test_with_ids():
    log = log_line("FAILED tests/test_x.py::test_a")
    result, gh, clock = poll(lambda t: jobs_done_view(failing=3), log=log, parts=5)
    assert result["outcome"] == "red-test"
    assert result["failed_ids"] == ["tests/test_x.py::test_a"]
    assert clock.t == 0


@pytest.mark.parametrize("view_now", [
    jobs_done_view(parts_done=4),                        # 4 of 5 parts present
    jobs_done_view(running=5),                           # one part still in progress
    jobs_done_view(plan=job("plan", 1, None)),           # the plan job still running
])
def test_run_in_progress_with_jobs_not_all_done_keeps_polling_until_the_run_completes(view_now):
    def view(t):
        if t < 120:
            return view_now
        return {**jobs_done_view(), "status": "completed", "conclusion": "success"}

    result, gh, clock = poll(view, parts=5)
    assert result["outcome"] == "green"
    assert clock.t >= 120 and len(gh.view_times) > 1


def test_run_in_progress_with_unknown_part_count_keeps_polling_until_the_run_completes():
    def view(t):
        if t < 120:
            return jobs_done_view()
        return {**jobs_done_view(), "status": "completed", "conclusion": "success"}

    result, gh, clock = poll(view, parts=None)
    assert result["outcome"] == "green"
    assert clock.t >= 120


def test_jobs_never_all_done_and_run_never_completed_is_lost_at_the_run_timeout():
    result, _, clock = poll(lambda t: jobs_done_view(running=5), parts=5)
    assert result["outcome"] == "lost"
    assert clock.t >= github_run.RUN_TIMEOUT_S
