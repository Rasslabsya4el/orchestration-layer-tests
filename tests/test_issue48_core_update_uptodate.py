"""Issue 48: a second `core update` at the head the target already holds is "up to date": nothing
changed, nothing removed, 0 commits, nothing raised -- also on a target with `core.autocrlf=true`
whose working copy git rewrote with CRLF after the first update (the bytes differ from the home's LF
content, the content git stores does not). Real git on a tmp target (bare origin + clone); the home is
a fake `gh` runner, no network."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.test_issue28_core_update_failed_commit import (
    NEW_HOME_FILES, ORCH_JSON, _fake_gh, _git, _target,
)
from tools.agentops.core_targets import run_core_targets
from tools.agentops.core_update import UpdateResult, run_core_update

ONE = "docs/agents/one.md"


def _update(root: Path) -> UpdateResult:
    return run_core_update(root, gh_runner=_fake_gh(NEW_HOME_FILES), live_check=lambda: False,
                           step_runner=lambda _root: [])


def _commits(root: Path) -> int:
    return int(_git(root, "rev-list", "--count", "HEAD").stdout.strip())


def _recheckout_with_crlf(root: Path) -> None:
    """What a later `git checkout` / `reset --hard` does under autocrlf=true: LF in the index, CRLF on disk."""
    subprocess.run(["git", "rm", "-q", "--cached", "-r", "."], cwd=str(root), check=True)
    subprocess.run(["git", "reset", "-q", "--hard"], cwd=str(root), check=True)


def _taken_target(tmp_path: Path, autocrlf: str) -> Path:
    root = _target(tmp_path)
    assert _git(root, "config", "core.autocrlf", autocrlf).returncode == 0
    first = _update(root)
    assert first.changed and "up to date" not in first.message
    return root


# A1 -- the second update at one head, working copy rewritten with CRLF: nothing to do.
def test_second_update_at_one_head_is_up_to_date_on_autocrlf(tmp_path: Path) -> None:
    root = _taken_target(tmp_path, "true")
    _recheckout_with_crlf(root)
    assert (root / ONE).read_bytes() == b"one v2\r\n"
    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    commits = _commits(root)

    second = _update(root)

    assert second.changed == [] and second.removed == []
    assert second.home_head == "b" * 40
    assert "up to date" in second.message
    assert _commits(root) == commits
    assert (root / ONE).read_bytes() == b"one v2\r\n"  # nothing was written
    assert _git(root, "status", "--porcelain").stdout.strip() == ""


# The same with a working copy still in LF (autocrlf=true, nothing rewrote it).
def test_second_update_with_lf_tree_is_up_to_date(tmp_path: Path) -> None:
    root = _taken_target(tmp_path, "true")
    commits = _commits(root)

    second = _update(root)

    assert second.changed == [] and second.removed == []
    assert "up to date" in second.message
    assert _commits(root) == commits


# A2 -- the home merge's line for that target reads taken, never failed.
def test_core_targets_line_is_not_failed(tmp_path: Path) -> None:
    root = _taken_target(tmp_path, "true")
    _recheckout_with_crlf(root)
    entry = {"repo": "owner/target", "folder": str(root)}

    lines = run_core_targets([entry], home_root=tmp_path, gh_runner=_fake_gh(NEW_HOME_FILES))

    assert lines == [f"owner/target: taken {'b' * 8}"]
    out = root / "artifacts" / "runs" / "dispatch.out"  # #49 logs started/taken lines there; a current target never gets a failed one
    assert "failed" not in (out.read_text(encoding="utf-8") if out.exists() else "")


# Kept: with autocrlf off a CRLF file in the repo is a real difference from the home's LF: still rewritten.
def test_crlf_file_with_autocrlf_off_is_still_updated(tmp_path: Path, monkeypatch) -> None:
    from tools.agentops import core_update

    # #148: a target at the head with clean core files stops after the head call; the patch forces the full-read
    # path that such a target no longer reaches
    monkeypatch.setattr(core_update, "_core_paths_dirty", lambda _root: True)
    root = _taken_target(tmp_path, "false")
    (root / ONE).write_bytes(b"one v2\r\n")
    assert _git(root, "add", ONE).returncode == 0
    assert _git(root, "commit", "-q", "-m", "crlf in the repo").returncode == 0
    commits = _commits(root)

    second = _update(root)

    assert second.changed == [ONE]
    assert (root / ONE).read_bytes() == b"one v2\n"
    assert _commits(root) == commits + 1


# Kept: a local edit of a core file is still refused, not read as "up to date".
def test_local_edit_is_still_refused(tmp_path: Path) -> None:
    import pytest
    from tools.agentops.core_update import CoreUpdateError

    root = _taken_target(tmp_path, "true")
    (root / ONE).write_bytes(b"edited by hand\n")

    with pytest.raises(CoreUpdateError, match="uncommitted change"):
        _update(root)


# Follow-up -- a path the update rewrites to content git already holds (here `_git_holds` is forced to
# miss it) stages nothing: no commit, no push, nothing raised.
def test_equal_after_normalisation_write_makes_no_commit(tmp_path: Path, monkeypatch) -> None:
    from tools.agentops import core_update

    root = _taken_target(tmp_path, "true")
    _recheckout_with_crlf(root)
    monkeypatch.setattr(core_update, "_git_holds", lambda *_args: False)
    # #148: the patch forces the full-read path (a target at head with clean core paths no longer reaches it)
    monkeypatch.setattr(core_update, "_core_paths_dirty", lambda _root: True)
    monkeypatch.setattr(core_update, "_clean_index_blobs", lambda _root: {})  # #148: ... and the tree's twin of it misses too
    commits = _commits(root)

    second = _update(root)

    assert second.changed != []  # the files were rewritten ...
    assert _commits(root) == commits  # ... but git held them already: nothing committed
    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    assert _git(root, "rev-parse", "HEAD").stdout.strip() == _git(root, "rev-parse", "origin/main").stdout.strip()
