"""#11, #173: `mods.command_step` writes the one command file `<commands>/monitor.md` from the core stub
`config/core_templates/monitor_command.md.stub`, for a monitor mod (its `hooks/project.ts` exists), whatever its
project.ts says about a command. It removes the old per-repo command files of the old shape and never touches
another command file: a decoy `pob.md` of other text must change nothing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tools.agentops import mods

ROOT = Path(__file__).resolve().parents[1]
STUB_TEXT = (ROOT / "config" / "core_templates" / "monitor_command.md.stub").read_text(encoding="utf-8")

DECOY_TEXT = "my own pob command, not the monitor's\n"


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """This repo's monitor copy (`mods/orch-monitor/hooks/project.ts`, with a COMMAND line that is no longer read) and
    an empty commands folder."""
    repo_dir = tmp_path / "repo" / "mods" / "orch-monitor"
    (repo_dir / "hooks").mkdir(parents=True)
    (repo_dir / "hooks" / "project.ts").write_text(
        "export const COMMAND = 'pob'\nexport const TITLE = 'Orch'\nexport const MOD_DIR = 'C:/x/orch-monitor'\n",
        encoding="utf-8")
    commands = tmp_path / "commands"
    commands.mkdir()
    monkeypatch.setenv("AGENTOPS_MODS_DIR", str(tmp_path / "mods"))
    return repo_dir, commands


def test_written_file_is_the_core_stub_and_names_no_repo_and_no_other_command(layout):
    repo_dir, commands = layout

    line = mods.command_step(repo_dir, commands)

    assert line is not None and line.startswith("command file written")
    written = (commands / "monitor.md").read_text(encoding="utf-8")
    assert written == STUB_TEXT
    assert sorted(p.name for p in commands.iterdir()) == ["monitor.md"]  # the COMMAND line of project.ts is ignored
    assert "Orch" not in written and "Junkroan" not in written and "pob-slots" not in written


def test_decoy_command_file_of_other_text_is_never_read_or_changed(layout):
    repo_dir, commands = layout
    (commands / "pob.md").write_text(DECOY_TEXT, encoding="utf-8")

    mods.command_step(repo_dir, commands)

    assert (commands / "pob.md").read_text(encoding="utf-8") == DECOY_TEXT
    assert (commands / "monitor.md").read_text(encoding="utf-8") == STUB_TEXT


def test_old_shape_command_file_of_a_per_repo_monitor_is_removed(layout):
    repo_dir, commands = layout
    old = ("---\ndescription: Open the Junkroan monitor pane (machine, runs, tickets)\n"
           "disable-model-invocation: true\n---\n"
           "Open the Junkroan monitor pane. (Answered by the pob-slots mod's command.run hook, "
           "C:\\Users\\user\\.claude\\mods\\pob-slots; this file only lists /pob in the desktop composer, "
           "which reads its command list at startup.)\n")
    (commands / "pob.md").write_text(old, encoding="utf-8")

    line = mods.command_step(repo_dir, commands)

    assert line is not None and "removed the old command files pob.md" in line
    assert not (commands / "pob.md").exists()
    assert (commands / "monitor.md").read_text(encoding="utf-8") == STUB_TEXT


def test_monitor_md_already_holding_the_stub_is_left_alone(layout):
    repo_dir, commands = layout
    (commands / "monitor.md").write_text(STUB_TEXT, encoding="utf-8")

    line = mods.command_step(repo_dir, commands)

    assert line is None
    assert (commands / "monitor.md").read_text(encoding="utf-8") == STUB_TEXT
    assert not list(commands.glob("monitor.md.before-*")), "an equal monitor.md was backed up"


def test_existing_monitor_md_of_the_owners_own_text_is_left_alone(layout):
    repo_dir, commands = layout
    (commands / "monitor.md").write_text(DECOY_TEXT, encoding="utf-8")

    line = mods.command_step(repo_dir, commands)

    assert line is not None and line.startswith("command file kept")
    assert (commands / "monitor.md").read_text(encoding="utf-8") == DECOY_TEXT
