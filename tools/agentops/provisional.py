"""Provisional decisions: on autopilot the brain takes decisions for the owner
(`GLOSSARY.md`); each must be revertible with one command when the owner vetoes it on return.

    python -m tools.agentops provisional
    python -m tools.agentops veto N [--test-cmd "..."]
    python -m tools.agentops approve N

A provisional decision is an open GitHub issue labelled `provisional`. Everything built on it
carries its number: a commit trailer `Provisional: #N` (several lines allowed), or -- for a
provisional issue built on another one -- a line `Provisional: #N` in the issue body. Those body
lines link provisional issues into a tree; `veto N` collects N and every descendant, reverts every
commit on `main` whose trailer names any of them (newest first, on a throwaway worktree, tests run
before anything touches `main`), then closes the descendants and N itself with reason `not planned`
and removes the label `provisional` from N. `approve N` only removes the label and comments; it
never touches git.

A child decision may live in another repository: its body line is `Provisional: <owner>/<repo>#N`,
naming N in this (home) repo. `veto N` finds those children by searching for that text, closes each
with reason `not planned` and one comment in the child's own repo; `approve N` makes no call on them.

`GhCli` is the only thing here that calls the real `gh`; tests inject a fake (the pattern
`tools/agentops/dispatch.py`'s `GhCli` / the tests' `FakeGh` already use). Git itself runs as plain
`subprocess` calls against a `repo` path the caller passes in (`ROOT` by default), so tests point
it at a throwaway fixture repo instead.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .core_settings import load as _load_settings
from .gh_call import run_gh
from .merge import resolve_suite_env
from .util import nested_env, no_window_flags
from .worktree import ROOT

REPO = _load_settings()["repo"]
PROVISIONAL_LABEL = "provisional"
DEFAULT_TEST_CMD = 'python -m pytest -q -m "not slow"'
DEFAULT_BRANCH = "main"

#: an issue body line naming its parent decision (several allowed, one per line).
_PARENT_RE = re.compile(r"^Provisional:\s*#(\d+)\s*$", re.MULTILINE)

#: an issue body line naming a parent decision in another repo: `Provisional: Owner/Repo#N`.
_CROSS_PARENT_RE = re.compile(r"^Provisional:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)\s*$", re.MULTILINE)

#: field / trailer separators for the one-line `git log --format=...` below -- control
#: characters that never occur in a commit subject or an issue-number trailer value.
_FIELD_SEP = "\x1f"
_TRAILER_SEP = "\x03"
_LOG_FORMAT = f"%H{_FIELD_SEP}%s{_FIELD_SEP}%(trailers:key=Provisional,valueonly,separator={_TRAILER_SEP})"


# -- data -------------------------------------------------------------------------------------

@dataclass(slots=True)
class ProvisionalIssue:
    number: int
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class CrossChild:
    """An open child decision in another repo (`repo` is `Owner/Repo`), built on a home decision."""
    repo: str
    number: int


@dataclass(slots=True)
class Commit:
    sha: str
    subject: str
    provisional: tuple[int, ...]


@dataclass(slots=True)
class VetoResult:
    ok: bool
    reverted: tuple[Commit, ...] = ()
    reason: str = ""


# -- GitHub side --------------------------------------------------------------------------------

class GhCli:
    """The only `gh` calls `provisional` / `veto` / `approve` make."""

    def __init__(self, repo: str = REPO, cwd: Path = ROOT, timeout: float = 60.0) -> None:
        self.repo = repo
        self.cwd = Path(cwd)
        self.timeout = timeout

    def _run(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        return run_gh(args, cwd=self.cwd, input=stdin, timeout=self.timeout)

    def ensure_labels(self) -> None:
        """Create `provisional` if missing (`--force` makes this idempotent)."""

        for label, color in ((PROVISIONAL_LABEL, "fbca04"),):
            self._run(["label", "create", label, "-R", self.repo, "--color", color, "--force"])

    def list_provisional_issues(self) -> list[ProvisionalIssue]:
        """Every open issue labelled `provisional` (number, title, body)."""

        result = self._run([
            "issue", "list", "-R", self.repo, "--state", "open", "--label", PROVISIONAL_LABEL,
            "--json", "number,title,body", "--limit", "200",
        ])
        if result.returncode != 0:
            raise RuntimeError(f"gh issue list --label {PROVISIONAL_LABEL} failed: "
                               f"{(result.stderr or '').strip()[:200]}")
        data = json.loads(result.stdout)
        return [ProvisionalIssue(number=int(i["number"]), title=str(i.get("title") or ""),
                                 body=str(i.get("body") or "")) for i in data]

    def search_issues_mentioning(self, text: str) -> list[tuple[str, int, str]]:
        """Open issues in any repo whose body contains `text`, as `(repo, number, body)`. Search is
        fuzzy: callers check the body's own lines (`cross_repo_children`)."""

        result = self._run(["search", "issues", f'"{text}"', "--match", "body", "--state", "open",
                            "--json", "number,repository,body", "--limit", "100"])
        if result.returncode != 0:
            raise RuntimeError(f"gh search issues failed: {(result.stderr or '').strip()[:200]}")
        data = json.loads(result.stdout)
        return [((i.get("repository") or {}).get("nameWithOwner") or "", int(i["number"]), str(i.get("body") or ""))
                for i in data]

    def comment(self, number: int, body: str, repo: str | None = None) -> bool:
        result = self._run(["issue", "comment", str(number), "-R", repo or self.repo, "--body-file", "-"],
                           stdin=body)
        return result.returncode == 0

    def close(self, number: int, reason: str = "not planned", repo: str | None = None) -> bool:
        result = self._run(["issue", "close", str(number), "-R", repo or self.repo, "--reason", reason])
        return result.returncode == 0

    def add_label(self, number: int, label: str) -> bool:
        result = self._run(["issue", "edit", str(number), "-R", self.repo, "--add-label", label])
        return result.returncode == 0

    def remove_label(self, number: int, label: str) -> bool:
        result = self._run(["issue", "edit", str(number), "-R", self.repo, "--remove-label", label])
        return result.returncode == 0


# -- the tree -------------------------------------------------------------------------------------

def parse_parents(body: str) -> tuple[int, ...]:
    """Every `Provisional: #N` line in an issue body, in order."""

    return tuple(int(m) for m in _PARENT_RE.findall(body or ""))


def parse_cross_parents(body: str) -> tuple[tuple[str, int], ...]:
    """Every `Provisional: Owner/Repo#N` line in an issue body, as `("Owner/Repo", N)`, in order."""

    return tuple((m.group(1), int(m.group(2))) for m in _CROSS_PARENT_RE.finditer(body or ""))


def cross_repo_children(number: int, *, gh: GhCli) -> list[CrossChild]:
    """The open issues in other repos whose body names `<home repo>#number` on a `Provisional:` line.
    The search is fuzzy, so each hit's own lines are checked: `#2011` is not a hit for `#201`."""

    home = gh.repo.lower()
    found: list[CrossChild] = []
    for repo, child, body in gh.search_issues_mentioning(f"Provisional: {gh.repo}#{number}"):
        if repo.lower() == home:
            continue
        if (home, number) in {(r.lower(), n) for r, n in parse_cross_parents(body)}:
            found.append(CrossChild(repo=repo, number=child))
    return found


def build_tree(issues: list[ProvisionalIssue]) -> tuple[dict[int, ProvisionalIssue], dict[int, list[int]]]:
    """`(by_number, children)`: `children[p]` lists the direct child decisions of `p`."""

    by_number = {i.number: i for i in issues}
    children: dict[int, list[int]] = {}
    for issue in issues:
        for parent in parse_parents(issue.body):
            children.setdefault(parent, []).append(issue.number)
    return by_number, children


def descendants(number: int, children: dict[int, list[int]]) -> list[int]:
    """Every decision built on `number`, transitively -- excludes `number` itself."""

    out: list[int] = []
    seen: set[int] = set()
    queue = list(children.get(number, ()))
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        out.append(current)
        queue.extend(children.get(current, ()))
    return out


def provisional_report(gh: GhCli, *, repo: Path = ROOT, branch: str = DEFAULT_BRANCH) -> str:
    """The open tree in plain words: each root decision, its children indented, per node the
    commit count and child-issue count; a decision with children in other repos gets one more line
    naming them (count and `Owner/Repo#N`). No such child, no such line."""

    issues = gh.list_provisional_issues()
    by_number, children = build_tree(issues)
    if not by_number:
        return "no open provisional decisions"

    commits = list_commits(repo, branch)
    is_child = {c for kids in children.values() for c in kids}
    roots = sorted(n for n in by_number if n not in is_child)
    cross = {n: cross_repo_children(n, gh=gh) for n in by_number}

    def commit_count(n: int) -> int:
        return sum(1 for c in commits if n in c.provisional)

    def render(n: int, depth: int) -> list[str]:
        issue = by_number.get(n)
        title = issue.title if issue is not None else "(not in the open tree)"
        kids = sorted(children.get(n, []))
        count = commit_count(n)
        lines = [
            f"{'  ' * depth}#{n}: {title} "
            f"({count} commit{'s' if count != 1 else ''}, {len(kids)} child issue{'s' if len(kids) != 1 else ''})"
        ]
        found = cross.get(n, [])
        if found:
            names = ", ".join(f"{c.repo}#{c.number}" for c in found)
            lines.append(f"{'  ' * (depth + 1)}{len(found)} cross-repo child issue{'s' if len(found) != 1 else ''}: "
                         f"{names}")
        for kid in kids:
            lines.extend(render(kid, depth + 1))
        return lines

    lines: list[str] = []
    for root in roots:
        lines.extend(render(root, 0))
    return "\n".join(lines)


# -- git side ---------------------------------------------------------------------------------

GIT_TIMEOUT = 60.0  # seconds per git call; a test on a loaded machine raises it (#827)


def _run_git(args: list[str], *, cwd: Path, timeout: float | None = None,
             env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    timeout = GIT_TIMEOUT if timeout is None else timeout
    full_env = {**os.environ, **env} if env else None
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=no_window_flags(), env=full_env)


def list_commits(repo: Path, branch: str = DEFAULT_BRANCH) -> list[Commit]:
    """Every commit on `branch`, newest first (git log's own default order), with its
    `Provisional:` trailer values parsed to ints."""

    result = _run_git(["log", branch, f"--format={_LOG_FORMAT}"], cwd=repo)
    if result.returncode != 0:
        raise RuntimeError(f"git log {branch} failed: {(result.stderr or '').strip()[:200]}")
    commits: list[Commit] = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        parts = line.split(_FIELD_SEP)
        sha = parts[0]
        subject = parts[1] if len(parts) > 1 else ""
        raw_trailers = parts[2] if len(parts) > 2 else ""
        numbers = tuple(
            int(re.match(r"#?(\d+)", value.strip()).group(1))
            for value in raw_trailers.split(_TRAILER_SEP)
            if value.strip() and re.match(r"#?(\d+)", value.strip())
        )
        commits.append(Commit(sha=sha, subject=subject, provisional=numbers))
    return commits


def commits_for(numbers: set[int], commits: list[Commit]) -> list[Commit]:
    """The commits whose trailers name any of `numbers`, in `commits`' own (newest-first) order."""

    return [c for c in commits if set(c.provisional) & numbers]


def _remote_exists(repo: Path, name: str) -> bool:
    result = _run_git(["remote"], cwd=repo)
    return name in (result.stdout or "").split()


def veto(number: int, *, gh: GhCli, repo: Path = ROOT, branch: str = DEFAULT_BRANCH,
         test_cmd: str = DEFAULT_TEST_CMD, remote: str = "origin") -> VetoResult:
    """Collect `number` and its descendants, revert every commit on `branch` naming any of them
    (newest first, on a throwaway worktree), run `test_cmd` there, fast-forward `branch` and push.
    On a revert conflict, a failing test or a failed fast-forward: leaves `branch` untouched, no
    `gh` writes, `VetoResult(ok=False, reason=...)`."""

    issues = gh.list_provisional_issues()
    by_number, children = build_tree(issues)
    targets = {number, *descendants(number, children)}
    # the cross-repo children of every target: read here, before git; closed below, after the push
    cross_children = list(dict.fromkeys(
        child for target in sorted(targets) for child in cross_repo_children(target, gh=gh)))

    all_commits = list_commits(repo, branch)
    target_commits = commits_for(targets, all_commits)  # already newest-first

    head = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    if (head.stdout or "").strip() != branch:
        return VetoResult(ok=False, reason=f"{repo} is on {(head.stdout or '').strip()!r}, not {branch!r}: "
                                           f"the fast-forward would land on the wrong branch")

    branch_name = f"veto-{number}"
    worktree_parent = Path(tempfile.mkdtemp(prefix=f"{branch_name}-"))
    worktree_path = worktree_parent / "wt"

    added = _run_git(["worktree", "add", str(worktree_path), "-b", branch_name, branch], cwd=repo)
    if added.returncode != 0:
        return VetoResult(ok=False, reason=f"git worktree add failed: {(added.stderr or '').strip()[:300]}")

    def _cleanup(*, delete_branch: bool) -> None:
        _run_git(["worktree", "remove", "--force", str(worktree_path)], cwd=repo)
        if delete_branch:
            _run_git(["branch", "-D", branch_name], cwd=repo)

    for commit in target_commits:
        result = _run_git(["revert", "--no-edit", commit.sha], cwd=worktree_path)
        if result.returncode != 0:
            _run_git(["revert", "--abort"], cwd=worktree_path)
            _cleanup(delete_branch=True)
            detail = (result.stderr or result.stdout or "").strip()[:300]
            return VetoResult(ok=False, reason=f"revert conflict on {commit.sha} ({commit.subject}): {detail}")

    # the suite's own environment switches (the settings file's `merge_suite_env`) apply here too,
    # so the veto gate runs the same kind of suite as the merge gate.
    test_env = nested_env()  # #195: no outer build's vars in the veto suite
    test_env.update(resolve_suite_env())
    try:
        test_result = subprocess.run(shlex.split(test_cmd), cwd=str(worktree_path), capture_output=True,
                                     text=True, encoding="utf-8", errors="replace", timeout=600,
                                     env=test_env, creationflags=no_window_flags())
    except subprocess.TimeoutExpired:
        _cleanup(delete_branch=True)
        return VetoResult(ok=False, reason=f"test command timed out: {test_cmd!r}")
    if test_result.returncode != 0:
        _cleanup(delete_branch=True)
        detail = (test_result.stdout or test_result.stderr or "").strip()[-500:]
        return VetoResult(ok=False, reason=f"tests failed after reverting: {detail}")

    merged = _run_git(["merge", "--ff-only", branch_name], cwd=repo)
    if merged.returncode != 0:
        _cleanup(delete_branch=True)
        detail = (merged.stderr or merged.stdout or "").strip()[:300]
        return VetoResult(ok=False, reason=f"fast-forward merge failed: {detail}")

    _cleanup(delete_branch=True)  # merged into `branch`, nothing left on it

    if _remote_exists(repo, remote):
        pushed = _run_git(["push", remote, branch], cwd=repo, env={"AGENTOPS_MAIN_PUSH": "1"})
        if pushed.returncode != 0:
            detail = (pushed.stderr or pushed.stdout or "").strip()[:300]
            return VetoResult(ok=False, reason=f"reverts are on local {branch} but `git push {remote} {branch}` "
                                               f"failed, issues left open: {detail}")

    descendant_numbers = descendants(number, children)
    for child in descendant_numbers:
        gh.comment(child, f"reverted by the veto of #{number}")
        gh.close(child, "not planned")
    for child in cross_children:
        gh.comment(child.number, f"reverted by the veto of #{number}", repo=child.repo)
        gh.close(child.number, "not planned", repo=child.repo)
    gh.remove_label(number, PROVISIONAL_LABEL)
    gh.comment(number, f"veto of #{number}: reverted {len(target_commits)} commit(s)")
    gh.close(number, "not planned")

    return VetoResult(ok=True, reverted=tuple(target_commits))


def approve(number: int, *, gh: GhCli) -> None:
    """Remove `provisional` from `number` only (children stay provisional until approved on their
    own). No git."""

    gh.remove_label(number, PROVISIONAL_LABEL)
    gh.comment(number, "approved by the owner")


# -- what the core loads this module for ------------------------------------------------------------
#
# Provisional decisions are a local module of this installation (extension point 4 of the extension registry): the
# core's CLI knows nothing of them and gets the three subcommands below only when the settings file
# names this module.


def _add_provisional_parser(sub) -> None:
    sub.add_parser("provisional", help="print the open provisional-decision tree")


def _run_provisional(args) -> int:
    print(provisional_report(GhCli()))
    return 0


def _add_veto_parser(sub) -> None:
    veto_cmd = sub.add_parser("veto", help="revert a provisional decision and its descendants")
    veto_cmd.add_argument("number", type=int)
    veto_cmd.add_argument("--test-cmd", default=DEFAULT_TEST_CMD,
                          help="run inside the throwaway revert worktree before main is touched")


def _run_veto(args) -> int:
    result = veto(args.number, gh=GhCli(), test_cmd=args.test_cmd)
    if result.ok:
        print(f"veto of #{args.number}: reverted {len(result.reverted)} commit(s)")
        return 0
    print(f"veto #{args.number} failed: {result.reason}")
    return 1


def _add_approve_parser(sub) -> None:
    approve_cmd = sub.add_parser("approve", help="approve a provisional decision, no reverts")
    approve_cmd.add_argument("number", type=int)


def _run_approve(args) -> int:
    approve(args.number, gh=GhCli())
    print(f"approved #{args.number}")
    return 0


def register(core) -> None:
    core.command("provisional", _add_provisional_parser, _run_provisional, after="routes")
    core.command("veto", _add_veto_parser, _run_veto, after="routes")
    core.command("approve", _add_approve_parser, _run_approve, after="routes")


__all__ = [
    "Commit",
    "CrossChild",
    "DEFAULT_BRANCH",
    "DEFAULT_TEST_CMD",
    "GhCli",
    "ProvisionalIssue",
    "PROVISIONAL_LABEL",
    "VetoResult",
    "approve",
    "build_tree",
    "commits_for",
    "cross_repo_children",
    "descendants",
    "list_commits",
    "parse_cross_parents",
    "parse_parents",
    "provisional_report",
    "register",
    "veto",
]
