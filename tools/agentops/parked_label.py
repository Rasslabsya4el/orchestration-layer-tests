"""One-off pass that turns the word "parked" in ticket titles into the label `parked` (#1074).

``python -m tools.agentops.parked_label --repo <owner/name> [--dry-run]``

For one repo it lists the open issues whose title carries the word (``parked``, case-insensitive, a whole word),
adds the label, rewrites the title without the phrase, and when the phrase carried a condition ("until the day-1
prototype is accepted") puts one line ``Parked: <condition>`` on top of the body, unless the body already says
``Parked:``. It prints one row per issue (number, old title, new title, the condition when there is one).
``--dry-run`` prints the same rows and writes nothing. A gh reply that is not the expected JSON ends the run with
exit code 2 and one line on stderr.

Standard library plus ``.gh_call`` only; every ``gh`` call goes through ``run_gh`` (the one place gh is run from).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass

from .core_settings import tickets_repo
from .gh_call import run_gh

LABEL = "parked"
LABEL_COLOR = "FBCA04"
LABEL_DESCRIPTION = "waiting on a named event; not worked until it comes"

#: The one detector of the parked word, used by both ``is_parked`` and ``split_title`` (one source, so they cannot
#: diverge). ``open`` is a "(" just before the word, ``tail`` is what follows the word up to a ")", ``close`` that ")".
PARKED_RE = re.compile(r"(?P<open>\(\s*)?\bparked\b(?P<tail>[^)]*)(?P<close>\))?", re.I)
#: A body that already states the condition.
PARKED_LINE_RE = re.compile(r"^\s*parked:", re.I | re.M)
_CONDITION_EDGE_RE = re.compile(r"^[\s:,;\-\u2013\u2014]+|[\s:,;\-\u2013\u2014]+$")
_TITLE_EDGE_RE = re.compile(r"^[\s:\-]+|[\s:\-]+$")


@dataclass
class Row:
    number: int
    old_title: str
    new_title: str
    condition: str | None = None
    error: str | None = None


def is_parked(title: str) -> bool:
    return PARKED_RE.search(title or "") is not None


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _drop_unmatched_open(text: str) -> str:
    """Remove every "(" that no later ")" closes, so a title never keeps a stray open paren."""
    stack: list[int] = []
    for i, ch in enumerate(text):
        if ch == "(":
            stack.append(i)
        elif ch == ")" and stack:
            stack.pop()
    unmatched = set(stack)
    return "".join(ch for i, ch in enumerate(text) if i not in unmatched)


def _clean_title(text: str) -> str:
    return _collapse(_TITLE_EDGE_RE.sub("", _collapse(_drop_unmatched_open(text))))


def _clean_condition(text: str) -> str:
    return _collapse(_CONDITION_EDGE_RE.sub("", _collapse(text)))


def split_title(title: str) -> tuple[str, str | None]:
    """Return (the title without the parked phrase, the condition the phrase carried or None).

    Three shapes: a parenthesised phrase ("X (parked until ...)"), a leading phrase ("Parked: code-shape findings",
    no condition), and a bare word in the middle ("Foo parked - bar baz": the title is what is before the word, the
    whole tail is the condition). Raises ValueError when there is no parked word or nothing is left of the title.
    """
    m = PARKED_RE.search(title or "")
    if m is None:
        raise ValueError(f"no parked word in {title!r}")
    before = title[:m.start()]
    after = title[m.end():]
    tail = m.group("tail")
    if m.group("open") is None and not before.strip():
        rest = tail
        condition = None
    else:
        rest = before + " " + after
        condition = _clean_condition(tail) or None
    new = _clean_title(rest)
    if not new:
        raise ValueError(f"nothing is left of the title {title!r} once the parked phrase is removed")
    return new, condition


def body_with_condition(body: str, condition: str | None) -> str:
    """Put ``Parked: <condition>`` on top of the body, unless there is no condition or the body says it already."""
    if condition is None or PARKED_LINE_RE.search(body or ""):
        return body or ""
    line = f"Parked: {condition}"
    rest = (body or "").lstrip()
    return f"{line}\n\n{rest}" if rest else f"{line}\n"


def _first_line(res) -> str:
    text = (res.stderr or res.stdout or "").strip()
    return text.splitlines()[0] if text else f"gh exited {res.returncode}"


def _json_list(res, what: str, valid) -> list[dict]:
    """The JSON list of a gh reply. A failed call, a reply that is not JSON, or one of another shape is a RuntimeError."""
    if res.returncode:
        raise RuntimeError(f"gh {what} failed: {_first_line(res)}")
    try:
        data = json.loads(res.stdout or "[]")
    except ValueError:
        raise RuntimeError(f"gh {what} gave a reply that is not JSON") from None
    if not isinstance(data, list) or not all(isinstance(item, dict) and valid(item) for item in data):
        raise RuntimeError(f"gh {what} gave a reply in an unexpected shape")
    return data


def _ensure_parked_label(repo: str, runner) -> None:
    res = runner(["label", "list", "-R", repo, "--json", "name", "--limit", "200"])
    names = {item["name"] for item in _json_list(res, "label list", lambda i: isinstance(i.get("name"), str))}
    if LABEL in names:
        return
    res = runner(["label", "create", LABEL, "-R", repo, "--color", LABEL_COLOR,
                  "--description", LABEL_DESCRIPTION])
    if res.returncode:
        raise RuntimeError(f"label create failed: {_first_line(res)}")


def _list_open(repo: str, runner) -> list[dict]:
    res = runner(["issue", "list", "-R", repo, "--state", "open", "--json", "number,title,body",
                  "--limit", "1000"])
    return _json_list(res, "issue list",
                      lambda i: isinstance(i.get("number"), int) and isinstance(i.get("title"), str))


def run(repo: str, *, dry_run: bool = False, skip: frozenset[int] = frozenset(), runner=run_gh) -> list[Row]:
    """Rewrite the parked issues of ``repo``; return one Row per parked issue (``error`` set when a write failed).

    ``skip`` holds issue numbers left alone: a ticket ABOUT parking ("Parked tickets carry a label") carries the word
    in its title without being parked."""
    issues = _list_open(repo, runner)
    if not dry_run:
        _ensure_parked_label(repo, runner)
    rows: list[Row] = []
    for issue in issues:
        title = issue["title"]
        if not is_parked(title) or issue["number"] in skip:
            continue
        number = issue["number"]
        try:
            new_title, condition = split_title(title)
        except ValueError as exc:
            rows.append(Row(number, title, title, error=str(exc)))
            continue
        row = Row(number, title, new_title, condition=condition)
        rows.append(row)
        if dry_run:
            continue
        body = issue.get("body") or ""
        new_body = body_with_condition(body, condition)
        args = ["issue", "edit", str(number), "-R", repo, "--title", new_title, "--add-label", LABEL]
        stdin = None
        if new_body != body:
            args += ["--body-file", "-"]
            stdin = new_body
        res = runner(args, input=stdin)
        if res.returncode:
            row.error = _first_line(res)
    return rows


def main(argv: list[str] | None = None, runner=run_gh) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.agentops.parked_label")
    ap.add_argument("--repo", default=None, help="owner/name of the repo (default: the settings' tickets_repo, #323)")
    ap.add_argument("--dry-run", action="store_true", help="print the rows, write nothing")
    ap.add_argument("--skip", type=int, action="append", default=[], metavar="N",
                    help="an issue number to leave alone (repeatable): a ticket about parking is not parked")
    args = ap.parse_args(argv)
    repo = args.repo or tickets_repo()
    try:
        rows = run(repo, dry_run=args.dry_run, skip=frozenset(args.skip), runner=runner)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for row in rows:
        line = f"#{row.number}  {row.old_title}  ->  {row.new_title}"
        if row.condition:
            line += f"  (condition: {row.condition})"
        if row.error:
            line += f"  [ERROR: {row.error}]"
        print(line)
    mode = "dry run, nothing written" if args.dry_run else "written"
    print(f"{len(rows)} parked issue(s) in {repo} ({mode})")
    return 1 if any(row.error for row in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
