"""Issue 148 (rules 1 and 2): one `core update` reads the home's tree once and fetches only the files whose blob
sha differs from the target's own copy, so its REST calls grow with the files that changed, not with the
manifest; a target already at the home's head costs the one head-sha call. A real git target (bare origin +
clone) and a fake `gh` that counts calls by endpoint class; no network."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._gh_guard import _no_real_gh  # noqa: F401
from tools.agentops import gh_call
from tools.agentops.core_update import CoreUpdateError, HomeReader, run_core_update

MANIFEST = "tools/agentops/core_manifest.txt"
ORCH_JSON = "config/orchestration.json"
OLD_HEAD = "a" * 40
NEW_HEAD = "b" * 40
FILES = 374  # plus the manifest itself: 375 entries
RATE_LIMIT = "gh: API rate limit exceeded for user (HTTP 403)"


def _name(i: int) -> str:
    return f"docs/agents/f{i:03d}.md"


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _manifest_bytes(paths: list[str]) -> bytes:
    return "".join(f"{p}\n" for p in [MANIFEST, *paths]).encode("utf-8")


class FakeHome:
    """Answers the `gh api` shapes a core update reads and counts them by endpoint class."""

    def __init__(self, files: dict[str, bytes], *, truncated: bool = False) -> None:
        self.files = dict(files)
        self.files[MANIFEST] = _manifest_bytes(sorted(files))
        self.truncated = truncated
        self.calls: Counter = Counter()
        self.fetched: list[str] = []

    def __call__(self, args: list[str]):
        path = args[1]
        if "/commits/main" in path:
            self.calls["head"] += 1
            return SimpleNamespace(returncode=0, stdout=NEW_HEAD + "\n", stderr="")
        if "/git/trees/" in path:
            self.calls["tree"] += 1
            assert path.endswith(f"{NEW_HEAD}?recursive=1"), path
            listing = sorted(self.files)[: len(self.files) // 2] if self.truncated else sorted(self.files)
            body = {"sha": NEW_HEAD, "truncated": self.truncated,
                    "tree": [{"path": "docs", "type": "tree", "sha": "0" * 40}]
                    + [{"path": p, "type": "blob", "sha": _blob_sha(self.files[p])} for p in listing]}
            return SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr="")
        if "/contents/" in path:
            rel = path.split("/contents/", 1)[1].split("?ref=", 1)[0]
            self.calls["manifest" if rel == MANIFEST else "contents"] += 1
            self.fetched.append(rel)
            return SimpleNamespace(returncode=0, stdout=base64.b64encode(self.files[rel]).decode("ascii") + "\n",
                                   stderr="")
        raise AssertionError(f"unexpected gh call {args}")

    @property
    def total(self) -> int:
        return sum(self.calls.values())


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def _write(root: Path, rel: str, data: bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _home_files() -> dict[str, bytes]:
    return {_name(i): f"file {i} home version\n".encode() for i in range(FILES)}


def _target(tmp_path: Path, files: dict[str, bytes], version: str) -> Path:
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    root = tmp_path / "target"
    root.mkdir()
    for args in (("init", "-b", "main"), ("config", "user.email", "t@example.com"), ("config", "user.name", "t"),
                 ("config", "core.autocrlf", "false"), ("remote", "add", "origin", str(bare))):
        assert _git(root, *args).returncode == 0
    _write(root, ".gitignore", b"var/\nartifacts/\n")
    _write(root, ORCH_JSON, (json.dumps({"update_rule": "word", "home": "owner/home", "core_version": version},
                                        indent=2) + "\n").encode())
    _write(root, MANIFEST, _manifest_bytes(sorted(files)))
    for rel, data in files.items():
        _write(root, rel, data)
    assert _git(root, "add", "-A").returncode == 0
    assert _git(root, "commit", "-q", "-m", "target before the update").returncode == 0
    assert _git(root, "push", "-q", "-u", "origin", "main").returncode == 0
    return root


def _update(root: Path, home: FakeHome):
    return run_core_update(root, gh_runner=home, live_check=lambda: False, step_runner=lambda _root: [])


def _changed_home(k: int) -> tuple[dict[str, bytes], dict[str, bytes], list[str]]:
    """The home's files, the target's (the first k of them held in an older version) and the k changed names."""
    home = _home_files()
    target = dict(home)
    changed = [_name(i) for i in range(k)]
    for rel in changed:
        target[rel] = b"older version\n"
    return home, target, changed


# Row 1: the REST calls of an update of k changed files out of 375 entries are at most k + 3.
@pytest.mark.parametrize("k, exact", [(0, 2), (1, 3), (40, 42)])
def test_update_costs_at_most_k_plus_three_calls(tmp_path: Path, k: int, exact: int) -> None:
    home_files, target_files, changed = _changed_home(k)
    root = _target(tmp_path, target_files, OLD_HEAD)
    home = FakeHome(home_files)

    result = _update(root, home)

    assert home.total == exact and home.total <= k + 3, dict(home.calls)
    assert home.calls["head"] == 1 and home.calls["tree"] == 1
    assert sorted(home.fetched) == sorted(changed)  # nothing but the changed files is read by contents
    assert sorted(result.changed) == sorted(changed)
    for rel in changed:
        assert (root / rel).read_bytes() == home_files[rel]
    assert json.loads((root / ORCH_JSON).read_text(encoding="utf-8"))["core_version"] == NEW_HEAD
    assert _git(root, "status", "--porcelain").stdout.strip() == ""


# A new manifest entry (the manifest and the new file are the two changed files): head + tree + manifest + new file.
def test_new_manifest_entry_is_fetched_with_the_manifest(tmp_path: Path) -> None:
    home_files = _home_files()
    target_files = {rel: data for rel, data in home_files.items() if rel != _name(5)}
    root = _target(tmp_path, target_files, OLD_HEAD)
    home = FakeHome(home_files)

    result = _update(root, home)

    assert home.total == 4 and home.total <= 2 + 3, dict(home.calls)
    assert sorted(result.changed) == sorted([_name(5), MANIFEST])
    assert (root / _name(5)).read_bytes() == home_files[_name(5)]


# A copy whose bytes differ from the home's but which git holds as the home's (CRLF on disk under autocrlf) is not fetched.
def test_crlf_copy_git_holds_is_not_fetched(tmp_path: Path) -> None:
    home_files = _home_files()
    root = _target(tmp_path, home_files, OLD_HEAD)
    assert _git(root, "config", "core.autocrlf", "true").returncode == 0
    subprocess.run(["git", "rm", "-q", "--cached", "-r", "."], cwd=str(root), check=True)
    subprocess.run(["git", "reset", "-q", "--hard"], cwd=str(root), check=True)
    assert (root / _name(0)).read_bytes().endswith(b"\r\n")
    home = FakeHome(home_files)

    result = _update(root, home)

    assert home.total == 2, dict(home.calls)
    assert result.changed == []


# Row 2: a target already at the home's head makes the head-sha call and reads nothing else.
def test_target_at_head_makes_one_call(tmp_path: Path) -> None:
    home_files = _home_files()
    root = _target(tmp_path, home_files, NEW_HEAD)
    home = FakeHome(home_files)

    result = _update(root, home)

    assert home.total == 1 and home.calls["head"] == 1, dict(home.calls)
    assert result.changed == [] and result.removed == []
    assert "up to date" in result.message
    assert result.home_head == NEW_HEAD


# A target at the head with an uncommitted edit of a core file still goes through the full read and is refused.
def test_target_at_head_with_a_local_edit_is_still_refused(tmp_path: Path) -> None:
    home_files = _home_files()
    root = _target(tmp_path, home_files, NEW_HEAD)
    _write(root, _name(3), b"edited by hand\n")

    with pytest.raises(CoreUpdateError, match="uncommitted change"):
        _update(root, FakeHome(home_files))


# A truncated tree is never used partially: the update falls back to one contents call per file, same result.
def test_truncated_tree_falls_back_to_per_file_reads(tmp_path: Path) -> None:
    home_files, target_files, changed = _changed_home(3)
    root = _target(tmp_path, target_files, OLD_HEAD)
    home = FakeHome(home_files, truncated=True)

    result = _update(root, home)

    assert home.calls["tree"] == 1
    assert home.calls["contents"] == FILES  # every non-manifest entry, as before the tree
    assert sorted(result.changed) == sorted(changed)
    for rel in changed:
        assert (root / rel).read_bytes() == home_files[rel]


# The tree call itself: blobs only, as {path: blob sha}; a truncated answer is None.
def test_home_reader_tree_lists_blobs_and_flags_truncation() -> None:
    files = {"a.md": b"a\n", "dir/b.md": b"b\n"}
    full = HomeReader("owner/home", gh_runner=FakeHome(files))
    assert full.tree(NEW_HEAD) == {p: _blob_sha(d) for p, d in FakeHome(files).files.items()}
    assert HomeReader("owner/home", gh_runner=FakeHome(files, truncated=True)).tree(NEW_HEAD) is None


# A tree call that fails (the rate limit) raises, the old per-file path is not tried.
def test_failed_tree_call_raises_without_per_file_reads(tmp_path: Path, monkeypatch) -> None:
    home_files = _home_files()
    root = _target(tmp_path, home_files, OLD_HEAD)
    home = FakeHome(home_files)
    monkeypatch.setattr(gh_call, "rate_limit_reset", lambda resource="core": 42.0)  # injected: no real read

    def limited(args: list[str]):
        if "/git/trees/" in args[1]:
            return SimpleNamespace(returncode=1, stdout="", stderr=RATE_LIMIT)
        return home(args)

    with pytest.raises(CoreUpdateError, match="rate limit"):
        run_core_update(root, gh_runner=limited, live_check=lambda: False, step_runner=lambda _root: [])
    assert home.calls["contents"] == 0 and home.calls["manifest"] == 0


# What a target at the head no longer does, recorded as a decision (the brain was told, #148 closing).

# (a) a core file committed in the target with other bytes while core_version == the home's head is not rewritten.
def test_committed_core_file_edit_is_not_healed_at_head(tmp_path: Path) -> None:
    home_files = _home_files()
    root = _target(tmp_path, home_files, NEW_HEAD)
    _write(root, _name(3), b"committed by hand\n")
    assert _git(root, "commit", "-q", "-am", "hand edit of a core file").returncode == 0
    home = FakeHome(home_files)

    result = _update(root, home)

    # rule 2 of #148: a target at head stops after the head-sha call
    assert home.total == 1 and home.calls["head"] == 1, dict(home.calls)
    assert result.changed == []
    assert (root / _name(3)).read_bytes() == b"committed by hand\n"  # still the hand edit, not the home's


# (b) a missing label at head is not synced: the post-copy steps (and their `gh label` calls) do not run.
def test_missing_label_is_not_healed_at_head(tmp_path: Path) -> None:
    home_files = _home_files()
    root = _target(tmp_path, home_files, NEW_HEAD)
    home = FakeHome(home_files)
    label_calls: list[list[str]] = []

    def gh(args: list[str]):
        if args[0] == "label":
            label_calls.append(list(args))
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")
        return home(args)

    # the real step runner is the one that lists and creates labels; at head it must not even start
    result = run_core_update(root, gh_runner=gh, live_check=lambda: False)

    # rule 2 of #148: a target at head stops after the head-sha call
    assert label_calls == [] and home.total == 1, (label_calls, dict(home.calls))
    assert result.changed == [] and "up to date" in result.message


# A target that is not a committable repo (no usable origin) has no merge lock and no at-head shortcut: it plans
# first, so at the head it reads head + tree and holds every file by blob (no manifest or contents call), writes
# nothing, and runs its post-copy steps as before.
def test_non_committable_target_at_head_reads_head_and_tree(tmp_path: Path) -> None:
    home_files = _home_files()
    root = tmp_path / "plain"
    _write(root, ORCH_JSON, (json.dumps({"update_rule": "word", "home": "owner/home", "core_version": NEW_HEAD},
                                        indent=2) + "\n").encode())
    _write(root, MANIFEST, _manifest_bytes(sorted(home_files)))
    for rel, data in home_files.items():
        _write(root, rel, data)
    home = FakeHome(home_files)

    result = _update(root, home)

    assert dict(home.calls) == {"head": 1, "tree": 1}, dict(home.calls)
    assert result.changed == [] and result.removed == []
