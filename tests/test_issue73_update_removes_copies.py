"""Issue 73 unit 2: `core update` removes the target's same-named copies of the home's tests. A target keeps only
tests of its own files: a `tests/test_*.py` that is not in the new manifest, imports only manifest modules and has
a same-named file in the home's tests folder is deleted by the update (counted in `removed`, committed with it);
without a home copy it is kept and named as kept; one that also touches the target's own files is never touched.
Real git on a tmp target (bare origin + clone); the home is a fake `gh` runner, no network."""

from __future__ import annotations

from pathlib import Path

from tests import test_issue28_core_update_failed_commit as base
from tests.home_tree_fake import tree_reply
from tools.agentops.core_update import run_core_update

CORE_MODULE = "tools/agentops/mod.py"
ONLY_CORE = "from tools.agentops import mod\n\n\ndef test_it():\n    assert mod\n"
WITH_OWN = "from tools.agentops import mod\nimport tools.mine\n\n\ndef test_it():\n    assert mod, tools.mine\n"


def _home(manifest_tests: dict[str, str] | None = None) -> dict[str, str]:
    return {**base.NEW_HOME_FILES, CORE_MODULE: "x = 1\n", **(manifest_tests or {})}


def _target(tmp_path: Path, tests: dict[str, str]) -> Path:
    """The #28 target with ``tests`` (name -> text) committed under tests/ and the target's own module tools/mine.py."""

    root = base._target(tmp_path)
    base._write(root, "tools/mine.py", "y = 2\n")
    for name, text in tests.items():
        base._write(root, f"tests/{name}", text)
    assert base._git(root, "add", "-A").returncode == 0
    assert base._git(root, "commit", "-m", "target tests").returncode == 0
    assert base._git(root, "push", "origin", "main").returncode == 0
    return root


def _update(root: Path, home_tests: set[str], home: dict[str, str] | None = None):
    home = home or _home()
    gh = base._fake_gh(home)

    def run(args: list[str]):
        if "/git/trees/" in args[1]:  # the home's tree (#148): its files and its tests folder
            files = {rel: text.encode("utf-8") for rel, text in home.items()}
            return tree_reply({**files, **{f"tests/{name}": b"home test\n" for name in home_tests}})
        return gh(args)

    return run_core_update(root, gh_runner=run, live_check=lambda: False, step_runner=lambda _root: [])


def test_copy_with_a_home_file_is_removed(tmp_path: Path) -> None:
    root = _target(tmp_path, {"test_x.py": ONLY_CORE})

    result = _update(root, {"test_x.py"})

    assert "tests/test_x.py" in result.removed
    assert not (root / "tests/test_x.py").exists()
    assert "tests/test_x.py" in result.message
    assert base._git(root, "status", "--porcelain").stdout.strip() == ""
    assert "tests/test_x.py" in base._git(root, "show", "--stat", "--format=", "HEAD").stdout


def test_copy_without_a_home_file_is_kept_and_named(tmp_path: Path) -> None:
    root = _target(tmp_path, {"test_x.py": ONLY_CORE})

    result = _update(root, set())

    assert "tests/test_x.py" not in result.removed
    assert (root / "tests/test_x.py").is_file()
    assert "kept (no home copy): test_x.py" in result.message


def test_test_that_touches_the_targets_own_files_is_kept(tmp_path: Path) -> None:
    root = _target(tmp_path, {"test_x.py": WITH_OWN})

    result = _update(root, {"test_x.py"})

    assert "tests/test_x.py" not in result.removed
    assert (root / "tests/test_x.py").is_file()
    assert "kept" not in result.message


def test_manifest_test_is_overwritten_not_removed(tmp_path: Path) -> None:
    root = _target(tmp_path, {"test_m.py": ONLY_CORE})
    home = _home({"tests/test_m.py": "from tools.agentops import mod\n# home v2\n"})

    result = _update(root, {"test_m.py"}, home)

    assert "tests/test_m.py" not in result.removed
    assert "tests/test_m.py" in result.changed
    assert (root / "tests/test_m.py").read_text(encoding="utf-8").endswith("# home v2\n")


def test_second_update_is_a_no_op(tmp_path: Path) -> None:
    root = _target(tmp_path, {"test_x.py": ONLY_CORE, "test_y.py": ONLY_CORE})
    _update(root, {"test_x.py"})
    head = base._git(root, "rev-parse", "HEAD").stdout

    result = _update(root, {"test_x.py"})

    assert result.removed == [] and result.changed == []
    assert base._git(root, "rev-parse", "HEAD").stdout == head
    assert (root / "tests/test_y.py").is_file()
