"""#454 unit 4 of 6: every code-built commit message passes `require_clean_built` right before the git call that
writes it. Each site is driven through its real function with a branch or slug named `fix-#7` / `closes #7`: the
step refuses in its own idiom, and the git calls it made hold no commit for that message. A clean-named control
in the same test shows the recorder does see a commit when nothing refuses, so an empty record means refusal.

Real git runs in tmp repos (never the real origin). Each site's git helper is wrapped by a recorder that passes
every call through. Sites that need the merge lock, the origin sync or the waiter line stub those collaborators;
`_merge_attempt` stops before the suite with a sentinel raised from `wait_while_held`.
"""

from __future__ import annotations

import contextlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from tools.agentops import core_update, line_check, merge, port, test_copy_send, worktree
from tools.agentops.closing_keywords import ClosingKeywordError, require_clean_built
from tools.agentops.core_update import CoreUpdateError
from tools.agentops.merge import Waiter
from tools.agentops.port import PortError
from tools.agentops.test_copy_check import BUILTIN, Patterns


class _Stop(Exception):
    """Ends a site's flow right after the point under test (no suite runs)."""


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True,
                          encoding="utf-8", errors="replace")
    return done.stdout.strip()


def _init(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(root, "config", "user.email", "agentops-test@example.invalid")
    _git(root, "config", "user.name", "agentops test")


@dataclass(frozen=True)
class Repo:
    root: Path
    base: str   # main's commit
    fix: str    # branch `fix-#7`: one commit off base
    clean: str  # branch `clean-x`: one commit off base


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    root = tmp_path / "repo"
    _init(root)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "initial")
    base = _git(root, "rev-parse", "HEAD")
    tips = {}
    for name, filename in (("fix-#7", "fix.txt"), ("clean-x", "clean.txt")):
        _git(root, "checkout", "-q", "-b", name, base)
        (root / filename).write_text(f"{name}\n", encoding="utf-8")
        _git(root, "add", filename)
        _git(root, "commit", "-q", "-m", f"unit work on {name}")
        tips[name] = _git(root, "rev-parse", "HEAD")
        _git(root, "checkout", "-q", "main")
    return Repo(root=root, base=base, fix=tips["fix-#7"], clean=tips["clean-x"])


def _record(monkeypatch, module, name: str, argv_of) -> list[tuple]:
    """Wrap ``module.<name>`` so every call is recorded as ``argv_of(args, kwargs)`` and passed through."""
    real = getattr(module, name)
    calls: list[tuple] = []

    def recording(*args, **kwargs):
        calls.append(tuple(argv_of(args, kwargs)))
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, recording)
    return calls


def _messages(calls: list[tuple]) -> list[str]:
    """The -m message of every commit and commit-tree call recorded."""
    out = []
    for argv in calls:
        if argv and argv[0] in ("commit", "commit-tree") and "-m" in argv:
            out.append(argv[argv.index("-m") + 1])
    return out


class _Lock:
    """The merge lock's methods the sites call, with a fixed line of waiters."""

    def __init__(self, waiters=()):
        self._waiters = list(waiters)
        self.line_stop = None
        self.ticket_ns = 0

    def heartbeat(self):
        pass

    def release(self):
        pass

    def refresh_priorities(self, **kwargs):
        pass

    def waiting_line(self):
        return list(self._waiters)

    def claim(self, line):
        return list(line)

    def unclaim(self, waiters):
        pass

    def post_result(self, waiter, result):
        pass

    def set_progress(self, path):
        pass


# -- the helper itself -----------------------------------------------------------------------------

def test_helper_refuses_built_branch_name():
    with pytest.raises(ClosingKeywordError, match="fix"):
        require_clean_built("Merge branch 'fix-#7'", where="the merge commit")


# -- merge.py: triage, author check, real merge, waiter ---------------------------------------------

def test_triage_refuses_closing_branch_before_its_commit(repo: Repo, monkeypatch, tmp_path: Path):
    checked: list[str] = []
    monkeypatch.setattr(merge, "ensure_suite_worktree", lambda cwd, commit, **kw: checked.append(commit))
    calls = _record(monkeypatch, merge, "_git", lambda a, k: a[1:])
    suite_dir = tmp_path / "suite"
    suite_dir.mkdir()
    merge._triage_red_batch(repo.root, repo.base, [("fix-#7", repo.fix), ("clean-x", repo.clean)],
                            "python -m pytest -q", [], suite_dir, None, tmp_path / "out.txt", (), _Lock())
    assert _messages(calls) == ["Triage clean-x"]
    assert len(checked) == 2 and checked[0] == repo.base  # base, then the clean member's triage commit only


def test_author_check_refuses_closing_branch_before_its_commit(repo: Repo, monkeypatch):
    monkeypatch.setattr(merge, "_target_check_hits", lambda *a, **k: [])
    calls = _record(monkeypatch, merge, "_git", lambda a, k: a[1:])
    authors = merge._target_hit_authors(repo.root, repo.base,
                                        [("fix-#7", repo.fix), ("clean-x", repo.clean)], (), _Lock())
    assert authors == {}
    assert _messages(calls) == ["Merge branch 'clean-x'"]


def _drive_merge_attempt(monkeypatch, repo: Repo, tmp_path: Path, branch: str, targets: dict,
                         waiters=()) -> tuple[list[tuple], Path]:
    """Run merge._merge_attempt with the lock, the origin sync and the claimed line stubbed; it stops before the
    suite (the sentinel from `wait_while_held`). Returns the recorded git calls and the merge log path."""
    lock = _Lock(waiters)
    monkeypatch.setattr(merge, "_make_line_check_hook", lambda *a, **k: None)
    monkeypatch.setattr(merge, "acquire_merge_line", lambda *a, **k: (lock, None))
    monkeypatch.setattr(merge, "_checkout_locked", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(merge, "_sync_up", lambda *a, **k: (True, "", ""))
    monkeypatch.setattr(merge, "_resolve_target", lambda cwd, name, remote: (targets[name], "", "", ""))
    monkeypatch.setattr(merge, "_core_branch_refusal", lambda *a, **k: "")

    def stop(*args, **kwargs):
        raise _Stop()

    monkeypatch.setattr(merge, "wait_while_held", stop)
    calls = _record(monkeypatch, merge, "_git", lambda a, k: a[1:])
    log = tmp_path / "merge.log"
    ticket_info = {"suite": "python -m pytest -q", "remote": "origin", "main": "main", "cwd": str(repo.root)}
    return calls, log, lambda: merge._merge_attempt(
        branch, "python -m pytest -q", cwd=repo.root, lock_path=tmp_path / "lock", remote="origin", main="main",
        lock_timeout=None, max_rebase_attempts=1, suite_env=None, suite_output_dir=tmp_path / "suites",
        merge_log_path=log, suite_shared=(), issues=(), timing=None, ticket_info=ticket_info, state={})


def test_real_merge_refuses_closing_branch_before_its_commit(repo: Repo, monkeypatch, tmp_path: Path):
    calls, _log, attempt = _drive_merge_attempt(monkeypatch, repo, tmp_path, "fix-#7", {"fix-#7": repo.fix})
    result = attempt()
    assert result.ok is False and result.reason == "merge-refused"
    assert "closing-check" in result.message and "#7" in result.message
    assert _messages(calls) == []


def test_waiter_merge_refusal_is_logged_with_its_text_and_clean_real_merge_commits(repo: Repo, monkeypatch,
                                                                                   tmp_path: Path):
    waiter = Waiter(ticket="w1", pid=0, info={"suite": "python -m pytest -q", "remote": "origin", "main": "main",
                                              "cwd": str(repo.root), "branch": "fix-#7"})
    calls, log, attempt = _drive_merge_attempt(monkeypatch, repo, tmp_path, "clean-x",
                                               {"clean-x": repo.clean, "fix-#7": repo.fix}, waiters=[waiter])
    with pytest.raises(_Stop):  # the clean real merge commits, the waiter refuses, then the stop before the suite
        attempt()
    assert _messages(calls) == ["Merge branch 'clean-x'"]
    line = log.read_text(encoding="utf-8").splitlines()
    assert len(line) == 1 and "fix-#7 | left in line: merge-refused: closing-check" in line[0]
    assert "\"fix\" before \"#7\"" in line[0]


# -- line_check.py: the check's preparation --------------------------------------------------------

def _line_check(repo: Repo, tmp_path: Path, branch: str, target: str) -> line_check.LineCheck:
    return line_check.LineCheck(repo.root, tmp_path / "lock", "t1", branch, target, "python -m pytest -q")


def test_line_check_refuses_closing_branch_before_its_commit(repo: Repo, monkeypatch, tmp_path: Path):
    calls = _record(monkeypatch, merge, "_git", lambda a, k: a[1:])
    prep = line_check._Prepared()
    _line_check(repo, tmp_path, "fix-#7", repo.fix)._prepare_into(prep)
    assert "closing-check" in prep.skip_reason and "#7" in prep.skip_reason
    assert _messages(calls) == []


def test_line_check_clean_branch_reaches_its_commit(repo: Repo, monkeypatch, tmp_path: Path):
    calls = _record(monkeypatch, merge, "_git", lambda a, k: a[1:])

    def stop(*args, **kwargs):
        raise _Stop()

    monkeypatch.setattr(line_check, "ensure_suite_worktree", stop)
    with pytest.raises(_Stop):
        _line_check(repo, tmp_path, "clean-x", repo.clean)._prepare_into(line_check._Prepared())
    assert _messages(calls) == ["Check clean-x"]


# -- port.py: commit_written -> _commit_paths ------------------------------------------------------

def _port_target(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    _init(target)
    (target / "README.md").write_text("hello\n", encoding="utf-8")
    _git(target, "add", "README.md")
    _git(target, "commit", "-q", "-m", "initial")
    (target / "core.txt").write_text("core\n", encoding="utf-8")
    return target


def test_port_commit_refuses_closing_message_before_its_commit(tmp_path: Path, monkeypatch):
    target = _port_target(tmp_path)
    calls = _record(monkeypatch, port, "_git_call", lambda a, k: a[1])
    with pytest.raises(PortError, match="closing-check"):
        port.commit_written(target, ["core.txt"], "fixes #7")
    assert _messages(calls) == []


def test_port_clean_message_commits(tmp_path: Path, monkeypatch):
    target = _port_target(tmp_path)
    calls = _record(monkeypatch, port, "_git_call", lambda a, k: a[1])
    message = "orchestration core ported from home at abc123"
    result = port.commit_written(target, ["core.txt"], message)
    assert result.sha and result.committed == ["core.txt"]
    assert _messages(calls) == [message]


# -- core_update.py: _commit_and_push --------------------------------------------------------------

def test_core_update_refuses_closing_message_and_undoes_its_paths(repo: Repo, monkeypatch):
    (repo.root / "core.txt").write_text("core\n", encoding="utf-8")
    calls = _record(monkeypatch, core_update, "_git", lambda a, k: a[1:])
    # a head sha never holds a keyword; `fixes #7` is the crafted head that puts one in `Core update: <sha8>`
    with pytest.raises(CoreUpdateError, match="closing-check"):
        core_update._commit_and_push(repo.root, {"core.txt"}, "fixes #7", {"core.txt": None})
    assert _messages(calls) == []
    assert not (repo.root / "core.txt").exists()  # the update created it: the undo removes it again


def test_core_update_clean_head_commits_and_pushes(repo: Repo, monkeypatch, tmp_path: Path):
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git(repo.root, "remote", "add", "origin", str(remote))
    _git(repo.root, "push", "-q", "origin", "main")
    (repo.root / "core.txt").write_text("core\n", encoding="utf-8")
    calls = _record(monkeypatch, core_update, "_git", lambda a, k: a[1:])
    core_update._commit_and_push(repo.root, {"core.txt"}, "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432",
                                 {"core.txt": None})
    assert _messages(calls) == ["Core update: 9f8e7d6c"]


# -- worktree.py: commit_wip -----------------------------------------------------------------------

def test_commit_wip_refuses_closing_slug_and_makes_no_commit(repo: Repo, monkeypatch):
    (repo.root / "work.txt").write_text("unsaved\n", encoding="utf-8")
    calls = _record(monkeypatch, worktree, "git", lambda a, k: a)
    committed, detail = worktree.commit_wip(repo.root, "closes #7", "finished", elapsed_seconds=10.0)
    assert committed is False
    assert "closing-check" in detail and "#7" in detail
    assert _messages(calls) == []


def test_commit_wip_clean_slug_commits(repo: Repo, monkeypatch):
    (repo.root / "work.txt").write_text("unsaved\n", encoding="utf-8")
    calls = _record(monkeypatch, worktree, "git", lambda a, k: a)
    committed, _detail = worktree.commit_wip(repo.root, "clean-slug", "finished", elapsed_seconds=10.0)
    assert committed is True
    messages = _messages(calls)
    assert len(messages) == 1 and messages[0].startswith("WIP (agentops): clean-slug ended by finished")


# -- test_copy_send.py: _send ----------------------------------------------------------------------

def _send_remote(tmp_path: Path) -> str:
    remote = tmp_path / "copy.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    return str(remote)


def _pinned_patterns(monkeypatch) -> None:
    # the machine file decides which kinds are not checked; the test pins it to none
    monkeypatch.setattr(test_copy_send, "load_patterns", lambda root=None: Patterns(dict(BUILTIN), ()))


def test_test_copy_send_refuses_closing_branch_before_its_commit(repo: Repo, monkeypatch, tmp_path: Path):
    _pinned_patterns(monkeypatch)
    calls = _record(monkeypatch, test_copy_send, "_git", lambda a, k: a[1:])
    result = test_copy_send._send(repo.root, "fix-#7", _send_remote(tmp_path), None)
    assert result.sent is False and result.sha is None
    assert "closing-check" in result.reason and "#7" in result.reason
    assert _messages(calls) == []


def test_test_copy_send_clean_branch_commits_and_pushes(repo: Repo, monkeypatch, tmp_path: Path):
    _pinned_patterns(monkeypatch)
    calls = _record(monkeypatch, test_copy_send, "_git", lambda a, k: a[1:])
    result = test_copy_send._send(repo.root, "clean-x", _send_remote(tmp_path), None)
    assert result.sent is True and result.sha
    assert _messages(calls) == ["test copy clean-x"]
