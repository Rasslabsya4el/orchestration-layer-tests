"""``core issue``: write one core issue into the home's tracker, from any repo running the core.

    python -m tools.agentops core issue --role R --kind broke --what <manifest file>[: <rule>] \\
        --error TEXT --log TEXT --did "worked around" --session ID [--same N]

Facts of the orchestration only: every path-looking token in ``--what``, ``--error`` and ``--log``
must name a file of the manifest (``core_check.read_manifest``); a token outside it refuses the
whole call before any tracker write. The new issue becomes a native sub-issue of the one open
parent issue at the home (found by its own label, created when none exists, capped so it never
grows without bound). ``gh`` is injected (``gh_runner``) so tests never touch a real tracker.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .core_check import MANIFEST_PATH, read_manifest
from .core_update import HOME_RULE, load_settings
from .gh_call import run_gh
from .util import SESSION_ID_ENV_VARS, no_window_flags

DEFAULT_ROOT = Path(__file__).resolve().parents[2]

CORE_ISSUE_LABEL = "core-issue"
CORE_ISSUE_LABEL_META = {
    "color": "5319e7",
    "description": "A defect, question or idea about the shared orchestration core",
}
CORE_EPIC_LABEL = "core-epic"
CORE_EPIC_LABEL_META = {
    "color": "5319e7",
    "description": "The one parent issue core issues attach to as native sub-issues",
}
PARENT_TITLE = "Core epic: core issues"
# #371: a core issue is a fixed protocol report (no rule text), so its agent block names no outside case.
CLASS_NONE_LINE = "Class: none (a fixed protocol: the body reports one defect, no rule text)"
SAME_OBJECT_LINE = "Same object: none (a finding filed by the core tool; the brain writes this line when it writes the fix)"
TITLE_MAX = 80
UNDO_LINE = "обратимо: один откат; затрагивает ядро оркестрации"
PARENT_BODY = (
    "## For the human\n"
    "- **What:** единая родительская задача, к которой каждая задача ядра прикрепляется как подзадача.\n"
    "- **Goal:** задачи ядра из каждого репозитория, где работает ядро, лежат в одном месте и видны там.\n"
    "- **Plan:** создаётся автоматически и остаётся открытой; никто не закрывает её вручную.\n"
    "- **Result:** ещё не задан\n"
    f"- **Undo:** {UNDO_LINE}\n"
    "\n## For the agent\n"
    f"{CLASS_NONE_LINE}\n"
    f"{SAME_OBJECT_LINE}\n"
    "Core issues attach here as native sub-issues. Created automatically; do not close by hand."
)

CAP_THRESHOLD = 90
CAP_TARGET = 80
SUB_ISSUE_LIMIT = 100  # GitHub's most sub-issues one parent may hold (HTTP 422 past it)

KIND_CHOICES = ("broke", "looks off", "change", "idea")
DID_CHOICES = ("worked around", "parked")
CAUGHT_BY_CHOICES = ("suite", "orch-diff", "brain-review", "owner", "later-run")
REWORK_KINDS = ("broke", "looks off")
_CAUSED_BY_RE = re.compile(r"^(#\d+|unknown)$")

_PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_./\\-]+")
_EXTENSION_RE = re.compile(r"\.(py|md|json|txt)$", re.IGNORECASE)


class CoreIssueError(RuntimeError):
    """One line to print; ``core issue`` exits non-zero on this, never writes anything."""


@dataclass
class CoreIssueResult:
    created_issue: int | None
    comment_added: bool
    parent_issue: int | None
    removed: list[int]
    repo: str
    core_version: str | None
    home: str
    message: str


# -- validation (facts of the orchestration only) ------------------------------------------------

def _path_like_tokens(text: str) -> list[str]:
    tokens = []
    for raw in _PATH_TOKEN_RE.findall(text):
        if "/" in raw or "\\" in raw or _EXTENSION_RE.search(raw):
            tokens.append(raw)
    return tokens


def _validate_paths(what: str, error: str, log: str, manifest_path: Path) -> None:
    manifest = set(read_manifest(manifest_path))
    what_path = what.split(": ", 1)[0].strip()
    if what_path not in manifest:
        raise CoreIssueError(f"not a file of the manifest: {what_path}")
    # only `what` is checked; error and log are free text
    for token in _path_like_tokens(what):
        normalized = token.replace("\\", "/")
        if normalized not in manifest:
            raise CoreIssueError(f"not a file of the manifest: {normalized}")


def _validate_rework_fields(kind: str, caused_by: str, caught_by: str | None) -> None:
    choices = " | ".join(CAUGHT_BY_CHOICES)
    if kind == "broke" and not caught_by:
        raise CoreIssueError(f"core issue: kind broke needs --caught-by (caught by: {choices})")
    if caught_by is not None and caught_by not in CAUGHT_BY_CHOICES:
        raise CoreIssueError(f"core issue: invalid --caught-by {caught_by!r} (caught by: {choices})")
    if not _CAUSED_BY_RE.match(caused_by or ""):
        raise CoreIssueError(f"core issue: invalid --caused-by {caused_by!r} (caused by: #<number> | unknown)")


# -- gh / git plumbing -----------------------------------------------------------------------

def _gh_runner(args: list[str]):
    return run_gh(args)


def _detect_repo(runner) -> str:
    result = runner(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"])
    if result.returncode != 0:
        raise CoreIssueError(f"gh repo view failed: {(result.stderr or '').strip()[:200]}")
    return result.stdout.strip()


def _git_head(cwd: Path, git_runner) -> str:
    runner = git_runner or (lambda args: subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", creationflags=no_window_flags(),
    ))
    result = runner(["rev-parse", "HEAD"])
    if result.returncode != 0:
        raise CoreIssueError(f"git rev-parse HEAD failed: {(result.stderr or '').strip()[:200]}")
    return result.stdout.strip()


def _parse_issue_number(stdout: str) -> int:
    return int(stdout.strip().rstrip("/").rsplit("/", 1)[-1])


def _ensure_label(runner, home: str, name: str, color: str, description: str) -> None:
    result = runner(["label", "create", name, "-R", home, "--color", color,
                     "--description", description, "--force"])
    if result.returncode != 0 and "already exists" not in (result.stderr or "").lower():
        raise CoreIssueError(f"gh label create {name!r} failed: {(result.stderr or '').strip()[:200]}")


# -- body / title -----------------------------------------------------------------------------

def _field_block(name: str, value: str) -> str:
    if "\n" in value:
        return f"**{name}:**\n```\n{value}\n```"
    return f"**{name}:** {value}"


_PATH_TOKEN = re.compile(r"[\w./\-]+\.(?:py|md)\b(?::\d+)?")


def _plain(text: str) -> str:
    """One plain sentence: first line, a file path token shortened to its bare name, no line refs."""

    first = (text.strip().splitlines() or [""])[0]
    first = _PATH_TOKEN.sub(lambda m: re.split(r"[/\\]", m.group(0))[-1].split(".")[0], first)
    first = re.sub(r"(?<![\d\s]):\d+", "", first)
    return first.strip() or "a defect in the core"


def _title(repo: str, kind: str, what: str) -> str:
    """``<repo>: <kind>: <what>``, at most ``TITLE_MAX`` characters, no issue number; a long
    ``what`` is cut at a word and ends in an ellipsis, the ``<repo>: <kind>:`` part is kept."""

    head = f"{repo}: {kind}: "
    body = re.sub(r"#(?=\d)", "", " ".join(what.split()))
    title = head + body
    if len(title) <= TITLE_MAX:
        return title
    room = max(TITLE_MAX - len(head) - 3, 1)
    cut = body[:room]
    if len(body) > room and body[room] != " " and " " in cut:
        cut = cut[:cut.rindex(" ")]
    return (head + cut.rstrip(" ,;:.-") + "...")[:TITLE_MAX]


def _human_block(repo: str, what: str, goal: str | None = None, result: str | None = None,
                 what_human: str | None = None) -> str:
    return (
        "## For the human\n"
        f"- **What:** {_plain(what_human if what_human is not None else what)}\n"
        f"- **Goal:** {goal or 'дефект ядра, найденный при работе в ' + repo + ', исправлен для всех репо с ядром'}\n"
        "- **Plan:** владелец ядра разберёт задачу.\n"
        f"- **Result:** {result or 'ещё не задан'}\n"
        f"- **Undo:** {UNDO_LINE}\n"
        "\n## For the agent\n"
    )


def _issue_body(repo: str, core_version: str | None, role: str, kind: str, what: str, error: str,
                log: str, did: str, session: str, caused_by: str = "unknown",
                caught_by: str | None = None, what_human: str | None = None) -> str:
    fields = [
        ("repo", repo),
        ("core_version", core_version or ""),
        ("role", role),
        ("kind", kind),
        ("what failed", what),
        ("error", error),
        ("dispatcher log", log),
        ("what the agent did", did),
        ("session", session),
    ]
    if kind in REWORK_KINDS:
        fields += [("caused by", caused_by), ("caught by", caught_by or "unknown")]
    return (_human_block(repo, what, what_human=what_human) + CLASS_NONE_LINE + "\n" + SAME_OBJECT_LINE + "\n\n"
            + "\n\n".join(_field_block(name, str(value)) for name, value in fields))


def _comment_body(repo: str, core_version: str | None, session: str) -> str:
    return "\n\n".join([
        f"**repo:** {repo}",
        f"**core_version:** {core_version or ''}",
        f"**session:** {session}",
    ])


# -- same-defect check --------------------------------------------------------------------------

def find_open_core_issue(*, kind: str, what: str, gh_runner=None, settings: dict | None = None,
                         cwd: Path | None = None) -> int | None:
    """The number of an already-open core issue naming the same defect (title starting
    ``<repo>: <kind>: <manifest file>``), or ``None``. A caller that wants a duplicate to become a
    comment rather than a new issue looks this up first and passes the result as ``same=`` to
    ``write_core_issue``."""

    cwd = cwd or DEFAULT_ROOT
    runner = gh_runner or _gh_runner
    settings = settings if settings is not None else load_settings(cwd)

    repo = _detect_repo(runner)
    if settings.get("update_rule") == HOME_RULE:
        home = repo
    else:
        home = settings["home"]

    core_file = what.split(": ", 1)[0].strip()
    prefix = f"{repo}: {kind}: {core_file}"
    result = runner(["issue", "list", "-R", home, "--label", CORE_ISSUE_LABEL, "--state", "open",
                     "--json", "number,title"])
    if result.returncode != 0:
        raise CoreIssueError(f"gh issue list failed: {(result.stderr or '').strip()[:200]}")
    rows = json.loads(result.stdout or "[]")
    for row in rows:
        if (row.get("title") or "").startswith(prefix):
            return row["number"]
    return None


def _print_same_defect_matches(runner, home: str, core_file: str) -> list[dict]:
    result = runner(["issue", "list", "-R", home, "--label", CORE_ISSUE_LABEL, "--state", "open",
                     "--json", "number,title,body"])
    if result.returncode != 0:
        raise CoreIssueError(f"gh issue list failed: {(result.stderr or '').strip()[:200]}")
    rows = json.loads(result.stdout or "[]")
    matches = [row for row in rows if core_file in (row.get("body") or "")]
    if matches:
        numbers = ", ".join(f"#{row['number']}" for row in matches)
        print(f"open core issue(s) already naming {core_file}: {numbers}")
    return matches


def _comment_same_defect(runner, home: str, same: int, repo: str, core_version: str | None,
                          session: str) -> None:
    body = _comment_body(repo, core_version, session)
    result = runner(["issue", "comment", str(same), "-R", home, "--body", body])
    if result.returncode != 0:
        raise CoreIssueError(f"gh issue comment failed: {(result.stderr or '').strip()[:200]}")


# -- issue creation, the parent, the cap ---------------------------------------------------------

def _area_for(settings: dict, home: str) -> str:
    # The area label exists only at the code's own repo; a target's issue goes to a home that
    # differs, and the target cannot know the home's areas, so it carries none.
    area = settings.get("code_issue_area") or ""
    return area if area and home == settings.get("repo") else ""


def _create_issue(runner, home: str, title: str, body: str, labels: list[str],
                  area: str = "") -> int:
    args = ["issue", "create", "-R", home, "--title", title, "--body", body]
    for label in labels + ([area] if area else []):
        args += ["--label", label]
    result = runner(args)
    if result.returncode != 0:
        raise CoreIssueError(f"gh issue create failed: {(result.stderr or '').strip()[:200]}")
    return _parse_issue_number(result.stdout)


def _issue_database_id(runner, home: str, number: int) -> int:
    result = runner(["api", f"repos/{home}/issues/{number}", "--jq", ".id"])
    if result.returncode != 0:
        raise CoreIssueError(f"gh api issues/{number} failed: {(result.stderr or '').strip()[:200]}")
    return int(result.stdout.strip())


def _find_or_create_parent(runner, home: str, area: str = "") -> int:
    result = runner(["issue", "list", "-R", home, "--label", CORE_EPIC_LABEL, "--state", "open",
                     "--json", "number,title"])
    if result.returncode != 0:
        raise CoreIssueError(f"gh issue list (parent) failed: {(result.stderr or '').strip()[:200]}")
    rows = json.loads(result.stdout or "[]")
    if rows:
        return rows[0]["number"]
    _ensure_label(runner, home, CORE_EPIC_LABEL, CORE_EPIC_LABEL_META["color"],
                 CORE_EPIC_LABEL_META["description"])
    return _create_issue(runner, home, PARENT_TITLE, PARENT_BODY, [CORE_EPIC_LABEL], area)


def _attach_sub_issue(runner, home: str, parent_number: int, sub_issue_id: int) -> None:
    result = runner(["api", "-X", "POST", f"repos/{home}/issues/{parent_number}/sub_issues",
                     "-F", f"sub_issue_id={sub_issue_id}"])
    if result.returncode != 0:
        raise CoreIssueError(f"sub-issue attach failed: {(result.stderr or '').strip()[:200]}")


@dataclass
class MakeRoomResult:
    removed: list[int]
    remaining: int


def make_room(home: str, parent_number: int, gh_runner=None) -> MakeRoomResult:
    """Before an attach (#1112): a parent holding ``CAP_THRESHOLD`` or more sub-issues gives up its
    oldest *closed* ones (by ``closed_at``) until ``CAP_TARGET`` remain, or as many as there are
    closed ones to give. An open one is never removed, so a parent full of open children keeps
    all of them and ``remaining`` says so.
    Callable alone (``make_room(home, parent_number)``) for a one-off trim of a parent."""
    runner = gh_runner or _gh_runner
    result = runner(["api", f"repos/{home}/issues/{parent_number}/sub_issues?per_page=100",
                     "--paginate", "--slurp"])
    if result.returncode != 0:
        raise CoreIssueError(f"gh api sub_issues (list) failed: {(result.stderr or '').strip()[:200]}")
    pages = json.loads(result.stdout or "[]")
    # `--slurp` wraps every page in one outer array; a flat list is accepted too.
    subs = [s for page in pages for s in page] if pages and isinstance(pages[0], list) else pages
    if len(subs) < CAP_THRESHOLD:
        return MakeRoomResult(removed=[], remaining=len(subs))

    closed = sorted((s for s in subs if s.get("state") == "closed"),
                    key=lambda s: s.get("closed_at") or "")
    to_remove = closed[:max(0, len(subs) - CAP_TARGET)]

    removed_ids: list[int] = []
    for sub in to_remove:
        result = runner(["api", "-X", "DELETE", f"repos/{home}/issues/{parent_number}/sub_issue",
                         "-F", f"sub_issue_id={sub['id']}"])
        if result.returncode != 0:
            raise CoreIssueError(f"sub-issue detach failed: {(result.stderr or '').strip()[:200]}")
        removed_ids.append(sub["id"])
    return MakeRoomResult(removed=removed_ids, remaining=len(subs) - len(removed_ids))


# -- the whole call -------------------------------------------------------------------------------

def write_core_issue(*, role: str, kind: str, what: str, error: str, log: str, did: str,
                     session: str, same: int | None = None, caused_by: str = "unknown",
                     caught_by: str | None = None, what_human: str | None = None, gh_runner=None,
                     git_runner=None, settings: dict | None = None, cwd: Path | None = None,
                     manifest_path: Path | None = None) -> CoreIssueResult:
    cwd = cwd or DEFAULT_ROOT
    manifest_path = manifest_path or MANIFEST_PATH
    _validate_rework_fields(kind, caused_by, caught_by)
    _validate_paths(what, error, log, manifest_path)

    runner = gh_runner or _gh_runner
    settings = settings if settings is not None else load_settings(cwd)

    repo = _detect_repo(runner)
    if settings.get("update_rule") == HOME_RULE:
        home = repo
        core_version = _git_head(cwd, git_runner)
    else:
        home = settings["home"]
        core_version = settings.get("core_version")

    core_file = what.split(": ", 1)[0].strip()
    _print_same_defect_matches(runner, home, core_file)

    if same is not None:
        _comment_same_defect(runner, home, same, repo, core_version, session)
        return CoreIssueResult(
            created_issue=None, comment_added=True, parent_issue=None, removed=[],
            repo=repo, core_version=core_version, home=home,
            message=f"same defect: commented on #{same} at {home}",
        )

    _ensure_label(runner, home, CORE_ISSUE_LABEL, CORE_ISSUE_LABEL_META["color"],
                 CORE_ISSUE_LABEL_META["description"])
    title = _title(repo, kind, what)
    body = _issue_body(repo, core_version, role, kind, what, error, log, did, session,
                       caused_by, caught_by, what_human=what_human)
    new_number = _create_issue(runner, home, title, body, [CORE_ISSUE_LABEL, "needs-triage"],
                              _area_for(settings, home))
    new_id = _issue_database_id(runner, home, new_number)

    parent_number = _find_or_create_parent(runner, home, _area_for(settings, home))
    room = make_room(home, parent_number, runner)
    removed = room.removed
    note = f"; {len(removed)} closed sub-issue(s) taken off the parent" if removed else ""

    if room.remaining >= SUB_ISSUE_LIMIT:
        return CoreIssueResult(
            created_issue=new_number, comment_added=False, parent_issue=None, removed=removed,
            repo=repo, core_version=core_version, home=home,
            message=(f"issue #{new_number} written at {home}{note}; not attached: parent "
                     f"#{parent_number} holds {SUB_ISSUE_LIMIT} open sub-issues"),
        )

    _attach_sub_issue(runner, home, parent_number, new_id)
    message = f"issue #{new_number} written at {home}, attached to parent #{parent_number}{note}"
    return CoreIssueResult(
        created_issue=new_number, comment_added=False, parent_issue=parent_number, removed=removed,
        repo=repo, core_version=core_version, home=home, message=message,
    )


# -- CLI ----------------------------------------------------------------------------------------

def _resolve_session(given: str | None) -> str:
    """The session field's value when `--session` is not given: the first non-empty of
    `SESSION_ID_ENV_VARS`, else `"none"`. A given `--session` always wins (#286)."""
    if given:
        return given
    import os

    for var in SESSION_ID_ENV_VARS:
        value = os.environ.get(var)
        if value:
            return value
    return "none"


def add_issue_subparser(core_sub) -> None:
    issue = core_sub.add_parser("issue", help="write one core issue at the home")
    issue.add_argument("--role", required=True)
    issue.add_argument("--kind", required=True, choices=list(KIND_CHOICES))
    issue.add_argument("--what", required=True, help="a manifest file, optionally 'path: rule text'")
    issue.add_argument("--error", required=True)
    issue.add_argument("--log", required=True, help="the dispatcher's log lines")
    issue.add_argument("--did", required=True, choices=list(DID_CHOICES))
    issue.add_argument("--session", required=False, default=None,
                       help="defaults to the first non-empty of SESSION_ID_ENV_VARS, else 'none'")
    issue.add_argument("--same", type=int, default=None, help="the number of an open duplicate")
    issue.add_argument("--caused-by", default="unknown", help="#N of the issue that caused it, or unknown")
    issue.add_argument("--caught-by", default=None,
                       help="broke needs it: " + " | ".join(CAUGHT_BY_CHOICES))


def run_issue_command(args) -> int:
    try:
        result = write_core_issue(
            role=args.role, kind=args.kind, what=args.what, error=args.error, log=args.log,
            did=args.did, session=_resolve_session(args.session), same=args.same,
            caused_by=args.caused_by, caught_by=args.caught_by,
        )
    except CoreIssueError as exc:
        print(str(exc))
        return 2
    print(result.message)
    return 0
