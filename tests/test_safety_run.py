"""#195 unit 3 (safety-queue): the full run after a landing is the safety run. One live run at a time; a landing
that arrives while one runs leaves one pending sha (newest wins, the older logged superseded); a green run logs
``safety run: green``, a red one ``safety run: red <ids>`` and writes the red marker, which holds the merge line
until it is removed. The build itself is faked: no real suite runs here."""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import map_run
from tools.agentops import merge as merge_module
from tools.agentops.test_select import MapBuildError, map_dir, read_meta, write_meta

S0 = "0" * 40
S1 = "1" * 40
S2 = "2" * 40
S3 = "3" * 40
CMD = "python -m pytest tests -q"
RED_TAIL = (
    "suite exited 1; map not written\n"
    "tests/test_b.py F\n"
    "FAILED tests/test_b.py::test_b - assert 3 == 2\n"
    "FAILED tests/test_c.py::test_c[1] - assert 3 == 4\n"
    "=========================== short test summary info ===========================\n"
    "FAILED tests/test_b.py::test_b - assert 3 == 2\n"
    "FAILED tests/test_c.py::test_c[1] - assert 3 == 4\n"
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A tmp main checkout (its map dir), a tmp merge log, the machine not held, the suite worktree the root
    itself, and a fake ``build_map``: it records each sha, runs an optional hook, and raises or writes a green map."""
    root = tmp_path / "root"
    root.mkdir()
    e = SimpleNamespace(root=root, mdir=map_dir(root), log=tmp_path / "merge.log", builds=[], hooks={}, fail={})

    def fake_build(suite_dir, sha, mdir, suite_cmd, bound=None):
        e.builds.append(sha)
        if sha in e.hooks:
            e.hooks[sha]()
        if sha in e.fail:
            raise e.fail[sha]
        meta = {"map_commit": sha, "last_green_commit": sha, "total": 4, "built_at": "t"}
        write_meta(mdir, meta)
        return meta

    monkeypatch.setattr(map_run, "build_map", fake_build)
    monkeypatch.setattr(map_run, "ensure_suite_worktree", lambda *a, **k: root)
    monkeypatch.setattr(map_run, "machine_holds", lambda: False)
    monkeypatch.setattr(map_run, "sha_on_main", lambda root, sha: True)  # #252: the fake shas are no repo's commits
    return e


def _run(e, sha):
    return map_run.run_map_run(sha, root=e.root, suite_cmd=CMD, shared=(), log_path=e.log)


def _live_lock(mdir: Path) -> None:
    """The map's lock held by this process: a live run."""
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / map_run.MAP_RUN_LOCK).write_text(str(os.getpid()), encoding="utf-8")


def _texts(log: Path) -> list[str]:
    """The merge-log lines of the map run, the ``map-run | `` prefix cut off."""
    if not log.exists():
        return []
    out = []
    for line in log.read_text(encoding="utf-8").splitlines():
        m = re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \| map-run \| (.*)", line)
        assert m, line
        if not m.group(1).startswith("map-build on "):  # #343: the place line is asserted in test_issue343_map_run_place
            out.append(m.group(1))
    return out


def _write_marker(mdir: Path, sha: str = S0) -> None:
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "safety_red.json").write_text(json.dumps({"sha": sha, "ids": ["x"], "map_commit": None}),
                                          encoding="utf-8")


# (a) one pending sha while a run is live; the newer one wins, the older is logged superseded

def test_a_a_landing_while_a_run_is_live_leaves_one_pending_sha(env):
    _live_lock(env.mdir)
    assert _run(env, S1) == 0
    assert (env.mdir / "safety_pending").read_text(encoding="utf-8") == S1
    assert env.builds == []
    assert _texts(env.log) == []


def test_a_b_two_landings_in_a_row_leave_the_newer_and_log_the_older_superseded(env):
    _live_lock(env.mdir)
    _run(env, S1)
    _run(env, S2)
    assert (env.mdir / "safety_pending").read_text(encoding="utf-8") == S2
    assert env.builds == []
    texts = _texts(env.log)  # #252: the line also names its poster; its start is unchanged
    assert len(texts) == 1 and texts[0].startswith(f"safety run: superseded | {S1[:12]} | poster "), texts


def test_a_c_the_live_run_picks_up_the_pending_sha_and_runs_it(env):
    # a landing that arrives while the first run builds: it is pending, and the loop runs it after the first
    env.hooks[S1] = lambda: _run(env, S2)
    assert _run(env, S1) == 0
    assert env.builds == [S1, S2]
    assert not (env.mdir / "safety_pending").exists()
    assert not (env.mdir / map_run.MAP_RUN_LOCK).exists()
    texts = _texts(env.log)
    assert [t for t in texts if t.startswith("safety run:")] == [
        f"safety run: green | {S1[:12]}", f"safety run: green | {S2[:12]}"]


# (b) a green run logs green and removes the red marker

def test_b_a_green_run_logs_green_and_removes_the_marker(env):
    _write_marker(env.mdir)
    assert _run(env, S1) == 0
    assert not (env.mdir / "safety_red.json").exists()
    texts = _texts(env.log)
    assert len(texts) == 2, texts
    assert re.fullmatch(rf"map built \| .*testmondata \| {S1[:12]} \| 4 tests", texts[0]), texts
    assert texts[1] == f"safety run: green | {S1[:12]}"


# (c) a red run logs red with the failing ids and writes the marker

def test_c_a_red_run_logs_the_failing_ids_and_writes_the_marker(env):
    write_meta(env.mdir, {"map_commit": S0, "last_green_commit": S0, "total": 4, "built_at": "t"})
    env.fail[S1] = MapBuildError(RED_TAIL)
    assert _run(env, S1) != 0
    assert _texts(env.log) == [
        f"safety run: red tests/test_b.py::test_b,tests/test_c.py::test_c[1] | {S1[:12]}"]
    marker = json.loads((env.mdir / "safety_red.json").read_text(encoding="utf-8"))
    assert marker == {"sha": S1, "ids": ["tests/test_b.py::test_b", "tests/test_c.py::test_c[1]"],
                      "map_commit": S0}
    assert read_meta(env.mdir)["last_green_commit"] == S0  # the old map stays


def test_c_b_a_red_run_with_no_parseable_ids_says_no_ids(env):
    env.fail[S1] = MapBuildError("suite exited 2; map not written\nboom")
    _run(env, S1)
    assert _texts(env.log) == [f"safety run: red (no ids) | {S1[:12]}"]
    marker = json.loads((env.mdir / "safety_red.json").read_text(encoding="utf-8"))
    assert marker["ids"] == [] and marker["map_commit"] is None


def test_c_c_more_than_five_failing_ids_show_five_and_the_rest_counted(env):
    ids = [f"tests/t.py::test_{i}" for i in range(7)]
    env.fail[S1] = MapBuildError("suite exited 1; map not written\n" + "\n".join(f"FAILED {i} - x" for i in ids))
    _run(env, S1)
    assert _texts(env.log) == [
        f"safety run: red {','.join(ids[:5])} and 2 more | {S1[:12]}"]


def test_c_d_a_build_that_is_not_a_red_suite_logs_map_failed_and_no_marker(env):
    env.fail[S1] = MapBuildError("suite was green but testmon wrote no datafile (is pytest-testmon installed?)")
    assert _run(env, S1) != 0
    assert not (env.mdir / "safety_red.json").exists()
    texts = _texts(env.log)
    assert len(texts) == 1 and texts[0].startswith(f"map failed | {S1[:12]} | suite was green"), texts


# (d) the red marker holds the merge line only until the hold resolves it (#195 unit 4)

def test_d_a_a_marker_present_is_resolved_by_the_hold_which_then_returns(env):
    """Unit 4 replaced blocking-until-removed: the caller holds the lock, so the hold resolves the marker (here a
    root that is no git repo: ``not reverted``) and returns; the marker is gone and the log has the two lines."""
    _write_marker(env.mdir, sha=S1)
    done = threading.Event()

    def hold():
        merge_module.hold_while_safety_red(env.root, log_path=env.log)
        done.set()

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert done.wait(10.0)
    t.join(5.0)
    assert not (env.mdir / "safety_red.json").exists()
    texts = _texts(env.log)
    assert texts[0] == f"safety run: queue held by red {S1[:12]}"
    assert len(texts) == 2 and texts[1].startswith("safety run: red x | not reverted: "), texts


def test_d_b_without_a_marker_the_hold_returns_at_once(env):
    done = threading.Event()

    def hold():
        merge_module.hold_while_safety_red(env.root, log_path=env.log)
        done.set()

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert done.wait(2.0)
    t.join(2.0)
    assert _texts(env.log) == []
