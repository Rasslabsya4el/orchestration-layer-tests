"""#758 rows 1-6: the time left of a suite and the restart rule read the share of work done (plan file)."""
import json
import os
import time

import pytest

from tools.agentops import merge, merge_status as ms
from tools.agentops.merge import SuiteRestart, decide_restart
from tools.agentops.suite_plan import finished_count, load_plan, plan_path, rest_seconds, work_share
from tools.agentops.util import human_duration

DESC = [438, 186, 185] + [1] * 100
ASC = [1] * 100 + [185, 186, 438]


_STAMPS = {}


def _stamp(epoch):
    text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))
    _STAMPS[text] = float(epoch)  # the reader gets this exact epoch back: no local-zone round trip in the test
    return text


@pytest.fixture(autouse=True)
def _exact_stamps(monkeypatch):
    real = ms._epoch
    monkeypatch.setattr(ms, "_epoch", lambda stamp: _STAMPS.get(str(stamp).replace("T", " "), real(stamp)))


@pytest.fixture
def root(tmp_path):
    (tmp_path / "var" / "locks" / "merge.lock.queue").mkdir(parents=True)
    (tmp_path / "var" / "agentops" / "merge_suite").mkdir(parents=True)
    (tmp_path / "var" / "agentops" / "results").mkdir(parents=True)
    return tmp_path


def _running(root, elapsed, out_text, plan=None):
    t0 = int(time.time()) - elapsed
    cur = root / "var" / "agentops" / "merge_suite" / "20261010T100000-000001-orch-1.out"
    cur.write_text(out_text, encoding="utf-8")
    if plan is not None:
        plan_path(cur).write_text(json.dumps({"seconds": plan}), encoding="utf-8")
    (root / "var" / "locks" / "merge.lock").write_text(
        json.dumps({"pid": os.getpid(), "started": _stamp(t0), "token": "t", "progress": str(cur)}), encoding="utf-8")
    return ms.collect(root, clock=t0 + elapsed)["running"], t0 + elapsed


def test_helpers():
    assert finished_count(".s. [  3%]\nFx [ 5%]\nnoise ... here\n") == 5
    assert work_share(DESC, 3) == pytest.approx(809 / 909)
    assert rest_seconds(100, 0) is None


def test_row1_long_tests_first_rest_100s(root):
    r, _ = _running(root, 809, "... [  3%]\n", DESC)
    assert abs(r["eta_s"] - 100) <= 1
    assert r["percent"] == 3
    snap = {"running": r, "full_run": None, "waiting": [], "landed": []}
    assert f"about {human_duration(100)} left" in ms.render_text(snap)


def test_row2_long_tests_last_rest_809s(root):
    r, _ = _running(root, 100, "." * 100 + " [ 97%]\n", ASC)
    assert abs(r["eta_s"] - 809) <= 1
    snap = {"running": r, "full_run": None, "waiting": [], "landed": []}
    assert f"about {human_duration(809)} left" in ms.render_text(snap)


def test_row3_no_plan_equals_today(root):
    r, _ = _running(root, 240, "." * 72 + " [ 40%]\n")
    assert r["eta_s"] == round(240 * 60 / 40)


def test_row6_share_zero_no_estimate_in_text(root):
    r, _ = _running(root, 100, ".. [ 66%]\n", [0, 0, 5])
    assert r["eta_s"] is None
    text = ms.render_text({"running": r, "full_run": None, "waiting": [], "landed": []})
    assert "left" not in text


def test_full_run_row_gets_eta_only_with_plan(root, monkeypatch):
    base = root / "var" / "agentops"
    (base / "full_run").mkdir()
    out = base / "full_run" / "abc.out"
    out.write_text("... [  3%]\n", encoding="utf-8")
    t0 = int(time.time()) - 809
    monkeypatch.setattr(ms, "_birth", lambda p: t0)  # #764 counts from the out file's birth: here the run's own start
    (base / "full_run.lock").write_text(json.dumps({"pid": os.getpid(), "started": _stamp(t0)}), encoding="utf-8")
    f = ms._full_run(root, t0 + 809)
    assert f["eta_s"] is None and "left" not in ms.render_text({"running": None, "full_run": f, "waiting": [], "landed": []})
    plan_path(out).write_text(json.dumps({"seconds": DESC}), encoding="utf-8")
    f = ms._full_run(root, t0 + 809)
    assert abs(f["eta_s"] - 100) <= 1
    assert f"about {human_duration(100)} left" in ms.render_text({"running": None, "full_run": f, "waiting": [], "landed": []})


def _tick(tmp_path, plan, text, elapsed, k_new=2, full=400.0):
    out = tmp_path / "x.out"
    if plan is not None:
        plan_path(out).write_text(json.dumps({"seconds": plan}), encoding="utf-8")
    clock = iter([0.0, 0.0, float(elapsed)])
    r = SuiteRestart(1, probe_k=lambda: k_new, full_for=lambda k: full, clock=lambda: next(clock), out_path=out)
    lines = []
    r.log = lines.append
    r.begin()
    return r.tick(text)[0], lines[0]


def test_row4_restart_keeps_when_rest_100s(tmp_path):
    go, line = _tick(tmp_path, DESC, "... [  3%]", 809)
    assert not go and "keep" in line


def test_row5_restart_when_rest_809s(tmp_path):
    go, line = _tick(tmp_path, ASC, "." * 100 + " [ 97%]", 100)
    assert go and "-> restart" in line
    assert decide_restart(1, 2, 100, 97, 400.0, False)[0] is False  # by tests it keeps


def test_row6_share_zero_no_estimate_in_the_restart_line(tmp_path):
    # share 0 (no test done) gives no estimate; #763: under 5 % done there is little to lose, so a
    # budget that doubled restarts at once, and one that did not keeps
    go, line = _tick(tmp_path, [0, 0, 5], ".. [ 66%]", 100)
    assert go and "-> restart" in line
    go, _ = _tick(tmp_path, [0, 0, 5], ".. [ 66%]", 100, k_new=1)
    assert not go


def test_row3_restart_without_plan_is_today(tmp_path):
    go, _ = _tick(tmp_path, None, "..... [ 28%]", 480, k_new=6, full=390.0)
    assert go


def _with_plugin(d):
    f = d / "tools" / "agentops" / "suite_plan_plugin.py"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("")


def test_suite_command_carries_plugin_and_env(tmp_path, monkeypatch):
    seen = {}
    _with_plugin(tmp_path)
    monkeypatch.setattr(merge, "_suite_router", lambda: None)
    monkeypatch.setattr(merge, "_run_suite", lambda cmd, cwd, env, out, *a, **k: seen.update(cmd=cmd, env=env, out=out) or (True, ""))
    out = tmp_path / "s.out"
    merge._run_merge_suite("python -m pytest tests -q", "python -m pytest tests -q", 1, "", tmp_path, {"A": "1"}, out)
    assert seen["cmd"] == "python -m pytest tests -q"   # the command stays today's
    assert "-p tools.agentops.suite_plan_plugin" in seen["env"]["PYTEST_ADDOPTS"]
    assert seen["env"]["SUITE_PLAN_FILE"] == str(out) + ".plan.json" and seen["env"]["A"] == "1"
    seen.clear()
    merge._run_merge_suite("echo hi", "echo hi", 0, "", tmp_path, None, out)
    assert seen["cmd"] == "echo hi" and not (seen["env"] or {}).get("SUITE_PLAN_FILE")
    seen.clear()   # a suite dir without the plugin file (a fixture repo): no env, nothing to import
    merge._run_merge_suite("python -m pytest tests", "python -m pytest tests", 1, "", tmp_path / "none", {"A": "1"}, out)
    assert seen["env"] == {"A": "1"}


def test_suite_durations_env_only_when_map_datafile_exists(tmp_path, monkeypatch):
    from tools.agentops.test_select import map_dir
    seen = {}
    _with_plugin(tmp_path)
    monkeypatch.setattr(merge, "_suite_router", lambda: None)
    monkeypatch.setattr(merge, "_run_suite", lambda cmd, cwd, env, out, *a, **k: seen.update(env=env) or (True, ""))
    root = tmp_path / "main"
    merge._run_merge_suite("python -m pytest tests", "python -m pytest tests", 1, "", tmp_path, None, tmp_path / "s.out", root=root)
    assert "SUITE_DURATIONS_FILE" not in seen["env"]
    (map_dir(root)).mkdir(parents=True)
    (map_dir(root) / "testmondata").write_text("x")
    merge._run_merge_suite("python -m pytest tests", "python -m pytest tests", 1, "", tmp_path, None, tmp_path / "s.out", root=root)
    assert seen["env"]["SUITE_DURATIONS_FILE"] == str(map_dir(root) / "testmondata")
