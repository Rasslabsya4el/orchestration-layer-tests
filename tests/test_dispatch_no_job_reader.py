"""#438: with no job reader configured (`pending_job_command` absent or empty, or no reader injected) no job
is awaited, so an issue a session let go at its end goes back to the pool on the next tick whatever its
newest comment says; a merge park, a configured reader that says "pending", and the `parked` label still
keep it out. Fake dispatcher, claude and gh of tests/test_issue5_merge_repool.py."""

from __future__ import annotations

import subprocess

import pytest

from tests.test_issue5_merge_repool import (
    P, _c, _ended, _offered_after_a_tick, _park_text, _summary)
from tests._area_rule import _no_area_rule  # noqa: F401

BACK = "#1: parked, but no job of its own is pending; back in the pool"
KEPT = "#1: parked, nothing newer than its checkpoint; kept out of the pool"

FORM_A = ("<!-- parked-on-run: this session is finishing; the session that claims the issue continues from the "
          "last checkpoint -->")
FORM_B = ("<!-- parked-on-run: this session (ORCH c4461108) is finishing; the session that claims the issue "
          "continues from the last checkpoint above -->")
FORM_C = "<!-- parked-on-run: context 172k; the next session continues from checkpoint 4 -->"


@pytest.mark.parametrize("form", ["a", "b", "c", "d"])
def test_row1_a_let_go_issue_with_no_job_reader_is_offered_next_tick(tmp_path, form):
    def build(sid):
        body = {"a": FORM_A, "b": FORM_B, "c": FORM_C,
                "d": "Checkpoint 3 (ORCH 1234abcd, #1): units 1-2 landed"}[form]
        return [_c(P, body)]

    claude, gh, d, log, _, _ = _ended(tmp_path, build, pending=None)
    assert d.has_pending_job is None
    _offered_after_a_tick(claude, d)
    assert log.count(BACK) == 1
    assert not any("kept out of the pool" in m for m in log)


def test_row2_e_a_merge_park_with_no_result_is_kept_out_logged_once(tmp_path):
    f = tmp_path / "r1.json"  # not written: the merge has not ended
    claude, gh, d, log, _, _ = _ended(tmp_path, lambda sid: [_c(P, _park_text(sid, f))], pending=None)
    started = len(claude.started)
    d.tick()
    d.tick()
    assert len(claude.started) == started
    assert log.count(KEPT) == 1
    assert BACK not in log


def test_row2_f_a_configured_reader_that_says_pending_keeps_it_out_logged_once(tmp_path):
    claude, gh, d, log, _, _ = _ended(tmp_path, lambda sid: [_c(P, FORM_C)], pending=lambda n: True)
    started = len(claude.started)
    d.tick()
    d.tick()
    assert len(claude.started) == started
    assert log.count(KEPT) == 1
    assert BACK not in log


def test_row2_g_an_issue_labelled_parked_is_not_a_candidate(tmp_path):
    claude, gh, d, log, comments, _ = _ended(tmp_path, lambda sid: [_c(P, FORM_B)], pending=None)
    labelled = _summary(1, comments)
    labelled.labels = ("ready-for-agent", "parked")
    gh._candidates = [labelled]
    started = len(claude.started)
    d.tick()
    d.tick()
    assert len(claude.started) == started
    assert BACK not in log  # the label keeps it out, the missing job reader does not put it back


def _fake_run(rc):
    def run(cmd, **kw):
        if rc == "timeout":
            raise subprocess.TimeoutExpired(cmd, 1)
        return subprocess.CompletedProcess(cmd, rc, b"", b"")
    return run


def test_row3_settings_pending_job_by_reader(monkeypatch):
    from tools.agentops import dispatch as dp

    with_reader = {"pending_job_command": "check {issue}"}
    got = [dp.settings_pending_job(7, settings={}),  # key absent
           dp.settings_pending_job(7, settings={"pending_job_command": "  "})]  # key empty
    for rc in (1, 0, 2, "timeout"):
        monkeypatch.setattr(dp.subprocess, "run", _fake_run(rc))
        got.append(dp.settings_pending_job(7, settings=with_reader))
    assert got == [False, False, False, True, True, True]


def test_no_reader_injected_means_no_job_is_awaited(tmp_path):
    claude, gh, d, log, _, _ = _ended(tmp_path, lambda sid: [_c(P, FORM_A)], pending=None)
    assert d._pending_job(1) is False
