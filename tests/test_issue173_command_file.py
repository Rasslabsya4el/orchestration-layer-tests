"""#173 unit 3: one command file, `<commands>/monitor.md`, owned by no repo.

The seam is ``mods.run_install`` (the install side of every monitor, with its commands folder passed in) and
``install_folder.command_step`` through it. Install root, home and the repo copy are redirected by env, so no real
folder is touched. Row 3: an install with a ``monitor.md`` written from the old stub for another repo is rc 0 and
leaves one ``monitor.md``; a fresh install writes it; a second install from another repo changes nothing; the old
per-repo command files of the old shape are removed; a user's own ``.md`` stays.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import install_folder, mods

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "config" / "core_templates" / "monitor_command.md.stub"
OTHER_REPO = "D:/elsewhere/other-repo"


def _old_stub(cmd: str, title: str, mod_dir: str, src: str | None) -> str:
    """The OLD stub's rendering (``git show HEAD:config/core_templates/monitor_command.md.stub``, filled)."""
    who = f"the {src} mod's" if src else "the mod's"
    return ("---\n"
            f"description: Open the {title} monitor pane (machine, runs, tickets)\n"
            "disable-model-invocation: true\n"
            "---\n"
            f"Open the {title} monitor pane. (Answered by {who} command.run hook, {mod_dir}; "
            f"this file only lists /{cmd} in the desktop composer, which reads its command list at startup.)\n")


def _snapshot(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir()) if p.is_file()} if folder.is_dir() else {}


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()
    home = tmp_path / "home"
    commands = home / ".claude" / "commands"
    commands.mkdir(parents=True)
    monkeypatch.setenv("AGENTOPS_MODS_DIR", str(mods_dir))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return SimpleNamespace(mods=mods_dir, commands=commands, settings=tmp_path / "settings.json", tmp=tmp_path)


def _repo_copy(machine, root: Path, name: str, repo_value: str) -> Path:
    """A repository root holding one monitor copy `mods/<name>` whose project.ts names `repo_value` as its REPO."""
    hooks = root / "mods" / name / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "project.ts").write_text(
        f"export const REPO = '{repo_value}'\n"
        f"export const TITLE = 'Own'\n"
        f"export const MOD_DIR = '{(machine.mods / name).as_posix()}'\n", encoding="utf-8")
    return root


def _install(monkeypatch, machine, repo_root: Path, name: str) -> int:
    monkeypatch.setenv("AGENTOPS_MODS_REPO", str(repo_root))
    return mods.run_install(name, settings_path=machine.settings, commands_dir=machine.commands)


def _other_repo_pob_folder(machine) -> Path:
    folder = machine.mods / "pob-slots"
    (folder / "hooks").mkdir(parents=True)
    (folder / "hooks" / "project.ts").write_text(f"export const REPO = '{OTHER_REPO}'\n", encoding="utf-8")
    return folder


def test_fresh_install_writes_the_one_monitor_command_file_from_the_core_stub(machine, monkeypatch, capsys):
    repo = _repo_copy(machine, machine.tmp / "this-repo", "orch-monitor", (machine.tmp / "this-repo").resolve().as_posix())

    rc = _install(monkeypatch, machine, repo, "orch-monitor")

    assert rc == 0, capsys.readouterr().out
    assert sorted(p.name for p in machine.commands.glob("*.md")) == ["monitor.md"]
    text = (machine.commands / "monitor.md").read_text(encoding="utf-8")
    assert text == STUB.read_text(encoding="utf-8")
    assert "description:" in text and "disable-model-invocation: true" in text
    # the body names no repo and no mod folder
    assert "mods" not in text and "{" not in text and "orch" not in text.lower()


def test_monitor_md_of_another_repo_is_not_a_refusal_and_is_replaced(machine, monkeypatch, capsys):
    pob = _other_repo_pob_folder(machine)
    old = _old_stub("monitor", "Junkroan", pob.as_posix(), src="pob-slots")
    (machine.commands / "monitor.md").write_text(old, encoding="utf-8")
    repo = _repo_copy(machine, machine.tmp / "this-repo", "orch-monitor", (machine.tmp / "this-repo").resolve().as_posix())

    rc = _install(monkeypatch, machine, repo, "orch-monitor")

    assert rc == 0, capsys.readouterr().out
    assert sorted(p.name for p in machine.commands.glob("*.md")) == ["monitor.md"]
    assert (machine.commands / "monitor.md").read_text(encoding="utf-8") == STUB.read_text(encoding="utf-8")
    backups = list(machine.commands.glob("monitor.md.before-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == old


def test_second_install_from_another_repo_changes_nothing(machine, monkeypatch, capsys):
    first = _repo_copy(machine, machine.tmp / "repo-a", "orch-monitor", (machine.tmp / "repo-a").resolve().as_posix())
    second = _repo_copy(machine, machine.tmp / "repo-b", "zorblax-monitor", (machine.tmp / "repo-b").resolve().as_posix())
    assert _install(monkeypatch, machine, first, "orch-monitor") == 0
    before = _snapshot(machine.commands)

    rc = _install(monkeypatch, machine, second, "zorblax-monitor")

    assert rc == 0, capsys.readouterr().out
    assert _snapshot(machine.commands) == before
    assert not list(machine.commands.glob("monitor.md.before-*")), "an equal monitor.md was backed up"
    assert sorted(p.name for p in machine.commands.glob("*.md")) == ["monitor.md"]


def test_old_per_repo_command_files_are_removed_and_a_users_own_file_stays(machine, monkeypatch, capsys):
    pob_dir = "C:\\Users\\user\\.claude\\mods\\pob-slots"
    (machine.commands / "pob.md").write_text(_old_stub("pob", "Junkroan", pob_dir, "pob-slots"), encoding="utf-8")
    (machine.commands / "orch.md").write_text(
        _old_stub("orch", "orchestration-layer", "C:\\Users\\user\\.claude\\mods\\orch-monitor", "orch-monitor"),
        encoding="utf-8")
    (machine.commands / "hermes-finances-agent-monitor.md").write_text(
        _old_stub("hermes-finances-agent-monitor", "Hermes-finances-agent", "C:/Users/user/.claude/mods/hermes", None),
        encoding="utf-8")
    own = "my own command, not the monitor's\n"
    (machine.commands / "foo.md").write_text(own, encoding="utf-8")
    repo = _repo_copy(machine, machine.tmp / "this-repo", "orch-monitor", (machine.tmp / "this-repo").resolve().as_posix())

    rc = _install(monkeypatch, machine, repo, "orch-monitor")

    assert rc == 0, capsys.readouterr().out
    assert sorted(p.name for p in machine.commands.glob("*.md")) == ["foo.md", "monitor.md"]
    assert (machine.commands / "foo.md").read_text(encoding="utf-8") == own


def test_a_users_own_monitor_md_is_kept_and_not_refused(machine, monkeypatch, capsys):
    own = "my own monitor command\n"
    (machine.commands / "monitor.md").write_text(own, encoding="utf-8")
    repo = _repo_copy(machine, machine.tmp / "this-repo", "orch-monitor", (machine.tmp / "this-repo").resolve().as_posix())

    rc = _install(monkeypatch, machine, repo, "orch-monitor")

    assert rc == 0, capsys.readouterr().out
    assert (machine.commands / "monitor.md").read_text(encoding="utf-8") == own


def test_a_mod_without_project_ts_writes_no_command_file(machine, monkeypatch, capsys):
    root = machine.tmp / "plain-repo"
    (root / "mods" / "plain-mod" / "hooks").mkdir(parents=True)
    (root / "mods" / "plain-mod" / "hooks" / "register.tsx").write_text("export default {}\n", encoding="utf-8")

    rc = _install(monkeypatch, machine, root, "plain-mod")

    assert rc == 0, capsys.readouterr().out
    assert list(machine.commands.glob("*.md")) == []
