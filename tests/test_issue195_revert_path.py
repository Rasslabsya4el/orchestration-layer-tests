"""#195 unit 3 (revert-path): the safety revert never picks a State commit as a landing, and a red marker whose landing
main already reverted reverts nothing (one log line, the marker cleared, the merge hold not held). Real temp git
repos with a local bare ``origin``, as tests/test_safety_revert.py; the selection against the old map is faked."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import merge as merge_module
from tools.agentops import safety_revert
from tools.agentops import test_select
from tools.agentops.test_select import Selection

RED_ID = "tests/test_b.py::test_b"


def git(cwd, *args) -> str:
    env = {**os.environ, "AGENTOPS_MAIN_PUSH": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8", env=env,
                       timeout=60)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout.strip()


def commit_file(repo: Path, name: str, text: str, msg: str) -> str:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def land(repo: Path, branch: str, name: str, text: str) -> str:
    git(repo, "checkout", "-q", "-b", branch)
    commit_file(repo, name, text, f"work {branch}")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-m", f"Merge branch '{branch}'", branch)
    return git(repo, "rev-parse", "HEAD")


def sel(ids: list[str], full: bool = False) -> Selection:
    return Selection(full, "", ids, 0, 4, "m", "")


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
    e = SimpleNamespace(repo=repo, origin=origin, mdir=mdir, log=tmp_path / "merge.log", base=base, a=a, b=b,
                        spawned=[], by_head={a: sel(["tests/test_a.py::test_a"]), b: sel([RED_ID])})
    monkeypatch.setattr(test_select, "select_for_diff", lambda root, base, head, mdir: e.by_head.get(head, sel([])))
    monkeypatch.setattr(safety_revert, "_spawn", lambda root, sha: e.spawned.append(sha) or "")
    return e


def marker(e, sha: str, ids=None) -> Path:
    path = e.mdir / "safety_red.json"
    path.write_text(json.dumps({"sha": sha, "ids": ids or [RED_ID], "map_commit": e.base}), encoding="utf-8")
    return path


def log_lines(log: Path) -> list[str]:
    if not log.exists():
        return []
    out = []
    for line in log.read_text(encoding="utf-8").splitlines():
        m = re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \| map-run \| (.*)", line)
        assert m, line
        out.append(m.group(1))
    return out


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def revert_on_main(e, landing: str) -> str:
    """What the queue's own revert is: ``git revert -m 1`` of the landing, pushed."""
    git(e.repo, "revert", "--no-edit", "-m", "1", landing)
    git(e.repo, "push", "-q", "origin", "main")
    return head(e.repo)


# (a) a State commit is not a candidate landing

def test_a_a_state_commit_is_never_picked_as_the_guilty_landing(env):
    state = commit_file(env.repo, "docs/STATE.md", "state\n", "State: brain hand-off 20:58")
    git(env.repo, "push", "-q", "origin", "main")
    env.by_head[state] = sel([], full=True)  # a full selection would be the "ambiguous" pick of the newest landing
    m = marker(env, state, ids=["tests/test_z.py::test_z"])  # selected by no landing's mapped ids
    before = head(env.repo)
    safety_revert.resolve_safety_red(env.repo, env.mdir, env.log)
    assert head(env.repo) == before and not m.exists()  # the State commit was not reverted
    assert (env.repo / "docs" / "STATE.md").read_text(encoding="utf-8") == "state\n"
    [text] = log_lines(env.log)
    assert text.startswith("safety run: red tests/test_z.py::test_z | not reverted: ")


def test_a_b_a_guilty_landing_below_a_state_commit_is_still_reverted(env):
    state = commit_file(env.repo, "docs/STATE.md", "state\n", "State: brain hand-off 20:58")
    git(env.repo, "push", "-q", "origin", "main")
    marker(env, state)
    safety_revert.resolve_safety_red(env.repo, env.mdir, env.log)
    assert (env.repo / "b.txt").read_text(encoding="utf-8") == "b0\n"
    assert (env.repo / "docs" / "STATE.md").read_text(encoding="utf-8") == "state\n"
    assert log_lines(env.log) == [f"safety run: red {RED_ID} | reverted {env.b[:12]}"]


def test_a_c_a_commit_changing_state_and_another_file_is_still_a_landing(env):
    (env.repo / "docs").mkdir()
    (env.repo / "docs" / "STATE.md").write_text("s\n", encoding="utf-8")
    (env.repo / "c.txt").write_text("c\n", encoding="utf-8")
    git(env.repo, "add", "-A")
    git(env.repo, "commit", "-m", "State and code")
    both = head(env.repo)
    git(env.repo, "push", "-q", "origin", "main")
    env.by_head[both] = sel([RED_ID])
    marker(env, both)
    safety_revert.resolve_safety_red(env.repo, env.mdir, env.log)
    assert not (env.repo / "c.txt").exists()
    assert log_lines(env.log) == [f"safety run: red {RED_ID} | reverted {both[:12]}"]


# (b) a marker whose landing main already reverted reverts nothing

def test_b_a_a_marker_for_an_already_reverted_landing_reverts_no_other_landing(env):
    m = marker(env, env.b)
    after = revert_on_main(env, env.b)
    env.by_head[env.a] = sel([RED_ID])  # another landing would be guilty by the selection, were one picked
    safety_revert.resolve_safety_red(env.repo, env.mdir, env.log)
    assert head(env.repo) == after  # no second revert commit
    assert (env.repo / "a.txt").read_text(encoding="utf-8") == "a1\n"
    assert not m.exists()
    assert log_lines(env.log) == [f"safety run: red {RED_ID} | already reverted: {env.b[:12]}"]
    assert env.spawned == []


def test_b_b_a_state_commit_after_the_revert_does_not_hide_it(env):
    m = marker(env, env.b)
    revert_on_main(env, env.b)
    state = commit_file(env.repo, "docs/STATE.md", "state\n", "State: brain hand-off 21:10")
    git(env.repo, "push", "-q", "origin", "main")
    safety_revert.resolve_safety_red(env.repo, env.mdir, env.log)
    assert head(env.repo) == state and not m.exists()
    assert log_lines(env.log) == [f"safety run: red {RED_ID} | already reverted: {env.b[:12]}"]


def test_b_c_a_marker_for_a_later_sha_whose_guilty_landing_was_reverted_reverts_nothing(env):
    """The 3b5747f case: the build was of a later commit that carries the reverted landing."""
    later = land(env.repo, "side-c", "c.txt", "c1\n")
    git(env.repo, "push", "-q", "origin", "main")
    m = marker(env, later)
    after = revert_on_main(env, env.b)
    safety_revert.resolve_safety_red(env.repo, env.mdir, env.log)
    assert head(env.repo) == after and not m.exists()
    assert (env.repo / "c.txt").read_text(encoding="utf-8") == "c1\n"
    assert log_lines(env.log) == [f"safety run: red {RED_ID} | already reverted: {env.b[:12]}"]


def test_b_d_a_landing_whose_revert_was_itself_reverted_is_not_reverted_already(env):
    revert = revert_on_main(env, env.b)
    git(env.repo, "revert", "--no-edit", revert)  # the landing is back on main
    git(env.repo, "push", "-q", "origin", "main")
    marker(env, env.b)
    safety_revert.resolve_safety_red(env.repo, env.mdir, env.log)
    assert (env.repo / "b.txt").read_text(encoding="utf-8") == "b0\n"  # reverted again, the normal way
    assert log_lines(env.log) == [f"safety run: red {RED_ID} | reverted {env.b[:12]}"]


def test_b_e_the_hold_does_not_hold_on_a_marker_of_a_reverted_landing(env):
    m = marker(env, env.b)
    after = revert_on_main(env, env.b)
    merge_module.hold_while_safety_red(env.repo, log_path=env.log)
    assert not m.exists() and head(env.repo) == after
    assert log_lines(env.log) == [f"safety run: red {RED_ID} | already reverted: {env.b[:12]}"]  # no "queue held"


def test_b_f_the_hold_still_holds_and_resolves_a_marker_of_a_live_landing(env):
    m = marker(env, env.b)
    merge_module.hold_while_safety_red(env.repo, log_path=env.log)
    assert not m.exists()
    assert log_lines(env.log) == [f"safety run: queue held by red {env.b[:12]}",
                                  f"safety run: red {RED_ID} | reverted {env.b[:12]}"]


def test_b_g_an_innocent_landing_newer_than_the_reverted_guilty_one_is_not_reverted_in_its_place(env):
    later = land(env.repo, "side-c", "c.txt", "c1\n")
    env.by_head[later] = sel(["tests/test_c.py::test_c"])  # the selection says: not this one
    git(env.repo, "push", "-q", "origin", "main")
    m = marker(env, later)
    after = revert_on_main(env, env.b)
    safety_revert.resolve_safety_red(env.repo, env.mdir, env.log)
    assert head(env.repo) == after and not m.exists()
    assert (env.repo / "c.txt").read_text(encoding="utf-8") == "c1\n"
    assert log_lines(env.log) == [f"safety run: red {RED_ID} | already reverted: {env.b[:12]}"]


def test_b_h_the_revert_commit_in_the_range_is_not_a_candidate_landing(env):
    """The revert of B changes b.txt, so the old map selects the failing id for it; it must not be reverted back."""
    revert = revert_on_main(env, env.b)
    later = land(env.repo, "side-c", "c.txt", "c1\n")
    git(env.repo, "push", "-q", "origin", "main")
    env.by_head[revert] = sel([RED_ID])
    m = marker(env, later)
    after = head(env.repo)
    safety_revert.resolve_safety_red(env.repo, env.mdir, env.log)
    assert head(env.repo) == after and not m.exists()
    assert (env.repo / "b.txt").read_text(encoding="utf-8") == "b0\n"  # B stays out
    assert log_lines(env.log) == [f"safety run: red {RED_ID} | already reverted: {env.b[:12]}"]


def test_b_i_a_reapplied_landing_is_still_a_candidate(env):
    """A revert of a revert re-lands the code: it is a landing the failing id can point at."""
    revert = revert_on_main(env, env.b)
    git(env.repo, "revert", "--no-edit", revert)
    git(env.repo, "push", "-q", "origin", "main")
    reapply = head(env.repo)
    env.by_head[reapply] = sel([RED_ID])
    marker(env, reapply)
    safety_revert.resolve_safety_red(env.repo, env.mdir, env.log)
    assert (env.repo / "b.txt").read_text(encoding="utf-8") == "b0\n"
    assert log_lines(env.log) == [f"safety run: red {RED_ID} | reverted {reapply[:12]}"]


# an unreadable history is a "not reverted" line, never a silent fall into the revert path

def test_b_j_an_unknown_marker_sha_is_a_not_reverted_line_and_the_marker_goes(env):
    m = marker(env, "0" * 40)
    before = head(env.repo)
    safety_revert.resolve_safety_red(env.repo, env.mdir, env.log)
    assert head(env.repo) == before and not m.exists()
    [text] = log_lines(env.log)
    assert text.startswith(f"safety run: red {RED_ID} | not reverted: ")


def test_b_k_the_hold_with_an_unknown_marker_sha_logs_the_reason_and_clears_it(env):
    m = marker(env, "0" * 40)
    merge_module.hold_while_safety_red(env.repo, log_path=env.log)
    assert not m.exists()
    first, second = log_lines(env.log)
    assert first == f"safety run: queue held by red {'0' * 12}"
    assert second.startswith(f"safety run: red {RED_ID} | not reverted: ")


def test_b_l_already_reverted_raises_on_an_unreadable_history(env):
    with pytest.raises(RuntimeError):
        safety_revert.already_reverted(env.repo, "0" * 40)


# (c) no landing selected by the failing ids: as before

def test_c_no_guilty_landing_is_the_old_not_reverted_line(env):
    m = marker(env, head(env.repo), ids=["tests/test_z.py::test_z"])
    before = head(env.repo)
    safety_revert.resolve_safety_red(env.repo, env.mdir, env.log)
    assert head(env.repo) == before and not m.exists()
    [text] = log_lines(env.log)
    assert text.startswith("safety run: red tests/test_z.py::test_z | not reverted: ")
