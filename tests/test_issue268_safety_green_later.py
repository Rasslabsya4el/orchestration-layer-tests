"""#268 unit 1 (guard): the safety run's red marker names the failing ids of an older head. Before blaming a landing,
``_resolve`` runs exactly those ids at the current head and drops the ones that pass there; none left reverts nothing
(``already green at <head sha12>: not reverted``). An unreadable or timed-out run keeps every id (as before). Throwaway
git repo with a local bare ``origin``, as tests/test_safety_revert.py; the selection and the ids' run are faked."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import safety_revert
from tools.agentops import test_select
from tools.agentops.test_select import Selection

RED_ID = "tests/test_b.py::test_b"
OTHER_ID = "tests/test_a.py::test_a"


def git(cwd, *args) -> str:
    env = {**os.environ, "AGENTOPS_MAIN_PUSH": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8", env=env)
    if r.returncode != 0:
        raise AssertionError(f"git {args}: {r.stderr}")
    return r.stdout.strip()


def commit_file(repo: Path, name: str, text: str, msg: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-m", msg)


def land(repo: Path, branch: str, name: str, text: str) -> str:
    git(repo, "checkout", "-q", "-b", branch)
    commit_file(repo, name, text, f"work {branch}")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-m", f"Merge branch '{branch}'", branch)
    return git(repo, "rev-parse", "HEAD")


def sel(ids: list[str]) -> Selection:
    return Selection(False, "", ids, 0, 4, "m", "")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in (("NAME", "t"), ("EMAIL", "t@t")):
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
    e = SimpleNamespace(repo=repo, mdir=mdir, log=tmp_path / "merge.log", base=base, a=a, b=b, spawned=[], asked=[],
                        by_head={a: sel([OTHER_ID]), b: sel([RED_ID])})
    monkeypatch.setattr(test_select, "select_for_diff", lambda root, base, head, mdir: e.by_head.get(head, sel([])))
    monkeypatch.setattr(safety_revert, "_spawn", lambda root, sha: e.spawned.append(sha) or "")
    return e


def marker(e, ids) -> Path:
    path = e.mdir / "safety_red.json"
    path.write_text(json.dumps({"sha": git(e.repo, "rev-parse", "HEAD"), "ids": ids, "map_commit": e.base}),
                    encoding="utf-8")
    return path


def result_line(log: Path) -> str:
    [line] = log.read_text(encoding="utf-8").splitlines()
    m = re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \| map-run \| (.*)", line)
    assert m, line
    return m.group(1)


def runner_of(e, failing):
    def run(root, ids):
        e.asked.append(list(ids))
        return failing(root, ids)
    return run


def test_a_ids_that_pass_at_the_head_revert_nothing(env):
    ids = [RED_ID, OTHER_ID]
    m = marker(env, ids)
    head = git(env.repo, "rev-parse", "HEAD")
    count = git(env.repo, "rev-list", "--count", "HEAD")
    line = _resolve_with(env, lambda root, got: [])
    assert git(env.repo, "rev-parse", "HEAD") == head and git(env.repo, "rev-list", "--count", "HEAD") == count
    assert (env.repo / "b.txt").read_text(encoding="utf-8") == "b1\n"
    assert env.spawned == []
    assert not m.exists()
    assert result_line(env.log) == f"safety run: red {RED_ID},{OTHER_ID} | already green at {head[:12]}: not reverted"
    assert "already green at" in line
    assert env.asked == [ids]  # one run, exactly the marker's ids


def test_b_an_id_still_red_at_the_head_goes_to_the_revert_path(env):
    ids = [RED_ID, OTHER_ID]
    marker(env, ids)
    _resolve_with(env, lambda root, got: [RED_ID])
    assert (env.repo / "b.txt").read_text(encoding="utf-8") == "b0\n"  # B (the guilty for RED_ID) reverted
    assert (env.repo / "a.txt").read_text(encoding="utf-8") == "a1\n"
    assert result_line(env.log) == f"safety run: red {RED_ID},{OTHER_ID} | reverted {env.b[:12]}"


def test_b_b_only_the_still_red_ids_pick_the_guilty_landing(env):
    marker(env, [RED_ID, OTHER_ID])  # RED_ID (B's) passes at the head; OTHER_ID (A's) is still red
    _resolve_with(env, lambda root, got: [OTHER_ID])
    assert (env.repo / "a.txt").read_text(encoding="utf-8") == "a0\n"  # A reverted, not the newer B
    assert (env.repo / "b.txt").read_text(encoding="utf-8") == "b1\n"
    assert result_line(env.log) == f"safety run: red {RED_ID},{OTHER_ID} | reverted {env.a[:12]}"


def test_c_a_run_that_raises_keeps_every_id_as_red(env):
    marker(env, [RED_ID])

    def boom(root, ids):
        raise OSError("no pytest")

    _resolve_with(env, boom)
    assert (env.repo / "b.txt").read_text(encoding="utf-8") == "b0\n"  # as before the guard: B reverted
    assert result_line(env.log) == f"safety run: red {RED_ID} | reverted {env.b[:12]}"


def test_c_b_a_timed_out_run_keeps_every_id_as_red(env):
    marker(env, [RED_ID])

    def slow(root, ids):
        raise subprocess.TimeoutExpired("pytest", 1)

    _resolve_with(env, slow)
    assert (env.repo / "b.txt").read_text(encoding="utf-8") == "b0\n"
    assert result_line(env.log) == f"safety run: red {RED_ID} | reverted {env.b[:12]}"


def test_c_c_the_real_run_of_an_unreadable_id_keeps_it_red(env):
    """No such test file at the head: pytest exits 4 (usage), which says nothing about the id passing."""
    assert safety_revert._run_ids(env.repo, [RED_ID]) == [RED_ID]


def _resolve_with(env, runner) -> str:
    """``resolve_safety_red`` with ``_still_red`` bound to the fake id runner."""
    original = safety_revert._still_red
    safety_revert._still_red = lambda root, ids, runner=runner_of(env, runner): original(root, ids, runner)
    try:
        return safety_revert.resolve_safety_red(env.repo, env.mdir, env.log)
    finally:
        safety_revert._still_red = original


def test_d_a_fixture_error_is_red_a_pass_is_green(tmp_path):
    (tmp_path / "test_x.py").write_text(
        "import pytest\n@pytest.fixture\ndef boom():\n    raise RuntimeError('x')\n"
        "def test_errors(boom):\n    pass\ndef test_fails():\n    assert 0\ndef test_ok():\n    pass\n", encoding="utf-8")
    ids = ["test_x.py::test_errors", "test_x.py::test_fails", "test_x.py::test_ok"]
    assert sorted(safety_revert._run_ids(tmp_path, ids)) == sorted(ids[:2])
