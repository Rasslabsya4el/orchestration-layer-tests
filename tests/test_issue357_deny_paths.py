"""Issue #357 unit 5: a replay run tree denies the real checkouts.

``_disable_memory(tree, case_repo, harness_root)`` writes the tree's ``.claude/settings.local.json`` with
``autoMemoryEnabled`` false and ``permissions.deny`` rules for every real checkout folder: the case's repo folder and
each ``targets`` folder of config/orchestration.json, each in three spellings (as given, backslashes, git-bash), as a
``Bash`` pattern, and as the five file-tool path rules. Without them a run's helper subagents cd into the real checkout
and read its files and git log. The config is read at run time, so a missing one raises: no run starts without it.

The folders here are made-up names; no test or tool names a real repo folder.
"""

import json
import re
from pathlib import Path

import pytest

from tools.replays.launcher import _disable_memory

CASE_REPO = "C:/work/alpha"
TARGET_FOLDERS = ["C:/work/alpha", "D:/other/beta"]
TREE_NAME = "r0123abcd"


def _harness(tmp_path: Path, folders=TARGET_FOLDERS) -> Path:
    harness = tmp_path / "harness"
    (harness / "config").mkdir(parents=True)
    targets = [{"repo": f"Owner/{Path(folder).name}", "folder": folder} for folder in folders]
    (harness / "config" / "orchestration.json").write_text(json.dumps({"targets": targets}), encoding="utf-8")
    return harness


def _tree(tmp_path: Path) -> Path:
    tree = tmp_path / "trees" / TREE_NAME
    tree.mkdir(parents=True)
    return tree


def _settings(tree: Path) -> dict:
    return json.loads((tree / ".claude" / "settings.local.json").read_text(encoding="utf-8"))


def _alpha_rules() -> set[str]:
    # the expected literals, typed out by hand (not built by the code under test)
    return {
        "Bash(*C:/work/alpha*)", "Bash(*C:\\work\\alpha*)", "Bash(*/c/work/alpha*)",
        "Read(C:/work/alpha/**)", "Edit(C:/work/alpha/**)", "Write(C:/work/alpha/**)",
        "Glob(C:/work/alpha/**)", "Grep(C:/work/alpha/**)",
    }


def _beta_rules() -> set[str]:
    return {
        "Bash(*D:/other/beta*)", "Bash(*D:\\other\\beta*)", "Bash(*/d/other/beta*)",
        "Read(D:/other/beta/**)", "Edit(D:/other/beta/**)", "Write(D:/other/beta/**)",
        "Glob(D:/other/beta/**)", "Grep(D:/other/beta/**)",
    }


def test_each_target_folder_gets_three_bash_spellings_and_five_path_rules(tmp_path):
    tree = _tree(tmp_path)
    _disable_memory(tree, CASE_REPO, _harness(tmp_path))
    deny = set(_settings(tree)["permissions"]["deny"])
    assert _alpha_rules() <= deny
    assert _beta_rules() <= deny


def test_the_case_repo_folder_is_denied_even_when_no_target_lists_it(tmp_path):
    tree = _tree(tmp_path)
    _disable_memory(tree, "E:/case/gamma", _harness(tmp_path, folders=["D:/other/beta"]))
    deny = set(_settings(tree)["permissions"]["deny"])
    assert "Bash(*E:/case/gamma*)" in deny
    assert "Read(E:/case/gamma/**)" in deny
    assert "Bash(*D:/other/beta*)" in deny


def test_an_existing_deny_entry_and_other_keys_are_kept(tmp_path):
    tree = _tree(tmp_path)
    (tree / ".claude").mkdir()
    (tree / ".claude" / "settings.local.json").write_text(json.dumps({
        "other": 1,
        "permissions": {"allow": ["Read(docs/**)"], "deny": ["Bash(git push --force*)"]},
    }), encoding="utf-8")
    _disable_memory(tree, CASE_REPO, _harness(tmp_path))
    settings = _settings(tree)
    assert settings["other"] == 1
    assert settings["autoMemoryEnabled"] is False
    assert settings["permissions"]["allow"] == ["Read(docs/**)"]
    assert settings["permissions"]["deny"][0] == "Bash(git push --force*)"
    assert _alpha_rules() <= set(settings["permissions"]["deny"])


def test_nothing_is_denied_under_the_tree_or_the_replay_trees_folder(tmp_path):
    tree = _tree(tmp_path)
    _disable_memory(tree, CASE_REPO, _harness(tmp_path))
    deny = _settings(tree)["permissions"]["deny"]
    assert not any(TREE_NAME in rule or "_replay_trees" in rule for rule in deny)


def test_a_missing_config_raises_naming_its_path_and_writes_nothing(tmp_path):
    tree = _tree(tmp_path)
    harness = tmp_path / "harness"  # no config folder at all
    config = harness / "config" / "orchestration.json"
    with pytest.raises(ValueError, match=re.escape(str(config))):
        _disable_memory(tree, CASE_REPO, harness)
    assert not (tree / ".claude" / "settings.local.json").exists()
