"""#231 (D119): `python -m tools.agentops test-copy check|plan|send`.

`check [--root PATH]` prints how many files a test copy would take, the 'not checked' kinds, the hits per kind and up
to 50 ``file:line kind`` lines (never the matched text); exit 0 with no hit, 1 otherwise (and 1 when the check cannot
run). `plan` prints a merge's run plan (GitHub tests and PC-only tests). `send --branch NAME [--root] [--remote]`
checks, then pushes the snapshot as one parentless commit, or refuses (exit 1).
"""

from __future__ import annotations

from pathlib import Path

from tools.agentops.test_copy_check import CheckError, DEFAULT_ROOT, load_patterns, scan, snapshot_files
from tools.agentops.test_copy_plan import run_plan
from tools.agentops.test_copy_send import send

#: the most hit lines the report prints; the rest are counted only.
MAX_LINES = 50


def add_subparser(sub) -> None:
    tc = sub.add_parser("test-copy", help="the test copy's send check (D119): files that go, hits per kind")
    tc_sub = tc.add_subparsers(dest="test_copy_command", required=True)
    check = tc_sub.add_parser("check", help="scan the files a test copy would take, exit 1 on any hit")
    check.add_argument("--root", type=Path, default=None, help="the repo to check (default: this repo)")
    send_p = tc_sub.add_parser("send", help="check, then push the snapshot as one parentless commit, or refuse")
    send_p.add_argument("--branch", required=True, help="the branch of the test copy (named after the merge)")
    send_p.add_argument("--root", type=Path, default=None, help="the repo to send (default: this repo)")
    send_p.add_argument("--remote", default=None, help="the test copy's remote (default: setting test_copy_remote)")
    plan = tc_sub.add_parser("plan", help="the run plan of a merge: GitHub tests and PC-only tests (D119)")
    plan.add_argument("--root", type=Path, default=None, help="the repo to plan for (default: this repo)")
    plan.add_argument("--reads-dir", type=Path, default=None, help="the reads data dir (default: the test map's reads/)")


def run_test_copy_command(args) -> int:
    root = args.root if args.root is not None else DEFAULT_ROOT
    if args.test_copy_command == "send":
        try:
            return 0 if send(root, args.branch, args.remote).sent else 1
        except CheckError as exc:
            print(f"test-copy: refused, {exc}")
            return 1
    if args.test_copy_command == "plan":
        return run_plan(root, args.reads_dir)
    try:
        patterns = load_patterns(root)
        files = snapshot_files(root)
        hits = scan(root, patterns, files)
    except CheckError as exc:
        print(f"test-copy: refused, {exc}")
        return 1
    counts = {kind: 0 for kind in patterns.compiled}
    for hit in hits:
        counts[hit.kind] += 1
    print(f"test-copy check: {len(files)} files go to the copy")
    for kind in patterns.not_checked:
        print(f"not checked: {kind}")
    print("hits per kind: " + " ".join(f"{kind}={n}" for kind, n in counts.items()))
    for hit in hits[:MAX_LINES]:
        print(f"{hit.file}:{hit.line} {hit.kind}")
    if len(hits) > MAX_LINES:
        print(f"... {len(hits) - MAX_LINES} more hit lines not shown")
    return 1 if hits else 0
