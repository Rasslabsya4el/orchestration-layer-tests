"""#231 unit 3 (D119): `python -m tools.agentops test-copy plan [--root PATH] [--reads-dir PATH]`, the run plan of a merge.

A test is PC-only when it reads at least one file the test copy does not take (a file excluded by rule 2, or any
tracked file absent from ``snapshot_files``), or when it lists a folder that holds a tracked file directly inside it
which the copy does not take (its entry: ``<dir>/ (lists excluded: <first excluded file name>)``). The rest run on
GitHub machines. Counts come from the reads data (``reads-*.json``, written by suite_reads_plugin, default dir: the
test map's ``reads/``): ``GitHub: <N>`` is the tests present in the reads data minus M. The plan cannot enumerate the
suite from reads data alone, so a test with no reads data is not counted; the plan says so on its last line.

Prints ``GitHub: <N> tests``, ``PC-only: <M> tests``, one line per PC-only test (up to 50, the rest counted) and
``tests without reads data are not counted``. A missing reads dir prints ``no reads data: <path>`` and exits 0. Exit 1
when the snapshot cannot be read or a reads dump does not parse. Stdlib only.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path, PurePosixPath

from tools.agentops.test_copy_check import CheckError, snapshot_files
from tools.agentops.test_select import map_dir
from tools.agentops.util import no_window_flags

#: the most PC-only lines the plan prints; the rest are counted only.
MAX_LINES = 50
NOT_COUNTED_LINE = "tests without reads data are not counted"


def _tests(count: int) -> str:
    return "test" if count == 1 else "tests"


def tracked_files(root: Path) -> set[str]:
    """Every file ``git ls-files`` lists for ``root``: the snapshot's superset, the excluded files included."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True,
                             creationflags=no_window_flags())
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CheckError(f"not a git repo: {root}") from exc
    return {rel for rel in out.stdout.decode("utf-8", "replace").split("\0") if rel}


def pc_only_tests(reads: dict[str, list[str]], snapshot: set[str],
                  dir_reads: dict[str, list[str]] | None = None,
                  tracked: set[str] = frozenset()) -> dict[str, list[str]]:
    """The tests that need the PC: test id -> its missing entries, sorted. A missing entry is a file path outside
    ``snapshot``, or ``<dir>/ (lists excluded: <name>)`` for a listed folder with a tracked file directly inside it
    that is outside ``snapshot`` (the first such file by path). Clean tests are absent."""
    dir_reads = dir_reads or {}
    children: dict[str, list[str]] = {}
    for rel in tracked:
        children.setdefault(PurePosixPath(rel).parent.as_posix(), []).append(rel)
    pc_only: dict[str, list[str]] = {}
    for test in sorted(set(reads) | set(dir_reads)):
        missing = [path for path in sorted(set(reads.get(test, []))) if path not in snapshot]
        for folder in sorted(set(dir_reads.get(test, []))):
            excluded = sorted(rel for rel in children.get(folder, []) if rel not in snapshot)
            if excluded:
                missing.append(f"{folder}/ (lists excluded: {PurePosixPath(excluded[0]).name})")
        if missing:
            pc_only[test] = sorted(missing)
    return pc_only


def load_reads(reads_dir: Path) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Merge the ``reads-*.json`` dumps of one dir over all workers: (test id -> files it opened, test id -> folders it
    listed). Every test named in a dump is a key of both, even one that opened nothing."""
    files: dict[str, set[str]] = {}
    dirs: dict[str, set[str]] = {}
    for path in sorted(Path(reads_dir).glob("reads-*.json")):
        try:
            dump = json.loads(path.read_text(encoding="utf-8"))
            for test in set(dump["files"]) | set(dump["dirs"]):
                files.setdefault(test, set()).update(dump["files"].get(test, []))
                dirs.setdefault(test, set()).update(dump["dirs"].get(test, []))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise CheckError(f"reads data does not parse: {path} ({type(exc).__name__})") from exc
    return ({test: sorted(paths) for test, paths in files.items()},
            {test: sorted(paths) for test, paths in dirs.items()})


def run_plan(root: Path, reads_dir: Path | None = None) -> int:
    """Print the run plan for ``root``; ``reads_dir`` defaults to the test map's ``reads/`` of ``root``."""
    reads_dir = Path(reads_dir) if reads_dir is not None else map_dir(root) / "reads"
    if not reads_dir.is_dir():
        print(f"no reads data: {reads_dir}")
        return 0
    try:
        reads, dir_reads = load_reads(reads_dir)
        tracked = tracked_files(root)
        snapshot = set(snapshot_files(root))
    except CheckError as exc:
        print(f"test-copy: refused, {exc}")
        return 1
    pc_only = pc_only_tests(reads, snapshot, dir_reads, tracked)
    pc_only_count = len(pc_only)
    github = len(reads) - pc_only_count
    print(f"GitHub: {github} {_tests(github)}")
    print(f"PC-only: {pc_only_count} {_tests(pc_only_count)}")
    for test, entries in list(pc_only.items())[:MAX_LINES]:
        print(f"  {test}: {', '.join(entries)}")
    rest = pc_only_count - MAX_LINES
    if rest > 0:
        print(f"... {rest} more PC-only {_tests(rest)} not shown")
    print(NOT_COUNTED_LINE)
    return 0
