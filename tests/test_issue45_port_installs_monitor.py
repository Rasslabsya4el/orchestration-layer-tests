"""Issue #45: the port installs the target's monitor mod and registers it, so the pane works right after the port.

Seams: ``port.run_port`` (rows 1-3, the whole port into a tmp git target), ``target_add.run_target_add`` (row 4,
the dry run). The install root is redirected by env ``AGENTOPS_MODS_DIR``, the user's home (the slash-command folder)
by ``HOME`` / ``USERPROFILE``, the owner's settings file is a tmp file passed as ``settings_path``: no real
``~/.claude`` is touched.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tools.agentops import install_folder, port, target_add
from tools.agentops.core_check import MONITOR_LITERAL

REPO = Path(__file__).resolve().parents[1]
OWNER_REPO = "owner/Target-Repo"
PLUGIN = "mattpocock-skills@claude-plugins-official"
PLUGIN_ID = "target-repo-monitor"
SOURCE = MONITOR_LITERAL  # the home's monitor source folder name, as the mod's own files spell it


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout


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
    return mods


ORIGINAL = ('{\n\t"model": "opus",\n\t"enabledPlugins": {"' + PLUGIN + '": true},\n'
            '\t"env": {\n\t\t"KEEP": "1",\n\t\t"CLAUDE_CODE_PLUGIN_DIRS": "C:\\\\a\\\\mods\\\\x;C:\\\\a\\\\mods\\\\y"\n\t}\n}\n')


@pytest.fixture
def settings(tmp_path) -> Path:
    path = tmp_path / "owner" / "settings.json"
    path.parent.mkdir()
    path.write_bytes(ORIGINAL.encode("utf-8"))
    return path


def _make_target(root: Path, name: str = "target") -> Path:
    target = root / name
    target.mkdir(parents=True)
    _git(target, "init", "-q")
    _git(target, "config", "user.name", "owner")
    _git(target, "config", "user.email", "owner@example.com")
    _git(target, "remote", "add", "origin", "https://github.com/owner/Target-Repo.git")
    (target / "README.md").write_text("readme\n", encoding="utf-8")
    _git(target, "add", "--", "README.md")
    _git(target, "commit", "-q", "-m", "initial")
    return target


def _run(target: Path, settings: Path):
    return port.run_port(target, OWNER_REPO, settings_path=settings, home_root=REPO)


@pytest.fixture(autouse=True)
def _origin_matches(monkeypatch):
    """The remote of the tmp target is `owner/Target-Repo`; the port checks it names `--repo` (not under test)."""
    monkeypatch.setattr(port, "check_target_ready", lambda *a, **k: None)


# -- row 1 ------------------------------------------------------------------------------------

def test_row1_port_installs_the_folder_under_the_plugin_id_and_registers_it(machine, settings, tmp_path):
    target = _make_target(tmp_path)

    report = _run(target, settings)

    folder = machine / PLUGIN_ID
    manifest = json.loads((folder / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == PLUGIN_ID
    assert SOURCE not in (folder / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    # the atoms are registered under the plugin id, not under the source name (the host rejects another id's atoms)
    register = (folder / "hooks" / "register.tsx").read_text(encoding="utf-8")
    assert f"plugin: '{PLUGIN_ID}'" in register and f"'{SOURCE}'" not in register
    assert (folder / "hooks" / "project.ts").is_file() and (folder / "bin" / "collect.py").is_file()
    # the settings file: only the one env key changed, by exactly the appended folder
    expected = ORIGINAL.replace('mods\\\\y"', 'mods\\\\y;' + str(folder).replace("\\", "\\\\") + '"')
    assert settings.read_bytes().decode("utf-8") == expected
    assert json.loads(settings.read_text(encoding="utf-8"))["env"]["CLAUDE_CODE_PLUGIN_DIRS"].endswith(str(folder))
    # the slash-command file the mod install writes: the one `monitor.md` of every repo (#173)
    assert (install_folder.commands_dir() / "monitor.md").is_file()
    # the owner's list has nothing about the monitor
    assert "CLAUDE_CODE_PLUGIN_DIRS" not in report.next_step and "manual" not in report.next_step
    assert f"monitor installed at {folder}" in report.render()


def test_row1_settings_without_the_key_or_the_file_get_it(machine, tmp_path):
    target = _make_target(tmp_path)
    bare = tmp_path / "bare.json"
    bare.write_text('{\n  "model": "opus"\n}\n', encoding="utf-8")
    _run(target, bare)
    data = json.loads(bare.read_text(encoding="utf-8"))
    assert data["model"] == "opus" and data["env"] == {"CLAUDE_CODE_PLUGIN_DIRS": str(machine / PLUGIN_ID)}



def test_row1_a_missing_settings_file_is_created_with_the_key(machine, tmp_path):
    target = _make_target(tmp_path)
    none = tmp_path / "absent" / "settings.json"
    _run(target, none)
    assert json.loads(none.read_text(encoding="utf-8"))["env"] == {"CLAUDE_CODE_PLUGIN_DIRS": str(machine / PLUGIN_ID)}


# -- row 2 ------------------------------------------------------------------------------------

def test_row2_the_second_run_changes_no_byte_of_the_settings_and_the_folder_is_the_same(machine, settings, tmp_path):
    target = _make_target(tmp_path)
    _run(target, settings)
    first_settings = settings.read_bytes()
    folder = machine / PLUGIN_ID
    first_tree = {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}

    _run(target, settings)

    assert settings.read_bytes() == first_settings
    second_tree = {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    assert second_tree == first_tree


# -- row 3 ------------------------------------------------------------------------------------

def test_row3_a_folder_claimed_by_another_repo_is_refused_and_nothing_is_written(machine, settings, tmp_path):
    target = _make_target(tmp_path)
    folder = machine / PLUGIN_ID
    (folder / "hooks").mkdir(parents=True)
    claim = folder / "hooks" / "project.ts"
    claim.write_text("export const REPO = 'C:/somewhere/else'\n", encoding="utf-8")

    with pytest.raises(port.PortError) as exc:
        _run(target, settings)

    assert f"refused: the monitor folder {folder} is installed for another REPO (C:/somewhere/else)" in str(exc.value)
    assert settings.read_bytes().decode("utf-8") == ORIGINAL
    assert sorted(p.name for p in folder.rglob("*")) == ["hooks", "project.ts"]
    assert claim.read_text(encoding="utf-8") == "export const REPO = 'C:/somewhere/else'\n"
    assert not (target / "config" / "orchestration.json").exists()   # nothing at the target either
    assert not install_folder.commands_dir().exists()


# -- row 4 ------------------------------------------------------------------------------------

def test_row4_target_add_dry_run_says_it_would_install_and_register_the_monitor(machine, tmp_path, monkeypatch):
    home = tmp_path / "homerepo"
    (home / "config").mkdir(parents=True)
    (home / "config" / "orchestration.json").write_text(json.dumps({"targets": []}), encoding="utf-8")
    target = _make_target(tmp_path, "tgt")
    monkeypatch.setattr(port, "plan_port", lambda *a, **k: (["CLAUDE.md"], ["bug"]))

    rc, lines = target_add.run_target_add(target, "Brain: t", dry_run=True, home_root=home,
                                          settings_path=tmp_path / "s.json")

    assert rc == 0, lines
    text = "\n".join(lines)
    folder = machine / "target-repo-monitor"
    assert f"would install the monitor at {folder} and register it" in text
    assert text.count('set "CLAUDE_CODE_PLUGIN_DIRS"') == 0
    assert not folder.exists()


def test_row4_port_dry_run_says_it_too(machine, tmp_path, monkeypatch):
    target = _make_target(tmp_path)
    monkeypatch.setattr(port, "check_machine_ready", lambda *a, **k: PLUGIN)
    out = port.run_dry_run(target, OWNER_REPO, home_root=REPO, settings_path=tmp_path / "s.json")
    assert f"would install the monitor at {machine / PLUGIN_ID} and register it" in out
    assert not (machine / PLUGIN_ID).exists()
