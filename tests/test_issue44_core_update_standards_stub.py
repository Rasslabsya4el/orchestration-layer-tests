"""Issue 44: `core update` writes the rendered CODING_STANDARDS.md stub into a target that has no
CODING_STANDARDS.md, in the same commit as the other files it touches; an existing file is kept byte
for byte. The write is a post-copy step (`run_update_steps`), so the UPDATED code writes it even when
the target updates from old code. Real git on a tmp target (bare origin + clone); the home is a fake
`gh` runner, no network. Reuses the helpers of the #28 family."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import test_issue28_core_update_failed_commit as base
from tools.agentops import core_update
from tools.agentops.core_update import CoreUpdateError, run_core_update

REPO = Path(__file__).resolve().parents[1]
STANDARDS = "CODING_STANDARDS.md"
CORE_FILE = "docs/agents/core-standards.md"
STUB_FILE = "config/core_templates/CODING_STANDARDS.md.stub"
TEST_FILE = "tests/test_coding_standards.py"
CORE_TEXT = (
    "# Coding standards: the generic rules\n"
    "\n"
    "Intro line that is not a rule.\n"
    "\n"
    "- Rule one stays in part one. (docs/agents/source-a.md)\n"
    "- Rule two stays in part one. (docs/agents/source-b.md)\n"
)
RULES = "- Rule one stays in part one. (docs/agents/source-a.md)\n- Rule two stays in part one. (docs/agents/source-b.md)"
OWN_TEXT = "# Ours\n\nThe target's own standards file, kept as it is.\n"


def _stub() -> str:
    return (REPO / STUB_FILE).read_text(encoding="utf-8")


def _home_files() -> dict[str, str]:
    return {**base.NEW_HOME_FILES, CORE_FILE: CORE_TEXT, STUB_FILE: _stub(),
            TEST_FILE: "def test_standards_file_exists():\n    pass\n"}


def _run(root: Path, home_files: dict[str, str]):
    # the real default step runner: the post-copy steps run as `core update` runs them
    return run_core_update(root, gh_runner=base._fake_gh(home_files), live_check=lambda: False)


def _last_commit_paths(root: Path) -> list[str]:
    out = base._git(root, "show", "--name-only", "--format=", "HEAD").stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


# Case 1 -- a target without CODING_STANDARDS.md gets the rendered stub, in the same commit as the test that checks it.
def test_case1_target_without_standards_file_gets_rendered_stub(tmp_path: Path) -> None:
    root = base._target(tmp_path)
    head_before = base._git(root, "rev-parse", "HEAD").stdout.strip()
    assert not (root / STANDARDS).exists()

    _run(root, _home_files())

    standards = root / STANDARDS
    assert standards.is_file(), "core update did not write CODING_STANDARDS.md"
    text = standards.read_text(encoding="utf-8")
    assert "{core_rules}" not in text
    # part 1 holds the core file's `- ` rule lines, rendered as port does; part 2 is the stub's own sentence
    assert text == _stub().replace("{core_rules}", RULES)
    part_two = text.split("## 2. This repo's rules", 1)[1]
    assert part_two.strip() == "This repo adds no rules yet."
    # the file and the test that checks it arrive in the update's one commit, which the update made
    assert base._git(root, "rev-parse", "HEAD").stdout.strip() != head_before
    assert STANDARDS in _last_commit_paths(root)
    assert TEST_FILE in _last_commit_paths(root)
    assert base._git(root, "status", "--porcelain").stdout.strip() == ""


# Case 2 -- a target with its own CODING_STANDARDS.md keeps it byte for byte, and the commit does not touch it.
def test_case2_target_with_own_standards_file_is_kept_byte_for_byte(tmp_path: Path) -> None:
    root = base._target(tmp_path)
    base._write(root, STANDARDS, OWN_TEXT)
    assert base._git(root, "add", "-A").returncode == 0
    assert base._git(root, "commit", "-m", "target's own standards file").returncode == 0
    assert base._git(root, "push", "origin", "main").returncode == 0
    original = (root / STANDARDS).read_bytes()

    _run(root, _home_files())

    assert (root / STANDARDS).read_bytes() == original
    assert STANDARDS not in _last_commit_paths(root)
    assert base._git(root, "status", "--porcelain").stdout.strip() == ""


# Case 3 -- a step failure after the stub was written: the update is undone, no CODING_STANDARDS.md is left,
# the tree is clean and HEAD is where it was.
def test_case3_step_failure_leaves_no_standards_file_and_a_clean_tree(tmp_path: Path, monkeypatch) -> None:
    root = base._target(tmp_path)
    head_before = base._git(root, "rev-parse", "HEAD").stdout.strip()
    real_steps = core_update.run_update_steps
    seen: dict[str, bool] = {}

    def failing_steps(target: Path) -> list[str]:
        real_steps(target)
        seen["written"] = (target / STANDARDS).is_file()
        raise RuntimeError("step boom")

    monkeypatch.setattr(core_update, "run_update_steps", failing_steps)
    with pytest.raises(CoreUpdateError):
        _run(root, _home_files())

    assert seen == {"written": True}, "the step did not write the stub before it failed"
    assert not (root / STANDARDS).exists()
    assert base._git(root, "status", "--porcelain").stdout.strip() == ""
    assert base._git(root, "rev-parse", "HEAD").stdout.strip() == head_before


# Seam -- the post-copy step itself writes the stub into a target without the file (the UPDATED code does it,
# so a target updating from old code gets the file in the same update), and returns its path.
def test_step_writes_stub_when_absent_and_returns_its_path(tmp_path: Path) -> None:
    root = base._target(tmp_path)
    base._write(root, CORE_FILE, CORE_TEXT)
    base._write(root, STUB_FILE, _stub())

    written = core_update.run_update_steps(root)

    assert STANDARDS in written
    assert (root / STANDARDS).read_text(encoding="utf-8") == _stub().replace("{core_rules}", RULES)
