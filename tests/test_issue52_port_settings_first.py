"""Issue #52: the port reads the owner's settings file before its first write, and a failed settings write leaves
nothing behind.

Seam: ``port.run_port`` on a tmp git target, with the install root (env ``AGENTOPS_MODS_DIR``), the user's home
(``HOME`` / ``USERPROFILE``) and the owner's settings file (a tmp file passed as ``settings_path``) all redirected:
no real ``~/.claude`` is touched.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tools.agentops import install_folder, port
from tools.agentops.core_check import MONITOR_LITERAL

REPO = Path(__file__).resolve().parents[1]
OWNER_REPO = "owner/Target-Repo"
PLUGIN = "mattpocock-skills@claude-plugins-official"
SOURCE = MONITOR_LITERAL
HOME_SHA = "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432"  # #367: the home's HEAD the port reads, pinned (no real checkout)


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
    monkeypatch.setattr(port, "_home_commit", lambda home_root, git_runner=None: HOME_SHA)  # #367: no real HEAD read
    return mods


@pytest.fixture(autouse=True)
def _origin_matches(monkeypatch):
    """The remote of the tmp target is `owner/Target-Repo`; the port checks it names `--repo` (not under test)."""
    monkeypatch.setattr(port, "check_target_ready", lambda *a, **k: None)


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


def _tree(root: Path) -> list[str]:
    """Every path of the target except its `.git` folder, relative to the target."""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if ".git" not in p.relative_to(root).parts)


def _run(target: Path, settings: Path):
    return port.run_port(target, OWNER_REPO, settings_path=settings, home_root=REPO)


# -- row 1: a bad settings file stops the port before the first write ---------------------------

def test_row1_bad_settings_stops_the_port_before_any_write(machine, tmp_path):
    bad = tmp_path / "owner" / "settings.json"
    bad.parent.mkdir()
    bad.write_text(json.dumps({"env": ["not", "an", "object"]}), encoding="utf-8")
    target = _make_target(tmp_path)
    tree_before = _tree(target)
    head_before = _git(target, "rev-parse", "HEAD")
    settings_before = bad.read_bytes()

    with pytest.raises(port.PortError) as exc:
        _run(target, bad)

    assert str(bad) in str(exc.value)
    assert _tree(target) == tree_before  # 0 new paths in the target repo
    assert _git(target, "rev-parse", "HEAD") == head_before  # nothing committed
    assert list(machine.iterdir()) == []  # 0 folders on the machine
    assert not install_folder.commands_dir().exists()  # no slash-command file either
    assert bad.read_bytes() == settings_before


# -- row 2: a failed swap of the settings write names the file and leaves no temporary file -----

def test_row2_failed_settings_swap_names_the_file_and_leaves_no_temporary_file(machine, settings, tmp_path,
                                                                              monkeypatch):
    target = _make_target(tmp_path)
    settings_before = settings.read_bytes()

    def failing_replace(src, dst, *a, **k):
        raise OSError("simulated swap failure")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(port.PortError) as exc:
        _run(target, settings)

    assert str(settings) in str(exc.value)
    assert list(settings.parent.glob("*.tmp-plugin-dirs")) == []  # 0 leftover temporary files
    assert settings.read_bytes() == settings_before  # the owner's file is untouched


def test_pocock_check_without_cache_or_settings_path_is_rc1_not_a_typeerror(capsys):
    """`core pocock-check` with no --plugin-cache and no settings path known (the standalone entry) reports it."""
    from tools.agentops.core_check import main

    assert main(["core", "pocock-check"]) == 1
    assert "pass --plugin-cache" in capsys.readouterr().out
