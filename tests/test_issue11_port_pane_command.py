"""Issue #11 unit 2 (pane, command, refusal): a target's monitor PANE and COMMAND are its plugin id, not ``monitor``.

The seam is ``port.write_monitor_stubs``: it writes the target's ``hooks/project.ts`` with PANE set to the
target's plugin id (the install folder's last part); COMMAND is pinned in the core hook code (#173), not written there. Before it writes, it refuses (``port.PortError``, nothing written)
when the install folder or the command file belongs to another REPO. The install root is redirected by env
``AGENTOPS_MODS_DIR``, the user's home (command folder) by env ``HOME`` / ``USERPROFILE``, so no real folder is touched.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tools.agentops import install_folder, port
from tools.agentops.core_check import MONITOR_LITERAL

REPO = Path(__file__).resolve().parents[1]
SOURCE = MONITOR_LITERAL
OTHER_REPO = "D:/elsewhere/other-repo"


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A fake install root, a fake home, and this repo's settings naming ``SOURCE`` as the monitor's source."""
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


def _write_installed(folder: Path, repo_value: str) -> None:
    (folder / "hooks").mkdir(parents=True)
    (folder / "hooks" / "project.ts").write_text(f"export const REPO = '{repo_value}'\n", encoding="utf-8")


def _written_project(target: Path, repo: str) -> str:
    port.write_monitor_stubs(target, repo, home_root=REPO)
    return (target / port.MONITOR_DIR / "hooks" / "project.ts").read_text(encoding="utf-8")


def _const(text: str, name: str) -> str:
    match = re.search(rf"^export const {name} = '([^']*)'", text, re.MULTILINE)
    assert match, f"no {name} line in the written project.ts:\n{text[:400]}"
    return match.group(1)


def test_pane_is_the_targets_own_plugin_id_and_command_is_pinned_in_core(machine, tmp_path):
    # #173: the command is `monitor` in every repo, pinned in the core hook code: the port writes no COMMAND line.
    target = _target(tmp_path / "targets", "my-repo-v2-checkout")
    text = _written_project(target, "X/My Repo.v2")
    assert _const(text, "PANE") == "my-repo-v2-monitor"
    assert "monitor" != _const(text, "PANE")
    assert "export const COMMAND" not in text


def test_two_targets_get_different_panes_and_the_same_command(machine, tmp_path):
    first = _target(tmp_path / "targets", "alpha-checkout")
    second = _target(tmp_path / "targets", "beta-checkout")
    first_text = _written_project(first, "owner/alpha-repo")
    second_text = _written_project(second, "owner/beta-repo")
    assert _const(first_text, "PANE") == "alpha-repo-monitor"
    assert _const(second_text, "PANE") == "beta-repo-monitor"
    assert "export const COMMAND" not in first_text and "export const COMMAND" not in second_text


def test_installed_folder_of_another_repo_is_refused_and_nothing_is_written(machine, tmp_path):
    _write_installed(machine / "my-repo-v2-monitor", OTHER_REPO)
    target = _target(tmp_path / "targets", "my-repo-v2-checkout")
    with pytest.raises(port.PortError) as err:
        port.write_monitor_stubs(target, "X/My Repo.v2", home_root=REPO)
    msg = str(err.value)
    assert "my-repo-v2-monitor" in msg and OTHER_REPO in msg, msg
    assert not (target / port.MONITOR_DIR).exists(), "a refused port wrote files into the target"


def test_installed_folder_of_the_same_repo_passes(machine, tmp_path):
    target = _target(tmp_path / "targets", "my-repo-v2-checkout")
    _write_installed(machine / "my-repo-v2-monitor", target.resolve().as_posix())
    text = _written_project(target, "X/My Repo.v2")
    assert _const(text, "PANE") == "my-repo-v2-monitor"


def test_monitor_md_naming_a_folder_of_another_repo_is_not_a_refusal(machine, tmp_path, monkeypatch):
    # #173: the command file is never a claim; one `monitor.md` for every repo, so a port of this target goes on
    home = Path(machine.parent / "home")
    commands = home / ".claude" / "commands"
    commands.mkdir(parents=True)
    monkeypatch.setattr(install_folder, "commands_dir", lambda: commands)
    (commands / "monitor.md").write_text(
        f"Open the monitor pane. (Answered by the {SOURCE} mod's command.run hook, "
        f"C:\\Users\\user\\.claude\\mods\\{SOURCE}; this file only lists the monitor command.)\n", encoding="utf-8")
    _write_installed(machine / SOURCE, OTHER_REPO)
    target = _target(tmp_path / "targets", "my-repo-v2-checkout")
    text = _written_project(target, "X/My Repo.v2")
    assert _const(text, "PANE") == "my-repo-v2-monitor"
