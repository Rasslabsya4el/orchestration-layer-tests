"""Tests for tools/agentops/provisional.py (#113): the `provisional` label, the `Provisional: #N`
commit trailer / issue-body line, and a one-command veto that reverts everything built on a
decision. Git side on a throwaway repo (with a bare `origin`) built fresh per test; GitHub side
through a fake `GhCli`, never the real `gh` -- same pattern `tests/test_agentops_dispatch.py`
already uses for `dispatch.py`'s `GhCli`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from timing_bound import bound
from tools.agentops import provisional as prov

ISSUE_A = 201
ISSUE_B = 202
ISSUE_UNRELATED = 999  # never used as a trailer; just documents "no relation"
HOME_REPO = "Owner/Home"  # the repo the tool is bound to in these tests


def _child_in(repo: str, number: int, parent: int = ISSUE_A, home: str = HOME_REPO) -> tuple[str, int, str]:
    """A cross-repo child decision: an issue in `repo` whose body names `home`#parent as its parent."""
    return (repo, number, f"Built on {home}#{parent}.\n\nProvisional: {home}#{parent}\n")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def _sha(cwd: Path, ref: str = "HEAD") -> str:
    return _git(cwd, "rev-parse", ref).stdout.strip()


def _commit(cwd: Path, message: str) -> str:
    _git(cwd, "commit", "-q", "-m", message)
    return _sha(cwd)


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")


class FakeGh:
    """A fake `gh`: an in-memory table of open provisional issues plus every call `veto` /
    `approve` / `provisional_report` can make."""

    def __init__(self, issues: list[prov.ProvisionalIssue], *, remote_issues: list[tuple[str, int, str]] = ()) -> None:
        self.repo = HOME_REPO
        self._issues = {i.number: i for i in issues}
        self.closed: set[int] = set()
        self.close_reasons: dict[int, str] = {}
        self.labels: dict[int, set[str]] = {n: set() for n in self._issues}
        self.comments: list[tuple[int, str]] = []
        self.removed_labels: list[tuple[int, str]] = []
        # issues in other repos: (repo, number, body); the search returns those whose body holds the text
        self.remote_issues = list(remote_issues)
        self.searches: list[str] = []
        # writes addressed to another repo: (repo, number, ...)
        self.remote_comments: list[tuple[str, int, str]] = []
        self.remote_closed: list[tuple[str, int, str]] = []

    def list_provisional_issues(self) -> list[prov.ProvisionalIssue]:
        return [i for n, i in self._issues.items() if n not in self.closed]

    def search_issues_mentioning(self, text: str) -> list[tuple[str, int, str]]:
        self.searches.append(text)
        return [(repo, n, body) for repo, n, body in self.remote_issues if text in body]

    def comment(self, number: int, body: str, repo: str | None = None) -> bool:
        if repo is None or repo == self.repo:
            self.comments.append((number, body))
        else:
            self.remote_comments.append((repo, number, body))
        return True

    def close(self, number: int, reason: str = "not planned", repo: str | None = None) -> bool:
        if repo is None or repo == self.repo:
            self.closed.add(number)
            self.close_reasons[number] = reason
        else:
            self.remote_closed.append((repo, number, reason))
        return True

    def add_label(self, number: int, label: str) -> bool:
        self.labels.setdefault(number, set()).add(label)
        return True

    def remove_label(self, number: int, label: str) -> bool:
        self.labels.setdefault(number, set()).discard(label)
        if label in self.labels.get(number, set()):
            self.labels[number].discard(label)
        self.labels.setdefault(number, set())
        return True


def _make_repo(tmp_path: Path, *, conflicting_last_edit: bool) -> tuple[Path, Path, dict[str, str]]:
    """7 commits: 3 with `Provisional: #{ISSUE_A}`, 1 with `Provisional: #{ISSUE_B}` (issue B's
    body names A as parent), 3 unrelated -- the last of which edits `a.txt` too, on a different
    line unless `conflicting_last_edit` (then it rewrites the exact line commit `a3` added, so
    reverting `a3` conflicts). Returns `(repo, origin, shas)`."""

    repo = tmp_path / "repo"
    origin = tmp_path / "origin.git"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "a@example.com")
    _git(repo, "config", "user.name", "a")

    lines = [f"l{i}" for i in range(1, 11)]
    _write(repo / "a.txt", "\n".join(lines) + "\n")
    _write(repo / "c.txt", "c-base\n")
    _git(repo, "add", "a.txt", "c.txt")
    shas = {"init": _commit(repo, "init")}

    def prepend_a(tag: str) -> None:
        current = (repo / "a.txt").read_text(encoding="utf-8")
        _write(repo / "a.txt", f"{tag}\n{current}")

    prepend_a("a1")
    _git(repo, "add", "a.txt")
    shas["A1"] = _commit(repo, f"add a1\n\nProvisional: #{ISSUE_A}")

    prepend_a("a2")
    _git(repo, "add", "a.txt")
    shas["A2"] = _commit(repo, f"add a2\n\nProvisional: #{ISSUE_A}")

    prepend_a("a3")
    _git(repo, "add", "a.txt")
    shas["A3"] = _commit(repo, f"add a3\n\nProvisional: #{ISSUE_A}")

    _write(repo / "b.txt", "b1\n")
    _git(repo, "add", "b.txt")
    shas["B1"] = _commit(repo, f"add b.txt\n\nProvisional: #{ISSUE_B}")

    current_c = (repo / "c.txt").read_text(encoding="utf-8")
    _write(repo / "c.txt", current_c + "u1\n")
    _git(repo, "add", "c.txt")
    shas["U1"] = _commit(repo, "unrelated: extend c.txt")

    current_a = (repo / "a.txt").read_text(encoding="utf-8")
    if conflicting_last_edit:
        current_a = current_a.replace("a3\n", "a3-changed\n")
    else:
        current_a = current_a.replace("l10", "l10-changed")
    _write(repo / "a.txt", current_a)
    _git(repo, "add", "a.txt")
    shas["U2"] = _commit(repo, "unrelated: edit a.txt")

    _git(repo, "init", "-q", "--bare", str(origin))
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "main")

    return repo, origin, shas


def _issue_a_b() -> list[prov.ProvisionalIssue]:
    return [
        prov.ProvisionalIssue(number=ISSUE_A, title="Autopilot picks the fallback route", body=""),
        prov.ProvisionalIssue(number=ISSUE_B, title="Autopilot tunes the fallback's budget",
                              body=f"Built on this.\n\nProvisional: #{ISSUE_A}\n"),
    ]


# --------------------------------------------------------------------------------- veto A -------


def test_veto_a_reverts_four_commits_keeps_the_three_unrelated(tmp_path: Path, monkeypatch):
    # #827: the 60 s per-git-call bound (provisional.GIT_TIMEOUT) timed out `worktree remove` on a loaded machine.
    monkeypatch.setattr(prov, "GIT_TIMEOUT", bound(60.0, 400.0))
    repo, origin, shas = _make_repo(tmp_path, conflicting_last_edit=False)
    gh = FakeGh(_issue_a_b())

    result = prov.veto(ISSUE_A, gh=gh, repo=repo, test_cmd="python -c \"1\"")

    assert result.ok, result.reason
    assert len(result.reverted) == 4
    assert {c.sha for c in result.reverted} == {shas["A1"], shas["A2"], shas["A3"], shas["B1"]}

    # the final tree equals one built from the 3 unrelated commits alone (init, U1, U2).
    _git(repo, "branch", "unrelated-only", shas["init"])
    _git(repo, "checkout", "-q", "unrelated-only")
    _git(repo, "cherry-pick", shas["U1"], shas["U2"])
    _git(repo, "checkout", "-q", "main")
    diff = _git(repo, "diff", "main", "unrelated-only").stdout
    assert diff == ""

    assert ISSUE_A in gh.closed
    assert ISSUE_B in gh.closed
    assert gh.close_reasons[ISSUE_A] == "not planned"
    assert gh.close_reasons[ISSUE_B] == "not planned"
    assert (ISSUE_B, f"reverted by the veto of #{ISSUE_A}") in gh.comments
    assert (ISSUE_A, f"veto of #{ISSUE_A}: reverted 4 commit(s)") in gh.comments

    origin_main = _git(origin, "rev-parse", "main").stdout.strip()
    assert origin_main == _sha(repo, "main")


def test_veto_b_reverts_only_its_own_commit_a_untouched(tmp_path: Path):
    repo, origin, shas = _make_repo(tmp_path, conflicting_last_edit=False)
    gh = FakeGh(_issue_a_b())

    result = prov.veto(ISSUE_B, gh=gh, repo=repo, test_cmd="python -c \"1\"")

    assert result.ok, result.reason
    assert len(result.reverted) == 1
    assert result.reverted[0].sha == shas["B1"]

    assert ISSUE_B in gh.closed
    assert ISSUE_A not in gh.closed

    # A's content is still present (a1/a2/a3 all still in a.txt).
    content = (repo / "a.txt").read_text(encoding="utf-8")
    assert "a1" in content and "a2" in content and "a3" in content


def test_veto_a_conflict_leaves_main_untouched_and_names_the_commit(tmp_path: Path):
    repo, origin, shas = _make_repo(tmp_path, conflicting_last_edit=True)
    gh = FakeGh(_issue_a_b())
    before = _sha(repo, "main")

    result = prov.veto(ISSUE_A, gh=gh, repo=repo, test_cmd="python -c \"1\"")

    assert not result.ok
    assert shas["A3"][:10] in result.reason or shas["A3"] in result.reason
    assert _sha(repo, "main") == before
    assert gh.closed == set()
    assert gh.comments == []
    assert all(not labels for labels in gh.labels.values())

    # no stray worktree left registered against the repo
    worktrees = _git(repo, "worktree", "list").stdout
    assert worktrees.count("\n") <= 1 or "veto-" not in worktrees


def test_veto_failing_test_cmd_leaves_main_untouched(tmp_path: Path):
    repo, origin, shas = _make_repo(tmp_path, conflicting_last_edit=False)
    gh = FakeGh(_issue_a_b())
    before = _sha(repo, "main")

    result = prov.veto(ISSUE_A, gh=gh, repo=repo, test_cmd="python -c \"import sys; sys.exit(1)\"")

    assert not result.ok
    assert _sha(repo, "main") == before
    assert gh.closed == set()


def test_approve_a_touches_no_commits_removes_only_its_own_label(tmp_path: Path):
    repo, origin, shas = _make_repo(tmp_path, conflicting_last_edit=False)
    gh = FakeGh(_issue_a_b())
    gh.labels[ISSUE_A].add(prov.PROVISIONAL_LABEL)
    gh.labels[ISSUE_B].add(prov.PROVISIONAL_LABEL)
    before = _sha(repo, "main")

    prov.approve(ISSUE_A, gh=gh)

    assert _sha(repo, "main") == before
    assert prov.PROVISIONAL_LABEL not in gh.labels[ISSUE_A]
    assert prov.PROVISIONAL_LABEL in gh.labels[ISSUE_B]
    assert any(number == ISSUE_A and "approved" in body for number, body in gh.comments)


def test_provisional_report_prints_a_with_child_b_and_commit_counts(tmp_path: Path):
    repo, origin, shas = _make_repo(tmp_path, conflicting_last_edit=False)
    gh = FakeGh(_issue_a_b())

    report = prov.provisional_report(gh, repo=repo)

    lines = report.splitlines()
    a_line = next(line for line in lines if line.strip().startswith(f"#{ISSUE_A}"))
    b_line = next(line for line in lines if line.strip().startswith(f"#{ISSUE_B}"))
    assert "3 commit" in a_line
    assert "1 commit" in b_line
    # B is indented under A (a child, not a root)
    assert lines.index(b_line) > lines.index(a_line)
    assert b_line.startswith(" ")


# --------------------------------------------------------------------------- unit-level helpers --


def test_parse_parents_reads_every_provisional_line():
    body = f"text\n\nProvisional: #{ISSUE_A}\nProvisional: #{ISSUE_B}\nmore text\n"
    assert prov.parse_parents(body) == (ISSUE_A, ISSUE_B)


def test_descendants_is_transitive_and_excludes_self():
    children = {ISSUE_A: [ISSUE_B], ISSUE_B: [300]}
    assert prov.descendants(ISSUE_A, children) == [ISSUE_B, 300]
    assert prov.descendants(300, children) == []


# -------------------------------------------------------------- #114: the default test gate ----


def _make_pytest_repo(tmp_path: Path, *, breaking: bool) -> tuple[Path, dict[str, str]]:
    """A tiny real Python project (not the real `main`): `mod.py`, `used.py` importing from it,
    one test that imports `used`. `A1` (`Provisional: #{ISSUE_A}`) renames `mod.helper` to
    `helper_v2`. Non-`breaking`: an unrelated commit only adds an untouched file, so reverting
    `A1` leaves everything importable. `breaking`: an unrelated commit updates `used.py` to the
    new name (so HEAD still imports cleanly) -- reverting `A1` alone then un-renames `mod.py`
    while `used.py` still imports the new name: no revert conflict (different files), but
    `python -m pytest -q` (`prov.DEFAULT_TEST_CMD`) fails to even collect."""

    repo = tmp_path / "pyrepo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "a@example.com")
    _git(repo, "config", "user.name", "a")

    _write(repo / "mod.py", "def helper():\n    return 1\n")
    _write(repo / "used.py", "from mod import helper\n\n\ndef call():\n    return helper()\n")
    _write(repo / "test_used.py", "from used import call\n\n\ndef test_call_returns_one():\n    assert call() == 1\n")
    _git(repo, "add", "mod.py", "used.py", "test_used.py")
    shas = {"init": _commit(repo, "init")}

    _write(repo / "mod.py", "def helper_v2():\n    return 1\n")
    _git(repo, "add", "mod.py")
    shas["A1"] = _commit(repo, f"rename helper to helper_v2\n\nProvisional: #{ISSUE_A}")

    if breaking:
        _write(repo / "used.py", "from mod import helper_v2 as helper\n\n\ndef call():\n    return helper()\n")
        _git(repo, "add", "used.py")
        shas["U1"] = _commit(repo, "unrelated: follow the rename in used.py")
    else:
        _write(repo / "other.py", "x = 1\n")
        _git(repo, "add", "other.py")
        shas["U1"] = _commit(repo, "unrelated: add other.py")

    return repo, shas


def test_veto_default_test_cmd_passes_the_gate_and_lands_the_revert(tmp_path: Path):
    repo, shas = _make_pytest_repo(tmp_path, breaking=False)
    gh = FakeGh([prov.ProvisionalIssue(number=ISSUE_A, title="rename helper", body="")])
    gh.labels[ISSUE_A].add(prov.PROVISIONAL_LABEL)

    result = prov.veto(ISSUE_A, gh=gh, repo=repo, test_cmd=prov.DEFAULT_TEST_CMD)

    assert result.ok, result.reason
    assert [c.sha for c in result.reverted] == [shas["A1"]]
    assert "def helper():" in (repo / "mod.py").read_text(encoding="utf-8")
    assert ISSUE_A in gh.closed
    assert gh.close_reasons[ISSUE_A] == "not planned"
    assert prov.PROVISIONAL_LABEL not in gh.labels[ISSUE_A]


def test_veto_default_test_cmd_fails_on_an_unrelated_import_break_main_unchanged(tmp_path: Path):
    repo, shas = _make_pytest_repo(tmp_path, breaking=True)
    gh = FakeGh([prov.ProvisionalIssue(number=ISSUE_A, title="rename helper", body="")])
    before = _sha(repo, "main")

    result = prov.veto(ISSUE_A, gh=gh, repo=repo, test_cmd=prov.DEFAULT_TEST_CMD)

    assert not result.ok
    assert _sha(repo, "main") == before
    assert gh.closed == set()
    # no stray worktree left registered against the repo
    worktrees = _git(repo, "worktree", "list").stdout
    assert worktrees.count("\n") <= 1 or "veto-" not in worktrees


def test_veto_refuses_when_the_checkout_is_not_on_main(tmp_path: Path):
    repo, origin, shas = _make_repo(tmp_path, conflicting_last_edit=False)
    gh = FakeGh(_issue_a_b())
    before = _sha(repo, "main")
    _git(repo, "checkout", "-b", "elsewhere")

    result = prov.veto(ISSUE_A, gh=gh, repo=repo, test_cmd="python -c \"pass\"")

    assert not result.ok
    assert "elsewhere" in result.reason
    assert _sha(repo, "main") == before
    assert gh.closed == set()


def test_veto_passes_merge_suite_env_to_its_suite_run(tmp_path: Path, monkeypatch):
    from tools.agentops import core_settings, merge

    def fake_load(path=None):
        return {"merge_suite_env": {"VETO_ENV_PROBE": "on"}}

    monkeypatch.setattr(core_settings, "load", fake_load)
    assert merge.resolve_suite_env() == {"VETO_ENV_PROBE": "on"}
    repo, origin, shas = _make_repo(tmp_path, conflicting_last_edit=False)
    gh = FakeGh(_issue_a_b())
    probe = "python -c \"import os, sys; sys.exit(0 if os.environ.get('VETO_ENV_PROBE') == 'on' else 1)\""

    result = prov.veto(ISSUE_A, gh=gh, repo=repo, test_cmd=probe)

    assert result.ok, result.reason


# ------------------------------------------------------------- cross-repo provisional children ----


@pytest.mark.parametrize(
    ("body", "same_repo", "cross_repo"),
    [
        (f"text\n\nProvisional: #{ISSUE_A}\n", (ISSUE_A,), ()),
        ("text\n\nProvisional: Owner/Repo#278\n", (), (("Owner/Repo", 278),)),
        (f"text\n\nProvisional: #{ISSUE_A}\nProvisional: Owner/Repo#278\n", (ISSUE_A,), (("Owner/Repo", 278),)),
    ],
    ids=["same-repo-only", "cross-repo-only", "both"],
)
def test_parse_parents_keeps_same_repo_and_reads_cross_repo_lines_apart(body, same_repo, cross_repo):
    assert prov.parse_parents(body) == same_repo
    assert prov.parse_cross_parents(body) == cross_repo


def test_veto_closes_three_cross_repo_children_in_their_own_repos(tmp_path: Path):
    repo, origin, shas = _make_repo(tmp_path, conflicting_last_edit=False)
    remote = [_child_in("Ext/One", 278), _child_in("Ext/Two", 12), _child_in("Ext/Three", 7)]
    gh = FakeGh(_issue_a_b(), remote_issues=remote)

    result = prov.veto(ISSUE_A, gh=gh, repo=repo, test_cmd="python -c \"1\"")

    assert result.ok, result.reason
    assert f"Provisional: {HOME_REPO}#{ISSUE_A}" in gh.searches
    assert sorted(gh.remote_closed) == sorted(
        [("Ext/One", 278, "not planned"), ("Ext/Two", 12, "not planned"), ("Ext/Three", 7, "not planned")])
    assert sorted(gh.remote_comments) == sorted(
        [("Ext/One", 278, f"reverted by the veto of #{ISSUE_A}"),
         ("Ext/Two", 12, f"reverted by the veto of #{ISSUE_A}"),
         ("Ext/Three", 7, f"reverted by the veto of #{ISSUE_A}")])
    # the home side is unchanged by the cross-repo walk: A and B closed as before, nothing else at home
    assert gh.closed == {ISSUE_A, ISSUE_B}


def test_provisional_report_lists_cross_repo_children_under_their_decision(tmp_path: Path):
    repo, origin, shas = _make_repo(tmp_path, conflicting_last_edit=False)
    remote = [_child_in("Ext/One", 278), _child_in("Ext/Two", 12), _child_in("Ext/Three", 7)]
    gh = FakeGh(_issue_a_b(), remote_issues=remote)

    report = prov.provisional_report(gh, repo=repo)

    lines = report.splitlines()
    a_index = next(i for i, line in enumerate(lines) if line.strip().startswith(f"#{ISSUE_A}"))
    cross_line = next(line for line in lines if "cross-repo" in line)
    assert lines.index(cross_line) == a_index + 1
    assert cross_line.startswith("  ")
    assert "3 cross-repo child issues" in cross_line
    assert "Ext/One#278" in cross_line and "Ext/Two#12" in cross_line and "Ext/Three#7" in cross_line


def test_provisional_report_without_cross_repo_children_is_byte_equal_to_today(tmp_path: Path):
    repo, origin, shas = _make_repo(tmp_path, conflicting_last_edit=False)
    gh = FakeGh(_issue_a_b())

    report = prov.provisional_report(gh, repo=repo)

    assert report == (
        "#201: Autopilot picks the fallback route (3 commits, 1 child issue)\n"
        "  #202: Autopilot tunes the fallback's budget (1 commit, 0 child issues)"
    )


def test_approve_makes_no_call_on_cross_repo_children(tmp_path: Path):
    repo, origin, shas = _make_repo(tmp_path, conflicting_last_edit=False)
    remote = [_child_in("Ext/One", 278), _child_in("Ext/Two", 12), _child_in("Ext/Three", 7)]
    gh = FakeGh(_issue_a_b(), remote_issues=remote)
    gh.labels[ISSUE_A].add(prov.PROVISIONAL_LABEL)

    prov.approve(ISSUE_A, gh=gh)

    assert gh.searches == []
    assert gh.remote_comments == []
    assert gh.remote_closed == []
    assert prov.PROVISIONAL_LABEL not in gh.labels[ISSUE_A]
    assert (ISSUE_A, "approved by the owner") in gh.comments


def test_cross_repo_children_ignores_a_hit_that_names_another_number():
    remote = [
        _child_in("Ext/One", 278),
        ("Ext/Two", 1, f"Provisional: {HOME_REPO}#{ISSUE_A}1\n"),  # a search hit for #2011, not #201
    ]
    gh = FakeGh(_issue_a_b(), remote_issues=remote)

    assert prov.cross_repo_children(ISSUE_A, gh=gh) == [prov.CrossChild(repo="Ext/One", number=278)]
