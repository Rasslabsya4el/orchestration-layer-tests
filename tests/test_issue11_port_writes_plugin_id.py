"""Issue #11: a port writes ``monitor.plugin_id`` into the target's own settings.

The target's ``config/orchestration.json`` is written by the port (``port.write_own_files``), never by hand, so the
monitor's install folder, PANE and COMMAND (``port.write_monitor_stubs``) and the settings agree on one id:
``port._default_plugin_id(repo)`` for a new target. ``install_folder.plugin_id(target)`` reads it back.
A target whose settings already name ``monitor.plugin_id`` keeps that value: the port never overwrites it.

The install root and the user's home are redirected by env (``AGENTOPS_MODS_DIR``, ``HOME``, ``USERPROFILE``,
``AGENTOPS_MODS_REPO``), so no real folder is touched.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tools.agentops import port
from tools.agentops.install_folder import install_root, plugin_id

REPO = Path(__file__).resolve().parents[1]
SOURCE = "heavy-tiles"
UPDATE_RULE = "pull-request"
HOME_REPO = "owner/orchestration-layer"
CORE_VERSION = "0" * 40
REPO_SLUG = "X/My Repo.v2"


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A fake install root, a fake home, and this repo's settings naming ``heavy-tiles`` as the monitor's source."""
    mods = tmp_path / "mods"
    mods.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    this_repo = tmp_path / "this-repo"
    (this_repo / "config").mkdir(parents=True)
    (this_repo / "config" / "orchestration.json").write_text(
        json.dumps({"monitor": {"source": SOURCE}}), encoding="utf-8")
    monkeypatch.setenv("AGENTOPS_MODS_DIR", str(mods))
    monkeypatch.setenv("AGENTOPS_MODS_REPO", str(this_repo))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return mods


def _target(root: Path, name: str) -> Path:
    target = root / name
    target.mkdir(parents=True)
    return target


def _port_settings(target: Path, repo: str) -> dict:
    """What the port writes for a target: its settings file (``write_own_files``) and the monitor's own files
    (``write_monitor_stubs``), the two steps of ``run_port`` that concern the monitor id."""
    port.write_own_files(target, repo, None, UPDATE_RULE, HOME_REPO, CORE_VERSION,
                         plugin_name="mattpocock-skills@claude-plugins-official")
    port.write_monitor_stubs(target, repo, home_root=REPO)
    return json.loads((target / "config" / "orchestration.json").read_text(encoding="utf-8"))


def _mod_dir(target: Path) -> str:
    text = (target / port.MONITOR_DIR / "hooks" / "project.ts").read_text(encoding="utf-8")
    match = re.search(r"^export const MOD_DIR = '([^']*)'", text, re.MULTILINE)
    assert match, f"no MOD_DIR line in the written project.ts:\n{text[:400]}"
    return match.group(1)


def test_port_writes_monitor_plugin_id_into_the_settings(machine, tmp_path):
    target = _target(tmp_path / "targets", "my-repo-v2-checkout")
    settings = _port_settings(target, REPO_SLUG)
    assert settings["monitor"] == {"plugin_id": "my-repo-v2-monitor"}, settings.get("monitor")


def test_plugin_id_reads_back_and_the_mod_dir_ends_with_it(machine, tmp_path):
    target = _target(tmp_path / "targets", "my-repo-v2-checkout")
    _port_settings(target, REPO_SLUG)
    pid = plugin_id(target)
    assert pid == "my-repo-v2-monitor", pid
    mod_dir = _mod_dir(target)
    assert mod_dir == (install_root() / pid).as_posix(), mod_dir
    assert mod_dir.endswith("/" + pid), mod_dir
    assert SOURCE not in mod_dir


def test_an_explicit_monitor_plugin_id_of_the_target_is_kept(machine, tmp_path):
    target = _target(tmp_path / "targets", "explicit-target")
    original = {"repo": REPO_SLUG, "monitor": {"plugin_id": "custom-mon"}}
    (target / "config").mkdir()
    (target / "config" / "orchestration.json").write_text(json.dumps(original), encoding="utf-8")
    settings = _port_settings(target, REPO_SLUG)
    assert settings == original, settings
    assert plugin_id(target) == "custom-mon"
    assert _mod_dir(target) == (install_root() / "custom-mon").as_posix()
