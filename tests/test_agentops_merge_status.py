"""#680: `merge-status` reads the merge line read-only; fixtures built in tmp_path, no heavy tool."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from tools.agentops import merge_status as ms

LINE = "." * 72
FINISHED = f"{LINE} [100%]\n4190 passed, 80 skipped in 285.43s (0:04:45)\n"


def _cut_out(percent_lines: int = 5) -> str:
    """The recorded orch-674 output shape, cut at the [ 62%] line."""
    return "bringing up nodes...\n\n" + "".join(f"{LINE} [{p:3d}%]\n" for p in (1, 3, 5, 20, 40, 62))


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _stamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


@pytest.fixture
def root(tmp_path):
    (tmp_path / "var" / "locks" / "merge.lock.queue").mkdir(parents=True)
    (tmp_path / "var" / "agentops" / "merge_suite").mkdir(parents=True)
    (tmp_path / "var" / "agentops" / "results").mkdir(parents=True)
    return tmp_path


def _log(root, minutes=(5, 5, 6, 7, 5, 9)):
    lines = [f"2026-10-09 19:0{i} | orch-{i} | pushed | suite {m} min, 6 workers | 0 failed | output x"
             for i, m in enumerate(minutes)]
    lines.append("2026-10-09 19:09 | commit | pushed | suite 0 s | 0 failed | output -")
    (root / "var" / "agentops" / "merge.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _running_lock(root, started, out_text):
    d = root / "var" / "agentops" / "merge_suite"
    (d / "20261009T190000-000000-orch-674.out").write_text(FINISHED, encoding="utf-8")
    cur = d / "20261009T194000-000001-orch-677.out"
    cur.write_text(out_text, encoding="utf-8")
    (root / "var" / "locks" / "merge.lock").write_text(
        json.dumps({"pid": os.getpid(), "started": _stamp(started), "token": "t", "progress": str(cur)}),
        encoding="utf-8")
    (root / "var" / "locks" / "merge.lock.holding").write_text(
        json.dumps({"pid": os.getpid(), "ns": 1, "info": {"kind": "merge", "branch": "orch-677"}}),
        encoding="utf-8")


def _ticket(root, ns, pid, branch, issues=None):
    info = {"kind": "merge", "branch": branch}
    if issues is not None:
        info["issues"] = issues
    (root / "var" / "locks" / "merge.lock.queue" / f"{ns:020d}-{pid}-0").write_text(json.dumps(info), encoding="utf-8")


def test_row1_percent_tests_and_eta_formula(root):
    _log(root)
    t0 = int(time.time()) - 240
    _running_lock(root, t0, _cut_out())
    snap = ms.collect(root, clock=t0 + 240)
    r = snap["running"]
    assert r["percent"] == 62
    assert r["tests_total"] is None and r["tests_done"] is None  # #116 A3: no plan, no events: not the 4270 of orch-674
    assert r["eta_s"] == round(240 * 38 / 62)
    assert r["branches"] == ["orch-677"] and r["issues"] == [677]
    assert r["phase"] == "suite" and r["workers"] == 6 and r["failed"] == 0


def test_eta_below_5_percent_uses_median_minus_elapsed_never_below_a_minute(root):
    _log(root)  # last 5 suites: 5, 6, 7, 5, 9 min -> median 6 min
    t0 = int(time.time()) - 100
    _running_lock(root, t0, f"{LINE} [  3%]\n")
    assert ms.collect(root, clock=t0 + 100)["running"]["eta_s"] == 360 - 100
    assert ms.collect(root, clock=t0 + 4000)["running"]["eta_s"] == 60  # #61: floored at 1 min


def test_row2_waiting_in_arrival_order_dead_pid_absent(root):
    _log(root)
    t0 = int(time.time()) - 240
    _running_lock(root, t0, _cut_out())
    base = int((t0 + 200) * 1e9)
    _ticket(root, base + 1, os.getpid(), "orch-621", [621])
    _ticket(root, base + 2, _dead_pid(), "orch-999", [999])
    _ticket(root, base + 3, os.getpid(), "orch-650")  # no issue field -> [650]
    snap = ms.collect(root, clock=t0 + 240)
    w = snap["waiting"]
    assert [x["branch"] for x in w] == ["orch-621", "orch-650"]
    assert [x["issues"] for x in w] == [[621], [650]]
    assert [x["position"] for x in w] == [1, 2]
    eta, median = snap["running"]["eta_s"], 360
    assert [x["eta_start_s"] for x in w] == [eta, eta]  # #120: two compatible waiters share one batch
    assert snap["queue_end_s"] == eta + median
    assert w[0]["waited_s"] == 40


def _queue(root, entries, started_ago=240):
    """A running suite (some eta) + median 1320 s; entries in queue order: ("merge", {key overrides}) or ("commit", needs_suite)."""
    _log(root, minutes=(22,) * 6)  # median 1320 s
    t0 = int(time.time()) - started_ago
    _running_lock(root, t0, f"{LINE} [ 67%]\n")
    base = int((t0 + 200) * 1e9)
    for n, (kind, extra) in enumerate(entries, 1):
        if kind == "merge":
            info = {"kind": "merge", "branch": f"orch-{n}", "suite": "s", "remote": "r", "main": "main", "cwd": "c", **extra}
        else:
            info = {"kind": "commit", "needs_suite": extra, "message": "m"}
        (root / "var" / "locks" / "merge.lock.queue" / f"{base + n:020d}-{os.getpid()}-0").write_text(
            json.dumps(info), encoding="utf-8")
    return ms.collect(root, clock=t0 + started_ago)


def test_120_six_compatible_waiters_share_one_batch(root):
    snap = _queue(root, [("merge", {})] * 6)
    eta = snap["running"]["eta_s"]
    assert [w["eta_start_s"] for w in snap["waiting"]] == [eta] * 6
    assert snap["queue_end_s"] == eta + 1320


def test_120_suite_commit_between_waiters_is_a_barrier(root):
    entries = [("merge", {})] * 3 + [("commit", True)] + [("merge", {})] * 3
    snap = _queue(root, entries)
    eta = snap["running"]["eta_s"]
    assert [w["eta_start_s"] for w in snap["waiting"]] == [eta] * 3 + [eta + 2 * 1320] * 3
    assert snap["queue_end_s"] == eta + 3 * 1320


def test_120_no_suite_commit_is_passed_over(root):
    entries = [("merge", {})] * 2 + [("commit", False)] + [("merge", {})] * 2
    snap = _queue(root, entries)
    eta = snap["running"]["eta_s"]
    assert [w["eta_start_s"] for w in snap["waiting"]] == [eta] * 4
    assert snap["queue_end_s"] == eta + 1320


def test_120_different_main_waits_for_a_later_batch(root):
    entries = [("merge", {})] * 4 + [("merge", {"main": "other"})] + [("merge", {})]
    snap = _queue(root, entries)
    eta = snap["running"]["eta_s"]
    assert [w["eta_start_s"] for w in snap["waiting"]] == [eta] * 4 + [eta + 1320] + [eta]
    assert snap["queue_end_s"] == eta + 2 * 1320


def test_120_queue_end_without_waiters_is_the_running_eta_or_zero(root):
    _log(root)
    assert ms.collect(root)["queue_end_s"] == 0
    t0 = int(time.time()) - 240
    _running_lock(root, t0, _cut_out())
    snap = ms.collect(root, clock=t0 + 240)
    assert snap["queue_end_s"] == snap["running"]["eta_s"]


def test_120_batch_starts_barrier_ends_the_walk():
    keys_a, keys_b = ("s", "r", "m", "c"), ("s", "r", "x", "c")
    starts, end = ms.batch_starts([keys_a, None, keys_a, keys_b, keys_a], 100, 1000)
    assert starts == [100, 1100, 2100, 3100, 2100]
    assert end == 4100
    assert ms.batch_starts([], 100, 1000) == ([], 100)


def test_row3_no_lock_running_null_and_landed(root, capsys):
    for i in range(7):
        (root / "var" / "agentops" / "results" / f"2026100919{i:02d}00-merge-orch-{i}.json").write_text(
            json.dumps({"argv": ["py", "-m", "tools.agentops", "merge", f"orch-{i}"], "rc": 0,
                        "started": f"2026-10-09T19:{i:02d}:00", "ended": f"2026-10-09T19:{i:02d}:30"}),
            encoding="utf-8")
    (root / "var" / "agentops" / "results" / "20261009195900-merge-orch-9.json").write_text(
        json.dumps({"argv": ["py", "-m", "tools.agentops", "merge", "orch-9"], "started": "2026-10-09T19:59:00"}),
        encoding="utf-8")  # still running: no ended
    snap = ms.collect(root)
    assert snap["running"] is None and snap["waiting"] == []
    # newest first, at most LANDED_MAX (60 since #995: the pane pages «Готово» itself), so all 7 here
    assert [d["branch"] for d in snap["landed"]] == [f"orch-{i}" for i in range(6, -1, -1)][: ms.LANDED_MAX]
    assert snap["landed"][0]["issues"] == [6] and snap["landed"][0]["queued_to_land_s"] == 30
    json.dumps(snap)


def test_116_failed_is_each_landed_rows_own_attempt_count(root):
    # #116 B1: a did-not-land row carries its own attempt's red-suite `failed` count (the card's «упали тесты: N»);
    # a landed row carries none; a branch's one row is its latest attempt, with that attempt's own count
    res = root / "var" / "agentops" / "results"

    def _res(stamp, branch, rc, ended, **over):
        d = {"argv": ["py", "-m", "tools.agentops", "merge", branch], "rc": rc,
             "started": "2026-10-09T19:00:00", "ended": ended, **over}
        (res / f"{stamp}-merge-{branch}.json").write_text(json.dumps(d), encoding="utf-8")

    _res("20261009190000", "orch-a", 1, "2026-10-09T19:10:00", reason="red-suite", failed=2)
    _res("20261009191000", "orch-b", 0, "2026-10-09T19:20:00")
    _res("20261009192000", "orch-c", 1, "2026-10-09T19:30:00", reason="red-suite", failed=3)
    _res("20261009193000", "orch-c", 1, "2026-10-09T19:40:00", reason="red-suite", failed=1)
    landed = {d["branch"]: d for d in ms.collect(root)["landed"]}
    assert landed["orch-a"]["failed"] == 2
    assert landed["orch-b"].get("failed") is None
    assert landed["orch-c"]["failed"] == 1 and landed["orch-c"]["attempts"] == 2


def test_cli_json_and_text_exit_zero(root, monkeypatch, capsys):
    from tools.agentops import cli

    orig = ms.collect
    monkeypatch.setattr(ms, "collect", lambda *a, **k: orig(root))
    for argv in (["merge-status", "--json"], ["merge-status"]):
        assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert json.loads(out.split("\n}\n")[0] + "\n}")["running"] is None
    assert "no suite running" in out


def test_698_selected_suite_total_is_the_selection_not_the_last_full_suite(root):
    """The 00:06 line: a suite out starting `selected 141 of 4382` shows 141 as total, never 4382 or the earlier 4190."""
    _log(root)
    t0 = int(time.time()) - 60
    _running_lock(root, t0, "selected 141 of 4382 (map e86aaaa1)\n" + _cut_out())
    r = ms.collect(root, clock=t0 + 60)["running"]
    assert r["tests_total"] == 141
    assert r["tests_done"] == round(62 * 141 / 100)
    assert "/141 tests" in ms.render_text({"running": r, "full_run": None, "waiting": [], "landed": []})


def test_698_full_run_on_main_is_its_own_row_next_to_a_queued_branch(root):
    base = root / "var" / "agentops"
    (base / "full_run").mkdir()
    (base / "full_run" / "aaaa1111.out").write_text(FINISHED, encoding="utf-8")
    time.sleep(0.05)
    t0 = time.time()
    (base / "full_run.lock").write_text(json.dumps({"pid": os.getpid(), "started": _stamp(t0 - 1)}), encoding="utf-8")
    (base / "full_run" / "bbbb2222.out").write_text(_cut_out(), encoding="utf-8")
    _write_plan(base / "full_run" / "bbbb2222.out", 4270)  # #123: the running run's own plan, not the finished run's total
    _ticket(root, int(t0 * 1e9), os.getpid(), "orch-700", [700])
    snap = ms.collect(root, clock=t0 + 30)
    f = snap["full_run"]
    assert snap["running"] is None and [w["branch"] for w in snap["waiting"]] == ["orch-700"]
    assert f["commit"] == "bbbb2222" and f["percent"] == 62 and f["tests_total"] == 4270 and f["failed"] == 0
    lines = ms.render_text(snap).splitlines()
    assert lines[0] == "merge line: no suite running"
    assert lines[1].startswith("full run on main bbbb2222: 62%, 2647/4270 tests, 0 failed")
    assert lines[2].startswith("waiting 1: orch-700")


def test_698_dead_full_run_lock_shows_no_row(root):
    base = root / "var" / "agentops"
    (base / "full_run.lock").write_text(json.dumps({"pid": _dead_pid(), "started": _stamp(time.time())}), encoding="utf-8")
    assert ms.collect(root)["full_run"] is None


def _full_run_with_finished_1500(root, t0, cut_text, plan=True):
    """#123: a finished full run of 1500 tests (older out) and the running full run (`cut_text`), with its plan of 3008."""
    base = root / "var" / "agentops"
    d = base / "full_run"
    d.mkdir(exist_ok=True)
    old = d / "aaaa1111.out"
    old.write_text("1500 passed in 100.00s\n", encoding="utf-8")
    os.utime(old, (t0 - 100, t0 - 100))
    (base / "full_run.lock").write_text(json.dumps({"pid": os.getpid(), "started": _stamp(t0 - 1)}), encoding="utf-8")
    cur = d / "bbbb2222.out"
    cur.write_text(cut_text, encoding="utf-8")
    if plan:
        _write_plan(cur, 3008)
    return cur


def test_123_full_run_total_is_the_running_runs_plan_not_the_finished_runs(root):
    """#123 A1: a finished run of 1500 and a running full run with a plan of 3008: the running row says 3008 on every path."""
    t0 = time.time()
    _full_run_with_finished_1500(root, t0, _cut_out())
    snap = ms.collect(root, clock=t0 + 30)
    f = snap["full_run"]
    assert f["percent"] == 62 and f["tests_total"] == 3008 and f["tests_done"] == round(62 * 3008 / 100)
    assert ms._full_run(root, t0 + 30)["tests_total"] == 3008
    line = ms.render_text(snap).splitlines()[1]
    assert "/3008 tests" in line and "/1500" not in line


def test_123_full_run_without_a_plan_has_no_total_not_the_finished_runs(root):
    """#123 A1: a running full run with no plan yet has no total; the finished run's 1500 is never borrowed."""
    t0 = time.time()
    _full_run_with_finished_1500(root, t0, _cut_out(), plan=False)
    f = ms.collect(root, clock=t0 + 30)["full_run"]
    assert f["percent"] == 62 and f["tests_total"] is None and f["tests_done"] is None
    assert "1500" not in ms.render_text(ms.collect(root, clock=t0 + 30))


def _check_file(root, ticket, state, pid):
    d = root / "var" / "locks" / "merge.lock.checks"
    d.mkdir(exist_ok=True)
    (d / f"{ticket}.json").write_text(json.dumps(
        {"state": state, "pid": pid, "branch": "orch-1", "since": "2026-10-10 10:00:00", "ids": []}), encoding="utf-8")


def _waiter(root):
    _ticket(root, int(time.time() * 1e9), os.getpid(), "orch-1", [1])
    return next((root / "var" / "locks" / "merge.lock.queue").iterdir()).name


def test_765_waiter_check_state_in_text_and_json(root):
    name = _waiter(root)
    _check_file(root, name, "running", os.getpid())
    snap = ms.collect(root)
    assert snap["waiting"][0]["check"]["state"] == "running"
    assert "checking its tests" in ms.render_text(snap)
    _check_file(root, name, "green", None)
    snap = ms.collect(root)
    assert snap["waiting"][0]["check"]["state"] == "green"
    assert "its tests passed alone" in ms.render_text(snap)


@pytest.mark.parametrize("state,pid", [("red", None), ("stopped", None), ("skipped", None), ("running", "dead"), (None, None)])
def test_765_no_wording_for_red_stopped_skipped_missing_or_stale(root, state, pid):
    name = _waiter(root)
    if state:
        _check_file(root, name, state, _dead_pid() if pid == "dead" else pid)
    snap = ms.collect(root)
    assert snap["waiting"][0]["check"] is None
    text = ms.render_text(snap)
    assert "checking" not in text and "passed alone" not in text


def test_765_check_out_is_not_the_suite_output_nor_a_finished_total(root):
    _log(root)
    t0 = int(time.time()) - 60
    _running_lock(root, t0, _cut_out())
    d = root / "var" / "agentops" / "merge_suite"
    _write_plan(d / "20261009T194000-000001-orch-677.out", 4270)  # the run's own plan (#116 A1)
    (d / "20261009T195000-000002-check-orch-9.out").write_text("selected 12 of 4382\n" + f"{LINE} [100%]\n12 passed in 3.00s\n", encoding="utf-8")
    assert ms.collect(root, clock=t0 + 60)["running"]["tests_total"] == 4270  # not 12
    assert ms._newest_finished_total(d, None) == 4270


def test_765_check_out_newer_than_running_one_is_not_read_as_its_total(root):
    d = root / "var" / "agentops" / "merge_suite"
    (d / "20261009T190000-000000-orch-1.out").write_text(FINISHED, encoding="utf-8")
    (d / "20261009T230000-000009-check-orch-2.out").write_text("12 passed in 3.00s\n", encoding="utf-8")
    assert ms._newest_finished_total(d, str(d / "20261009T230000-000009-orch-3.out")) == 4270


def _write_plan(out, n: int) -> None:
    """A run's own plan: one number per test in `<out>.plan.json` (#758)."""
    out.with_name(out.name + ".plan.json").write_text(json.dumps({"seconds": [1.0] * n}), encoding="utf-8")


def _events(out, ended: int, worker: str = "main") -> None:
    """A run's events: `<ts> S|E <worker> <nodeid>` lines in `<out>.events.<worker>` (#900); one start and one end per test."""
    lines = "".join(f"{1000 + i} S {worker} t::test_{i}\n{1000 + i} E {worker} t::test_{i}\n" for i in range(ended))
    out.with_name(out.name + f".events.{worker}").write_text(lines, encoding="utf-8")


def _full_suite_run(root, t0, ended: dict) -> None:
    """A running full suite (`_running_lock`) with its plan of 3008 tests, one events file per worker in `ended`,
    and a newer finished out of one test (#116: never the total)."""
    _running_lock(root, t0, "full suite: data change\n" + _cut_out())
    d = root / "var" / "agentops" / "merge_suite"
    cur = d / "20261009T194000-000001-orch-677.out"
    _write_plan(cur, 3008)
    for worker, n in ended.items():
        _events(cur, n, worker)
    (d / "20261009T200000-000002-i999.out").write_text("1 passed in 0.10s\n", encoding="utf-8")


def test_a1_a2_running_full_suite_counts_its_own_plan_and_its_own_ended_tests(root):
    """#116 A1/A2: a full suite's total is its plan (3008 tests), its done its E lines (1500), never the 1 of a newer finished out."""
    t0 = int(time.time()) - 60
    _full_suite_run(root, t0, {"main": 1500})
    r = ms.collect(root, clock=t0 + 60)["running"]
    assert r["tests_total"] == 3008 and r["tests_done"] == 1500


def test_a2_done_is_the_sum_of_the_ended_tests_of_all_workers_files(root):
    """#116 A2: all workers' events files of one run are summed (main 1000 + w1 700)."""
    t0 = int(time.time()) - 60
    _full_suite_run(root, t0, {"main": 1000, "w1": 700})
    assert ms.collect(root, clock=t0 + 60)["running"]["tests_done"] == 1700


def test_a2_done_is_capped_at_the_total(root):
    """#116 A2: the summed E lines (2000 + 1500) are capped at the plan's 3008."""
    t0 = int(time.time()) - 60
    _full_suite_run(root, t0, {"main": 2000, "w1": 1500})
    r = ms.collect(root, clock=t0 + 60)["running"]
    assert r["tests_total"] == 3008 and r["tests_done"] == 3008


def test_a1_selected_running_out_total_is_its_selection(root):
    """#116 A1: `selected 1271 of 2683` is the running total, whatever the finished outs say."""
    _log(root)
    t0 = int(time.time()) - 60
    _running_lock(root, t0, "selected 1271 of 2683\n" + _cut_out())
    assert ms.collect(root, clock=t0 + 60)["running"]["tests_total"] == 1271


def test_a3_running_out_with_no_plan_and_no_events_shows_percent_only(root):
    """#116 A3: no plan and no events yet: the percent and no done/total pair, never the 4270 of the older finished out."""
    _log(root)
    t0 = int(time.time()) - 60
    _running_lock(root, t0, "full suite: data change\n" + _cut_out())
    snap = ms.collect(root, clock=t0 + 60)
    r = snap["running"]
    assert r["percent"] == 62 and r["tests_total"] is None and r["tests_done"] is None
    first = ms.render_text(snap).splitlines()[0]
    assert "62%, 0 failed" in first and "tests" not in first


COMMIT_MSG = "#29: third repo joins the home's targets (3 entries) and more words after"


def _commit_lock(root, started, out_text, *, kind="commit", message=COMMIT_MSG, issues=(29,)):
    d = root / "var" / "agentops" / "merge_suite"
    (d / "20261009T190000-000000-orch-674.out").write_text(FINISHED, encoding="utf-8")
    cur = d / "20261016T134438-000000-commit.out"
    cur.write_text(out_text, encoding="utf-8")
    rec = {"pid": os.getpid(), "started": _stamp(started), "token": "t", "progress": str(cur)}
    if kind is not None:
        rec.update({"kind": kind, "message": message, "issues": list(issues)})
    (root / "var" / "locks" / "merge.lock").write_text(json.dumps(rec), encoding="utf-8")


def test_row2_commit_holder_is_named_by_its_ticket_and_message_not_as_a_question_mark(root):
    _log(root)
    t0 = int(time.time()) - 240
    _commit_lock(root, t0, f"{LINE} [ 76%]\n")
    _write_plan(root / "var" / "agentops" / "merge_suite" / "20261016T134438-000000-commit.out", 4270)  # #116 A1: its own plan
    snap = ms.collect(root, clock=t0 + 240)
    r = snap["running"]
    assert r["kind"] == "commit" and r["issues"] == [29] and r["branches"] == []
    assert r["message"] == COMMIT_MSG[:60]
    assert r["percent"] == 76 and r["tests_total"] == 4270
    line = ms.render_text(snap).splitlines()[0]
    assert line.startswith(f'running: commit #29 "{COMMIT_MSG[:60]}" suite, 76%, ')
    assert "? ()" not in line


def test_row2_commit_holder_without_ticket_names_the_commit_and_message(root):
    _log(root)
    t0 = int(time.time()) - 60
    _commit_lock(root, t0, f"{LINE} [ 20%]\n", message="docs fix", issues=())
    line = ms.render_text(ms.collect(root, clock=t0 + 60)).splitlines()[0]
    assert line.startswith('running: commit "docs fix" suite, 20%, ')


def test_row2_old_record_without_kind_still_reads_as_today(root):
    _log(root)
    t0 = int(time.time()) - 60
    _commit_lock(root, t0, f"{LINE} [ 20%]\n", kind=None)
    snap = ms.collect(root, clock=t0 + 60)
    assert snap["running"]["kind"] is None
    assert ms.render_text(snap).splitlines()[0].startswith("running: ? () suite, 20%, ")


def test_row2_merge_holder_line_is_unchanged_by_the_commit_fields(root):
    _log(root)
    t0 = int(time.time()) - 240
    _running_lock(root, t0, _cut_out())
    holder = json.loads((root / "var" / "locks" / "merge.lock").read_text(encoding="utf-8"))
    holder.update({"kind": "merge", "branch": "orch-677"})
    (root / "var" / "locks" / "merge.lock").write_text(json.dumps(holder), encoding="utf-8")
    line = ms.render_text(ms.collect(root, clock=t0 + 240)).splitlines()[0]
    assert line.startswith("running: orch-677 (#677) suite, 62%, ")


# --- #61: a sane time left before the tests start counting -------------------------------------------------

def _plan(out_name_dir, tests=1873, head=56, head_s=0.01, rest_s=1.0):
    """The plan of the i53-u1+i8-u1 suite: the first tests are tiny, so a share from them is ~0."""
    cur = out_name_dir / "20261009T194000-000001-orch-677.out"
    secs = [head_s] * head + [rest_s] * (tests - head)
    (out_name_dir / (cur.name + ".plan.json")).write_text(json.dumps({"seconds": secs}), encoding="utf-8")


def _text(snap):
    return ms.render_text({"running": snap["running"], "full_run": None, "waiting": [], "landed": []})


@pytest.mark.parametrize("elapsed_min, out_text", [
    (2.0, "."),                        # one test counted, no percent line yet
    (4.0, "..."),
    (7.4, "." * 40),                   # the 16:35 sample, "0 %"
    (8.8, f"{'.' * 56} [  3%]\n"),     # 16:36:27, 56/1873 tests, 3 %
    (14.1, f"{'.' * 60} [  3%]\n"),    # 16:41:41, still 3 %
])
def test_61_row1_below_5_percent_is_median_of_recent_runs_minus_elapsed(root, elapsed_min, out_text):
    _log(root, minutes=(21, 21, 21))
    t0 = int(time.time()) - 5000
    _running_lock(root, t0, out_text)
    _plan(root / "var" / "agentops" / "merge_suite")  # the plan-based figure was 2 h 46 min .. 7 h 42 min here
    elapsed = int(elapsed_min * 60)
    r = ms.collect(root, clock=t0 + elapsed)["running"]
    assert r["eta_s"] == max(60, 21 * 60 - elapsed)
    assert r["eta_s"] <= 2 * 21 * 60
    assert "(from recent runs)" in _text(ms.collect(root, clock=t0 + elapsed))


def test_61_row1_floor_is_one_minute_when_the_suite_runs_past_the_median(root):
    _log(root, minutes=(21, 21, 21))
    t0 = int(time.time()) - 5000
    _running_lock(root, t0, "..\n")
    r = ms.collect(root, clock=t0 + 25 * 60)["running"]
    assert r["eta_s"] == 60


def test_61_row2_no_recent_run_at_zero_percent_is_not_known_yet(root):
    t0 = int(time.time()) - 5000
    _running_lock(root, t0, "...\n")
    _plan(root / "var" / "agentops" / "merge_suite")
    snap = ms.collect(root, clock=t0 + 540)
    assert snap["running"]["eta_s"] is None
    text = _text(snap)
    assert "estimate not known yet" in text and "left" not in text


def test_61_row3_49_percent_at_18_min_keeps_the_current_figure(root):
    _log(root, minutes=(21, 21, 21))
    t0 = int(time.time()) - 5000
    _running_lock(root, t0, f"{LINE} [ 49%]\n")
    r = ms.collect(root, clock=t0 + 18 * 60)["running"]
    assert r["eta_s"] == round(18 * 60 * 51 / 49)
    assert "(from recent runs)" not in _text(ms.collect(root, clock=t0 + 18 * 60))


def test_61_from_5_percent_the_figure_is_capped_at_2x_median_while_0_failed(root):
    _log(root, minutes=(21, 21, 21))
    t0 = int(time.time()) - 5000
    _running_lock(root, t0, f"{LINE} [ 20%]\n")
    assert ms.collect(root, clock=t0 + 18 * 60)["running"]["eta_s"] == 2 * 21 * 60  # uncapped: 4320
    _running_lock(root, t0, f"{'.' * 71}F [ 20%]\n")
    assert ms.collect(root, clock=t0 + 18 * 60)["running"]["eta_s"] == 18 * 60 * 80 // 20  # a red run: no cap


def _full_run_at(root, t0, out_text, recent_minutes, plan=None):
    """The full run on main, started at t0, its out `ffff9999.out`; each recent minute value is one finished earlier run."""
    base = root / "var" / "agentops"
    d = base / "full_run"
    d.mkdir(parents=True, exist_ok=True)
    (base / "full_run.lock").write_text(json.dumps({"pid": os.getpid(), "started": _stamp(t0)}), encoding="utf-8")
    for i, m in enumerate(recent_minutes):
        old = d / f"a{i:07x}.out"
        old.write_text(f"{LINE} [100%]\n4190 passed, 80 skipped in {m * 60}.00s (0:{m:02d}:00)\n", encoding="utf-8")
        ts = t0 - 1000 * (i + 1)  # older than the current run, so the current out is the newest
        os.utime(old, (ts, ts))
    cur = d / "ffff9999.out"
    cur.write_text(out_text, encoding="utf-8")
    if plan is not None:
        cur.with_name(cur.name + ".plan.json").write_text(json.dumps({"seconds": plan}), encoding="utf-8")
    return cur


def test_61_full_run_at_zero_percent_uses_recent_full_runs_minus_elapsed(root, monkeypatch):
    monkeypatch.setattr(ms, "_birth", lambda p: None)  # no creation time: the lock's start stands
    t0 = int(time.time()) - 5000
    _full_run_at(root, t0, "..\n", recent_minutes=(21, 21, 21))
    snap = ms.collect(root, clock=t0 + 120)
    assert snap["full_run"]["eta_s"] == 19 * 60  # median 21 min less 2 min elapsed
    assert snap["full_run"]["eta_s"] <= 2 * 21 * 60
    assert "(from recent runs)" in ms.render_text(snap)


def test_61_full_run_at_zero_percent_without_a_recent_full_run_is_not_known_yet(root, monkeypatch):
    monkeypatch.setattr(ms, "_birth", lambda p: None)
    t0 = int(time.time()) - 5000
    _full_run_at(root, t0, "...\n", recent_minutes=())
    snap = ms.collect(root, clock=t0 + 120)
    assert snap["full_run"]["eta_s"] is None
    text = ms.render_text(snap)
    assert "estimate not known yet" in text and "left" not in text


def test_61_full_run_at_49_percent_at_18_min_keeps_the_plan_figure(root, monkeypatch):
    monkeypatch.setattr(ms, "_birth", lambda p: None)
    t0 = int(time.time()) - 5000
    _full_run_at(root, t0, f"{'.' * 49} [ 49%]\n", recent_minutes=(21, 21, 21), plan=[1] * 100)
    snap = ms.collect(root, clock=t0 + 18 * 60)
    assert snap["full_run"]["eta_s"] == round(18 * 60 * 51 / 49)  # about 19 min: the plan's share 49 %, unchanged
    text = ms.render_text(snap)
    assert "(from recent runs)" not in text and "estimate not known yet" not in text


def test_70_red_full_runs_are_not_in_the_median(root, monkeypatch):
    monkeypatch.setattr(ms, "_birth", lambda p: None)
    t0 = int(time.time()) - 5000
    _full_run_at(root, t0, "..\n", recent_minutes=(21, 2, 2))
    d = root / "var" / "agentops" / "full_run"
    for name in ("a0000001.out", "a0000002.out"):  # two stopped-early red runs of 2 min
        (d / name).write_text("3 failed, 4000 passed in 120.00s (0:02:00)\n", encoding="utf-8")
    snap = ms.collect(root, clock=t0 + 120)
    assert snap["full_run"]["eta_s"] == 19 * 60  # the one green run of 21 min less 2 min elapsed; red ones ignored


def test_6_running_merge_carries_the_enqueue_time_of_its_ticket(root):
    # #6: the running entry's `queued` is the enqueue time of its claimed ticket (the queue name's time_ns), not the suite start
    _log(root)
    t0 = int(time.time()) - 240
    _running_lock(root, t0, _cut_out())
    enqueued = t0 - 600
    ns = enqueued * 1_000_000_000
    _ticket(root, ns, 4321, "orch-677", [677])
    batch = root / "var" / "locks" / "merge.lock.batch"
    batch.mkdir(parents=True)
    (batch / f"{ns:020d}-4321-0.claim").write_text(json.dumps({"pid": os.getpid(), "started": _stamp(t0)}), encoding="utf-8")
    r = ms.collect(root, clock=t0 + 240)["running"]
    assert r["queued"] == time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(enqueued))
    assert r["queued"] != r["started"]
