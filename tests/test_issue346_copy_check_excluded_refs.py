"""#346 unit 2: the test copy check reports a copied test file that names an excluded source file (kind ``excluded-ref``),
by its repo-relative path, by its dotted module path (``scripts.secret``), or, for a ``.py``, by its file name or its
module stem (a quoted string or an import / ``from ... import`` module name). The hit names the test file, the line
and the excluded file; the check refuses the copy. Only excluded source files (``.py .ts .js .sh .ps1``) count: a
data or markdown file a temp-repo fixture names is no hit. A folder listing names no excluded file and is no hit.
Real git in a temp repo; the machine file is absent (``AGENTOPS_MACHINE_DIR`` points at an empty temp folder).
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from tools.agentops import test_copy_check as tc
from tools.agentops import test_copy_send
from tools.agentops.test_copy_cmd import run_test_copy_command

EXCLUDED = "scripts/secret.py"


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _repo(tmp_path: Path, files: dict) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(tmp_path, "init", "-q", str(repo))
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


def _machine(tmp_path: Path, monkeypatch) -> None:
    machine = tmp_path / "machine"
    machine.mkdir()
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(machine))


def _settings(excluded: list) -> str:
    return json.dumps({"test_copy_exclude": excluded}, indent=2)


def _test_lines(*body: str) -> str:
    """A copied test file: a six-line header (no excluded name in it), then ``body`` from line 7 on."""
    return "\n".join([
        '"""a copied test"""',
        "from pathlib import Path",
        "",
        "ROOT = Path(__file__).resolve().parents[1]",
        "",
        "def test_x():",
        *body,
        "",
    ])


def test_a_path_reference_to_an_excluded_script_is_a_hit_and_refuses(tmp_path, monkeypatch, capsys):
    _machine(tmp_path, monkeypatch)
    repo = _repo(tmp_path, {
        "scripts/secret.py": "VALUE = 1\n",
        "tests/test_a.py": _test_lines('    assert (ROOT / "scripts" / "secret.py").name'),
        "config/orchestration.json": _settings([EXCLUDED]),
    })
    assert "scripts/secret.py" not in tc.snapshot_files(repo)
    assert tc.scan(repo) == [tc.Hit("tests/test_a.py", 7, "excluded-ref", EXCLUDED)]
    # the CLI check refuses and prints the hit with the excluded file's name
    rc = run_test_copy_command(argparse.Namespace(test_copy_command="check", root=repo))
    out = capsys.readouterr().out
    assert rc == 1
    assert "tests/test_a.py:7 excluded-ref scripts/secret.py" in out
    # the send refuses the copy before any push (no remote is set, so only the scan can refuse)
    result = test_copy_send.send(repo, "i346-check", remote=None)
    assert result.sent is False
    assert "tests/test_a.py:7 excluded-ref scripts/secret.py" in result.reason


def test_a_module_name_import_of_an_excluded_script_is_one_hit(tmp_path, monkeypatch):
    _machine(tmp_path, monkeypatch)
    repo = _repo(tmp_path, {
        "scripts/secret.py": "VALUE = 1\n",
        "tests/test_a.py": _test_lines("import secret"),
        "config/orchestration.json": _settings([EXCLUDED]),
    })
    assert tc.scan(repo) == [tc.Hit("tests/test_a.py", 7, "excluded-ref", EXCLUDED)]


def test_a_from_import_of_an_excluded_module_is_one_hit(tmp_path, monkeypatch):
    _machine(tmp_path, monkeypatch)
    repo = _repo(tmp_path, {
        "scripts/secret.py": "VALUE = 1\n",
        "tests/test_a.py": _test_lines("from scripts.secret import VALUE"),
        "config/orchestration.json": _settings([EXCLUDED]),
    })
    assert tc.scan(repo) == [tc.Hit("tests/test_a.py", 7, "excluded-ref", EXCLUDED)]


def test_a_dotted_module_path_of_an_excluded_script_is_a_hit_in_any_string_or_import(tmp_path, monkeypatch):
    _machine(tmp_path, monkeypatch)
    repo = _repo(tmp_path, {
        "scripts/secret.py": "VALUE = 1\n",
        "tests/test_a.py": _test_lines(
            '    importlib.import_module("scripts.secret")',
            '    patch("scripts.secret.VALUE")',
            "    import os, scripts.secret",
            '    assert os.name == "tools.scripts.secret_x"',
        ),
        "config/orchestration.json": _settings([EXCLUDED]),
    })
    assert tc.scan(repo) == [
        tc.Hit("tests/test_a.py", 7, "excluded-ref", EXCLUDED),
        tc.Hit("tests/test_a.py", 8, "excluded-ref", EXCLUDED),
        tc.Hit("tests/test_a.py", 9, "excluded-ref", EXCLUDED),
    ]


def test_a_folder_listing_without_an_excluded_file_is_no_hit(tmp_path, monkeypatch):
    _machine(tmp_path, monkeypatch)
    repo = _repo(tmp_path, {
        "scripts/secret.py": "VALUE = 1\n",
        "tests/test_a.py": _test_lines('    assert (ROOT / "scripts").is_dir()  # the scripts/ folder'),
        "config/orchestration.json": _settings([EXCLUDED]),
    })
    assert tc.scan(repo) == []


def test_words_that_only_contain_the_stem_are_no_hit(tmp_path, monkeypatch):
    _machine(tmp_path, monkeypatch)
    repo = _repo(tmp_path, {
        "scripts/secret.py": "VALUE = 1\n",
        "tests/test_a.py": _test_lines("    # keep the secret value, a mysecret.pyx and secretary notes"),
        "config/orchestration.json": _settings([EXCLUDED]),
    })
    assert tc.scan(repo) == []


def test_an_excluded_non_source_file_named_by_a_test_is_no_hit(tmp_path, monkeypatch):
    """Only excluded source files are checked: a markdown or data file a temp-repo fixture names is no hit."""
    _machine(tmp_path, monkeypatch)
    repo = _repo(tmp_path, {
        "scripts/secret.py": "VALUE = 1\n",
        "docs/state.md": "# state\n",
        "tests/test_a.py": _test_lines('    write("docs/state.md")'),
        "config/orchestration.json": _settings([EXCLUDED, "docs/state.md"]),
    })
    assert tc.scan(repo) == []
    (repo / "tests/test_a.py").write_text(_test_lines('    write("docs/state.md")', "    assert (ROOT / 'scripts' / 'secret.py')"))
    assert tc.scan(repo) == [tc.Hit("tests/test_a.py", 8, "excluded-ref", EXCLUDED)]


def test_an_excluded_test_file_is_not_scanned(tmp_path, monkeypatch):
    _machine(tmp_path, monkeypatch)
    repo = _repo(tmp_path, {
        "scripts/secret.py": "VALUE = 1\n",
        "tests/test_zz_secret_ref.py": _test_lines('    assert (ROOT / "scripts" / "secret.py")'),
        "tests/test_a.py": _test_lines("    assert True"),
        "config/orchestration.json": _settings([EXCLUDED, "tests/test_zz_secret_ref.py"]),
    })
    assert tc.scan(repo) == []


def test_generic_stems_are_not_flagged(tmp_path, monkeypatch):
    _machine(tmp_path, monkeypatch)
    repo = _repo(tmp_path, {
        "scripts/abc.py": "VALUE = 1\n",
        "scripts/pkg/__init__.py": "VALUE = 2\n",
        "tests/test_a.py": _test_lines('    assert "abc" and "__init__" and (ROOT / "scripts" / "abc.py")'),
        "config/orchestration.json": _settings(["scripts/abc.py", "scripts/pkg/__init__.py"]),
    })
    assert tc.scan(repo) == []


def test_the_check_prints_excluded_ref_zero_when_no_hit(tmp_path, monkeypatch, capsys):
    """Row 5: the count line names the kind even when it has no hit."""
    _machine(tmp_path, monkeypatch)
    repo = _repo(tmp_path, {
        "scripts/secret.py": "VALUE = 1\n",
        "tests/test_a.py": _test_lines("    assert True"),
        "config/orchestration.json": _settings([EXCLUDED]),
    })
    rc = run_test_copy_command(argparse.Namespace(test_copy_command="check", root=repo))
    out = capsys.readouterr().out
    assert rc == 0
    assert "excluded-ref=0" in out
