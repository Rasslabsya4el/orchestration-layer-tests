"""Issue #103: the leak check's pattern I sees a bare absolute import of a sibling module (``import shell_words``
in ``tools/hooks/x.py`` where ``tools/hooks/shell_words.py`` exists), and a folder a file adds to ``sys.path``
with a static ``Path(__file__)``-relative expression. Planted temp trees only: this test never reads the repo's
own manifest or its core files."""

from __future__ import annotations

from functools import partial

import pytest

from tools.agentops import core_check
from tools.agentops.core_check import main, run_check


def _write_tree(root, files):
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _write_manifest(tmp_path, entries, name="m.txt"):
    manifest = tmp_path / name
    manifest.write_text("\n".join(entries) + "\n", encoding="utf-8")
    return manifest


def _check(tmp_path, root, manifest_entries):
    """run_check over a planted root; no settings file, so only I and H are counted."""
    manifest = _write_manifest(tmp_path, manifest_entries, name=f"m_{root.name}.txt")
    return run_check(root=root, manifest_path=manifest, patterns_path=tmp_path / "absent.json")


def _one_row(tmp_path, files, manifest_entries, subject=None):
    """The row of ``subject`` (default: the only manifest entry) and the check's ok."""
    root = tmp_path / "root"
    _write_tree(root, files)
    rows, ok = _check(tmp_path, root, manifest_entries)
    subject = subject or manifest_entries[0]
    return next(row for row in rows if row.path == subject), ok


# -- a bare absolute import of a sibling module in the importing file's own folder ---------------

@pytest.mark.parametrize("statement", ["import shell_words\n", "from shell_words import thing\n"])
def test_bare_import_of_a_non_manifest_sibling_counts_one(tmp_path, statement) -> None:
    row, ok = _one_row(
        tmp_path,
        {
            "tools/hooks/x.py": statement,
            "tools/hooks/shell_words.py": "thing = 1\n",
        },
        ["tools/hooks/x.py"],
        subject="tools/hooks/x.py",
    )
    assert row.counts.get("I") == 1
    assert ok is False


def test_bare_import_of_a_manifest_sibling_counts_zero(tmp_path) -> None:
    row, ok = _one_row(
        tmp_path,
        {
            "tools/hooks/x.py": "import shell_words\n",
            "tools/hooks/shell_words.py": "thing = 1\n",
        },
        ["tools/hooks/x.py", "tools/hooks/shell_words.py"],
        subject="tools/hooks/x.py",
    )
    assert row.counts.get("I", 0) == 0
    assert ok is True


# -- a folder the file adds to sys.path with a Path(__file__)-relative expression -----------------

SYS_PATH_FOLDERS = [
    'str(Path(__file__).parent.parent / "x")',
    'str(Path(__file__).resolve().parents[1] / "x")',
    'os.path.join(os.path.dirname(os.path.dirname(__file__)), "x")',
]


@pytest.mark.parametrize("folder", SYS_PATH_FOLDERS)
def test_import_of_a_module_in_a_sys_path_folder_counts_one(tmp_path, folder) -> None:
    body = (
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {folder})\n"
        "import helper_bare\n"
    )
    row, ok = _one_row(
        tmp_path,
        {
            "tools/hooks/hook.py": body,
            "tools/x/helper_bare.py": "V = 1\n",
        },
        ["tools/hooks/hook.py"],
    )
    assert row.counts.get("I") == 1
    assert ok is False


def test_a_sys_path_folder_outside_the_root_is_ignored(tmp_path) -> None:
    # parents[3] of tools/hooks/hook.py is the folder above the root: the module there is not a repo module
    body = (
        "import sys\n"
        "from pathlib import Path\n"
        'sys.path.append(str(Path(__file__).parents[3] / "x"))\n'
        "import helper_bare\n"
    )
    root = tmp_path / "root"
    _write_tree(root, {"tools/hooks/hook.py": body})
    _write_tree(tmp_path, {"x/helper_bare.py": "V = 1\n"})
    rows, ok = _check(tmp_path, root, ["tools/hooks/hook.py"])
    assert rows[0].counts.get("I", 0) == 0
    assert ok is True


# -- names found nowhere, and sibling modules that are in the manifest, stay uncounted ------------

def test_stdlib_and_absent_third_party_imports_count_zero(tmp_path) -> None:
    row, ok = _one_row(
        tmp_path,
        {
            "tools/hooks/x.py": (
                "import json\n"
                "import definitely_not_installed_anywhere_103\n"
                "from os import path\n"
            ),
        },
        ["tools/hooks/x.py"],
        subject="tools/hooks/x.py",
    )
    assert row.counts.get("I", 0) == 0
    assert ok is True


# -- a folder named through a module-level variable (the repo's own pattern: _ROOT = Path(__file__)...) --

VARIABLE_BASE = (
    "import sys\n"
    "from pathlib import Path\n"
    "_ROOT = Path(__file__).resolve().parents[1]\n"
    'sys.path.insert(0, str(_ROOT / "x"))\n'
    "import helper_bare\n"
)


def test_variable_base_folder_holding_a_non_manifest_module_counts_one(tmp_path) -> None:
    row, ok = _one_row(
        tmp_path,
        {
            "tools/hooks/hook.py": VARIABLE_BASE,
            "tools/x/helper_bare.py": "V = 1\n",
        },
        ["tools/hooks/hook.py"],
    )
    assert row.counts.get("I") == 1
    assert ok is False


def test_variable_base_folder_holding_a_manifest_module_counts_zero(tmp_path) -> None:
    row, ok = _one_row(
        tmp_path,
        {
            "tools/hooks/hook.py": VARIABLE_BASE,
            "tools/x/helper_bare.py": "V = 1\n",
        },
        ["tools/hooks/hook.py", "tools/x/helper_bare.py"],
    )
    assert row.counts.get("I", 0) == 0
    assert ok is True


@pytest.mark.parametrize("body", [
    # a name never assigned in the file
    'import sys\nsys.path.insert(0, str(_MISSING / "x"))\nimport helper_bare\n',
    # a name that leads back to itself
    'import sys\n_A = _B / "x"\n_B = _A\nsys.path.insert(0, str(_A))\nimport helper_bare\n',
    # a name assigned twice at module level
    "import sys\nfrom pathlib import Path\n_ROOT = Path(__file__).resolve().parents[1]\n"
    '_ROOT = Path(__file__).resolve().parents[1]\nsys.path.insert(0, str(_ROOT / "x"))\nimport helper_bare\n',
    # a name assigned only inside a function, not at module level
    "import sys\nfrom pathlib import Path\ndef setup():\n    _ROOT = Path(__file__).resolve().parents[1]\n"
    'sys.path.insert(0, str(_ROOT / "x"))\nimport helper_bare\n',
])
def test_an_unresolvable_sys_path_name_counts_zero_without_a_crash(tmp_path, body) -> None:
    row, ok = _one_row(
        tmp_path,
        {
            "tools/hooks/hook.py": body,
            "tools/x/helper_bare.py": "V = 1\n",
        },
        ["tools/hooks/hook.py"],
    )
    assert row.counts.get("I", 0) == 0
    assert ok is True


# -- a bare script's own folder shadows the stdlib (the current behaviour, kept on purpose) -------

def test_bare_json_beside_a_non_manifest_json_module_counts_one(tmp_path) -> None:
    # running a script puts its own folder first on sys.path, so `import json` there finds tools/hooks/json.py
    # before the stdlib: the check follows the same order and counts it
    row, ok = _one_row(
        tmp_path,
        {
            "tools/hooks/x.py": "import json\n",
            "tools/hooks/json.py": "V = 1\n",
        },
        ["tools/hooks/x.py"],
        subject="tools/hooks/x.py",
    )
    assert row.counts.get("I") == 1
    assert ok is False


# -- the command: exit 1 on a bare sibling import -------------------------------------------------

def test_cli_exit_code_is_one_on_a_bare_sibling_import(tmp_path, monkeypatch, capsys) -> None:
    dirty = tmp_path / "dirty"
    _write_tree(dirty, {"tools/hooks/x.py": "import shell_words\n",
                        "tools/hooks/shell_words.py": "thing = 1\n"})
    manifest = _write_manifest(tmp_path, ["tools/hooks/x.py"], name="cli_manifest.txt")
    monkeypatch.setattr(core_check, "run_check",
                        partial(core_check.run_check, manifest_path=manifest,
                                patterns_path=tmp_path / "absent.json"))
    assert main(["core", "check", "--root", str(dirty)]) == 1
    assert "I" in capsys.readouterr().out.splitlines()[0].split()
