"""Issue #351, unit 1 (dir-args): a commit's directory arguments are judged by the files they stand for.

Before ``commit_needs_suite`` judges a commit, a directory argument is replaced by the changed files under it:
what ``git add`` of that directory would stage (modified, added, deleted, renamed with both names, and untracked
files that are not ignored). An unchanged tracked file does not count, and an ignored file does not count. A file
argument stays as it is. The verdict is on those files, and the result names the suite the commit ran
(``suite-free``, ``quick check``, ``full suite``).

Seam: ``merge.run_commit`` on a real tmp git repo (bare origin + clone). The suite gate and the quick-check gate
are replaced by spies that record whether a gate was asked for; a green gate is simulated by ``None``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tools.agentops import merge as merge_module
from tools.agentops.merge import run_commit

SUITE = "python -m pytest tests -q"


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(merge_module, "SUITE_OUTPUT_DIR", tmp_path / "suite_out")
    monkeypatch.setattr(merge_module, "MERGE_LOG_PATH", tmp_path / "merge.log")
    monkeypatch.setattr(merge_module, "resolve_suite_cmd", lambda explicit=None: explicit or SUITE)


def _spy(monkeypatch: pytest.MonkeyPatch, gate_name: str) -> list:
    """Replaces the gate ``gate_name`` by a spy: it records each call's arguments and passes (``None``)."""
    calls: list = []

    def spy(*args, **kwargs):
        calls.append(args)
        return None

    monkeypatch.setattr(merge_module, gate_name, spy)
    return calls


@pytest.fixture
def suite_calls(monkeypatch: pytest.MonkeyPatch) -> list:
    """Each full-suite gate the commit asks for; the suite is green."""
    return _spy(monkeypatch, "_commit_suite_gate")


@pytest.fixture
def quick_calls(monkeypatch: pytest.MonkeyPatch) -> list:
    """Each quick-check gate the commit asks for (its arguments: cwd, head, judged paths, ...); green."""
    return _spy(monkeypatch, "_quick_check_gate")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60)
    assert result.returncode == 0, f"git {args} failed in {cwd}: {result.stderr}"
    return result


def _clone(bare: Path, dest: Path) -> Path:
    _git(dest.parent, "clone", "-q", str(bare), str(dest))
    _git(dest, "config", "user.email", "agentops-test@example.invalid")
    _git(dest, "config", "user.name", "agentops test")
    return dest


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _world(tmp_path: Path) -> tuple[Path, Path]:
    """A bare origin whose main holds a .gitignore (``scratch.py`` ignored), a markdown-only folder, a folder
    with an unchanged tracked .py beside a .md, a folder to delete, a folder for a rename, a code folder and a
    root note. Returns (checkout, lock_path)."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = _clone(bare, tmp_path / "seed")
    _write(seed, ".gitignore", "scratch.py\n")
    _write(seed, "README.md", "root v0\n")
    _write(seed, "tools/agentops/x.py", "x = 1\n")
    _write(seed, "docs/research/old/a.md", "a v0\n")
    _write(seed, "docs/research/mixed/code.py", "m = 1\n")
    _write(seed, "docs/research/mixed/a.md", "m v0\n")
    _write(seed, "docs/research/gone/code.py", "g = 1\n")
    _write(seed, "docs/research/gone/a.md", "g v0\n")
    _write(seed, "docs/research/ren/code.py", "r = 1\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "initial")
    _git(seed, "push", "-q", "origin", "main")
    checkout = _clone(bare, tmp_path / "checkout")
    lock = tmp_path / "var" / "locks" / "merge.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    return checkout, lock


def _commit(checkout: Path, lock: Path, paths: list[str]):
    return run_commit(paths, "#351 dir args test", cwd=checkout, lock_path=lock, lock_timeout=10)


# Row 1 -- a directory holding only docs/**/*.md files: no full suite, the result names it suite-free.
def test_row1_dir_of_markdown_only_is_suite_free(tmp_path, suite_calls, quick_calls) -> None:
    checkout, lock = _world(tmp_path)
    _write(checkout, "docs/research/2026-10-23-draft/a.md", "a\n")
    _write(checkout, "docs/research/2026-10-23-draft/b.md", "b\n")

    result = _commit(checkout, lock, ["docs/research/2026-10-23-draft"])

    assert result.ok, result.render()
    assert suite_calls == [], "a markdown-only directory asked for the full suite"
    assert quick_calls == []
    assert result.suite == "suite-free"
    assert "suite-free" in result.render()


# Row 2 -- a directory with one .py file and .md files: the full suite runs, as today.
def test_row2_dir_with_one_py_file_needs_the_full_suite(tmp_path, suite_calls, quick_calls) -> None:
    checkout, lock = _world(tmp_path)
    _write(checkout, "docs/research/2026-10-23-draft/a.md", "a\n")
    _write(checkout, "docs/research/2026-10-23-draft/code.py", "x = 2\n")

    result = _commit(checkout, lock, ["docs/research/2026-10-23-draft"])

    assert result.ok, result.render()
    assert len(suite_calls) == 1, "a directory with a .py file did not ask for the full suite"
    assert result.suite == "full suite"


# A plain-file argument stays as it is: a markdown file is suite-free, a python file needs the suite.
def test_plain_markdown_file_argument_is_suite_free(tmp_path, suite_calls, quick_calls) -> None:
    checkout, lock = _world(tmp_path)
    _write(checkout, "docs/research/old/a.md", "a v1\n")

    result = _commit(checkout, lock, ["docs/research/old/a.md"])

    assert result.ok, result.render()
    assert suite_calls == []
    assert result.suite == "suite-free"


def test_plain_python_file_argument_needs_the_full_suite(tmp_path, suite_calls, quick_calls) -> None:
    checkout, lock = _world(tmp_path)
    _write(checkout, "tools/agentops/x.py", "x = 3\n")

    result = _commit(checkout, lock, ["tools/agentops/x.py"])

    assert result.ok, result.render()
    assert len(suite_calls) == 1
    assert result.suite == "full suite"


# Nested directory: a markdown-only tree stays suite-free; one .py file deep inside it needs the suite.
def test_nested_markdown_only_directory_is_suite_free(tmp_path, suite_calls, quick_calls) -> None:
    checkout, lock = _world(tmp_path)
    _write(checkout, "docs/research/nest/deep/er/x.md", "x\n")
    _write(checkout, "docs/research/nest/y.md", "y\n")

    result = _commit(checkout, lock, ["docs/research/nest"])

    assert result.ok, result.render()
    assert suite_calls == []
    assert result.suite == "suite-free"


def test_nested_python_file_deep_in_directory_needs_the_full_suite(tmp_path, suite_calls, quick_calls) -> None:
    checkout, lock = _world(tmp_path)
    _write(checkout, "docs/research/nest/deep/er/x.md", "x\n")
    _write(checkout, "docs/research/nest/deep/er/code.py", "x = 4\n")

    result = _commit(checkout, lock, ["docs/research/nest"])

    assert result.ok, result.render()
    assert len(suite_calls) == 1
    assert result.suite == "full suite"


# A changed tracked file under a directory is judged; an ignored file under it is not.
def test_tracked_markdown_under_directory_is_suite_free(tmp_path, suite_calls, quick_calls) -> None:
    checkout, lock = _world(tmp_path)
    _write(checkout, "docs/research/old/a.md", "a v1\n")

    result = _commit(checkout, lock, ["docs/research/old"])

    assert result.ok, result.render()
    assert suite_calls == []
    assert result.suite == "suite-free"


def test_ignored_python_file_under_directory_does_not_count(tmp_path, suite_calls, quick_calls) -> None:
    checkout, lock = _world(tmp_path)
    _write(checkout, "docs/research/ign/x.md", "x\n")
    _write(checkout, "docs/research/ign/scratch.py", "x = 5\n")  # ignored by .gitignore

    result = _commit(checkout, lock, ["docs/research/ign"])

    assert result.ok, result.render()
    assert suite_calls == [], "an ignored file under the directory was judged"
    assert result.suite == "suite-free"


# An unchanged tracked .py under the directory does not force the suite; a changed one does.
def test_unchanged_tracked_py_beside_changed_markdown_is_suite_free(tmp_path, suite_calls, quick_calls) -> None:
    checkout, lock = _world(tmp_path)
    _write(checkout, "docs/research/mixed/a.md", "m v1\n")  # code.py in the folder is tracked and unchanged

    result = _commit(checkout, lock, ["docs/research/mixed"])

    assert result.ok, result.render()
    assert suite_calls == [], "an unchanged tracked .py under the directory forced the full suite"
    assert quick_calls == []
    assert result.suite == "suite-free"


def test_changed_tracked_py_under_directory_needs_the_full_suite(tmp_path, suite_calls, quick_calls) -> None:
    checkout, lock = _world(tmp_path)
    _write(checkout, "docs/research/mixed/code.py", "m = 2\n")
    _write(checkout, "docs/research/mixed/a.md", "m v1\n")

    result = _commit(checkout, lock, ["docs/research/mixed"])

    assert result.ok, result.render()
    assert len(suite_calls) == 1
    assert result.suite == "full suite"


# A deleted tracked .py under a directory is a change: the full suite runs.
def test_deleted_py_under_directory_needs_the_full_suite(tmp_path, suite_calls, quick_calls) -> None:
    checkout, lock = _world(tmp_path)
    (checkout / "docs/research/gone/code.py").unlink()

    result = _commit(checkout, lock, ["docs/research/gone"])

    assert result.ok, result.render()
    assert len(suite_calls) == 1, "a deleted .py under the directory did not ask for the full suite"
    assert result.suite == "full suite"


# A deleted directory argument: its deleted files are judged, so a markdown-only one is suite-free ...
def test_deleted_markdown_only_directory_is_suite_free(tmp_path, suite_calls, quick_calls) -> None:
    checkout, lock = _world(tmp_path)
    shutil.rmtree(checkout / "docs/research/old")

    result = _commit(checkout, lock, ["docs/research/old"])

    assert result.ok, result.render()
    assert suite_calls == []
    assert result.suite == "suite-free"


# ... and one whose deleted files include a .py needs the full suite.
def test_deleted_directory_with_py_needs_the_full_suite(tmp_path, suite_calls, quick_calls) -> None:
    checkout, lock = _world(tmp_path)
    shutil.rmtree(checkout / "docs/research/gone")

    result = _commit(checkout, lock, ["docs/research/gone"])

    assert result.ok, result.render()
    assert len(suite_calls) == 1
    assert result.suite == "full suite"


# A rename inside a directory is judged by both names: the old .py name alone needs the suite.
def test_renamed_py_to_markdown_under_directory_needs_the_full_suite(tmp_path, suite_calls, quick_calls) -> None:
    checkout, lock = _world(tmp_path)
    _git(checkout, "mv", "docs/research/ren/code.py", "docs/research/ren/code.md")

    result = _commit(checkout, lock, ["docs/research/ren"])

    assert result.ok, result.render()
    assert len(suite_calls) == 1, "the old .py name of a rename was not judged"
    assert result.suite == "full suite"


# A directory of changed data files (``config/*.json``, suite-free) still reaches the quick-check gate through the
# judged list: the gate is handed the files, not the directory name.
def test_changed_json_under_directory_reaches_the_quick_check_gate(tmp_path, suite_calls, quick_calls) -> None:
    checkout, lock = _world(tmp_path)
    _write(checkout, "config/x.json", '{"a": 1}\n')
    _write(checkout, "config/y.json", '{"b": 2}\n')

    result = _commit(checkout, lock, ["config"])

    assert result.ok, result.render()
    assert suite_calls == []
    assert len(quick_calls) == 1, "a directory of changed .json files did not reach the quick check"
    judged = quick_calls[0][2]  # _quick_check_gate(cwd, head_before, judged, ...)
    assert sorted(judged) == ["config/x.json", "config/y.json"]
    assert result.suite == "quick check"
