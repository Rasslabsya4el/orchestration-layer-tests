"""Issue #1096 unit 2 (tools-imports): a port-like copy of the core imports with no ImportError.

The manifest's files alone are copied to a folder of their own (paths kept). Every manifest .py module is
imported there by a fresh ``python -I`` that has only that folder on its path, and two paths the imports of
this unit reach are run there: ``worktree.standing_suite_paths`` (the map run's FULL_WORKTREE, which map_run.py imports; worktree.py:338)
and ``test_select.select_for_diff`` on a commit that changes an unmapped .py file (the referrer import,
test_select.py:262). A repo module that is not in the manifest is absent from the copy, so an import of one
fails here.

Unit 3 (monitor-and-port: the monitor mod's bin/collect.py, tools/agentops/port.py) is not in this unit's files;
a module it still owns is named in UNIT3_SKIP and skipped by name until unit 3 lands.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from tools.agentops import core_check

REPO = Path(__file__).resolve().parents[1]
#: unit 3's modules (not this unit's files): skipped by name here, until unit 3 removes their imports.
UNIT3_SKIP: tuple[str, ...] = ()
GIT_ID = ("-c", "user.name=t", "-c", "user.email=t@t")

#: the child's own sys.path: the copy folder first (cwd), then the third-party site paths of this interpreter
#: (``-I`` drops the user site, where pytest's pygments lives; a plugin module of the core imports pytest).
_PATH_HEAD = r"""
import json, os, sys
sys.path[:0] = [os.getcwd()] + json.loads(sys.argv[1])
"""

#: each module is imported the way a script runs it: its own folder first on sys.path (the hooks of tools/hooks
#: import their sibling ``shell_words`` by its bare name, as Claude Code runs them with their folder on the path).
_IMPORT_ALL = _PATH_HEAD + r"""
import importlib
done = []
for name, folder in json.loads(sys.argv[2]):
    sys.path.insert(0, os.path.join(os.getcwd(), folder))
    importlib.import_module(name)
    done.append(name)
print(json.dumps(done))
"""

_SELECT = _PATH_HEAD + r"""
from pathlib import Path
from tools.agentops import merge, test_select, worktree
main = Path(os.getcwd())
standing = sorted(worktree.standing_suite_paths(main))
sel = test_select.select_for_diff(main, sys.argv[2], sys.argv[3], Path(sys.argv[4]))
print(json.dumps({"standing": standing, "line": sel.line, "full": sel.full,
                  "referrers_loaded": "tools.agentops.test_referrers" in sys.modules,
                  "priority_flags": merge._local_suite_flags(), "router": merge._suite_router() is not None}))
"""


def _module_name(rel: str) -> str:
    """``tools/agentops/merge.py`` -> ``tools.agentops.merge``; a package's ``__init__.py`` -> the package."""
    parts = rel[:-len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _remove_own_copy(folder: Path) -> None:
    """Removes this test's own copy. git's read-only files are made writable first (Windows cannot unlink them)."""
    for path in [folder, *folder.rglob("*")]:
        path.chmod(stat.S_IREAD | stat.S_IWRITE)
    shutil.rmtree(folder)


@contextmanager
def _port_copy():
    """A folder of its own in the system temp folder holding the manifest's files, paths kept, and the settings file.

    The settings file is not in the manifest: ``port`` writes ``config/orchestration.json`` into every target, and
    the core's modules read it at import (dispatch.py REPO). Not under ``var/``: that is a junction into the main
    checkout, and a child process started with that folder as its cwd reports the main checkout as its cwd."""
    folder = Path(tempfile.mkdtemp(prefix="port-copy-1096-"))
    try:
        for rel in [*core_check.read_manifest(), "config/orchestration.json"]:
            src = REPO / rel
            if src.is_file():
                (folder / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, folder / rel)
        yield folder
    finally:
        _remove_own_copy(folder)


def _git(folder: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(folder), *GIT_ID, *args], capture_output=True, text=True,
                          encoding="utf-8", check=True).stdout.strip()


def _site_paths() -> str:
    """This interpreter's third-party site paths, as JSON for the child (its own repo copy comes first)."""
    import site
    paths = [*site.getsitepackages(), site.getusersitepackages()]
    return json.dumps([p for p in paths if isinstance(p, str)])


def _py_modules() -> list[str]:
    entries = [e for e in core_check.read_manifest() if e.endswith(".py")]
    return [e for e in entries if e not in UNIT3_SKIP]


def test_every_manifest_module_imports_in_a_port_copy():
    modules = _py_modules()
    assert len(modules) >= 70, f"the manifest lists {len(modules)} .py modules; a count this low is no check"
    with _port_copy() as folder:
        names = [_module_name(rel) for rel in modules]
        plan = [[_module_name(rel), str(Path(rel).parent)] for rel in modules]
        run = subprocess.run([sys.executable, "-I", "-c", _IMPORT_ALL, _site_paths(), json.dumps(plan)], cwd=folder,
                             capture_output=True, text=True, encoding="utf-8")
    assert not folder.exists(), f"the test's own copy is still there: {folder}"
    assert run.returncode == 0, f"an import failed in the port copy:\n{run.stderr[-3000:]}"
    assert len(json.loads(run.stdout.strip().splitlines()[-1])) == len(names)
    print(f"\nport copy: {len(names)} manifest .py modules imported with python -I, no ImportError")


def test_standing_paths_and_referrer_path_run_in_a_port_copy():
    with _port_copy() as folder:
        # a target holds its own tests/ next to the core: the no-window guard (#950) runs for any change under
        # tools/agentops/, so the copy carries a stand-in for it (the manifest's files alone have no tests/)
        guard = folder / "tests" / "test_agentops_no_window.py"
        guard.parent.mkdir(parents=True, exist_ok=True)
        guard.write_text("def test_guard():\n    pass\n", encoding="utf-8")
        _git(folder, "init", "-q")
        _git(folder, "add", "-A")
        _git(folder, "commit", "-q", "-m", "base")
        base = _git(folder, "rev-parse", "HEAD")
        target = folder / "tools" / "agentops" / "util.py"
        target.write_text(target.read_text(encoding="utf-8") + "\n# port copy change\n", encoding="utf-8")
        _git(folder, "add", "-A")
        _git(folder, "commit", "-q", "-m", "head")
        head = _git(folder, "rev-parse", "HEAD")
        mdir = folder / "map"
        mdir.mkdir()
        (mdir / "meta.json").write_text(json.dumps({"map_commit": base, "last_green_commit": base, "total": 1}),
                                        encoding="utf-8")
        con = sqlite3.connect(mdir / "testmondata")  # the testmon schema, one test: util.py has no row, so it is unmapped
        con.execute("create table test_execution (id integer, test_name text)")
        con.execute("create table test_execution_file_fp (test_execution_id integer, fingerprint_id integer)")
        con.execute("create table file_fp (id integer, filename text)")
        con.execute("insert into test_execution values (1, 'tests/test_other.py::test_other')")  # one test, so the map is not empty (#74)
        con.commit()
        con.close()
        run = subprocess.run([sys.executable, "-I", "-c", _SELECT, _site_paths(), base, head, str(mdir)], cwd=folder,
                             capture_output=True, text=True, encoding="utf-8")
    assert run.returncode == 0, f"the port copy's paths raised:\n{run.stderr[-3000:]}"
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert any(path.endswith("full-run") for path in out["standing"]), out["standing"]
    assert out["referrers_loaded"], f"select_for_diff did not reach the referrer import (test_select.py:262): {out['line']}"
    assert "selection failed" not in out["line"], out["line"]
    # a target has no map_run and no suite_router: the two merge hooks answer their absent-module values, not an error
    assert out["priority_flags"] == 0 and out["router"] is False, out
