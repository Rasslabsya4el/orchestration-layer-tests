"""#145 unit 2: a merge for a ticket of ANOTHER repo takes ``--issue owner/repo#N``; the detached runner posts its
result comment and re-adds ``ready-for-agent`` in THAT repo's issue, whether the merge landed or failed.

Seams: the ``--issue`` argparse type (``merge.issue_arg``), ``detach.run_and_record`` (the record), and
``merge_handback.post_merge_result`` / ``post_foreign_result`` with fake ``gh`` per repo."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from tools.agentops import cli, detach, merge as merge_module, merge_handback
from tools.agentops.dispatch import GhCli


def make_gh(repo: str, calls: list, closed=(), fail=False) -> GhCli:
    """A GhCli of ``repo`` over a fake runner; every call is recorded as (repo, args, stdin)."""
    def fake(args, stdin):
        calls.append((repo, args, stdin))
        if fail:
            return subprocess.CompletedProcess(args, 1, "", "boom")
        if args[:2] == ["issue", "view"]:
            state = "CLOSED" if int(args[2]) in closed else "OPEN"
            return subprocess.CompletedProcess(args, 0, json.dumps({"state": state, "labels": [], "assignees": []}), "")
        return subprocess.CompletedProcess(args, 0, "", "")
    return GhCli(repo=repo, runner=fake)


def writes(calls):
    return [c for c in calls if c[1][:2] != ["issue", "view"]]


def rec(**kw):
    base = dict(kind="merge", rc=1, pushed_sha=None, reason="red-suite", suite_output="out.txt",
                started="", ended="", issues=[], foreign_issues=["owner/repo#7"], in_line_s=10, suite_s=60, failed=2)
    base.update(kw)
    return base


# -- the argparse type -------------------------------------------------------------------------------

def test_issue_arg_parses_a_number_and_keeps_a_foreign_ref():
    assert merge_module.issue_arg("5") == 5
    assert merge_module.issue_arg("o/r#5") == "o/r#5"
    assert merge_module.issue_arg("my-org/my.repo_x#12") == "my-org/my.repo_x#12"


@pytest.mark.parametrize("bad", ["abc", "o/r#", "#5", "o/r", "o/r#5x", "a/b/c#5", ""])
def test_issue_arg_rejects_anything_else(bad):
    with pytest.raises(argparse.ArgumentTypeError):
        merge_module.issue_arg(bad)


def test_cli_rejects_a_bad_issue_value(capsys):
    with pytest.raises(SystemExit):
        cli.main(["merge", "b", "--detach", "--issue", "abc"])
    assert "--issue" in capsys.readouterr().err


def test_foreign_issue_alone_is_not_refused_by_the_detached_merge_check(monkeypatch, capsys):
    launched = []
    monkeypatch.setattr(detach, "launch", lambda argv, kind, branch: launched.append(argv) or (Path("r.json"), 1))
    monkeypatch.setattr(merge_module, "worktree_refusal", lambda cwd: None)
    monkeypatch.setattr(merge_module, "queue_target_refusals", lambda branch, *, cwd: None)
    rc = cli.main(["merge", "somebranch", "--detach", "--issue", "owner/repo#5"])
    assert rc == 0, capsys.readouterr().out
    assert len(launched) == 1 and "owner/repo#5" in launched[0]


def test_the_merge_itself_sees_only_the_own_repo_numbers(monkeypatch):
    seen = {}

    def fake_run_merge(branch, suite_cmd, **kw):
        seen["issues"] = tuple(kw["issues"])
        raise merge_module.MergeAlreadyQueued(branch, 1, 0.0)

    monkeypatch.setattr(merge_module, "worktree_refusal", lambda cwd: None)
    monkeypatch.setattr(merge_module, "resolve_suite_cmd", lambda s: "true")
    monkeypatch.setattr(merge_module, "resolve_suite_env", lambda: {})
    monkeypatch.setattr(merge_module, "resolve_suite_shared", lambda: ())
    monkeypatch.setattr(merge_module, "run_merge", fake_run_merge)
    ns = argparse.Namespace(branch="b", cwd=Path("."), suite=None, lock=None, timeout=None, queue_checked=True,
                            issue=[3, "owner/repo#7"])
    assert merge_module.run_merge_command(ns) == merge_module.EXIT_ALREADY_QUEUED
    assert seen["issues"] == (3,)


# -- the hand-back to the foreign repo ----------------------------------------------------------------

def _factory(calls, **per_repo):
    return lambda repo: make_gh(repo, calls, **per_repo.get(repo, {}))


def test_failed_merge_comments_and_relabels_in_the_foreign_repo():
    calls: list = []
    posted = merge_handback.post_foreign_result(rec(), "r.json", "orch-1", _factory(calls))
    assert posted == ["owner/repo#7"]
    w = writes(calls)
    assert [c[0] for c in w] == ["owner/repo", "owner/repo"]
    assert w[0][1][:3] == ["issue", "comment", "7"]
    assert w[0][2].count("<!-- merge result -->") == 1
    assert "did not land (red-suite)" in w[0][2]
    assert w[1][1][:3] == ["issue", "edit", "7"] and w[1][1][-2:] == ["--add-label", "ready-for-agent"]
    assert "owner/repo" in w[0][1]  # -R owner/repo on every call


def test_landed_merge_likewise():
    calls: list = []
    r = rec(rc=0, pushed_sha="abcdef1234567890", reason="", failed=0)
    assert merge_handback.post_foreign_result(r, "r.json", "orch-1", _factory(calls)) == ["owner/repo#7"]
    w = writes(calls)
    assert "landed on main abcdef123456" in w[0][2]
    assert w[1][1][-2:] == ["--add-label", "ready-for-agent"]


def test_closed_foreign_issue_gets_nothing():
    calls: list = []
    f = _factory(calls, **{"owner/repo": {"closed": (7,)}})
    assert merge_handback.post_foreign_result(rec(), "r.json", "orch-1", f) == []
    assert writes(calls) == []


def test_gh_error_on_the_foreign_repo_is_swallowed():
    calls: list = []
    f = _factory(calls, **{"owner/repo": {"fail": True}})
    assert merge_handback.post_foreign_result(rec(), "r.json", "orch-1", f) == []


def test_malformed_foreign_ref_is_skipped_and_the_next_one_still_posted():
    calls: list = []
    r = rec(foreign_issues=["junk", "owner/repo#7", "a/b#2"])
    assert merge_handback.post_foreign_result(r, "r.json", "orch-1", _factory(calls)) == ["owner/repo#7", "a/b#2"]


def test_post_merge_result_posts_own_ints_to_its_gh_and_foreign_through_the_factory():
    own_calls: list = []
    foreign_calls: list = []
    own = make_gh("o/own", own_calls)
    posted = merge_handback.post_merge_result(rec(issues=[3]), "r.json", "orch-1", own,
                                              gh_for_repo=_factory(foreign_calls))
    assert posted == [3]
    assert [c[1][:3] for c in writes(own_calls)] == [["issue", "comment", "3"], ["issue", "edit", "3"]]
    assert [c[1][:3] for c in writes(foreign_calls)] == [["issue", "comment", "7"], ["issue", "edit", "7"]]
    assert all(c[0] == "owner/repo" for c in foreign_calls)


def test_foreign_only_record_never_touches_the_own_gh():
    own_calls: list = []
    foreign_calls: list = []
    merge_handback.post_merge_result(rec(), "r.json", "orch-1", make_gh("o/own", own_calls),
                                     gh_for_repo=_factory(foreign_calls))
    assert own_calls == []
    assert len(writes(foreign_calls)) == 2


# -- the record --------------------------------------------------------------------------------------

def test_record_splits_own_and_foreign_issues_and_posts_both(tmp_path, monkeypatch):
    own_calls: list = []
    foreign_calls: list = []
    monkeypatch.setattr(merge_handback, "_default_gh_for_repo", _factory(foreign_calls))
    argv = ["python", "-m", "tools.agentops", "merge", "b", "--issue", "3", "--issue", "o/r#7"]
    assert detach._issues_of(argv) == [3]
    assert detach._foreign_issues_of(argv) == ["o/r#7"]
    assert detach._foreign_issues_of(["x", "--issue=a/b#9", "--issue", "4"]) == ["a/b#9"]

    monkeypatch.setattr(detach.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "red\n", ""))
    result = tmp_path / "res.json"
    rc = detach.run_and_record(argv, result, "merge", "b", merge_log=tmp_path / "m.log",
                               gh=make_gh("o/own", own_calls))
    assert rc == 1
    record = json.loads(result.read_text(encoding="utf-8"))
    assert record["issues"] == [3]
    assert record["foreign_issues"] == ["o/r#7"]
    assert [c[1][:3] for c in writes(own_calls)] == [["issue", "comment", "3"], ["issue", "edit", "3"]]
    assert [(c[0], c[1][:3]) for c in writes(foreign_calls)] == [
        ("o/r", ["issue", "comment", "7"]), ("o/r", ["issue", "edit", "7"])]


def test_record_of_a_foreign_only_merge_posts_the_hand_back(tmp_path, monkeypatch):
    foreign_calls: list = []
    monkeypatch.setattr(merge_handback, "_default_gh_for_repo", _factory(foreign_calls))
    monkeypatch.setattr(detach.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "red\n", ""))
    argv = ["python", "-m", "tools.agentops", "merge", "b", "--issue", "o/r#7"]
    detach.run_and_record(argv, tmp_path / "res.json", "merge", "b", merge_log=tmp_path / "m.log",
                          gh=make_gh("o/own", []))
    record = json.loads((tmp_path / "res.json").read_text(encoding="utf-8"))
    assert record["issues"] == [] and record["foreign_issues"] == ["o/r#7"]
    assert len(writes(foreign_calls)) == 2


def test_non_merge_record_has_an_empty_foreign_list(tmp_path, monkeypatch):
    monkeypatch.setattr(detach.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    detach.run_and_record(["x", "--issue", "o/r#7"], tmp_path / "res.json", "commit", None,
                          merge_log=tmp_path / "m.log")
    assert json.loads((tmp_path / "res.json").read_text(encoding="utf-8"))["foreign_issues"] == []


def test_real_gh_in_a_test_is_refused_for_a_foreign_issue_too(tmp_path):
    with pytest.raises(detach.RealGhInTestError):
        detach.run_and_record(["x", "merge", "b", "--issue", "o/r#7"], tmp_path / "res.json", "merge", "b",
                              merge_log=tmp_path / "m.log")
