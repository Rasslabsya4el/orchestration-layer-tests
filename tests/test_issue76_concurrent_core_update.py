"""#76 unit 1: merges landing together update each target once. A core update of one target holds the target's
merge-then-checkout lock (`<target>/var/locks/merge.lock`, #49) for its whole run; a waiter that gets it plans again
and, if the target already holds the home's head, ends `taken <sha8> (by a concurrent update)` with nothing written. Real git on a tmp
target (bare origin + clone), the home a fake `gh` runner: no network.

Row 1: two `run_core_targets` calls at once on one target. Row 2: a real foreign uncommitted change still refuses
with its path. Row 3: a lock whose holder pid is dead is taken."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from tests.home_tree_fake import tree_reply
from tools.agentops import core_update
from tools.agentops.core_targets import run_core_targets
from tools.agentops.util import now

OLD_VERSION = "a" * 40
NEW_VERSION = "b" * 40
MANIFEST = "tools/agentops/core_manifest.txt"
ORCH_JSON = "config/orchestration.json"
ONE = "docs/agents/one.md"
OLD_FILES = {ONE: "one v1\n", "docs/agents/two.md": "two v1\n"}
NEW_HOME_FILES = {ONE: "one v2\n", "docs/agents/two.md": "two v1\n", "docs/agents/three.md": "three v2 (new)\n"}
SHA8 = NEW_VERSION[:8]


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _manifest_text(files: dict[str, str]) -> str:
    return "".join(f"{rel}\n" for rel in [MANIFEST, *files])


def _fake_gh(home_files: dict[str, str], head_delay: float = 0.0):
    contents = {rel: text.encode("utf-8") for rel, text in home_files.items()}
    contents[MANIFEST] = _manifest_text(home_files).encode("utf-8")

    def run(args: list[str]):
        path = args[1]
        if "/commits/main" in path:
            time.sleep(head_delay)  # widens the window between the head read and the commit
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


def _entry(root: Path) -> list[dict]:
    return [{"repo": "owner/target", "folder": str(root)}]


def _lock(root: Path) -> Path:
    return root / "var" / "locks" / "merge.lock"


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# -- row 1 ---------------------------------------------------------------------------------------------------------

def test_row1_two_updates_at_once_one_writes_the_other_is_taken_by_a_concurrent_update(tmp_path: Path) -> None:
    root = _target(tmp_path)
    gh = _fake_gh(NEW_HOME_FILES, head_delay=0.3)
    lines: list[list[str]] = []

    def call() -> None:
        lines.append(run_core_targets(_entry(root), home_root=tmp_path, gh_runner=gh))

    threads = [threading.Thread(target=call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    flat = sorted(line for result in lines for line in result)
    assert flat == [f"owner/target: taken {SHA8}", f"owner/target: taken {SHA8} (by a concurrent update)"]
    assert not any("refused" in line or "failed" in line for line in flat)
    assert json.loads((root / ORCH_JSON).read_text(encoding="utf-8"))["core_version"] == NEW_VERSION
    assert (root / ONE).read_text(encoding="utf-8") == "one v2\n"
    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    assert _git(root, "log", "--oneline").stdout.count("Core update") == 1
    assert not _lock(root).exists()


# -- row 2 ---------------------------------------------------------------------------------------------------------

def test_row2_a_real_foreign_uncommitted_change_still_refuses_with_its_path(tmp_path: Path) -> None:
    root = _target(tmp_path)
    _write(root, ONE, "one edited by hand\n")

    lines = run_core_targets(_entry(root), home_root=tmp_path, gh_runner=_fake_gh(NEW_HOME_FILES))

    assert lines == [f"owner/target: failed: core update refused: uncommitted change(s) at {ONE}"]
    assert (root / ONE).read_text(encoding="utf-8") == "one edited by hand\n"
    assert not _lock(root).exists()


# -- row 3 ---------------------------------------------------------------------------------------------------------

def test_row3_a_lock_of_a_dead_pid_is_taken(tmp_path: Path) -> None:
    root = _target(tmp_path)
    _lock(root).parent.mkdir(parents=True, exist_ok=True)
    _lock(root).write_text(json.dumps({"pid": _dead_pid(), "started": now()}), encoding="utf-8")

    lines = run_core_targets(_entry(root), home_root=tmp_path, gh_runner=_fake_gh(NEW_HOME_FILES))

    assert lines == [f"owner/target: taken {SHA8}"]  # the plain line: it did not wait for anyone
    assert not _lock(root).exists()
