"""#343 unit 3: build the test map on a GitHub runner and stage it as the ``test-map`` artifact folder.

    python scripts/ci_map_build.py --root . --out <dir> [--work <dir>] [--suite "<command>"]

The map is built the way the PC builds it (``tools.agentops.test_select.build_map``), over the public test copy of
``--root``: the snapshot ``test_copy_check.snapshot_files`` takes (``test_copy_exclude`` and the other exclusions
removed), committed as one commit in ``--work``. So a test the copy does not take is absent from the map, and the PC's
selection treats it as unknown. ``--suite`` defaults to the settings file's ``merge_suite``; the bound is the settings'
``map_build_bound_minutes``.

``--out`` gets exactly: ``testmondata``, ``reads/`` and ``meta.json``. ``meta.json`` carries ``built_at`` (UTC ISO),
``sha`` (git HEAD of ``--root``), ``tests``, ``green``, and the keys ``select_for_diff`` reads: ``map_commit`` (= sha),
``last_green_commit`` (= sha when green, empty when red) and ``total`` (= tests). A red build leaves only ``meta.json``
(``green`` false, ``tests`` 0). Exit 0 on a green build, 1 otherwise. Output goes to stdout; the workflow log keeps it.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]  # the core: its tools/ are the ones that build the map
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.agentops.core_settings import load as load_settings  # noqa: E402
from tools.agentops.map_run import map_suite_cmd, resolve_map_build_bound  # noqa: E402
from tools.agentops.test_copy_check import git_env, load_patterns, snapshot_bytes, snapshot_files  # noqa: E402
from tools.agentops.test_select import MapBuildError, build_map  # noqa: E402

COPY_IDENTITY = ("-c", "user.name=test-map", "-c", "user.email=test-map@localhost", "-c", "commit.gpgsign=false")
GIT_TIMEOUT_MINUTES = 5  # one git call (rev-parse, copy commit) that runs longer is a hang: the build is red


class GitTimeout(RuntimeError):
    """A git call of this script ran past ``GIT_TIMEOUT_MINUTES``: a red build, named by its git subcommand."""


def _git(cwd: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8",
                              check=True, env=git_env(), timeout=GIT_TIMEOUT_MINUTES * 60).stdout.strip()
    except subprocess.TimeoutExpired as exc:
        raise GitTimeout(f"git {args[0]} timed out (over {GIT_TIMEOUT_MINUTES} min)") from exc


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _meta(sha: str, *, green: bool, tests: int = 0) -> dict:
    """The meta.json of one build: the keys the PC's ``select_for_diff`` reads (``map_commit``, ``last_green_commit``,
    ``total``, as ``build_map`` writes them) next to the ones the artifact names (``built_at``, ``sha``, ``tests``,
    ``green``). A red build has no green run of its own, so its ``last_green_commit`` is empty."""
    return {"built_at": _utc_now(), "sha": sha, "tests": tests, "green": green,
            "map_commit": sha, "last_green_commit": sha if green else "", "total": tests}


def public_copy(root: Path, dest: Path) -> str:
    """The public test copy of ``root`` written to ``dest`` as one commit (its own sha, returned). Same files and bytes
    as ``test_copy_send`` takes; the map build needs a git HEAD to check its suite directory against."""
    patterns = load_patterns(root)
    dest.mkdir(parents=True, exist_ok=True)
    for rel in snapshot_files(root):
        if not (root / rel).is_file():
            continue  # tracked but deleted in the working tree
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(snapshot_bytes(root, rel, patterns))
    _git(dest, "init", "-q")
    info = dest / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text("* -text -filter\n", encoding="utf-8")
    _git(dest, "add", "-A", "--force")
    _git(dest, *COPY_IDENTITY, "commit", "-q", "--allow-empty", "-m", "map copy")
    return _git(dest, "rev-parse", "HEAD")


def stage(out: Path, map_dir: Path | None, meta: dict) -> None:
    """``out`` holds exactly the artifact: on a green build the map's ``testmondata`` and ``reads/`` (an empty
    ``reads/`` when the suite wrote none), always ``meta.json``. A stale ``out`` is replaced."""
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    if map_dir is not None:
        shutil.copy2(map_dir / "testmondata", out / "testmondata")
        if (map_dir / "reads").is_dir():
            shutil.copytree(map_dir / "reads", out / "reads")
        else:
            (out / "reads").mkdir()
    (out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the test map on a GitHub runner (#343).")
    parser.add_argument("--root", default=".", help="the checkout to build the map of (default: the current folder)")
    parser.add_argument("--out", required=True, help="the artifact folder to write (replaced)")
    parser.add_argument("--work", default=None, help="a scratch folder for the copy and the map (default: a temp folder)")
    parser.add_argument("--suite", default=None, help="the suite command (default: the settings' merge_suite)")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    out = Path(args.out).resolve()
    work = Path(args.work).resolve() if args.work else Path(
        tempfile.mkdtemp(prefix="ci-map-", dir=os.environ.get("RUNNER_TEMP") or None))
    work.mkdir(parents=True, exist_ok=True)
    sha = ""  # the private checkout's HEAD; empty until git answers
    rc = 1
    try:
        sha = _git(root, "rev-parse", "HEAD")
        suite = args.suite or load_settings()["merge_suite"]
        copy_sha = public_copy(root, work / "copy")
        map_dir = work / "map"
        result = build_map(work / "copy", copy_sha, map_dir, map_suite_cmd(suite), bound=resolve_map_build_bound())
        meta = _meta(sha, green=True, tests=int(result["total"]))
        stage(out, map_dir, meta)
        rc = 0
        print(f"map build green: {meta['tests']} tests at {sha[:12]}, built {meta['built_at']}", flush=True)
    except (MapBuildError, GitTimeout) as exc:
        print(f"map build red: {exc}", flush=True)
    except Exception:  # noqa: BLE001 -- a failed build still writes its meta (green false) and exits non-zero
        traceback.print_exc()
    if rc != 0:  # also a failure after the green line (e.g. in staging): still red, the meta says so
        stage(out, None, _meta(sha, green=False))
        print(f"map build red: meta written with green=false to {out}", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
