"""#195 unit 4 (safety-revert): a red safety run leaves a marker; the merge line, holding the merge lock, resolves it
instead of waiting forever: the newest landing the old map says the failing test depends on is reverted and pushed, the
marker removed, one line logged. Every outcome is one log line and the marker goes; nothing raises. A throwaway git repo
with a local bare ``origin``; the selection against the old map is faked (the real one is test_select's)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import merge as merge_module
from tools.agentops import safety_revert
from tools.agentops import test_select
from tools.agentops.test_select import Selection

RED_ID = "tests/test_b.py::test_b"


def git(cwd, *args, check=True) -> str:
    env = {**os.environ, "AGENTOPS_MAIN_PUSH": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8", env=env)
    if check and r.returncode != 0:
        raise AssertionError(f"git {args}: {r.stderr}")
    return r.stdout.strip()


def commit_file(repo: Path, name: str, text: str, msg: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-m", msg)


def land(repo: Path, branch: str, name: str, text: str) -> str:
    """A landing: a merge commit on main from a side branch that changed ``name``; returns its sha."""
    git(repo, "checkout", "-q", "-b", branch)
    commit_file(repo, name, text, f"work {branch}")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-m", f"Merge branch '{branch}'", branch)
    return git(repo, "rev-parse", "HEAD")


def sel(ids: list[str], full: bool = False) -> Selection:
    return Selection(full, "", ids, 0, 4, "m", "")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in (("NAME", "t"), ("EMAIL", "t@t")):  # the identity of the revert commit
        monkeypatch.setenv(f"GIT_AUTHOR_{k}", v)
        monkeypatch.setenv(f"GIT_COMMITTER_{k}", v)
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    repo = tmp_path / "root"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "remote", "add", "origin", str(origin))
    commit_file(repo, "a.txt", "a0\n", "base a")
    commit_file(repo, "b.txt", "b0\n", "base b")
    base = git(repo, "rev-parse", "HEAD")
    a = land(repo, "side-a", "a.txt", "a1\n")
    b = land(repo, "side-b", "b.txt", "b1\n")
    git(repo, "push", "-q", "origin", "main")
    mdir = test_select.map_dir(repo)
    mdir.mkdir(parents=True, exist_ok=True)
    e = SimpleNamespace(repo=repo, origin=origin, mdir=mdir, log=tmp_path / "merge.log", base=base, a=a, b=b,
                        spawned=[], by_head={a: sel(["tests/test_a.py::test_a"]), b: sel([RED_ID])})
    monkeypatch.setattr(test_select, "select_for_diff",
                        lambda root, base, head, mdir: e.by_head.get(head, sel([])))
    monkeypatch.setattr(safety_revert, "_spawn", lambda root, sha: e.spawned.append(sha) or "")
    return e


def marker(e, sha=None, ids=None) -> Path:
    path = e.mdir / "safety_red.json"
    path.write_text(json.dumps({"sha": sha or git(e.repo, "rev-parse", "HEAD"), "ids": ids or [RED_ID],
                                "map_commit": e.base}), encoding="utf-8")
    return path


def lines(log: Path) -> list[str]:
    out = []
    for line in log.read_text(encoding="utf-8").splitlines():
        m = re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \| map-run \| (.*)", line)
        assert m, line
        out.append(m.group(1))
    return out


def count(repo: Path) -> int:
    return int(git(repo, "rev-list", "--count", "HEAD"))


# (a) a red marker: the newest landing the failing test depends on is reverted and pushed

def test_a_the_guilty_landing_is_reverted_pushed_and_the_marker_removed(env):
    m = marker(env)
    before = count(env.repo)
    safety_revert.resolve_safety_red(env.repo, env.mdir, env.log)
    assert (env.repo / "b.txt").read_text(encoding="utf-8") == "b0\n"  # B's change is gone
    assert (env.repo / "a.txt").read_text(encoding="utf-8") == "a1\n"  # A stays
    assert count(env.repo) == before + 1  # one revert commit
    assert git(env.repo, "rev-parse", "HEAD") == git(env.origin, "rev-parse", "main")  # pushed
    assert not m.exists()
    assert lines(env.log) == [f"safety run: red {RED_ID} | reverted {env.b[:12]}"]
    assert env.spawned == [git(env.repo, "rev-parse", "HEAD")]  # the revert is a landing: the next safety run


def test_a_b_a_full_selection_is_guilty_only_when_no_other_landing_is(env):
    env.by_head[env.b] = sel([], full=True)
    marker(env)
    safety_revert.resolve_safety_red(env.repo, env.mdir, env.log)
    assert (env.repo / "b.txt").read_text(encoding="utf-8") == "b0\n"  # no non-full guilty one: the newest full is
    assert lines(env.log) == [f"safety run: red {RED_ID} | reverted {env.b[:12]}"]


def test_a_c_a_non_full_guilty_landing_beats_a_newer_full_one(env):
    env.by_head[env.a] = sel([RED_ID])
    env.by_head[env.b] = sel([], full=True)
    marker(env)
    safety_revert.resolve_safety_red(env.repo, env.mdir, env.log)
    assert (env.repo / "a.txt").read_text(encoding="utf-8") == "a0\n"  # A reverted
    assert (env.repo / "b.txt").read_text(encoding="utf-8") == "b1\n"
    assert lines(env.log) == [f"safety run: red {RED_ID} | reverted {env.a[:12]}"]


def test_a_d_a_file_the_selection_only_pads_does_not_make_a_landing_guilty(env):
    """The failing file is changed by A alone; B's selection lists it only as a padded 'young' whole file."""
    env.by_head[env.a] = sel([])
    env.by_head[env.b] = sel(["a.txt"])
    ids = ["a.txt::test_a"]
    marker(env, ids=ids)
    safety_revert.resolve_safety_red(env.repo, env.mdir, env.log)
    assert (env.repo / "a.txt").read_text(encoding="utf-8") == "a0\n"  # A (it changed the file) reverted, not B
    assert (env.repo / "b.txt").read_text(encoding="utf-8") == "b1\n"
    assert lines(env.log) == [f"safety run: red a.txt::test_a | reverted {env.a[:12]}"]


# (b) no guilty landing: the marker goes, no commit

def test_b_no_guilty_landing_clears_the_marker_without_a_commit(env):
    m = marker(env, ids=["tests/test_z.py::test_z"])
    before, head = count(env.repo), git(env.repo, "rev-parse", "HEAD")
    safety_revert.resolve_safety_red(env.repo, env.mdir, env.log)
    assert not m.exists()
    assert count(env.repo) == before and git(env.repo, "rev-parse", "HEAD") == head
    [text] = lines(env.log)
    assert text.startswith(f"safety run: red tests/test_z.py::test_z | not reverted: ")
    assert env.spawned == []


def test_b_b_a_marker_without_ids_reverts_nothing(env):
    m = marker(env)
    m.write_text(json.dumps({"sha": git(env.repo, "rev-parse", "HEAD"), "ids": [], "map_commit": env.base}),
                 encoding="utf-8")
    head = git(env.repo, "rev-parse", "HEAD")
    safety_revert.resolve_safety_red(env.repo, env.mdir, env.log)
    assert not m.exists() and git(env.repo, "rev-parse", "HEAD") == head
    assert lines(env.log) == ["safety run: red (no ids) | not reverted: no failing id"]


# (c) a revert that conflicts is aborted; the tree is clean

def test_c_a_conflicting_revert_is_aborted_and_the_marker_removed(env):
    commit_file(env.repo, "b.txt", "b2\n", "later change of b")  # a first-parent commit after B: revert B conflicts
    git(env.repo, "push", "-q", "origin", "main")
    m = marker(env)
    before = count(env.repo)
    safety_revert.resolve_safety_red(env.repo, env.mdir, env.log)
    assert git(env.repo, "status", "--porcelain") == ""
    assert not (env.repo / ".git" / "REVERT_HEAD").exists()
    assert count(env.repo) == before
    assert (env.repo / "b.txt").read_text(encoding="utf-8") == "b2\n"
    assert not m.exists()
    [text] = lines(env.log)
    assert text.startswith(f"safety run: red {RED_ID} | not reverted: ") and "conflict" in text


def test_c_b_a_refused_push_leaves_main_as_it_was_and_clears_the_marker(env):
    hook = env.origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    m = marker(env)
    before = git(env.repo, "rev-parse", "HEAD")
    safety_revert.resolve_safety_red(env.repo, env.mdir, env.log)
    assert git(env.repo, "rev-parse", "HEAD") == before  # the local revert is taken back with the refused push
    assert (env.repo / "b.txt").read_text(encoding="utf-8") == "b1\n"
    assert not m.exists()
    [text] = lines(env.log)
    assert text.startswith(f"safety run: red {RED_ID} | not reverted: ") and "push" in text


def test_c_c_a_git_call_over_the_timeout_is_a_not_reverted_timeout(env, monkeypatch):
    real = subprocess.run

    def slow_push(cmd, *a, **k):
        if "push" in cmd:
            raise subprocess.TimeoutExpired(cmd, k.get("timeout"))
        return real(cmd, *a, **k)

    monkeypatch.setattr(safety_revert.subprocess, "run", slow_push)
    m = marker(env)
    before = git(env.repo, "rev-parse", "HEAD")
    safety_revert.resolve_safety_red(env.repo, env.mdir, env.log)
    assert git(env.repo, "rev-parse", "HEAD") == before  # the unpublished revert is taken back
    assert (env.repo / "b.txt").read_text(encoding="utf-8") == "b1\n"
    assert not m.exists()
    assert lines(env.log) == [f"safety run: red {RED_ID} | not reverted: timeout"]


# (d) the merge line's hold resolves the marker instead of waiting

def test_d_a_the_hold_with_a_marker_resolves_it_and_returns(env):
    m = marker(env)
    done = threading.Event()

    def hold():
        merge_module.hold_while_safety_red(env.repo, log_path=env.log)
        done.set()

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert done.wait(30.0)
    t.join(5.0)
    assert not m.exists()
    assert lines(env.log) == [f"safety run: queue held by red {env.b[:12]}",
                              f"safety run: red {RED_ID} | reverted {env.b[:12]}"]


def test_d_b_the_hold_without_a_marker_returns_at_once(env):
    merge_module.hold_while_safety_red(env.repo, log_path=env.log)
    assert not env.log.exists()
