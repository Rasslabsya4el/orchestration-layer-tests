"""#252: the test map is built only at a commit reachable from origin/main. A pending sha that is not (absent from the
repo, or a commit off main) is one ``map skipped | <sha12> | not on main`` line: nothing is built, the suite worktree is
not brought to it, and last_green_commit and the red marker stay as they were. A repo with no ``origin/main`` ref (a
test repo) is checked by the object alone. The superseded line also names its poster (ppid, cwd, ``--cwd``) so the
next stray sha can be traced (#252 diagnostic aid).

Seams: ``map_run.run_map_run`` (the drain: post, lock, pop, ``_safety_run``) on a tmp git repo; the build itself is
faked, no real suite runs here."""
from __future__ import annotations

import os
import re
from types import SimpleNamespace

import pytest

from tests.test_select_build_map import _git, _repo
from tools.agentops import map_run
from tools.agentops.test_select import map_dir, read_meta, write_meta

FAKE = "ab" * 20  # a made-up 40-hex sha: no object in any repo here
S1 = "1" * 40
S2 = "2" * 40
CMD = "python -m pytest tests -q"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A tmp git repo as the main checkout (its one commit is ``sha``), a tmp merge log, the machine not held, and a
    fake ``build_map`` that records each sha and writes a green map. ``ensure_suite_worktree`` records its calls."""
    main = tmp_path / "main"
    sha = _repo(main)
    e = SimpleNamespace(root=main, sha=sha, log=tmp_path / "merge.log", builds=[], worktrees=[])

    def fake_build(suite_dir, sha, mdir, suite_cmd, bound=None):
        e.builds.append(sha)
        meta = {"map_commit": sha, "last_green_commit": sha, "total": 4, "built_at": "t"}
        write_meta(mdir, meta)
        return meta

    def fake_worktree(root, commit, **kwargs):
        e.worktrees.append(commit)
        return root

    monkeypatch.setattr(map_run, "build_map", fake_build)
    monkeypatch.setattr(map_run, "ensure_suite_worktree", fake_worktree)
    monkeypatch.setattr(map_run, "machine_holds", lambda: False)
    return e


def _run(e, sha, cwd_arg=None):
    return map_run.run_map_run(sha, root=e.root, suite_cmd=CMD, shared=(), log_path=e.log, cwd_arg=cwd_arg)


def _texts(log):
    """The merge-log lines of the map run, the ``map-run | `` prefix cut off."""
    if not log.exists():
        return []
    out = []
    for line in log.read_text(encoding="utf-8").splitlines():
        m = re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \| map-run \| (.*)", line)
        assert m, line
        out.append(m.group(1))
    return out


def _set_origin_main(e, sha):
    _git(e.root, "update-ref", "refs/remotes/origin/main", sha)


def _no_suite_worktree(*args, **kwargs):
    raise AssertionError("ensure_suite_worktree must not run for a sha that is not on main")


def test_absent_sha_is_skipped_as_not_on_main_and_leaves_the_map_alone(env, monkeypatch):
    _set_origin_main(env, env.sha)
    mdir = map_dir(env.root)
    write_meta(mdir, {"map_commit": env.sha, "last_green_commit": env.sha, "total": 4, "built_at": "t"})
    marker = mdir / "safety_red.json"
    marker.write_text('{"sha": "kept"}', encoding="utf-8")
    monkeypatch.setattr(map_run, "ensure_suite_worktree", _no_suite_worktree)

    assert _run(env, FAKE) == 0

    assert _texts(env.log) == [f"map skipped | {FAKE[:12]} | not on main"]
    assert env.builds == []
    assert read_meta(mdir)["last_green_commit"] == env.sha
    assert marker.read_text(encoding="utf-8") == '{"sha": "kept"}'
    assert not (mdir / map_run.MAP_RUN_LOCK).exists()


def test_absent_sha_without_origin_main_is_skipped_by_the_object_check(env, monkeypatch):
    monkeypatch.setattr(map_run, "ensure_suite_worktree", _no_suite_worktree)

    assert _run(env, FAKE) == 0

    assert _texts(env.log) == [f"map skipped | {FAKE[:12]} | not on main"]
    assert env.builds == []


def test_commit_off_main_is_skipped(env, monkeypatch):
    _set_origin_main(env, env.sha)
    _git(env.root, "checkout", "-q", "-b", "side")
    (env.root / "side.txt").write_text("side\n", encoding="utf-8")
    _git(env.root, "add", "-A")
    _git(env.root, "commit", "-q", "-m", "side")
    side = _git(env.root, "rev-parse", "HEAD")
    monkeypatch.setattr(map_run, "ensure_suite_worktree", _no_suite_worktree)

    assert _run(env, side) == 0

    assert _texts(env.log) == [f"map skipped | {side[:12]} | not on main"]
    assert env.builds == []


def test_sha_reachable_from_origin_main_still_builds(env):
    _set_origin_main(env, env.sha)

    assert _run(env, env.sha) == 0

    assert env.worktrees == [env.sha]
    assert env.builds == [env.sha]
    assert _texts(env.log)[-1] == f"safety run: green | {env.sha[:12]}"


def test_existing_object_without_origin_main_still_builds(env):
    assert _run(env, env.sha) == 0

    assert env.worktrees == [env.sha]
    assert env.builds == [env.sha]
    assert f"safety run: green | {env.sha[:12]}" in _texts(env.log)


def test_superseded_line_names_the_poster_and_keeps_its_start(env):
    mdir = map_dir(env.root)
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / map_run.MAP_RUN_LOCK).write_text(str(os.getpid()), encoding="utf-8")  # a live run holds the map
    _run(env, S1, cwd_arg="C:/first")
    _run(env, S2, cwd_arg="C:/second")

    texts = _texts(env.log)
    assert len(texts) == 1, texts
    assert texts[0].startswith(f"safety run: superseded | {S1[:12]}"), texts
    assert texts[0] == (f"safety run: superseded | {S1[:12]} | poster ppid={os.getppid()} "
                        f"cwd={os.getcwd()} --cwd=C:/second"), texts
