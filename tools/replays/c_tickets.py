"""tools/replays/c_tickets.py: a ticket as it stood when the owner made it ready (issue #376, unit 2).

``body_at_ready(number)`` returns the ticket's title and body as they were when the label ``ready-for-agent`` was first
added. The label time comes from the issue timeline (``LabeledEvent``), the body from the edit history
(``userContentEdits``), the title from ``RenamedTitleEvent``. ``select_ready_version`` is the pure selection on the
fetched data, so it is tested without the network.

What the GraphQL data holds (read on #355, edited six times, and on #127, #238, #218, never edited): a
``userContentEdits`` node's ``diff`` is the whole text of the body version written at its ``editedAt``, not a diff; the
newest node's ``diff`` is the current body. A node whose ``editedAt`` is older than its ``createdAt`` holds the body as
it stood before the first recorded edit. So a version's time is its ``editedAt``.

``acceptance_rows(body)`` returns the rows of the section headed Acceptance, Done when or Result.
``base_commit(number, repo_dir)`` returns the first parent of the first commit of main's first-parent line (from the
oldest) that names the ticket (``names_ticket``: a merge by its branch form, a plain commit by a subject starting
with ``#<N>``). ``main(argv)`` writes ``ticket.md``, ``rows.txt`` and ``base.txt`` for each ticket into the main checkout's
``artifacts/runs/c376/tickets/<N>/``. It reads only (``gh issue view``, ``gh api graphql``, ``git log``) and never
writes to GitHub.

Standard library plus ``tools.agentops.gh_call.run_gh``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from tools.agentops.gh_call import run_gh

READY_LABEL = "ready-for-agent"
DEFAULT_REPO = "Rasslabsya4el/orchestration-layer"

QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){issue(number:$number){"
    "title body "
    "userContentEdits(first:100){totalCount nodes{editedAt createdAt diff}} "
    "timelineItems(first:100,itemTypes:[LABELED_EVENT,RENAMED_TITLE_EVENT]){totalCount nodes{"
    "__typename ... on LabeledEvent{createdAt label{name}} "
    "... on RenamedTitleEvent{createdAt previousTitle currentTitle}}}"
    "}}}"
)

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_NUMBERED = re.compile(r"^\d+[.)]\s+\S")
_BULLET = re.compile(r"^-\s+\S")
GIT_TIMEOUT_S = 120


def _when(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _ready_time(label_events: list[dict]) -> str:
    """The time of the first ``ready-for-agent`` label added (the earliest LabeledEvent of that name)."""
    added = [e for e in label_events
             if e.get("__typename") == "LabeledEvent" and (e.get("label") or {}).get("name") == READY_LABEL]
    if not added:
        raise ValueError(f"the issue was never labelled {READY_LABEL}")
    return min(added, key=lambda e: _when(e["createdAt"]))["createdAt"]


def _body_at(current: str, edits: list[dict], at: datetime) -> tuple[str, str | None]:
    """The body version live at ``at``: the newest version written at or before it, or the current body when no edit
    came after ``at``. Returns (text, editedAt of the version used, or None for the current body)."""
    if not any(_when(e["editedAt"]) > at for e in edits):
        return current, None
    before = [e for e in edits if _when(e["editedAt"]) <= at]
    if not before:
        raise ValueError("no body version at the label time: every recorded edit is after it")
    chosen = max(before, key=lambda e: _when(e["editedAt"]))
    return chosen["diff"], chosen["editedAt"]


def _title_at(current: str, renames: list[dict], at: datetime) -> str:
    """The title live at ``at``: the previous title of the first rename after it, or the current title."""
    after = sorted((r for r in renames if _when(r["createdAt"]) > at), key=lambda r: _when(r["createdAt"]))
    return after[0]["previousTitle"] if after else current


def select_ready_version(title: str, body: str, edits: list[dict], renames: list[dict],
                         label_events: list[dict]) -> dict:
    """The title and body as they stood when ``ready-for-agent`` was first added. Pure: takes the fetched data.

    ``edits`` are ``userContentEdits`` nodes (``editedAt``, ``diff`` = the version's text); ``renames`` are
    ``RenamedTitleEvent`` nodes; ``label_events`` are the timeline's ``LabeledEvent`` nodes. Returns
    ``{"title", "body", "ready_at", "edit_used"}``; ``edit_used`` is the ``editedAt`` of the body version taken from
    the edit history, None when the current body is the one at that time.
    """
    ready_at = _ready_time(label_events)
    at = _when(ready_at)
    text, edit_used = _body_at(body, edits, at)
    return {"title": _title_at(title, renames, at), "body": text, "ready_at": ready_at, "edit_used": edit_used}


def _is_row_heading(text: str) -> bool:
    name = text.strip().strip("*:").strip().lower()
    return name == "result" or name.startswith(("acceptance", "done when"))


def _is_separator(line: str) -> bool:
    return line.startswith("|") and "--" in line and set(line) <= set("|-: ")


def acceptance_rows(body: str) -> list[str]:
    """The acceptance rows of a ticket body, each as written (stripped): the markdown table rows and the numbered or
    bullet (``- ``, ``- [ ]``) lines under a heading named Acceptance, Done when or Result. A table's header and
    separator rows are not rows; a numbered or bullet line starts its own row, and its continuation lines are joined
    to it with a space; any other text is not a row."""
    rows: list[str] = []
    level: int | None = None
    open_item: int | None = None
    lines = [line.strip() for line in body.splitlines()]
    for index, line in enumerate(lines):
        heading = _HEADING.match(line)
        if heading:
            depth = len(heading.group(1))
            if level is not None and depth <= level:
                level = None
            if level is None and _is_row_heading(heading.group(2)):
                level = depth
            open_item = None
            continue
        if level is None:
            continue
        if not line:
            open_item = None
        elif line.startswith("|"):
            is_header = index + 1 < len(lines) and _is_separator(lines[index + 1])
            if not is_header and not _is_separator(line):
                rows.append(line)
            open_item = None
        elif _NUMBERED.match(line) or _BULLET.match(line):
            rows.append(line)
            open_item = len(rows) - 1
        elif open_item is not None:
            rows[open_item] = f"{rows[open_item]} {line}"
    return rows


def _git(repo_dir: str | Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo_dir), *args], check=True, capture_output=True,
                         encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT_S)
    return out.stdout.strip()


def names_ticket(subject: str, number: int, merge: bool) -> bool:
    """Whether a commit subject names ticket ``number``. A merge names it by ``i<N>``, ``u<N>`` or ``m<N>``; a plain
    commit by a subject that starts with ``#<N>``. The number is one whole token: no letter or digit right after it,
    and for a merge no letter or digit right before the form's letter (``i4200`` is not 420, ``r219`` is not 219,
    ``2026-10-17`` and `` 127`` name nothing)."""
    if merge:
        return re.search(rf"(?<![0-9A-Za-z])[ium]{number}(?![0-9A-Za-z])", subject) is not None
    return re.match(rf"#{number}(?![0-9A-Za-z])", subject) is not None


def base_commit(number: int, repo_dir: str | Path) -> str:
    """The first parent of the first commit on main's first-parent line, walked from the oldest, whose subject names
    the ticket (``names_ticket``: a merge by its branch form, a plain commit by a subject starting with ``#<N>``).
    Raises when no commit of main names it."""
    listing = _git(repo_dir, "log", "main", "--first-parent", "--reverse", "--format=%H%x09%P%x09%s")
    for line in listing.splitlines():
        _sha, parents, subject = line.split("\t", 2)
        parent_list = parents.split()
        if names_ticket(subject, number, merge=len(parent_list) > 1):
            if not parent_list:
                raise ValueError(f"the first commit on main naming ticket {number} is a root commit: no base")
            return parent_list[0]
    raise ValueError(f"no commit of main names ticket {number}")


def fetch_history(number: int, repo: str = DEFAULT_REPO) -> dict:
    """The title, body, body edits, renames and timeline label events of one issue, by one read of `gh api graphql`."""
    owner, name = repo.split("/", 1)
    result = run_gh(["api", "graphql", "-f", f"query={QUERY}", "-f", f"owner={owner}", "-f", f"name={name}",
                     "-F", f"number={number}"])
    if result.returncode != 0:
        raise RuntimeError(f"gh api graphql failed for #{number}: {result.stderr.strip()}")
    issue = (json.loads(result.stdout)["data"]["repository"] or {}).get("issue")
    if issue is None:
        raise RuntimeError(f"gh api graphql found no issue #{number} in {repo}")
    edits = issue["userContentEdits"]
    timeline = issue["timelineItems"]
    for connection, what in ((edits, "body edits"), (timeline, "timeline events")):
        if connection["totalCount"] > len(connection["nodes"]):
            raise RuntimeError(f"#{number} has more {what} than one read holds; a page is needed")
    nodes = timeline["nodes"]
    return {
        "title": issue["title"],
        "body": issue["body"],
        "edits": edits["nodes"],
        "renames": [n for n in nodes if n.get("__typename") == "RenamedTitleEvent"],
        "label_events": [n for n in nodes if n.get("__typename") == "LabeledEvent"],
    }


def body_at_ready(number: int, repo: str = DEFAULT_REPO) -> dict:
    """The ticket's title and body as they stood when ``ready-for-agent`` was first added (reads GitHub only)."""
    fetched = fetch_history(number, repo)
    return select_ready_version(fetched["title"], fetched["body"], fetched["edits"], fetched["renames"],
                                fetched["label_events"])


def write_files(out_dir: Path, record: dict, rows: list[str], base: str) -> None:
    """ticket.md (title, blank line, body, exactly), rows.txt (one row per line), base.txt (the sha)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ticket.md").write_bytes((record["title"] + "\n\n" + record["body"]).encode("utf-8"))
    (out_dir / "rows.txt").write_bytes("".join(row + "\n" for row in rows).encode("utf-8"))
    (out_dir / "base.txt").write_bytes((base + "\n").encode("ascii"))


def main_checkout(start: str | Path) -> Path:
    """The main checkout of the repo that holds ``start`` (the parent of the common git dir, the same from a worktree)."""
    common = _git(start, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(common).parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.replays.c_tickets",
                                     description="Write each ticket's ready-time text, rows and base into artifacts.")
    parser.add_argument("numbers", nargs="+", type=int, help="ticket numbers")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--repo-dir", type=Path, default=None, help="the git checkout of the repo (default: main)")
    parser.add_argument("--out-root", type=Path, default=None,
                        help="the tickets folder (default: the main checkout's artifacts/runs/c376/tickets)")
    args = parser.parse_args(argv)
    repo_dir = args.repo_dir or main_checkout(Path(__file__).resolve().parent)
    out_root = args.out_root or repo_dir / "artifacts" / "runs" / "c376" / "tickets"
    for number in args.numbers:
        record = body_at_ready(number, args.repo)
        rows = acceptance_rows(record["body"])
        if not rows:
            print(f"#{number}: no acceptance rows under a heading Acceptance, Done when or Result", file=sys.stderr)
            return 2
        base = base_commit(number, repo_dir)
        write_files(out_root / str(number), record, rows, base)
        print(f"#{number}: {record['title']} | {len(rows)} rows | base {base[:8]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
