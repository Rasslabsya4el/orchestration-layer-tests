"""#343 unit 5 (rules 1, 2, 4, 5): ``map_run`` asks ``heavy_place.decide("map-build", ...)`` before it builds.

- github: ``map-build on github``; the map is built on GitHub and installed; red on GitHub holds the merge line as the PC
  build does (marker with the ids, ``safety run: red``); a GitHub fallback (any reason) builds on the PC and is never red;
- pc: today's build, with ``map-build on pc (<reason>)``;
- skip (coalesced): ``map-build on skip (coalesced)``, nothing built, the sha stays pending (newest wins) and one detached
  delayed re-run is requested (no pile-up); the re-run builds the pending sha once the interval has passed.
- the minutes of a ``map-build`` record stay out of the merge median but count in the used minutes of the cap; ``build_on_github`` records the
  Plan's repo name when given; a GitHub install keeps the map folder's run-state files.
Seams: ``map_run.run_map_run`` / ``run_map_run_delayed`` with injected ``PlaceDeps`` (settings, plan, the GitHub build,
the re-run spawn, the clock); GitHub itself is faked, no process is started and no suite runs."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tools.agentops import core_settings, github_minutes, heavy_place, map_github, map_run, merge_github
from tools.agentops.test_select import map_dir, read_meta, write_meta

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
S1 = "1" * 40
S2 = "2" * 40
CMD = "python -m pytest tests -q"
PLAN = merge_github.Plan(remote="https://github.com/o/t.git", own_repo=False, repo_slug="o/r", repo_name="the-folder")
ON = {**core_settings.DEFAULTS, "github_suite": True, "repo": "o/r", "test_copy_remote": PLAN.remote}
OFF = {**core_settings.DEFAULTS, "github_suite": False, "repo": "o/r"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A tmp main checkout (its map dir), a tmp merge log, a fake PC ``build_map`` and a fake GitHub build."""
    root = tmp_path / "root"
    root.mkdir()
    e = SimpleNamespace(root=root, mdir=map_dir(root), log=tmp_path / "merge.log", pc=[], gh=[], reruns=[],
                        settings=ON, plan=PLAN, github_result=None, github_effect=None, rerun_alive=False,
                        clock=NOW)

    def fake_build(suite_dir, sha, mdir, suite_cmd, bound=None):
        e.pc.append(sha)
        meta = {"map_commit": sha, "last_green_commit": sha, "total": 4, "built_at": "t"}
        write_meta(mdir, meta)
        return meta

    def fake_github(**kw):
        e.gh.append(kw)
        if e.github_effect:
            e.github_effect(kw)
        return e.github_result

    def fake_rerun(sha, wait_seconds, cwd_arg):
        e.reruns.append((sha, wait_seconds, cwd_arg))
        return os.getpid() if e.rerun_alive else 2**22 + 12345  # a live pid / a pid no process has

    monkeypatch.setattr(map_run, "build_map", fake_build)
    monkeypatch.setattr(map_run, "ensure_suite_worktree", lambda *a, **k: root)
    monkeypatch.setattr(map_run, "machine_holds", lambda: False)
    monkeypatch.setattr(map_run, "sha_on_main", lambda root, sha: True)
    e.deps = lambda: map_run.PlaceDeps(
        load_settings=lambda: e.settings,
        place=heavy_place.Deps(plan_for=lambda settings, root, now=None: (e.plan, "")),
        github=fake_github, github_ref=_no_ref, spawn_rerun=fake_rerun, clock=lambda: e.clock)
    return e


class _no_ref:
    """The local branch the GitHub build is sent from: nothing here."""

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run(e, sha, **kw):
    return map_run.run_map_run(sha, root=e.root, suite_cmd=CMD, shared=(), log_path=e.log, place_deps=e.deps(), **kw)


def _texts(log):
    if not log.exists():
        return []
    out = []
    for line in log.read_text(encoding="utf-8").splitlines():
        m = re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \| map-run \| (.*)", line)
        assert m, line
        out.append(m.group(1))
    return out


def _install(meta_total=7):
    """What ``build_on_github`` leaves on green: the map in ``map_dir``, stamped for the sha."""
    def effect(kw):
        write_meta(kw["map_dir"], {"map_commit": kw["sha"], "last_green_commit": kw["sha"], "total": meta_total,
                                   "built_at": "2026-10-23T11:00:00Z"})
    return effect


GREEN = map_github.MapBuildResult(status="green", minutes_used=9, built_at="2026-10-23T11:00:00Z", green=True, run_id=77)


# -- github -------------------------------------------------------------------------------------------------------------

def test_github_green_installs_the_map_logs_the_lines_and_clears_the_red_marker(env):
    env.mdir.mkdir(parents=True)
    (env.mdir / "safety_red.json").write_text("{}", encoding="utf-8")
    env.github_result, env.github_effect = GREEN, _install(7)
    assert _run(env, S1) == 0
    assert env.pc == []
    call = env.gh[0]
    assert call["sha"] == S1 and call["map_dir"] == env.mdir and call["settings"] == ON
    assert call["branch"] == "map-build/" + S1[:12]
    assert call["repo_name"] == "the-folder"  # the Plan's name: github_minutes.over_budget keys on it
    texts = _texts(env.log)
    assert texts[0] == "map-build on github"
    assert texts[1] == f"map built | {env.mdir / 'testmondata'} | {S1[:12]} | 7 tests | github run 77"
    assert texts[2:] == [f"safety run: green | {S1[:12]}"]
    assert not (env.mdir / "safety_red.json").exists()
    assert read_meta(env.mdir)["map_commit"] == S1


def test_github_red_writes_the_marker_with_the_ids_and_holds_the_line_like_the_pc_build(env):
    write_meta(env.mdir, {"map_commit": S2, "last_green_commit": S2, "total": 3, "built_at": "t"})
    env.github_result = map_github.MapBuildResult(status="red", minutes_used=8, green=False, run_id=78,
                                                  failed_ids=["tests/a.py::t1", "tests/b.py::t2"])
    assert _run(env, S1) == 1
    assert env.pc == []
    marker = json.loads((env.mdir / "safety_red.json").read_text(encoding="utf-8"))
    assert marker == {"sha": S1, "ids": ["tests/a.py::t1", "tests/b.py::t2"], "map_commit": S2}
    assert _texts(env.log) == [
        "map-build on github",
        f"safety run: red tests/a.py::t1,tests/b.py::t2 | {S1[:12]} | github run 78"]


@pytest.mark.parametrize("fallback", [
    map_github.MapBuildResult(status="fallback", reason="lost", minutes_used=3, run_id=5),
    map_github.MapBuildResult(status="fallback", reason="refused: 2 hits"),
    map_github.MapBuildResult(status="fallback", reason="no-places"),
])
def test_github_fallback_builds_on_the_pc_and_is_never_red(env, fallback):
    env.github_result = fallback
    assert _run(env, S1) == 0
    assert env.pc == [S1]
    assert not (env.mdir / "safety_red.json").exists()
    texts = _texts(env.log)
    assert texts[0] == f"map-build on pc (github fallback: {fallback.reason})"
    assert "map-build on github" not in texts
    assert texts[-1] == f"safety run: green | {S1[:12]}"


def test_a_github_build_that_raises_is_a_fallback_never_red(env):
    def boom(kw):
        raise RuntimeError("gh exploded")
    env.github_effect = boom
    assert _run(env, S1) == 0
    assert env.pc == [S1]
    assert _texts(env.log)[0] == "map-build on pc (github fallback: error: gh exploded)"
    assert not (env.mdir / "safety_red.json").exists()


# -- pc -----------------------------------------------------------------------------------------------------------------

def test_pc_place_builds_as_today_with_its_reason_in_the_line(env):
    env.settings = OFF
    assert _run(env, S1) == 0
    assert env.gh == [] and env.pc == [S1]
    assert _texts(env.log)[0] == "map-build on pc (github_suite off)"
    assert _texts(env.log)[-1] == f"safety run: green | {S1[:12]}"


def test_unreadable_settings_build_on_the_pc_with_the_reason(env):
    def broken():
        raise RuntimeError("missing settings file")
    deps = env.deps()
    deps.load_settings = broken
    rc = map_run.run_map_run(S1, root=env.root, suite_cmd=CMD, shared=(), log_path=env.log, place_deps=deps)
    assert rc == 0 and env.pc == [S1]
    assert _texts(env.log)[0] == "map-build on pc (github settings unreadable)"


def test_every_build_notes_its_start_for_the_coalescing_check(env):
    env.settings = OFF
    _run(env, S1)
    assert heavy_place.read_build_start(env.root) == NOW


# -- skip (coalesced) ---------------------------------------------------------------------------------------------------

def _recent_build(e, minutes_ago):
    heavy_place.note_build_start(e.root, NOW - timedelta(minutes=minutes_ago))


def test_coalesced_build_logs_skip_keeps_the_sha_pending_and_asks_one_delayed_rerun(env):
    _recent_build(env, 10)
    assert _run(env, S1, cwd_arg="C:/main") == 0
    assert env.pc == [] and env.gh == []
    texts = _texts(env.log)
    assert texts[0] == "map-build on skip (coalesced)"
    assert "20 min" in texts[1] and S1[:12] in texts[1]  # 30 - 10 left, said for a human
    assert heavy_place.pending(env.root) == S1
    (sha, wait, cwd_arg), = env.reruns
    assert (sha, cwd_arg) == (S1, "C:/main")
    assert 20 * 60 <= wait <= 20 * 60 + 30


def test_a_newer_landing_while_a_rerun_waits_replaces_the_pending_sha_and_starts_no_second_rerun(env):
    _recent_build(env, 10)
    env.rerun_alive = True
    _run(env, S1)
    _run(env, S2)
    assert heavy_place.pending(env.root) == S2  # the newest main wins
    assert len(env.reruns) == 1  # one waiter, no pile-up


def test_a_dead_rerun_is_replaced_by_a_new_one(env):
    _recent_build(env, 10)
    _run(env, S1)
    _run(env, S2)
    assert [r[0] for r in env.reruns] == [S1, S2]


def test_a_build_after_the_interval_takes_the_pending_marker(env):
    _recent_build(env, 10)
    _run(env, S1)
    env.clock = NOW + timedelta(minutes=31)
    env.github_result, env.github_effect = GREEN, _install(5)
    _run(env, S2)
    assert [call["sha"] for call in env.gh] == [S2] and env.pc == []
    assert heavy_place.pending(env.root) is None


def test_the_delayed_rerun_builds_the_pending_sha_once_the_interval_has_passed(env):
    _recent_build(env, 10)
    _run(env, S1)
    env.clock = NOW + timedelta(minutes=21)
    env.github_result, env.github_effect = GREEN, _install(5)
    slept = []
    rc = map_run.run_map_run_delayed(S1, 1200.0, root=env.root, suite_cmd=CMD, shared=(), log_path=env.log,
                                     place_deps=env.deps(), sleep=slept.append)
    assert rc == 0 and slept == [1200.0]
    assert env.gh[0]["sha"] == S1
    assert "map-build on github" in _texts(env.log)
    assert heavy_place.pending(env.root) is None


def test_the_delayed_rerun_with_nothing_pending_builds_nothing(env):
    env.github_result = GREEN
    rc = map_run.run_map_run_delayed(S1, 5.0, root=env.root, suite_cmd=CMD, shared=(), log_path=env.log,
                                     place_deps=env.deps(), sleep=lambda s: None)
    assert rc == 0 and env.gh == [] and env.pc == []


def test_the_delayed_rerun_builds_the_newest_pending_sha_not_the_one_it_was_asked_for(env):
    env.settings = OFF
    heavy_place.note_pending(env.root, S2)
    map_run.run_map_run_delayed(S1, 5.0, root=env.root, suite_cmd=CMD, shared=(), log_path=env.log,
                                place_deps=env.deps(), sleep=lambda s: None)
    assert env.pc == [S2]


# -- a coalesced sha is always built: the waiter is checked, re-armed and swept -------------------------------------------

def _mark(e):
    return json.loads((e.mdir / map_run.RERUN_MARK).read_text(encoding="utf-8"))


def test_the_rerun_mark_stores_the_pid_its_start_and_when_it_is_due(env):
    _recent_build(env, 10)
    env.rerun_alive = True
    _run(env, S1)
    mark = _mark(env)
    assert mark["pid"] == os.getpid() and mark["sha"] == S1
    assert datetime.fromisoformat(mark["due_at"]) == NOW + timedelta(minutes=20, seconds=map_run.RERUN_MARGIN_S)
    assert isinstance(mark["start"], float)  # the pid's create time: a reused pid is told apart by it


def test_a_landing_that_ends_early_restarts_a_waiter_whose_process_is_dead(env, monkeypatch):
    _recent_build(env, 10)
    _run(env, S1)  # the fake spawn's pid is a pid no process has
    assert len(env.reruns) == 1
    monkeypatch.setattr(map_run, "machine_holds", lambda: True)  # the landing builds nothing and posts nothing
    env.clock = NOW + timedelta(minutes=5)
    _run(env, S2)
    assert [r[0] for r in env.reruns] == [S1, S1]  # the sweep waits for the sha that is pending
    assert 15 * 60 <= env.reruns[1][1] <= 15 * 60 + 30


def test_a_stale_mark_past_its_due_time_and_the_bound_is_not_a_live_waiter(env, monkeypatch):
    _recent_build(env, 10)
    env.rerun_alive = True  # the pid is alive (it is this process), but the mark is old
    _run(env, S1)
    monkeypatch.setattr(map_run, "machine_holds", lambda: True)
    env.clock = NOW + timedelta(minutes=20, seconds=map_run.RERUN_MARGIN_S + map_run.RERUN_STALE_S - 60)
    _run(env, S2)
    assert len(env.reruns) == 1  # still within the bound: the waiter is trusted
    env.clock = NOW + timedelta(minutes=20, seconds=map_run.RERUN_MARGIN_S + map_run.RERUN_STALE_S + 60)
    _run(env, S2)
    assert len(env.reruns) == 2


def test_a_mark_whose_pid_is_alive_but_started_at_another_time_is_a_reused_pid(env):
    pytest.importorskip("psutil")
    _recent_build(env, 10)
    env.mdir.mkdir(parents=True, exist_ok=True)
    (env.mdir / map_run.RERUN_MARK).write_text(json.dumps(
        {"pid": os.getpid(), "start": 12345.0, "sha": S1,
         "due_at": (NOW + timedelta(minutes=20)).isoformat()}), encoding="utf-8")
    _run(env, S1)
    assert len(env.reruns) == 1  # the live pid is another process: a new waiter


def test_no_waiter_is_started_when_nothing_is_pending(env):
    env.settings = OFF
    _run(env, S1)
    assert env.reruns == []


def test_a_wake_while_the_machine_is_busy_arms_one_more_waiter_and_keeps_the_sha_pending(env, monkeypatch):
    _recent_build(env, 10)
    env.rerun_alive = True
    _run(env, S1)
    env.reruns.clear()
    monkeypatch.setattr(map_run, "machine_holds", lambda: True)
    rc = map_run.run_map_run_delayed(S1, 5.0, root=env.root, suite_cmd=CMD, shared=(), log_path=env.log,
                                     place_deps=env.deps(), sleep=lambda s: None)
    assert rc == 0 and env.pc == [] and env.gh == []
    (sha, wait, _cwd), = env.reruns
    assert sha == S1 and wait == map_run.WAKE_RETRY_S
    assert heavy_place.pending(env.root) == S1
    assert _mark(env)["retries"] == 1


def test_a_wake_that_has_retried_the_limit_logs_machine_busy_and_leaves_the_sha_pending(env, monkeypatch):
    _recent_build(env, 10)
    env.rerun_alive = True
    _run(env, S1)
    mark = {**_mark(env), "retries": map_run.MAX_WAKE_RETRIES}
    (env.mdir / map_run.RERUN_MARK).write_text(json.dumps(mark), encoding="utf-8")
    env.reruns.clear()
    monkeypatch.setattr(map_run, "machine_holds", lambda: True)
    map_run.run_map_run_delayed(S1, 5.0, root=env.root, suite_cmd=CMD, shared=(), log_path=env.log,
                                place_deps=env.deps(), sleep=lambda s: None)
    assert env.reruns == []
    assert f"map pending | {S1[:12]} | machine busy" in _texts(env.log)
    assert heavy_place.pending(env.root) == S1  # the next landing's sweep starts a waiter again


# -- minutes: the map-build kind stays out of the merge arithmetic ------------------------------------------------------

def _runs_log(tmp_path, rows):
    path = tmp_path / "github_runs.jsonl"
    path.write_text("\n".join(json.dumps({"repo": "r", "month": "2026-10", "own_runner": True, **row})
                              for row in rows) + "\n", encoding="utf-8")
    return path


def test_map_build_minutes_count_in_the_used_minutes_but_not_in_the_merge_median(tmp_path):
    path = _runs_log(tmp_path, [{"minutes": 10}, {"minutes": 50, "kind": "map-build"}, {"minutes": 12, "kind": "merge"}])
    assert github_minutes.median_merge_minutes("r", path=path) == 11  # 10 and 12, never 50
    assert github_minutes.minutes_used("r", "2026-10", path=path) == 72  # 22 + 50: the cap bills a map build too


# -- build_on_github: the Plan's repo name ------------------------------------------------------------------------------

def test_build_on_github_records_the_given_repo_name_and_defaults_to_the_slug_basename(tmp_path):
    records = []
    deps = map_github.Deps(send=lambda *a: (_ for _ in ()).throw(RuntimeError("no")), record=records.append)
    settings = {"repo": "owner/slug-name", "test_copy_remote": "https://github.com/o/t.git"}
    kw = dict(root=tmp_path, branch="b", sha="s" * 40, settings=settings, map_dir=tmp_path / "m", deps=deps)
    map_github.build_on_github(**kw)
    map_github.build_on_github(**kw, repo_name="folder-name")
    assert [r["repo"] for r in records] == ["slug-name", "folder-name"]


def test_decide_gives_the_plans_repo_name_on_a_github_place(tmp_path):
    place = heavy_place.decide("map-build", ON, now=NOW, root=tmp_path,
                               deps=heavy_place.Deps(plan_for=lambda s, r, now=None: (PLAN, "")))
    assert place.where == "github" and place.repo_name == "the-folder"


# -- the install keeps the map folder's run-state files ------------------------------------------------------------------

def test_the_github_install_carries_the_lock_and_markers_over_to_the_new_map(tmp_path):
    mdir = tmp_path / "test_map"
    mdir.mkdir()
    for name in ("full_run.lock", "safety_pending", "build_started.json", "safety_red.json", "build-abc.out"):
        (mdir / name).write_text(name, encoding="utf-8")
    (mdir / "testmondata").write_bytes(b"old")
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "testmondata").write_bytes(b"new")
    map_github._install(staged, mdir)
    assert (mdir / "testmondata").read_bytes() == b"new"
    for name in ("full_run.lock", "safety_pending", "build_started.json", "safety_red.json", "build-abc.out"):
        assert (mdir / name).read_text(encoding="utf-8") == name


def test_the_install_takes_a_state_file_written_into_the_old_folder_during_the_swap(tmp_path, monkeypatch):
    """A marker written into the old folder after the old one was set aside's decision but before the new one is in
    (between a copy and the rename, in the old order) must not be lost: the copy runs after the swap."""
    mdir = tmp_path / "test_map"
    mdir.mkdir()
    (mdir / "testmondata").write_bytes(b"old")
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "testmondata").write_bytes(b"new")
    real_rename = map_github.os.rename

    def rename(src, dst):
        real_rename(src, dst)
        if str(dst) == str(mdir):  # the new folder is in; a late writer still holds the old folder
            return
        # the old folder has just been set aside: a map run writes its pending sha into it now
        (dst / "safety_pending").write_text(S1, encoding="utf-8")
        (dst / "pending_build.json").write_text("{}", encoding="utf-8")
        (dst / "build_started.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(map_github.os, "rename", rename)
    map_github._install(staged, mdir)
    assert (mdir / "testmondata").read_bytes() == b"new"
    assert (mdir / "safety_pending").read_text(encoding="utf-8") == S1
    assert (mdir / "pending_build.json").exists() and (mdir / "build_started.json").exists()
    assert not list(tmp_path.glob("test_map.old-*"))


def test_the_install_never_carries_the_old_testmon_sidecars_next_to_the_new_database(tmp_path):
    mdir = tmp_path / "test_map"
    mdir.mkdir()
    for name in ("testmondata", "testmondata-wal", "testmondata-shm", ".testmondata-wal", "safety_pending"):
        (mdir / name).write_text("old", encoding="utf-8")
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "testmondata").write_text("new", encoding="utf-8")
    map_github._install(staged, mdir)
    assert (mdir / "testmondata").read_text(encoding="utf-8") == "new"
    assert (mdir / "safety_pending").read_text(encoding="utf-8") == "old"
    assert not (mdir / "testmondata-wal").exists() and not (mdir / "testmondata-shm").exists()
    assert not (mdir / ".testmondata-wal").exists()


def test_the_install_keeps_a_state_file_the_new_map_also_holds_as_the_new_one(tmp_path):
    mdir = tmp_path / "test_map"
    mdir.mkdir()
    (mdir / "meta.json").write_text("old", encoding="utf-8")
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "meta.json").write_text("new", encoding="utf-8")
    map_github._install(staged, mdir)
    assert (mdir / "meta.json").read_text(encoding="utf-8") == "new"


def test_github_ref_logs_one_line_for_a_branch_delete_that_failed_and_keeps_the_result(tmp_path, monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if "-d" in argv or "--delete" in argv:
            raise RuntimeError("cannot delete\nsecond line")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(map_run.subprocess, "run", fake_run)
    lines = []
    with map_run.github_ref(tmp_path, "map-build/abc", S1, {"github_own_runners": True}, log=lines.append):
        pass
    assert len(lines) == 2  # the origin delete and the local delete, one line each
    assert all(line.startswith("map-build branch not deleted | map-build/abc | ") and "cannot delete" in line
               and "second line" not in line for line in lines)


# -- the subcommand -----------------------------------------------------------------------------------------------------

def test_the_map_run_subcommand_takes_after_seconds(monkeypatch):
    from tools.agentops import cli
    seen = []
    monkeypatch.setattr(map_run, "run_map_run_command", lambda args: seen.append(args) or 0)
    assert cli.main(["map-run", "--commit", S1, "--after-seconds", "90"]) == 0
    assert seen[0].after_seconds == 90.0 and seen[0].commit == S1
