"""pytest plugin (``-p tools.agentops.suite_reads_plugin``): which repo files each test opened (#195).

When the env var ``SUITE_READS_DIR`` names a directory, every process that runs tests (the one process without xdist,
each xdist worker; never the xdist controller) installs a ``sys.addaudithook`` and attributes each file ``open`` and each
directory listing (``os.listdir`` / ``os.scandir``) to the test running at that moment (its setup, call and teardown;
what happens at collection or between tests belongs to no test). At the end of the process it writes
``<dir>/reads-<worker>-<pid>.json``:

    {"files": {nodeid: [repo-relative posix path, ...]}, "dirs": {nodeid: [repo-relative posix dir, ...]}}

the format ``tools.agentops.test_select.load_reads`` merges. Only paths under the repo root (``SUITE_READS_ROOT``, else
pytest's rootdir) are kept, and only reads: python sources and bytecode, ``.git/``, ``var/``, ``.claude/worktrees/``,
``__pycache__`` and testmon's own files are dropped, as is a file opened for writing. Without the env var the plugin
does nothing."""
from __future__ import annotations

import json
import os
import sys

import pytest

ENV_DIR = "SUITE_READS_DIR"
ENV_ROOT = "SUITE_READS_ROOT"
PLUGIN = "tools.agentops.suite_reads_plugin"
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_SKIP_SUFFIXES = (".py", ".pyc", ".pyo")
_SKIP_PARTS = frozenset({".git", "__pycache__"})
_SKIP_PREFIXES = ("var/", ".claude/worktrees/")

_state = {"current": None, "root": "", "out": "", "files": {}, "dirs": {}, "busy": False}


def repo_relative(path, root: str, skip_dir: str = "") -> str | None:
    """``path`` as a repo-relative posix path, or None when it is outside ``root`` or one of the dropped kinds."""
    try:
        full = os.path.abspath(os.fsdecode(path))
    except (TypeError, ValueError):
        return None
    low_root, low = os.path.normcase(root), os.path.normcase(full)
    if skip_dir and (low == os.path.normcase(skip_dir) or low.startswith(os.path.normcase(skip_dir) + os.sep)):
        return None
    if low == low_root:
        return "."
    if not low.startswith(low_root.rstrip(os.sep) + os.sep):
        return None
    rel = full[len(root.rstrip(os.sep)) + 1:].replace(os.sep, "/")
    if _SKIP_PARTS & set(rel.split("/")) or rel.startswith(_SKIP_PREFIXES):
        return None
    return rel


def _is_dropped_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return rel.endswith(_SKIP_SUFFIXES) or name.lstrip(".").startswith("testmondata")


def _hook(event: str, args: tuple) -> None:
    nodeid = _state["current"]
    if nodeid is None or _state["busy"]:
        return
    if event == "open":
        kind, path, flags = "files", args[0], args[2]
        if isinstance(path, int) or (isinstance(flags, int) and flags & _WRITE_FLAGS):
            return
    elif event in ("os.listdir", "os.scandir"):
        kind, path = "dirs", args[0] if args and args[0] is not None else "."
        if isinstance(path, int):
            return
    else:
        return
    _state["busy"] = True
    try:
        rel = repo_relative(path, _state["root"], _state["out"])
        if rel is None or (kind == "files" and _is_dropped_file(rel)):
            return
        _state[kind].setdefault(nodeid, set()).add(rel)
    finally:
        _state["busy"] = False


def _active(config) -> bool:
    if not os.environ.get(ENV_DIR):
        return False
    if hasattr(config, "workerinput"):
        return True
    return not getattr(config.option, "numprocesses", None)  # xdist controller: the workers write


def pytest_configure(config):
    if not _active(config):
        return
    _state["root"] = os.path.abspath(os.environ.get(ENV_ROOT) or str(config.rootpath))
    _state["out"] = os.path.abspath(os.environ[ENV_DIR])
    _state["files"], _state["dirs"] = {}, {}
    sys.addaudithook(_hook)  # a hook cannot be removed: it stays inert while "current" is None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    if not _state["root"]:
        yield
        return
    _state["current"] = item.nodeid
    try:
        yield
    finally:
        _state["current"] = None


def pytest_sessionfinish(session, exitstatus):
    if not _state["root"] or not _active(session.config):
        return
    out = _state["out"]
    os.makedirs(out, exist_ok=True)
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    ids = set(_state["files"]) | set(_state["dirs"])
    dump = {"files": {t: sorted(_state["files"].get(t, ())) for t in sorted(ids)},
            "dirs": {t: sorted(_state["dirs"].get(t, ())) for t in sorted(ids)}}
    tmp = os.path.join(out, f".reads-{worker}-{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(dump, fh)
    os.replace(tmp, os.path.join(out, f"reads-{worker}-{os.getpid()}.json"))
    _state["root"] = ""
