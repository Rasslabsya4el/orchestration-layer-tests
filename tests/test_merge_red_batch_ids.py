"""#189 unit 2 (row 3): a red batch of five drops only the branch whose tests fail; the other four land.

Seam: ``merge.run_merge`` with a batch (the holder plus claimed waiters, faked as in test_issue95). The batch's own suite
run is a stub that answers red once, naming the ids of one test file that only one branch adds; the triage re-runs are
real pytest on real git worktrees in a tmp repo. ``_triage_red_batch`` is wrapped by a spy to read what it returns."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tools.agentops import merge as merge_module
from tools.agentops.merge import DEFAULT_MAIN, DEFAULT_REMOTE, MergeLock, SuiteRun, Waiter, run_merge

ADDED = "tests/test_neutral_added.py"
ODD_ID = f'{ADDED}::test_added_row[{{"full_at": 1, "nodes": 3}}]'
RED_IDS = [f"{ADDED}::test_added_row[p{n}]" for n in range(17)] + [ODD_ID]
SUITE = f'"{sys.executable}" -m pytest tests -q -p no:faulthandler'
NAMES = ("alpha", "bravo", "charlie", "delta", "echo")  # the holder is the first; charlie adds the red file


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _world(tmp_path: Path):
    """A bare origin whose main has one plain file; a checkout of it; the merge lock path."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(bare), str(seed))
    (seed / "notes").mkdir()
    (seed / "notes" / "base.txt").write_text("base\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "initial")
    _git(seed, "push", "-q", "origin", "main")
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(bare), str(checkout))
    lock = tmp_path / "var" / "locks" / "merge.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    return bare, checkout, lock


def _added_file_text() -> str:
    """Eighteen tests, all failing: seventeen plain ids and one whose id holds quotes, a space, braces and a colon."""
    return (
        "import pytest\n\n"
        "@pytest.mark.parametrize('v', [\n"
        + "".join(f"    pytest.param(1, id='p{n}'),\n" for n in range(17))
        + "    pytest.param(1, id='{\"full_at\": 1, \"nodes\": 3}'),\n"
        "])\n"
        "def test_added_row(v):\n"
        "    assert False\n"
    )


def _branch(tmp_path: Path, bare: Path, name: str, relpath: str, text: str) -> str:
    worker = tmp_path / f"worker-{name}"
    _git(tmp_path, "clone", "-q", str(bare), str(worker))
    _git(worker, "checkout", "-q", "-b", name)
    (worker / Path(relpath).parent).mkdir(parents=True, exist_ok=True)
    (worker / relpath).write_text(text, encoding="utf-8")
    _git(worker, "add", "-A")
    _git(worker, "commit", "-q", "-m", f"add {relpath} on {name}")
    _git(worker, "push", "-q", "origin", name)
    return f"origin/{name}"


def _run_red_batch(tmp_path, monkeypatch):
    (tmp_path / "suite_out").mkdir()  # the real suite run makes it; this run is stubbed
    monkeypatch.setattr(merge_module, "SUITE_OUTPUT_DIR", tmp_path / "suite_out")
    monkeypatch.setattr(merge_module, "MERGE_LOG_PATH", tmp_path / "merge.log")
    monkeypatch.setenv(merge_module.ENV_MERGE_LOG, str(tmp_path / "merge.log"))
    monkeypatch.setenv(merge_module.ENV_SUITE_OUTPUT_DIR, str(tmp_path / "suite_out"))
    monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: False)
    monkeypatch.setattr(merge_module, "_spawn_map_run", lambda cwd, sha: "")
    from tools.agentops import core_targets
    monkeypatch.setattr(core_targets, "run_core_targets", lambda *a, **k: [])

    bare, checkout, lock = _world(tmp_path)
    refs = []
    for name in NAMES:
        if name == "charlie":
            refs.append(_branch(tmp_path, bare, name, ADDED, _added_file_text()))
        else:
            refs.append(_branch(tmp_path, bare, name, f"notes/{name}.txt", f"{name}\n"))
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

    suite_calls: list[int] = []

    def batch_suite(*_a, **_k):  # the batch's own run: red once (the ids of the added file), green on the re-run
        suite_calls.append(1)
        if len(suite_calls) == 1:
            return SuiteRun(False, "\n".join(f"FAILED {i} - assert False" for i in RED_IDS) + "\n", 1,
                            rechecked_done=True, red_ids=list(RED_IDS), still=list(RED_IDS))
        return SuiteRun(True, "18 passed\n", 1)

    monkeypatch.setattr(merge_module, "_run_merge_suite", batch_suite)

    triage_results = []
    real_triage = merge_module._triage_red_batch

    def spy(*a, **k):
        out = real_triage(*a, **k)
        triage_results.append(out)
        return out

    monkeypatch.setattr(merge_module, "_triage_red_batch", spy)
    result = run_merge(refs[0], SUITE, cwd=checkout, lock_path=lock, lock_timeout=60)
    log_path = tmp_path / "merge.log"
    log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    return result, posted, log, bare, refs, triage_results, suite_calls


def test_row3_red_batch_of_five_ejects_the_one_branch_whose_tests_fail_and_the_other_four_land(tmp_path, monkeypatch):
    result, posted, log, bare, refs, triage_results, suite_calls = _run_red_batch(tmp_path, monkeypatch)

    # the triage: nothing red on base, the one culprit is charlie with its eighteen ids
    assert triage_results == [([], {refs[2]: RED_IDS})]
    # charlie gets red-suite, the rest of the batch is rerun and lands
    charlie = [r for b, r in posted if b == refs[2]]
    assert len(charlie) == 1 and charlie[0]["reason"] == "red-suite" and charlie[0]["ok"] is False
    assert result.ok and result.pushed, result
    assert len(suite_calls) == 2 and result.suite_runs == 2
    landed = _git(bare, "ls-tree", "--name-only", "main", "notes/")
    assert all(f"notes/{n}.txt" in landed for n in ("alpha", "bravo", "delta", "echo"))
    assert ADDED not in _git(bare, "ls-tree", "-r", "--name-only", "main")
    # no red-on-base anywhere: not in the result, not in the merge log
    assert result.reason != "red-on-base" and "red-on-base" not in log
    assert "red-on-base" not in str(result.message)
