"""#341 row 4: a GitHub part that ends with the plugin's ``ids sent but not collected`` message names the first uncollected id.

The part's log (``--log-failed``) is read through the injected ``Deps.failed_log``; the poll says ``red-test`` with no
``FAILED`` id, which is the case that used to read ``red, no test named``. A plain red with no such message keeps that
reason. What the PC runs is unchanged: the result still goes to the PC (``goes_to_pc``).
"""
from __future__ import annotations

from types import SimpleNamespace

from tools.agentops import github_suite as gs
from tools.agentops import merge_github

COPY = "https://github.com/owner/copy-repo.git"
SETTINGS = {"github_machines_per_merge": 5, "github_machines_total": 20}
NOT_COLLECTED = "ERROR: ids sent but not collected: 1 of 1908: tests/x.py"
PLAN = merge_github.Plan(remote=COPY, own_repo=False, repo_slug="owner/repo", repo_name="orch")


def _run(log_text: str):
    deps = gs.Deps(
        send=lambda root, branch, remote: SimpleNamespace(sent=True, reason=None),
        acquire_machines=lambda *a, **k: SimpleNamespace(machines=2, release=lambda: None),
        dispatch=lambda *a, **k: 7,
        poll=lambda copy, run_id, **k: {"run_id": run_id, "parts": [], "minutes_used": 0, "outcome": "red-test",
                                        "failed_ids": [], "full_recheck": True},
        record=lambda record: None,
        failed_log=lambda copy, run_id: log_text,
    )
    return gs.run_github_suite(root=None, branch="i341-not-collected", repo_slug="owner/repo", remote=COPY,
                               repo_name="orch", priority=False, settings=SETTINGS, deps=deps)


def test_first_uncollected_id_is_read_from_the_part_output():
    assert gs.first_not_collected(NOT_COLLECTED) == "tests/x.py"


def test_first_uncollected_id_takes_the_first_of_several_and_keeps_brackets_with_commas():
    text = ("ERROR: ids sent but not collected: 2 of 9: tests/a.py::t[1, 2], tests/b.py::u")
    assert gs.first_not_collected(text) == "tests/a.py::t[1, 2]"


def test_a_log_without_the_message_names_no_id():
    assert gs.first_not_collected("FAILED tests/y.py::test_q - assert 1 == 2") == ""


def test_not_collected_part_brain_line_names_the_id_not_no_test_named():
    result = _run(NOT_COLLECTED)
    assert result.status == "red" and result.full_recheck is True
    line = merge_github.brain_line_for(PLAN, "#341", result)
    assert "not collected: tests/x.py" in line, line
    assert "no test named" not in line, line


def test_not_collected_part_still_goes_to_the_pc():
    result = _run(NOT_COLLECTED)
    assert merge_github.goes_to_pc(result) is True


def test_plain_red_without_the_message_reads_the_345_job_reason():
    result = _run("FAILED tests/y.py::test_q - assert 1 == 2")
    assert result.status == "red" and result.failed_ids == [] and result.full_recheck is True
    assert merge_github.pc_reason(result) == "red: no job read; ids found: 0"  # #345 reason text
    assert "red: no job read; ids found: 0" in merge_github.brain_line_for(PLAN, "#341", result)
