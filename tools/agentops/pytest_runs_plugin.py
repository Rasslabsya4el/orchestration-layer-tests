"""pytest plugin (``-p tools.agentops.pytest_runs_plugin``, loaded by ``pytest.ini`` addopts): one log line per run (#235).

At session finish the controller process appends ONE JSON line to ``pytest_runs.jsonl`` in ``machine_dir()``:

    ``start``, ``end``  "%Y-%m-%d %H:%M:%S" local time (``util.now``)
    ``repo``            folder name of the MAIN checkout (a worktree: the parent of ``git rev-parse --git-common-dir``)
    ``worktree``        folder name of the rootdir
    ``ticket``          ``issue_of`` the folder name, else of the current branch; else null
    ``selected``        tests collected (``session.testscollected``: a ``-x`` stop does not shrink it)
    ``passed``          tests whose call passed and that failed in no phase
    ``failed``          ``session.testsfailed``: failed phases, collection errors included

The plugin never delays, moves or refuses a run and swallows its own errors. It is inert when the env var
``AGENTOPS_NO_RUNLOG`` is ``1``: ``with_no_runlog`` sets it in the env of every suite run that is not a logged agent run.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .machine_mode import machine_dir
from .util import append_jsonl, no_window_flags, now

NO_RUNLOG_ENV = "AGENTOPS_NO_RUNLOG"
LOG_NAME = "pytest_runs.jsonl"
_I_PREFIX = re.compile(r"^(?:wt-)?[io]?(\d+)")  # ORCH's names: `wt-i235-u1`, `i235-u1` (branch_issue reads only N, oN)

_run: dict = {}


def with_no_runlog(env: dict | None = None) -> dict[str, str]:
    """A copy of ``env`` with the guard set: the one place a suite run's env says it is not logged (#235)."""
    return {**(env or {}), NO_RUNLOG_ENV: "1"}


def issue_of(name: str | None) -> int | None:
    """The issue a folder or branch name carries: ``worktree.branch_issue``'s rule, else an ``i`` (or ``o``) prefix
    after an optional ``wt-``. Dates are not numbers (``2026-10-19-smoke`` -> None)."""
    from .worktree import BRANCH_DATE, branch_issue  # lazy: the plugin's import stays light
    number = branch_issue(name)
    if number is None:
        match = _I_PREFIX.match(BRANCH_DATE.sub("", name or ""))
        number = int(match.group(1)) if match else None
    return number


def _git(cwd: Path, *args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=10, creationflags=no_window_flags())
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _repo_name(root: Path) -> str:
    common = _git(root, "rev-parse", "--git-common-dir")
    if common:
        return (root / common).resolve().parent.name  # main checkout: .git's parent; worktree: the same repo
    return root.name


def _ticket(root: Path) -> int | None:
    for name in (root.name, _git(root, "rev-parse", "--abbrev-ref", "HEAD")):
        number = issue_of(name)
        if number is not None:
            return number
    return None


def pytest_configure(config):
    try:
        _run.clear()
        _run.update(start=now(), dir=machine_dir(), failed=set(), passed=set())  # the dir at this run's start
    except Exception:  # noqa: BLE001 -- a log failure never fails the run
        pass


def pytest_runtest_logreport(report):
    try:
        if report.failed:
            _run["failed"].add(report.nodeid)
        elif report.when == "call" and report.passed:
            _run["passed"].add(report.nodeid)
    except Exception:  # noqa: BLE001
        pass


def pytest_sessionfinish(session, exitstatus):
    try:
        if os.environ.get(NO_RUNLOG_ENV) == "1" or hasattr(session.config, "workerinput"):
            return  # the guard, or an xdist worker (the controller saw every report)
        root = Path(str(session.config.rootpath))
        record = {
            "start": _run["start"],
            "end": now(),
            "repo": _repo_name(root),
            "worktree": root.name,
            "ticket": _ticket(root),
            "selected": session.testscollected,
            "passed": len(_run["passed"] - _run["failed"]),
            "failed": session.testsfailed,
        }
        append_jsonl(_run["dir"] / LOG_NAME, record)
    except Exception:  # noqa: BLE001 -- a log failure never fails the run
        return
