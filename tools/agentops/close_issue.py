"""`python -m tools.agentops close <N> --comment-file <file> [--repo OWNER/NAME] [--not-planned] [--dry-run]` (#58).

The close step of an issue. Before the issue closes, every comment that is a person's words (not a machine line, see
`comment_markers`) and was written after the issue text's last edit must be answered in the closing comment, one line
per comment:

    comment <databaseId>: done in row <n> | moved to #<n> | not a task

A comment with no such line refuses the close: its line is printed and nothing is closed (rc 1). Otherwise the issue
is closed with the closing comment's text (rc 0).

Two more refusals come before any close (#165), each printed and rc 1, the issue untouched:
`refused: empty closing text` when the closing text is empty or whitespace only, and
`refused: closing text names no acceptance row` when the issue body holds an acceptance block (a heading or a line
starting `Acceptance`, any case) and the closing text matches no `rows? N`.

The gh access is injectable (`runner`: a callable of the gh arguments and the stdin text, returning a
CompletedProcess), so the tests run with a fake gh and no live call.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

from .comment_markers import first_line, is_machine_comment
from .gh_call import run_gh

#: a gh call: the arguments and the stdin text (or None), the result has returncode, stdout and stderr
Runner = Callable[[list[str], "str | None"], subprocess.CompletedProcess]

#: the first line of a closing-comment answer: "comment 1001: done in row 3" (bullets and case allowed)
_ANSWER = re.compile(r"^\s*(?:[-*]\s*)?comment\s+(\d+)\s*:\s*(done in row \d+|moved to #\d+|not a task)\b", re.I)
#: an acceptance block of the issue body: a heading or a line starting "Acceptance" (#165)
_ACCEPTANCE_BLOCK = re.compile(r"^[ \t]*(?:#+[ \t]*)?acceptance", re.I | re.M)
#: a closing text that names an acceptance row: "row 3", "Rows 1-2", "- Row 1: pytest -> 3 passed" (#165)
_ROW_NAMED = re.compile(r"\brows? \d", re.I)
_LINE_CUT = 120
_ERROR_CUT = 200

_ISSUE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      id state createdAt lastEditedAt body
      comments(first: 100) {
        pageInfo { hasNextPage endCursor }
        nodes { databaseId createdAt body author { login } }
      }
    }
  }
}
"""

_COMMENTS_QUERY = """
query($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on Issue {
      comments(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { databaseId createdAt body author { login } }
      }
    }
  }
}
"""


class CloseIssueError(Exception):
    """A read or a file the close step cannot use; the CLI prints it and returns 2."""


def default_runner(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return run_gh(args, input=stdin)


def _when(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _graphql(runner: Runner, query: str, variables: dict) -> dict:
    payload = json.dumps({"query": query, "variables": variables})
    result = runner(["api", "graphql", "--input", "-"], payload)
    if result.returncode:
        raise CloseIssueError(f"gh api graphql failed: {(result.stderr or '').strip()[:_ERROR_CUT]}")
    body = json.loads(result.stdout)
    if body.get("errors"):
        raise CloseIssueError(f"graphql error: {body['errors'][0].get('message')}")
    return body["data"]


def _detect_repo(runner: Runner) -> str:
    result = runner(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"], None)
    if result.returncode:
        raise CloseIssueError(f"gh repo view failed: {(result.stderr or '').strip()[:_ERROR_CUT]}")
    return result.stdout.strip()


def read_issue(runner: Runner, repo: str, number: int) -> dict:
    """The issue's state, body, createdAt, lastEditedAt and all its comments, oldest first. One graphql call; the
    comments past the first page come by `node(id)` calls, as in scripts/comment_tasks_extract.py."""
    if "/" not in repo:
        raise CloseIssueError(f"--repo must be OWNER/NAME, got {repo!r}")
    owner, name = repo.split("/", 1)
    repository = _graphql(runner, _ISSUE_QUERY, {"owner": owner, "name": name, "number": number})["repository"]
    issue = (repository or {}).get("issue")
    if issue is None:
        raise CloseIssueError(f"issue #{number} not found in {repo}")
    conn = issue["comments"]
    comments = list(conn["nodes"])
    info = conn["pageInfo"]
    while info["hasNextPage"]:
        data = _graphql(runner, _COMMENTS_QUERY, {"id": issue["id"], "cursor": info["endCursor"]})
        conn = data["node"]["comments"]
        comments.extend(conn["nodes"])
        info = conn["pageInfo"]
    return {"state": issue["state"], "body": issue.get("body") or "", "createdAt": issue["createdAt"],
            "lastEditedAt": issue.get("lastEditedAt"), "comments": comments}


def answered_ids(closing_text: str) -> set[int]:
    """The comment ids the closing comment answers (one per line, the three answer forms)."""
    ids: set[int] = set()
    for line in closing_text.splitlines():
        match = _ANSWER.match(line)
        if match:
            ids.add(int(match.group(1)))
    return ids


def unanswered_comments(issue: dict, closing_text: str) -> list[dict]:
    """The person's comments written after the issue text's last edit that the closing comment does not answer."""
    edited = _when(issue.get("lastEditedAt") or issue["createdAt"])
    answered = answered_ids(closing_text)
    return [
        comment for comment in issue["comments"]
        if not is_machine_comment(comment.get("body") or "")
        and _when(comment["createdAt"]) > edited
        and comment["databaseId"] not in answered
    ]


def _listing(comment: dict) -> str:
    author = (comment.get("author") or {}).get("login") or "-"
    line = first_line(comment.get("body") or "") or ""
    return f"comment {comment['databaseId']} | {comment['createdAt'][:10]} | {author} | {line[:_LINE_CUT]}"


def run_close(number: int, comment_file: Path, *, repo: str | None = None, not_planned: bool = False,
              dry_run: bool = False, runner: Runner | None = None) -> int:
    """Check the closing comment against the issue, then close it when every comment is answered.

    Returns 0 when the issue is closed (or, with dry_run, would be), 1 when a refusal stops the close (an empty closing
    text, an acceptance body the text names no row of, an unanswered comment), and raises CloseIssueError on a read
    that cannot be used. Prints the refusal lines, or the dry-run line, or
    gh's own output of the close.
    """
    try:
        closing_text = Path(comment_file).read_text(encoding="utf-8")
    except OSError as exc:
        raise CloseIssueError(f"cannot read the closing comment file {comment_file}: {exc}") from None
    if not closing_text.strip():
        print("refused: empty closing text")
        return 1
    gh = runner or default_runner
    repo = repo or _detect_repo(gh)
    issue = read_issue(gh, repo, number)

    listed = unanswered_comments(issue, closing_text)
    if listed:
        for comment in listed:
            print(_listing(comment))
        print(f"not closed: {len(listed)} comment(s) after the last edit of the issue text are not answered "
              "in the closing comment")
        return 1

    if _ACCEPTANCE_BLOCK.search(issue["body"]) and not _ROW_NAMED.search(closing_text):
        print("refused: closing text names no acceptance row")
        return 1

    reason = " as not planned" if not_planned else ""
    if dry_run:
        print(f"dry run: would close #{number} in {repo}{reason}; nothing closed")
        return 0

    args = ["issue", "close", str(number), "--repo", repo, f"--comment={closing_text}"]
    if not_planned:
        args += ["--reason", "not planned"]
    result = gh(args, None)
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)
    return result.returncode


def add_subparser(sub) -> None:
    parser = sub.add_parser(
        "close", help="close an issue only when its closing comment answers every comment after the text's last edit")
    parser.add_argument("number", type=int)
    parser.add_argument("--comment-file", required=True, type=Path,
                        help="the closing comment (UTF-8): one 'comment <id>: done in row N | moved to #M | not a task' line per comment")
    parser.add_argument("--repo", default=None, help="OWNER/NAME (default: the current clone's)")
    parser.add_argument("--not-planned", action="store_true", help="close as not planned")
    parser.add_argument("--dry-run", action="store_true", help="run the check and print what would happen; close nothing")


def run_close_command(args) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        return run_close(args.number, args.comment_file, repo=args.repo, not_planned=args.not_planned,
                         dry_run=args.dry_run)
    except CloseIssueError as exc:
        print(f"error: {exc}")
        return 2
