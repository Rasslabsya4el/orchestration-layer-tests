"""#73 unit 1: the foreign core tests of a target split into a (core only) and b (also touches the target's own)."""

from __future__ import annotations

from pathlib import Path

from tools.agentops.core_tests import foreign_core_tests, main, split_core_tests


def _manifest(tmp_path: Path, lines: list[str]) -> Path:
    path = tmp_path / "core_manifest.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write(folder: Path, rel: str, text: str) -> None:
    path = folder / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _target(tmp_path: Path, files: dict[str, str], name: str = "target") -> Path:
    folder = tmp_path / name
    for file_name, text in files.items():
        _write(folder, f"tests/{file_name}", text)
    return folder


CORE = "from tools.agentops.core_check import read_manifest\n"

MANIFEST = [
    "tools/agentops/core_check.py",
    "tools/agentops/core_manifest.txt",
    "tools/hooks/permission_guard.py",
    "tests/test_listed.py",
]


def test_a_file_touching_only_manifest_modules_is_a(tmp_path):
    manifest = _manifest(tmp_path, MANIFEST)
    folder = _target(tmp_path, {"test_a.py": CORE})
    a, b = split_core_tests(folder, manifest_path=manifest)
    assert isinstance(a, list) and isinstance(b, list)
    assert a == ["test_a.py"]
    assert b == []


def test_an_own_module_import_makes_b(tmp_path):
    manifest = _manifest(tmp_path, MANIFEST)
    folder = _target(tmp_path, {
        "test_own_top.py": CORE + "from ownmod import engine\n",
        "test_own_tools.py": CORE + "import tools.full_run\n",
        "test_only_core.py": CORE,
    })
    _write(folder, "ownmod.py", "engine = 1\n")
    _write(folder, "tools/full_run.py", "def run():\n    return 1\n")
    a, b = split_core_tests(folder, manifest_path=manifest)
    assert b == ["test_own_tools.py", "test_own_top.py"]
    assert a == ["test_only_core.py"]


def test_a_helper_imported_from_the_tests_dir_makes_b(tmp_path):
    manifest = _manifest(tmp_path, MANIFEST)
    folder = _target(tmp_path, {
        "test_bare.py": CORE + "from _helper import make\n",
        "test_pkg.py": CORE + "from tests import shared\n",
        "test_plain.py": CORE,
    })
    _write(folder, "tests/_helper.py", "def make():\n    return 1\n")
    _write(folder, "tests/shared.py", "X = 1\n")
    a, b = split_core_tests(folder, manifest_path=manifest)
    assert b == ["test_bare.py", "test_pkg.py"]
    assert a == ["test_plain.py"]


def test_a_string_naming_an_own_config_file_makes_b(tmp_path):
    manifest = _manifest(tmp_path, MANIFEST)
    folder = _target(tmp_path, {
        "test_cfg.py": CORE + 'PATH = "config/core_leak_patterns.json"\n',
        "test_missing_cfg.py": CORE + 'PATH = "config/not_there.json"\n',
    })
    _write(folder, "config/core_leak_patterns.json", "{}\n")
    a, b = split_core_tests(folder, manifest_path=manifest)
    assert b == ["test_cfg.py"]
    assert a == ["test_missing_cfg.py"]


def test_a_string_naming_a_manifest_file_or_another_test_stays_a(tmp_path):
    manifest = _manifest(tmp_path, MANIFEST)
    folder = _target(tmp_path, {
        "test_manifest_string.py": CORE + 'SRC = "tools/agentops/core_check.py"\n',
        "test_other_test_string.py": CORE + 'OTHER = "tests/test_other.py"\n',
        "test_other.py": "X = 1\n",
    })
    _write(folder, "tools/agentops/core_check.py", "def read_manifest():\n    return []\n")
    a, b = split_core_tests(folder, manifest_path=manifest)
    assert a == ["test_manifest_string.py", "test_other_test_string.py"]
    assert b == []


def test_a_file_that_does_not_parse_is_skipped(tmp_path):
    manifest = _manifest(tmp_path, MANIFEST)
    folder = _target(tmp_path, {
        "test_broken.py": CORE + "def (:\n",
        "test_ok.py": CORE,
    })
    a, b = split_core_tests(folder, manifest_path=manifest)
    assert a == ["test_ok.py"]
    assert b == []


def test_a_and_b_together_are_the_foreign_core_tests(tmp_path):
    manifest = _manifest(tmp_path, MANIFEST)
    folder = _target(tmp_path, {
        "test_a1.py": CORE,
        "test_b1.py": CORE + "from ownmod import engine\n",
        "test_b2.py": CORE + "from _helper import make\n",
        "test_not_core.py": "import os\n",
    })
    _write(folder, "ownmod.py", "engine = 1\n")
    _write(folder, "tests/_helper.py", "def make():\n    return 1\n")
    a, b = split_core_tests(folder, manifest_path=manifest)
    assert a + b == foreign_core_tests(folder, manifest_path=manifest)
    assert a == sorted(a) and b == sorted(b)


def test_an_import_of_the_src_or_scripts_root_makes_b(tmp_path):
    manifest = _manifest(tmp_path, MANIFEST)
    folder = _target(tmp_path, {
        "test_src_pkg.py": CORE + "import ownpkg.mod\n",
        "test_scripts_tool.py": CORE + "import helper_tool\n",
        "test_plain.py": CORE,
    })
    _write(folder, "src/ownpkg/mod.py", "X = 1\n")
    _write(folder, "scripts/helper_tool.py", "Y = 1\n")
    a, b = split_core_tests(folder, manifest_path=manifest)
    assert b == ["test_scripts_tool.py", "test_src_pkg.py"]
    assert a == ["test_plain.py"]


def test_a_manifest_line_under_src_stays_core_even_when_it_resolves(tmp_path):
    manifest = _manifest(tmp_path, MANIFEST + ["src/ownpkg/mod.py"])
    folder = _target(tmp_path, {
        "test_listed_src.py": CORE + "import ownpkg.mod\n",
    })
    _write(folder, "src/ownpkg/mod.py", "X = 1\n")
    a, b = split_core_tests(folder, manifest_path=manifest)
    assert a == ["test_listed_src.py"]
    assert b == []


def test_cli_prints_the_split_format_per_folder(tmp_path, capsys):
    folder = _target(tmp_path, {
        "test_a.py": CORE,
        "test_b.py": CORE + "from ownmod import engine\n",
        "test_not_core.py": "import os\n",
    })
    _write(folder, "ownmod.py", "engine = 1\n")
    code = main([str(folder), str(tmp_path / "missing")])
    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert out == [
        f"{folder}: a=1 b=1",
        "a test_a.py",
        "b test_b.py",
        f"{tmp_path / 'missing'}: a=0 b=0",
    ]
