"""Issue 49 units 1 and 2. Unit 1: a core update takes the target's merge-then-checkout lock BEFORE it writes a
file and holds it through the push. Busy lock + bounded wait -> refused `merge running`, 0 files written,
`core_version` unchanged; a free (or soon freed) lock -> the update lands and does not wait on itself.
Unit 2: a killed update (the process gone between its writes and its commit) leaves a snapshot in
`var/agentops/core_update_snapshot.json`; the finally of the update, or the next run, puts the paths back.
Real git on a tmp target (bare origin + clone); the home is a fake `gh` runner; the lock holder is a real
other process."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.home_tree_fake import tree_reply
from tools.agentops import core_targets, core_update
from tools.agentops.core_update import CoreUpdateError, run_core_update

REPO = Path(__file__).resolve().parents[1]
WAIT_S = 30.0  # every wait on a helper process ends after this
OLD_VERSION = "a" * 40
NEW_VERSION = "b" * 40
MANIFEST = "tools/agentops/core_manifest.txt"
ORCH_JSON = "config/orchestration.json"
OLD_FILES = {"docs/agents/one.md": "one v1\n", "docs/agents/two.md": "two v1\n"}
NEW_HOME_FILES = {"docs/agents/one.md": "one v2\n", "docs/agents/two.md": "two v1\n",
                  "docs/agents/three.md": "three v2 (new)\n"}

HOLDER = """
import sys, time
from pathlib import Path
from tools.agentops.merge import merge_then_checkout_locked
with merge_then_checkout_locked(Path(sys.argv[1])):
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
"""


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _manifest_text(files: dict[str, str]) -> str:
    return "".join(f"{rel}\n" for rel in [MANIFEST, *files])


def _fake_gh(home_files: dict[str, str]):
    contents = {rel: text.encode("utf-8") for rel, text in home_files.items()}
    contents[MANIFEST] = _manifest_text(home_files).encode("utf-8")

    def run(args: list[str]):
        path = args[1]
        if "/commits/main" in path:
            return SimpleNamespace(returncode=0, stdout=NEW_VERSION + "\n", stderr="")
        if "/git/trees/" in path:  # #148: the home's tree, one call
            return tree_reply(contents)
        rel = path.split("/contents/", 1)[1].split("?ref=", 1)[0]
        body = base64.b64encode(contents[rel]).decode("ascii")
        return SimpleNamespace(returncode=0, stdout=body + "\n", stderr="")

    return run


def _target(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    root = tmp_path / "target"
    root.mkdir()
    for args in (("init", "-b", "main"), ("config", "user.email", "t@example.com"),
                 ("config", "user.name", "t"), ("config", "core.autocrlf", "false"),
                 ("remote", "add", "origin", str(bare))):
        assert _git(root, *args).returncode == 0
    _write(root, ".gitignore", "var/\nartifacts/\n")
    _write(root, ORCH_JSON, json.dumps({"update_rule": "word", "home": "owner/home",
                                        "core_version": OLD_VERSION}, indent=2) + "\n")
    _write(root, MANIFEST, _manifest_text(OLD_FILES))
    for rel, text in OLD_FILES.items():
        _write(root, rel, text)
    assert _git(root, "add", "-A").returncode == 0
    assert _git(root, "commit", "-m", "target before the update").returncode == 0
    assert _git(root, "push", "-u", "origin", "main").returncode == 0
    return root


def _tree(root: Path) -> dict[str, bytes | None]:
    rels = [ORCH_JSON, MANIFEST, *OLD_FILES, *NEW_HOME_FILES]
    return {rel: (root / rel).read_bytes() if (root / rel).is_file() else None for rel in dict.fromkeys(rels)}


def _version(root: Path) -> str:
    return json.loads((root / ORCH_JSON).read_text(encoding="utf-8"))["core_version"]


def _hold_lock(root: Path, seconds: float) -> subprocess.Popen:
    """Another process holding the target's merge-then-checkout lock for ``seconds``."""
    lock_path = root / "var" / "locks" / "merge.lock"
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(lock_path), str(seconds)], cwd=str(REPO), env=env,
                            stdout=subprocess.PIPE, text=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    line: list[str] = []
    reader = threading.Thread(target=lambda: line.append(proc.stdout.readline()), daemon=True)
    reader.start()
    reader.join(WAIT_S)
    if not line:
        _stop(proc)
        pytest.fail(f"the lock holder printed nothing in {WAIT_S:.0f} s")
    assert line[0].strip() == "held"
    return proc


def _stop(proc: subprocess.Popen) -> None:
    """Ends a helper process; every wait is bounded."""
    proc.kill()
    proc.wait(timeout=WAIT_S)


def _update(root: Path, home_files: dict[str, str] | None = None, **kwargs):
    return run_core_update(root, gh_runner=_fake_gh(home_files or NEW_HOME_FILES), live_check=lambda: False,
                           step_runner=lambda _root: [], **kwargs)


# Row 1 -- another process holds the lock: the bounded wait ends in `merge running`, nothing was written.
def test_row1_lock_held_by_another_process_refuses_merge_running_with_0_files_written(tmp_path: Path) -> None:
    root = _target(tmp_path)
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    before = _tree(root)
    holder = _hold_lock(root, 30.0)
    try:
        started = time.monotonic()
        with pytest.raises(CoreUpdateError) as caught:
            _update(root, lock_wait_s=0.5)
        waited = time.monotonic() - started
    finally:
        _stop(holder)

    assert "merge running" in str(caught.value)
    assert 0.4 <= waited < 10.0
    assert _tree(root) == before
    assert _version(root) == OLD_VERSION
    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    assert _git(root, "rev-parse", "HEAD").stdout.strip() == head


# The wait is a wait: a lock freed inside the bound lets the update through.
def test_lock_freed_inside_the_bound_lets_the_update_land(tmp_path: Path) -> None:
    root = _target(tmp_path)
    holder = _hold_lock(root, 1.0)
    try:
        result = _update(root, lock_wait_s=30.0)
    finally:
        _stop(holder)

    assert result.home_head == NEW_VERSION
    assert _version(root) == NEW_VERSION
    assert _git(root, "status", "--porcelain").stdout.strip() == ""


# A free lock: the update lands, and its own commit step does not wait on the lock the run already holds.
def test_lock_free_normal_update_lands_without_waiting_on_itself(tmp_path: Path) -> None:
    root = _target(tmp_path)

    result = _update(root, lock_wait_s=5.0)

    assert result.home_head == NEW_VERSION
    assert _version(root) == NEW_VERSION
    assert (root / "docs/agents/three.md").read_text(encoding="utf-8") == "three v2 (new)\n"
    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    assert _git(root, "log", "-1", "--format=%s").stdout.strip() == f"Core update: {NEW_VERSION[:8]}"
    assert _git(root, "rev-parse", "HEAD").stdout == _git(root, "rev-parse", "origin/main").stdout


# -- unit 2: undo covers a kill ---------------------------------------------------------------------

SNAPSHOT = Path("var") / "agentops" / "core_update_snapshot.json"
KILLED = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import json
from test_issue49_core_update import _fake_gh, NEW_HOME_FILES
home_files = json.loads(sys.argv[3]) if len(sys.argv) > 3 else NEW_HOME_FILES
from tools.agentops import core_update
core_update._commit_and_push = lambda *a, **k: os._exit(9)  # the process ends: no finally, no undo
core_update.run_core_update(Path(sys.argv[1]), gh_runner=_fake_gh(home_files), live_check=lambda: False,
                            step_runner=lambda _root: [])
"""


def _kill_an_update(root: Path, home_files: dict[str, str] | None = None) -> None:
    """A real update process that ends (``os._exit``) after its writes, before its commit."""
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    proc = subprocess.run([sys.executable, "-c", KILLED, str(root), str(REPO / "tests"),
                           *([json.dumps(home_files)] if home_files else [])], cwd=str(REPO), env=env,
                          capture_output=True, text=True, timeout=WAIT_S,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.returncode == 9, proc.stderr[-500:]


def _dispatch_out(root: Path) -> str:
    path = root / "artifacts" / "runs" / "dispatch.out"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def test_restore_after_a_raise_between_the_writes_and_the_commit_leaves_0_dirty_paths_and_the_next_run_lands(
        tmp_path: Path, monkeypatch) -> None:
    root = _target(tmp_path)
    before = _tree(root)

    def boom(*_a, **_k):
        raise RuntimeError("the update died after its writes")

    monkeypatch.setattr(core_update, "_commit_and_push", boom)
    with pytest.raises(RuntimeError, match="died"):
        _update(root, lock_wait_s=5.0)
    monkeypatch.undo()

    assert _tree(root) == before  # the finally put every path and core_version back
    assert _version(root) == OLD_VERSION
    assert "core_version_before" not in json.loads((root / ORCH_JSON).read_text(encoding="utf-8"))
    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    assert not (root / SNAPSHOT).exists()

    result = _update(root, lock_wait_s=5.0)
    assert result.home_head == NEW_VERSION
    assert _git(root, "status", "--porcelain").stdout.strip() == ""


def test_restore_snapshot_is_written_before_the_first_write_and_a_killed_update_leaves_it(tmp_path: Path) -> None:
    root = _target(tmp_path)
    head = _git(root, "rev-parse", "HEAD").stdout.strip()

    _kill_an_update(root)

    assert _version(root) == NEW_VERSION  # the kill left the tree written
    snap = json.loads((root / SNAPSHOT).read_text(encoding="utf-8"))
    assert snap["target_head"] == head
    assert base64.b64decode(snap["before"]["docs/agents/one.md"]) == b"one v1\n"
    assert snap["before"]["docs/agents/three.md"] is None
    assert "docs/agents/three.md" in snap["created"]
    assert snap["core_version"] == OLD_VERSION


def test_restore_leftover_snapshot_is_restored_first_logged_and_the_next_update_lands(tmp_path: Path,
                                                                                   monkeypatch) -> None:
    root = _target(tmp_path)
    before = _tree(root)
    _kill_an_update(root)
    assert _git(root, "status", "--porcelain").stdout.strip() != ""

    seen: dict[str, object] = {}
    real_plan = core_update._plan_update

    def spy(*args, **kwargs):
        seen.setdefault("status_at_first_plan", _git(root, "status", "--porcelain").stdout.strip())
        seen.setdefault("tree_at_first_plan", _tree(root))
        return real_plan(*args, **kwargs)

    monkeypatch.setattr(core_update, "_plan_update", spy)
    result = _update(root, lock_wait_s=5.0)
    monkeypatch.undo()

    assert seen["status_at_first_plan"] == ""  # restored FIRST, before the new update is planned
    assert seen["tree_at_first_plan"] == before
    assert f"core update {NEW_VERSION[:8]}: restored 4 path(s) left by a killed update" in _dispatch_out(root)
    assert result.home_head == NEW_VERSION
    assert _version(root) == NEW_VERSION
    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    assert not (root / SNAPSHOT).exists()
    assert _git(root, "rev-parse", "HEAD").stdout == _git(root, "rev-parse", "origin/main").stdout


def test_restore_leftover_whose_commit_landed_is_deleted_and_the_tree_is_not_touched(tmp_path: Path) -> None:
    root = _target(tmp_path)
    _kill_an_update(root)
    assert _git(root, "add", "-A").returncode == 0
    assert _git(root, "commit", "-m", f"Core update: {NEW_VERSION[:8]}").returncode == 0
    assert (root / SNAPSHOT).is_file()
    tree = _tree(root)

    result = _update(root, lock_wait_s=5.0)

    assert not (root / SNAPSHOT).exists()
    assert _tree(root) == tree
    assert "restored" not in _dispatch_out(root)
    assert result.changed == []
    assert _git(root, "status", "--porcelain").stdout.strip() == ""


def test_restore_never_touches_a_path_outside_the_snapshot(tmp_path: Path) -> None:
    root = _target(tmp_path)
    _kill_an_update(root)
    _write(root, "notes.txt", "my own untracked work\n")
    _write(root, ".gitignore", "var/\nartifacts/\n# my own edit\n")

    _update(root, lock_wait_s=5.0)

    assert (root / "notes.txt").read_text(encoding="utf-8") == "my own untracked work\n"
    assert (root / ".gitignore").read_text(encoding="utf-8") == "var/\nartifacts/\n# my own edit\n"
    assert sorted(line[3:] for line in _git(root, "status", "--porcelain").stdout.splitlines()) == [
        ".gitignore", "notes.txt"]
    assert _version(root) == NEW_VERSION


def test_restore_a_normal_update_leaves_no_snapshot_behind(tmp_path: Path) -> None:
    root = _target(tmp_path)

    _update(root, lock_wait_s=5.0)

    assert not (root / SNAPSHOT).exists()
    assert "restored" not in _dispatch_out(root)


def test_restore_refused_push_keeps_the_existing_undo_and_no_snapshot(tmp_path: Path) -> None:
    root = _target(tmp_path)
    before = _tree(root)
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    hook = tmp_path / "origin.git" / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

    with pytest.raises(CoreUpdateError, match="push refused"):
        _update(root, lock_wait_s=5.0)

    assert _tree(root) == before
    assert _git(root, "rev-parse", "HEAD").stdout.strip() == head
    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    assert not (root / SNAPSHOT).exists()


# -- review follow-up: the home merge's push to a target is bounded by the dispatcher's wait --------------

def test_core_targets_busy_target_is_refused_merge_running_after_the_bound(tmp_path: Path, monkeypatch) -> None:
    root = _target(tmp_path)
    before = _tree(root)
    monkeypatch.setattr(core_targets, "CORE_UPDATE_WAIT_S", 0.5)
    holder = _hold_lock(root, 30.0)
    try:
        started = time.monotonic()
        lines = core_targets.run_core_targets([{"repo": "o/t", "folder": str(root)}], home_root=tmp_path,
                                              gh_runner=_fake_gh(NEW_HOME_FILES))
        waited = time.monotonic() - started
    finally:
        _stop(holder)

    assert lines == ["o/t: refused: merge running"]
    assert 0.4 <= waited < 10.0
    assert _tree(root) == before


def test_restore_core_status_also_restores_a_leftover_snapshot(tmp_path: Path) -> None:
    root = _target(tmp_path)
    before = _tree(root)
    _kill_an_update(root)

    def home_unreachable(_args):
        return SimpleNamespace(returncode=1, stdout="", stderr="offline")

    with pytest.raises(Exception):  # the home is not read; the restore came first
        core_update.run_core_status(root, gh_runner=home_unreachable)

    assert _tree(root) == before
    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    assert not (root / SNAPSHOT).exists()
    assert f"core update {NEW_VERSION[:8]}: restored 4 path(s) left by a killed update" in _dispatch_out(root)


# -- unit 2 review: "landed" is the update's own commit, not any commit ---------------------------------------

def _status_with_the_home_down(root: Path) -> None:
    """core status: the leftover is restored first, then the unreachable home stops it."""
    with pytest.raises(Exception):
        core_update.run_core_status(root, gh_runner=lambda _args: SimpleNamespace(returncode=1, stdout="",
                                                                                  stderr="offline"))


def test_restore_after_an_unrelated_commit_the_snapshot_paths_are_still_restored(tmp_path: Path,
                                                                              monkeypatch) -> None:
    root = _target(tmp_path)
    before = _tree(root)
    _kill_an_update(root)
    _write(root, "STATE.md", "the target brain's own state line\n")
    assert _git(root, "add", "STATE.md").returncode == 0
    assert _git(root, "commit", "-m", "State: the target's own commit", "--", "STATE.md").returncode == 0
    assert _git(root, "status", "--porcelain").stdout.strip() != ""  # the core paths are still dirty

    seen: dict[str, object] = {}
    real_plan = core_update._plan_update

    def spy(*args, **kwargs):
        seen.setdefault("tree_at_first_plan", _tree(root))
        return real_plan(*args, **kwargs)

    monkeypatch.setattr(core_update, "_plan_update", spy)
    result = _update(root, lock_wait_s=5.0)
    monkeypatch.undo()

    assert seen["tree_at_first_plan"] == before
    assert f"core update {NEW_VERSION[:8]}: restored 4 path(s) left by a killed update" in _dispatch_out(root)
    assert result.home_head == NEW_VERSION
    assert _version(root) == NEW_VERSION
    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    assert (root / "STATE.md").read_text(encoding="utf-8") == "the target brain's own state line\n"


def test_restore_leaves_a_snapshot_path_that_a_later_commit_changed(tmp_path: Path) -> None:
    root = _target(tmp_path)
    _kill_an_update(root)
    _write(root, "docs/agents/one.md", "one v1, edited and committed by the target\n")  # a path the update wrote
    assert _git(root, "commit", "-m", "State: edits one.md", "--", "docs/agents/one.md").returncode == 0

    _status_with_the_home_down(root)

    assert (root / "docs/agents/one.md").read_text(encoding="utf-8") == "one v1, edited and committed by the target\n"
    assert _version(root) == OLD_VERSION


def test_restore_gives_back_a_binary_file_and_a_crlf_file_byte_for_byte(tmp_path: Path) -> None:
    root = _target(tmp_path)
    binary = bytes(range(256)) + b"\r\n\x00\xff\xfe"
    crlf = b"two v1\r\nsecond line\r\n"
    (root / "docs/agents/one.md").write_bytes(binary)
    (root / "docs/agents/two.md").write_bytes(crlf)
    assert _git(root, "commit", "-am", "binary and CRLF originals").returncode == 0
    assert _git(root, "push").returncode == 0
    _kill_an_update(root, {"docs/agents/one.md": "one v2\n", "docs/agents/two.md": "two v2\n"})
    assert (root / "docs/agents/one.md").read_bytes() == b"one v2\n"
    assert (root / "docs/agents/two.md").read_bytes() == b"two v2\n"

    _status_with_the_home_down(root)

    assert (root / "docs/agents/one.md").read_bytes() == binary
    assert (root / "docs/agents/two.md").read_bytes() == crlf
    assert _git(root, "status", "--porcelain").stdout.strip() == ""


def test_restore_unreadable_snapshot_is_logged_and_removed(tmp_path: Path) -> None:
    root = _target(tmp_path)
    (root / SNAPSHOT).parent.mkdir(parents=True, exist_ok=True)
    (root / SNAPSHOT).write_text("{ not json", encoding="utf-8")

    result = _update(root, lock_wait_s=5.0)

    assert not (root / SNAPSHOT).exists()
    assert _dispatch_out(root).count("core update: unreadable snapshot removed") == 1
    assert result.home_head == NEW_VERSION
    assert _git(root, "status", "--porcelain").stdout.strip() == ""


def test_restore_core_status_reports_a_restore_that_cannot_write_on_one_line(tmp_path: Path, monkeypatch) -> None:
    root = _target(tmp_path)
    _kill_an_update(root)

    def locked(*_a, **_k):
        raise PermissionError(13, "the file is in use")

    monkeypatch.setattr(core_update, "_restore", locked)
    with pytest.raises(CoreUpdateError) as caught:
        core_update.run_core_status(root, gh_runner=lambda _args: SimpleNamespace(returncode=1, stdout="",
                                                                                  stderr="offline"))

    assert "\n" not in str(caught.value)
    assert "killed update" in str(caught.value)
    assert (root / SNAPSHOT).is_file()  # kept: the next run tries again


# -- #49 review: the final line is never missing after `started`, and the restore comes before the home read ----

def test_log_oserror_after_the_writes_writes_started_and_one_failed_line_and_propagates(tmp_path: Path,
                                                                                       monkeypatch) -> None:
    root = _target(tmp_path)

    def disk_full(*_a, **_k):
        raise OSError("disk full after the writes")

    monkeypatch.setattr(core_update, "_commit_and_push", disk_full)
    with pytest.raises(OSError, match="disk full"):
        _update(root, lock_wait_s=5.0)

    assert _log_lines(root) == [f"core update {NEW_VERSION[:8]}: started",
                                f"core update {NEW_VERSION[:8]}: failed: disk full after the writes"]


def test_log_keyboard_interrupt_after_started_writes_failed_interrupted_and_propagates(tmp_path: Path,
                                                                                      monkeypatch) -> None:
    root = _target(tmp_path)

    def interrupted(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(core_update, "_commit_and_push", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _update(root, lock_wait_s=5.0)

    assert _log_lines(root) == [f"core update {NEW_VERSION[:8]}: started",
                                f"core update {NEW_VERSION[:8]}: failed: interrupted"]


def test_restore_home_read_failure_after_a_killed_update_restores_the_leftover_first(tmp_path: Path) -> None:
    root = _target(tmp_path)
    before = _tree(root)
    _kill_an_update(root)
    fake = _fake_gh(NEW_HOME_FILES)

    def home_down(args):
        if "/commits/main" in args[1]:
            return SimpleNamespace(returncode=1, stdout="", stderr="offline")
        return fake(args)

    with pytest.raises(CoreUpdateError):
        run_core_update(root, gh_runner=home_down, live_check=lambda: False,
                        step_runner=lambda _root: [], lock_wait_s=5.0)

    assert _tree(root) == before  # the leftover was restored although the home could not be read
    assert not (root / SNAPSHOT).exists()
    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    assert f"core update {NEW_VERSION[:8]}: restored 4 path(s) left by a killed update" in _dispatch_out(root)


# -- #49: every real update is logged in the target's dispatch.out, once at its start and once at its end ----

def _log_lines(root: Path) -> list[str]:
    """The `core update ...` lines of the target's dispatch.out, without their time stamp."""
    return [line.split(" | ", 1)[1] for line in _dispatch_out(root).splitlines() if " | core update " in line]


def test_log_normal_update_writes_one_started_and_one_taken_line(tmp_path: Path) -> None:
    root = _target(tmp_path)

    _update(root, lock_wait_s=5.0)

    landed = _git(root, "rev-parse", "HEAD").stdout.strip()[:8]
    assert _log_lines(root) == [f"core update {NEW_VERSION[:8]}: started",
                                f"core update {NEW_VERSION[:8]}: taken {landed}"]


def test_log_busy_lock_refusal_writes_one_refused_merge_running_line_and_no_started_line(tmp_path: Path) -> None:
    root = _target(tmp_path)
    holder = _hold_lock(root, 30.0)
    try:
        with pytest.raises(CoreUpdateError):
            _update(root, lock_wait_s=0.5)
    finally:
        _stop(holder)

    assert _log_lines(root) == [f"core update {NEW_VERSION[:8]}: refused: merge running"]


def test_log_dirty_path_refusal_writes_one_refused_line_and_no_started_line(tmp_path: Path) -> None:
    root = _target(tmp_path)
    _write(root, "docs/agents/one.md", "my own uncommitted edit\n")

    with pytest.raises(CoreUpdateError, match="uncommitted"):
        _update(root, lock_wait_s=5.0)

    assert _log_lines(root) == [
        f"core update {NEW_VERSION[:8]}: refused: uncommitted change(s) at docs/agents/one.md"]


def test_log_live_orch_refusal_writes_one_refused_line_and_no_started_line(tmp_path: Path) -> None:
    root = _target(tmp_path)

    with pytest.raises(CoreUpdateError, match="ORCH session is live"):
        run_core_update(root, gh_runner=_fake_gh(NEW_HOME_FILES), live_check=lambda: True, lock_wait_s=5.0)

    assert _log_lines(root) == [
        f"core update {NEW_VERSION[:8]}: refused: an ORCH session is live in this target"]


def test_log_failed_post_copy_step_writes_one_failed_line_and_no_taken_line(tmp_path: Path) -> None:
    root = _target(tmp_path)

    def broken_step(_root):
        raise RuntimeError("the post-copy step broke")

    with pytest.raises(CoreUpdateError, match="post-copy"):
        run_core_update(root, gh_runner=_fake_gh(NEW_HOME_FILES), live_check=lambda: False,
                        step_runner=broken_step, lock_wait_s=5.0)

    lines = _log_lines(root)
    assert len(lines) == 2
    assert lines[0] == f"core update {NEW_VERSION[:8]}: started"
    assert lines[1].startswith(f"core update {NEW_VERSION[:8]}: failed: core update failed in its post-copy steps")


def test_log_update_with_nothing_to_change_writes_no_line(tmp_path: Path) -> None:
    root = _target(tmp_path)
    _update(root, lock_wait_s=5.0)
    assert len(_log_lines(root)) == 2  # the first update landed and logged its own two lines
    before = _dispatch_out(root)

    result = _update(root, lock_wait_s=5.0)

    assert result.changed == [] and result.removed == []
    assert _dispatch_out(root) == before


def test_log_core_targets_call_writes_one_started_and_one_taken_line_in_the_target(tmp_path: Path) -> None:
    root = _target(tmp_path)

    lines = core_targets.run_core_targets([{"repo": "o/t", "folder": str(root)}], home_root=tmp_path,
                                          gh_runner=_fake_gh(NEW_HOME_FILES))

    landed = _git(root, "rev-parse", "HEAD").stdout.strip()[:8]
    assert lines == [f"o/t: taken {NEW_VERSION[:8]}"]
    assert _log_lines(root) == [f"core update {NEW_VERSION[:8]}: started",
                                f"core update {NEW_VERSION[:8]}: taken {landed}"]
