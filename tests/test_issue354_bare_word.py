"""#354: a quoted bare word that equals an excluded ``.py`` stem is an ``excluded-ref`` hit only when its line also shows a
file context (a path separator, a ``.py``, or an import / load / spec word). ``{"name": "zqsecret"}`` in a copied test is no
hit when ``scripts/zqsecret.py`` is excluded; ``import zqsecret`` and ``importlib.import_module("zqsecret")`` are.
Real git in a temp repo; the machine file is absent (``AGENTOPS_MACHINE_DIR`` points at an empty temp folder).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tools.agentops import test_copy_check as tc

EXCLUDED = "scripts/zqsecret.py"


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


def test_a_quoted_bare_word_with_no_file_context_is_no_hit(tmp_path, monkeypatch):
    """Row 1: a fake job name ``"zqsecret"`` in data, on a line with no path, ``.py``, or import word, is no reference."""
    _machine(tmp_path, monkeypatch)
    repo = _repo(tmp_path, {
        "scripts/zqsecret.py": "VALUE = 1\n",
        "tests/test_a.py": _test_lines('    job = {"name": "zqsecret"}'),
        "config/orchestration.json": _settings([EXCLUDED]),
    })
    assert tc.scan(repo) == []


def test_a_module_name_import_of_an_excluded_script_is_one_hit(tmp_path, monkeypatch):
    """Row 2: ``import zqsecret`` is a file reference (the import form keeps its rule)."""
    _machine(tmp_path, monkeypatch)
    repo = _repo(tmp_path, {
        "scripts/zqsecret.py": "VALUE = 1\n",
        "tests/test_a.py": _test_lines("    import zqsecret"),
        "config/orchestration.json": _settings([EXCLUDED]),
    })
    assert tc.scan(repo) == [tc.Hit("tests/test_a.py", 7, "excluded-ref", EXCLUDED)]


def test_a_path_built_from_parts_naming_the_excluded_file_is_one_hit(tmp_path, monkeypatch):
    """Row 2: ``ROOT / "scripts" / "zqsecret.py"`` names the file by its file name (the ``.py`` form keeps its rule)."""
    _machine(tmp_path, monkeypatch)
    repo = _repo(tmp_path, {
        "scripts/zqsecret.py": "VALUE = 1\n",
        "tests/test_a.py": _test_lines('    assert (ROOT / "scripts" / "zqsecret.py").name'),
        "config/orchestration.json": _settings([EXCLUDED]),
    })
    assert tc.scan(repo) == [tc.Hit("tests/test_a.py", 7, "excluded-ref", EXCLUDED)]


def test_a_quoted_stem_with_an_import_word_on_its_line_is_one_hit(tmp_path, monkeypatch):
    """Row 2: a quoted stem on a line with an import word (``importlib``) is a file reference."""
    _machine(tmp_path, monkeypatch)
    repo = _repo(tmp_path, {
        "scripts/zqsecret.py": "VALUE = 1\n",
        "tests/test_a.py": _test_lines('    importlib.import_module("zqsecret")'),
        "config/orchestration.json": _settings([EXCLUDED]),
    })
    assert tc.scan(repo) == [tc.Hit("tests/test_a.py", 7, "excluded-ref", EXCLUDED)]


def test_a_quoted_stem_on_a_line_with_a_path_separator_is_one_hit(tmp_path, monkeypatch):
    """Row 2: a quoted stem on a line that shows a path separator is a file reference."""
    _machine(tmp_path, monkeypatch)
    repo = _repo(tmp_path, {
        "scripts/zqsecret.py": "VALUE = 1\n",
        "tests/test_a.py": _test_lines('    assert open("scripts/" + "zqsecret")'),
        "config/orchestration.json": _settings([EXCLUDED]),
    })
    assert tc.scan(repo) == [tc.Hit("tests/test_a.py", 7, "excluded-ref", EXCLUDED)]
