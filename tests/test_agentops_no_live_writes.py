"""#417: a merge call without its own paths writes under the session tmp dir, never the live
`var/agentops/` (merge.log, merge_suite/, results/)."""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

from tests._merge_paths import _agentops_merge_paths_in_tmp  # noqa: F401
from tools.agentops import detach, merge

LIVE = merge.ROOT / "var" / "agentops"


def test_paths_are_redirected_in_process_and_child(_agentops_merge_paths_in_tmp: Path):
    base = _agentops_merge_paths_in_tmp
    assert merge._merge_log_path() == base / "merge.log"
    assert merge._suite_output_dir() == base / "merge_suite"
    assert detach.result_path_for("merge", "b").parent == base / "results"
    code = ("from tools.agentops import merge; print(merge._merge_log_path())")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(merge.ROOT), env=os.environ.copy(),
                         capture_output=True, text=True, timeout=60).stdout.strip()
    assert Path(out) == base / "merge.log"


def test_merge_log_append_leaves_live_var_untouched(_agentops_merge_paths_in_tmp: Path):
    """Only this test's own marked line counts: a real merge of another session may append to the
    live log meanwhile, so no raw size compare."""
    mark = f"origin/no-live-writes-417-{uuid.uuid4().hex}"
    live_log = LIVE / "merge.log"
    merge._append_merge_log(mark, merge.MergeResult(ok=False, reason="red-suite"))
    live_text = live_log.read_text(encoding="utf-8", errors="replace") if live_log.exists() else ""
    assert mark not in live_text
    assert mark in (_agentops_merge_paths_in_tmp / "merge.log").read_text(encoding="utf-8")
