"""#885: `mods install|status` of the source folder `pob-tiles` goes to `<install_root>/<monitor.plugin_id>`.

Temp folders only (env AGENTOPS_MODS_REPO / AGENTOPS_MODS_DIR); the real ~/.claude/mods is never touched.
"""

import hashlib
import json
import os
import subprocess
import time

import pytest

from tools.agentops import mods

SRC = {
    ".claude-plugin/plugin.json": b'{\n  "name": "pob-tiles",\n  "version": "0.3.0",\n'
                                  b'  "author": { "name": "orchestration core" }\n}\n',
    "hooks/register.tsx": b"export const a = 1;\r\n",
    "hooks/project.ts": b"export const MOD_DIR = 'x';\n",
    "addon/addon.py": b"print('addon')\n",
}


def _git(repo, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True,
                   capture_output=True)


def _write(root, files):
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def _tree(root):
    """relative path -> sha256 of every file under root (empty when root is missing)."""
    if not root.is_dir():
        return {}
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _src_tree():
    return {rel: hashlib.sha256(data).hexdigest() for rel, data in SRC.items()}


def _installed_tree():
    """what an install under zorblax-monitor holds: the source bytes, the manifest's top-level name renamed (#885)."""
    t = _src_tree()
    renamed = SRC[".claude-plugin/plugin.json"].replace(b'"name": "pob-tiles"', b'"name": "zorblax-monitor"', 1)
    t[".claude-plugin/plugin.json"] = hashlib.sha256(renamed).hexdigest()
    return t


def _no_cache(tree):
    return {k: v for k, v in tree.items() if not k.startswith(".cache/")}


def _set_config(repo, cfg):
    (repo / "config" / "orchestration.json").write_text(json.dumps(cfg), encoding="utf-8")


def _future(p):
    t = time.time() + 3600
    os.utime(p, (t, t))


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo, root = tmp_path / "repo", tmp_path / "mods-installed"
    _write(repo / "mods" / "pob-tiles", SRC)
    (repo / "config").mkdir(parents=True)
    _set_config(repo, {"monitor": {"source": "pob-tiles", "addon": "mods/pob-tiles/addon/addon.py"}})
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "import")
    monkeypatch.setenv("AGENTOPS_MODS_REPO", str(repo))
    monkeypatch.setenv("AGENTOPS_MODS_DIR", str(root))
    return repo, root


def test_row1_install_under_plugin_id_leaves_pob_slots_folder_alone(env, capsys):
    repo, root = env
    _set_config(repo, {"monitor": {"source": "pob-tiles", "plugin_id": "zorblax-monitor"}})
    _write(root / "pob-tiles", {"only-pob-tiles.txt": b"mine", "hooks/register.tsx": b"installed pob-tiles",
                                "bin/.cache/x.log": b"log"})
    before = _tree(root / "pob-tiles")
    assert mods.run_install("pob-tiles") == 0
    out = capsys.readouterr().out
    after = _tree(root / "pob-tiles")
    changed = sum(1 for k in before if after.get(k) != before[k])
    deleted = sum(1 for k in before if k not in after)
    print(f"\nrow 1: files under pob-tiles before {len(before)}, after {len(after)}, "
          f"changed {changed}, deleted {deleted}")
    assert after == before
    assert changed == 0 and deleted == 0 and sorted(after) == sorted(before)
    assert _tree(root / "zorblax-monitor") == _installed_tree()
    assert sorted(p.name for p in root.iterdir()) == ["pob-tiles", "zorblax-monitor"]
    assert "installed as zorblax-monitor" in out


@pytest.mark.parametrize("case", ["no_key", "no_monitor_section", "no_file"])
def test_row2_without_plugin_id_target_is_pob_slots(env, case):
    repo, root = env
    if case == "no_key":
        _set_config(repo, {"monitor": {"source": "pob-tiles", "addon": "mods/pob-tiles/addon/addon.py"}})
    elif case == "no_monitor_section":
        _set_config(repo, {"repo": "x"})
    else:
        (repo / "config" / "orchestration.json").unlink()
    # #1096: the source folder is named by `monitor.source`; without it no folder is special (the plain default id)
    assert mods.plugin_id() == ("pob-tiles" if case == "no_key" else "monitor")
    assert mods.run_install("pob-tiles") == 0
    assert _tree(root / "pob-tiles") == _src_tree()
    assert not (root / "zorblax-monitor").exists()
    assert mods.compare("pob-tiles") == (len(SRC), [])


def test_row3_status_under_plugin_id_compares_only_its_folder(env, capsys):
    repo, root = env
    _set_config(repo, {"monitor": {"source": "pob-tiles", "plugin_id": "zorblax-monitor"}})
    _write(root / "pob-tiles", {"stale.txt": b"old pob-tiles", "hooks/register.tsx": b"decoy"})
    decoy = _tree(root / "pob-tiles")
    assert mods.run_install("pob-tiles") == 0
    assert mods.run_status(["pob-tiles"]) == 0
    out = capsys.readouterr().out
    assert "4 files compared, 0 differ" in out
    assert "stale.txt" not in out
    reg = root / "zorblax-monitor" / "hooks" / "register.tsx"
    reg.write_bytes(SRC["hooks/register.tsx"].replace(b"1", b"2", 1))  # one byte changed in the id folder
    assert mods.run_status(["pob-tiles"]) == 1
    out = capsys.readouterr().out
    print(f"\nrow 3 status after one byte: {out.strip()}")
    assert "4 files compared, 1 differ" in out and "  hooks/register.tsx" in out
    assert "stale.txt" not in out
    assert _tree(root / "pob-tiles") == decoy


def test_installed_manifest_name_is_the_plugin_id_and_status_is_clean(env):
    repo, root = env
    _set_config(repo, {"monitor": {"source": "pob-tiles", "plugin_id": "zorblax-monitor"}})
    assert mods.run_install("pob-tiles") == 0
    installed = (root / "zorblax-monitor" / ".claude-plugin" / "plugin.json").read_bytes()
    assert json.loads(installed)["name"] == "zorblax-monitor"
    # only the top-level name changed; the rest of the manifest is the repository's bytes
    assert installed == SRC[".claude-plugin/plugin.json"].replace(b'"name": "pob-tiles"', b'"name": "zorblax-monitor"', 1)
    assert (repo / "mods" / "pob-tiles" / ".claude-plugin" / "plugin.json").read_bytes() == SRC[".claude-plugin/plugin.json"]
    assert mods.compare("pob-tiles") == (len(SRC), [])
    assert mods.run_status(["pob-tiles"]) == 0


def test_row4_refusal_and_backup_act_only_inside_plugin_id_folder(env, capsys):
    repo, root = env
    _set_config(repo, {"monitor": {"source": "pob-tiles", "plugin_id": "zorblax-monitor"}})
    assert mods.run_install("pob-tiles") == 0
    hand = root / "zorblax-monitor" / "hooks" / "register.tsx"
    hand.write_bytes(b"hand edit in no commit")
    _future(hand)  # newer than the commit, bytes in no commit: refused
    _write(root / "pob-tiles", {"hooks/register.tsx": b"decoy hand edit"})
    _future(root / "pob-tiles" / "hooks" / "register.tsx")  # a newer decoy in the other folder: never consulted
    decoy = _tree(root / "pob-tiles")
    assert mods.run_install("pob-tiles") == 1
    out = capsys.readouterr().out
    assert "refused" in out and "hooks/register.tsx" in out
    assert hand.read_bytes() == b"hand edit in no commit"
    assert _tree(root / "pob-tiles") == decoy
    assert mods.run_install("pob-tiles", force=True) == 0
    backups = [p for p in root.rglob("backup-*") if p.is_dir()]
    assert len(backups) == 1 and root / "zorblax-monitor" in backups[0].parents
    assert (backups[0] / "hooks" / "register.tsx").read_bytes() == b"hand edit in no commit"
    assert _tree(root / "pob-tiles") == decoy  # nothing written, backed up or deleted in the other folder
    assert _no_cache(_tree(root / "zorblax-monitor")) == _installed_tree()


def test_other_mods_keep_their_own_folder_name(env):
    repo, root = env
    _write(repo / "mods" / "unslop", {"DESIGN.md": b"design"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "unslop")
    _set_config(repo, {"monitor": {"source": "pob-tiles", "plugin_id": "zorblax-monitor"}})
    assert mods.install_name("unslop") == "unslop"
    assert mods.run_install("unslop") == 0
    assert _tree(root / "unslop") == {"DESIGN.md": hashlib.sha256(b"design").hexdigest()}
    assert not (root / "zorblax-monitor" / "DESIGN.md").exists()


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", ".", "unslop"])
def test_plugin_id_that_is_not_a_plain_free_folder_name_is_refused(env, bad):
    repo, root = env
    _write(repo / "mods" / "unslop", {"DESIGN.md": b"design"})
    _set_config(repo, {"monitor": {"source": "pob-tiles", "plugin_id": bad}})
    with pytest.raises(RuntimeError):
        mods.plugin_id()
    assert not root.exists() or not any(root.iterdir())
