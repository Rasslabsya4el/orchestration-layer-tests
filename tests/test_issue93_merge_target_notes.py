"""#93 unit 2: the home merge names each target that has no leak-words file, and never refuses for it.

Seams: ``target_check.unchecked_targets`` (the note lines), ``merge.run_merge`` (the result's ``target_notes``; a clean
merge still lands) and ``merge_handback._body`` (the note reaches the ``<!-- merge result -->`` comment). The fake
targets are folders in tmp_path; no real target is read."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import core_targets, merge as merge_module
from tools.agentops.merge import DEFAULT_MAIN, DEFAULT_REMOTE, MergeLock, MergeResult, Waiter, run_merge
from tools.agentops.merge_handback import _body
from tools.agentops.target_check import NOTE_PREFIX, check_targets, note_lines, unchecked_targets

SUITE = f'"{sys.executable}" --version'
REPO = "acme/widgets"
NOTE = "acme/without: no leak words, not checked"


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _tree(root: Path, core_text: str = "# core\nplain text\n") -> Path:
    (root / "tools" / "agentops").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "agents").mkdir(parents=True, exist_ok=True)
    (root / "tools" / "agentops" / "core_manifest.txt").write_text("docs/agents/core.md\n", encoding="utf-8")
    (root / "docs" / "agents" / "core.md").write_text(core_text, encoding="utf-8")
    return root


def _target(folder: Path, *, patterns: dict | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    if patterns is not None:
        (folder / "config").mkdir(exist_ok=True)
        (folder / "config" / "core_leak_patterns.json").write_text(json.dumps(patterns), encoding="utf-8")
    return folder


WIDGET = {"L1": "widget", "L2": "zzqq-never", "L3": "zzqq-never-2"}


# -- the note lines -------------------------------------------------------------------------------------------------

def test_a_folder_without_the_patterns_file_gets_one_note_line_naming_it(tmp_path):
    with_file = _target(tmp_path / "with", patterns=WIDGET)
    without = _target(tmp_path / "without")
    targets = [{"repo": "acme/with", "folder": str(with_file)}, {"repo": "acme/without", "folder": str(without)}]
    assert unchecked_targets(targets, tmp_path) == [NOTE]


def test_a_missing_folder_or_no_folder_contributes_no_note_as_today(tmp_path):
    targets = [{"repo": "a/missing", "folder": str(tmp_path / "nowhere")}, {"repo": "a/nofolder"}]
    assert unchecked_targets(targets, tmp_path) == []


def test_a_target_without_the_file_is_never_refused(tmp_path):
    merged = _tree(tmp_path / "merged", "widget gizmo\n")
    bare = _target(tmp_path / "bare")
    assert check_targets(merged, [{"repo": "acme/without", "folder": str(bare)}], tmp_path) == []


def test_note_lines_carry_the_prefix_the_result_output_and_the_comment_read(tmp_path):
    assert note_lines([NOTE]) == [f"{NOTE_PREFIX}{NOTE}"]
    assert note_lines([]) == []


# -- the merge ------------------------------------------------------------------------------------------------------

def _world(tmp_path: Path, targets: list[dict]):
    """A bare origin whose main is a tiny home with the given targets, a manifest, one core doc."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(bare), str(seed))
    _tree(seed)
    (seed / "config").mkdir()
    (seed / "config" / "orchestration.json").write_text(
        json.dumps({"update_rule": "home", "targets": targets}), encoding="utf-8")
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


def test_a_home_merge_names_the_target_without_the_file_and_still_lands(tmp_path, merge_env):
    with_file = _target(tmp_path / "with", patterns=WIDGET)
    without = _target(tmp_path / "without")
    gone = tmp_path / "nowhere"  # a folder that does not exist contributes nothing
    bare, checkout, lock = _world(tmp_path, [{"repo": "acme/with", "folder": str(with_file)},
                                             {"repo": "acme/without", "folder": str(without)},
                                             {"repo": "acme/gone", "folder": str(gone)}])
    ref = _branch(tmp_path, bare, "# core\nnothing special\n")
    main_before = _git(bare, "rev-parse", "main")
    result = run_merge(ref, SUITE, cwd=checkout, lock_path=lock, lock_timeout=60)
    assert result.ok and result.pushed, result
    assert _git(bare, "rev-parse", "main") != main_before
    assert result.target_notes == [NOTE]


def _push_branch(tmp_path: Path, bare: Path, name: str, rel: str, text: str) -> str:
    """A second branch of the origin: one new file, so it merges beside the first branch's core edit."""
    worker = tmp_path / f"worker-{name}"
    _git(tmp_path, "clone", "-q", str(bare), str(worker))
    _git(worker, "checkout", "-q", "-b", name)
    (worker / rel).write_text(text, encoding="utf-8")
    _git(worker, "add", "-A")
    _git(worker, "commit", "-q", "-m", f"edit {rel}")
    _git(worker, "push", "-q", "origin", name)
    return f"origin/{name}"


def test_a_batched_member_gets_the_holders_target_notes(tmp_path, merge_env, monkeypatch):
    """The holder merges its own branch and one claimed waiter (a batch member); the member's posted result carries
    the same note (the holder's list) and reaches the member's run as its own result."""
    with_file = _target(tmp_path / "with", patterns=WIDGET)
    without = _target(tmp_path / "without")
    bare, checkout, lock = _world(tmp_path, [{"repo": "acme/with", "folder": str(with_file)},
                                             {"repo": "acme/without", "folder": str(without)}])
    ref = _branch(tmp_path, bare, "# core\nnothing special\n")
    member_branch = _push_branch(tmp_path, bare, "feat2", "other2.txt", "two\n")
    waiter = Waiter(ticket="member-ticket", pid=0, info={
        "suite": SUITE, "remote": DEFAULT_REMOTE, "main": DEFAULT_MAIN,
        "cwd": str(checkout.resolve()), "branch": member_branch})
    posted: list = []
    def _line(self):  # the real walk sets line_stop; the fake line is the one waiter
        self.line_stop = None
        return [waiter]
    monkeypatch.setattr(MergeLock, "waiting_line", _line)
    monkeypatch.setattr(MergeLock, "claim", lambda self, waiters: list(waiters))
    monkeypatch.setattr(MergeLock, "post_result", lambda self, w, result: posted.append((w, result)))
    result = run_merge(ref, SUITE, cwd=checkout, lock_path=lock, lock_timeout=60)
    assert result.ok and result.pushed, result
    assert result.batch_branches == [ref, member_branch]
    assert len(posted) == 1
    member, member_result = posted[0]
    assert member is waiter and member_result["batched"] is True
    assert member_result["target_notes"] == [NOTE]


def test_the_merge_command_prints_one_target_check_line_per_note(tmp_path, monkeypatch, capsys):
    notes = [NOTE, "acme/other: no leak words, not checked"]
    monkeypatch.setattr(merge_module, "worktree_refusal", lambda cwd: None)
    monkeypatch.setattr(merge_module, "resolve_suite_cmd", lambda explicit: SUITE)
    monkeypatch.setattr(merge_module, "resolve_suite_env", lambda: {})
    monkeypatch.setattr(merge_module, "resolve_suite_shared", lambda: ())
    monkeypatch.setattr(merge_module, "run_merge", lambda *a, **k: MergeResult(
        ok=True, reason="pushed", message="feat -> main", pushed=True, target_notes=notes))
    args = SimpleNamespace(cwd=tmp_path, branch="origin/feat", suite=SUITE, lock=tmp_path / "merge.lock", timeout=60)
    assert merge_module.run_merge_command(args) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line for line in lines if line.startswith(NOTE_PREFIX)] == [f"{NOTE_PREFIX}{note}" for note in notes]


def test_the_note_reaches_the_merge_result_comment_source(tmp_path):
    output = "merged: feat -> main (suite ran 1x, 0 rebase(s), head abc)\n" + f"{NOTE_PREFIX}{NOTE}\n" + \
        "selected 3 of 3\n"
    record = {"rc": 0, "pushed_sha": "a" * 40, "in_line_s": 5, "suite_s": 12, "failed": 0,
              "output": output, "suite_output": "-"}
    body = _body(record, "r.json", "feat")
    assert body.count(f"{NOTE_PREFIX}{NOTE}") == 1


def test_the_comment_has_one_target_check_line_per_note():
    notes = [NOTE, "acme/other: no leak words, not checked"]
    output = "merged: feat -> main (suite ran 1x, 0 rebase(s), head abc)\n" + \
        "".join(f"{NOTE_PREFIX}{note}\n" for note in notes) + "selected 3 of 3\n"
    record = {"rc": 0, "pushed_sha": "a" * 40, "in_line_s": 5, "suite_s": 12, "failed": 0,
              "output": output, "suite_output": "-"}
    body = _body(record, "r.json", "feat")
    assert [line for line in body.splitlines() if line.startswith(NOTE_PREFIX)] == \
        [f"{NOTE_PREFIX}{note}" for note in notes]
