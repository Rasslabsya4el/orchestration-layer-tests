"""Issue 75: the skill counter the core ships is not a target's own local module.

`.skill_counter` is a core manifest module, so it joins `port.CORE_LOCAL_MODULES`: `core update` never counts its
manifest path as a collision (row 1), `port` seeds it into a new target's `local_modules` (row 3). Any other manifest
module a target lists still collides (row 2, unchanged). Seams: `core_update.run_core_update` on a real-git tmp target
(the #28 family's helpers, a fake `gh` home, no network) and `port.run_port` on a tmp git target (the #45 pattern).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.test_issue28_core_update_failed_commit import NEW_HOME_FILES, ORCH_JSON, _fake_gh, _git, _target
from tests.test_issue53_collision_refusal import (
    CLI, HOME_CLI, MAP_RUN, REGISTER, SHIPPED, _tree_hashes, _with_local_modules,
)
from tools.agentops import port
from tools.agentops.core_update import CoreUpdateError, UpdateResult, run_core_update

REPO = Path(__file__).resolve().parents[1]
OWNER_REPO = "owner/Target-Repo"
PLUGIN = "mattpocock-skills@claude-plugins-official"
HOME_SHA = "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432"  # #367: the home's HEAD the port reads, pinned (no real checkout)
SOURCE = "pob-slots"
SKILL = "tools/agentops/skill_counter.py"
FOO = "tools/agentops/foo.py"
HOME_LOCAL_MODULES = [".autopilot", ".provisional", ".mods", ".skill_counter"]  # config/orchestration.json:8-13
HOME_FILES = {**NEW_HOME_FILES, **SHIPPED, CLI: HOME_CLI, MAP_RUN: "# core map_run\n",
              SKILL: "# core skill_counter\n", FOO: "# core foo\n"}


def _update(root: Path) -> UpdateResult:
    return run_core_update(root, gh_runner=_fake_gh(HOME_FILES), live_check=lambda: False,
                           step_runner=lambda _root: [])


def _refused(root: Path) -> str:
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    before = _tree_hashes(root)
    with pytest.raises(CoreUpdateError) as caught:
        _update(root)
    assert _tree_hashes(root) == before, "the target's tree is byte-identical: nothing written, no leftover"
    assert _git(root, "rev-parse", "HEAD").stdout.strip() == head, "no commit"
    return str(caught.value)


# (1): the home's own list, `.skill_counter` included, with the core's skill_counter.py in the target -- no collision.
def test_skill_counter_in_local_modules_is_not_a_collision(tmp_path: Path) -> None:
    root = _target(tmp_path)
    _with_local_modules(root, HOME_LOCAL_MODULES, {**SHIPPED, SKILL: "# core skill_counter\n"})

    result = _update(root)

    assert "docs/agents/three.md" in result.changed
    assert (root / SKILL).is_file()


# (2): any other local module whose file is a manifest path still collides -- unchanged by #75.
def test_other_manifest_module_still_collides(tmp_path: Path) -> None:
    root = _target(tmp_path)
    _with_local_modules(root, [".foo"], {FOO: REGISTER})

    message = _refused(root)

    assert message == f"core update refused: local module .foo ({FOO}) collides with core {FOO}"


# (3): `port` seeds `.skill_counter` into a new target's local_modules, next to the autopilot, provisional and mods.
@pytest.fixture
def machine(tmp_path, monkeypatch):
    mods = tmp_path / "mods"
    mods.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    this_repo = tmp_path / "this-repo"
    (this_repo / "config").mkdir(parents=True)
    (this_repo / "config" / "orchestration.json").write_text(json.dumps({"monitor": {"source": SOURCE}}),
                                                             encoding="utf-8")
    monkeypatch.setenv("AGENTOPS_MODS_DIR", str(mods))
    monkeypatch.setenv("AGENTOPS_MODS_REPO", str(this_repo))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(port, "check_home_clean", lambda root=None: None)
    monkeypatch.setattr(port, "check_machine_ready", lambda *a, **k: PLUGIN)
    monkeypatch.setattr(port, "sync_labels", lambda repo, gh_runner=None, labels=None: ([], []))
    monkeypatch.setattr(port, "check_target_ready", lambda *a, **k: None)
    monkeypatch.setattr(port, "_home_commit", lambda home_root, git_runner=None: HOME_SHA)  # #367: no real HEAD read
    return mods


@pytest.fixture
def settings(tmp_path) -> Path:
    path = tmp_path / "owner" / "settings.json"
    path.parent.mkdir()
    path.write_bytes(('{\n\t"model": "opus",\n\t"enabledPlugins": {"' + PLUGIN + '": true},\n'
                      '\t"env": {\n\t\t"KEEP": "1",\n\t\t"CLAUDE_CODE_PLUGIN_DIRS": "C:\\\\a\\\\mods\\\\x"\n\t}\n}\n'
                      ).encode("utf-8"))
    return path


def _make_target(root: Path) -> Path:
    target = root / "target"
    target.mkdir(parents=True)
    for args in (("init", "-q"), ("config", "user.name", "owner"), ("config", "user.email", "owner@example.com"),
                 ("remote", "add", "origin", "https://github.com/owner/Target-Repo.git")):
        subprocess.run(["git", "-C", str(target), *args], capture_output=True, text=True, check=True)
    (target / "README.md").write_text("readme\n", encoding="utf-8")
    for args in (("add", "--", "README.md"), ("commit", "-q", "-m", "initial")):
        subprocess.run(["git", "-C", str(target), *args], capture_output=True, text=True, check=True)
    return target


def test_port_seeds_skill_counter_into_local_modules(machine, settings, tmp_path) -> None:
    target = _make_target(tmp_path)

    port.run_port(target, OWNER_REPO, settings_path=settings, home_root=REPO)

    seeded = json.loads((target / ORCH_JSON).read_text(encoding="utf-8"))["local_modules"]
    assert ".skill_counter" in seeded
    assert seeded == HOME_LOCAL_MODULES
