"""#345 unit 2: every fallback of the merge's GitHub step carries a non-empty reason, and a reason that a job was read for
names that job (``<code>: job <name> <conclusion or "no conclusion">; ids found: <n>``).

Fakes only (no network, no git push, no machines). Each path that returns a fallback, or that goes to the PC after a
red run, is walked here: the send, the refusal, the machine line, the dispatch, the poll, not-started, lost, a red that
names no test, and the merge's own error path. The place seam is ``merge_github.pc_place_text(merge_github.pc_reason(r))``,
the value merge.py gives ``run.place``, which reaches merge.log through the existing place plumbing.
"""
from __future__ import annotations

import json
import re

import pytest

from tests.test_issue232_wiring import BAD, Fakes, World, red
from tools.agentops import github_suite as gs
from tools.agentops import merge_github
from tools.agentops import merge_status as ms
from tools.agentops.test_copy_send import SendResult

COPY = "https://github.com/owner/copy-repo.git"
SETTINGS = {"github_machines_per_merge": 5, "github_machines_total": 20}
PLAN = merge_github.Plan(remote=COPY, own_repo=False, repo_slug="me/repo", repo_name="orch")

JOB_NO_CONCL = {"name": "test (2)", "conclusion": "", "failed_ids": []}
JOB_CANCELLED = {"name": "test (windows)", "conclusion": "cancelled", "failed_ids": []}
JOB_OK = {"name": "test (1)", "conclusion": "success", "failed_ids": []}
JOBS_LOST = [JOB_NO_CONCL, JOB_CANCELLED, JOB_OK]


def polled(outcome, jobs=(), failed_ids=()):
    """The record ``github_run.poll`` returns (#345 unit 1 adds ``jobs``)."""
    return {"run_id": 77, "parts": [], "minutes_used": 2, "outcome": outcome, "failed_ids": list(failed_ids),
            "full_recheck": outcome == "red-test" and not failed_ids, "jobs": list(jobs)}


def _acquire(repo, run, want=None, **kw):
    class Berth:
        machines = 3

        def release(self):
            pass
    return Berth()


def _raiser(text):
    def raise_(*args, **kw):
        raise RuntimeError(text)
    return raise_


def _no_log(copy, run_id):
    """The red run's `gh run view --log-failed` log, answered empty: no ids in it, the reason comes from the jobs."""
    return ""


def deps_with(**over):
    records = []
    base = dict(send=lambda root, branch, remote: SendResult(True, "abc"), acquire_machines=_acquire,
                dispatch=lambda repo, branch, parts, *a, **kw: 77,
                poll=lambda repo, run_id, *a, **kw: polled("green"), record=records.append,
                push=lambda *a: None, clock=lambda: 1234.5, in_tree=lambda root, branch, path: True,
                failed_log=_no_log)  # a red run's log is read only with no failed id: a fake, never `gh run view`
    base.update(over)
    return gs.Deps(**base), records


def suite(deps, branch="i345-reasons"):
    return gs.run_github_suite(root="C:/x/repo", branch=branch, repo_slug="me/repo", remote=COPY, repo_name="orch",
                               priority=False, settings=SETTINGS, deps=deps)


# -- paths with no job yet: the reason they already had is kept ---------------------------------------------------

@pytest.mark.parametrize("over, expected", [
    pytest.param({"send": _raiser("no network")}, "send-failed", id="send-failed"),
    pytest.param({"send": lambda root, branch, remote: SendResult(False, None, "test copy: no remote set")},
                 "refused: test copy: no remote set", id="refused-with-reason"),
    pytest.param({"send": lambda root, branch, remote: SendResult(False, None)}, "refused", id="refused-bare"),
    pytest.param({"acquire_machines": _raiser("line closed")}, "no-places", id="no-places"),
    pytest.param({"dispatch": _raiser("gh: boom\nsecond line")}, "dispatch-failed: gh: boom", id="dispatch-failed"),
    pytest.param({"poll": _raiser("gh run view failed")}, "poll-failed", id="poll-failed"),
])
def test_a_fallback_with_no_job_yet_keeps_its_reason(over, expected):
    deps, records = deps_with(**over)
    result = suite(deps)
    assert result.status == "fallback"
    assert result.reason == expected and result.reason.strip()
    assert result.brain_line.endswith(f"GitHub {expected}, suite on the PC")
    assert records[-1]["outcome"] == expected.partition(":")[0]


# -- not-started and lost: the job the reader saw is named --------------------------------------------------------

def test_not_started_with_no_job_read_keeps_the_bare_code():
    deps, records = deps_with(poll=lambda *a, **kw: polled("not-started"))
    result = suite(deps)
    assert (result.status, result.reason) == ("fallback", "not-started")
    assert records[-1]["outcome"] == "not-started"


def test_not_started_names_the_job_it_read():
    deps, records = deps_with(poll=lambda *a, **kw: polled("not-started", jobs=[JOB_NO_CONCL]))
    result = suite(deps)
    assert result.reason == "not-started: job test (2) no conclusion; ids found: 0"
    assert records[-1]["outcome"] == "not-started"


def test_lost_names_every_job_that_is_not_success():
    deps, records = deps_with(poll=lambda *a, **kw: polled("lost", jobs=JOBS_LOST))
    result = suite(deps)
    assert result.status == "fallback"
    assert result.reason == "lost: job test (2) no conclusion, job test (windows) cancelled; ids found: 0"
    assert "job test (2) no conclusion" in result.reason and "job test (windows) cancelled" in result.reason
    assert records[-1]["outcome"] == "lost"
    assert "job test (windows) cancelled" in result.brain_line


def test_lost_with_every_job_read_as_success_says_so():
    all_ok = [{"name": f"test ({i})", "conclusion": "success", "failed_ids": []} for i in (1, 2, 3)]
    deps, _ = deps_with(poll=lambda *a, **kw: polled("lost", jobs=all_ok))
    assert suite(deps).reason == "lost: 3 jobs read, all success; ids found: 0"


def test_the_place_of_a_lost_job_names_the_job():
    deps, _ = deps_with(poll=lambda *a, **kw: polled("lost", jobs=JOBS_LOST))
    result = suite(deps)
    place = merge_github.pc_place_text(merge_github.pc_reason(result))
    assert place == ("suite on the PC (GitHub lost: job test (2) no conclusion, job test (windows) cancelled; "
                     "ids found: 0)")
    assert "job test (windows) cancelled" in merge_github.brain_line_for(PLAN, "#345", result)


def test_a_long_job_list_is_cut_at_300_characters_and_counts_the_rest():
    jobs = [{"name": f"test ({'w' * 30}{i})", "conclusion": "failure", "failed_ids": []} for i in range(12)]
    reason = gs.job_reason("lost", jobs)
    assert len(reason) <= 300
    listed = reason.count("job test (")
    more = int(re.search(r"and (\d+) more; ids found: 0$", reason).group(1))
    assert listed >= 1 and listed + more == 12


# -- a red run that names no test goes to the PC, and says which job it read ---------------------------------------

def test_red_without_ids_names_its_job_on_the_pc():
    failed = {"name": "test (windows)", "conclusion": "failure", "failed_ids": []}
    deps, _ = deps_with(poll=lambda *a, **kw: polled("red-test", jobs=[failed]))
    result = suite(deps)
    assert result.status == "red" and merge_github.goes_to_pc(result)
    assert merge_github.pc_reason(result) == "red: job test (windows) failure; ids found: 0"


def test_red_whose_ids_are_not_in_the_tree_names_the_job_and_the_ids_it_found():
    ids = ["tests/test_gone.py::test_a"]
    failed = {"name": "test (windows)", "conclusion": "failure", "failed_ids": ids}
    deps, _ = deps_with(poll=lambda *a, **kw: polled("red-test", jobs=[failed], failed_ids=ids),
                        in_tree=lambda root, branch, path: False)
    result = suite(deps)
    assert result.status == "red" and result.full_recheck and result.failed_ids == []
    assert merge_github.pc_reason(result) == "red: job test (windows) failure; ids found: 1"


def test_red_with_no_job_read_still_gives_a_reason():
    deps, _ = deps_with(poll=lambda *a, **kw: polled("red-test"))
    result = suite(deps)
    assert merge_github.goes_to_pc(result)
    assert merge_github.pc_reason(result) == "red: no job read; ids found: 0"


# -- the merge's own error path: any exception becomes ``error: <class>: <first line>`` ----------------------------

@pytest.mark.parametrize("exc, expected", [
    pytest.param(RuntimeError("gh exploded\nsecond line"), "error: RuntimeError: gh exploded", id="first-line"),
    pytest.param(ValueError(), "error: ValueError", id="no-message"),
    pytest.param(OSError("x" * 400), ("error: OSError: " + "x" * 400)[:300], id="cut-at-300"),
])
def test_the_merge_error_path_names_the_exception(monkeypatch, tmp_path, exc, expected):
    def boom(**kw):
        raise exc
    monkeypatch.setattr(gs, "run_github_suite", boom)
    res = merge_github.run(PLAN, cwd=tmp_path, suite_dir=tmp_path, sha="x", branch="origin/feat", priority=False,
                           settings={}, alive=lambda: None, output_path=tmp_path / "out.txt", every=0.1)
    assert res.status == "fallback" and res.reason == expected
    assert res.reason.strip() and len(res.reason) <= 300
    assert merge_github.pc_reason(res) == expected


# -- the merge's place reaches merge-status: the landed row and its rendered line (#345 review 1) --------------------

T0 = 1_800_000_000.0
FALLBACK_PLACE = "suite on the PC (GitHub lost: job test (windows) cancelled; ids found: 0)"


def _landed_file(root, name, rc, reason, place):
    """A merge's result file as merge.py writes it (``asdict(MergeResult)``): the place is its ``suite_place``."""
    d = root / "var" / "agentops" / "results"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"x-merge-{name}.json").write_text(json.dumps({
        "started": "2026-10-10T13:00:00", "ended": "2026-10-10T13:30:00", "rc": rc, "reason": reason,
        "suite_place": place, "argv": ["a", "b", "c", "d", name]}), encoding="utf-8")


def _rendered_rows(root):
    landed = ms.collect(root, clock=T0)["landed"]
    return ms.render_text({"running": None, "full_run": None, "waiting": [], "landed": landed}).splitlines()[1:]


def test_a_fallback_place_is_in_the_landed_row_and_its_rendered_line(tmp_path):
    _landed_file(tmp_path, "orch-345", 0, None, FALLBACK_PLACE)
    assert ms.collect(tmp_path, clock=T0)["landed"][0]["suite_place"] == FALLBACK_PLACE
    line = _rendered_rows(tmp_path)[0]
    assert line.startswith("landed: orch-345") and line.endswith(f"rc 0, {FALLBACK_PLACE}")


def test_a_fallback_place_of_a_merge_that_did_not_land_is_in_its_rendered_line(tmp_path):
    _landed_file(tmp_path, "orch-346", 1, "red-suite", FALLBACK_PLACE)
    line = _rendered_rows(tmp_path)[0]
    assert line.startswith("did not land: orch-346") and "reason red-suite" in line and line.endswith(FALLBACK_PLACE)


def test_a_plain_github_place_stays_in_the_row_and_is_not_rendered(tmp_path):
    github_place = "suite on GitHub: 3 machines, parts 1 min/10 min, 12 min used, run 77"
    _landed_file(tmp_path, "orch-347", 0, None, github_place)
    assert ms.collect(tmp_path, clock=T0)["landed"][0]["suite_place"] == github_place
    assert "suite on GitHub" not in _rendered_rows(tmp_path)[0]


# -- the merge.py call site: the fallback reason lands in run.place (MergeResult.suite_place) and in merge.log -------

LOST_JOBS = {"run_id": 79, "parts": [], "minutes_used": 1, "outcome": "lost", "failed_ids": [], "full_recheck": False,
             "jobs": JOBS_LOST}


def test_a_lost_github_run_puts_its_jobs_in_the_merge_place_and_the_log(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, fakes=Fakes(LOST_JOBS))
    result = w.merge()
    assert result.ok and result.pushed and len(w.pc_runs) == 1
    assert result.suite_place == ("suite on the PC (GitHub lost: job test (2) no conclusion, "
                                  "job test (windows) cancelled; ids found: 0)")
    assert any(result.suite_place in x for x in w.log_lines())


def test_a_failed_id_whose_github_recheck_falls_back_says_why_in_the_place(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, fakes=Fakes(red(), later=[LOST_JOBS]))
    result = w.merge()
    assert w.rechecks == [[BAD]]  # the re-check fell back: the PC runs the failed id again
    assert ("; re-check of 1 on the PC (lost: job test (2) no conclusion, job test (windows) cancelled; "
            "ids found: 0)") in result.suite_place
