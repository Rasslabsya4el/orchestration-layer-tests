"""Issue 53 (unit 3b): `core update` refuses, before any write, when a path of the core manifest or a core
subcommand name collides with a module in the target's `local_modules` or a subcommand that module registers.
The refusal names both sides; there is no "local overrides core". Real git on a tmp target (the #28 family's
helpers, a fake `gh` home, no network)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.test_issue28_core_update_failed_commit import (
    NEW_HOME_FILES, ORCH_JSON, _fake_gh, _git, _target, _write,
)
from tools.agentops.core_update import CoreUpdateError, UpdateResult, run_core_update
from tools.agentops.port import CORE_LOCAL_MODULES

CLI = "tools/agentops/cli.py"
MAP_RUN = "tools/agentops/map_run.py"
REGISTER = "def register(core):\n    pass\n"
HOME_CLI = (
    "def build(sub):\n"                                    # line 1
    "    sub.add_parser('worker')\n"                       # line 2
    "    sub.add_parser(\"map-run\", help='x')\n"          # line 3
)
SHIPPED = {"tools/agentops/autopilot.py": "# core autopilot\n", "tools/agentops/provisional.py": "# core provisional\n",
           "tools/agentops/mods.py": "# core mods\n"}  # the modules `port` seeds into local_modules
SUB_MOD = "tools/agentops/sub/mod.py"
HOME_FILES = {**NEW_HOME_FILES, **SHIPPED, CLI: HOME_CLI, MAP_RUN: "# core map_run\n", SUB_MOD: "# core sub.mod\n"}


def _with_local_modules(root: Path, names: list[str] | None, modules: dict[str, str]) -> None:
    settings = json.loads((root / ORCH_JSON).read_text(encoding="utf-8"))
    if names is not None:
        settings["local_modules"] = names
    _write(root, ORCH_JSON, json.dumps(settings, indent=2) + "\n")
    for rel, text in modules.items():
        _write(root, rel, text)
    assert _git(root, "add", "-A").returncode == 0
    assert _git(root, "commit", "--allow-empty", "-m", "target local modules").returncode == 0


def _tree_hashes(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file() and ".git" not in path.relative_to(root).parts
            and path.relative_to(root).as_posix() != "artifacts/runs/dispatch.out"}  # #49: the refusal's log line


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
    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    return str(caught.value)


# (1) + (5): a local module whose file is a manifest path -- the message names both sides.
def test_local_module_on_a_manifest_path_is_refused_naming_both_sides(tmp_path: Path) -> None:
    root = _target(tmp_path)
    _with_local_modules(root, [".map_run"], {MAP_RUN: "def register(core):\n    pass\n"})

    message = _refused(root)

    assert message == (f"core update refused: local module .map_run ({MAP_RUN}) collides with core {MAP_RUN}")
    assert "\n" not in message


# (2): a differently named local module registers a subcommand the home's cli.py registers.
def test_local_module_subcommand_equal_to_a_core_subcommand_is_refused(tmp_path: Path) -> None:
    root = _target(tmp_path)
    _with_local_modules(root, [".mine"], {
        "tools/agentops/mine.py": "def add(sub):\n    sub.add_parser('other')\n    sub.add_parser('map-run')\n"})

    message = _refused(root)

    assert message == ("core update refused: local module .mine (tools/agentops/mine.py) collides with core "
                       f"subcommand 'map-run' registered at {CLI}:3")


# several collisions: one line, separated by `; `; a dotted module name maps to the same file.
def test_several_collisions_share_one_line(tmp_path: Path) -> None:
    root = _target(tmp_path)
    _with_local_modules(root, ["tools.agentops.map_run", ".mine"], {
        MAP_RUN: "def register(core):\n    core.add_parser('worker')\n",
        "tools/agentops/mine.py": "sub.add_parser('map-run')\n"})

    message = _refused(root)

    assert "\n" not in message
    assert message.startswith("core update refused: local module tools.agentops.map_run ")
    parts = message.split("; ")
    assert len(parts) == 3
    assert f"collides with core {MAP_RUN}" in parts[0]
    assert f"subcommand 'worker' registered at {CLI}:2" in parts[1]
    assert f"local module .mine (tools/agentops/mine.py) collides with core subcommand 'map-run'" in parts[2]


# (3): a local module with its own file name and its own subcommand -- the update goes through.
def test_local_module_with_own_names_does_not_block_the_update(tmp_path: Path) -> None:
    root = _target(tmp_path)
    _with_local_modules(root, [".mine"], {
        "tools/agentops/mine.py": "def add(sub):\n    sub.add_parser('my-own-command')\n"})

    result = _update(root)

    assert "docs/agents/three.md" in result.changed
    assert (root / "docs/agents/three.md").read_text(encoding="utf-8") == "three v2 (new)\n"
    assert (root / "tools/agentops/mine.py").is_file()


# (4): no local_modules at all, or an empty list -- no check, the update goes through.
@pytest.mark.parametrize("names", [None, []])
def test_target_without_local_modules_is_updated(tmp_path: Path, names: list[str] | None) -> None:
    root = _target(tmp_path)
    _with_local_modules(root, names, {})

    result = _update(root)

    assert "docs/agents/three.md" in result.changed


# the core-shipped modules `port` seeds are not a target's own: no collision on their manifest paths.
def test_port_default_local_modules_are_not_a_collision(tmp_path: Path) -> None:
    root = _target(tmp_path)
    _with_local_modules(root, list(CORE_LOCAL_MODULES), dict(SHIPPED))

    result = _update(root)

    assert "docs/agents/three.md" in result.changed


def test_own_module_next_to_the_port_defaults_is_refused_alone(tmp_path: Path) -> None:
    root = _target(tmp_path)
    _with_local_modules(root, [*CORE_LOCAL_MODULES, ".map_run"], {**SHIPPED, MAP_RUN: REGISTER})

    message = _refused(root)

    assert message == f"core update refused: local module .map_run ({MAP_RUN}) collides with core {MAP_RUN}"


# `.a.b` is `tools.agentops.a.b` (extensions.Registry.load): the file tools/agentops/a/b.py.
def test_dotted_relative_name_maps_to_a_file_in_a_subfolder(tmp_path: Path) -> None:
    root = _target(tmp_path)
    _with_local_modules(root, [".sub.mod"], {SUB_MOD: REGISTER})

    message = _refused(root)

    assert message == f"core update refused: local module .sub.mod ({SUB_MOD}) collides with core {SUB_MOD}"
