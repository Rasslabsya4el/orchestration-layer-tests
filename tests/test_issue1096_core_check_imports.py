"""Issue #1096 unit 1: the leak check's pattern I (every import of every manifest .py file, any depth,
relative imports resolved, a repo module outside the manifest counts). Planted temp trees only: this test
never reads the repo's own manifest or its core files."""

from __future__ import annotations

from functools import partial

import pytest

from tools.agentops import core_check
from tools.agentops.core_check import format_report, main, run_check


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


# -- a non-manifest repo module imported inside a function body ----------------------------------

def test_function_body_import_of_a_non_manifest_repo_module_counts_one(tmp_path) -> None:
    row, ok = _one_row(
        tmp_path,
        {
            "core/leaky.py": "def run():\n    import helper.extra\n    return helper.extra\n",
            "helper/extra.py": "X = 1\n",
        },
        ["core/leaky.py"],
    )
    assert row.counts.get("I") == 1
    assert ok is False


def test_json_and_a_manifest_module_import_count_zero(tmp_path) -> None:
    row, ok = _one_row(
        tmp_path,
        {
            "core/clean.py": (
                "import json\n"
                "from core.other import thing\n"
                "def f():\n"
                "    import json\n"
                "    return thing\n"
            ),
            "core/other.py": "thing = 1\n",
        },
        ["core/clean.py", "core/other.py"],
    )
    assert row.counts.get("I", 0) == 0
    assert ok is True


def test_a_try_block_import_of_a_non_manifest_repo_module_counts_one(tmp_path) -> None:
    row, ok = _one_row(
        tmp_path,
        {
            "core/t.py": "try:\n    import helper.extra\nexcept ImportError:\n    helper = None\n",
            "helper/extra.py": "X = 1\n",
        },
        ["core/t.py"],
    )
    assert row.counts.get("I") == 1
    assert ok is False


def test_an_import_inside_a_comment_or_a_string_is_not_an_import(tmp_path) -> None:
    row, ok = _one_row(
        tmp_path,
        {
            "core/c.py": "# import helper.extra\nTEXT = 'import helper.extra'\n",
            "helper/extra.py": "X = 1\n",
        },
        ["core/c.py"],
    )
    assert row.counts.get("I", 0) == 0
    assert ok is True


# -- relative imports, resolved against the file's package ---------------------------------------

def test_relative_import_of_a_non_manifest_sibling_counts_one(tmp_path) -> None:
    row, ok = _one_row(
        tmp_path,
        {
            "core/sub/__init__.py": "",
            "core/sub/mod.py": "from .x import y\n",
            "core/sub/x.py": "y = 1\n",
        },
        ["core/sub/__init__.py", "core/sub/mod.py"],
        subject="core/sub/mod.py",
    )
    assert row.counts.get("I") == 1
    assert ok is False


def test_relative_import_of_a_manifest_sibling_counts_zero(tmp_path) -> None:
    row, ok = _one_row(
        tmp_path,
        {
            "core/sub/__init__.py": "",
            "core/sub/mod.py": "from .x import y\n",
            "core/sub/x.py": "y = 1\n",
        },
        ["core/sub/__init__.py", "core/sub/mod.py", "core/sub/x.py"],
        subject="core/sub/mod.py",
    )
    assert row.counts.get("I", 0) == 0
    assert ok is True


def test_from_package_import_of_a_non_manifest_submodule_counts_one(tmp_path) -> None:
    row, ok = _one_row(
        tmp_path,
        {
            "core/sub/__init__.py": "",
            "core/sub/mod.py": "from . import z\n",
            "core/sub/z.py": "Z = 1\n",
        },
        ["core/sub/__init__.py", "core/sub/mod.py"],
        subject="core/sub/mod.py",
    )
    assert row.counts.get("I") == 1
    assert ok is False


# -- counted per file and in the total -----------------------------------------------------------

def test_counts_are_per_file_and_the_report_total_sums_them(tmp_path) -> None:
    root = tmp_path / "root"
    _write_tree(root, {
        "core/a.py": "def f():\n    import helper.extra\n",
        "core/b.py": "import helper.extra\ndef g():\n    from helper import extra\n",
        "helper/extra.py": "X = 1\n",
    })
    rows, ok = _check(tmp_path, root, ["core/a.py", "core/b.py"])
    by_path = {row.path: row for row in rows}
    assert by_path["core/a.py"].counts.get("I") == 1
    assert by_path["core/b.py"].counts.get("I") == 2
    assert ok is False
    total_line = [line for line in format_report(rows).splitlines() if line.startswith("total")][0]
    assert total_line.split()[-1] == "3"


# -- --root is the tree that decides what is a repo module ---------------------------------------

def test_root_decides_whether_the_import_is_a_repo_module(tmp_path) -> None:
    files = {"core/uses.py": "def f():\n    import helper.extra\n"}
    with_helper = tmp_path / "with_helper"
    _write_tree(with_helper, {**files, "helper/extra.py": "X = 1\n"})
    without_helper = tmp_path / "without_helper"
    _write_tree(without_helper, files)

    rows_with, ok_with = _check(tmp_path, with_helper, ["core/uses.py"])
    rows_without, ok_without = _check(tmp_path, without_helper, ["core/uses.py"])

    assert rows_with[0].counts.get("I") == 1
    assert ok_with is False
    assert rows_without[0].counts.get("I", 0) == 0
    assert ok_without is True


def test_a_core_file_that_does_not_parse_is_counted_as_one(tmp_path) -> None:
    row, ok = _one_row(tmp_path, {"core/broken.py": "def (:\n"}, ["core/broken.py"])
    assert row.counts.get("I") == 1
    assert ok is False


# -- the command: exit 1 on I > 0, the help text names the pattern -------------------------------

def _patch_check_to_planted(monkeypatch, tmp_path, manifest_entries):
    manifest = _write_manifest(tmp_path, manifest_entries, name="cli_manifest.txt")
    monkeypatch.setattr(core_check, "run_check",
                        partial(core_check.run_check, manifest_path=manifest,
                                patterns_path=tmp_path / "absent.json"))


def test_cli_exit_code_is_one_on_an_i_count_and_zero_when_clean(tmp_path, monkeypatch, capsys) -> None:
    dirty = tmp_path / "dirty"
    _write_tree(dirty, {"core/leaky.py": "def run():\n    import helper.extra\n",
                        "helper/extra.py": "X = 1\n"})
    _patch_check_to_planted(monkeypatch, tmp_path, ["core/leaky.py"])
    assert main(["core", "check", "--root", str(dirty)]) == 1
    assert "I" in capsys.readouterr().out.splitlines()[0].split()

    clean = tmp_path / "clean"
    _write_tree(clean, {"core/clean.py": "import json\n"})
    _patch_check_to_planted(monkeypatch, tmp_path, ["core/clean.py"])
    assert main(["core", "check", "--root", str(clean)]) == 0


def test_check_help_text_names_the_pattern_in_one_line(monkeypatch, capsys) -> None:
    monkeypatch.setenv("COLUMNS", "300")
    with pytest.raises(SystemExit):
        main(["core", "--help"])  # the subcommand's help line is listed by its parent
    listed = [line for line in capsys.readouterr().out.splitlines() if line.strip().startswith("check")]
    assert listed and "pattern I" in listed[0]


# -- a local_modules entry (extensions.py, ADR 0109 point 4): only the loader may import it --------

HOME_SETTINGS = {"update_rule": "home", "local_modules": [".helper_mod"]}


def _local_tree(files, settings=HOME_SETTINGS):
    """A planted root whose settings file names one local module, `.helper_mod`, which lives in
    tools/agentops (the package the loader resolves a leading dot in)."""
    import json as _json
    return {
        "config/orchestration.json": _json.dumps(settings),
        "tools/agentops/helper_mod.py": "X = 1\n",
        **files,
    }


@pytest.mark.parametrize("statement", ["from . import helper_mod\n", "from .helper_mod import X\n"])
def test_a_direct_import_of_a_local_module_counts_one_even_when_it_is_in_the_manifest(tmp_path, statement) -> None:
    row, ok = _one_row(
        tmp_path,
        _local_tree({"tools/agentops/core_user.py": statement}),
        ["tools/agentops/helper_mod.py", "tools/agentops/core_user.py"],
        subject="tools/agentops/core_user.py",
    )
    assert row.counts.get("I") == 1
    assert ok is False


def test_loading_the_local_module_through_importlib_counts_zero(tmp_path) -> None:
    body = (
        "import importlib\n"
        "def load():\n"
        "    return importlib.import_module('.helper_mod', package=__package__)\n"
    )
    row, ok = _one_row(
        tmp_path,
        _local_tree({"tools/agentops/core_user.py": body}),
        ["tools/agentops/helper_mod.py", "tools/agentops/core_user.py"],
        subject="tools/agentops/core_user.py",
    )
    assert row.counts.get("I", 0) == 0
    assert ok is True


def test_without_a_settings_file_a_manifest_module_import_is_not_a_local_one(tmp_path) -> None:
    row, ok = _one_row(
        tmp_path,
        {
            "tools/agentops/helper_mod.py": "X = 1\n",
            "tools/agentops/core_user.py": "from . import helper_mod\n",
        },
        ["tools/agentops/helper_mod.py", "tools/agentops/core_user.py"],
        subject="tools/agentops/core_user.py",
    )
    assert row.counts.get("I", 0) == 0
    assert ok is True
