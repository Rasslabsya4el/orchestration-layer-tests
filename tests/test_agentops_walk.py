"""tools/agentops/util.py `walk_paths` (#360): a junction is yielded once and never descended."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tools.agentops.util import walk_paths

pytestmark = pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows-only")


def _mklink(link: Path, target: Path) -> None:
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=True)


def test_self_loop_junction_terminates(tmp_path: Path):
    a = tmp_path / "a"
    a.mkdir()
    (a / "f.txt").write_text("x", encoding="utf-8")
    _mklink(a / "loop", a)
    try:
        seen = list(walk_paths(tmp_path))
    finally:
        os.rmdir(a / "loop")
    assert seen.count(a / "loop") == 1
    assert a / "f.txt" in seen
    assert not any("loop" in p.parts[len(a.parts):][:1] and len(p.parts) > len(a.parts) + 1 for p in seen)


def test_junction_to_sibling_not_descended(tmp_path: Path):
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "hidden.txt").write_text("x", encoding="utf-8")
    (tmp_path / "a").mkdir()
    _mklink(tmp_path / "a" / "j", tmp_path / "b")
    try:
        seen = list(walk_paths(tmp_path / "a"))
    finally:
        os.rmdir(tmp_path / "a" / "j")
    assert seen == [tmp_path / "a" / "j"]
