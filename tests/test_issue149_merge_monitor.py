"""#149 unit 2: a merge that lands on main installs the monitor when its diff touches the monitor's files.

Seams: ``merge.run_merge`` (the result's ``monitor_note``; the merge stays ``pushed`` whatever the install says),
``merge.run_merge_command`` (the printed line) and ``merge_handback._body`` (the comment). The install itself is
faked through ``monitor_install._default_runner``: no test starts a real ``mods install``."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import core_targets, merge as merge_module, monitor_install
from tools.agentops.merge import DEFAULT_MAIN, DEFAULT_REMOTE, MergeLock, Waiter, run_merge, run_merge_command
from tools.agentops.merge_handback import _body

SUITE = f'"{sys.executable}" --version'
SOURCE = "demo-monitor"


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _world(tmp_path: Path):
    """A bare origin whose main names the monitor source and holds one monitor file and one other file."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(bare), str(seed))
    (seed / "config").mkdir()
    (seed / "config" / "orchestration.json").write_text(
        json.dumps({"monitor": {"source": SOURCE, "plugin_id": "orch-monitor"}}), encoding="utf-8")
    (seed / "mods" / SOURCE / "hooks").mkdir(parents=True)
    (seed / "mods" / SOURCE / "hooks" / "x.tsx").write_text("one\n", encoding="utf-8")
    (seed / "other.txt").write_text("1\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "initial")
    _git(seed, "push", "-q", "origin", "main")
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(bare), str(checkout))
    lock = tmp_path / "var" / "locks" / "merge.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    return bare, checkout, lock


def _push_branch(tmp_path: Path, bare: Path, name: str, rel: str, text: str) -> str:
    worker = tmp_path / f"worker-{name}"
    _git(tmp_path, "clone", "-q", str(bare), str(worker))
    _git(worker, "checkout", "-q", "-b", name)
    path = worker / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(worker, "add", "-A")
    _git(worker, "commit", "-q", "-m", f"edit {rel}")
    _git(worker, "push", "-q", "origin", name)
    return f"origin/{name}"


class FakeRunner:
    def __init__(self, error: BaseException | None = None):
        self.calls: list[tuple[Path, str]] = []
        self.error = error

    def __call__(self, root: Path, source: str) -> None:
        self.calls.append((root, source))
        if self.error is not None:
            raise self.error


@pytest.fixture
def merge_env(tmp_path, monkeypatch):
    monkeypatch.setattr(merge_module, "SUITE_OUTPUT_DIR", tmp_path / "suite_out")
    monkeypatch.setattr(merge_module, "MERGE_LOG_PATH", tmp_path / "merge.log")
    monkeypatch.setattr(merge_module, "resolve_select_tests", lambda: False)
    monkeypatch.setattr(merge_module, "_spawn_map_run", lambda cwd, sha: "")
    monkeypatch.setattr(core_targets, "run_core_targets", lambda *a, **k: [])
    monkeypatch.setattr(merge_module, "MOD_TEST_RUNNER", lambda name, root: 0)  # #194: a touched mod's tests are not this file's seam
    runner = FakeRunner()
    monkeypatch.setattr(monitor_install, "_default_runner", runner)
    return runner


def _command_args(checkout: Path, lock: Path, branch: str):
    return SimpleNamespace(cwd=checkout, branch=branch, suite=SUITE, lock=lock, timeout=60, queue_checked=True,
                           issue=None)


@pytest.fixture
def command_env(monkeypatch):
    monkeypatch.setattr(merge_module, "resolve_suite_cmd", lambda explicit: SUITE)
    monkeypatch.setattr(merge_module, "resolve_suite_env", lambda: {})
    monkeypatch.setattr(merge_module, "resolve_suite_shared", lambda: ())


def test_a_merge_touching_the_monitor_installs_once_and_prints_the_line(tmp_path, merge_env, command_env, capsys):
    bare, checkout, lock = _world(tmp_path)
    ref = _push_branch(tmp_path, bare, "feat", f"mods/{SOURCE}/hooks/x.tsx", "two\n")
    rc = run_merge_command(_command_args(checkout, lock, ref))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert merge_env.calls == [(checkout, SOURCE)]
    assert out.splitlines().count("monitor installed") == 1
    assert out.index("merged:") < out.index("monitor installed")


def test_a_merge_touching_no_monitor_file_installs_nothing_and_prints_no_line(tmp_path, merge_env, command_env, capsys):
    bare, checkout, lock = _world(tmp_path)
    ref = _push_branch(tmp_path, bare, "feat", "other.txt", "2\n")
    rc = run_merge_command(_command_args(checkout, lock, ref))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert merge_env.calls == []
    assert "monitor" not in out


def test_a_failing_install_leaves_the_merge_pushed(tmp_path, merge_env):
    merge_env.error = RuntimeError("boom")
    bare, checkout, lock = _world(tmp_path)
    ref = _push_branch(tmp_path, bare, "feat", f"mods/{SOURCE}/hooks/x.tsx", "two\n")
    main_before = _git(bare, "rev-parse", "main")
    result = run_merge(ref, SUITE, cwd=checkout, lock_path=lock, lock_timeout=60)
    assert result.ok is True and result.reason == "pushed" and result.pushed
    assert result.monitor_note == "monitor install failed: boom"
    assert _git(bare, "rev-parse", "main") != main_before
    assert len(merge_env.calls) == 1


def test_a_batch_puts_the_same_note_on_the_member_result(tmp_path, merge_env, monkeypatch):
    bare, checkout, lock = _world(tmp_path)
    ref = _push_branch(tmp_path, bare, "feat", f"mods/{SOURCE}/hooks/x.tsx", "two\n")
    member_branch = _push_branch(tmp_path, bare, "feat2", "other2.txt", "two\n")
    waiter = Waiter(ticket="member-ticket", pid=0, info={
        "suite": SUITE, "remote": DEFAULT_REMOTE, "main": DEFAULT_MAIN,
        "cwd": str(checkout.resolve()), "branch": member_branch})
    posted: list = []

    def _line(self):
        self.line_stop = None
        return [waiter]

    monkeypatch.setattr(MergeLock, "waiting_line", _line)
    monkeypatch.setattr(MergeLock, "claim", lambda self, waiters: list(waiters))
    monkeypatch.setattr(MergeLock, "post_result", lambda self, w, result: posted.append((w, result)))
    result = run_merge(ref, SUITE, cwd=checkout, lock_path=lock, lock_timeout=60)
    assert result.ok and result.batch_branches == [ref, member_branch]
    assert result.monitor_note == "monitor installed"
    assert len(merge_env.calls) == 1
    assert [r["monitor_note"] for _w, r in posted] == ["monitor installed"]


def test_the_comment_of_a_landed_merge_carries_the_monitor_line():
    for line in ("monitor installed", "monitor install failed: boom"):
        output = "merged: feat -> main (suite ran 1x, 0 rebase(s), head abc)\n" + f"{line}\n" + "selected 3 of 3\n"
        record = {"rc": 0, "pushed_sha": "a" * 40, "in_line_s": 5, "suite_s": 12, "failed": 0,
                  "output": output, "suite_output": "-"}
        assert _body(record, "r.json", "feat").splitlines().count(line) == 1


def test_the_comment_of_a_merge_without_the_line_has_none():
    record = {"rc": 0, "pushed_sha": "a" * 40, "in_line_s": 5, "suite_s": 12, "failed": 0,
              "output": "merged: monitor installed on the way (not a line of its own)\n", "suite_output": "-"}
    assert "monitor" not in _body(record, "r.json", "feat")


def _fail_git(monkeypatch, matches, stderr):
    """``merge._git`` answers a failure for the one call ``matches(args)`` picks; every other call is real."""
    real = merge_module._git
    pushed: list[bool] = []

    def fake(cwd, *args, **kwargs):
        if args[:1] == ("push",):
            pushed.append(True)
        if pushed and matches(args):  # only the calls after the push: the sync before it stays real
            return subprocess.CompletedProcess(["git", *args], 1, stdout="", stderr=stderr)
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(merge_module, "_git", fake)


def test_a_monitor_diff_with_local_main_not_fast_forwarded_installs_nothing(tmp_path, merge_env, monkeypatch):
    _fail_git(monkeypatch, lambda a: a[:2] == ("merge", "--ff-only"), "fatal: not possible")
    bare, checkout, lock = _world(tmp_path)
    ref = _push_branch(tmp_path, bare, "feat", f"mods/{SOURCE}/hooks/x.tsx", "two\n")
    result = run_merge(ref, SUITE, cwd=checkout, lock_path=lock, lock_timeout=60)
    assert result.ok is True and result.reason == "pushed" and result.pushed
    assert result.monitor_note == "monitor install failed: local main not fast-forwarded"
    assert merge_env.calls == []


def test_a_non_monitor_diff_with_local_main_not_fast_forwarded_prints_no_monitor_line(tmp_path, merge_env, monkeypatch):
    _fail_git(monkeypatch, lambda a: a[:2] == ("merge", "--ff-only"), "fatal: not possible")
    bare, checkout, lock = _world(tmp_path)
    ref = _push_branch(tmp_path, bare, "feat", "other.txt", "2\n")
    result = run_merge(ref, SUITE, cwd=checkout, lock_path=lock, lock_timeout=60)
    assert result.ok and result.monitor_note == "" and merge_env.calls == []


def test_a_failed_listing_of_the_landed_files_is_a_failed_line_not_a_skip(tmp_path, merge_env, monkeypatch):
    _fail_git(monkeypatch, lambda a: a[:2] == ("diff", "--name-only") and "..." not in a[2], "fatal: bad object")
    bare, checkout, lock = _world(tmp_path)
    ref = _push_branch(tmp_path, bare, "feat", f"mods/{SOURCE}/hooks/x.tsx", "two\n")
    result = run_merge(ref, SUITE, cwd=checkout, lock_path=lock, lock_timeout=60)
    assert result.ok is True and result.reason == "pushed" and result.pushed
    assert result.monitor_note == "monitor install failed: cannot list the landed files"
    assert merge_env.calls == []
