"""#343 unit 1 (rule 1): the workflow files the core dispatches, and the tests repo's default branch that holds them.

GitHub finds a ``workflow_dispatch`` workflow by its file on the repo's DEFAULT branch, so a copy whose default branch
lacks ``map.yml`` answers 404 to the map build's dispatch (#343 facts). The registry below is the one place a dispatched
workflow file's name is written; every dispatch call (``map_github.dispatch_map``, ``github_run.dispatch``) takes its
name from here, and the sync derives its list from the same constant.

* ``live_default_branch(repo, runner)`` reads the repo's default branch name from GitHub (``gh api repos/<repo>``).
* ``ensure_workflows(tests_repo, default_branch, runner)`` makes each registry file exist on that branch with the same
  bytes as ``.github/workflows/<file>`` of this core repo (read by the contents API, compared by content; written by the
  contents API ``PUT`` when missing or different). It returns the names it wrote, and any failure propagates. Only the
  test copy named by the ``test_copy_remote`` setting is written to: any other repo gets nothing (``[]``).
* ``prepare_dispatch(repo, runner)`` is what every dispatch calls first: the guard, then the live default branch and
  ``ensure_workflows``.

``runner`` is the ``gh`` seam: ``runner(args, input=None) -> str`` (stdout, a failed call raises). The default goes
through ``gh_call.run_gh``. Stdlib plus ``gh_call`` and ``util`` only; nothing here imports the dispatchers, so they can
import this module.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
from pathlib import Path
from typing import Callable

from . import core_settings
from .gh_call import run_gh
from .util import AgentOpsError

#: the registry: every workflow file of the core that a dispatch call starts, by name.
TESTS_WORKFLOW = "tests.yml"
MAP_WORKFLOW = "map.yml"
DISPATCHED_WORKFLOWS = (TESTS_WORKFLOW, MAP_WORKFLOW)

WORKFLOW_DIR = ".github/workflows"
_NOT_FOUND = re.compile(r"^gh: Not Found \(HTTP 404\)", re.M)

Runner = Callable[..., str]


def gh_runner(args: list[str], input: str | None = None) -> str:
    """The real ``gh`` through the repo's adapter (no console window); a failed call raises."""
    return run_gh(args, input=input, check=True).stdout


def source_dir() -> Path:
    """This core repo's ``.github/workflows``: the module sits in ``tools/agentops``, two folders below the repo root."""
    return Path(__file__).resolve().parents[2] / WORKFLOW_DIR


def live_default_branch(repo: str, runner: Runner = gh_runner) -> str:
    """The name of ``repo``'s default branch as GitHub holds it now; raises when GitHub gives none."""
    name = runner(["api", f"repos/{repo}", "--jq", ".default_branch"]).strip()
    if not name:
        raise AgentOpsError(f"{repo} gives no default branch")
    return name


def _remote_file(repo: str, name: str, branch: str, runner: Runner) -> tuple[bytes | None, str | None]:
    """The bytes and blob sha of ``name`` on ``branch``, or ``(None, None)`` when GitHub answers 404 for it."""
    try:
        body = json.loads(runner(["api", f"repos/{repo}/contents/{WORKFLOW_DIR}/{name}?ref={branch}"]))
    except subprocess.CalledProcessError as exc:
        if "404" in (exc.stderr or ""):
            return None, None
        raise
    return base64.b64decode(body["content"]), body["sha"]


def _settings() -> dict:
    """The installation's settings (``config/orchestration.json``, through the core's one settings reader)."""
    return core_settings.load()


def _copy_slug() -> str:
    """``<owner>/<name>`` of the test copy the ``test_copy_remote`` setting names; ValueError when it names none."""
    from .github_suite import copy_slug  # local: github_suite imports test_copy_send, which imports this module

    return copy_slug(str(_settings().get("test_copy_remote") or ""))


def _is_copy(repo: str) -> bool:
    """True only for the test copy itself: an own-runners repo (no test copy named) is never a copy."""
    try:
        return repo == _copy_slug()
    except ValueError:
        return False


def _first_line(exc: subprocess.CalledProcessError) -> str:
    lines = (exc.stderr or "").strip().splitlines()
    return lines[0] if lines else f"gh exited {exc.returncode}"


def _remote_file(repo: str, name: str, branch: str, runner: Runner) -> tuple[bytes | None, str | None]:
    """The bytes and blob sha of ``name`` on ``branch``, or ``(None, None)`` when GitHub answers 404 for it: the
    status line ``gh: Not Found (HTTP 404)`` only. Any other failed call raises ``AgentOpsError`` with its first line."""
    try:
        body = json.loads(runner(["api", f"repos/{repo}/contents/{WORKFLOW_DIR}/{name}?ref={branch}"]))
    except subprocess.CalledProcessError as exc:
        if _NOT_FOUND.search(exc.stderr or ""):
            return None, None
        raise AgentOpsError(_first_line(exc)) from exc
    return base64.b64decode(body["content"]), body["sha"]


def ensure_workflows(tests_repo: str, default_branch: str, runner: Runner) -> list[str]:
    """Make every registry file exist on ``default_branch`` of ``tests_repo`` with this core's bytes; return the names
    written (an empty list when all of them already match, or when ``tests_repo`` is not the test copy). Raises when a
    file is not in this core's ``.github/workflows`` or when a GitHub call fails."""
    if not _is_copy(tests_repo):
        return []
    written = []
    for name in DISPATCHED_WORKFLOWS:
        path = source_dir() / name
        if not path.is_file():
            raise AgentOpsError(f"{name} is dispatched but {WORKFLOW_DIR}/{name} is not in the core repo")
        local = path.read_bytes()
        remote, sha = _remote_file(tests_repo, name, default_branch, runner)
        if remote == local:
            continue
        body = {"message": f"core sync: {name}", "branch": default_branch,
                "content": base64.b64encode(local).decode("ascii")}
        if sha:
            body["sha"] = sha
        try:
            runner(["api", "-X", "PUT", f"repos/{tests_repo}/contents/{WORKFLOW_DIR}/{name}", "--input", "-"],
                   input=json.dumps(body))
        except subprocess.CalledProcessError as exc:
            raise AgentOpsError(_first_line(exc)) from exc
        written.append(name)
    return written


def prepare_dispatch(repo: str, runner: Runner = gh_runner) -> list[str]:
    """The step every dispatch takes before it dispatches: the guard first (nothing is read or written for a repo that is
    not the test copy), then the live default branch of the copy, then ``ensure_workflows``. Returns the names written."""
    if not _is_copy(repo):
        return []
    return ensure_workflows(repo, live_default_branch(repo, runner), runner)
