"""#73 unit 2: a target's foreign core tests (tests that import a manifest module), and the Target-only set."""

from __future__ import annotations

from pathlib import Path

from tools.agentops.core_tests import foreign_core_tests, main, poe_only


def _manifest(tmp_path: Path, lines: list[str]) -> Path:
    path = tmp_path / "core_manifest.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _target(tmp_path: Path, files: dict[str, str], name: str = "target") -> Path:
    folder = tmp_path / name
    tests = folder / "tests"
    tests.mkdir(parents=True)
    for file_name, text in files.items():
        (tests / file_name).write_text(text, encoding="utf-8")
    return folder


MANIFEST = [
    "tools/agentops/core_check.py",
    "tools/agentops/core_manifest.txt",
    "tools/hooks/permission_guard.py",
    "tests/test_listed.py",
]


def test_three_files_only_the_one_importing_an_unlisted_core_module_is_counted(tmp_path):
    manifest = _manifest(tmp_path, MANIFEST + ["tests/test_c.py"])
    folder = _target(tmp_path, {
        "test_a.py": "from tools.agentops.core_check import read_manifest\n",
        "test_b.py": "import os\n",
        "test_c.py": "from tools.agentops.core_check import read_manifest\n",
    })
    assert foreign_core_tests(folder, manifest_path=manifest) == ["test_a.py"]


def test_every_import_form_counts(tmp_path):
    manifest = _manifest(tmp_path, MANIFEST)
    forms = {
        "test_from_package.py": "from tools.agentops import core_check\n",
        "test_from_module.py": "from tools.agentops.core_check import read_manifest\n",
        "test_import_dotted.py": "import tools.agentops.core_check\n",
        "test_hooks_package.py": "from tools.hooks import permission_guard\n",
        "test_hooks_module.py": "from tools.hooks.permission_guard import main\n",
        "test_hooks_dotted_alias.py": "import tools.hooks.permission_guard as guard\n",
        "test_parenthesised.py": "from tools.agentops import (\n    other,\n    core_check,\n)\n",
        "test_inside_function.py": "def test_x():\n    from tools.agentops.core_check import read_manifest\n",
    }
    folder = _target(tmp_path, forms)
    assert foreign_core_tests(folder, manifest_path=manifest) == sorted(forms)


def test_imports_that_are_not_manifest_modules_do_not_count(tmp_path):
    manifest = _manifest(tmp_path, MANIFEST)
    folder = _target(tmp_path, {
        "test_other_module.py": "from tools.agentops.other import x\n",
        "test_other_from_package.py": "from tools.agentops import other\n",
        "test_bare_package.py": "import tools.agentops\n",
        "test_other_hooks.py": "from tools.hooks import transcript\n",
        "test_string_only.py": "name = 'tools.agentops.core_check'\n",
    })
    assert foreign_core_tests(folder, manifest_path=manifest) == []


def test_only_top_level_test_files_are_scanned(tmp_path):
    manifest = _manifest(tmp_path, MANIFEST)
    folder = _target(tmp_path, {
        "test_top.py": "from tools.agentops.core_check import read_manifest\n",
        "helper.py": "from tools.agentops.core_check import read_manifest\n",
    })
    deep = folder / "tests" / "sub"
    deep.mkdir()
    (deep / "test_deep.py").write_text("from tools.agentops.core_check import read_manifest\n", encoding="utf-8")
    assert foreign_core_tests(folder, manifest_path=manifest) == ["test_top.py"]


def test_a_test_file_listed_in_the_manifest_is_not_foreign(tmp_path):
    manifest = _manifest(tmp_path, MANIFEST)
    folder = _target(tmp_path, {
        "test_listed.py": "from tools.agentops.core_check import read_manifest\n",
        "test_free.py": "from tools.agentops.core_check import read_manifest\n",
    })
    assert foreign_core_tests(folder, manifest_path=manifest) == ["test_free.py"]


def test_foreign_only_excludes_a_test_with_a_same_named_home_file(tmp_path):
    manifest = _manifest(tmp_path, MANIFEST)
    folder = _target(tmp_path, {
        "test_a.py": "from tools.agentops.core_check import read_manifest\n",
        "test_b.py": "from tools.agentops.core_check import read_manifest\n",
    })
    home_tests = tmp_path / "home" / "tests"
    home_tests.mkdir(parents=True)
    (home_tests / "test_a.py").write_text("# home copy\n", encoding="utf-8")

    assert foreign_core_tests(folder, manifest_path=manifest) == ["test_a.py", "test_b.py"]
    assert poe_only(folder, home_tests, manifest_path=manifest) == ["test_b.py"]


def test_cli_prints_count_then_names_and_exits_zero(tmp_path, capsys):
    folder = _target(tmp_path, {
        "test_a.py": "from tools.agentops.core_check import read_manifest\n",
        "test_b.py": "import os\n",
    })
    code = main([str(folder), str(tmp_path / "missing")])
    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert out[0] == f"{folder}: a=1 b=0"
    assert out[1] == "a test_a.py"
    assert out[2] == f"{tmp_path / 'missing'}: a=0 b=0"
    assert len(out) == 3
