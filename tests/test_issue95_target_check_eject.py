"""#95: a refused target check names the guilty batch member, ejects only it and lands the rest.

Seam: ``merge.run_merge`` with a batch (the holder plus claimed waiters, faked as in the #93 batch test). The fake
target is a folder in tmp_path whose leak word is ``widget``; each branch edits its own core file."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.agentops import core_targets, merge as merge_module, target_check
from tools.agentops.merge import DEFAULT_MAIN, DEFAULT_REMOTE, MergeLock, Waiter, run_merge
from tools.agentops.target_check import TargetHit

SUITE = f'"{sys.executable}" --version'
REPO = "acme/widgets"
WIDGET = {"L1": "widget", "L2": "zzqq-never", "L3": "zzqq-never-2"}
FILES = ("f1", "f2", "f3")


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _world(tmp_path: Path):
    """A bare origin whose main is a tiny home: one target with leak word `widget`, three manifest core docs."""
    target = tmp_path / "t"
    (target / "config").mkdir(parents=True)
    (target / "config" / "core_leak_patterns.json").write_text(json.dumps(WIDGET), encoding="utf-8")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(bare), str(seed))
    (seed / "tools" / "agentops").mkdir(parents=True)
    (seed / "docs" / "agents").mkdir(parents=True)
    (seed / "config").mkdir()
    (seed / "tools" / "agentops" / "core_manifest.txt").write_text(
        "".join(f"docs/agents/{name}.md\n" for name in FILES), encoding="utf-8")
    for name in FILES:
        (seed / "docs" / "agents" / f"{name}.md").write_text("# core\nplain\n", encoding="utf-8")
    (seed / "config" / "orchestration.json").write_text(
        json.dumps({"update_rule": "home", "targets": [{"repo": REPO, "folder": str(target)}]}), encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "initial")
    _git(seed, "push", "-q", "origin", "main")
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(bare), str(checkout))
    lock = tmp_path / "var" / "locks" / "merge.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    return bare, checkout, lock


def _branch(tmp_path: Path, bare: Path, name: str, text: str) -> str:
    worker = tmp_path / f"worker-{name}"
    _git(tmp_path, "clone", "-q", str(bare), str(worker))
    _git(worker, "checkout", "-q", "-b", name)
    (worker / "docs" / "agents" / f"{name}.md").write_text(text, encoding="utf-8")
    _git(worker, "commit", "-q", "-am", f"edit {name}")
    _git(worker, "push", "-q", "origin", name)
    return f"origin/{name}"


CLEAN, HIT = "# core\nplain\nmore\n", "# core\nthe widget\n"


def _run_batch(tmp_path, monkeypatch, texts):
    """Run a merge whose holder is the first of three branches and whose claimed waiters are the other two."""
    monkeypatch.setattr(merge_module, "SUITE_OUTPUT_DIR", tmp_path / "suite_out")
    monkeypatch.setattr(merge_module, "MERGE_LOG_PATH", tmp_path / "merge.log")
    monkeypatch.setenv(merge_module.ENV_MERGE_LOG, str(tmp_path / "merge.log"))
    monkeypatch.setenv(merge_module.ENV_SUITE_OUTPUT_DIR, str(tmp_path / "suite_out"))
    monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: False)
    monkeypatch.setattr(merge_module, "_spawn_map_run", lambda cwd, sha: "")
    monkeypatch.setattr(core_targets, "run_core_targets", lambda *a, **k: [])
    bare, checkout, lock = _world(tmp_path)
    refs = [_branch(tmp_path, bare, name, text) for name, text in zip(FILES, texts)]
    waiters = [Waiter(ticket=f"t-{ref}", pid=0, info={
        "suite": SUITE, "remote": DEFAULT_REMOTE, "main": DEFAULT_MAIN,
        "cwd": str(checkout.resolve()), "branch": ref}) for ref in refs[1:]]
    posted, unclaimed = [], []

    def _line(self):
        self.line_stop = None
        return list(waiters)
    monkeypatch.setattr(MergeLock, "waiting_line", _line)
    monkeypatch.setattr(MergeLock, "claim", lambda self, ws: list(ws))
    monkeypatch.setattr(MergeLock, "unclaim", lambda self, ws: unclaimed.extend(w.info["branch"] for w in ws))
    monkeypatch.setattr(MergeLock, "post_result", lambda self, w, result: posted.append((w.info["branch"], result)))
    main_before = _git(bare, "rev-parse", "main")
    result = run_merge(refs[0], SUITE, cwd=checkout, lock_path=lock, lock_timeout=60)
    log = (tmp_path / "merge.log").read_text(encoding="utf-8") if (tmp_path / "merge.log").exists() else ""
    return result, posted, unclaimed, log, bare, main_before, refs


def _landed(bare, name):
    return "plain\nmore" in _git(bare, "show", f"main:docs/agents/{name}.md")


def test_row1_one_member_adds_the_leak_word_the_two_others_land_and_the_third_is_ejected(tmp_path, monkeypatch):
    result, posted, unclaimed, log, bare, _before, refs = _run_batch(tmp_path, monkeypatch, [CLEAN, HIT, CLEAN])
    assert result.ok and result.pushed, result
    assert result.batch_branches == [refs[0], refs[2]]
    line = f"refused: {REPO} rejects core text: docs/agents/f2.md:2 widget (branch {refs[1]})"
    assert [(b, r["reason"], r["message"], r["ok"]) for b, r in posted if b == refs[1]] == \
        [(refs[1], "target-check", line, False)]
    assert f"{refs[1]} | ejected: target-check {REPO} rejects core text: docs/agents/f2.md:2 widget" in log
    assert _landed(bare, "f1") and _landed(bare, "f3") and not _landed(bare, "f2")


def test_row3_two_members_each_add_it_both_are_ejected_with_their_own_branch_and_the_holder_lands(tmp_path, monkeypatch):
    result, posted, unclaimed, log, bare, _before, refs = _run_batch(tmp_path, monkeypatch, [CLEAN, HIT, HIT])
    assert result.ok and result.pushed, result
    assert sorted((b, r["message"]) for b, r in posted if not r["ok"]) == [
        (refs[1], f"refused: {REPO} rejects core text: docs/agents/f2.md:2 widget (branch {refs[1]})"),
        (refs[2], f"refused: {REPO} rejects core text: docs/agents/f3.md:2 widget (branch {refs[2]})")]
    assert log.count("ejected: target-check") == 2
    assert _landed(bare, "f1") and not _landed(bare, "f2") and not _landed(bare, "f3")


def test_the_holder_adding_it_is_refused_with_its_branch_and_the_clean_members_go_back_in_line(tmp_path, monkeypatch):
    result, posted, unclaimed, log, bare, before, refs = _run_batch(tmp_path, monkeypatch, [HIT, CLEAN, CLEAN])
    assert not result.ok and result.reason == "target-check"
    assert result.message == f"refused: {REPO} rejects core text: docs/agents/f1.md:2 widget (branch {refs[0]})"
    assert posted == [] and sorted(unclaimed) == [refs[1], refs[2]]
    assert _git(bare, "rev-parse", "main") == before


def test_a_hit_no_single_member_holds_refuses_the_batch_naming_every_member(tmp_path, monkeypatch):
    def together(tree, targets, home, *, scan_avoid=True):  # the hit exists only when f2 and f3 are both in the tree
        both = all("widget" in (tree / "docs" / "agents" / f"{n}.md").read_text(encoding="utf-8") for n in ("f2", "f3"))
        return [TargetHit(REPO, "docs/agents/f2.md", 2, "widget")] if both else []
    monkeypatch.setattr(target_check, "check_targets", together)
    result, posted, unclaimed, log, bare, before, refs = _run_batch(tmp_path, monkeypatch, [CLEAN, HIT, HIT])
    assert not result.ok and result.reason == "target-check"
    assert result.message == (f"refused: {REPO} rejects core text: docs/agents/f2.md:2 widget "
                              f"(branches {', '.join(refs)})")
    assert posted == [] and sorted(unclaimed) == [refs[1], refs[2]]
    assert _git(bare, "rev-parse", "main") == before
