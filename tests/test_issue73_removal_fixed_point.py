"""Issue 73 removal fixed point: `core update` removes a target's test copies to a fixed point. A helper test module
(group a, home copy) and the files that import it (`from tests.<helper> import X`) go together, so no kept test is
left importing a removed module; an importer with no home copy stays and keeps its helper with it.
Real git on a tmp target (bare origin + clone); the home is a fake `gh` runner, no network."""

from __future__ import annotations

import ast
from pathlib import Path

from tests import test_issue28_core_update_failed_commit as base
from tests.test_issue73_update_removes_copies import ONLY_CORE, _home, _target, _update
from tools.agentops.core_check import read_manifest
from tools.agentops.core_tests import split_core_tests

HELPER = "from tools.agentops import mod\n\n\ndef helper():\n    return mod\n"
IMPORTER = "from tests.test_helper import helper\nfrom tools.agentops import mod\n\n\ndef test_it():\n    assert helper(), mod\n"
HOME_FILES = ("test_helper.py", "test_a.py", "test_b.py", "test_c.py")


def _missing_tests_imports(root: Path) -> list[str]:
    """``<file> -> tests.<x>`` for every remaining test file that imports a ``tests.<x>`` module that is not there."""
    missing: list[str] = []
    for path in sorted((root / "tests").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and node.module.startswith("tests."):
                name = node.module.split(".")[1]
                if not (root / "tests" / f"{name}.py").is_file():
                    missing.append(f"{path.name} -> tests.{name}")
    return missing


def test_helper_and_its_importers_with_home_copies_all_go_in_one_update(tmp_path: Path) -> None:
    root = _target(tmp_path, {"test_helper.py": HELPER, "test_a.py": IMPORTER, "test_b.py": IMPORTER,
                              "test_c.py": IMPORTER})

    result = _update(root, set(HOME_FILES))

    assert sorted(result.removed) == sorted(f"tests/{name}" for name in HOME_FILES)
    for name in HOME_FILES:
        assert not (root / "tests" / name).exists()
    assert _missing_tests_imports(root) == []
    assert base._git(root, "status", "--porcelain").stdout.strip() == ""


def test_importer_without_a_home_copy_keeps_its_helper(tmp_path: Path) -> None:
    root = _target(tmp_path, {"test_helper.py": HELPER, "test_a.py": IMPORTER, "test_b.py": IMPORTER,
                              "test_c.py": IMPORTER})

    result = _update(root, {"test_helper.py", "test_a.py", "test_b.py"})  # test_c has no home copy

    assert "tests/test_helper.py" not in result.removed
    assert (root / "tests/test_helper.py").is_file()
    assert (root / "tests/test_c.py").is_file()
    assert _missing_tests_imports(root) == []


def test_importer_chain_is_followed_to_the_end(tmp_path: Path) -> None:
    middle = "from tests.test_helper import helper\nfrom tools.agentops import mod\n\n\ndef mid():\n    return helper(), mod\n"
    top = "from tests.test_mid import mid\nfrom tools.agentops import mod\n\n\ndef test_it():\n    assert mid(), mod\n"
    root = _target(tmp_path, {"test_helper.py": HELPER, "test_mid.py": middle, "test_top.py": top})

    result = _update(root, {"test_helper.py", "test_mid.py", "test_top.py"})

    assert sorted(result.removed) == ["tests/test_helper.py", "tests/test_mid.py", "tests/test_top.py"]
    assert _missing_tests_imports(root) == []


def test_split_treats_absent_names_as_not_existing(tmp_path: Path) -> None:
    root = _target(tmp_path, {"test_helper.py": HELPER, "test_a.py": IMPORTER})
    manifest = tmp_path / "manifest.txt"
    manifest.write_text("\n".join([*base.NEW_HOME_FILES, "tools/agentops/mod.py"]) + "\n", encoding="utf-8")
    assert "tools/agentops/mod.py" in read_manifest(manifest)

    assert split_core_tests(root, manifest) == (["test_helper.py"], ["test_a.py"])
    assert split_core_tests(root, manifest, absent={"test_helper.py"}) == (["test_a.py", "test_helper.py"], [])
