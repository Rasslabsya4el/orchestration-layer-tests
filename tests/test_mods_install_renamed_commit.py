"""#167 unit 2: under another plugin id the install holds the installer's own output (the quoted source name in
`hooks/` rewritten to the install name), so those bytes are in no commit as they are. A differing install file that is
only the previous committed version, rewritten by the installer, is no hand edit: the check looks for the bytes
rewritten back to the source name too. A real hand edit stays refused.

Temp folders only: the repo and the install are under tmp_path.
"""

import os
import subprocess

import pytest

from tools.agentops import mods

SRC, INST = "x-src", "x-inst"
PROJECT_V1 = b"// the plugin id stays spelled 'x-src' in register.tsx\nexport const MOD_DIR = 'a'\n"
PROJECT_V2 = PROJECT_V1 + b"export const COLLECTOR_FAST = 'b'\n"
REL = "hooks/project.ts"


def _git(repo, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True,
                   capture_output=True, text=True)


def _commit(repo, data):
    p = repo / "mods" / SRC / REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "project.ts " + str(len(data)))


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo, inst_root, home = tmp_path / "repo", tmp_path / "inst", tmp_path / "home"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, PROJECT_V1)
    monkeypatch.setenv("AGENTOPS_MODS_REPO", str(repo))
    monkeypatch.setenv("AGENTOPS_MODS_DIR", str(inst_root))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(mods, "install_name", lambda name: INST if name == SRC else name)
    kw = dict(settings_path=repo / "no-settings.json", commands_dir=repo / "cmds")
    assert mods.run_install(SRC, **kw) == 0  # the installer writes v1 under the install name
    return repo, inst_root / INST, kw


def _later_than_commit(repo, path):
    ts = int(subprocess.run(["git", "log", "-1", "--format=%ct"], cwd=repo, capture_output=True, text=True).stdout) + 3600
    os.utime(path, (ts, ts))


def test_previous_committed_version_installed_under_other_id_is_not_refused(env, capsys):
    repo, inst, kw = env
    assert b"'x-inst'" in (inst / REL).read_bytes()
    _commit(repo, PROJECT_V2)
    _later_than_commit(repo, inst / REL)  # the install was written after the last commit of the file
    assert mods.run_install(SRC, **kw) == 0
    assert "refused" not in capsys.readouterr().out
    assert (inst / REL).read_bytes() == PROJECT_V2.replace(b"'x-src'", b"'x-inst'")


def test_hand_edit_of_hook_file_under_other_id_stays_refused(env, capsys):
    repo, inst, kw = env
    _commit(repo, PROJECT_V2)
    (inst / REL).write_bytes((inst / REL).read_bytes() + b"// hand edit\n")
    _later_than_commit(repo, inst / REL)
    assert mods.run_install(SRC, **kw) == 1
    out = capsys.readouterr().out
    assert "refused" in out and REL in out
    assert b"hand edit" in (inst / REL).read_bytes()
