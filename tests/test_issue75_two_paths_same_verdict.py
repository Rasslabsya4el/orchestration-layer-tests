"""Issue 75 (unit 2): the home's merge path and the target dispatcher's auto path give one verdict on a collision.

Both paths end in the same `core_update.run_core_update`: the home's `core_targets.run_core_targets` runs it from the
home's checkout, `dispatch.apply_auto_core_update` runs it from the target's own installed copy. On Poe the verdicts
differed (55cb3006) only because the target's copy of `core_update.py` was older than #53 and held no
`_local_module_collisions`; the update carried the check in. With the same code in both places the verdicts match,
which these rows pin on a real-git tmp target (the #28/#53 fixtures, a fake `gh` home, no network):

(1) `.skill_counter` in `local_modules` is no collision: both paths take the update, the target holds the home's head.
(2) a `local_modules` entry whose file is a core manifest path refuses on both paths with the same phrase and the
    target's tree is byte-identical afterwards.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_issue28_core_update_failed_commit import (
    NEW_HOME_FILES, NEW_VERSION, ORCH_JSON, _fake_gh, _git, _target,
)
from tests.test_issue53_collision_refusal import (
    CLI, HOME_CLI, MAP_RUN, REGISTER, SHIPPED, _tree_hashes, _with_local_modules,
)
from tools.agentops import core_targets, dispatch

SKILL = "tools/agentops/skill_counter.py"
FOO = "tools/agentops/foo.py"
HOME_LOCAL_MODULES = [".autopilot", ".provisional", ".mods", ".skill_counter"]  # config/orchestration.json:8-13
HOME_FILES = {**NEW_HOME_FILES, **SHIPPED, CLI: HOME_CLI, MAP_RUN: "# core map_run\n",
              SKILL: "# core skill_counter\n", FOO: "# core foo\n"}


def _auto_target(tmp_path: Path, names: list[str], modules: dict[str, str]) -> Path:
    root = _target(tmp_path)
    settings = json.loads((root / ORCH_JSON).read_text(encoding="utf-8"))
    settings["update_rule"] = "auto"
    (root / ORCH_JSON).write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    _with_local_modules(root, names, modules)
    return root


def _home_path(root: Path) -> str:
    entry = {"repo": "owner/Target-Repo", "folder": str(root)}
    [line] = core_targets.run_core_targets([entry], home_root=root.parent, gh_runner=_fake_gh(HOME_FILES))
    return line


def _dispatcher_path(root: Path) -> str:
    line = dispatch.apply_auto_core_update(root, settings={"update_rule": "auto"}, gh_runner=_fake_gh(HOME_FILES))
    assert line is not None
    return line


def _tracked_tree(root: Path) -> dict[str, str]:
    """The tree without `artifacts/` -- the log lines each path writes about its own verdict."""
    return {rel: digest for rel, digest in _tree_hashes(root).items() if not rel.startswith("artifacts/")}


def _core_version(root: Path) -> str:
    return json.loads((root / ORCH_JSON).read_text(encoding="utf-8"))["core_version"]


# (1): `.skill_counter` is listed, the core ships skill_counter.py: both paths take the update.
def test_skill_counter_is_taken_by_both_paths(tmp_path: Path) -> None:
    home_root = _auto_target(tmp_path / "a", HOME_LOCAL_MODULES, {**SHIPPED, SKILL: "# core skill_counter\n"})
    auto_root = _auto_target(tmp_path / "b", HOME_LOCAL_MODULES, {**SHIPPED, SKILL: "# core skill_counter\n"})

    home_line = _home_path(home_root)
    auto_line = _dispatcher_path(auto_root)

    assert home_line == f"owner/Target-Repo: taken {NEW_VERSION[:8]}"
    assert auto_line.startswith("core update (auto): ") and "refused" not in auto_line and "failed" not in auto_line
    for root in (home_root, auto_root):
        assert _core_version(root) == NEW_VERSION
        assert (root / "docs/agents/three.md").is_file()
        assert (root / SKILL).read_text(encoding="utf-8") == "# core skill_counter\n"


# (2): a local module that is a manifest path refuses on both paths, same phrase, nothing written.
def test_manifest_collision_is_refused_by_both_paths(tmp_path: Path) -> None:
    home_root = _auto_target(tmp_path / "a", [".foo"], {FOO: REGISTER})
    auto_root = _auto_target(tmp_path / "b", [".foo"], {FOO: REGISTER})
    phrase = f"core update refused: local module .foo ({FOO}) collides with core {FOO}"
    before = {root: (_tracked_tree(root), _git(root, "rev-parse", "HEAD").stdout) for root in (home_root, auto_root)}

    home_line = _home_path(home_root)
    auto_line = _dispatcher_path(auto_root)

    assert home_line == f"owner/Target-Repo: failed: {phrase}"
    assert auto_line == f"core update (auto) refused: {phrase}"
    for root in (home_root, auto_root):
        assert (_tracked_tree(root), _git(root, "rev-parse", "HEAD").stdout) == before[root]
        assert _core_version(root) != NEW_VERSION


@pytest.fixture(autouse=True)
def _no_live_orch(monkeypatch):
    """Both paths ask `blocking_orch_live` (#188) of the tmp target (no ORCH there): pin it so a stray lock cannot colour the row."""
    from tools.agentops import core_update
    monkeypatch.setattr(core_update, "blocking_orch_live", lambda _root: False)
    monkeypatch.setattr(core_targets, "blocking_orch_live", lambda _root: False)
