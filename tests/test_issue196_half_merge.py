"""#196 change 3: a session the dispatcher ends while the main checkout holds a half merge (MERGE_HEAD) or a half
rebase (a rebase dir) tells the area's brain chat ONE line and comments the same line on each held issue. It
aborts nothing. Through `Dispatcher` with its fakes and a real tmp git repo as the dispatcher's folder."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.test_agentops_dispatch import FakeClaude, FakeGh, FakeClock, _issue
from tools.agentops.dispatch import Dispatcher

TAIL = "check before a core update"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True,
                   capture_output=True, text=True)


def _repo_with_half_merge(root: Path) -> Path:
    """A real repo whose `git merge --no-commit --no-ff` left a MERGE_HEAD."""
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "a")
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "b")
    _git(repo, "checkout", "-q", "main")
    (repo / "c.txt").write_text("c\n", encoding="utf-8")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-q", "-m", "c")
    _git(repo, "merge", "--no-commit", "--no-ff", "side")
    assert (repo / ".git" / "MERGE_HEAD").is_file()
    return repo


def _plain_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "a")
    return repo


def _holding(tmp_path: Path, repo: Path, issues=(70, 71)):
    """A dispatcher whose folder is `repo`, a live session holding `issues`, and a live brain chat."""
    claude = FakeClaude(cwd=repo)
    gh = FakeGh(candidates=[_issue(n, n) for n in issues])
    d = Dispatcher(claude=claude, gh=gh, max_hours=4.0, events_file=tmp_path / "events.jsonl",
                   orch_offer_max=99, state_file=tmp_path / "orch_session.json",
                   projects_dir=tmp_path / "projects", clock=FakeClock())
    d.tick()
    for n in issues:
        gh.claim(n)
    d.tick()
    cur = d.current
    assert cur is not None and cur.held == set(issues)
    title = d._targets_of(issues)[0]
    claude.add_session("brain-chat", name=title, kind="interactive", status="idle")
    return claude, gh, d, cur, title


def _line(cur, what: str, held: str = "#70, #71") -> str:
    return f"ORCH {cur.session[:8]} ended with a half {what} in the main checkout; held issues {held}; {TAIL}"


@pytest.fixture
def spy(monkeypatch):
    """Every subprocess.run call's argv, so a `--abort` anywhere is seen."""
    calls: list[list[str]] = []
    real = subprocess.run

    def run(args, *a, **kw):
        calls.append([str(x) for x in args] if isinstance(args, (list, tuple)) else [str(args)])
        return real(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def _told(claude: FakeClaude) -> list[tuple[str, str]]:
    return [(t, m) for t, m in claude.waked if m.startswith("ORCH ")]


def _commented(gh: FakeGh) -> list[tuple[int, str]]:
    return [(n, b) for n, b in gh.comments if b.startswith("ORCH ") and "half" in b]


def test_finish_with_merge_head_one_line_to_brain_one_comment_per_held_issue_nothing_aborted(tmp_path, spy):
    repo = _repo_with_half_merge(tmp_path)
    claude, gh, d, cur, title = _holding(tmp_path, repo)
    gh.close(70)
    gh.park(71)

    d._finish(cur, "stopped")

    line = _line(cur, "merge")
    assert _told(claude) == [(title, line)]
    assert _commented(gh) == [(70, line), (71, line)]
    assert (repo / ".git" / "MERGE_HEAD").is_file()
    assert not any("--abort" in call for call in spy)


def test_finish_with_a_rebase_dir_says_half_rebase(tmp_path, spy):
    repo = _plain_repo(tmp_path)
    (repo / ".git" / "rebase-merge").mkdir()
    claude, gh, d, cur, title = _holding(tmp_path, repo, issues=(70,))
    gh.close(70)

    d._finish(cur, "done")

    line = _line(cur, "rebase", "#70")
    assert _told(claude) == [(title, line)]
    assert _commented(gh) == [(70, line)]
    assert (repo / ".git" / "rebase-merge").is_dir()
    assert not any("--abort" in call for call in spy)


def test_finish_with_a_rebase_apply_dir_says_half_rebase(tmp_path, spy):
    repo = _plain_repo(tmp_path)
    (repo / ".git" / "rebase-apply").mkdir()
    claude, gh, d, cur, title = _holding(tmp_path, repo, issues=(70,))
    gh.close(70)

    d._finish(cur, "done")

    assert _told(claude) == [(title, _line(cur, "rebase", "#70"))]
    assert (repo / ".git" / "rebase-apply").is_dir()
    assert not any("--abort" in call for call in spy)


def test_finish_without_merge_head_or_rebase_no_line_no_comment(tmp_path):
    repo = _plain_repo(tmp_path)
    claude, gh, d, cur, title = _holding(tmp_path, repo)
    gh.close(70)
    gh.close(71)

    d._finish(cur, "done")

    assert _told(claude) == []
    assert _commented(gh) == []


def test_timeout_with_merge_head_the_same_line_and_comments(tmp_path, spy):
    repo = _repo_with_half_merge(tmp_path)
    claude, gh, d, cur, title = _holding(tmp_path, repo)

    d._timeout(cur)

    line = _line(cur, "merge")
    assert _told(claude) == [(title, line)]
    assert _commented(gh) == [(70, line), (71, line)]
    assert (repo / ".git" / "MERGE_HEAD").is_file()
    assert not any("--abort" in call for call in spy)


def test_timeout_without_a_half_merge_no_line(tmp_path):
    repo = _plain_repo(tmp_path)
    claude, gh, d, cur, title = _holding(tmp_path, repo)

    d._timeout(cur)

    assert _told(claude) == []
    assert _commented(gh) == []


def test_unreadable_checkout_means_no_line_and_finish_does_not_raise(tmp_path):
    repo = _plain_repo(tmp_path)
    claude, gh, d, cur, title = _holding(tmp_path, repo)
    gh.close(70)
    gh.close(71)
    claude.cwd = tmp_path / "does-not-exist"

    d._finish(cur, "done")

    assert _told(claude) == []
    assert _commented(gh) == []


def test_a_failed_comment_on_the_first_issue_does_not_skip_the_rest(tmp_path):
    repo = _repo_with_half_merge(tmp_path)
    claude, gh, d, cur, title = _holding(tmp_path, repo)
    gh.close(70)
    gh.close(71)
    real = gh.comment

    def comment(number, body):
        if number == 70 and body.startswith("ORCH ") and "half" in body:
            raise RuntimeError("gh down")
        return real(number, body)

    gh.comment = comment

    d._finish(cur, "done")

    line = _line(cur, "merge")
    assert _told(claude) == [(title, line)]
    assert _commented(gh) == [(71, line)]
