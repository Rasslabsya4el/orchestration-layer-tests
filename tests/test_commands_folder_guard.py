"""#100: no test reaches the owner's real Claude commands folder, and the session guard (conftest) fails the run when
that folder's files change. This file tests the isolation and the snapshot helpers; it writes nothing into the real folder."""
import os
from pathlib import Path

from tools.agentops import install_folder
from _commands_guard import REAL_COMMANDS, REAL_HOME, changes, snapshot


def test_default_commands_folder_is_not_the_owners_real_one():
    folder = install_folder.commands_dir()
    assert folder != REAL_COMMANDS
    assert REAL_COMMANDS not in folder.parents
    assert REAL_HOME / ".claude" not in folder.parents  # the temp folder may sit under the real home's AppData


def test_snapshot_of_a_missing_folder_is_empty(tmp_path):
    assert snapshot(tmp_path / "nope") == {}


def test_snapshot_sees_added_changed_and_removed_files(tmp_path):
    folder = tmp_path / "commands"
    folder.mkdir()
    (folder / "a.md").write_text("one", encoding="utf-8")
    (folder / "b.md").write_text("two", encoding="utf-8")
    (folder / "d.md").write_text("four", encoding="utf-8")
    before = snapshot(folder)
    (folder / "a.md").write_text("changed", encoding="utf-8")
    (folder / "b.md").unlink()
    (folder / "c.md").write_text("three", encoding="utf-8")
    os.utime(folder / "d.md", ns=(0, 0))  # same bytes, new mtime: still a change
    assert changes(before, snapshot(folder)) == {"added": ["c.md"], "removed": ["b.md"], "changed": ["a.md", "d.md"]}


def test_unchanged_folder_reports_nothing(tmp_path):
    folder = tmp_path / "commands"
    folder.mkdir()
    (folder / "a.md").write_text("one", encoding="utf-8")
    before = snapshot(folder)
    assert changes(before, snapshot(folder)) == {"added": [], "removed": [], "changed": []}
