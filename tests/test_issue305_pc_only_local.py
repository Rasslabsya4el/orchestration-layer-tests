"""#305: a repo lists its own PC-only tests in ``tests/pc_only.local.txt`` beside the core's ``tests/pc_only.txt``.

Seams: ``load_pc_only`` (the union of the two lists), the core manifest (the local file is not in it), ``run_core_update``
(an existing local file stays byte for byte), and the shard plugin's ``drop_pc_only`` (a local-list test leaves a GitHub
part like a core-list test)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import test_issue28_core_update_failed_commit as base
from tools.agentops.core_update import run_core_update
from tools.agentops.pc_only import drop_pc_only, load_pc_only

REPO = Path(__file__).resolve().parents[1]
LOCAL = "tests/pc_only.local.txt"


def _put(root: Path, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8")


def test_only_the_local_list_is_read(tmp_path):
    _put(tmp_path, LOCAL, "tests/test_a.py\n")
    assert load_pc_only(tmp_path) == {"tests/test_a.py"}


def test_both_lists_give_the_union_and_neither_gives_nothing(tmp_path):
    assert load_pc_only(tmp_path) == set()
    _put(tmp_path, "tests/pc_only.txt", "# core\ntests/test_core.py\n")
    _put(tmp_path, LOCAL, "# mine\n\n  tests/test_a.py  \ntests/test_core.py\n")
    assert load_pc_only(tmp_path) == {"tests/test_core.py", "tests/test_a.py"}


def test_an_unreadable_local_list_raises(tmp_path):
    (tmp_path / "tests" / "pc_only.local.txt").mkdir(parents=True)  # exists, cannot be read as a file
    with pytest.raises(OSError):
        load_pc_only(tmp_path)


def test_a_local_list_test_leaves_a_github_part_like_a_core_list_test(tmp_path):
    _put(tmp_path, "tests/pc_only.txt", "tests/test_core.py\n")
    _put(tmp_path, LOCAL, "tests/test_a.py\n")
    items = [SimpleNamespace(nodeid=f"tests/{n}.py::t") for n in ("test_core", "test_a", "test_other")]
    kept, dropped = drop_pc_only(items, load_pc_only(tmp_path))
    assert [i.nodeid for i in kept] == ["tests/test_other.py::t"]
    assert len(dropped) == 2


def test_the_local_list_is_not_a_core_manifest_file():
    manifest = (REPO / "tools" / "agentops" / "core_manifest.txt").read_text(encoding="utf-8").splitlines()
    assert LOCAL not in [line.strip() for line in manifest]
    assert "tests/pc_only.txt" in [line.strip() for line in manifest]


def test_a_core_update_leaves_an_existing_local_list_byte_for_byte(tmp_path):
    root = base._target(tmp_path)
    body = "# mine\r\ntests/test_a.py\ntests/test_b.py".encode("utf-8")  # CRLF, no final newline: bytes kept as written
    (root / "tests").mkdir(exist_ok=True)
    (root / LOCAL).write_bytes(body)
    assert base._git(root, "add", "-A").returncode == 0
    assert base._git(root, "commit", "-m", "local list").returncode == 0
    assert base._git(root, "push", "origin", "main").returncode == 0
    run_core_update(root, gh_runner=base._fake_gh(base.NEW_HOME_FILES), live_check=lambda: False,
                    step_runner=lambda _root: [])
    assert (root / LOCAL).read_bytes() == body
