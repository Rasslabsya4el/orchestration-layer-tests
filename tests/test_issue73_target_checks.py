"""#73 unit 1: the home merge checks the merged core text against each target's own checks before main moves.

Seams: ``target_check.check_targets`` (the hits for a tree) and ``merge.run_merge`` (a refusal leaves main where it was,
a clean merge lands and pushes as before). The fake target is a folder in tmp_path; no real target is read."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.agentops import core_targets, merge as merge_module
from tools.agentops.merge import run_merge
from tools.agentops.target_check import check_targets

SUITE = f'"{sys.executable}" --version'
REPO = "acme/widgets"


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _tree(root: Path, core_text: str = "# core\nplain text\n") -> Path:
    """A merged core tree: one manifest file, `docs/agents/core.md`, and a second doc outside the manifest."""
    (root / "tools" / "agentops").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "agents").mkdir(parents=True, exist_ok=True)
    (root / "tools" / "agentops" / "core_manifest.txt").write_text("docs/agents/core.md\n", encoding="utf-8")
    (root / "docs" / "agents" / "core.md").write_text(core_text, encoding="utf-8")
    return root


def _target(folder: Path, *, patterns: dict | None = None, avoid: str | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    if patterns is not None:
        (folder / "config").mkdir(exist_ok=True)
        (folder / "config" / "core_leak_patterns.json").write_text(json.dumps(patterns), encoding="utf-8")
    if avoid is not None:
        (folder / "GLOSSARY.md").write_text(f"# G\n\n**Gadget**:\nA thing.\n_Avoid_: {avoid}\n", encoding="utf-8")
    return folder


WIDGET = {"L1": "widget", "L2": "zzqq-never", "L3": "zzqq-never-2"}


def test_a_leak_pattern_hit_names_file_line_and_word(tmp_path):
    merged = _tree(tmp_path / "merged", "# core\nuse the Widget here\n")
    target = _target(tmp_path / "t", patterns=WIDGET)
    hits = check_targets(merged, [{"repo": REPO, "folder": str(target)}], tmp_path)
    assert [h.line() for h in hits] == [f"{REPO} rejects core text: docs/agents/core.md:2 Widget"]


def test_a_clean_tree_has_no_hit(tmp_path):
    merged = _tree(tmp_path / "merged")
    target = _target(tmp_path / "t", patterns=WIDGET, avoid="gizmo")
    assert check_targets(merged, [{"repo": REPO, "folder": str(target)}], tmp_path) == []


def test_a_missing_folder_or_a_folder_with_neither_file_is_skipped(tmp_path):
    merged = _tree(tmp_path / "merged", "widget gizmo\n")
    bare = _target(tmp_path / "bare")
    targets = [{"repo": "a/missing", "folder": str(tmp_path / "nowhere")}, {"repo": "a/bare", "folder": str(bare)},
               {"repo": "a/nofolder"}]
    assert check_targets(merged, targets, tmp_path) == []


def test_a_relative_folder_is_resolved_against_the_home(tmp_path):
    merged = _tree(tmp_path / "merged", "widget\n")
    _target(tmp_path / "home" / "rel", patterns=WIDGET)
    hits = check_targets(merged, [{"repo": REPO, "folder": "rel"}], tmp_path / "home")
    assert [h.line() for h in hits] == [f"{REPO} rejects core text: docs/agents/core.md:1 widget"]


def test_an_avoid_word_in_an_agent_doc_is_a_hit_whole_word_case_insensitive_code_spans_ignored(tmp_path):
    merged = _tree(tmp_path / "merged", "ok\nThe Gizmo is here\ngizmos and `gizmo` are fine\n")
    (merged / "docs" / "agents" / "sub").mkdir()
    (merged / "docs" / "agents" / "sub" / "x.md").write_text("a hand-off, and the second line\n", encoding="utf-8")
    target = _target(tmp_path / "t", patterns={"checked_avoid_words": ["gizmo", "hand-off"]},
                     avoid="gizmo (a thing, not this), hand-off, the user")
    hits = check_targets(merged, [{"repo": REPO, "folder": str(target)}], tmp_path)
    assert [h.line() for h in hits] == [
        f"{REPO} rejects core text: docs/agents/core.md:2 gizmo",
        f"{REPO} rejects core text: docs/agents/sub/x.md:1 hand-off"]


def test_avoid_scan_can_be_left_out(tmp_path):
    merged = _tree(tmp_path / "merged", "gizmo\n")
    target = _target(tmp_path / "t", avoid="gizmo")
    assert check_targets(merged, [{"repo": REPO, "folder": str(target)}], tmp_path, scan_avoid=False) == []


# -- the merge ------------------------------------------------------------------------------------------------------

def _world(tmp_path: Path, target: Path, update_rule: str = "home"):
    """A bare origin whose main is a tiny home: settings with one target, a manifest, one core doc."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(bare), str(seed))
    _tree(seed)
    (seed / "config").mkdir()
    (seed / "config" / "orchestration.json").write_text(
        json.dumps({"update_rule": update_rule, "targets": [{"repo": REPO, "folder": str(target)}]}),
        encoding="utf-8")
    (seed / "other.txt").write_text("1\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "initial")
    _git(seed, "push", "-q", "origin", "main")
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(bare), str(checkout))
    lock = tmp_path / "var" / "locks" / "merge.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    return bare, checkout, lock


def _branch(tmp_path: Path, bare: Path, text: str) -> str:
    worker = tmp_path / "worker"
    _git(tmp_path, "clone", "-q", str(bare), str(worker))
    _git(worker, "checkout", "-q", "-b", "feat")
    (worker / "docs" / "agents" / "core.md").write_text(text, encoding="utf-8")
    _git(worker, "commit", "-q", "-am", "edit core")
    _git(worker, "push", "-q", "origin", "feat")
    return "origin/feat"


@pytest.fixture
def merge_env(tmp_path, monkeypatch):
    monkeypatch.setattr(merge_module, "SUITE_OUTPUT_DIR", tmp_path / "suite_out")
    monkeypatch.setattr(merge_module, "MERGE_LOG_PATH", tmp_path / "merge.log")
    monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: False)
    monkeypatch.setattr(merge_module, "_spawn_map_run", lambda cwd, sha: "")
    calls: list = []
    monkeypatch.setattr(core_targets, "run_core_targets", lambda *a, **k: calls.append((a, k)) or [])
    return calls


def test_row2_a_merge_adding_a_target_pattern_is_refused_and_main_does_not_move(tmp_path, merge_env):
    target = _target(tmp_path / "t", patterns=WIDGET)
    bare, checkout, lock = _world(tmp_path, target)
    ref = _branch(tmp_path, bare, "# core\nthe widget\n")
    main_before, local_before = _git(bare, "rev-parse", "main"), _git(checkout, "rev-parse", "HEAD")
    result = run_merge(ref, SUITE, cwd=checkout, lock_path=lock, lock_timeout=60)
    assert not result.ok and not result.pushed
    assert result.message == f"refused: {REPO} rejects core text: docs/agents/core.md:2 widget"
    assert _git(bare, "rev-parse", "main") == main_before
    assert _git(checkout, "rev-parse", "HEAD") == local_before == result.head_before == result.head_after
    assert merge_env == []  # no target touched


def test_row3_a_merge_not_adding_it_lands_and_pushes_as_today(tmp_path, merge_env):
    target = _target(tmp_path / "t", patterns=WIDGET)
    bare, checkout, lock = _world(tmp_path, target)
    ref = _branch(tmp_path, bare, "# core\nnothing special\n")
    main_before = _git(bare, "rev-parse", "main")
    result = run_merge(ref, SUITE, cwd=checkout, lock_path=lock, lock_timeout=60)
    assert result.ok and result.pushed, result
    assert _git(bare, "rev-parse", "main") != main_before


@pytest.mark.parametrize("settings", [
    {"update_rule": "manual", "targets": [{"repo": REPO, "folder": "{t}"}]},
    {"update_rule": "home", "targets": []},
    {"update_rule": "home"},
    None])
def test_only_a_home_with_targets_is_checked(tmp_path, settings):
    target = _target(tmp_path / "t", patterns=WIDGET)
    merged = _tree(tmp_path / "merged", "the widget\n")
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    if settings is not None:
        text = json.dumps(settings).replace("{t}", target.as_posix())
        (home / "config" / "orchestration.json").write_text(text, encoding="utf-8")
    assert merge_module._target_check_refusal(home, "unused", merged, (), None) == ""


def test_an_avoid_word_refuses_the_merge_when_the_avoid_scan_is_on(tmp_path, merge_env, monkeypatch):
    monkeypatch.setattr(merge_module, "SCAN_AVOID_WORDS", True)
    target = _target(tmp_path / "t", patterns={"checked_avoid_words": ["gizmo"]}, avoid="gizmo")
    bare, checkout, lock = _world(tmp_path, target)
    ref = _branch(tmp_path, bare, "# core\nthe gizmo\n")
    main_before = _git(bare, "rev-parse", "main")
    result = run_merge(ref, SUITE, cwd=checkout, lock_path=lock, lock_timeout=60)
    assert not result.ok
    assert result.message == f"refused: {REPO} rejects core text: docs/agents/core.md:2 gizmo"
    assert _git(bare, "rev-parse", "main") == main_before
