import json
import subprocess

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


def rec(**kw):
    base = dict(kind="merge", rc=0, pushed_sha="abcdef1234567890", reason="", suite_output="out.txt",
                started="", ended="", issues=[5], in_line_s=125, suite_s=1020, failed=0)
    base.update(kw)
    return base


def writes(calls):
    return [c for c in calls if c[0][:2] != ["issue", "view"]]


def test_landed(tmp_path):
    gh, calls = make(tmp_path)
    assert post_merge_result(rec(), "r.json", "orch-1", gh) == [5]
    w = writes(calls)
    assert w[0][0][:3] == ["issue", "comment", "5"]
    lines = w[0][1].splitlines()
    assert lines[0] == "<!-- merge result -->"
    assert "landed on main abcdef123456 after 2.1 min in line and a suite of 17 min, 0 failed" in lines[1]
    assert w[1][0][:3] == ["issue", "edit", "5"] and w[1][0][-2:] == ["--add-label", "ready-for-agent"]


def test_not_landed(tmp_path):
    gh, calls = make(tmp_path)
    post_merge_result(rec(rc=1, reason="red-suite", failed=3, pushed_sha=None), "r.json", "b", gh)
    body = writes(calls)[0][1]
    assert "did not land (red-suite)" in body and "3 failed" in body and "abcdef" not in body


def test_silent_reasons_and_kind(tmp_path):
    for r in (rec(reason="nothing-to-merge"), rec(reason="already-queued"), rec(kind="commit")):
        gh, calls = make(tmp_path)
        assert post_merge_result(r, "r.json", "b", gh) == []
        assert calls == []


def test_closed(tmp_path):
    gh, calls = make(tmp_path, closed=(5,))
    assert post_merge_result(rec(), "r.json", "b", gh) == []
    assert writes(calls) == []


def test_two_issues_order(tmp_path):
    gh, calls = make(tmp_path)
    assert post_merge_result(rec(issues=[5, 7]), "r.json", "b", gh) == [5, 7]
    assert [(c[0][1], c[0][2]) for c in writes(calls)] == [
        ("comment", "5"), ("edit", "5"), ("comment", "7"), ("edit", "7")]
