"""#219 unit 2b: a merge result a withdraw or a supersede ended is silent: no hand-back comment, no label.

Seams: ``merge_handback.post_merge_result`` (the hand-back) and ``detach.run_and_record`` (the record it writes
and the hand-back it posts, the child's rc patched to 1), both with a fake ``gh`` (a GhCli over a fake runner).
"""

import json
import subprocess

import pytest

from tools.agentops import detach
from tools.agentops.dispatch import GhCli
from tools.agentops.merge_handback import post_merge_result


def make(tmp_path, closed=()):
    calls = []

    def fake(args, stdin):
        calls.append((args, stdin))
        if args[:2] == ["issue", "view"]:
            state = "CLOSED" if int(args[2]) in closed else "OPEN"
            return subprocess.CompletedProcess(args, 0, json.dumps({"state": state, "labels": [], "assignees": []}), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    return GhCli(repo="o/r", cwd=tmp_path, runner=fake), calls


def writes(calls):
    return [c for c in calls if c[0][:2] != ["issue", "view"]]


def rec(**kw):
    base = dict(kind="merge", rc=1, pushed_sha=None, reason="red-suite", suite_output="out.txt",
                started="", ended="", issues=[5], in_line_s=125, suite_s=1020, failed=3)
    base.update(kw)
    return base


@pytest.mark.parametrize("reason", ["withdrawn", "superseded"])
def test_silent_reason_posts_nothing_and_re_adds_no_label(tmp_path, reason):
    gh, calls = make(tmp_path)
    assert post_merge_result(rec(reason=reason), "r.json", "orch-1", gh) == []
    assert writes(calls) == []


def test_withdrawn_run_through_run_and_record_posts_nothing(tmp_path, monkeypatch):
    gh, calls = make(tmp_path)
    monkeypatch.setattr(detach.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "red\n", ""))
    result = tmp_path / "res.json"
    result.with_name(result.name + ".withdrawn").write_text("withdrawn: by the owner", encoding="utf-8")
    argv = ["python", "-m", "tools.agentops", "merge", "orch-1", "--issue", "5"]
    rc = detach.run_and_record(argv, result, "merge", "orch-1", merge_log=tmp_path / "m.log", gh=gh)
    assert rc == 1
    assert json.loads(result.read_text(encoding="utf-8"))["reason"] == "withdrawn"
    assert writes(calls) == []
    assert "hand-back failed" not in (tmp_path / "m.log").read_text(encoding="utf-8")


def test_red_suite_still_posts_comment_and_label(tmp_path):
    gh, calls = make(tmp_path)
    assert post_merge_result(rec(), "r.json", "orch-1", gh) == [5]
    assert [c[0][:3] for c in writes(calls)] == [["issue", "comment", "5"], ["issue", "edit", "5"]]
    assert writes(calls)[1][0][-2:] == ["--add-label", "ready-for-agent"]
