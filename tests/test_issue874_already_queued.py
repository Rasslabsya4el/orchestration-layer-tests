"""#874 unit 2: `already-queued` hands back the earlier queued merge's result; the #842 thread replay."""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_agentops_merge_handback import make, rec, writes
from tools.agentops.merge_handback import post_merge_result

FIX = Path(__file__).parent / "fixtures" / "issue874_thread_842.json"


def _earlier(tmp_path, **kw):
    f = tmp_path / "20261011T175353-merge-837p2-int.json"
    f.write_text(json.dumps(rec(issues=[], **kw)), encoding="utf-8")
    return f


def _asked(f, issues=(837,)):
    return rec(rc=5, reason="already-queued", pushed_sha=None, issues=list(issues), in_line_s=None,
               suite_s=None, failed=None, output=f"already queued: 837p2-int since 17:53 (42 min) by pid 8764, result {f}\n")


def test_already_queued_posts_the_earlier_landing(tmp_path):
    f = _earlier(tmp_path)
    gh, calls = make(tmp_path)
    assert post_merge_result(_asked(f), "asked.json", "837p2-int", gh) == [837]
    w = writes(calls)
    assert w[0][0][:3] == ["issue", "comment", "837"]
    assert "landed on main abcdef123456" in w[0][1] and str(f) in w[0][1]
    assert w[1][0][-2:] == ["--add-label", "ready-for-agent"]


def test_already_queued_posts_the_earlier_conflict(tmp_path):
    f = _earlier(tmp_path, rc=1, reason="conflict", pushed_sha=None)
    gh, calls = make(tmp_path)
    assert post_merge_result(_asked(f), "asked.json", "837p2-int", gh) == [837]
    assert "did not land (conflict)" in writes(calls)[0][1]


def test_already_queued_earlier_still_running_or_already_commented_is_silent(tmp_path):
    gh, calls = make(tmp_path)
    assert post_merge_result(_asked(tmp_path / "missing.json"), "a.json", "b", gh) == []
    f = tmp_path / "e.json"
    f.write_text(json.dumps(rec(issues=[837])), encoding="utf-8")
    assert post_merge_result(_asked(f), "a.json", "b", gh) == []
    assert writes(calls) == []


def test_replay_842_thread_has_no_no_checkpoint(tmp_path, monkeypatch):
    """Same #852 harness; passes on main too (#850/#852 already cover it): `no_checkpoint` lines 0."""
    from tests import test_issue852_seven_ends as seven
    monkeypatch.setattr(seven, "FIXTURE", json.loads(FIX.read_text(encoding="utf-8")))
    outcome, gh, core_gh, d = seven._replay(tmp_path, 842)
    assert outcome != "no_checkpoint"
    assert outcome == "released"
    assert d._consecutive_no_checkpoint(9) == 0
