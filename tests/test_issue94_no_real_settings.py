"""#94: a test run never writes the owner's Claude Code settings.

`mods.run_install` called with no `settings_path` under pytest writes a throwaway settings file under the system temp
folder; the file at HOME/.claude/settings.json (a fake home here) keeps its bytes. Temp folders only.
"""

import hashlib
import json
import tempfile
from pathlib import Path

import pytest

from tools.agentops import mods

SRC = {
    ".claude-plugin/plugin.json": b'{\n  "name": "pob-tiles",\n  "version": "0.3.0"\n}\n',
    "hooks/register.tsx": b"export const a = 1;\n",
}
HOME_SETTINGS = b'{\r\n  "env": {"CLAUDE_CODE_PLUGIN_DIRS": "C:\\real\\mod"},\r\n  "theme": "dark"\r\n}\r\n'


@pytest.fixture
def world(tmp_path, monkeypatch):
    repo, root, home = tmp_path / "repo", tmp_path / "mods-installed", tmp_path / "home"
    for rel, data in SRC.items():
        p = repo / "mods" / "pob-tiles" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "orchestration.json").write_text(json.dumps({"repo": "x"}), encoding="utf-8")
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_bytes(HOME_SETTINGS)
    monkeypatch.setenv("AGENTOPS_MODS_REPO", str(repo))
    monkeypatch.setenv("AGENTOPS_MODS_DIR", str(root))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("AGENTOPS_SETTINGS_PATH", raising=False)
    return home, root


def test_install_without_settings_path_leaves_the_home_settings_alone(world):
    home, root = world
    real = home / ".claude" / "settings.json"
    before = hashlib.sha256(real.read_bytes()).hexdigest()
    assert mods.run_install("pob-tiles", commands_dir=home / "cmds") == 0
    written = mods.default_settings_path()
    print(f"\nhome settings sha before {before[:12]} after "
          f"{hashlib.sha256(real.read_bytes()).hexdigest()[:12]}; throwaway {written}")
    assert real.read_bytes() == HOME_SETTINGS
    assert written != real
    assert Path(tempfile.gettempdir()).resolve() in written.resolve().parents
    assert home.resolve() not in written.resolve().parents
    assert str(root / "pob-tiles") in json.loads(written.read_text(encoding="utf-8"))["env"]["CLAUDE_CODE_PLUGIN_DIRS"]


def test_explicit_env_path_wins_over_the_throwaway(world, monkeypatch, tmp_path):
    explicit = tmp_path / "mine.json"
    monkeypatch.setenv("AGENTOPS_SETTINGS_PATH", str(explicit))
    assert mods.default_settings_path() == explicit


def test_each_test_gets_a_fresh_throwaway(world):
    p = mods.default_settings_path()
    p.write_text('{"env": {"CLAUDE_CODE_PLUGIN_DIRS": "stale"}}', encoding="utf-8")
    assert json.loads(mods.default_settings_path().read_text(encoding="utf-8")) == {"env": {"CLAUDE_CODE_PLUGIN_DIRS": "stale"}}
