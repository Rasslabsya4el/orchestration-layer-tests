"""Issue 170 unit 2: a core update writes exactly the three keys of `config/orchestration.json` that the code rule
names (`core_version`, `core_version_before`, `core_version_count`), and no other key.

Rows: 1 a fixture target with keys `core_version`, `areas`, `monitor` takes a new home head: the changed keys are
exactly the three named ones and `areas` and `monitor` are byte-equal; 2 a stand-in `_write_core_version` that also
writes a fourth key makes the same check fail.

Seam: `run_core_update` on a real local git target (bare origin + clone); the home is `FakeHome` of unit 148 (no
network, no gh); the count is injected (`home_count`), the post-copy steps and the monitor install are stubbed."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.test_issue148_tree_update import FakeHome, MANIFEST, NEW_HEAD, OLD_HEAD, ORCH_JSON, _git, _manifest_bytes, _write
from tools.agentops import core_update
from tools.agentops.core_update import run_core_update

NAMED = {"core_version", "core_version_before", "core_version_count"}
ABSENT = object()
FILE = "docs/agents/one.md"
COUNT = 41
ORCH = {
    "update_rule": "word",
    "home": "owner/home",
    "core_version": OLD_HEAD,
    "areas": ["orchestration", "builds"],
    "monitor": {"mode": "normal", "port": 8765},
}
_ORIGINAL_WRITE = core_update._write_core_version


def _target(tmp_path: Path, orch: dict) -> Path:
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    root = tmp_path / "target"
    root.mkdir()
    for args in (("init", "-b", "main"), ("config", "user.email", "t@example.com"), ("config", "user.name", "t"),
                 ("config", "core.autocrlf", "false"), ("remote", "add", "origin", str(bare))):
        assert _git(root, *args).returncode == 0
    _write(root, ".gitignore", b"var/\nartifacts/\n")
    _write(root, ORCH_JSON, (json.dumps(orch, indent=2) + "\n").encode("utf-8"))
    _write(root, MANIFEST, _manifest_bytes([FILE]))
    _write(root, FILE, b"same\n")
    assert _git(root, "add", "-A").returncode == 0
    assert _git(root, "commit", "-q", "-m", "target before the update").returncode == 0
    assert _git(root, "push", "-q", "-u", "origin", "main").returncode == 0
    return root


def _orch(root: Path) -> dict:
    return json.loads((root / ORCH_JSON).read_text(encoding="utf-8"))


def _update(root: Path):
    home = FakeHome({FILE: b"same\n"})
    return run_core_update(root, gh_runner=home, live_check=lambda: False, step_runner=lambda _root: [],
                           home_count=lambda sha: COUNT, monitor_runner=lambda _root, _source: None)


def changed_keys(before: dict, after: dict) -> set[str]:
    """The top-level keys whose value differs, a key added or removed included."""
    keys = set(before) | set(after)
    return {key for key in keys if before.get(key, ABSENT) != after.get(key, ABSENT)}


def assert_writes_only_named_keys(before: dict, after: dict) -> set[str]:
    """The one check: every changed key is one of the three the code rule names. Returns the changed keys."""
    changed = changed_keys(before, after)
    assert changed <= NAMED, f"the update wrote keys the code rule does not name: {sorted(changed - NAMED)}"
    return changed


def _stand_in(root: Path, head: str, count: int | None = None) -> bool:
    """`_write_core_version` plus a fourth key: the deliberate write the check must reject."""
    wrote = _ORIGINAL_WRITE(root, head, count)
    if wrote:
        data = _orch(root)
        data["core_version_note"] = "written by the stand-in"
        (root / ORCH_JSON).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return wrote


# Row 1 -- the update writes exactly the three named keys; the other keys are byte-equal.
def test_update_writes_exactly_the_three_named_keys(tmp_path: Path) -> None:
    root = _target(tmp_path, ORCH)
    before = _orch(root)

    _update(root)

    after = _orch(root)
    assert assert_writes_only_named_keys(before, after) == NAMED
    assert after["core_version"] == NEW_HEAD
    assert after["core_version_before"] == OLD_HEAD
    assert after["core_version_count"] == COUNT
    for key in ("areas", "monitor"):
        assert json.dumps(after[key], ensure_ascii=False, sort_keys=True) == \
            json.dumps(before[key], ensure_ascii=False, sort_keys=True), key


# Row 2 -- a stand-in that also writes a fourth key makes the same check fail.
def test_stand_in_writing_a_fourth_key_fails_the_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _target(tmp_path, ORCH)
    before = _orch(root)
    monkeypatch.setattr(core_update, "_write_core_version", _stand_in)

    _update(root)

    after = _orch(root)
    assert "core_version_note" in after, "the stand-in did not run"
    with pytest.raises(AssertionError, match="core_version_note"):
        assert_writes_only_named_keys(before, after)
