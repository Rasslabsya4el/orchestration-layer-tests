"""Test selection for a merge (#690): which tests a diff can affect, from the test-dependency map.

Map dir (default ``var/agentops/test_map/``): ``testmondata`` (testmon 2.2.0 sqlite), ``reads/reads-*.json``
(audit-hook dumps of the files and directories each test opened), ``meta.json``.
Pure functions here; the pytest audit-hook plugin still lives in scripts/test_selection_replay.py.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .core_format import human_duration
from .util import nested_env, no_window_flags

COLLECT_KEY = "<collect>"
MAP_REL = Path("var") / "agentops" / "test_map"
TEST_FILE = re.compile(r"tests/test_[^/]*\.py")
#: #950: the no-window guard reads every walked module as text (ast), which neither the testmon map nor the
#: audit-hook read map records; so any change under tools/agentops/ runs it, whatever the map says.
NO_WINDOW_GUARD = "tests/test_agentops_no_window.py"
#: #959: a monitor pane (``mods/<mod>/``) keeps its own pytest files in ``bin/``; the merge suite never ran them,
#: so the map has no row for them. A diff with any file under a mod selects that mod's ``bin/test_*.py``.
MOD_FILE = re.compile(r"mods/[^/]+/.+")
MOD_BIN_TEST = re.compile(r"mods/[^/]+/bin/test_[^/]*\.py")
#: #1118: test files whose ids are parametrized from repo content (docs), so an id recorded in the map vanishes
#: when that content changes on main (pytest: ``not found``, read by the merge as a red suite). A selected
#: member runs whole by its bare path, never by recorded id.
CONTENT_PARAMETRIZED_TESTS = frozenset({"tests/test_agent_docs_refs.py"})
#: #41 rule 4: a change to a file every test imports runs the full suite, whatever the map says (checked first).
SHARED_IMPORTS = frozenset({"tests/conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg",
                            "tools/agentops/test_select.py"})
#: #41 rule 2, the fourth allowed full-suite reason: files tests read through open(), which no testmon map sees:
#: ``config/``, the core manifest and the rule documents (tests grep rule lines in them, so none may select 0 tests).
DATA_CHANGE_FILES = frozenset({"tools/agentops/core_manifest.txt", "CLAUDE.md", "GLOSSARY.md"})
DATA_CHANGE_PREFIXES = ("config/", "docs/agents/", ".claude/agents/", ".claude/skills/")
#: #217: a test file whose subject is the core check, told by its name (leak words, imports, manifest, avoid words):
#: the only tests a changed core-manifest entry adds. A new such file follows the name, not a list.
CORE_CHECK_TEST = re.compile(r"tests/test_[^/]*(core_check|manifest|leak|avoid_words)[^/]*\.py")


def classify_file(path: str) -> str:
    """``code`` (a .py file: the testmon map sees it), ``data`` (config/, src/**/*.json,
    tests/fixtures/, or any other non-.py file under src/tests/config/tools/schemas: read by tests
    through open(), seen by the audit-hook map), ``docs`` (everything else not python: docs/, *.md,
    repo root files; tests reading them are still found by the audit-hook map, same as data)."""
    p = PurePosixPath(path.replace("\\", "/"))
    if p.suffix == ".py":
        return "code"
    top = p.parts[0] if p.parts else ""
    if top == "config" or "fixtures" in p.parts[:3] and top == "tests":
        return "data"
    if top == "src" and p.suffix == ".json":
        return "data"
    if top in {"src", "tests", "tools", "schemas", "scripts", "reference"}:
        return "data"
    return "docs"


def _is_root_md(path: str) -> bool:
    """A Markdown file at the repository root (``GLOSSARY.md``, ``CLAUDE.md``), no folder in its path."""
    p = PurePosixPath(path.replace("\\", "/"))
    return len(p.parts) == 1 and p.suffix == ".md"


def select_tests(changed: list[str], code_deps: dict[str, set[str]], read_deps: dict[str, set[str]],
                 dir_reads: dict[str, set[str]] | None = None) -> tuple[set[str], list[str]]:
    """Tests selected for ``changed`` files. ``code_deps``: file -> tests that executed it (testmon);
    ``read_deps``: file -> tests that opened it; ``dir_reads``: directory -> tests that listed it
    (a new/removed file under it counts). Returns ``(tests, blind_files)``: a non-python file that
    NO test opened (and no listed directory covers) is "blind" -- the map has no way to say who
    reads it (read in a subprocess, or not read by the merge suite at all)."""
    dir_reads = dir_reads or {}
    sel: set[str] = set()
    blind: list[str] = []
    for f in changed:
        kind = classify_file(f)
        hit = False
        if kind == "code":
            if f in code_deps:
                sel |= code_deps[f]
                hit = True
            # a .py nobody executed is not blind: no test ran it, nothing selects
            continue
        if f in read_deps:
            sel |= read_deps[f]
            hit = True
        parent = str(PurePosixPath(f).parent)
        while True:
            if parent in dir_reads:
                sel |= dir_reads[parent]
                hit = True
            if parent in ("", "."):
                break
            parent = str(PurePosixPath(parent).parent)
        if not hit:
            blind.append(f)
    return sel, blind


def load_reads(reads_dir: str) -> tuple[dict, dict, set]:
    """Merge the per-worker read dumps -> (file -> tests, dir -> tests, set of tests seen)."""
    files: dict[str, set[str]] = {}
    dirs: dict[str, set[str]] = {}
    for p in Path(reads_dir).glob("reads-*.json"):
        d = json.loads(p.read_text(encoding="utf-8"))
        for t, fl in d["files"].items():
            for f in fl:
                files.setdefault(f, set()).add(t)
        for t, dl in d["dirs"].items():
            for f in dl:
                dirs.setdefault(f, set()).add(t)
    return files, dirs


def load_testmon(db_path: str) -> tuple[dict[str, set[str]], set[str]]:
    """testmon database -> (python file -> tests that depend on it, all tests)."""
    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()
        tables = {r[0] for r in cur.execute("select name from sqlite_master where type='table'")}
        deps: dict[str, set[str]] = {}
        tests: set[str] = set()
        if {"test_execution", "test_execution_file_fp", "file_fp"} <= tables:
            q = ("select te.test_name, f.filename from test_execution te "
                 "join test_execution_file_fp t on t.test_execution_id = te.id "
                 "join file_fp f on f.id = t.fingerprint_id")
            for name, fn in cur.execute(q):
                deps.setdefault(fn.replace("\\", "/"), set()).add(name)
                tests.add(name)
            for (name,) in cur.execute("select test_name from test_execution"):
                tests.add(name)
        else:
            raise RuntimeError(f"unknown testmon schema: {sorted(tables)}")
        return deps, tests
    finally:
        con.close()


def git(root: str, *args: str) -> str:
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=True,
                          encoding="utf-8", creationflags=no_window_flags()).stdout


_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def (test_\w+)", re.M)


def count_new_tests(root: str, commit: str, parent: str) -> int:
    """Test functions in ADDED tests/test_*.py files (a new file has no map entry; its tests always run).
    Counted by ``def test_`` -- parametrize is not expanded, so a lower bound."""
    n = 0
    for line in git(root, "diff", "--name-status", "--diff-filter=A", parent, commit).splitlines():
        f = line.split("	", 1)[1]
        if re.fullmatch(r"tests/test_[^/]*\.py", f):
            n += len(_TEST_DEF.findall(git(root, "show", f"{commit}:{f}")))
    return n


# ---------------------------------------------------------------------------------------------
# map dir

def map_dir(root) -> Path:
    return Path(root) / MAP_REL


def read_meta(mdir) -> dict | None:
    """``meta.json`` of the map dir; None when absent or corrupt."""
    try:
        d = json.loads((Path(mdir) / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def write_meta(mdir, meta: dict) -> None:
    """Write ``meta.json`` ({map_commit, total, built_at, last_green_commit}) via temp file + replace."""
    mdir = Path(mdir)
    mdir.mkdir(parents=True, exist_ok=True)
    tmp = mdir / f"meta.json.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(meta), encoding="utf-8")
    os.replace(tmp, mdir / "meta.json")


class MapBuildError(RuntimeError):
    """A map build that wrote nothing: the suite was red, or the suite directory is not at the sha. ``output`` is the
    file the build's full output was saved to (#195), None when the build failed before the suite ran."""

    def __init__(self, message: str, output: Path | None = None):
        super().__init__(message)
        self.output = output


DEFAULT_MAP_SUITE = "python -m pytest tests -q"
READS_PLUGIN = "tools.agentops.suite_reads_plugin"  # #195: records, per test, the repo files it opened
BUILD_OUTPUT_GLOB = "build-*.out"  # #195: a build's full output, ``build-<sha12>-<hex>.out`` under the map dir
BUILD_OUTPUT_KEEP = 20  # the newest this many are kept; older ones are deleted when a build writes its file


def bound_text(seconds: float) -> str:
    """A map build's bound as a human reads it (#201): whole hours as ``N h`` (120 min -> ``2 h``), else ``human_duration``."""
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{int(seconds // 3600)} h"
    return human_duration(seconds)


def _run_bounded(cmd: str, *, cwd: str, env: dict, bound: float | None) -> subprocess.CompletedProcess:
    """``subprocess.run`` of a shell command with a wall-clock bound (seconds, None = no bound). Past the bound the
    whole process tree is stopped (``taskkill /T /F`` through the merge's ``_kill_tree``: on Windows ``shell=True``
    means a timeout alone kills only the shell, and the suite's child lives on) and ``MapBuildError`` with the text
    ``over the <bound> bound`` is raised."""
    proc = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", creationflags=no_window_flags())
    try:
        out, err = proc.communicate(timeout=bound)
    except subprocess.TimeoutExpired:
        from .merge import _kill_tree  # the merge suite's own tree stop (line_check does the same lazy import)
        _kill_tree(proc.pid)
        try:
            proc.communicate(timeout=30)  # the tree is gone, so the pipes close; drain them
        except subprocess.TimeoutExpired:
            pass
        raise MapBuildError(f"over the {bound_text(bound)} bound") from None
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def build_map(root, sha: str, mdir, suite_cmd: str = DEFAULT_MAP_SUITE, bound: float | None = None) -> dict:
    """Run the whole suite under pytest-testmon in ``root`` (a checkout at ``sha``) and, on a green run, write the
    map ``select_for_diff`` reads: ``<mdir>/testmondata`` and ``<mdir>/meta.json`` (``map_commit`` and
    ``last_green_commit`` = ``sha``, ``total`` = tests in the datafile, ``built_at``). Returns the meta.
    testmon writes to a temp datafile that replaces ``testmondata`` only when the run is green, so a red run
    leaves the old map (and no map at all) as it was and raises ``MapBuildError`` with the tail of the output.
    When the checkout holds ``suite_reads_plugin`` (#195) the run also records the files each test opened into a temp
    dir that replaces ``<mdir>/reads`` on a green run only, by the same discipline.
    #195: the suite's full output (stdout + stderr, red or green) is saved to ``<mdir>/build-<sha12>-<hex>.out``
    before any raise; the returned meta carries it as ``output``, and a raised ``MapBuildError`` as ``.output``.
    ``suite_cmd`` is the shell command of the suite (``--testmon`` is added); ``mdir`` lives outside any worktree.
    ``bound`` (seconds, #201): a suite run longer than it is stopped with its tree and raises ``MapBuildError`` (``over the <bound> bound``)."""
    root = Path(root)
    full = git(str(root), "rev-parse", "--verify", f"{sha}^{{commit}}").strip()
    head = git(str(root), "rev-parse", "HEAD").strip()
    if head != full:
        raise MapBuildError(f"suite directory {root} is at {head[:8]}, not at {full[:8]}")
    mdir = Path(mdir).resolve()
    mdir.mkdir(parents=True, exist_ok=True)
    tmp = mdir / f"testmondata.build-{os.getpid()}"
    cmd = suite_cmd if "--testmon" in suite_cmd else f"{suite_cmd} --testmon"
    env = nested_env({"TESTMON_DATAFILE": str(tmp)})  # absolute: testmon changes directory; no outer build's vars (#195)
    reads_tmp = mdir / f"reads.build-{os.getpid()}"
    if (root / (READS_PLUGIN.replace(".", "/") + ".py")).is_file():
        env["SUITE_READS_DIR"] = str(reads_tmp)
        env["PYTEST_ADDOPTS"] = f"-p {READS_PLUGIN}"
    out = None
    try:
        run = _run_bounded(cmd, cwd=str(root), env=env, bound=bound)
        captured = (run.stdout or "") + (run.stderr or "")
        out = _write_build_output(mdir, full, captured)  # #195: the full output, red or green, before any raise
        if run.returncode != 0:
            tail = "\n".join(captured.strip().splitlines()[-40:])
            raise MapBuildError(f"suite exited {run.returncode}; map not written\n{tail}{_output_note(out)}", out)
        if not tmp.is_file():
            raise MapBuildError("suite was green but testmon wrote no datafile (is pytest-testmon installed?)"
                                + _output_note(out), out)
        _, tests = load_testmon(str(tmp))
        os.replace(tmp, mdir / "testmondata")
        shutil.rmtree(mdir / "reads", ignore_errors=True)  # a map and reads of different commits never coexist
        if reads_tmp.is_dir():
            os.replace(reads_tmp, mdir / "reads")
    except Exception as exc:
        if out is not None:
            exc.output = out  # #195: a failure after the write still names its output file (map_run logs it)
        raise
    finally:
        for p in mdir.glob(f"{tmp.name}*"):
            p.unlink(missing_ok=True)
        shutil.rmtree(reads_tmp, ignore_errors=True)
    meta = {"map_commit": full, "last_green_commit": full, "total": len(tests - {COLLECT_KEY}),
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    write_meta(mdir, meta)
    return {**meta, "output": str(out)}  # the returned meta names the build's output file (#195); meta.json does not


def _output_note(out: Path) -> str:
    """The line a MapBuildError message ends with, naming the build's saved output (#195)."""
    return f"\nfull output: {out}"


def _write_build_output(mdir: Path, sha: str, captured: str) -> Path:
    """Save one build's full output to a new ``build-<sha12>-<hex>.out`` in ``mdir`` (#195) and delete all but the
    newest ``BUILD_OUTPUT_KEEP`` such files (this build's file is always one of them). Returns its path."""
    out = mdir / f"build-{sha[:12]}-{uuid.uuid4().hex[:8]}.out"
    out.write_text(captured, encoding="utf-8")
    older = sorted((p for p in mdir.glob(BUILD_OUTPUT_GLOB) if p != out),
                   key=lambda p: (p.stat().st_mtime_ns, p.name))
    for p in older[:max(0, len(older) - (BUILD_OUTPUT_KEEP - 1))]:
        p.unlink(missing_ok=True)
    return out


# ---------------------------------------------------------------------------------------------
# selection

@dataclass(frozen=True)
class Selection:
    """``ids``: sorted mapped test node ids NOT inside an added/changed test file, plus the bare path
    of every added or changed ``tests/test_*.py`` (the whole file is run). ``new_tests``: tests
    (by ``def test_``) in whole test files the map does not know, each file once (#47 unit B; N may exceed T
    by this). ``line``: the suite out's line 1."""
    full: bool
    reason: str
    ids: list[str]
    new_tests: int
    total: int
    map_commit: str
    line: str


EMPTY_MAP_REASON = "map holds 0 tests"  # #74: a map with no test is no map (the full suite runs)


def _full(reason: str, total: int = 0, map_commit: str = "") -> Selection:
    return Selection(True, reason, [], 0, total, map_commit, f"full suite: {reason}")


def _empty_map(total: int, map_commit: str) -> Selection:
    """#74: the full suite for a map that holds 0 tests. The line names the map (its sha8): the merge result
    carries that line to the brain notice, which is keyed by the map commit."""
    return Selection(True, EMPTY_MAP_REASON, [], 0, total, map_commit,
                     f"full suite: {EMPTY_MAP_REASON} (map {map_commit[:8]})")


def _name_status(root, base, head) -> list[tuple[str, str, str | None]]:
    """``(status letter, path, old path or None)``; a rename becomes D old + A new."""
    rows: list[tuple[str, str, str | None]] = []
    for line in git(str(root), "diff", "--name-status", base, head).splitlines():
        parts = line.split("	")
        st = parts[0][:1]
        if st in "RC" and len(parts) >= 3:
            if st == "R":
                rows.append(("D", parts[1], None))
            rows.append(("A", parts[2], None))
        elif len(parts) >= 2:
            rows.append((st, parts[1], None))
    return rows


def select_for_diff(root, base: str, head: str, mdir) -> Selection:
    """Tests a merge of ``head`` over ``base`` must run, or a full-suite fallback (see #690 "Rule")."""
    try:
        return _select(str(root), base, head, Path(mdir))
    except Exception as e:  # the selection tool failed: never a half answer
        first = (str(e).strip().splitlines() or [type(e).__name__])[0]
        return _full(f"selection failed: {first}")


def _mod_bin_tests(root: str, head: str, rows: list[tuple[str, str, str | None]]) -> set[str]:
    """#959: the ``bin/test_*.py`` files (direct children, as at ``head``) of every ``mods/<mod>/`` a diff touches.
    The plugin's own test command (``claude plugin test .``) is not run by a merge."""
    mods: set[str] = set()
    for _, f, _ in rows:
        if MOD_FILE.fullmatch(f):
            mods.add("/".join(f.split("/")[:2]))
    out: set[str] = set()
    for mod in sorted(mods):
        for path in git(root, "ls-tree", "--name-only", head, f"{mod}/bin/").splitlines():
            if MOD_BIN_TEST.fullmatch(path):
                out.add(path)
    return out


def _select(root: str, base: str, head: str, mdir: Path) -> Selection:
    rows = _name_status(root, base, head)
    if any(f in SHARED_IMPORTS for _, f, _ in rows):  # #41 rule 4: before any map-based selection
        return _full("change touches a file every test imports")
    meta = read_meta(mdir)
    if meta is None or not (mdir / "testmondata").is_file():
        return _full("no map")
    map_commit = str(meta.get("map_commit") or "")
    total = int(meta.get("total") or 0)
    green = meta.get("last_green_commit")
    if not map_commit:
        return _full("no map")
    if total == 0:  # #74: a map that holds no test is no map
        return _empty_map(total, map_commit)
    code_deps, tests = load_testmon(str(mdir / "testmondata"))  # the same count build_map writes as ``total``
    if not (tests - {COLLECT_KEY}):
        return _empty_map(total, map_commit)
    if not green:
        return _full("no green full run", total, map_commit)
    # #41 rule 2: the map's commit must be an ancestor of the tested head, else the map says nothing about it
    anc = subprocess.run(["git", "-C", root, "merge-base", "--is-ancestor", map_commit, head],
                         capture_output=True, text=True, encoding="utf-8", creationflags=no_window_flags())
    if anc.returncode == 1:
        return _full("map commit not an ancestor", total, map_commit)
    if anc.returncode != 0:
        raise RuntimeError(anc.stderr.strip() or "merge-base failed")

    read_files, read_dirs = load_reads(str(mdir / "reads"))
    # #195 rule 1: a data change is decided by the reads map; a data file no test was seen to open (new file,
    # never read, or no reads dir) is the full suite as before
    data_files = sorted({f for _, f, _ in rows if f in DATA_CHANGE_FILES or f.startswith(DATA_CHANGE_PREFIXES)})
    if any(f not in read_files for f in data_files):
        return _full("data change")
    by_reads: set[str] = set().union(*(read_files[f] for f in data_files)) & tests if data_files else set()

    unmapped = [f for st, f, _ in rows  # a changed mod bin test is a test file (#959), not an unmapped module
                if classify_file(f) == "code" and st != "A" and not TEST_FILE.fullmatch(f)
                and not MOD_BIN_TEST.fullmatch(f) and f not in code_deps]
    ref_ids: set[str] = set()
    ref_whole: set[str] = set()
    unreached: list[str] = []
    if unmapped:  # #737 rule C: the tests of its referrers, not the full suite
        from .test_referrers import referrer_tests
        ref_ids, ref_whole, unreached = referrer_tests(root, head, unmapped, code_deps)
    changed = [f for _, f, _ in rows]
    sel, blind = select_tests(changed, code_deps, read_files, read_dirs)
    for f in blind:
        if not (f.startswith("docs/") or _is_root_md(f)):  # a root document no test opens is a document (#719)
            return _full(f"{f} is unknown to the map", total, map_commit)
    from . import core_check  # a changed entry of the core manifest also runs the core check's own tests (#217)
    manifest = set(core_check.read_manifest())
    if any(f in manifest for f in changed):
        sel |= {t for t in tests if CORE_CHECK_TEST.fullmatch(t.split("::", 1)[0])}
    sel = (sel | ref_ids) & tests

    live = [f for st, f, _ in rows if st != "D" and TEST_FILE.fullmatch(f)]
    gone = {f for st, f, _ in rows if st == "D" and TEST_FILE.fullmatch(f)}
    # #737 rule B: a test file added or changed on main since the map's commit runs whole in every selection
    # (a failing diff raises: select_for_diff turns it into the ``selection failed`` full fallback)
    since_map = _name_status(root, map_commit, base)
    young = {f for st, f, _ in since_map if st != "D" and TEST_FILE.fullmatch(f)} - gone - set(live)
    # row 11: a test file deleted or renamed on main since the map keeps no id (pytest: ``not found``)
    gone |= {f for st, f, _ in since_map if st == "D" and TEST_FILE.fullmatch(f)} - set(live)
    ref_whole = ref_whole | young
    if NO_WINDOW_GUARD not in gone and any(f.startswith("tools/agentops/") for f in changed):
        ref_whole = ref_whole | {NO_WINDOW_GUARD}  # #950: a change under tools/agentops/ always runs the guard
    # #959: the bin tests of each touched mod run whole; an added one is counted by new_tests, the others here
    added = {f for st, f, _ in rows if st == "A"}
    mod_bin = _mod_bin_tests(root, head, rows)
    ref_whole = ref_whole | (mod_bin - added)
    # #1118: a content-parametrized test file with any selected mapped id runs whole, its recorded ids dropped
    ref_whole = ref_whole | {f for f in CONTENT_PARAMETRIZED_TESTS - gone if any(i.split("::", 1)[0] == f for i in sel)}
    whole = set(live) | ref_whole | mod_bin
    inside = lambda i: i.split("::", 1)[0]  # noqa: E731
    ids = sorted(i for i in sel if inside(i) not in whole and inside(i) not in gone)
    mapped_in_whole = sum(1 for t in tests if inside(t) in whole)
    # a whole file the map does not know (a new test file, a referrer younger than the map, or both)
    # is counted once, by its ``def test_``; these are the tests that run beyond the map's total
    new_tests = sum(len(_TEST_DEF.findall(git(root, "show", f"{head}:{f}")))
                    for f in sorted(whole) if not any(inside(t) == f for t in tests))
    n = len(ids) + mapped_in_whole + new_tests
    if new_tests:  # N may exceed T by exactly these: the line names them
        head_text = f"selected {n} of {total} tests in the map (+{new_tests} added since, map {map_commit[:8]})"
    else:
        head_text = f"selected {n} of {total} tests (map {map_commit[:8]})"
    reason = "; ".join(f"no test reaches {f}" for f in unreached)
    line = head_text + (f"; {reason}" if reason else "")
    if data_files:  # #195 rule 3
        line += f"; selected by reads: {len(by_reads)} tests for {len(data_files)} data files"
    return Selection(False, reason, ids + sorted(whole), new_tests, total, map_commit, line)
