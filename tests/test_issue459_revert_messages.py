"""#459 unit 1 (revert-sites): a revert the layer makes is committed with the layer's own message, never git's copy of
the old subject. The message is `Revert <sha12>: <old subject>` when the old subject holds no closing keyword, else
`Revert <sha12>` alone, with the body line `This reverts commit <sha40>.` that `_revert_history` reads; the message
passes #454's built-message check before the commit, so a closing keyword never reaches main through a revert.

Scratch clones only: a bare local ``origin`` and a clone that holds the repo's pre-push hook (never the real origin, never
the real ``gh``). The helpers ``git``, ``commit_file``, ``land`` and ``sel`` are test_safety_revert's."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_safety_revert import commit_file, git, land, sel
from tools.agentops import closing_keywords, githooks, provisional as prov, safety_revert, test_select
from tools.agentops.merge import safety_red_path

REAL_ROOT = Path(__file__).resolve().parents[1]
REAL_HOOK = REAL_ROOT / "tools" / "githooks" / "pre-push"
RED_ID = "tests/test_b.py::test_b"


def _sha_of(cwd, ref="HEAD") -> str:
    return git(cwd, "rev-parse", ref)


def _remote_main(origin: Path) -> str:
    return git(origin, "rev-parse", "refs/heads/main")


def _message(repo: Path, ref: str) -> str:
    return git(repo, "log", "-1", "--format=%B", ref)


def _subject(repo: Path, ref: str) -> str:
    return git(repo, "log", "-1", "--format=%s", ref)


def _setup_clone(tmp_path: Path, monkeypatch, commits) -> tuple[Path, Path]:
    """A bare origin and a clone on main. ``commits(repo)`` makes the history and returns; main is pushed
    before the pre-push hook is installed, so the history is on origin as it is in a real repo, and the hook
    checks only what the test pushes next."""
    for k, v in (("NAME", "t"), ("EMAIL", "t@t")):
        monkeypatch.setenv(f"GIT_AUTHOR_{k}", v)
        monkeypatch.setenv(f"GIT_COMMITTER_{k}", v)
    monkeypatch.setenv("PYTHONPATH", str(REAL_ROOT))  # the hook runs `python -m tools.agentops closing-check`
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    repo = tmp_path / "root"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "remote", "add", "origin", str(origin))
    commits(repo)
    git(repo, "push", "-q", "origin", "main")
    hooks = repo / "tools" / "githooks"
    hooks.mkdir(parents=True)
    shutil.copyfile(REAL_HOOK, hooks / "pre-push")
    assert not githooks.check(repo)
    githooks.install(repo)
    assert githooks.check(repo)
    return repo, origin


@pytest.fixture
def landing_clone(tmp_path, monkeypatch):
    """#459 row 1: `State: test closed #425` is on main, then a landing (a merge) that breaks one test."""
    state = {}

    def commits(repo: Path) -> None:
        commit_file(repo, "a.txt", "a0\n", "base a")
        commit_file(repo, "b.txt", "b0\n", "base b")
        base = _sha_of(repo)
        (repo / "docs").mkdir()
        commit_file(repo, "docs/STATE.md", "state\n", "State: test closed #425")
        state["sha"] = _sha_of(repo)
        landing_sha = land(repo, "side-b", "b.txt", "b1\n")
        state.update(base=base, landing=landing_sha)

    repo, origin = _setup_clone(tmp_path, monkeypatch, commits)
    mdir = test_select.map_dir(repo)
    mdir.mkdir(parents=True, exist_ok=True)
    e = SimpleNamespace(repo=repo, origin=origin, mdir=mdir, log=tmp_path / "merge.log", state=state["sha"],
                        base=state["base"], landing=state["landing"])
    monkeypatch.setattr(test_select, "select_for_diff",
                        lambda root, base, head, mdir: sel([RED_ID]) if head == e.landing else sel([]))
    monkeypatch.setattr(safety_revert, "_spawn", lambda root, sha: "")
    monkeypatch.setattr(safety_revert, "_still_red", lambda root, ids, runner=None: list(ids))
    return e


@pytest.fixture
def plain_clone(tmp_path, monkeypatch):
    """#459 rows 2 and 4: a clone whose history is on origin, the hook installed; the tests make the commit."""
    def commits(repo: Path) -> None:
        commit_file(repo, "a.txt", "a0\n", "base a")

    repo, origin = _setup_clone(tmp_path, monkeypatch, commits)
    return repo, origin


def test_row1_safety_revert_of_a_landing_is_pushed_with_the_layer_message(landing_clone):
    e = landing_clone
    marker = safety_red_path(e.repo)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"sha": e.landing, "ids": [RED_ID], "map_commit": e.base}), encoding="utf-8")

    outcome = safety_revert.resolve_safety_red(e.repo, e.mdir, e.log)

    assert outcome.startswith("safety run: red"), outcome
    assert "| reverted " in outcome, outcome
    new_head = _remote_main(e.origin)
    assert new_head == _sha_of(e.repo)  # local main is what the push sent
    subject = _subject(e.origin, "main")
    assert subject == f"Revert {e.landing[:12]}: Merge branch 'side-b'", subject
    assert f"This reverts commit {e.landing}." in _message(e.origin, "main")
    assert safety_revert.already_reverted(e.repo, e.landing) is True


def test_row2_safety_revert_of_the_closing_commit_has_no_old_subject(landing_clone):
    e = landing_clone
    new_head = safety_revert._revert_and_push(e.repo, e.state, False)

    assert new_head == _remote_main(e.origin)
    subject = _subject(e.origin, "main")
    assert subject == f"Revert {e.state[:12]}", subject
    message = _message(e.origin, "main")
    assert f"This reverts commit {e.state}." in message
    assert closing_keywords.find(message) == []


def test_row3_veto_reverts_the_old_form_fix_commit_and_pushes_it(tmp_path, monkeypatch):
    """#459 row 3: a `Fix #150: x` on main (the old form) with a `Provisional: 7` trailer; the veto reverts it,
    pushes the revert, and the closing keyword never reaches main."""
    fix = {}

    def commits(repo: Path) -> None:
        commit_file(repo, "a.txt", "a0\n", "base a")
        (repo / "x.txt").write_text("x\n", encoding="utf-8")
        git(repo, "add", "x.txt")
        git(repo, "commit", "-q", "-m", "Fix #150: x", "-m", "Provisional: 7")
        fix["sha"] = _sha_of(repo)

    repo, origin = _setup_clone(tmp_path, monkeypatch, commits)
    gh = RecordingGh()

    result = prov.veto(7, gh=gh, repo=repo, test_cmd='python -c "1"')

    assert result.ok, result.reason
    assert [c.sha for c in result.reverted] == [fix["sha"]]
    assert _subject(origin, "main") == f"Revert {fix['sha'][:12]}"
    message = _message(origin, "main")
    assert f"This reverts commit {fix['sha']}." in message
    assert closing_keywords.find(message) == []
    assert _sha_of(repo, "main") == _remote_main(origin)  # local main = remote main


def test_row4_revert_of_a_subject_with_no_keyword_keeps_the_subject(plain_clone):
    repo, origin = plain_clone
    (repo / "b.txt").write_text("b1\n", encoding="utf-8")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-q", "-m", "unit work")
    work = _sha_of(repo)
    git(repo, "push", "-q", "origin", "main")  # on origin already: the revert's push is the only one checked

    new_head = safety_revert._revert_and_push(repo, work, False)

    assert new_head == _remote_main(origin)
    subject = _subject(origin, "main")
    assert subject == f"Revert {work[:12]}: unit work", subject
    assert f"This reverts commit {work}." in _message(origin, "main")


def test_a_hit_at_the_helper_refuses_and_leaves_the_checkout_as_it_was(plain_clone, monkeypatch):
    """The helper refuses a message with a closing keyword: no commit, nothing pushed, the revert undone."""
    repo, origin = plain_clone
    (repo / "b.txt").write_text("b1\n", encoding="utf-8")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-q", "-m", "unit work")
    work = _sha_of(repo)
    git(repo, "push", "-q", "origin", "main")
    before = _sha_of(repo)
    monkeypatch.setattr(safety_revert, "revert_message",
                        lambda sha, old: f"Revert {sha[:12]}\n\nfixes #9\n\nThis reverts commit {sha}.\n")

    with pytest.raises(RuntimeError, match="closing"):
        safety_revert._revert_and_push(repo, work, False)

    assert _sha_of(repo) == before
    assert git(repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert _remote_main(origin) == before


def _commit_unit_work(repo: Path) -> str:
    (repo / "b.txt").write_text("b1\n", encoding="utf-8")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-q", "-m", "unit work")
    return _sha_of(repo)


def _fix_commits(repo: Path) -> None:
    """Row 3's history: a `Fix #150: x` commit with a `Provisional: 7` trailer, on the base commit."""
    commit_file(repo, "a.txt", "a0\n", "base a")
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    git(repo, "add", "x.txt")
    git(repo, "commit", "-q", "-m", "Fix #150: x", "-m", "Provisional: 7")


def test_a_timeout_on_the_commit_takes_the_revert_back(plain_clone, monkeypatch):
    """#459 send-back 1: a timeout on the commit step leaves no staged revert; the checkout is taken back and the
    timeout still reaches the caller."""
    repo, origin = plain_clone
    work = _commit_unit_work(repo)
    git(repo, "push", "-q", "origin", "main")
    before = _sha_of(repo)
    real_git = safety_revert._git

    def git_timing_out_on_commit(cwd, *args, env=None):
        if args[:1] == ("commit",):
            raise safety_revert._Timeout("timeout")
        return real_git(cwd, *args, env=env)

    monkeypatch.setattr(safety_revert, "_git", git_timing_out_on_commit)

    with pytest.raises(safety_revert._Timeout):
        safety_revert._revert_and_push(repo, work, False)

    assert _sha_of(repo) == before
    assert git(repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert _remote_main(origin) == before


def test_a_failing_take_back_is_named_in_the_refusal(plain_clone, monkeypatch):
    """#459 send-back 2: a refusal whose take-back failed says so; it never reports a restored checkout it did not get."""
    repo, origin = plain_clone
    work = _commit_unit_work(repo)
    git(repo, "push", "-q", "origin", "main")
    monkeypatch.setattr(safety_revert, "revert_message",
                        lambda sha, old: f"Revert {sha[:12]}\n\nfixes #9\n\nThis reverts commit {sha}.\n")
    real_git = safety_revert._git

    def git_failing_hard_reset(cwd, *args, env=None):
        if args[:2] == ("reset", "--hard"):
            return subprocess.CompletedProcess(["git", *args], 1, "", "fatal: cannot lock ref\n")
        return real_git(cwd, *args, env=env)

    monkeypatch.setattr(safety_revert, "_git", git_failing_hard_reset)

    with pytest.raises(RuntimeError) as caught:
        safety_revert._revert_and_push(repo, work, False)

    assert str(caught.value).startswith("refused: "), str(caught.value)
    assert str(caught.value).endswith("; take-back failed: fatal: cannot lock ref"), str(caught.value)


def test_a_failing_take_back_of_the_veto_is_named_in_the_reason(tmp_path, monkeypatch):
    """#459 send-back 2, the veto's own take-back: a conflicting revert whose reset fails names the failed take-back."""
    repo, _origin = _setup_clone(tmp_path, monkeypatch, _fix_commits)
    real_run = prov._run_git

    def run_git(args, *, cwd, timeout=None, env=None):
        if args[:2] == ["revert", "--no-commit"]:
            return subprocess.CompletedProcess(["git", *args], 1, "", "CONFLICT (content): x.txt\n")
        if args[:1] == ["reset"]:
            return subprocess.CompletedProcess(["git", *args], 1, "", "fatal: boom\n")
        return real_run(args, cwd=cwd, timeout=timeout, env=env)

    monkeypatch.setattr(prov, "_run_git", run_git)

    result = prov.veto(7, gh=RecordingGh(), repo=repo, test_cmd='python -c "1"')

    assert not result.ok
    assert result.reason.startswith("revert conflict on "), result.reason
    assert result.reason.endswith("; take-back failed: fatal: boom"), result.reason


def test_a_veto_with_no_before_commit_fails_before_any_revert(tmp_path, monkeypatch):
    """#459 send-back 3: an empty `rev-parse HEAD` of the veto worktree fails the veto before any revert or reset runs;
    no reset of an empty revision, no gh write, the throwaway branch removed."""
    repo, _origin = _setup_clone(tmp_path, monkeypatch, _fix_commits)
    real_run = prov._run_git
    calls: list[list[str]] = []

    def run_git(args, *, cwd, timeout=None, env=None):
        calls.append(list(args))
        if args == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(["git", *args], 0, "", "")
        return real_run(args, cwd=cwd, timeout=timeout, env=env)

    monkeypatch.setattr(prov, "_run_git", run_git)
    gh = RecordingGh()

    result = prov.veto(7, gh=gh, repo=repo, test_cmd='python -c "1"')

    assert not result.ok
    assert "rev-parse HEAD" in result.reason, result.reason
    assert not [c for c in calls if c[:1] in (["revert"], ["reset"])], calls
    assert gh.comments == [] and gh.closed == []
    assert git(repo, "branch", "--list", "veto-7") == ""


class RecordingGh:
    """A fake `gh` for `provisional.veto`: no provisional issues, every write recorded, nothing sent anywhere."""

    repo = "Owner/Home"

    def __init__(self) -> None:
        self.comments: list[tuple[int, str]] = []
        self.closed: list[int] = []
        self.removed: list[tuple[int, str]] = []

    def list_provisional_issues(self) -> list:
        return []

    def search_issues_mentioning(self, text: str) -> list:
        return []

    def comment(self, number: int, body: str, repo=None) -> bool:
        self.comments.append((number, body))
        return True

    def close(self, number: int, reason: str = "not planned", repo=None) -> bool:
        self.closed.append(number)
        return True

    def remove_label(self, number: int, label: str) -> bool:
        self.removed.append((number, label))
        return True
