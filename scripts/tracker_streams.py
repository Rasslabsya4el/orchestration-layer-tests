"""Cut a map into streams on GitHub: parents, native sub-issue links, `Part of #N` lines.

    python scripts/tracker_streams.py --plan PLAN.json [--dry-run]

Plan: ``maps`` -> ``rows``. A row is a parent to create (``kind`` "stream" is attached to its map, "beside" gets no
parent) or an existing parent by number (``kind`` "existing"); kind "root" rows are nobody's sub-issue:
only their `Part of` line goes, a native parent they have is named, never unlinked. ``numbers`` are the tickets that become its native
sub-issues. Every step checks the live state first, so a second run writes nothing. Never closes, reopens or
relabels anything but the parents it creates; never deletes; never touches a comment.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HEAD_LINES = 8
# `Part of #N`, an optional `(note)` with one level of nested parens, then `.` / `;` or the end of the line
# (live bodies also carry `Part of #108 (Autopilot map).` and `Part of #63; the work item ...`)
_PART_OF = re.compile(r"^Part of #\d+(?:\s*\((?:[^()]|\([^()]*\))*\))?(?:[.;]|(?=\s*$))")


class GhError(RuntimeError):
    pass


def strip_part_of(body: str) -> str:
    """Drop the `Part of #N` line/clause within the first 8 lines; every other byte stays."""
    lines = body.splitlines(keepends=True)
    for i, line in enumerate(lines[:HEAD_LINES]):
        m = _PART_OF.match(line)
        if not m:
            continue
        rest = line[m.end():]
        if rest.strip() == "":
            end = i + 1
            if end < len(lines) and lines[end].strip() == "":
                end += 1
            del lines[i:end]
        else:
            lines[i] = rest[1:] if rest.startswith(" ") else rest
        break
    return "".join(lines)


def _default_runner(args: list[str], data: bytes | None = None):
    # Bounded wait; no console window of its own (CREATE_NO_WINDOW on Windows, 0 elsewhere).
    flags = 0x08000000 if sys.platform == "win32" else 0
    return subprocess.run(["gh", *args], input=data, capture_output=True, timeout=120, creationflags=flags)


class GhClient:
    def __init__(self, repo: str, runner=_default_runner):
        self.repo, self.runner = repo, runner

    def _call(self, args, payload=None, allow_404=False):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        if payload is not None:
            args = [*args, "--input", "-"]
        r = self.runner(args, data)
        out = (r.stdout or b"").decode("utf-8", "replace")
        if r.returncode != 0:
            err = (r.stderr or b"").decode("utf-8", "replace")
            if allow_404 and ("404" in err or "Not Found" in err):
                return None
            raise GhError(f"gh {' '.join(args[:4])} failed: {err.strip()[:200]}")
        return json.loads(out) if out.strip() else None

    def list_titles(self) -> dict[str, int]:
        pages = self._call(["api", f"repos/{self.repo}/issues?state=all&per_page=100", "--paginate", "--slurp"])
        items = [i for p in pages for i in (p if isinstance(p, list) else [p])]
        titles: dict[str, int] = {}
        for i in items:
            if "pull_request" not in i:
                titles.setdefault(i["title"], i["number"])
        return titles

    def get_issue(self, n: int) -> dict:
        return self._call(["api", f"repos/{self.repo}/issues/{n}"])

    def get_parent(self, n: int) -> int | None:
        p = self._call(["api", f"repos/{self.repo}/issues/{n}/parent"], allow_404=True)
        return p["number"] if p else None

    def create_issue(self, title, body, labels, closed) -> dict:
        issue = self._call(["api", "-X", "POST", f"repos/{self.repo}/issues"],
                           {"title": title, "body": body, "labels": labels})
        if closed:
            self._call(["api", "-X", "PATCH", f"repos/{self.repo}/issues/{issue['number']}"],
                       {"state": "closed", "state_reason": "completed"})
        return issue

    def attach(self, parent: int, child_id: int) -> None:
        self._call(["api", "-X", "POST", f"repos/{self.repo}/issues/{parent}/sub_issues"],
                   {"sub_issue_id": child_id, "replace_parent": True})

    def edit_body(self, n: int, body: str) -> None:
        self._call(["api", "-X", "PATCH", f"repos/{self.repo}/issues/{n}"], {"body": body})


def run(plan: dict, client, dry_run: bool = False, out=print) -> list[str]:
    """Do the plan; returns the printed action lines (one per write)."""
    lines: list[str] = []

    def act(text: str) -> None:
        lines.append(text)
        out(("[dry-run] " if dry_run else "") + text)

    labels = plan.get("labels", ["area:orchestration", "enhancement"])
    titles = client.list_titles()
    for m in plan["maps"]:
        mapn = m["map"]
        rows = m["rows"]
        map_labels = m.get("labels", labels)   # a map may name its own area label for the parents it creates
        parents: dict[int, int | None] = {}   # row index -> number (None: would be created)
        created: set[int] = set()
        # 1. missing parents
        for ri, row in enumerate(rows):
            if row["kind"] == "existing":
                parents[ri] = row["existing"]
            elif row["kind"] == "root":
                parents[ri] = None   # nobody's sub-issue: no move, only the Part of line goes
            elif row["title"] in titles:
                parents[ri] = titles[row["title"]]
            else:
                closed = row.get("born") == "closed"
                what = "stream" if row["kind"] == "stream" else "parent beside the map"
                tail = f" + attach to #{mapn}" if row["kind"] == "stream" else ""
                act(f'create {what} "{row["title"]}" ({"closed" if closed else "open"}){tail}')
                created.add(ri)
                if dry_run:
                    parents[ri] = None
                else:
                    if row["kind"] == "stream":
                        body = f'{row["holds"]}\nMap: #{mapn}.\n'
                    else:
                        body = f'{row["holds"]}\nBeside the map #{mapn}, not a stream of it.\n'
                    parents[ri] = client.create_issue(row["title"], body, map_labels, closed)["number"]
        # 2. tickets: move + strip the Part of line
        for ri, row in enumerate(rows):
            pnum = parents[ri]
            name = row.get("title") or (f"#{row['existing']}" if row["kind"] == "existing" else "no parent")
            same_row = set(row["numbers"])
            for n in row["numbers"]:
                issue = client.get_issue(n)
                if row["kind"] == "root":
                    cur = client.get_parent(n)
                    if cur is not None:
                        # never unlinks: a root with a native parent is named, not written
                        out(f"note: root #{n} has the native parent #{cur}; left as it is")
                elif not ((cur := client.get_parent(n)) is not None and (cur == pnum or cur in same_row)):
                    act(f'move #{n} under {name}')
                    if not dry_run:
                        client.attach(pnum, issue["id"])
                body = issue.get("body") or ""
                new = strip_part_of(body)
                if new != body:
                    act(f'edit body #{n}: drop the Part of line')
                    if not dry_run:
                        client.edit_body(n, new)
        # 3. streams under the map, in the plan's order (after the moves: the map is at the 100 cap)
        for ri, row in enumerate(rows):
            if row["kind"] != "stream":
                continue
            if ri in created:
                if not dry_run:
                    client.attach(mapn, client.get_issue(parents[ri])["id"])
            elif client.get_parent(parents[ri]) != mapn:
                act(f'attach stream "{row["title"]}" to #{mapn}')
                if not dry_run:
                    client.attach(mapn, client.get_issue(parents[ri])["id"])
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--plan", required=True, help="the plan file (JSON: repo, maps, rows); no default")
    ap.add_argument("--repo", help="owner/name (default: the plan's repo)")
    ap.add_argument("--dry-run", action="store_true", help="print the actions, write nothing")
    a = ap.parse_args(argv)
    plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
    lines = run(plan, GhClient(a.repo or plan["repo"]), dry_run=a.dry_run)
    print(f"{len(lines)} action(s){' (dry-run, nothing written)' if a.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
