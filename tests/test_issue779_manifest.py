"""#779 row 14: every new core file of the ticket is in the core manifest, and the core check stays clean."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tools.agentops.core_check import DEFAULT_ROOT, MANIFEST_PATH, read_manifest, run_check

# a path is core when it lies where the manifest already keeps core files of that kind
_CORE_DIRS = ("tools/agentops/",)
_CORE_SUFFIXES = (".py", ".json", ".ps1")
_NOT_CORE = {"tools/agentops/test_reads_plugin.py", "tools/agentops/test_referrers.py", "tools/agentops/test_select.py",
             # #1005: this repo's own session statuses (names the heavy tool, jobs.py, watcher), not core
             "tools/agentops/agent_status.py",
             # #1114: seeds the orchestration home from this repo and names home paths (L1/L3 lines), not core; row 5: no manifest file changes
             "tools/agentops/home_seed.py"}


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(DEFAULT_ROOT), *args], capture_output=True, text=True, check=True).stdout


def _added_files() -> list[str]:
    try:
        base = _git("merge-base", "origin/main", "HEAD").strip()
    except (subprocess.CalledProcessError, OSError):
        return []
    return [p for p in _git("diff", "--name-only", "--diff-filter=A", f"{base}..HEAD").splitlines() if p]


def test_every_added_core_file_is_in_the_manifest():
    manifest = set(read_manifest())
    expected = [p for p in _added_files()
                if p.startswith(_CORE_DIRS) and p.endswith(_CORE_SUFFIXES) and "/" not in p[len("tools/agentops/"):]
                and p not in _NOT_CORE]
    missing = [p for p in expected if p not in manifest]
    assert not missing, missing


def test_the_two_machine_mode_modules_are_manifest_lines():
    manifest = read_manifest()
    assert "tools/agentops/machine_mode.py" in manifest
    assert "tools/agentops/hold.py" in manifest
    assert MANIFEST_PATH.exists()


def test_no_hook_and_no_orchestration_key_was_added():
    # static, not a diff against origin/main: a later ticket may add its own key (#898's start_admission)
    keys = (DEFAULT_ROOT / "config" / "orchestration.json").read_text(encoding="utf-8")
    hooks = (DEFAULT_ROOT / "config" / "core_templates" / "settings.hooks.json").read_text(encoding="utf-8")
    assert "machine_mode" not in keys and "machine_dir" not in keys and "AGENTOPS_MACHINE_DIR" not in keys
    assert "machine_mode" not in hooks and "machine play" not in hooks


def test_core_check_is_clean_with_the_machine_docs():
    rows, ok = run_check()
    assert ok
    by_path = {row.path: row for row in rows}
    for path in ("tools/agentops/machine_mode.py", "tools/agentops/hold.py", "docs/agents/brain.md"):
        row = by_path[path]
        assert (row.counts.get("L1", 0), row.counts.get("L2", 0), row.counts.get("L3", 0), row.h) == (0, 0, 0, 0), path
