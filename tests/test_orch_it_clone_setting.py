"""#415 unit 1: the optional setting `orch_it_clone` and the machine-free wording of the rule files.

Seams: `core_settings.orch_it_clone()` (the default and an override, read through a settings file) and the three
rule files this unit edits (no drive letter, no user folder, no repo root path in their text).
"""

from __future__ import annotations

import json
import re
import subprocess

from tools.agentops import core_settings
from tools.agentops.worktree import main_checkout_root

CHANGED_DOCS = ("docs/agents/brain.md", "docs/agents/brain-reference.md", "docs/agents/orch.md")

#: a drive-letter path (C:\ or C:/) or a Windows user folder (Users\<name>, Users/<name>)
MACHINE_PATH = re.compile(r"[A-Za-z]:[\\/]|Users[\\/]")


def _settings(tmp_path, extra: dict | None = None):
    data = {k: "x" for k in core_settings.REQUIRED_KEYS}
    data["local_modules"] = []
    data.update(extra or {})
    path = tmp_path / "orchestration.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_default_clone_is_the_sibling_of_the_main_checkout(tmp_path):
    settings = _settings(tmp_path)
    clone = core_settings.orch_it_clone(settings)
    expected = (main_checkout_root(core_settings.ROOT).parent / "orch-it-orch").resolve()
    assert clone == expected
    assert clone.is_absolute()


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def test_from_a_worktree_the_clone_is_beside_the_main_checkout(tmp_path, monkeypatch):
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    _git(main, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "init")
    worktree = (tmp_path / "wts" / "wt").resolve()
    _git(main, "worktree", "add", "-q", "-b", "wt-branch", str(worktree))
    monkeypatch.setattr(core_settings, "ROOT", worktree)
    clone = core_settings.orch_it_clone(_settings(tmp_path))
    assert clone == (tmp_path / "orch-it-orch").resolve()
    assert worktree not in clone.parents


def test_default_is_the_relative_text_stored_in_defaults():
    assert core_settings.DEFAULTS["orch_it_clone"] == "../orch-it-orch"


def test_relative_override_in_settings_wins(tmp_path):
    settings = _settings(tmp_path, {"orch_it_clone": "../elsewhere-clone"})
    clone = core_settings.orch_it_clone(settings)
    assert clone == (main_checkout_root(core_settings.ROOT) / "../elsewhere-clone").resolve()
    assert clone != (main_checkout_root(core_settings.ROOT).parent / "orch-it-orch").resolve()


def test_absolute_override_in_settings_wins(tmp_path):
    target = (tmp_path / "abs-clone").resolve()
    settings = _settings(tmp_path, {"orch_it_clone": str(target)})
    assert core_settings.orch_it_clone(settings) == target


def test_changed_docs_hold_no_machine_path_or_repo_root_string():
    root_forms = {str(core_settings.ROOT), core_settings.ROOT.as_posix()}
    found = []
    for rel in CHANGED_DOCS:
        text = (core_settings.ROOT / rel).read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if MACHINE_PATH.search(line) or any(form in line for form in root_forms):
                found.append(f"{rel}:{number}")
    assert not found, "machine path in rule text (move it to a setting or a run-time name): " + ", ".join(found)
