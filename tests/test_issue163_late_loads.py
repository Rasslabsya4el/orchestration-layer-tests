"""#163: the two lazy imports of the merge path after the lock wait are loaded BEFORE the wait.

``merge_priority.gh_marks`` imports ``core_table.GhIssueReader`` and ``merge_priority.gh_blockers`` imports
``dispatch.parse_candidates`` on first use. The holder that has just left the wait reads another waiter's labels and
blockers (``Lock.refresh_priorities``), so a batch the leader landed meanwhile and that rewrote those files would pair
a module of the new files with ``core_update`` of the old ones (an ImportError, logged as ``priority not read`` /
``blockers not read``).

Case (rows 1 and 2): the #156 world (a copy of the package with the real lock protocol); a second live waiter is in the
queue with an issue of its own; while the child waits, ``core_table.py`` and ``dispatch.py`` on disk change to import a
name only the new ``core_update`` has; the lock is released and the child takes it. The merge log holds no
``priority not read`` / ``blockers not read`` line and the child's gh stand-in was asked for both reads.

Test-only seam: under pytest the real ``gh`` reads refuse (``PYTEST_CURRENT_TEST``); the child runs with that variable
removed from its own environment and ``merge_priority._gh_raw`` replaced by a recording stand-in, so the suite's
environment is untouched."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from tests._merge_paths import _agentops_merge_paths_in_tmp  # noqa: F401
from tests.test_issue156_preload import NEW_NAME, _World

_WAITER = textwrap.dedent("""
    import argparse, json, sys
    sys.path.insert(0, {pkg!r})
    from pathlib import Path
    import tools.agentops.core_update  # a CLI entry has loaded it before any wait
    from tools.agentops import merge, merge_priority
    CALLS = Path({calls!r})

    def fake_gh(args):
        with open(CALLS, "a", encoding="utf-8") as fh:
            fh.write(" ".join(args) + chr(10))
        return "[]" if args[0] == "api" else json.dumps({{"number": 8, "blockedBy": {{"nodes": []}}}})

    merge_priority._gh_raw = fake_gh
    ns = argparse.Namespace(branch="b", cwd=Path({home!r}), suite={suite!r}, lock=Path({lock!r}), timeout=120,
                            queue_checked=True, issue=[])
    sys.exit(merge.run_merge_command(ns))
""")


def _patch_to_need_new_core_update(path: Path, old: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, (path, old)
    path.write_text(text.replace(old, old.replace(" import ", f" import {NEW_NAME}, ", 1), 1), encoding="utf-8")


def test_rows1_2_the_holder_that_reads_another_waiters_issue_after_the_swap_loads_no_new_module(tmp_path: Path):
    world = _World(tmp_path)
    calls = tmp_path / "gh_calls.txt"
    script = _WAITER.format(pkg=str(world.pkg), home=str(world.home), lock=str(world.lock), calls=str(calls),
                            suite=f'"{sys.executable}" --version')
    env = {k: v for k, v in world.env.items() if k != "PYTEST_CURRENT_TEST"}
    proc = subprocess.Popen([sys.executable, "-c", script, "--cwd", str(world.home)], cwd=world.pkg, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        ticket = world.wait_until_queued()
        other = ticket.with_name(f"{int(ticket.name.split('-')[0]) + 1:020d}-{os.getpid()}-999")
        other.write_text(json.dumps({"kind": "merge", "branch": "other", "suite": "not-the-same-suite",
                                     "remote": "origin", "main": "main", "cwd": str(world.home),
                                     "issues": [8], "priority": None}), encoding="utf-8")
        agentops = world.pkg / "tools" / "agentops"
        agentops_core_update = agentops / "core_update.py"
        with open(agentops_core_update, "a", encoding="utf-8") as fh:
            fh.write(f"\n{NEW_NAME} = 1\n")
        _patch_to_need_new_core_update(agentops / "core_table.py", "from .core_update import ")
        _patch_to_need_new_core_update(agentops / "dispatch.py", "from .core_update import ")
        world.lock.unlink()  # the holder lets go with no result: the child takes the lock after the swap
        out, _ = proc.communicate(timeout=100)
    finally:
        if proc.poll() is None:
            proc.kill()
    log = Path(os.environ["AGENTOPS_MERGE_LOG"])
    log_text = log.read_text(encoding="utf-8") if log.exists() else ""
    assert "ImportError" not in out and "Traceback" not in out, out
    assert "ImportError" not in log_text and "not read" not in log_text, log_text
    assert proc.returncode == 0 and "merged: b -> main" in out, out
    asked = calls.read_text(encoding="utf-8") if calls.exists() else ""
    assert "issues/8/timeline" in asked and "issue view 8" in asked, asked  # both late loads were really reached
