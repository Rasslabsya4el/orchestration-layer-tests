"""#281 rule 3 (counter part): on the own runners a Windows part's minutes count twice (GitHub bills a private repo's
Windows minutes at 2x). The weighting sits at the record (`github_suite._record`), so `minutes` in
`github_runs.jsonl` is billed minutes; `minutes_used`, `median_merge_minutes` and `over_budget` read it as before.

Seams: `github_run.poll` (a fake `gh` replaying `gh run view --json`), `github_suite.run_github_suite` (fake deps; the
record is captured), `github_minutes.over_budget` / `minutes_used` (a log file in tmp_path).
"""
from __future__ import annotations

import json
import types
from datetime import datetime, timedelta, timezone

import pytest

from tools.agentops import github_minutes as gm
from tools.agentops import github_run
from tools.agentops import github_suite as gs

BASE = datetime(2026, 10, 21, 10, 0, 0, tzinfo=timezone.utc)
SETTINGS = {"github_machines_per_merge": 5, "github_machines_total": 20}
OWN_SLUG = "owner/private-repo"
COPY = "https://github.com/owner/copy-repo.git"


@pytest.fixture(autouse=True)
def mdir(tmp_path, monkeypatch):
    path = tmp_path / "machine"
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(path))
    return path


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def iso(seconds: float) -> str:
    return (BASE + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def job(name: str, seconds: float) -> dict:
    """One finished, green job of `gh run view --json jobs`, started at BASE."""
    return {"name": name, "status": "completed", "conclusion": "success", "startedAt": iso(0),
            "completedAt": iso(seconds), "steps": []}


def view_linux_and_windows(linux_seconds: float, windows_seconds: float) -> dict:
    return {"status": "completed", "conclusion": "success",
            "jobs": [job("test (1)", linux_seconds), job("test (windows)", windows_seconds)]}


def poll_run(view: dict) -> dict:
    """The run result `github_run.poll` returns for a run whose jobs are `view` (one Linux part, one Windows part)."""
    return github_run.poll(OWN_SLUG, 9, lambda args, input=None: json.dumps(view), parts=1, windows=True,
                           clock=lambda: 0.0, sleep=lambda seconds: None, wall=lambda: BASE + timedelta(hours=1))


class Berth:
    machines = 2

    def release(self):
        pass


def run_suite(polled: dict, *, own_repo: bool):
    """One merge's suite through `run_github_suite` with the given run result; returns (result, records)."""
    records: list[dict] = []
    deps = gs.Deps(
        send=lambda root, branch, remote: types.SimpleNamespace(sent=True, reason=None),
        acquire_machines=lambda *a, **kw: Berth(),
        dispatch=lambda *a, **kw: 9,
        poll=lambda *a, **kw: polled,
        record=records.append,
        push=lambda *a, **kw: None,
        clock=lambda: 0.0,
        failed_log=lambda copy, run_id: "",  # #341: no real gh run view in a test
    )
    result = gs.run_github_suite(root="C:/x/repo", branch="i281u1", repo_slug=OWN_SLUG,
                                 remote="" if own_repo else COPY, repo_name="demo-repo", priority=False,
                                 settings=SETTINGS, deps=deps, own_repo=own_repo, windows=True)
    return result, records


def write_log(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


def row(repo, month, minutes, **extra):
    return {"time": f"{month}-15T12:00:00+00:00", "repo": repo, "slug": f"owner/{repo}", "month": month,
            "minutes": minutes, "machines": 1, "outcome": "passed", "run_id": 1, "own_runner": True, **extra}


def test_row1_one_linux_part_and_one_windows_part_of_10_min_record_30_billed_minutes():
    polled = poll_run(view_linux_and_windows(600, 600))
    assert [part["part"] for part in polled["parts"]] == [1, "windows"]
    assert polled["minutes_used"] == 20  # the run's wall minutes, unchanged
    result, records = run_suite(polled, own_repo=True)
    assert result.status == "green"
    assert len(records) == 1 and records[0]["own_runner"] is True
    assert records[0]["minutes"] == 30


def test_row1_a_copy_run_with_the_same_parts_is_recorded_at_wall_minutes(tmp_path):
    """The public test copy's Windows minutes are free and never counted: its line keeps the wall minutes."""
    polled = poll_run(view_linux_and_windows(600, 600))
    result, records = run_suite(polled, own_repo=False)
    assert result.status == "green"
    assert records[0]["own_runner"] is False and records[0]["minutes"] == 20


def test_row1_the_default_record_writes_the_billed_minutes_into_the_counted_log(mdir):
    """No fake record: the line the counter reads carries the 2x Windows minutes (30), and the counter counts it."""
    polled = poll_run(view_linux_and_windows(600, 600))
    deps = gs.Deps(
        send=lambda root, branch, remote: types.SimpleNamespace(sent=True, reason=None),
        acquire_machines=lambda *a, **kw: Berth(),
        dispatch=lambda *a, **kw: 9,
        poll=lambda *a, **kw: polled,
        push=lambda *a, **kw: None,
        clock=lambda: 0.0,
        failed_log=lambda copy, run_id: "",  # #341: no real gh run view in a test
    )
    gs.run_github_suite(root="C:/x/repo", branch="i281u1", repo_slug=OWN_SLUG, remote="", repo_name="demo-repo",
                        priority=False, settings=SETTINGS, deps=deps, own_repo=True, windows=True)
    line = json.loads((mdir / "github_runs.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert line["minutes"] == 30
    assert gm.minutes_used("demo-repo", line["month"]) == 30


def test_row2_1990_used_and_median_15_is_over_budget(tmp_path):
    log = tmp_path / "runs.jsonl"
    write_log(log, [row("U", "2026-10", 1840)] + [row("U", "2026-10", 15) for _ in range(10)])
    assert gm.minutes_used("U", "2026-10", path=log) == 1990
    assert gm.median_merge_minutes("U", path=log) == 15
    assert gm.over_budget("U", now=utc(2026, 10, 21, 12), limit=2000, path=log) == (
        True,
        "1990 used + median 15 > 2000 until 2026-11-01",
    )


def test_row3_1980_used_and_median_15_is_1995_and_under_budget(tmp_path):
    log = tmp_path / "runs.jsonl"
    write_log(log, [row("U", "2026-10", 1830)] + [row("U", "2026-10", 15) for _ in range(10)])
    assert gm.minutes_used("U", "2026-10", path=log) == 1980
    assert gm.median_merge_minutes("U", path=log) == 15
    assert gm.over_budget("U", now=utc(2026, 10, 21, 12), limit=2000, path=log) == (False, "")
