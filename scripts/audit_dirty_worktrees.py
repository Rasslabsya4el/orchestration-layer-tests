#!/usr/bin/env python3
"""Read-only audit of the dirty agent worktrees that `worktree gc` keeps (#1050).

Input is the saved output of a gc DRY RUN (`python -m tools.agentops worktree gc --keep-newer-than-hours 3`,
no apply flag). Its `kept: dirty` rows are the list. Per worktree the script runs read-only git calls only
(`status` with the shared names var/, artifacts/, vendor/ excluded exactly as gc does, `log`, `rev-list`,
`ls-files`), with GIT_OPTIONAL_LOCKS=0 so no index is written, and one `gh issue list` call for the issue
states. Nothing is removed, reset, committed or checked out. Stdlib only.

    python scripts/audit_dirty_worktrees.py --gc-dry-run var/agentops/gc-dry-1050.txt --out var/agentops/worktrees-dirty-1050.tsv
    python scripts/audit_dirty_worktrees.py --gc-dry-run var/agentops/gc-dry-1050.txt --sample 5

Kinds (first rule that fits):
  K1  0 ahead of origin/main, no source edit, every changed path junk (caches, logs, var/, artifacts/, vendor/)
  K2  0 ahead of origin/main, tracked edits present
  K3  commits ahead of origin/main, the issue the name points to is CLOSED
  K4  commits ahead of origin/main, the issue the name points to is OPEN
  K5  anything else (no issue number, status failed or timed out, non-junk untracked only, nothing visible)
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.agentops.core_settings import tickets_repo  # noqa: E402

SHARED = ("var", "artifacts", "vendor")          # junction names: never walked (gc's own pathspec, #1032)
CACHE_PARTS = (".pytest_cache", "__pycache__")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: no console window per child process
GIT_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
STATUS_EXCLUDES = [f":(exclude){name}" for name in SHARED]
ROW_REASON = "kept: dirty"
FOOTER = re.compile(r"kept: dirty (\d+)")
ISSUE_IN_NAME = re.compile(r"(?:\bo|\bissue-?|\borch-)(\d{2,5})")  # wt-o1038-a, issue-179-u6, orch-516-u2
KIND_TEXT = {
    "K1": "0 ahead, only junk changed",
    "K2": "0 ahead, tracked edits",
    "K3": "ahead, issue closed",
    "K4": "ahead, issue open",
    "K5": "anything else",
}


@dataclass
class Row:
    name: str
    path: str
    branch: str
    last_commit: str = ""
    ahead: int | None = None
    issue: str = ""
    issue_state: str = ""
    status: str = ""
    tracked: int = 0
    untracked: int = 0
    dirt_tracked: int = 0      # tracked changes that are not junk
    dirt_untracked: int = 0    # untracked paths that are not junk
    n_pytest: int = 0
    n_pycache: int = 0
    n_log: int = 0
    n_junk_untracked: int = 0
    n_junk_tracked: int = 0
    shared_tracked_index: int = 0
    plain_examples: str = ""
    kind: str = ""
    note: str = ""

    @property
    def dirty(self) -> int:
        return self.dirt_tracked + self.dirt_untracked


def git(args: list[str], cwd: Path, timeout: float = 60.0):
    """(stdout, status). status is 'ok', 'timeout' or 'rc<n>'; stdout is '' unless ok."""
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                              errors="replace", timeout=timeout, env=GIT_ENV, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return "", "timeout"
    if proc.returncode != 0:
        return "", f"rc{proc.returncode}"
    return proc.stdout, "ok"


def main_checkout(cwd: Path) -> Path:
    """The main checkout is the first entry of `git worktree list --porcelain`, whatever worktree we run in."""
    out, status = git(["worktree", "list", "--porcelain"], cwd)
    if status != "ok":
        raise SystemExit(f"git worktree list failed: {status}")
    for line in out.splitlines():
        if line.startswith("worktree "):
            return Path(line[len("worktree "):].strip())
    raise SystemExit("git worktree list printed no entry")


def parse_dry_run(text: str) -> tuple[list[tuple[str, str]], int | None]:
    """`kept: dirty` rows of the gc table: (name, branch). Also the footer count, for the cross-check."""
    rows = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) == 5 and cells[2] == ROW_REASON:
            rows.append((cells[0], cells[1]))
    footers = FOOTER.findall(text)
    footer = int(footers[-1]) if footers else None
    return sorted(rows), footer


def junk_class(path: str) -> dict[str, bool]:
    parts = path.split("/")
    return {
        "pytest": ".pytest_cache" in parts,
        "pycache": "__pycache__" in parts,
        "log": path.endswith(".log"),
        "shared": parts[0] in SHARED,
    }


def is_junk(path: str) -> bool:
    """The one junk rule: every junk class of junk_class, so dirt and K1 never disagree about a path."""
    return any(junk_class(path).values())


def parse_status(out: str) -> tuple[list[str], list[str]]:
    tracked, untracked = [], []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip('"')
        (untracked if code == "??" else tracked).append(path)
    return tracked, untracked


def audit_one(name: str, branch: str, path: Path, issue_states: dict[str, str], timeout: float) -> Row:
    row = Row(name=name, path=str(path), branch=branch)
    m = ISSUE_IN_NAME.search(branch if branch != "(detached)" else name) or ISSUE_IN_NAME.search(name)
    if m:
        row.issue = m.group(1)
        row.issue_state = issue_states.get(row.issue, "unknown")
    out, st = git(["log", "-1", "--format=%cI"], path, timeout)
    row.last_commit = out.strip() if st == "ok" else st
    out, st = git(["rev-list", "--count", "origin/main..HEAD"], path, timeout)
    row.ahead = int(out.strip()) if st == "ok" and out.strip().isdigit() else None
    out, st = git(["ls-files", "--", *SHARED], path, timeout)
    row.shared_tracked_index = len(out.splitlines()) if st == "ok" else -1
    out, st = git(["status", "--porcelain", "--untracked-files=all", "--", ".", *STATUS_EXCLUDES], path, timeout)
    row.status = st
    if st != "ok":
        row.note = f"git status {st}"
        return row
    tracked, untracked = parse_status(out)
    row.tracked, row.untracked = len(tracked), len(untracked)
    row.dirt_tracked = sum(not is_junk(p) for p in tracked)
    row.dirt_untracked = sum(not is_junk(p) for p in untracked)
    plain = []
    for p in untracked:
        c = junk_class(p)
        row.n_pytest += c["pytest"]
        row.n_pycache += c["pycache"]
        row.n_log += c["log"]
        row.n_junk_untracked += any(c.values())
        if not any(c.values()):
            plain.append(p)
    row.plain_examples = "; ".join(plain[:3])
    for p in tracked:
        c = junk_class(p)
        row.n_junk_tracked += any(c.values())
    return row


def classify(row: Row) -> str:
    if row.status != "ok" or row.ahead is None:
        row.note = row.note or "git read failed"
        return "K5"
    if row.ahead == 0:
        if row.dirt_tracked > 0:
            return "K2"
        if row.dirt_untracked == 0 and row.tracked + row.untracked > 0:
            return "K1"
        row.note = row.note or ("nothing visible now" if row.tracked + row.untracked == 0
                                else "non-junk untracked only: " + row.plain_examples)
        return "K5"
    if row.issue_state == "CLOSED":
        return "K3"
    if row.issue_state == "OPEN":
        return "K4"
    row.note = row.note or f"issue state {row.issue_state or 'no issue number'}"
    return "K5"


def load_issue_states() -> tuple[dict[str, str], str]:
    try:
        proc = subprocess.run(["gh", "issue", "list", "--repo", tickets_repo(), "--state", "all", "--limit", "5000",
                               "--json", "number,state"], capture_output=True, text=True,
                              errors="replace", timeout=120, creationflags=NO_WINDOW)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return {}, f"gh failed: {exc}"
    if proc.returncode != 0:
        return {}, f"gh rc{proc.returncode}: {proc.stderr.strip()[:200]}"
    return {str(item["number"]): item["state"] for item in json.loads(proc.stdout)}, "ok"


def write_tsv(path: Path, rows: list[Row]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ["worktree", "path", "branch", "last_commit", "ahead_origin_main", "issue", "issue_state",
              "git_status", "tracked_modified", "untracked", "untracked_junk", "untracked_pytest_cache",
              "untracked_pycache", "untracked_log", "tracked_junk", "shared_names_tracked_in_index",
              "kind", "note"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        writer.writerow(header)
        for r in rows:
            writer.writerow([r.name, r.path, r.branch, r.last_commit, "" if r.ahead is None else r.ahead,
                             r.issue, r.issue_state, r.status, r.tracked, r.untracked, r.n_junk_untracked,
                             r.n_pytest, r.n_pycache, r.n_log, r.n_junk_tracked, r.shared_tracked_index,
                             r.kind, r.note])


def report(rows: list[Row], footer: int | None, issue_note: str, elapsed_min: float) -> None:
    kinds = {k: [r for r in rows if r.kind == k] for k in KIND_TEXT}
    print(f"audited {len(rows)} dirty worktrees in {elapsed_min:.1f} min; issue states: {issue_note}")
    print(f"git status ok {sum(r.status == 'ok' for r in rows)}, timeout {sum(r.status == 'timeout' for r in rows)}")
    print(f"dry-run footer kept: dirty = {footer}")
    print("")
    print("kind | count | meaning")
    for k, text in KIND_TEXT.items():
        print(f"{k} | {len(kinds[k])} | {text}")
    total = sum(len(v) for v in kinds.values())
    print(f"sum | {total} | {'= dry-run count' if total == len(rows) else 'MISMATCH with rows ' + str(len(rows))}")
    print("")
    for k in KIND_TEXT:
        print(f"examples {k}:")
        for r in kinds[k][:3]:
            print(f"  {r.path} | branch {r.branch} | dirty {r.dirty} (tracked {r.dirt_tracked}, untracked {r.dirt_untracked}; junk {r.n_junk_tracked + r.n_junk_untracked})")
    print("")
    print("kind counts: " + " ".join(f"{k}={len(kinds[k])}" for k in KIND_TEXT))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gc-dry-run", required=True, type=Path, help="saved output of the gc dry run")
    ap.add_argument("--out", type=Path, help="TSV file, one row per worktree (omit for stdout summary only)")
    ap.add_argument("--sample", type=int, default=0, help="audit only the first N dirty rows (by name)")
    ap.add_argument("--timeout", type=float, default=60.0, help="seconds per git call")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)

    rows_in, footer = parse_dry_run(args.gc_dry_run.read_text(encoding="utf-8", errors="replace"))
    if args.sample:
        rows_in = rows_in[: args.sample]
    main_path = main_checkout(Path.cwd())
    wt_dir = main_path / ".claude" / "worktrees"
    issue_states, issue_note = load_issue_states()
    started = time.time()
    results: dict[str, Row] = {}
    with cf.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(audit_one, name, branch, wt_dir / name, issue_states, args.timeout): name
                   for name, branch in rows_in}
        for fut in cf.as_completed(futures):
            results[futures[fut]] = fut.result()
    rows = [results[name] for name, _ in rows_in]
    for r in rows:
        r.kind = classify(r)
    elapsed = (time.time() - started) / 60
    report(rows, footer, issue_note, elapsed)
    if args.out:
        write_tsv(args.out, rows)
        print(f"table: {args.out} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
