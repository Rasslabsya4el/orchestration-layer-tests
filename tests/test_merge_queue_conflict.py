"""#125: a branch that conflicts with origin/main is refused when it is queued, not after the wait.

Seams: ``cli.main(["merge", <branch>, "--detach" | "", "--cwd", <checkout>])`` (rows 1, 2, 3, 5: the detached runner
is a recorder, the foreground ``run_merge`` must not start) and ``merge.run_merge`` (row 4: the head-of-line check).
The fake origin is a bare repo in tmp_path; no real target and no real suite is read (the suite is ``--version``)."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tools.agentops import cli, core_targets, detach, merge as merge_module
from tools.agentops.merge import queue_target_refusals, run_merge
from tools.agentops.util import no_window_flags

SUITE = f'"{sys.executable}" --version'
CONFLICT_LINE = "not merged (conflict): docs/f1.md"


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True, creationflags=no_window_flags()).stdout.strip()


def _repo(tmp_path: Path, settings: dict | None):
    """A bare origin with two docs on main; ``settings`` is written to config/orchestration.json (None: no file)."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(bare), str(seed))
    (seed / "docs").mkdir()
    (seed / "docs" / "f1.md").write_text("base\n", encoding="utf-8")
    (seed / "docs" / "f2.md").write_text("base\n", encoding="utf-8")
    if settings is not None:
        (seed / "config").mkdir()
        (seed / "config" / "orchestration.json").write_text(json.dumps(settings), encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "initial")
    _git(seed, "push", "-q", "origin", "main")
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(bare), str(checkout))
    lock = tmp_path / "var" / "locks" / "merge.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    return bare, checkout, lock


def _branch(tmp_path: Path, bare: Path, name: str, doc: str, text: str) -> str:
    """A branch cut from the current origin/main, one edited doc, pushed; returns ``origin/<name>``."""
    worker = tmp_path / f"worker-{name}"
    _git(tmp_path, "clone", "-q", str(bare), str(worker))
    _git(worker, "checkout", "-q", "-b", name)
    (worker / "docs" / f"{doc}.md").write_text(text, encoding="utf-8")
    _git(worker, "commit", "-q", "-am", f"edit {name}")
    _git(worker, "push", "-q", "origin", name)
    return f"origin/{name}"


def _land_on_main(tmp_path: Path, bare: Path, doc: str, text: str) -> None:
    """A commit that lands on origin/main (what a second branch's merge does to main)."""
    worker = Path(tempfile.mkdtemp(dir=tmp_path, prefix=f"main-{doc}-"))  # an empty folder: a fresh clone each call
    _git(tmp_path, "clone", "-q", str(bare), str(worker))
    (worker / "docs" / f"{doc}.md").write_text(text, encoding="utf-8")
    _git(worker, "commit", "-q", "-am", f"main takes {doc}")
    _git(worker, "push", "-q", "origin", "main")


@pytest.fixture
def queue(tmp_path, monkeypatch):
    """Recorders: detached launches, the merge log and the result folder that a refusal must leave untouched."""
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


@pytest.mark.parametrize("settings", [{"update_rule": "home", "targets": []}, {"update_rule": "home"}, None],
                         ids=["targets-empty", "no-targets-key", "no-config"])
def test_row1_a_branch_conflicting_with_main_is_refused_at_queue_with_file_and_no_line(tmp_path, queue, capsys,
                                                                                      settings):
    launched, results, log = queue
    bare, checkout, _lock = _repo(tmp_path, settings)
    ref = _branch(tmp_path, bare, "clash", "f1", "branch side\n")
    _land_on_main(tmp_path, bare, "f1", "main side\n")
    rc = cli.main(["merge", ref, "--detach", "--issue", "1", "--cwd", str(checkout)])
    out = capsys.readouterr().out.strip().splitlines()
    assert rc == 1
    assert out == [CONFLICT_LINE]
    assert launched == []
    _no_trace(results, log)


def test_row2_a_repo_whose_config_has_no_targets_refuses_a_conflicting_branch(tmp_path, queue, capsys):
    launched, results, log = queue
    bare, checkout, _lock = _repo(tmp_path, {"update_rule": "home"})
    ref = _branch(tmp_path, bare, "clash", "f1", "branch side\n")
    _land_on_main(tmp_path, bare, "f1", "main side\n")
    assert queue_target_refusals(ref, cwd=checkout) == [CONFLICT_LINE]
    rc = cli.main(["merge", ref, "--detach", "--issue", "1", "--cwd", str(checkout)])
    assert rc == 1 and capsys.readouterr().out.strip() == CONFLICT_LINE
    assert launched == []
    _no_trace(results, log)


def test_row3_a_clean_branch_is_queued_as_before_with_queue_checked(tmp_path, queue, capsys):
    launched, results, log = queue
    bare, checkout, _lock = _repo(tmp_path, {"update_rule": "home"})
    ref = _branch(tmp_path, bare, "ok", "f2", "branch side\n")
    rc = cli.main(["merge", ref, "--detach", "--issue", "1", "--cwd", str(checkout)])
    assert rc == 0, capsys.readouterr().out
    assert len(launched) == 1
    assert "--queue-checked" in launched[0] and "--detach" not in launched[0]


def test_row4_a_branch_clean_at_queue_that_conflicts_after_a_landing_is_refused_at_the_head(tmp_path, monkeypatch):
    """Row 4. No existing test covers the head-of-line conflict, so it is written here. The branch is clean when
    queued (checked, ``[]``); a second commit then lands on f2 while it waits (pushed to origin/main directly, which
    is what a landed branch leaves on main); the head check in ``run_merge`` reports ``conflict`` (no rebase)."""
    bare, checkout, lock = _repo(tmp_path, {"update_rule": "home"})
    monkeypatch.setattr(merge_module, "SUITE_OUTPUT_DIR", tmp_path / "suite_out")
    monkeypatch.setattr(merge_module, "MERGE_LOG_PATH", tmp_path / "merge.log")
    monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: False)
    monkeypatch.setattr(merge_module, "_spawn_map_run", lambda cwd, sha: "")
    monkeypatch.setattr(core_targets, "run_core_targets", lambda *a, **k: [])
    ref = _branch(tmp_path, bare, "waits", "f2", "branch side\n")
    assert queue_target_refusals(ref, cwd=checkout) == []
    _land_on_main(tmp_path, bare, "f2", "second branch side\n")
    result = run_merge(ref, SUITE, cwd=checkout, lock_path=lock, lock_timeout=60)
    assert not result.ok and result.reason == "conflict", result
    assert result.conflicting_files == ["docs/f2.md"]
    assert result.rebases == 0


def test_row5_the_foreground_merge_refuses_a_conflicting_branch_before_run_merge(tmp_path, queue, monkeypatch, capsys):
    launched, results, log = queue
    bare, checkout, _lock = _repo(tmp_path, {"update_rule": "home"})
    ref = _branch(tmp_path, bare, "clash", "f1", "branch side\n")
    _land_on_main(tmp_path, bare, "f1", "main side\n")

    def boom(*a, **k):
        raise AssertionError("run_merge must not start")
    monkeypatch.setattr(merge_module, "run_merge", boom)
    rc = cli.main(["merge", ref, "--cwd", str(checkout)])
    assert rc == 1
    assert capsys.readouterr().out.strip() == CONFLICT_LINE
    _no_trace(results, log)
