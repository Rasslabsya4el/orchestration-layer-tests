"""Issue #40, unit 2 (quick check): a data commit (``commit_needs_suite`` False) runs, before its push and
under the checkout lock, a JSON load of each changed ``.json`` file and the test files under ``tests/``
that name a changed non-Markdown file. A failure refuses the commit (reason ``quick-check-red``, one line
naming the file or the failing node id), nothing is pushed, and the local commit is undone as a red
suite's is. The result carries ``quick_check``; a Markdown-only commit carries none.

Real git on a tmp repo (bare origin + clone); the suite is pytest over the tmp repo's own tests."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.agentops import detach
from tools.agentops import merge as merge_module
from tools.agentops.merge import MergeLock, run_commit, run_commit_command

SUITE = f'"{sys.executable}" -m pytest tests -q -p no:faulthandler -m "not slow"'
SETTINGS = "config/orchestration.json"


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(merge_module, "SUITE_OUTPUT_DIR", tmp_path / "suite_out")
    monkeypatch.setattr(merge_module, "MERGE_LOG_PATH", tmp_path / "merge.log")
    monkeypatch.setattr(merge_module, "resolve_suite_cmd", lambda explicit=None: explicit or SUITE)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
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


READS_SETTINGS = (
    "import json\n"
    "from pathlib import Path\n\n"
    "def test_reads_settings():\n"
    f"    data = json.loads(Path('{SETTINGS}').read_text(encoding='utf-8'))\n"
    "    assert data['update_rule'] == 'word'\n"
)
NAMES_SETTINGS = (
    "from pathlib import Path\n\n"
    "def test_names_settings_file():\n"
    f"    assert Path('{SETTINGS}').is_file()\n"
)
UNRELATED = "def test_unrelated():\n    assert True\n"


def _world(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A bare origin whose main holds a settings file, two tests that name it, one that does not, and
    a note. Returns (bare, checkout, lock_path)."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = _clone(bare, tmp_path / "seed")
    _write(seed, SETTINGS, json.dumps({"update_rule": "word"}) + "\n")
    _write(seed, "docs/agents/note.md", "note v0\n")
    _write(seed, "tests/test_reads_settings.py", READS_SETTINGS)
    _write(seed, "tests/test_names_settings.py", NAMES_SETTINGS)
    _write(seed, "tests/test_unrelated.py", UNRELATED)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "initial")
    _git(seed, "push", "-q", "origin", "main")
    checkout = _clone(bare, tmp_path / "checkout")
    lock = tmp_path / "var" / "locks" / "merge.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    return bare, checkout, lock


def _main(bare: Path) -> str:
    return _git(bare, "rev-parse", "main").stdout.strip()


def _count(bare: Path) -> int:
    return int(_git(bare, "rev-list", "--count", "main").stdout.strip())


def _args(paths, message, checkout, lock) -> argparse.Namespace:
    return argparse.Namespace(paths=paths, message=message, cwd=checkout, lock=lock, timeout=10,
                              state_entry=None, state_as="", review_entry=None)


# Row 1 -- a settings commit alone, with the merge line held by a live holder: lands at once, green.
def test_row1_settings_commit_lands_without_the_merge_line(tmp_path: Path) -> None:
    bare, checkout, lock = _world(tmp_path)
    _write(checkout, SETTINGS, json.dumps({"update_rule": "word", "extra": 1}) + "\n")
    commits_before = _count(bare)
    holder = MergeLock(lock)
    holder.acquire(max_age_seconds=600)  # a merge holds the line for its whole suite
    try:
        result = run_commit([SETTINGS], "settings", cwd=checkout, lock_path=lock, lock_timeout=10)
    finally:
        holder.release()

    assert result.ok, result.render()
    assert _count(bare) == commits_before + 1, "the settings commit did not land on the remote"
    assert result.quick_check["status"] == "green"
    assert result.quick_check["files"] == 1
    assert result.quick_check["tests"] == 2  # test_reads_settings.py and test_names_settings.py
    assert result.quick_check["seconds"] < 90


def test_row1_cli_line_carries_the_quick_check_for_the_result_record(tmp_path: Path, capsys) -> None:
    bare, checkout, lock = _world(tmp_path)
    _write(checkout, SETTINGS, json.dumps({"update_rule": "word", "extra": 2}) + "\n")

    rc = run_commit_command(_args([SETTINGS], "settings", checkout, lock))

    out = capsys.readouterr().out
    assert rc == 0, out
    quick = detach._quick_check_of(out)
    assert quick is not None and quick["status"] == "green" and quick["tests"] == 2


# Row 3 -- a settings file with a trailing comma: refused, one line naming the file, nothing pushed.
def test_row3_broken_json_is_refused_and_names_the_file(tmp_path: Path, capsys) -> None:
    bare, checkout, lock = _world(tmp_path)
    head_before = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    main_before = _main(bare)
    _write(checkout, SETTINGS, '{"update_rule": "word",}\n')

    result = run_commit([SETTINGS], "broken settings", cwd=checkout, lock_path=lock, lock_timeout=10)

    assert not result.ok, result.render()
    assert result.reason == "quick-check-red", result.render()
    assert SETTINGS in result.message
    assert "\n" not in result.message
    assert result.quick_check["status"] == "red"
    assert _main(bare) == main_before, "a refused commit reached the remote"
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == head_before, "the local commit was kept"

    rc = run_commit_command(_args([SETTINGS], "broken settings", checkout, lock))
    out = capsys.readouterr().out
    assert rc == 1
    assert [line for line in out.splitlines() if line.startswith("not committed")] == [
        line for line in out.splitlines() if SETTINGS in line]


# Row 4 -- a test that reads the settings file fails: refused, the line carries the failing node id.
def test_row4_failing_reader_test_is_refused_with_its_node_id(tmp_path: Path) -> None:
    bare, checkout, lock = _world(tmp_path)
    main_before = _main(bare)
    _write(checkout, SETTINGS, json.dumps({"update_rule": "home"}) + "\n")  # valid JSON, wrong value

    result = run_commit([SETTINGS], "settings", cwd=checkout, lock_path=lock, lock_timeout=10)

    assert not result.ok, result.render()
    assert result.reason == "quick-check-red", result.render()
    assert "tests/test_reads_settings.py::test_reads_settings" in result.message
    assert "tests/test_names_settings.py" not in result.message
    assert "\n" not in result.message
    assert _main(bare) == main_before, "a refused commit reached the remote"


# A Markdown-only commit: no tests, no JSON load, no quick_check key (today's behaviour).
def test_markdown_only_commit_has_no_quick_check(tmp_path: Path, capsys) -> None:
    bare, checkout, lock = _world(tmp_path)
    _write(checkout, "docs/agents/note.md", "note v1\n")

    rc = run_commit_command(_args(["docs/agents/note.md"], "note", checkout, lock))

    out = capsys.readouterr().out
    assert rc == 0, out
    assert "commit-quick-check" not in out
    assert detach._quick_check_of(out) is None


def test_markdown_only_run_commit_leaves_quick_check_unset(tmp_path: Path) -> None:
    _bare, checkout, lock = _world(tmp_path)
    _write(checkout, "docs/agents/note.md", "note v2\n")

    result = run_commit(["docs/agents/note.md"], "note", cwd=checkout, lock_path=lock, lock_timeout=10)

    assert result.ok, result.render()
    assert result.quick_check is None
