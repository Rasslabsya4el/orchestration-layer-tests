"""`mods install` under another plugin id rewrites the quoted id in the hook files (#1043 acceptance 1).

Temp folders only: the repository copy, the install root and the user's home are all under tmp_path.
"""

import json

import pytest

from tools.agentops import mods

SRC, INST = "x-src", "x-inst"

REGISTER = (
    b"// the plugin id \"x-src\" is spelled in this comment too\n"
    b"export const a = atom({ plugin: 'x-src', key: 'a' })\n"
    b"export const b = atom({ plugin: 'x-src', key: 'b' })\n"
)
MANIFEST_TEXT = json.dumps({"name": SRC, "version": "0.1.0"}, indent=2).encode("utf-8")
COLLECT = b"PLUGIN = 'x-src'\n"


def _file(root, rel, data):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


@pytest.fixture
def two_file_mod(tmp_path, monkeypatch):
    repo, inst_root, home = tmp_path / "repo", tmp_path / "inst", tmp_path / "home"
    _file(repo, f"mods/{SRC}/hooks/register.tsx", REGISTER)
    _file(repo, f"mods/{SRC}/bin/collect.py", COLLECT)
    _file(repo, f"mods/{SRC}/.claude-plugin/plugin.json", MANIFEST_TEXT)
    monkeypatch.setenv("AGENTOPS_MODS_REPO", str(repo))
    monkeypatch.setenv("AGENTOPS_MODS_DIR", str(inst_root))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(mods, "install_name", lambda name: INST if name == SRC else name)
    return repo, inst_root / INST


def test_install_under_other_id_rewrites_quoted_ids_in_hooks_only(two_file_mod, capsys):
    repo, inst = two_file_mod
    assert mods.run_install(SRC, settings_path=repo / "no-settings.json", commands_dir=repo / "cmds") == 0
    hooks = (inst / "hooks" / "register.tsx").read_bytes()
    assert hooks.count(SRC.encode()) == 0
    assert hooks.count(INST.encode()) == 3
    # outside hooks/ every byte is the repository's
    assert (inst / "bin" / "collect.py").read_bytes() == COLLECT
    manifest = json.loads((inst / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == INST
    assert "x-inst" in capsys.readouterr().out


def test_compare_is_clean_after_install_under_other_id(two_file_mod):
    repo, inst = two_file_mod
    mods.run_install(SRC, settings_path=repo / "no-settings.json", commands_dir=repo / "cmds")
    total, diff = mods.compare(SRC)
    assert diff == [] and total == 3


def test_home_install_name_leaves_every_byte_unchanged():
    assert mods._installed(SRC, SRC, "hooks/register.tsx", REGISTER) == REGISTER
    assert mods._installed(SRC, SRC, ".claude-plugin/plugin.json", MANIFEST_TEXT) == MANIFEST_TEXT


def test_rewrite_only_quoted_source_name_under_hooks_with_listed_suffixes():
    other = b"const x = 'x-srcx' + \"x-src\" + `x-src`\n"
    assert mods._installed(SRC, INST, "hooks/a.ts", other) == b"const x = 'x-srcx' + \"x-inst\" + `x-src`\n"
    assert mods._installed(SRC, INST, "hooks/hooks.json", b'{"plugin": "x-src"}') == b'{"plugin": "x-inst"}'
    assert mods._installed(SRC, INST, "hooks/a.js", b"'x-src'") == b"'x-src'"
    assert mods._installed(SRC, INST, "bin/a.ts", b"'x-src'") == b"'x-src'"
    assert mods._installed(SRC, INST, "types/index.ts", b"'x-src'") == b"'x-src'"
