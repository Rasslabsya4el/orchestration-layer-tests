"""Issue 198 unit 1: `core update` refuses a target whose checkout is in the middle of a git operation (merge,
cherry-pick, revert, rebase) before any write: the refusal names the operation, nothing is written, the index and
the worktree stay as they were, and no snapshot file is left. A clean target still updates. Real git on a tmp target
(bare origin + clone); the home is a fake `gh` runner, no network."""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.home_tree_fake import tree_reply
from tools.agentops.core_update import CoreUpdateRefused, run_core_update

OLD_VERSION = "a" * 40
NEW_VERSION = "b" * 40
MANIFEST = "tools/agentops/core_manifest.txt"
ORCH_JSON = "config/orchestration.json"
SNAPSHOT = "var/agentops/core_update_snapshot.json"
OLD_FILES = {"docs/agents/one.md": "one v1\n", "docs/agents/two.md": "two v1\n"}
NEW_HOME_FILES = {"docs/agents/one.md": "one v2\n", "docs/agents/two.md": "two v1\n",
                  "docs/agents/three.md": "three v2 (new)\n"}


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
        if "/git/trees/" in path:
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


def _side_branch_with_extra_file(root: Path) -> None:
    """A `side` branch with one commit adding a file main does not have: merging it into main is clean."""
    assert _git(root, "checkout", "-b", "side").returncode == 0
    _write(root, "docs/agents/extra.md", "extra\n")
    assert _git(root, "add", "-A").returncode == 0
    assert _git(root, "commit", "-m", "side adds extra").returncode == 0
    assert _git(root, "checkout", "main").returncode == 0


def _state(root: Path) -> dict:
    """Everything a refused update must leave as it was: status, the staged diff, HEAD, the bytes of every path
    the update could write, and core_version."""
    rels = [ORCH_JSON, MANIFEST, *OLD_FILES, *NEW_HOME_FILES]
    return {
        "porcelain": _git(root, "status", "--porcelain").stdout,
        "cached": _git(root, "diff", "--cached").stdout,
        "head": _git(root, "rev-parse", "HEAD").stdout,
        "tree": {rel: (root / rel).read_bytes() if (root / rel).is_file() else None for rel in dict.fromkeys(rels)},
        "version": json.loads((root / ORCH_JSON).read_text(encoding="utf-8"))["core_version"],
    }


def _update(root: Path):
    return run_core_update(root, gh_runner=_fake_gh(NEW_HOME_FILES), live_check=lambda: False,
                           step_runner=lambda _root: [])


def _assert_untouched(root: Path, before: dict) -> None:
    assert _state(root) == before
    assert not (root / SNAPSHOT).exists()


@pytest.mark.parametrize("marker, label", [
    ("MERGE_HEAD", "mid-merge"),
    ("CHERRY_PICK_HEAD", "mid-cherry-pick"),
    ("REVERT_HEAD", "mid-revert"),
    ("rebase-merge", "mid-rebase"),
    ("rebase-apply", "mid-rebase"),
])
def test_mid_operation_is_refused_before_any_write(tmp_path: Path, marker: str, label: str) -> None:
    root = _target(tmp_path)
    git_path = root / ".git" / marker
    if marker.startswith("rebase-"):
        git_path.mkdir(parents=True)
    else:
        git_path.write_text(_git(root, "rev-parse", "HEAD").stdout)  # a valid commit sha, as git writes it
    before = _state(root)
    with pytest.raises(CoreUpdateRefused) as caught:
        _update(root)
    assert str(caught.value) == f"core update refused: target is {label}"
    assert caught.value.reason == f"target is {label}"
    _assert_untouched(root, before)


def test_real_merge_in_progress_is_refused_with_index_unchanged(tmp_path: Path) -> None:
    root = _target(tmp_path)
    _side_branch_with_extra_file(root)
    assert _git(root, "merge", "--no-commit", "--no-ff", "side").returncode == 0
    assert (root / ".git" / "MERGE_HEAD").is_file()
    before = _state(root)
    assert before["cached"], "the merge must have staged the side's file"
    with pytest.raises(CoreUpdateRefused) as caught:
        _update(root)
    assert "mid-merge" in str(caught.value)
    _assert_untouched(root, before)


def test_linked_worktree_mid_merge_is_refused(tmp_path: Path) -> None:
    """A linked worktree's `.git` is a file: the git dir is resolved by git, so the merge still counts."""
    root = _target(tmp_path)
    _side_branch_with_extra_file(root)
    wt = tmp_path / "wt"
    assert _git(root, "worktree", "add", "-b", "wt-branch", str(wt), "main").returncode == 0
    assert _git(wt, "merge", "--no-commit", "--no-ff", "side").returncode == 0
    before = _state(wt)
    with pytest.raises(CoreUpdateRefused) as caught:
        _update(wt)
    assert str(caught.value) == "core update refused: wt is mid-merge"
    _assert_untouched(wt, before)


def test_clean_target_still_updates(tmp_path: Path) -> None:
    root = _target(tmp_path)
    _update(root)
    assert _state(root)["version"] == NEW_VERSION
