"""#352 unit 1: the start-to-first-test time of GitHub machines, read in the same `gh run view` as a run's record.

No test calls `gh`: the fixtures are the JSON shape of `gh run view --json createdAt,jobs`, the poll's fake `gh` replays
it, and the count script's `gh` is a fake subprocess runner.
"""
from __future__ import annotations

import importlib.util
import json
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.agentops import core_settings, github_run
from tools.agentops import github_suite as gs

ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
ZERO = "0001-01-01T00:00:00Z"


def iso(seconds: float) -> str:
    return (BASE + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def step(name: str, started: float | None) -> dict:
    return {"name": name, "status": "completed", "conclusion": "success",
            "startedAt": iso(started) if started is not None else ZERO, "completedAt": iso((started or 0) + 1)}


def job(name: str, *, started: float, steps: list[dict]) -> dict:
    return {"name": name, "status": "completed", "conclusion": "success", "startedAt": iso(started),
            "completedAt": iso(started + 300), "steps": steps}


def part_job(name: str, pytest_at: float | None, *, started: float = 0.0) -> dict:
    """A test job whose steps are the workflow's: checkout, setup, a pip install line, then the pytest step."""
    return job(name, started=started, steps=[
        step("Set up job", started), step("actions/checkout@v4", started), step("Run pip install pytest pytest-xdist", started),
        step("pytest part" if "windows" not in name else "pytest PC-only part", pytest_at),
        step("Post actions/checkout@v4", pytest_at),
    ])


def plan_job() -> dict:
    return job("plan", started=5, steps=[step("Set up job", 5), step("matrix", 6)])


# --- the pure function ----------------------------------------------------------------------------------------------

def test_median_per_kind_of_the_jobs_and_the_plan_job_is_no_part():
    run = {"createdAt": iso(0), "jobs": [
        plan_job(),
        part_job("test (1)", 90),
        part_job("test (2)", 120),
        part_job("test (3)", 60),
        part_job("test (windows)", 240),
    ]}
    assert github_run.start_to_first_test_seconds(run) == {"linux": 90, "windows": 240}


def test_even_count_of_linux_jobs_takes_the_ceiling_of_the_middle_pair():
    run = {"createdAt": iso(0), "jobs": [part_job("test (1)", 60), part_job("test (2)", 90),
                                         part_job("test (3)", 120), part_job("test (4)", 240)]}
    assert github_run.start_to_first_test_seconds(run) == {"linux": 105, "windows": None}


def test_a_job_that_never_reached_its_pytest_step_is_left_out_and_no_job_gives_none():
    run = {"createdAt": iso(0), "jobs": [part_job("test (1)", 90), part_job("test (2)", None)]}
    assert github_run.start_to_first_test_seconds(run) == {"linux": 90, "windows": None}
    assert github_run.start_to_first_test_seconds({"createdAt": iso(0), "jobs": [plan_job()]}) == {
        "linux": None, "windows": None}


def test_the_install_step_is_not_the_first_test():
    job_one = job("test (1)", started=0, steps=[step("Run pip install pytest pytest-xdist", 30),
                                                 step("pytest part", 75)])
    assert github_run.start_to_first_test_seconds({"createdAt": iso(0), "jobs": [job_one]}) == {
        "linux": 75, "windows": None}


# --- poll: the same read carries createdAt and the record's start_seconds --------------------------------------------

def test_poll_reads_created_at_and_returns_start_seconds_of_the_same_read():
    view = {"status": "completed", "conclusion": "success", "createdAt": iso(0), "jobs": [
        plan_job(), part_job("test (1)", 90), part_job("test (2)", 120)]}
    calls: list[list[str]] = []

    def gh(args, input=None):  # noqa: A002 - the seam's own name
        calls.append(list(args))
        return json.dumps(view)

    result = github_run.poll("owner/copy", 77, gh, parts=2, clock=lambda: 0.0, sleep=lambda s: None,
                             wall=lambda: BASE + timedelta(seconds=300))
    assert result["outcome"] == "green"
    assert result["start_seconds"] == {"linux": 105, "windows": None}
    assert calls[0] == ["run", "view", "77", "-R", "owner/copy", "--json", "status,conclusion,jobs,createdAt"]


# --- the run row --------------------------------------------------------------------------------------------------

class Recorder:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def record(self, rec: dict) -> None:
        self.rows.append(rec)


def test_record_row_carries_start_seconds_per_kind():
    deps = Recorder()
    gs._record(deps, gs.GithubSuiteResult(status="green", run_id=77,
                                          start_seconds={"linux": 90, "windows": None}), "orch", "me/repo", True)
    assert deps.rows[0]["start_seconds"] == {"linux": 90, "windows": None}
    assert deps.rows[0]["minutes"] == 0 and deps.rows[0]["run_id"] == 77


def test_record_row_without_a_read_has_null_kinds():
    deps = Recorder()
    gs._record(deps, gs.GithubSuiteResult(status="fallback", reason="poll-failed"), "orch", "me/repo", True)
    assert deps.rows[0]["start_seconds"] == {"linux": None, "windows": None}


# --- settings: DEFAULTS and the repo's config carry the measured medians -------------------------------------------

def test_settings_defaults_match_the_repos_config_and_are_whole_seconds():
    config = json.loads((ROOT / "config" / "orchestration.json").read_text(encoding="utf-8"))
    for key in ("github_start_seconds_linux", "github_start_seconds_windows"):
        assert type(core_settings.DEFAULTS[key]) is int and core_settings.DEFAULTS[key] > 0
        assert config[key] == core_settings.DEFAULTS[key]


# --- the count script (scripts/counts/github_start.py) with a fake subprocess ----------------------------------------

def load_script():
    spec = importlib.util.spec_from_file_location("github_start", ROOT / "scripts" / "counts" / "github_start.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_minutes_text_is_seconds_under_ninety_and_minutes_and_seconds_from_ninety():
    script = load_script()
    assert script.minutes_text(16) == "16 s"
    assert script.minutes_text(89) == "89 s"
    assert script.minutes_text(90) == "1 min 30 s"
    assert script.minutes_text(600) == "10 min 0 s"


def test_the_repo_read_is_the_test_copy_or_the_own_repo_and_none_is_a_refusal():
    script = load_script()
    assert script.runs_repo({"test_copy_remote": "https://github.com/a/b.git", "repo": "me/private"}) == "a/b"
    assert script.runs_repo({"test_copy_remote": "", "repo": "me/private", "github_own_runners": True}) == "me/private"
    assert script.runs_repo({"repo": "me/private"}) == "me/private"
    try:
        script.runs_repo({"github_own_runners": True})
    except ValueError:
        pass
    else:
        raise AssertionError("no repo must raise")


def test_a_repo_without_a_test_copy_reads_its_own_runs_and_says_so(tmp_path, capsys):
    script = load_script()
    config = tmp_path / "orchestration.json"
    config.write_text('{\n  "repo": "me/private",\n  "github_own_runners": true\n}\n', encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[:3] == ["gh", "run", "list"]:
            return types.SimpleNamespace(stdout=json.dumps([{"databaseId": 9, "createdAt": iso(0)}]), returncode=0)
        return types.SimpleNamespace(stdout=json.dumps({"createdAt": iso(0), "jobs": [part_job("test (1)", 45)]}),
                                     returncode=0)

    assert script.main(["--last", "1"], config=config, run=fake_run, log=tmp_path / "none.jsonl") == 0
    out = capsys.readouterr().out
    assert "me/private" in out and "own repo" in out and "linux: median 45 s" in out
    assert all("me/private" in argv for argv in calls)


def test_rows_without_start_seconds_stay_readable_and_nulls_count_nothing(tmp_path):
    script = load_script()
    log = tmp_path / "github_runs.jsonl"
    log.write_text("\n".join([
        json.dumps({"repo": "orch", "minutes": 3, "own_runner": True, "run_id": 1}),  # old row, no key
        json.dumps({"repo": "orch", "minutes": 3, "run_id": 2, "start_seconds": {"linux": 90, "windows": None}}),
        json.dumps({"repo": "orch", "minutes": 3, "run_id": 3, "start_seconds": {"linux": None, "windows": 200}}),
        "not json",
    ]) + "\n", encoding="utf-8")
    rows = script.read_rows(log)
    assert rows == [(2, {"linux": 90, "windows": None}), (3, {"linux": None, "windows": 200})]


def test_gh_is_read_only_when_the_rows_hold_ten_per_kind():
    script = load_script()
    rows = [(i, {"linux": 60 + i, "windows": 200 + i}) for i in range(10)]

    def fetch():
        raise AssertionError("gh must not be read when the rows reach 10 per kind")

    samples = script.collect(rows, fetch, need=10)
    assert len(samples["linux"]) == 10 and len(samples["windows"]) == 10


def test_gh_is_read_for_the_kind_short_of_ten_and_runs_are_not_counted_twice():
    script = load_script()
    rows = [(i, {"linux": 60, "windows": None}) for i in range(10)]
    calls = []

    def fetch():
        calls.append(1)
        return [(5, {"linux": 60, "windows": 240}), (500, {"linux": 90, "windows": 250})]

    samples = script.collect(rows, fetch, need=10)
    assert calls == [1]
    assert [run_id for run_id, _ in samples["linux"]].count(5) == 1
    assert [run_id for run_id, _ in samples["windows"]] == [5, 500]


def test_summary_gives_median_count_and_run_ids_per_kind():
    script = load_script()
    out = script.summary({"linux": [(1, 60), (2, 90), (3, 120)], "windows": []})
    assert out["linux"] == {"median": 90, "count": 3, "runs": [1, 2, 3]}
    assert out["windows"] == {"median": None, "count": 0, "runs": []}


def test_fetch_runs_reads_the_last_runs_with_gh_and_a_sixty_second_timeout():
    script = load_script()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[:3] == ["gh", "run", "list"]:
            return types.SimpleNamespace(stdout=json.dumps([{"databaseId": 9, "createdAt": iso(0)},
                                                             {"databaseId": 8, "createdAt": iso(0)}]), returncode=0)
        run_id = argv[3]
        jobs = [plan_job(), part_job("test (1)", 90), part_job("test (windows)", 240)]
        return types.SimpleNamespace(stdout=json.dumps({"createdAt": iso(0), "jobs": jobs}), returncode=0)

    rows = script.fetch_runs("owner/copy", fake_run, limit=30)
    assert rows == [(9, {"linux": 90, "windows": 240}), (8, {"linux": 90, "windows": 240})]
    assert calls[0][0][:3] == ["gh", "run", "list"] and "30" in calls[0][0]
    assert all(kwargs["timeout"] == 60 for _, kwargs in calls)
    assert calls[1][0][:4] == ["gh", "run", "view", "9"]


def test_write_sets_both_keys_in_the_config_text_and_keeps_the_rest(tmp_path):
    script = load_script()
    config = tmp_path / "orchestration.json"
    config.write_text('{\n  "repo": "x",\n  "github_suite": true,\n  "test_copy_remote": "https://e/x.git",\n'
                      '  "merge_suite": "python -m pytest tests -q"\n}\n', encoding="utf-8")
    script.write_settings(config, {"linux": 90, "windows": 240})
    script.write_settings(config, {"linux": 95, "windows": 240})  # a second write replaces, never duplicates
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["github_start_seconds_linux"] == 95 and data["github_start_seconds_windows"] == 240
    assert data["repo"] == "x" and data["merge_suite"] == "python -m pytest tests -q"
    assert config.read_text(encoding="utf-8").count("github_start_seconds_linux") == 1


def test_write_keeps_a_last_member_key_valid_json(tmp_path):
    script = load_script()
    config = tmp_path / "orchestration.json"
    config.write_text('{\n  "repo": "x",\n  "github_start_seconds_linux": 90\n}\n', encoding="utf-8")
    script.write_settings(config, {"linux": 95, "windows": 240})
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data == {"repo": "x", "github_start_seconds_linux": 95, "github_start_seconds_windows": 240}


def test_write_adds_the_keys_after_a_last_test_copy_remote_line(tmp_path):
    script = load_script()
    config = tmp_path / "orchestration.json"
    config.write_text('{\n  "repo": "x",\n  "test_copy_remote": "https://e/x.git"\n}\n', encoding="utf-8")
    script.write_settings(config, {"linux": 16, "windows": 26})
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data == {"repo": "x", "test_copy_remote": "https://e/x.git",
                    "github_start_seconds_linux": 16, "github_start_seconds_windows": 26}


def test_write_without_a_test_copy_line_puts_the_keys_under_the_opening_brace(tmp_path):
    script = load_script()
    config = tmp_path / "orchestration.json"
    config.write_text('{\n  "repo": "x",\n  "github_own_runners": true\n}\n', encoding="utf-8")
    script.write_settings(config, {"linux": 16, "windows": 26})
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data == {"repo": "x", "github_own_runners": True,
                    "github_start_seconds_linux": 16, "github_start_seconds_windows": 26}
