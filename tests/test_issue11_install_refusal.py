"""Issue #11: `mods.run_install` refuses, changing nothing, when its install folder belongs to another REPO (Rule 2).
#173: the command file is never a claim; `monitor.md` naming another repo's mod is not a refusal.

The seam is ``mods.run_install``. Its install root is redirected by env ``AGENTOPS_MODS_DIR``, its repo copies by
``AGENTOPS_MODS_REPO``, its home (the command folder) by ``HOME`` / ``USERPROFILE``, and its settings file and command
folder are passed in, so no real folder is touched.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import install_folder, mods

NAME = "orch-monitor"
COMMAND = "monitor"
TITLE = "Orch"
OTHER_REPO = "D:/elsewhere/other-repo"
STUB_TEXT = install_folder.COMMAND_STUB.read_text(encoding="utf-8")


def _project(repo_value: str, title: str = TITLE, mod_dir: str = "") -> str:
    return (f"export const REPO = '{repo_value}'\n"
            f"export const TITLE = '{title}'\n"
            f"export const MOD_DIR = '{mod_dir}'\n")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _snapshot(root: Path) -> dict[str, bytes | None]:
    """Every folder (None) and file (its bytes) under `root`; {} when `root` is absent."""
    if not root.exists():
        return {}
    out: dict[str, bytes | None] = {}
    for p in sorted(root.rglob("*")):
        out[p.relative_to(root).as_posix()] = p.read_bytes() if p.is_file() else None
    return out


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fake install root, a fake home with an empty command folder, and this repo's own copy of the mod
    (`mods/orch-monitor/hooks/project.ts`, REPO = this repo's path)."""
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()
    home = tmp_path / "home"
    commands = home / ".claude" / "commands"
    commands.mkdir(parents=True)
    repo = tmp_path / "this-repo"
    repo.mkdir()
    own_repo = repo.resolve().as_posix()
    own_mod_dir = (mods_dir / NAME).as_posix()
    _write(repo / "mods" / NAME / "hooks" / "project.ts", _project(own_repo, mod_dir=own_mod_dir))
    monkeypatch.setenv("AGENTOPS_MODS_DIR", str(mods_dir))
    monkeypatch.setenv("AGENTOPS_MODS_REPO", str(repo))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return SimpleNamespace(mods=mods_dir, commands=commands, own_repo=own_repo, own_mod_dir=own_mod_dir,
                           settings=tmp_path / "settings.json", repo_project=repo / "mods" / NAME / "hooks" / "project.ts")


def _install(machine, **kw) -> int:
    return mods.run_install(NAME, settings_path=machine.settings, commands_dir=machine.commands, **kw)


def test_folder_held_by_another_repo_is_refused_and_nothing_changes(machine, capsys):
    folder = machine.mods / NAME
    _write(folder / "hooks" / "project.ts", _project(OTHER_REPO, mod_dir=folder.as_posix()))
    _write(folder / "hooks" / "other.ts", "export const other = 1\n")
    before_folder, before_commands = _snapshot(folder), _snapshot(machine.commands)

    rc = _install(machine)

    out = capsys.readouterr().out
    assert rc != 0, out
    assert str(folder) in out and OTHER_REPO in out, out
    assert _snapshot(folder) == before_folder, "a refused install changed its folder"
    assert _snapshot(machine.commands) == before_commands, "a refused install wrote the command folder"
    assert not machine.settings.exists(), "a refused install wrote the settings file"


def test_monitor_md_naming_another_repos_mod_is_not_a_refusal(machine, capsys):
    other = machine.mods / "pob-slots"
    _write(other / "hooks" / "project.ts",
           _project(OTHER_REPO, title="Junkroan", mod_dir=other.as_posix()))
    text_before = f"Open the Junkroan pane (answered by {other.as_posix()}).\n"
    _write(machine.commands / f"{COMMAND}.md", text_before)

    rc = _install(machine)

    assert rc == 0, capsys.readouterr().out
    assert (machine.mods / NAME / "hooks" / "project.ts").read_bytes() == machine.repo_project.read_bytes()
    assert (machine.commands / f"{COMMAND}.md").read_text(encoding="utf-8") == text_before, \
        "a monitor.md of other text is the owner's and stays"


def test_same_repo_folder_and_command_install_as_before(machine, capsys):
    folder = machine.mods / NAME
    _write(folder / "hooks" / "project.ts", _project(machine.own_repo, title="Old", mod_dir=folder.as_posix()))
    other = machine.mods / "pob-slots"
    _write(other / "hooks" / "project.ts",
           _project(machine.own_repo, title="Old", mod_dir=other.as_posix()))
    _write(machine.commands / f"{COMMAND}.md", f"Open the pane (answered by {other.as_posix()}).\n")

    rc = _install(machine, force=True)

    assert rc == 0, capsys.readouterr().out
    assert (folder / "hooks" / "project.ts").read_bytes() == machine.repo_project.read_bytes()


def test_nothing_there_installs_and_writes_the_command_file(machine, capsys):
    rc = _install(machine)

    assert rc == 0, capsys.readouterr().out
    assert (machine.mods / NAME / "hooks" / "project.ts").read_bytes() == machine.repo_project.read_bytes()
    command = (machine.commands / f"{COMMAND}.md").read_text(encoding="utf-8")
    assert command == STUB_TEXT and TITLE not in command and machine.own_mod_dir not in command
