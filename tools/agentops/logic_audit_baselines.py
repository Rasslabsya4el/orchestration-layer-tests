"""``logic-audit`` part 1 (#191): the baselines added in a window, and the older code each one would refuse today.

A baseline is something added in the git history in ``[since, until)`` (commit author date):

* a file added under ``docs/adr/``: its whole text holds its tokens;
* a line added in a rule file (``CLAUDE.md``, ``CODING_STANDARDS.md``, ``docs/agents/``, ``.claude/agents/``,
  ``.claude/skills/``): its own line holds its tokens;
* a new ``.py`` file under ``tools/`` named (by path, stem, ``<stem>.py`` or dotted module name) in a rule line of the
  window: its whole text holds its tokens, and its place is the first rule line that names it.

A token is a backticked span of 3 to 60 characters. A token held by more than 8 text files of the baseline's parent
tree is generic and dropped for that baseline. The candidates of a baseline are the other text files of the parent
(at most 200 kB, not ADRs, ``docs/research/`` or rule files) that hold one of its tokens literally (case-sensitive),
at the file's first matching line. A baseline keeps at most 5 candidates (by path). The result is ordered by the number
of candidates per baseline (fewest first, the most specific), capped at 150 rows, the last one a note of the rows cut.

Read-only: git only, through ``subprocess`` with no shell; nothing is written. Stdlib only.
"""

from __future__ import annotations

import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

from .logic_audit_rows import Row, no_source_row
from .util import no_window_flags

ADR_DIR = "docs/adr/"
RESEARCH_DIR = "docs/research/"
RULE_FILES = ("CLAUDE.md", "CODING_STANDARDS.md")
RULE_DIRS = ("docs/agents/", ".claude/agents/", ".claude/skills/")
MODULE_DIR = "tools/"
MAX_CANDIDATE_BYTES = 200 * 1024
MAX_CANDIDATES_PER_BASELINE = 5
MAX_TOKEN_FILES = 8  # a token held by more files than this is generic
MAX_ROWS = 150
MIN_TOKEN, MAX_TOKEN = 3, 60
WHAT_MAX = 120
HUNK_RE = re.compile(r"^@@ -\S+ \+(\d+)")
GIT_TIMEOUT_S = 60
GREP_WORKERS = 8  # git processes in flight for the token greps


class _GitError(RuntimeError):
    """A git call the reading cannot go on without (no repo, no commits, a failed call, a timeout)."""


@dataclass(frozen=True)
class _Commit:
    sha: str
    parent: str | None
    when: datetime
    added: tuple[str, ...]  # the paths this commit adds (no renames)


@dataclass(frozen=True)
class _Rule:
    path: str
    line: int
    text: str
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class _Baseline:
    parent: str
    says: str
    place_a: str
    tokens: tuple[str, ...]


def is_rule_file(path: str) -> bool:
    return path in RULE_FILES or path.startswith(RULE_DIRS)


def tokens_of(text: str) -> tuple[str, ...]:
    """The backticked spans of ``text`` of 3 to 60 characters on one line, each once, in order.

    Spans are paired the way Markdown pairs them (the text between the 1st and 2nd backtick, the 3rd and 4th, ...).
    """
    spans = text.split("`")[1::2]
    return tuple(dict.fromkeys(s for s in spans if MIN_TOKEN <= len(s) <= MAX_TOKEN and "\n" not in s))


class _Git:
    """The git reads of one repo: tree sizes per commit and token hits per (commit, token), each read once."""

    def __init__(self, root: Path):
        self.root = root
        self._sizes: dict[str, dict[str, int]] = {}
        self._hits: dict[tuple[str, str], dict[str, int]] = {}

    def run(self, *args: str, ok: tuple[int, ...] = (0,)) -> str:
        argv = ["git", "-C", str(self.root), "-c", "core.quotepath=off", "-c", "color.ui=never", *args]
        try:
            done = subprocess.run(argv, capture_output=True, creationflags=no_window_flags(), timeout=GIT_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            # the same error path as a git failure: never read as "no baselines"
            raise _GitError(f"git {args[0]} timed out after {GIT_TIMEOUT_S} s") from exc
        if done.returncode not in ok:
            reason = done.stderr.decode("utf-8", "replace").strip()
            raise _GitError(f"git {args[0]} exited {done.returncode}: {reason}")
        return done.stdout.decode("utf-8", "replace")

    def history(self) -> list[_Commit]:
        """Every non-merge commit of the history, newest first, with its author date and the paths it adds.

        One ``git log`` for all of it (a parent of ``None`` is a root commit).
        """
        out = self.run("log", "--no-merges", "--no-renames", "--name-status", "--format=%x01%H%x1f%P%x1f%aI")
        commits: list[_Commit] = []
        header: list[str] | None = None
        added: list[str] = []

        def close() -> None:
            sha, parents, when = header
            commits.append(_Commit(sha, parents.split()[0] if parents.strip() else None,
                                   datetime.fromisoformat(when), tuple(added)))

        for line in out.splitlines():
            if line.startswith("\x01"):
                if header is not None:
                    close()
                header = line[1:].split("\x1f")
                added = []
            elif line.startswith("A\t") and header is not None:
                added.append(line[2:])
        if header is not None:
            close()
        return commits

    def rule_lines(self) -> dict[str, list[tuple[str, int, str]]]:
        """The added lines of the rule files, per commit sha: (path, line number in the new file, text).

        One ``git log -p`` limited to the rule files, so a commit that touches none of them costs nothing.
        """
        pathspec = [p.rstrip("/") for p in (*RULE_FILES, *RULE_DIRS)]
        out = self.run("log", "--no-merges", "--no-renames", "-p", "-U0", "--format=%x01%H", "--", *pathspec)
        found: dict[str, list[tuple[str, int, str]]] = {}
        sha: str | None = None
        path: str | None = None
        line_no = 0
        in_hunk = False
        for raw in out.splitlines():
            if raw.startswith("\x01"):
                sha = raw[1:].strip()
                found.setdefault(sha, [])
                path, in_hunk = None, False
            elif raw.startswith("diff --git "):
                path, in_hunk = None, False
            elif raw.startswith("@@"):
                match = HUNK_RE.match(raw)
                line_no = int(match.group(1)) if match else 0
                in_hunk = True
            elif in_hunk:
                if raw.startswith("+") and path is not None and sha is not None:
                    found[sha].append((path, line_no, raw[1:]))
                    line_no += 1
            elif raw.startswith("+++ b/"):
                path = raw[6:]
        return found

    def show(self, sha: str, path: str) -> str:
        return self.run("show", f"{sha}:{path}")

    def tree_sizes(self, rev: str) -> dict[str, int]:
        if rev not in self._sizes:
            sizes: dict[str, int] = {}
            for record in self.run("ls-tree", "-r", "-l", "-z", rev).split("\0"):
                meta, tab, path = record.partition("\t")
                size = meta.split()[-1] if tab and meta.split() else ""
                if size.isdigit():
                    sizes[path] = int(size)
            self._sizes[rev] = sizes
        return self._sizes[rev]

    def _grep(self, rev: str, token: str) -> dict[str, int]:
        """One ``git grep -F``: the text files of ``rev`` holding ``token`` literally, each with its first matching line."""
        out = self.run("grep", "-n", "-I", "-F", "-z", "-e", token, rev, ok=(0, 1))
        first: dict[str, int] = {}
        for record in out.split("\n"):
            head, _, rest = record.partition("\0")
            if head:
                line_no = rest.partition("\0")[0]
                first.setdefault(head.partition(":")[2], int(line_no))
        return first

    def prefetch(self, pairs: set[tuple[str, str]]) -> None:
        """Run the greps of every (commit, token) pair not read yet, ``GREP_WORKERS`` git processes at a time."""
        todo = sorted(pair for pair in pairs if pair not in self._hits)
        with ThreadPoolExecutor(max_workers=GREP_WORKERS) as pool:
            for key, hits in zip(todo, pool.map(lambda pair: self._grep(*pair), todo)):
                self._hits[key] = hits

    def token_files(self, rev: str, token: str) -> dict[str, int]:
        """The text files of ``rev`` holding ``token``: one grep per (commit, token), cached, so a token shared by several
        baselines of one commit is searched once."""
        if (rev, token) not in self._hits:
            self._hits[(rev, token)] = self._grep(rev, token)
        return self._hits[(rev, token)]

    def candidates(self, parent: str, tokens: tuple[str, ...]) -> list[tuple[str, int, str]]:
        """Every candidate file of a baseline (not capped): (path, first matching line, the token on that line)."""
        best: dict[str, tuple[int, str]] = {}
        for token in tokens:
            hits = self.token_files(parent, token)
            if len(hits) > MAX_TOKEN_FILES:
                continue  # generic: it says nothing about one old file
            for path, line_no in hits.items():
                if path not in best or line_no < best[path][0]:
                    best[path] = (line_no, token)
        sizes = self.tree_sizes(parent)
        found: list[tuple[str, int, str]] = []
        for path in sorted(best):
            if path.startswith((ADR_DIR, RESEARCH_DIR)) or is_rule_file(path):
                continue
            size = sizes.get(path)
            if size is None or size > MAX_CANDIDATE_BYTES:
                continue
            found.append((path, *best[path]))
        return found


def _as_datetime(value: date | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.combine(value, time())


def _in_window(when: datetime, since: datetime, until: datetime | None) -> bool:
    """``[since, until)``. A naive bound is compared with the author's wall-clock time, an aware one as an instant."""

    def on_bound_footing(bound: datetime) -> datetime:
        return when if bound.tzinfo is not None else when.replace(tzinfo=None)

    if on_bound_footing(since) < since:
        return False
    return until is None or on_bound_footing(until) < until


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _names_module(path: str, token: str) -> bool:
    """True when a backticked token names the module at ``path``: its path, its stem, ``<stem>.py`` or its dotted
    module name (``tools/agentops/x.py`` -> ``tools.agentops.x``)."""
    stem = Path(path).stem
    dotted = path[: -len(".py")].replace("/", ".")
    return token in (path, stem, f"{stem}.py", dotted)


def _naming_rule(path: str, rules: list[_Rule]) -> _Rule | None:
    """The first rule line that names a module in backticks (see ``_names_module``)."""
    return next((rule for rule in rules if any(_names_module(path, t) for t in rule.tokens)), None)


def baseline_rows(root: Path, since: date | datetime, until: date | datetime | None = None) -> list[Row]:
    """The baselines added in ``[since, until)`` (``until`` None = now) and their older candidate files.

    No git history under ``root``, or no commit in the window: one ``no source`` row. A window with commits but no
    baseline: no rows. Otherwise at most ``MAX_ROWS`` rows, the most specific baselines first; when rows are cut, the
    last row says how many.
    """
    git = _Git(Path(root))
    since_at = _as_datetime(since)
    until_at = None if until is None else _as_datetime(until)
    try:
        window = [c for c in git.history() if _in_window(c.when, since_at, until_at)]
        lines_by_sha = git.rule_lines()
    except _GitError as exc:
        return [no_source_row("baselines", f"no git history under {root} ({exc})")]
    if not window:
        return [no_source_row("baselines", f"no commit in [{since}, {until or 'now'}) under {root}")]
    window.reverse()  # oldest first

    baselines: list[_Baseline] = []
    rules: list[_Rule] = []
    modules: list[tuple[_Commit, str]] = []
    for commit in window:
        for path in commit.added:
            if commit.parent is None:
                continue
            if path.startswith(ADR_DIR):
                text = git.show(commit.sha, path)
                baselines.append(_Baseline(commit.parent, _first_line(text), f"{path}:1", tokens_of(text)))
            elif path.startswith(MODULE_DIR) and path.endswith(".py"):
                modules.append((commit, path))
        for path, line_no, text in lines_by_sha.get(commit.sha, []):
            tokens = tokens_of(text)
            rules.append(_Rule(path, line_no, text.strip(), tokens))
            if commit.parent is not None:
                baselines.append(_Baseline(commit.parent, text.strip(), f"{path}:{line_no}", tokens))
    for commit, path in modules:
        rule = _naming_rule(path, rules)
        if rule is None:
            continue
        baselines.append(_Baseline(
            commit.parent, f"new module {path}: {rule.text}", f"{rule.path}:{rule.line}",
            tokens_of(git.show(commit.sha, path)),
        ))

    git.prefetch({(baseline.parent, token) for baseline in baselines for token in baseline.tokens})
    ranked: list[tuple[int, int, list[Row]]] = []
    for index, baseline in enumerate(baselines):
        if not baseline.tokens:
            continue
        found = git.candidates(baseline.parent, baseline.tokens)
        if not found:
            continue
        rows = [
            Row(
                kind="baseline",
                finding=f"{baseline.says[:WHAT_MAX]} | older code holds {token}",
                place_a=baseline.place_a,
                place_b=f"{path}:{line_no}",
                count=1,
                verdict="reader checks",
            )
            for path, line_no, token in found[:MAX_CANDIDATES_PER_BASELINE]
        ]
        ranked.append((len(found), index, rows))

    ranked.sort(key=lambda item: (item[0], item[1]))
    rows = [row for _, _, group in ranked for row in group]
    if len(rows) > MAX_ROWS:
        kept = MAX_ROWS - 1
        more = len(rows) - kept
        rows = rows[:kept] + [Row(kind="baseline", finding=f"... {more} more baseline rows not shown", count=more)]
    return rows
