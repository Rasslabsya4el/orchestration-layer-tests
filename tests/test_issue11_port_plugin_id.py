"""Issue #11 unit 1 (plugin id): a target's default monitor plugin id comes from the target's own repo name.

A target without ``monitor.plugin_id`` gets ``<repo name, lowercased, each run of non-alphanumeric characters
one "-">-monitor`` as its install folder, never this repo's ``monitor.source`` folder (the source's name is no
default for a target). An explicit ``monitor.plugin_id`` of the target wins. Two targets get two folders.

The seam is ``port.write_monitor_stubs``: it writes the target's ``hooks/project.ts`` with ``MOD_DIR`` set to the
install folder, the value a port hands to ``mods install``. The install root and this repo's settings are
redirected by env (``AGENTOPS_MODS_DIR``, ``AGENTOPS_MODS_REPO``), so no real home folder is touched.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tools.agentops import port
from tools.agentops.core_check import MONITOR_LITERAL
from tools.agentops.install_folder import install_root

REPO = Path(__file__).resolve().parents[1]
SOURCE = MONITOR_LITERAL


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A fake install root, and this repo's settings naming ``SOURCE`` as the monitor's source folder."""
    mods = tmp_path / "mods"
    mods.mkdir()
    this_repo = tmp_path / "this-repo"
    (this_repo / "config").mkdir(parents=True)
    (this_repo / "config" / "orchestration.json").write_text(
        json.dumps({"monitor": {"source": SOURCE}}), encoding="utf-8")
    monkeypatch.setenv("AGENTOPS_MODS_DIR", str(mods))
    monkeypatch.setenv("AGENTOPS_MODS_REPO", str(this_repo))
    return mods


def _target(root: Path, name: str, plugin_id: str | None = None) -> Path:
    target = root / name
    target.mkdir(parents=True)
    if plugin_id is not None:
        (target / "config").mkdir()
        (target / "config" / "orchestration.json").write_text(
            json.dumps({"monitor": {"plugin_id": plugin_id}}), encoding="utf-8")
    return target


def _mod_dir_of(target: Path, repo: str) -> str:
    """The MOD_DIR value a port wrote into the target's monitor ``hooks/project.ts``."""
    port.write_monitor_stubs(target, repo, home_root=REPO)
    text = (target / port.MONITOR_DIR / "hooks" / "project.ts").read_text(encoding="utf-8")
    match = re.search(r"^export const MOD_DIR = '([^']*)'", text, re.MULTILINE)
    assert match, f"no MOD_DIR line in the written project.ts:\n{text[:400]}"
    return match.group(1)


def test_target_without_plugin_id_gets_its_own_repo_name_not_the_source_folder(machine, tmp_path):
    target = _target(tmp_path / "targets", "my-repo-v2-checkout")
    mod_dir = _mod_dir_of(target, "X/My Repo.v2")
    assert mod_dir == (install_root() / "my-repo-v2-monitor").as_posix(), mod_dir
    assert SOURCE not in mod_dir, f"the source folder {SOURCE!r} leaked into a target's folder: {mod_dir}"


def test_explicit_monitor_plugin_id_of_the_target_wins(machine, tmp_path):
    target = _target(tmp_path / "targets", "explicit-target", plugin_id="custom-mon")
    mod_dir = _mod_dir_of(target, "X/My Repo.v2")
    assert mod_dir == (install_root() / "custom-mon").as_posix(), mod_dir


def test_two_targets_without_plugin_id_get_two_folders(machine, tmp_path):
    first = _target(tmp_path / "targets", "alpha-checkout")
    second = _target(tmp_path / "targets", "beta-checkout")
    first_dir = _mod_dir_of(first, "owner/alpha-repo")
    second_dir = _mod_dir_of(second, "owner/beta-repo")
    assert first_dir == (install_root() / "alpha-repo-monitor").as_posix(), first_dir
    assert second_dir == (install_root() / "beta-repo-monitor").as_posix(), second_dir
    assert first_dir != second_dir
    assert SOURCE not in first_dir and SOURCE not in second_dir
