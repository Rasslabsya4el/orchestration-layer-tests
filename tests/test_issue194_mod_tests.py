"""Issue 194: a merge runs the tests of every mod its change touches, after the pytest suite.

Seam: ``run_merge`` on fake repos (bare origin + clones) with the mod runner (``merge.MOD_TEST_RUNNER``) faked;
`claude plugin test` never starts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tools.agentops import merge as merge_module
from tools.agentops.merge import run_merge
from tools.agentops.util import no_window_flags

GREEN = f'"{sys.executable}" -c "pass"'


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", creationflags=no_window_flags())
    assert result.returncode == 0, f"git {args} failed in {cwd}: {result.stderr}"
    return result


def _clone(bare: Path, dest: Path) -> Path:
    _git(dest.parent, "clone", "-q", str(bare), str(dest))
    _git(dest, "config", "user.email", "agentops-test@example.invalid")
    _git(dest, "config", "user.name", "agentops test")
    return dest


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(merge_module, "SUITE_OUTPUT_DIR", tmp_path / "suite_out")
    monkeypatch.setattr(merge_module, "MERGE_LOG_PATH", tmp_path / "merge.log")
    monkeypatch.setenv(merge_module.ENV_MERGE_LOG, str(tmp_path / "merge.log"))
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = _clone(bare, tmp_path / "seed")
    for rel in ("mods/pob-slots/hooks/x.ts", "mods/ticket-links/hooks/y.ts", "mods/shared/a.ts", "tools/t.py"):
        _write(seed, rel, "v0\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "initial")
    _git(seed, "push", "-q", "origin", "main")
    checkout = _clone(bare, tmp_path / "checkout")
    lock = tmp_path / "var" / "locks" / "merge.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    return bare, checkout, lock, tmp_path


def _branch(tmp_path: Path, bare: Path, name: str, rel: str) -> str:
    worker = _clone(bare, tmp_path / f"worker-{name}")
    _git(worker, "checkout", "-q", "-b", name)
    _write(worker, rel, "v1\n")
    _git(worker, "add", "-A")
    _git(worker, "commit", "-q", "-m", "change")
    _git(worker, "push", "-q", "origin", name)
    return f"origin/{name}"


def _merge(world, rel: str, monkeypatch, code: int = 0, prints: str = ""):
    bare, checkout, lock, tmp_path = world
    calls: list[str] = []

    def fake(name: str, root: Path) -> int:
        assert (Path(root) / "mods" / name).is_dir()  # run in the suite's checkout
        calls.append(name)
        if prints:
            print(prints)
        return code

    monkeypatch.setattr(merge_module, "MOD_TEST_RUNNER", fake)
    ref = _branch(tmp_path, bare, "feat", rel)
    result = run_merge(ref, GREEN, cwd=checkout, lock_path=lock, lock_timeout=60)
    return result, calls, (tmp_path / "merge.log").read_text(encoding="utf-8")


def test_row1_a_touched_mod_is_run_once(world, monkeypatch) -> None:
    result, calls, _log = _merge(world, "mods/pob-slots/hooks/x.ts", monkeypatch)
    assert result.ok, result.render()
    assert calls == ["pob-slots"]


def test_row1_shared_runs_every_mod_but_shared(world, monkeypatch) -> None:
    result, calls, _log = _merge(world, "mods/shared/a.ts", monkeypatch)
    assert result.ok, result.render()
    assert sorted(calls) == ["pob-slots", "ticket-links"]


def test_row1_a_change_outside_mods_runs_no_mod(world, monkeypatch) -> None:
    result, calls, log = _merge(world, "tools/t.py", monkeypatch)
    assert result.ok, result.render()
    assert calls == []
    assert "mod tests" not in log


def test_row2_a_red_mod_makes_the_merge_red_and_names_it(world, monkeypatch) -> None:
    bare = world[0]
    before = _git(bare, "rev-parse", "main").stdout.strip()
    result, calls, log = _merge(world, "mods/pob-slots/hooks/x.ts", monkeypatch, code=1)
    assert not result.ok and result.reason == "red-suite", result.render()
    assert "pob-slots" in result.message
    assert calls == ["pob-slots"]
    assert _git(bare, "rev-parse", "main").stdout.strip() == before, "a red mod moved main"
    assert "mod tests: pob-slots" in log


def test_row2_a_green_mod_lands_and_the_log_line_names_it(world, monkeypatch) -> None:
    result, _calls, log = _merge(world, "mods/pob-slots/hooks/x.ts", monkeypatch, code=0)
    assert result.ok and result.pushed, result.render()
    assert "mod tests: pob-slots " in log


def test_row2_a_batch_triage_drops_the_branch_whose_change_breaks_the_mod(world, monkeypatch) -> None:
    """The red-batch triage reads ``mod:<name>`` like a test id: only the branch that breaks the mod is a culprit."""
    bare, checkout, _lock, tmp_path = world
    good = _branch(tmp_path, bare, "good", "tools/t.py")
    bad = _branch(tmp_path, bare, "bad", "mods/pob-slots/hooks/x.ts")
    _git(checkout, "fetch", "-q", "origin")  # the branches' refs
    base = _git(checkout, "rev-parse", "origin/main").stdout.strip()
    suite_dir = _clone(bare, tmp_path / "suite")

    def checkout_commit(cwd, commit, shared=(), lock=None):
        _git(suite_dir, "fetch", "-q", str(cwd), commit)  # the triage makes its commits in cwd
        _git(suite_dir, "checkout", "-q", "-f", commit)
        return suite_dir

    class Lock:
        def heartbeat(self) -> None:
            pass

    monkeypatch.setattr(merge_module, "ensure_suite_worktree", checkout_commit)
    monkeypatch.setattr(merge_module, "MOD_TEST_RUNNER",
                        lambda name, root: 1 if "v1" in (Path(root) / "mods" / name / "hooks" / "x.ts").read_text() else 0)
    on_base, culprits = merge_module._triage_red_batch(
        checkout, base, [("good", good), ("bad", bad)], GREEN, ["mod:pob-slots"], suite_dir, None,
        tmp_path / "out.txt", (), Lock())
    assert on_base == []
    assert culprits == {"bad": ["mod:pob-slots"]}


def test_row2_a_mod_with_no_tests_gets_no_line_and_is_not_red(world, monkeypatch) -> None:
    result, calls, log = _merge(world, "mods/pob-slots/hooks/x.ts", monkeypatch,
                                prints="pob-slots:\n  plugin validate: exit 0\n  plugin test: no tests in this project")
    assert result.ok and result.pushed, result.render()
    assert calls == ["pob-slots"]
    assert "mod tests" not in log


def test_row2_the_counts_of_the_runner_output_reach_the_log_line(world, monkeypatch) -> None:
    """The format `mods.run_test` prints: ``  plugin test: 3 pass, 1 fail, exit 1``."""
    result, _calls, log = _merge(world, "mods/pob-slots/hooks/x.ts", monkeypatch, code=1,
                                 prints="pob-slots:\n  plugin validate: exit 0\n  plugin test: 3 pass, 1 fail, exit 1")
    assert not result.ok and result.reason == "red-suite", result.render()
    assert "mod tests: pob-slots 3/1" in log
