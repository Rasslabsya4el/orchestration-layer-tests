"""#173 unit 4: the test suite never adds or removes a mod folder in the owner's real `~/.claude/mods`.

conftest's autouse fixture points `AGENTOPS_MODS_DIR` at a per-test temp folder, and the session guard fails the run
when the set of top-level mod folder names under the real `~/.claude/mods` changes between its start and its end.
Content under a mod folder is not compared (the live monitors rewrite their caches). This file tests that isolation
and the name-set helper; it writes nothing into the real folder.
"""
from __future__ import annotations

from pathlib import Path

import _commands_guard as guard
from tools.agentops import install_folder, mods

REAL_MODS = guard.REAL_HOME / ".claude" / "mods"
REAL_CLAUDE = guard.REAL_HOME / ".claude"


def _not_under(path: Path, folder: Path) -> bool:
    return folder != path and folder not in path.parents


def test_default_install_root_is_not_under_the_real_claude_folder():
    # pytest's temp folder may sit under the real home (AppData\Local\Temp), so the check is against ~/.claude itself
    root = install_folder.install_root()
    assert _not_under(root, REAL_MODS), f"install root {root} is the owner's real mods folder or inside it"
    assert _not_under(root, REAL_CLAUDE), f"install root {root} is inside the owner's real ~/.claude"


def test_run_install_of_a_fake_monitor_leaves_the_real_mod_folder_names_unchanged(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    hooks = repo / "mods" / "fake-monitor" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "project.ts").write_text(
        "export const COMMAND = 'fake-monitor'\nexport const TITLE = 'Fake'\n", encoding="utf-8")
    (hooks / "register.tsx").write_text("export default {}\n", encoding="utf-8")
    monkeypatch.setenv("AGENTOPS_MODS_REPO", str(repo))

    # fail before any write if the default root is the owner's real folder (red-first: nothing reaches the real one)
    root = install_folder.install_root()
    assert _not_under(root, REAL_CLAUDE), f"install root {root} is inside the owner's real ~/.claude"

    before = guard.mod_folder_names(REAL_MODS)
    rc = mods.run_install("fake-monitor", commands_dir=tmp_path / "commands")
    assert rc == 0
    assert (root / "fake-monitor").is_dir(), "the fake monitor went to the temp install root"
    assert guard.name_changes(before, guard.mod_folder_names(REAL_MODS)) == {"added": [], "removed": []}


def test_helper_reports_an_added_and_a_removed_mod_folder_name(tmp_path: Path):
    fake_real = tmp_path / "real_mods"
    (fake_real / "orch-monitor" / "bin" / ".cache").mkdir(parents=True)
    (fake_real / "pob-slots").mkdir()
    before = guard.mod_folder_names(fake_real)

    (fake_real / "some-repo-monitor" / "hooks").mkdir(parents=True)
    (fake_real / "some-repo-monitor" / "hooks" / "project.ts").write_text("x", encoding="utf-8")
    (fake_real / "pob-slots").rmdir()

    assert guard.name_changes(before, guard.mod_folder_names(fake_real)) == {
        "added": ["some-repo-monitor"], "removed": ["pob-slots"]}


def test_content_under_a_mod_folder_is_not_compared(tmp_path: Path):
    fake_real = tmp_path / "real_mods"
    (fake_real / "orch-monitor" / "bin" / ".cache").mkdir(parents=True)
    before = guard.mod_folder_names(fake_real)

    (fake_real / "orch-monitor" / "bin" / ".cache" / "activity.json").write_text("{\"n\": 2}", encoding="utf-8")
    (fake_real / "orch-monitor" / "bin" / ".cache" / "shared-fast.lock").write_text("", encoding="utf-8")

    assert guard.name_changes(before, guard.mod_folder_names(fake_real)) == {"added": [], "removed": []}


def test_mod_folder_names_of_a_missing_folder_is_empty(tmp_path: Path):
    assert guard.mod_folder_names(tmp_path / "nope") == set()
