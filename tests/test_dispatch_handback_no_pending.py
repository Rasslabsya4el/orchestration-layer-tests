"""#818: a ticket handed back with `ready-for-agent` kept and no job of its own pending is offered
on the next tick; one parked on a running job stays out until something newer (its result comment) lands."""

import json

from tests.test_agentops_dispatch import FakeClaude, _dispatcher
from tests.test_agentops_dispatch_parked import RealMarkGh, _issue, _summary
from tests._area_rule import _no_area_rule  # noqa: F401

T0 = "2026-10-01T14:47:50Z"
T2 = "2026-10-01T14:59:12Z"
T3 = "2026-10-01T15:10:00Z"


def _park(tmp_path, pending):
    claude = FakeClaude()
    gh = RealMarkGh([], T0, candidates=[_issue(1)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh)
    d.has_pending_job = lambda n: pending["on"]
    d.tick()
    live_id = claude.list_sessions()[0]["id"]
    gh._view = json.dumps({"comments": [{"body": f"ORCH {live_id}: FINISHING, releasing", "createdAt": T0}],
                           "updatedAt": T0})
    gh.claim(1)
    d.tick()
    d.sessions[0].let_go.add(1)
    claude.set_state(live_id, state="done", status="idle")
    assert d.tick().outcomes == {1: "parked"}
    gh._candidates = [_summary(1, comment_at=T0, updated_at=T0)]
    return claude, gh, d


def test_a_no_pending_job_is_offered_next_tick(tmp_path):
    claude, gh, d = _park(tmp_path, {"on": False})
    started = len(claude.started)
    d.tick()
    assert len(claude.started) == started + 1
    assert claude.started[-1][0] == [1]


def test_b_running_job_keeps_it_out_until_result_comment(tmp_path):
    pending = {"on": True}
    claude, gh, d = _park(tmp_path, pending)
    started = len(claude.started)
    d.tick()
    assert len(claude.started) == started
    gh._candidates = [_summary(1, comment_at=T3, updated_at=T3)]  # the job's result comment
    pending["on"] = False
    d.tick()
    assert claude.started[-1][0] == [1]


def test_c_parked_on_merge_stays_out_until_merge_result_comment(tmp_path):
    claude = FakeClaude()
    gh = RealMarkGh([], T0, candidates=[_issue(1)])
    d = _dispatcher(tmp_path, claude=claude, gh=gh)
    d.has_pending_job = lambda n: False
    d.tick()
    live_id = claude.list_sessions()[0]["id"]
    gh._view = json.dumps({"comments": [{"body": f"ORCH {live_id}: parked on merge var/agentops/results/x-merge-b.json",
                                         "createdAt": T0}], "updatedAt": T0})
    gh.claim(1)
    d.tick()
    d.sessions[0].let_go.add(1)
    claude.set_state(live_id, state="done", status="idle")
    assert d.tick().outcomes == {1: "parked"}
    gh._candidates = [_summary(1, comment_at=T0, updated_at=T0)]
    started = len(claude.started)
    d.tick()
    assert len(claude.started) == started
    gh._candidates = [_summary(1, comment_at=T3, updated_at=T3)]  # "<!-- merge result -->" comment
    d.tick()
    assert claude.started[-1][0] == [1]


def test_d_command_template_reader(monkeypatch):
    import subprocess
    from tools.agentops import dispatch as dp

    seen = []

    def fake(rc):
        def run(cmd, **kw):
            seen.append(cmd)
            if rc == "timeout":
                raise subprocess.TimeoutExpired(cmd, 1)
            return subprocess.CompletedProcess(cmd, rc, b"", b"")
        return run

    s = {"pending_job_command": "check {issue}"}
    monkeypatch.setattr(dp.subprocess, "run", fake(0))
    assert dp.settings_pending_job(7, settings=s) is True and seen == ["check 7"]
    monkeypatch.setattr(dp.subprocess, "run", fake(1))
    assert dp.settings_pending_job(7, settings=s) is False
    monkeypatch.setattr(dp.subprocess, "run", fake("timeout"))
    assert dp.settings_pending_job(7, settings=s) is True
    assert dp.settings_pending_job(7, settings={}) is True
