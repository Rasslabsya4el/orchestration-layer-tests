"""Issue 91 unit 2: a target's own `tests/conftest.py` survives a `core update` byte for byte, and the update's
commit does not touch it. The file is not a line of the core manifest, so the update never writes it; this test
pins that. Real git on a tmp target (bare origin + clone); the home is a fake `gh` runner, no network. Reuses the
helpers of the #28 family, as the #44 standards test does."""

from __future__ import annotations

from pathlib import Path

from tests import test_issue28_core_update_failed_commit as base
from tools.agentops.core_update import run_core_update

REPO = Path(__file__).resolve().parents[1]
MANIFEST_FILE = "tools/agentops/core_manifest.txt"
CONFTEST = "tests/conftest.py"
HOME_TEST = "tests/test_home_shipped.py"
OWN_CONFTEST = (
    "# the target's own test settings, kept as they are\n"
    "import pytest\n"
    "\n"
    "\n"
    "@pytest.fixture(autouse=True)\n"
    "def _own_fixture():\n"
    "    yield\n"
)


def _run(root: Path, home_files: dict[str, str]):
    # the real default step runner, as `core update` runs it
    return run_core_update(root, gh_runner=base._fake_gh(home_files), live_check=lambda: False)


def _last_commit_paths(root: Path) -> list[str]:
    out = base._git(root, "show", "--name-only", "--format=", "HEAD").stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def _tests_tree(root: Path) -> dict[str, bytes]:
    # every file under the target's tests/ folder, by repo path, with its bytes
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted((root / "tests").rglob("*")) if p.is_file()}


# Case 1 -- the target's own tests/conftest.py is kept byte for byte, 0 files under tests/ differ, and the
# update's commit does not name it.
def test_target_own_conftest_is_kept_byte_for_byte_by_core_update(tmp_path: Path) -> None:
    root = base._target(tmp_path)
    base._write(root, CONFTEST, OWN_CONFTEST)
    assert base._git(root, "add", "-A").returncode == 0
    assert base._git(root, "commit", "-m", "target's own tests/conftest.py").returncode == 0
    assert base._git(root, "push", "origin", "main").returncode == 0
    original = (root / CONFTEST).read_bytes()
    tests_before = _tests_tree(root)

    head_before = base._git(root, "rev-parse", "HEAD").stdout.strip()
    _run(root, {**base.NEW_HOME_FILES, HOME_TEST: "def test_home():\n    pass\n"})

    assert base._git(root, "rev-parse", "HEAD").stdout.strip() != head_before, "the update made no commit"
    assert (root / CONFTEST).read_bytes() == original
    tests_after = _tests_tree(root)
    differing = sorted(rel for rel in tests_before if tests_after.get(rel) != tests_before[rel])
    assert differing == [], f"core update changed files under tests/ that the target had: {differing}"
    assert set(tests_after) - set(tests_before) == {HOME_TEST}, "only the home's shipped test is new under tests/"
    assert CONFTEST not in _last_commit_paths(root)
    assert base._git(root, "status", "--porcelain").stdout.strip() == ""


# Case 2 -- the core manifest never names tests/conftest.py: the target's own settings file is not core.
def test_conftest_is_not_a_line_of_the_core_manifest() -> None:
    lines = [line.strip() for line in (REPO / MANIFEST_FILE).read_text(encoding="utf-8").splitlines()]
    assert CONFTEST not in lines
