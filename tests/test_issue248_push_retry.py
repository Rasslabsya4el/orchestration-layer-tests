"""#248: a push refused while origin/main did not move is retried with the same merged commit and no suite run.

Real git (the bare origin of the #212 `World`), a fake suite (it counts its runs), and a fake push seam
(`merge._push_main`) that refuses like a transient network or hook error would. Rows 1-3 of the issue."""
from __future__ import annotations

import subprocess

from tools.agentops import merge as merge_module
from tests.test_issue212_merge_recheck import IDS, World, _git

STDERR = "fatal: unable to access 'https://github.com/x/y.git/': Could not resolve host: github.com"


class PushSpy:
    """Stands in for `merge._push_main`: refuses the first `refuse` calls (0 = none, None = all), then pushes for real."""

    def __init__(self, monkeypatch, *, refuse, before_refusal=None):
        self.real = merge_module._push_main
        self.refuse = refuse
        self.before_refusal = before_refusal  # called on the first refusal: moves origin like another writer
        self.calls: list[str] = []
        self.sleeps: list[float] = []
        monkeypatch.setattr(merge_module, "_push_main", self)
        monkeypatch.setattr(merge_module, "PUSH_RETRY_WAIT_S", 0)

    def __call__(self, cwd, remote, sha, main):
        self.calls.append(sha)
        if self.refuse is None or len(self.calls) <= self.refuse:
            if self.before_refusal and len(self.calls) == 1:
                self.before_refusal()
            return subprocess.CompletedProcess(["git", "push"], 1, "", STDERR + "\nsecond line of git's hint\n")
        return self.real(cwd, remote, sha, main)


def test_row1_one_refusal_with_main_unmoved_retries_the_push_without_a_suite_run(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    spy = PushSpy(monkeypatch, refuse=1)
    result = w.merge()
    assert result.ok and result.pushed, result
    assert len(w.calls) == 1  # the one green suite; the retry ran none
    assert len(spy.calls) == 2 and spy.calls[0] == spy.calls[1]  # the same merged commit
    assert _git(w.bare, "rev-parse", "main") == spy.calls[0]
    refusals = [x for x in w.log_lines() if "push refused" in x]
    assert len(refusals) == 1 and STDERR in refusals[0] and "second line" not in refusals[0], refusals


def test_row2_a_push_refused_every_time_does_not_land_and_keeps_the_green_suite(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    spy = PushSpy(monkeypatch, refuse=None)
    old_main = _git(w.bare, "rev-parse", "main")
    result = w.merge()
    assert not result.ok and not result.pushed
    assert result.reason == "push-refused" and f"push refused: {STDERR}" in result.message, result
    assert len(w.calls) == 1  # no re-run of the suite
    assert len(spy.calls) == 3 and len(set(spy.calls)) == 1  # the first push and 2 retries of the same commit
    assert _git(w.bare, "rev-parse", "main") == old_main
    assert len([x for x in w.log_lines() if "push refused" in x]) == 3


def test_row2_the_wait_between_pushes_is_30_seconds_by_default():
    assert merge_module.PUSH_RETRY_WAIT_S == 30 and merge_module.PUSH_ATTEMPTS == 3


def test_rule3_a_refusal_because_main_moved_takes_the_old_recheck_path(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.fake_map(monkeypatch, IDS)

    def another_writer_lands():
        _git(w.checkout, "fetch", "-q", "origin")
        worker = tmp_path / "other"
        _git(tmp_path, "clone", "-q", str(w.bare), str(worker))
        (worker / "pkg" / "b.py").write_text("B = 2\n")
        _git(worker, "commit", "-q", "-am", "move b")
        _git(worker, "push", "-q", "origin", "main")

    spy = PushSpy(monkeypatch, refuse=1, before_refusal=another_writer_lands)
    result = w.merge()
    assert result.ok and result.pushed and result.suite_runs == 2 and result.rebases == 1, result
    assert len(spy.calls) == 2 and spy.calls[0] != spy.calls[1]  # a new merge commit, not a retry of the old one
    assert w.arg_ids[1] == IDS  # the old recheck path ran the tests the new commit reaches
    assert len([x for x in w.log_lines() if "green suite thrown away" in x]) == 1
