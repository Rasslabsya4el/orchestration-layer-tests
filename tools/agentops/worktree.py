"""Git worktree plumbing for a worker run: its own branch, shared directories reached through
junctions, and the WIP-commit-on-every-exit-path behaviour of the GPT-era harness (requirement 2,
now removed, #65), ported so `tools/agentops` does not import from `scripts/`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Callable

from . import core_settings
from .gh_call import run_gh
from .util import human_duration, no_window_flags, walk_paths

ROOT = Path(__file__).resolve().parents[2]
SHARED = ("var", "vendor", "artifacts")
STAGE_CHUNK = 50


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def main_checkout_root(root: Path) -> Path:
    """The repo's main checkout for ``root`` (#357): ``root`` itself when its ``.git`` is a
    directory (or missing), else the parent of the common git dir a linked worktree points at.
    Reads the ``.git`` file and ``commondir`` directly -- no subprocess."""
    root = Path(root)
    dotgit = root / ".git"
    try:
        if not dotgit.is_file():
            return root
        text = dotgit.read_text(encoding="utf-8").strip()
        if not text.startswith("gitdir:"):
            return root
        gitdir = Path(text[len("gitdir:"):].strip())
        if not gitdir.is_absolute():
            gitdir = root / gitdir
        common = gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()
        common = common.resolve()
        return common.parent if common.name == ".git" else root
    except OSError:
        return root


def git(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", creationflags=no_window_flags())


def check_junction_target(link: Path, target: Path, worktree: Path) -> None:
    """Refuse a junction whose target lies under the worktree that holds the link, under the link itself,
    or is the link (Zorblax #224: a runtime directory linked inside its own tree made `git status --ignored`
    recurse for minutes and hold the index lock). Also refuses a link that lies under its own target (#360:
    a snapshot under `artifacts/` with a junction `artifacts` back to it loops every recursive walk).
    Raises ValueError naming both paths."""
    def norm(p: Path) -> str:
        return os.path.normcase(os.path.realpath(p))
    t, wt = norm(target), norm(worktree)
    link = Path(link)
    # not realpath(link): a link that already is a junction to a good target must not resolve into it
    lk = os.path.normcase(os.path.join(os.path.realpath(link.parent), link.name))
    if lk.startswith(t.rstrip("\\/") + os.sep):
        raise ValueError(f"junction {link} -> {target} refused: the link lies inside its own target {target} "
                         f"(a loop for every recursive walk)")
    for outer in (wt, lk):
        if t == outer or t.startswith(outer.rstrip("\\/") + os.sep):
            raise ValueError(f"junction {link} -> {target} refused: the target lies inside {outer} "
                             f"(a junction's target must be in the main checkout, not in its own tree)")


def _remove_tracked_only_dir(root: Path, name: str) -> bool:
    """#633: remove the real directory `root/name` when everything under it is tracked by git (a fresh
    checkout of a repo that tracks e.g. `var/cache/wiki/.gitkeep`), so a junction can take its place.
    Returns False, deleting nothing, when any entry is untracked or ignored (`git status --porcelain
    --ignored --untracked-files=all` prints it) or any entry is a link/junction. Deletion is os.walk
    bottom-up with os.remove/os.rmdir: it never follows a link on any Python version (shutil.rmtree only
    stopped following junctions in 3.12), and links were already refused before the first delete."""
    link = root / name
    st = git("status", "--porcelain", "--ignored", "--untracked-files=all", "--", name, cwd=root)
    if st.returncode != 0 or st.stdout.strip():
        return False
    if os.path.islink(link) or os.path.isjunction(link):
        return False
    files, dirs = [], []
    for cur, dirnames, filenames in os.walk(link, followlinks=False):
        for entry in dirnames + filenames:
            p = os.path.join(cur, entry)
            if os.path.islink(p) or os.path.isjunction(p):
                return False
        dirs.append(cur)
        files.extend(os.path.join(cur, f) for f in filenames)
    for f in files:
        os.remove(f)
    for d in reversed(dirs):  # os.walk is top-down: reversed = children before parents
        os.rmdir(d)
    return True


def _ensure_junctions(root: Path, main: Path) -> dict[str, str]:
    """Link `var`/`vendor`/`artifacts` of the worktree `root` to the main checkout `main` (#449).
    Returns {name: "made" | "present"} for each shared directory `main` holds. An existing junction is left
    alone; a real directory, a target inside the worktree, or a failing `mklink` raises SystemExit."""
    root, main = Path(root), Path(main)
    out: dict[str, str] = {}
    for name in SHARED:
        link, target = root / name, main / name
        if not target.is_dir():
            continue
        if os.path.isjunction(link):
            out[name] = "present"
            continue
        if link.exists():
            if not (link.is_dir() and _remove_tracked_only_dir(root, name)):
                raise SystemExit(f"{link} is a real directory, not a junction: refused, never replaced")
        try:
            check_junction_target(link, target, root)
        except ValueError as exc:
            raise SystemExit(str(exc))
        res = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True,
                             text=True, errors="replace", creationflags=no_window_flags())
        if res.returncode != 0 or not os.path.isjunction(link):
            raise SystemExit(f"mklink /J {link} {target} failed (rc {res.returncode}): "
                             f"{(res.stdout + res.stderr).strip()}")
        out[name] = "made"
    return out


def add_junctions_subparser(sub) -> None:
    parser = sub.add_parser("junctions", help="link var/ vendor/ artifacts/ of a worktree to the main checkout (#449)")
    parser.add_argument("worktree", nargs="?", help="the worktree (default: this directory's worktree root)")


def run_junctions_command(args) -> int:
    if args.worktree:
        wt = Path(args.worktree).resolve()
    else:
        top = git("rev-parse", "--show-toplevel", cwd=Path.cwd())
        wt = Path(top.stdout.strip()).resolve() if top.returncode == 0 and top.stdout.strip() else Path.cwd()
    main = main_checkout_root(wt)
    if os.path.normcase(os.path.realpath(main)) == os.path.normcase(os.path.realpath(wt)):
        print(f"{wt} is the main checkout, not a linked worktree: refused")
        return 1
    try:
        result = _ensure_junctions(wt, main)
    except SystemExit as exc:
        print(f"junctions failed: {exc}")
        return 1
    for name, state in result.items():
        print(f"{name}: {state}")
    made = sum(1 for v in result.values() if v == "made")
    print(f"{len(result) - made} present / {made} made")
    return 0


def worktree_path(name: str, main: Path) -> Path:
    """The one place a work folder goes: `<main>/.claude/worktrees/wt-<name>`, inside the project
    (git-ignored there, outside the test collection), never in the project's parent folder."""
    return Path(main) / ".claude" / "worktrees" / f"wt-{name}"


def _copy_shared_draw(wt: Path) -> None:
    """#133: every `mods/<mod>/.gitignore` in `wt` that lists `hooks/draw.ts` gets `mods/<mod>/hooks/draw.ts`,
    a byte copy of the worktree's own `mods/shared/draw.ts` (git-ignored, so the worktree stays clean).
    No `mods/shared/draw.ts` -> nothing copied."""
    mods = wt / "mods"
    source = mods / "shared" / "draw.ts"
    if not source.is_file():
        return
    for gitignore in sorted(mods.glob("*/.gitignore")):
        lines = gitignore.read_text(encoding="utf-8").splitlines()
        if "hooks/draw.ts" not in (line.strip() for line in lines):
            continue
        hooks = gitignore.parent / "hooks"
        hooks.mkdir(exist_ok=True)
        shutil.copyfile(source, hooks / "draw.ts")


def add_sibling_worktree(name: str, base: str, *, main: Path) -> Path:
    """A worktree at `worktree_path(name, main)` on a NEW branch `name` from `base`, shared directories
    linked to `main`, and the mods' hooks/draw.ts copied (#133). Raises SystemExit (one line) before changing
    anything when the branch or directory exists; a bad `base` leaves nothing behind (git makes neither branch
    nor directory)."""
    main = Path(main)
    path = worktree_path(name, main)
    if git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}", cwd=main).returncode == 0:
        raise SystemExit(f"branch {name} already exists: nothing changed")
    if path.exists():
        raise SystemExit(f"directory {path} already exists: nothing changed")
    res = git("worktree", "add", "-b", name, str(path), base, cwd=main)
    if res.returncode != 0:
        lines = res.stderr.strip().splitlines()
        raise SystemExit(lines[-1] if lines else f"git worktree add failed (rc {res.returncode})")
    _ensure_junctions(path, main)
    _copy_shared_draw(path)
    return path


def add_worktree_subparser(sub) -> None:
    parser = sub.add_parser("worktree", help="worktree helpers (#576, gc #1032)")
    ws = parser.add_subparsers(dest="worktree_command", required=True)
    add = ws.add_parser("add", help="new worktree .claude/worktrees/wt-<name> in the project on a new branch <name> from --base")
    add.add_argument("name")
    add.add_argument("--base", required=True, help="branch or commit to start from")
    gc = ws.add_parser("gc", help="remove finished worktrees under .claude/worktrees, link by link (#1032); dry run unless --apply")
    gc.add_argument("--apply", action="store_true", help="really remove (without it: the table only)")
    gc.add_argument("--dirty", action="store_true",
                    help="second pass (#1052): a worktree the first pass keeps for unsaved edits or unmerged commits "
                         "is saved (commits.bundle, tracked.patch, untracked/, head.txt under var/agentops/worktree-save/) "
                         "and removed unless its branch's issue is OPEN; with --apply the first pass runs before it")
    gc.add_argument("--older-than-days", type=float, default=3.0,
                    help="a clean, unmerged worktree whose newest file is older than this is removed (default 3)")
    gc.add_argument("--keep-newer-than-hours", type=float, default=0.0,
                    help="a worktree whose folder was created less than this many hours ago is kept: live, "
                         "merged or not (default 0 = off); a fresh worker's worktree sits at main's head and "
                         "would otherwise count as merged")


def run_worktree_command(args) -> int:
    top = git("rev-parse", "--show-toplevel", cwd=Path.cwd())
    if top.returncode != 0 or not top.stdout.strip():
        print("not inside a git repository")
        return 1
    main = main_checkout_root(Path(top.stdout.strip()).resolve())
    if args.worktree_command == "gc":
        return run_worktree_gc(main, apply=args.apply, older_than_days=args.older_than_days,
                               keep_newer_than_hours=args.keep_newer_than_hours, dirty=args.dirty)
    try:
        path = add_sibling_worktree(args.name, args.base, main=main)
    except SystemExit as exc:
        print(str(exc).splitlines()[0] if str(exc) else "worktree add failed")
        return 1
    print(str(path))
    return 0


GC_REASON_MERGED = "merged"
GC_REASON_OLD = "clean and old"
GC_REASON_DIRTY = "kept: dirty"
GC_REASON_LIVE = "kept: live"
GC_REASON_LOCKED = "kept: locked"
GC_REASON_STANDING = "kept: standing suite folder"  # #1061: the merge-suite and full-run folders, whatever their state
GC_REASON_PRUNABLE = "prunable"
GC_REASON_ORDER = (GC_REASON_MERGED, GC_REASON_OLD, GC_REASON_DIRTY, GC_REASON_LIVE, GC_REASON_LOCKED,
                   GC_REASON_STANDING, GC_REASON_PRUNABLE)
# `git status` must never walk into a shared junction: exclude the three names by pathspec (#1032)
GC_STATUS_EXCLUDES = tuple(f":(exclude){name}" for name in SHARED)


class GcRow:
    """One worktree under `.claude/worktrees` and what `worktree gc` decides for it (#1032)."""

    def __init__(self, path: Path, branch: str | None, reason: str, remove: bool, merged: bool,
                 junctions: list[Path], age_seconds: float | None) -> None:
        self.path = path
        self.branch = branch
        self.reason = reason
        self.remove = remove
        self.merged = merged
        self.junctions = junctions
        self.age_seconds = age_seconds


def _norm_path(path: Path | str) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def list_worktree_entries(main: Path) -> list[dict]:
    """`git worktree list --porcelain` as dicts: path, branch (short name, or None when detached), and the
    flags `prunable` / `locked` when git prints them."""
    res = git("worktree", "list", "--porcelain", cwd=main)
    if res.returncode != 0:
        raise SystemExit(f"git worktree list failed: {res.stderr.strip()}")
    entries: list[dict] = []
    current: dict = {}
    for line in res.stdout.splitlines() + [""]:
        if not line:
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current["path"] = value
        elif key == "branch":
            current["branch"] = value[len("refs/heads/"):] if value.startswith("refs/heads/") else value
        elif key in ("prunable", "locked"):
            current[key] = True
    return entries


def merged_into_main(main: Path) -> set[str]:
    res = git("branch", "--merged", "main", "--format=%(refname:short)", cwd=main)
    if res.returncode != 0:
        raise SystemExit(f"git branch --merged main failed: {res.stderr.strip()}")
    return {line.strip() for line in res.stdout.splitlines() if line.strip()}


def reparse_points(root: Path) -> list[Path]:
    """Every junction or symlink under `root`, the link itself only: `walk_paths` yields a junction and
    never descends into it, so a shared target is never read here."""
    return [p for p in walk_paths(Path(root)) if os.path.isjunction(p) or os.path.islink(p)]


def newest_file_age(root: Path, now: float) -> float:
    """Seconds since the newest regular file under `root` was written; the worktree's own `.git` pointer,
    junctions and directories do not count. An empty tree falls back to its folder's own time."""
    root = Path(root)
    newest: float | None = None
    for p in walk_paths(root):
        if p.name == ".git" and p.parent == root:
            continue
        if os.path.isjunction(p) or os.path.islink(p):
            continue
        try:
            st = os.lstat(p)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode):
            continue
        newest = st.st_mtime if newest is None else max(newest, st.st_mtime)
    if newest is None:
        try:
            newest = os.lstat(root).st_mtime
        except OSError:
            return 0.0
    return max(0.0, now - newest)


def worktree_dirty(root: Path) -> bool:
    """True when `git status` shows any change outside the shared names. A status that cannot be read
    counts as dirty: such a tree is kept."""
    res = git("status", "--porcelain", "--", ".", *GC_STATUS_EXCLUDES, cwd=root)
    return res.returncode != 0 or bool(res.stdout.strip())


def folder_age(root: Path, now: float) -> float:
    """Seconds since the folder itself was created (Windows creation time); 0 when unreadable."""
    try:
        st = os.lstat(root)
    except OSError:
        return 0.0
    born = getattr(st, "st_birthtime", None) or st.st_ctime
    return max(0.0, now - born)


def standing_suite_paths(main: Path) -> set[str]:
    """The normalized paths of the standing suite folders under `<main>/.claude/worktrees` (#1061): the merge
    suite's (`suite_worktree.SUITE_NAME`) and the full run's (`suite_worktree.FULL_WORKTREE`), read from that module.
    Imported here, not at module level: suite_worktree imports this one (a cycle otherwise)."""
    from .suite_worktree import FULL_WORKTREE, SUITE_NAME
    return {_norm_path(Path(main) / ".claude" / "worktrees" / name) for name in (SUITE_NAME, FULL_WORKTREE)}


def plan_worktree_gc(main: Path, *, older_than_days: float, now: float,
                     keep_newer_than_hours: float = 0.0) -> list[GcRow]:
    """Decide for every worktree under `<main>/.claude/worktrees` (never the main checkout, never a
    snapshot): a standing suite folder (merge-suite, full-run) -> kept: standing suite folder, whatever its
    state (#1061); merged into main and clean -> removed; dirty -> kept; clean, unmerged and with a newest
    file older than `older_than_days` -> removed; clean, unmerged and younger -> kept: live (rule 5, the
    conservative offline form of 'a live worker'). Nothing is removed or written here."""
    main = Path(main)
    base = _norm_path(main / ".claude" / "worktrees") + os.sep
    merged = merged_into_main(main)
    standing = standing_suite_paths(main)
    rows: list[GcRow] = []
    for entry in sorted(list_worktree_entries(main), key=lambda e: e.get("path", "")):
        path = Path(entry.get("path", ""))
        if not _norm_path(path).startswith(base):
            continue
        branch = entry.get("branch")
        is_merged = branch is not None and branch in merged
        if _norm_path(path) in standing:
            junctions = reparse_points(path) if path.is_dir() else []
            rows.append(GcRow(path, branch, GC_REASON_STANDING, False, is_merged, junctions, None))
            continue
        if entry.get("prunable") or not path.is_dir():
            rows.append(GcRow(path, branch, GC_REASON_PRUNABLE, False, is_merged, [], None))
            continue
        junctions = reparse_points(path)
        if entry.get("locked"):
            rows.append(GcRow(path, branch, GC_REASON_LOCKED, False, is_merged, junctions, None))
            continue
        if worktree_dirty(path):
            rows.append(GcRow(path, branch, GC_REASON_DIRTY, False, is_merged, junctions, None))
            continue
        if keep_newer_than_hours > 0 and folder_age(path, now) < keep_newer_than_hours * 3600:
            rows.append(GcRow(path, branch, GC_REASON_LIVE, False, is_merged, junctions, folder_age(path, now)))
            continue
        if is_merged:
            rows.append(GcRow(path, branch, GC_REASON_MERGED, True, True, junctions, None))
            continue
        age = newest_file_age(path, now)
        if age > older_than_days * 86400:
            rows.append(GcRow(path, branch, GC_REASON_OLD, True, False, junctions, age))
        else:
            rows.append(GcRow(path, branch, GC_REASON_LIVE, False, False, junctions, age))
    return rows


def format_worktree_gc_table(rows: list[GcRow]) -> str:
    header = ("worktree", "branch", "reason", "junctions", "newest file")
    body = [(r.path.name, r.branch or "(detached)", r.reason, str(len(r.junctions)),
             human_duration(r.age_seconds) if r.age_seconds is not None else "-") for r in rows]
    widths = [max(len(row[i]) for row in [header, *body]) for i in range(len(header))]
    lines = [" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(header)),
             "-+-".join("-" * w for w in widths)]
    lines += [" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in body]
    counts = {reason: sum(1 for r in rows if r.reason == reason) for reason in GC_REASON_ORDER}
    lines.append("")
    lines.append(f"{len(rows)} worktrees: " + ", ".join(f"{reason} {counts[reason]}" for reason in GC_REASON_ORDER))
    lines.append(f"junctions in the worktrees to remove: {sum(len(r.junctions) for r in rows if r.remove)}")
    return "\n".join(lines)


def remove_junction(link: Path) -> None:
    """`cmd /c rmdir <link>` without /S: removes the link only, never the folder it points to (#1032).
    The caller checks the result by re-listing the reparse points, not by this return value."""
    subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True, text=True, errors="replace",
                   creationflags=no_window_flags())


def gc_summary(removed: int, links: int, deleted_branches: int, skipped: int) -> str:
    return (f"removed {removed} worktree(s), {links} junction link(s), deleted {deleted_branches} branch(es), "
            f"skipped {skipped}")


def apply_worktree_gc(main: Path, rows: list[GcRow], *, force: bool = False, save_skipped: int = 0) -> int:
    """Per removable row: every junction through `remove_junction`, re-list (stop the whole run with exit 2
    on any survivor), `git worktree remove` without --force (with --force only from the #1052 pass, whose
    rows were saved first), `git branch -d` only for merged branches. Ends with `git worktree prune` (rule 4).
    The summary's `skipped` is `save_skipped` (the failed saves of `apply_dirty_gc`) plus the failed removes (#1056)."""
    main = Path(main)
    removed = deleted_branches = links = 0
    skipped = save_skipped
    for row in rows:
        if not row.remove:
            continue
        if not row.path.is_dir():
            # a worktree nested inside one removed earlier in this run: its folder went with the parent
            print(f"gone already {row.path.name}: its folder was inside a removed worktree; the prune at the end drops its record")
            continue
        for link in row.junctions:
            remove_junction(link)
        left = reparse_points(row.path) if row.path.exists() else []
        if left:
            print(f"STOP: {len(left)} junction(s) survive in {row.path} (first: {left[0]}); "
                  "the run stops here, exit 2, nothing else removed")
            return 2
        links += len(row.junctions)
        # A file tracked under a shared name (an old worktree holds `var/cache/...`) shows as deleted once the
        # link is gone; restore it into the now ordinary folder, per name (a name with no tracked file errors).
        for name in SHARED:
            git("checkout", "--", name, cwd=row.path)
        res = git("worktree", "remove", *(["--force"] if force else []), str(row.path), cwd=main)
        if res.returncode != 0:
            last = (res.stderr.strip().splitlines() or ["git worktree remove failed"])[-1]
            print(f"SKIP: git worktree remove {row.path} failed: {last}; its links are already removed, the run goes on")
            skipped += 1
            continue
        removed += 1
        print(f"removed {row.path.name}: {row.reason}, {len(row.junctions)} junction(s)")
        if row.merged and row.branch:
            dropped = git("branch", "-d", row.branch, cwd=main)
            if dropped.returncode == 0:
                deleted_branches += 1
            else:
                print(f"branch {row.branch} kept: git branch -d refused")
    git("worktree", "prune", cwd=main)
    print(gc_summary(removed, links, deleted_branches, skipped))
    return 0


def run_worktree_gc(main: Path, *, apply: bool, older_than_days: float, keep_newer_than_hours: float = 0.0,
                    dirty: bool = False, issue_state: Callable[[int], str] | None = None) -> int:
    now = time.time()
    rows = plan_worktree_gc(main, older_than_days=older_than_days, now=now,
                            keep_newer_than_hours=keep_newer_than_hours)
    print(format_worktree_gc_table(rows))
    if not dirty:
        if not apply:
            print("dry run: nothing removed or written (add --apply to remove)")
            return 0
        return apply_worktree_gc(main, rows)
    lookup = issue_state or (lambda number: gh_issue_state(number, main))
    if apply:
        rc = apply_worktree_gc(main, rows)
        if rc != 0:
            return rc
        rows = plan_worktree_gc(main, older_than_days=older_than_days, now=time.time(),
                                keep_newer_than_hours=keep_newer_than_hours)
    dirty_rows = plan_dirty_gc(rows, issue_state=lookup, keep_newer_than_hours=keep_newer_than_hours,
                               now=time.time())
    print(format_dirty_gc_table(dirty_rows))
    if not apply:
        print("dry run: nothing removed or written (add --apply to save and remove)")
        return 0
    return apply_dirty_gc(main, dirty_rows)


# --- #1052: the second pass over the worktrees the first pass keeps for unsaved edits or unmerged commits ---

GC_REASON_SAVED = "saved: dirty"
SAVE_ROOT_PARTS = ("var", "agentops", "worktree-save")
SAVE_JUNK_PARTS = (".pytest_cache", "__pycache__")
SAVE_STEP_NAMES = ("bundle", "patch", "untracked", "head")


def is_save_junk(path: str) -> bool:
    """The junk notion of the read-only audit (scripts/audit_dirty_worktrees.py `junk_class`), copied here so
    `tools/agentops` imports nothing from `scripts/` (#1052): caches, logs, and the shared names."""
    parts = path.split("/")
    return any(part in SAVE_JUNK_PARTS for part in parts) or path.endswith(".log") or parts[0] in SHARED


def is_nested_git(path: str) -> bool:
    """Nested repository content, never saved (#1056): a path part `.git` or ending in `.git` (`x/origin.git/HEAD`
    of a bare repo), or a directory git lists with a trailing `/` because it holds its own `.git` (`x/nested/`)."""
    return path.endswith("/") or any(part == ".git" or part.endswith(".git") for part in path.split("/"))


BRANCH_DATE = re.compile(r"(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{8})(?!\d)")
BRANCH_ISSUE_PART = re.compile(r"(?:o(\d+)|(\d+))[a-z0-9]*")


def branch_issue(branch: str | None) -> int | None:
    """The issue a branch names (#1056): the first part of the name, split at `-`, `_` or `/`, that starts with a
    digit run (or `o` then digits) and may go on with a unit or variant tag (letters and digits: `b`, `u1`,
    `fix`, `u1b`). `issue-246`, `945-cards`, `wt-861-u2`, `restore-1052-bundle`, `orch-203b`, `orch-724u1`,
    `research/168-x` -> 246, 945, 861, 1052, 203, 724, 168; `o1052`, `o1051b`, `o958-film` -> 1052, 1051, 958.
    Dates (`2026-09-22-smoke`, `20260922`) are removed first; a letter-then-digits part (`bat3`) and an agent
    hash (`agent-a0018ec...`) name no ticket. None = no ticket: the cleanup then follows its other rules
    (dirty -> save+remove), it never reads that as an unknown state."""
    name = BRANCH_DATE.sub("", branch or "")
    for part in re.split(r"[-_/]", name):
        match = BRANCH_ISSUE_PART.fullmatch(part)
        if match:
            return int(match.group(1) or match.group(2))
    return None


def gh_issue_state(number: int, main: Path) -> str:
    """`OPEN` / `CLOSED` of issue N through `gh issue view`; "" when gh cannot tell (the caller then keeps the row)."""
    try:
        res = run_gh(["issue", "view", str(number), "--json", "state", "-R", core_settings.tickets_repo()],
                     cwd=main, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    if res.returncode != 0:
        return ""
    try:
        return str(json.loads(res.stdout).get("state", "")).upper()
    except (ValueError, AttributeError):
        return ""


def _status_records(worktree: Path) -> list[tuple[str, str]] | None:
    """(state, path) for every change outside the shared names, from `git status -z` (`??` = untracked).
    None when git cannot read the status."""
    res = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", ".",
                          *GC_STATUS_EXCLUDES], cwd=str(worktree), capture_output=True,
                         creationflags=no_window_flags())
    if res.returncode != 0:
        return None
    records = res.stdout.split(b"\0")
    out: list[tuple[str, str]] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        state = record[:2].decode("ascii", "replace")
        out.append((state, os.fsdecode(record[3:])))
        if "R" in state or "C" in state:
            index += 1  # a rename or copy: its origin path follows as a record of its own
    return out


class DirtyRow:
    """One worktree the second pass looked at: the first-pass row, the decision and the counts for the table."""

    def __init__(self, row: GcRow, decision: str, remove: bool, ahead: int | None,
                 tracked: int | None, untracked: int | None) -> None:
        self.row = row
        self.decision = decision
        self.remove = remove
        self.ahead = ahead
        self.tracked = tracked
        self.untracked = untracked


def plan_dirty_gc(rows: list[GcRow], *, issue_state: Callable[[int], str], keep_newer_than_hours: float,
                  now: float) -> list[DirtyRow]:
    """The second pass's decisions, read only. A row is looked at when the first pass kept it dirty, or kept
    it as unmerged (its commits are not in main), or kept it as a standing suite folder (#1061); every other
    row is the first pass's business and is not listed. A standing suite folder is `kept: standing suite folder`
    whatever its state. Decision: a folder younger than `keep_newer_than_hours` is `kept: live`; a branch whose issue is
    OPEN is `kept: open issue #N`; an issue gh cannot tell is `kept: issue #N state unknown`; any other
    branch (its issue CLOSED, or no issue number at all) is `save+remove`."""
    cache: dict[int, str] = {}
    out: list[DirtyRow] = []
    for row in rows:
        if row.reason == GC_REASON_STANDING:
            out.append(DirtyRow(row, GC_REASON_STANDING, False, None, None, None))
            continue
        if not (row.reason == GC_REASON_DIRTY or (row.reason == GC_REASON_LIVE and not row.merged)):
            continue
        records = _status_records(row.path)
        tracked = None if records is None else sum(1 for state, _ in records if state != "??")
        untracked = None if records is None else sum(1 for state, _ in records if state == "??")
        ahead = branch_commit_count(row.path)
        if keep_newer_than_hours > 0 and folder_age(row.path, now) < keep_newer_than_hours * 3600:
            out.append(DirtyRow(row, "kept: live", False, ahead, tracked, untracked))
            continue
        number = branch_issue(row.branch)
        if number is None:
            out.append(DirtyRow(row, "save+remove", True, ahead, tracked, untracked))
            continue
        if number not in cache:
            cache[number] = issue_state(number) or ""
        state = cache[number]
        if state == "OPEN":
            decision, remove = f"kept: open issue #{number}", False
        elif state:
            decision, remove = "save+remove", True
        else:
            decision, remove = f"kept: issue #{number} state unknown", False
        out.append(DirtyRow(row, decision, remove, ahead, tracked, untracked))
    return out


def format_dirty_gc_table(rows: list[DirtyRow]) -> str:
    header = ("worktree", "branch", "decision", "commits ahead", "tracked edits", "untracked")
    body = [(d.row.path.name, d.row.branch or "(detached)", d.decision,
             "?" if d.ahead is None else str(d.ahead),
             "?" if d.tracked is None else str(d.tracked),
             "?" if d.untracked is None else str(d.untracked)) for d in rows]
    widths = [max(len(row[i]) for row in [header, *body]) for i in range(len(header))]
    lines = [" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(header)),
             "-+-".join("-" * w for w in widths)]
    lines += [" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in body]
    removing = sum(1 for d in rows if d.remove)
    lines.append("")
    lines.append(f"{len(rows)} dirty worktrees: save+remove {removing}, kept {len(rows) - removing}")
    lines.append(f"junctions in the worktrees to remove: {sum(len(d.row.junctions) for d in rows if d.remove)}")
    return "\n".join(lines)


def _save_worktree(row: GcRow, folder: Path) -> str | None:
    """Writes the save folder of one worktree: `commits.bundle` (commits not in main, verified), `tracked.patch`
    (tracked edits, checked by `git apply --check --reverse` in the worktree: the saved patch is exactly the
    live edits; when `git diff` prints no hunk the tracked edits are line endings only (#1060): no patch is
    written and the line says "tracked: line endings only"), `untracked/` (every non-junk untracked file, count and bytes checked after the copy),
    `head.txt` (branch and HEAD sha). Nested git repositories are not saved and the line says so (#1056); the
    count and bytes are checked over what was copied. Returns None when every step passed, else the failing
    step's name.
    A final folder that already exists is never written into (`folder`). The steps write into `<folder>.partial`
    (#1063), renamed to `folder` only when every step passed; a failing step leaves the partial folder, and the next
    call writes over it in place (nothing is ever deleted)."""
    if folder.exists():
        return "folder"
    partial = folder.with_name(folder.name + ".partial")
    step = _save_worktree_steps(row, partial)
    if step is not None:
        return step
    try:
        if folder.exists():
            return "folder"
        os.replace(partial, folder)
    except OSError:
        return "folder"
    return None


def _save_worktree_steps(row: GcRow, folder: Path) -> str | None:
    """The steps of `_save_worktree`, written into `folder` (the partial folder); an existing one is reused."""
    wt = Path(row.path)
    records = _status_records(wt)
    if records is None:
        return "untracked"
    folder.mkdir(parents=True, exist_ok=True)
    ahead = branch_commit_count(wt)
    if ahead > 0:
        base = branch_base(wt)
        bundle = folder / "commits.bundle"
        revision = f"{base}..HEAD" if base else "HEAD"
        made = git("bundle", "create", str(bundle), revision, cwd=wt)
        if made.returncode != 0 or git("bundle", "verify", str(bundle), cwd=wt).returncode != 0:
            return "bundle"
    tracked = [path for state, path in records if state != "??"]
    if tracked:
        patch = folder / "tracked.patch"
        diff = subprocess.run(["git", "diff", "--binary", "HEAD", "--", ".", *GC_STATUS_EXCLUDES], cwd=str(wt),
                              capture_output=True, creationflags=no_window_flags())
        if diff.returncode != 0:
            return "patch"
        if not diff.stdout:
            # the tracked edits print no hunk (CRLF-only under core.autocrlf=true, #1060): nothing to save as a patch
            print(f"tracked: line endings only, no patch saved: {', '.join(tracked)}")
        else:
            patch.write_bytes(diff.stdout)
            check = subprocess.run(["git", "apply", "--check", "--reverse", str(patch)], cwd=str(wt),
                                   capture_output=True, creationflags=no_window_flags())
            if check.returncode != 0:
                return "patch"
    found = [path for state, path in records if state == "??" and not is_save_junk(path)]
    nested = [path for path in found if is_nested_git(path)]
    untracked = [path for path in found if not is_nested_git(path)]
    if nested:
        print(f"untracked: nested git skipped, not saved: {', '.join(nested)}")
    if untracked:
        dest = folder / "untracked"
        sources = [wt / path for path in untracked]
        try:
            for src in sources:
                if os.path.islink(src) or os.path.isjunction(src):
                    return "untracked"  # a link is never followed into the save
            before = (len(sources), sum(os.path.getsize(src) for src in sources))
            for src, rel in zip(sources, untracked):
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
            copied = [dest / rel for rel in untracked]  # this run's copies only: a leftover file of an earlier partial run may stay
            after = (len(copied), sum(os.path.getsize(p) for p in copied))
        except OSError:
            return "untracked"
        if before != after:
            return "untracked"
    sha = head_commit(wt)
    if not sha:
        return "head"
    try:
        (folder / "head.txt").write_text(f"branch: {row.branch or '(detached)'}\nhead: {sha}\n", encoding="utf-8")
    except OSError:
        return "head"
    return None


def apply_dirty_gc(main: Path, dirty_rows: list[DirtyRow]) -> int:
    """Per row marked save+remove: save first (`_save_worktree`); a failed save keeps the worktree and prints
    `SKIP: <name>: save failed (<step>)`. The rows whose saves all passed go through `apply_worktree_gc` with
    --force (link by link first, the same path as the first pass)."""
    main = Path(main)
    save_root = main.joinpath(*SAVE_ROOT_PARTS)
    ready: list[GcRow] = []
    save_skipped = 0
    for dirty in dirty_rows:
        if not dirty.remove:
            continue
        name = dirty.row.path.name
        which = _save_worktree(dirty.row, save_root / name)
        if which is not None:
            print(f"SKIP: {name}: save failed ({which})")
            save_skipped += 1
            continue
        print(f"saved {name} to {save_root / name}")
        ready.append(GcRow(dirty.row.path, dirty.row.branch, GC_REASON_SAVED, True, dirty.row.merged,
                           dirty.row.junctions, None))
    if not ready:
        print("nothing saved and removed")
        print(gc_summary(0, 0, 0, save_skipped))
        return 0
    return apply_worktree_gc(main, ready, force=True, save_skipped=save_skipped)


def make_worktree(branch: str, *, root: Path = ROOT, base: str = "HEAD") -> Path:
    """A fresh worktree at `.claude/worktrees/<branch>`, `var`/`vendor`/`artifacts` linked in
    through directory junctions so the worker shares the shared runtime and run outputs
    without ever holding a real copy of them (CLAUDE.md "Worktrees hold junctions")."""
    path = root / ".claude" / "worktrees" / branch
    res = git("worktree", "add", "-b", branch, str(path), base, cwd=root)
    if res.returncode != 0:
        raise SystemExit(f"worktree failed: {res.stderr.strip()}")
    _ensure_junctions(path, root)
    return path


def head_commit(worktree: Path) -> str:
    result = git("rev-parse", "HEAD", cwd=worktree)
    return result.stdout.strip() if result.returncode == 0 else ""


def branch_base(worktree: Path) -> str:
    for ref in ("main", "origin/main"):
        if git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=worktree).returncode == 0:
            return ref
    return ""


def branch_commit_count(worktree: Path) -> int:
    base = branch_base(worktree)
    revision = f"{base}..HEAD" if base else "HEAD"
    result = git("rev-list", "--count", revision, cwd=worktree)
    try:
        return int(result.stdout.strip()) if result.returncode == 0 else 0
    except ValueError:
        return 0


def _is_shared_path(path: bytes) -> bool:
    return path.replace(b"\\", b"/").split(b"/", 1)[0] in {name.encode() for name in SHARED}


def _changed_paths(worktree: Path) -> list[bytes] | None:
    """Every changed non-junction path from `git status --porcelain=v1 -z`."""
    status = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                            cwd=str(worktree), capture_output=True, creationflags=no_window_flags())
    if status.returncode != 0:
        return None
    paths: list[bytes] = []
    records = status.stdout.split(b"\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record or len(record) < 4:
            continue
        state, path = record[:2], record[3:]
        record_paths = [path]
        if (b"R" in state or b"C" in state) and index < len(records):
            record_paths.append(records[index])
            index += 1
        for changed_path in record_paths:
            if _is_shared_path(changed_path):
                continue
            if changed_path not in paths:
                paths.append(changed_path)
    return paths


def commit_wip(worktree: Path, slug: str, reason: str, elapsed_seconds: float) -> tuple[bool, str]:
    """Commit only changed worktree files, never paths reached through a shared junction
    (ported from the GPT-era harness's `commit_wip`, now removed (#65), same behaviour: this is
    the harness's WIP-commit-on-every-exit-path guarantee, requirement 2)."""
    try:
        raw_paths = _changed_paths(worktree)
        if raw_paths is None:
            return False, "could not read git status"
        if not raw_paths:
            return False, "clean tree"
        paths = [os.fsdecode(path) for path in raw_paths]
        unstaged = git("restore", "--staged", "--", ".", cwd=worktree)
        if unstaged.returncode != 0:
            return False, f"could not clear staging area: {unstaged.stderr.strip()}"
        failed: list[str] = []
        for start in range(0, len(paths), STAGE_CHUNK):
            chunk = paths[start:start + STAGE_CHUNK]
            if git("add", "--", *chunk, cwd=worktree).returncode != 0:
                failed.extend(chunk)
        note = f"; could not stage: {', '.join(failed)}" if failed else ""
        staged = git("diff", "--cached", "--quiet", cwd=worktree)
        if staged.returncode == 0:
            return False, f"no stageable changes{note}"
        if staged.returncode != 1:
            return False, f"could not inspect staged changes: {staged.stderr.strip()}{note}"
        message = (f"WIP (agentops): {slug} ended by {reason} after {human_duration(elapsed_seconds)}; "
                  "the worker's uncommitted tree")
        committed = git("commit", "-m", message, cwd=worktree)
        if committed.returncode != 0:
            return False, f"commit failed: {committed.stderr.strip() or committed.stdout.strip()}{note}"
        return True, f"{head_commit(worktree)[:12]}{note}"
    except Exception as error:  # a failed WIP attempt must be reported, never mask the worker's result
        return False, f"commit failed: {error}"
