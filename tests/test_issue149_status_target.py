"""#149 acceptance row 6: `mods status` in a target whose settings name only `monitor.plugin_id` compares the monitor
folder against `<install root>/<plugin_id>`, never against `<install root>/<monitor folder>`.

Temp folders only: the repository copy, the install root and the user's home are all under tmp_path.
"""

import json
from pathlib import Path

import pytest

from tools.agentops import cli, core_check

FOLDER = core_check.MONITOR_LITERAL
PID = "x-monitor"


def _manifest(name: str) -> bytes:
    return json.dumps({"name": name, "version": "0.1.0"}, indent=2).encode("utf-8")


#: the repository copy: its manifest names the monitor folder (the install rewrites that name to the plugin id)
REPO_FILES = {
    ".claude-plugin/plugin.json": _manifest(FOLDER),
    "hooks/project.ts": b"export const TITLE = 'Monitor'\n",
    "hooks/register.tsx": b"export const a = atom({ plugin: 'x-monitor', key: 'a' })\n",
}

#: the install under the plugin id: the same bytes, the manifest name rewritten as `mods install` writes it
PLUGIN_ID_INSTALL = {
    ".claude-plugin/plugin.json": _manifest(PID),
    "hooks/project.ts": REPO_FILES["hooks/project.ts"],
    "hooks/register.tsx": REPO_FILES["hooks/register.tsx"],
}

#: a decoy under the monitor folder's own name: other bytes and one more file, so comparing it would differ
DECOY = {
    ".claude-plugin/plugin.json": _manifest(FOLDER),
    "hooks/project.ts": b"export const TITLE = 'an older build'\n",
    "hooks/register.tsx": b"// a different build\n",
    "hooks/old.tsx": b"export const gone = 1\n",
}


def _file(root: Path, rel: str, data: bytes) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


@pytest.fixture
def target(tmp_path, monkeypatch):
    repo, inst_root, home = tmp_path / "repo", tmp_path / "inst", tmp_path / "home"
    cfg = repo / "config"
    cfg.mkdir(parents=True)
    (cfg / "orchestration.json").write_text(json.dumps({"monitor": {"plugin_id": PID}}), encoding="utf-8")
    for rel, data in REPO_FILES.items():
        _file(repo / "mods" / FOLDER, rel, data)
    for rel, data in PLUGIN_ID_INSTALL.items():
        _file(inst_root / PID, rel, data)
    for rel, data in DECOY.items():
        _file(inst_root / FOLDER, rel, data)
    monkeypatch.setenv("AGENTOPS_MODS_REPO", str(repo))
    monkeypatch.setenv("AGENTOPS_MODS_DIR", str(inst_root))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # every file read goes through Path.read_bytes (compare reads both sides): record the reads
    reads: list[Path] = []
    real_read_bytes = Path.read_bytes

    def spy(self):
        reads.append(self)
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", spy)
    return repo, inst_root, reads


def test_status_compares_the_plugin_id_install_and_never_the_monitor_folder(target, capsys):
    repo, inst_root, reads = target
    assert cli.main(["mods", "status"]) == 0
    out = capsys.readouterr().out
    assert f"{FOLDER} (installed as {PID}): 3 files compared, 0 differ" in out
    # the spy is live: the plugin-id install was read, so the zero is a comparison and not a skip
    assert any(p.is_relative_to(inst_root / PID) for p in reads)
    # the decoy under the monitor folder's own name is never read
    assert not [p for p in reads if p.is_relative_to(inst_root / FOLDER)]


def test_status_counts_a_change_in_the_plugin_id_install(target, capsys):
    repo, inst_root, reads = target
    (inst_root / PID / "hooks" / "register.tsx").write_bytes(b"// edited in the install\n")
    assert cli.main(["mods", "status"]) == 1
    out = capsys.readouterr().out
    assert f"{FOLDER} (installed as {PID}): 3 files compared, 1 differ" in out
    assert "  hooks/register.tsx" in out
