"""Issue #357 unit 3: the launcher's gh-shim preflight.

``Config`` makes every path absolute, so a relative ``runs_dir`` gives an absolute ``state_dir`` and the run's PATH
entry for the shim resolves from any cwd. ``check_gh_shim`` runs ``shutil.which('gh')`` in the run's own environment and
cwd before the model starts and refuses unless the answer is a file inside the run's state dir. ``run_one`` calls it
before ``claude -p`` starts; a failed check writes no record.
"""

import os
import textwrap
from pathlib import Path

import pytest

from tools.replays import launcher
from tools.replays.launcher import Config, run_one

CASE = {"moment_utc": "2026-10-01T00:00:00Z"}

#: writes a marker when it starts; the test checks the marker is absent (the model never started).
MARK_CLAUDE = textwrap.dedent(
    r'''
    import os
    from pathlib import Path
    Path(os.environ["FAKE_MARK"]).write_text("started", encoding="utf-8")
    '''
)

windows_only = pytest.mark.skipif(os.name != "nt", reason="the gh shim is gh.cmd: Windows PATH lookup")


def _config(root: Path, **over) -> Config:
    (root / "harness" / "config").mkdir(parents=True, exist_ok=True)  # the deny list reads it (#357 unit 5)
    (root / "harness" / "config" / "orchestration.json").write_text('{"targets": []}', encoding="utf-8")
    base = dict(claude_bin=str(root / "fake.py"), role_repo=root / "role", draft_dir=root / "draft",
                runs_dir=root / "runs", trees_dir=root / "trees", harness_root=root / "harness",
                state_dir=root / "state")
    base.update(over)
    return Config(**base)


def _tree_and_state(root: Path) -> tuple[Path, Path]:
    tree = root / "trees" / "rtree"
    tree.mkdir(parents=True)
    state = root / "state" / "rstate"
    state.mkdir(parents=True)
    return tree, state


def test_config_makes_relative_paths_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Config(claude_bin="fake.py", role_repo=Path("role"), draft_dir=Path("draft"),
                 runs_dir=Path("var/agentops/replays-2"), trees_dir=Path("trees"), harness_root=Path("harness"),
                 projects_dir=Path("projects"), cases_path=Path("cases.json"))
    for name in ("runs_dir", "trees_dir", "state_dir", "role_repo", "draft_dir", "harness_root", "projects_dir",
                 "cases_path"):
        assert getattr(cfg, name).is_absolute(), name
    assert cfg.state_dir == Path.cwd() / "var" / "agentops" / "replays-2-state"


@windows_only
def test_check_gh_shim_passes_when_shim_dir_resolves_first(tmp_path):
    tree, state = _tree_and_state(tmp_path)
    cfg = _config(tmp_path)
    launcher._write_shims(state, cfg)
    env = launcher._environment(state, CASE, "a")
    launcher.check_gh_shim(env, tree, state)  # raises when the check fails


@windows_only
def test_check_gh_shim_refuses_relative_state_entry_that_does_not_resolve_from_tree(tmp_path, monkeypatch):
    # the defect: the state dir is a RELATIVE PATH entry; the run's cwd is its own tree, where it resolves to nothing
    monkeypatch.chdir(tmp_path)
    tree, state = _tree_and_state(tmp_path)
    launcher._write_shims(state, _config(tmp_path))
    env = launcher._environment(state, CASE, "a")
    rest = env["PATH"].split(os.pathsep)[1:]  # drop the absolute state entry _environment put first
    env["PATH"] = os.pathsep.join([str(state.relative_to(tmp_path)), *rest])
    with pytest.raises(RuntimeError, match=r"gh shim does not resolve first on the run's PATH"):
        launcher.check_gh_shim(env, tree, state)


@windows_only
def test_check_gh_shim_refuses_when_another_gh_precedes_the_shim(tmp_path):
    tree, state = _tree_and_state(tmp_path)
    launcher._write_shims(state, _config(tmp_path))
    other = tmp_path / "other"
    other.mkdir()
    (other / "gh.cmd").write_text("@echo off\r\n", encoding="utf-8")
    env = launcher._environment(state, CASE, "a")
    env["PATH"] = str(other) + os.pathsep + env["PATH"]
    with pytest.raises(RuntimeError, match=r"does not resolve first.*other"):
        launcher.check_gh_shim(env, tree, state)


def test_run_one_does_not_start_the_model_when_gh_check_fails(tmp_path, monkeypatch):
    mark = tmp_path / "mark.txt"
    fake = tmp_path / "fake_claude.py"
    fake.write_text(MARK_CLAUDE, encoding="utf-8")
    monkeypatch.setenv("FAKE_MARK", str(mark))
    for name in launcher.TOKEN_VARS:
        monkeypatch.delenv(name, raising=False)

    def fake_tree(case, arm, n, base_dir, role_repo, draft_dir, **_):
        tree = Path(base_dir) / f"{case['key']}-{arm}-{n}"
        tree.mkdir(parents=True)
        return tree

    def refuse(env, tree, state):
        raise RuntimeError("gh shim does not resolve first on the run's PATH: got None")

    monkeypatch.setattr(launcher, "make_run_tree", fake_tree)
    monkeypatch.setattr(launcher, "check_gh_shim", refuse)
    cfg = _config(tmp_path, claude_bin=str(fake), timeout_s=60)

    with pytest.raises(RuntimeError, match=r"gh shim does not resolve first"):
        run_one("a", "old", 1, cfg)
    assert not mark.exists(), "the fake claude was started although the gh check failed"
    assert not cfg.runs_dir.exists() or not any(cfg.runs_dir.glob("*.json")), "a record was written"
