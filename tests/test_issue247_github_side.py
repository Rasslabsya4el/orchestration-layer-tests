"""#247 unit 1: the GitHub side carries `ids`, `tests` and `windows` (fakes only, no network)."""
from __future__ import annotations

import base64
import gzip
import json

import pytest

from tests.test_issue232_github_run import BASE, FakeGh, Clock, REPO, good_job, job, PLAN_OK, poll, steps
from tests.test_issue232_github_suite import COPY, SETTINGS, Fakes, run
from tools.agentops import github_run, github_suite as gs, merge_github, workflow_sync
from datetime import timedelta


def _dispatch(**kw):
    clock = Clock()
    created = (BASE + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    gh = FakeGh(clock, lambda t: {}, list_runs=[{"databaseId": 9, "status": "queued", "createdAt": created,
                                                  "event": "workflow_dispatch"}])
    run_id = github_run.dispatch(REPO, "b", 3, gh, wall=lambda: BASE, sleep=clock.sleep, **kw)
    assert run_id == 9
    at = next(i for i, c in enumerate(gh.calls) if c[:2] == ["workflow", "run"])
    assert gh.calls[at] == ["workflow", "run", "tests.yml", "-R", REPO, "--ref", "b", "--json"]
    return json.loads(gh.stdins[at])  # #267: the inputs travel as JSON on stdin


def test_dispatch_without_ids_or_windows_sends_the_old_args_exactly():
    assert _dispatch() == {"ref": "b", "parts": "3"}
    assert _dispatch(ids=[], windows=False) == _dispatch()
    assert _dispatch(ids=None) == _dispatch()


def test_dispatch_ids_round_trip_through_base64_gzip():
    ids = ["tests/a.py::test_x[1-2]", "tests/b.py::test_y"]
    inputs = _dispatch(ids=ids)
    assert set(inputs) == {"ref", "parts", "ids"}
    raw = inputs["ids"]
    assert "\n" not in raw
    assert gzip.decompress(base64.b64decode(raw, validate=True)).decode("utf-8") == "\n".join(ids)
    assert {k: v for k, v in inputs.items() if k != "ids"} == _dispatch()


def test_dispatch_windows_sends_windows_true():
    inputs = _dispatch(windows=True)
    assert inputs == {**_dispatch(), "windows": "true"}


@pytest.mark.parametrize("tests, per_merge, want", [(4430, 5, 5), (1000, 5, 2), (179, 5, 1), (0, 5, 1),
                                                    (None, 5, 5), (4430, 3, 3), (900, 5, 1), (901, 5, 2)])
def test_machines_for(tests, per_merge, want):
    assert gs.machines_for(tests, per_merge) == want


def test_suite_asks_machines_by_test_count_and_passes_ids_and_windows():
    f = Fakes(machines=2)
    r = gs.run_github_suite(root="C:/x/repo", branch="b", repo_slug="me/repo", remote=COPY, repo_name="orch",
                            priority=False, settings=SETTINGS, deps=f.deps(), tests=1000, ids=["t.py::a"],
                            windows=True)
    assert r.status == "green"
    assert next(c for c in f.calls if c[0] == "acquire")[3] == 2
    assert f.dispatch_kw == {"ids": ["t.py::a"], "windows": True}
    assert f.poll_kw.get("windows") is True and f.poll_kw.get("parts") == 2


def test_suite_without_new_inputs_wants_per_merge_and_adds_nothing_to_the_calls():
    f = Fakes(machines=2)
    run(f)
    assert next(c for c in f.calls if c[0] == "acquire")[3] == 5
    assert f.dispatch_kw == {} and "windows" not in f.poll_kw


def _view(windows_job, done=True):
    return {"status": "completed" if done else "in_progress", "conclusion": "success" if done else "",
            "jobs": [PLAN_OK, good_job(1, 10, 100), good_job(2, 10, 120), windows_job]}


def test_red_windows_job_is_red_with_its_failed_ids():
    win = job("test (windows)", 10, 200, "failure", steps(fail="pytest part"))
    log = "test (windows)\tpytest part\t2026-10-19T12:03:00Z FAILED tests/test_win.py::test_w - boom\n"
    result, _, _ = poll(lambda t: _view(win), log=log, parts=2, windows=True)
    assert result["outcome"] == "red-test"
    assert result["failed_ids"] == ["tests/test_win.py::test_w"]
    assert [p["part"] for p in result["parts"]] == [1, 2, "windows"]
    assert result["parts"][2]["seconds"] == 190


def test_green_needs_every_part_including_windows():
    ok = job("test (windows)", 10, 200, "success", steps())
    result, _, _ = poll(lambda t: _view(ok), parts=2, windows=True)
    assert result["outcome"] == "green" and len(result["parts"]) == 3
    # the windows job is missing: two parts alone are not a green run
    missing = {"status": "completed", "conclusion": "success", "jobs": [PLAN_OK, good_job(1, 10, 100), good_job(2, 10, 120)]}
    result, _, _ = poll(lambda t: missing, parts=2, windows=True)
    assert result["outcome"] == "lost"


def test_suite_passes_a_red_windows_run_through_with_part_seconds():
    f = Fakes(poll={"run_id": 77, "parts": [{"part": 1, "seconds": 5}, {"part": "windows", "seconds": 190}],
                    "minutes_used": 5, "outcome": "red-test", "failed_ids": ["w"], "full_recheck": False})
    r = gs.run_github_suite(root="C:/x/repo", branch="b", repo_slug="me/repo", remote=COPY, repo_name="orch",
                            priority=False, settings=SETTINGS, deps=f.deps(), windows=True)
    assert (r.status, r.failed_ids, r.part_seconds) == ("red", ["w"], [5, 190])


def test_merge_run_passes_tests_ids_windows_through(tmp_path):
    seen = {}

    class D(gs.Deps):
        pass

    def fake_suite(**kw):
        seen.update(kw)
        return gs.GithubSuiteResult(status="green")

    real = gs.run_github_suite
    gs.run_github_suite = fake_suite
    try:
        plan = merge_github.Plan(remote=COPY, own_repo=False, repo_slug="me/repo", repo_name="orch")
        merge_github.run(plan, cwd=tmp_path, suite_dir=tmp_path, sha="s", branch="b", priority=False,
                         settings=SETTINGS, alive=lambda: None, output_path=tmp_path / "o", every=1,
                         tests=10, ids=["x"], windows=True)
        assert (seen["tests"], seen["ids"], seen["windows"]) == (10, ["x"], True)
        seen.clear()
        merge_github.run(plan, cwd=tmp_path, suite_dir=tmp_path, sha="s", branch="b", priority=False,
                         settings=SETTINGS, alive=lambda: None, output_path=tmp_path / "o", every=1)
        assert (seen["tests"], seen["ids"], seen["windows"]) == (None, None, False)
    finally:
        gs.run_github_suite = real


@pytest.fixture(autouse=True)
def _workflow_files_in_sync(monkeypatch):
    """#343 rule 1: a dispatch first makes the workflow files exist on the default branch. The tests here are about the
    dispatch's own calls, so the sync is answered as done (tests/test_issue343_workflow_sync.py tests the sync itself)."""
    monkeypatch.setattr(workflow_sync, "prepare_dispatch", lambda *args, **kwargs: [])
