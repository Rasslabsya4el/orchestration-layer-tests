"""#110: a branch that adds a word a target forbids is refused when it is queued, not hours later in the line.

Seam: ``cli.main(["merge", <branch>, "--detach" | "", "--cwd", <home checkout>])``. The fake target is a folder in
tmp_path whose leak word is ``zorblat``; the detached runner is replaced by a recorder so no process starts."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.agentops import cli, detach, merge as merge_module, target_check
from tools.agentops.util import no_window_flags

REPO = "acme/widgets"
WORD = {"L1": "zorblat", "L2": "zzqq-never", "L3": "zzqq-never-2"}
FILES = ("f1", "f2")


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True, creationflags=no_window_flags()).stdout.strip()


def _world(tmp_path: Path, *, home: bool = True, targets_listed: bool = True, main_text: str = "# core\nplain\n"):
    """A bare origin whose main is a tiny home: one target with leak word `zorblat`, two manifest core docs."""
    target = tmp_path / "t"
    (target / "config").mkdir(parents=True)
    (target / "config" / "core_leak_patterns.json").write_text(json.dumps(WORD), encoding="utf-8")
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
        (seed / "docs" / "agents" / f"{name}.md").write_text(main_text if name == "f1" else "# core\nplain\n",
                                                             encoding="utf-8")
    listed = [{"repo": REPO, "folder": str(target)}] if targets_listed else []
    settings = {"update_rule": "home", "targets": listed} if home else {}
    (seed / "config" / "orchestration.json").write_text(json.dumps(settings), encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "initial")
    _git(seed, "push", "-q", "origin", "main")
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(bare), str(checkout))
    return bare, checkout


def _branch(tmp_path: Path, bare: Path, name: str, doc: str, text: str) -> str:
    worker = tmp_path / f"worker-{name}"
    _git(tmp_path, "clone", "-q", str(bare), str(worker))
    _git(worker, "checkout", "-q", "-b", name)
    (worker / "docs" / "agents" / f"{doc}.md").write_text(text, encoding="utf-8")
    _git(worker, "commit", "-q", "-am", f"edit {name}")
    _git(worker, "push", "-q", "origin", name)
    return f"origin/{name}"


@pytest.fixture
def queue(tmp_path, monkeypatch):
    """Recorders: detached launches, and the merge log / results the refusal must leave untouched."""
    launched = []
    results = tmp_path / "results"
    monkeypatch.setattr(detach, "launch", lambda argv, kind, branch: launched.append(argv) or (results / "r.json", 1))
    log = tmp_path / "merge.log"
    monkeypatch.setenv(merge_module.ENV_MERGE_LOG, str(log))
    monkeypatch.setattr(merge_module, "MERGE_LOG_PATH", log)
    return launched, results, log


def _no_trace(results: Path, log: Path):
    assert not results.exists() or list(results.iterdir()) == []
    assert not log.exists() or log.read_text(encoding="utf-8") == ""


def test_row1_a_branch_adding_the_word_is_refused_at_queue_with_file_line_word_and_branch(tmp_path, queue, capsys):
    launched, results, log = queue
    bare, checkout = _world(tmp_path)
    ref = _branch(tmp_path, bare, "bad", "f2", "# core\nthe zorblat\n")
    rc = cli.main(["merge", ref, "--detach", "--issue", "1", "--cwd", str(checkout)])
    out = capsys.readouterr().out.strip().splitlines()
    assert rc == 1
    assert out == [f"refused at queue: {REPO} rejects core text: docs/agents/f2.md:2 zorblat (branch {ref})"]
    assert launched == []
    _no_trace(results, log)


def test_row1_foreground_merge_is_refused_before_the_merge_starts(tmp_path, queue, monkeypatch, capsys):
    bare, checkout = _world(tmp_path)
    ref = _branch(tmp_path, bare, "bad", "f2", "# core\nthe zorblat\n")

    def boom(*a, **k):
        raise AssertionError("run_merge must not start")
    monkeypatch.setattr(merge_module, "run_merge", boom)
    rc = cli.main(["merge", ref, "--cwd", str(checkout)])
    assert rc == 1
    assert capsys.readouterr().out.strip() == \
        f"refused at queue: {REPO} rejects core text: docs/agents/f2.md:2 zorblat (branch {ref})"
    _no_trace(*queue[1:])


def test_row2_a_clean_branch_is_queued_as_before(tmp_path, queue, capsys):
    launched, results, log = queue
    bare, checkout = _world(tmp_path)
    ref = _branch(tmp_path, bare, "ok", "f2", "# core\nplain\nmore\n")
    rc = cli.main(["merge", ref, "--detach", "--issue", "1", "--cwd", str(checkout)])
    assert rc == 0
    assert capsys.readouterr().out.startswith("detached: result ")
    assert len(launched) == 1


def test_row3_a_word_main_already_holds_is_not_the_branchs(tmp_path, queue, capsys):
    launched, results, log = queue
    bare, checkout = _world(tmp_path, main_text="# core\nold zorblat\n")
    ref = _branch(tmp_path, bare, "other", "f2", "# core\nplain\nmore\n")
    rc = cli.main(["merge", ref, "--detach", "--issue", "1", "--cwd", str(checkout)])
    assert rc == 0, capsys.readouterr().out
    assert len(launched) == 1


@pytest.mark.parametrize("kwargs", [{"home": False}, {"targets_listed": False}], ids=["not-a-home", "no-targets"])
def test_row4_a_repo_without_targets_runs_no_target_check(tmp_path, queue, monkeypatch, kwargs):
    launched, results, log = queue
    bare, checkout = _world(tmp_path, **kwargs)
    ref = _branch(tmp_path, bare, "bad", "f2", "# core\nthe zorblat\n")
    calls = []
    monkeypatch.setattr(target_check, "check_targets", lambda *a, **k: calls.append(a) or [])
    rc = cli.main(["merge", ref, "--detach", "--issue", "1", "--cwd", str(checkout)])
    assert rc == 0
    assert calls == [] and len(launched) == 1
    assert "--queue-checked" in launched[0]  # #125: the conflict check ran in every repo and found it clean


def test_a_branch_that_does_not_merge_cleanly_is_refused_at_queue_as_a_conflict(tmp_path, queue, capsys):
    """#125: the conflict with main is refused at queue time (no line taken), not left to the head of the line."""
    launched, results, log = queue
    bare, checkout = _world(tmp_path)
    ref = _branch(tmp_path, bare, "clash", "f2", "# core\nthe zorblat\n")
    other = tmp_path / "worker-main"
    _git(tmp_path, "clone", "-q", str(bare), str(other))
    (other / "docs" / "agents" / "f2.md").write_text("# core\ndifferent\n", encoding="utf-8")
    _git(other, "commit", "-q", "-am", "main moves")
    _git(other, "push", "-q", "origin", "main")
    rc = cli.main(["merge", ref, "--detach", "--issue", "1", "--cwd", str(checkout)])
    assert rc == 1
    assert capsys.readouterr().out.strip() == "not merged (conflict): docs/agents/f2.md"
    assert launched == []


def test_the_detached_runner_does_not_repeat_the_check(tmp_path, queue):
    launched, results, log = queue
    bare, checkout = _world(tmp_path)
    ref = _branch(tmp_path, bare, "ok", "f2", "# core\nplain\nmore\n")
    cli.main(["merge", ref, "--detach", "--issue", "1", "--cwd", str(checkout)])
    assert "--queue-checked" in launched[0] and "--detach" not in launched[0]  # checked and clean


def test_a_main_copy_that_cannot_be_built_skips_the_refusal_instead_of_over_refusing(tmp_path, queue, monkeypatch):
    launched, results, log = queue
    bare, checkout = _world(tmp_path, main_text="# core\nold zorblat\n")
    ref = _branch(tmp_path, bare, "other", "f2", "# core\nplain\nmore\n")
    real, calls = merge_module._archive_copy, []

    def second_fails(cwd, treeish, dest):  # the merged copy builds, main's copy does not
        calls.append(treeish)
        return real(cwd, treeish, dest) if len(calls) == 1 else False
    monkeypatch.setattr(merge_module, "_archive_copy", second_fails)
    rc = cli.main(["merge", ref, "--detach", "--issue", "1", "--cwd", str(checkout)])
    assert rc == 0 and len(calls) == 2 and len(launched) == 1
    assert "--queue-checked" not in launched[0]
