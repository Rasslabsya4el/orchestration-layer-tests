"""One merge's suite on GitHub machines, or the reason it goes to the PC.

``run_github_suite(...)`` sends the branch to the repo's test copy, takes GitHub machines from the machine-wide line
(blocking until the line gives them), dispatches the copy's ``tests.yml`` split into that many parts, polls it, and
returns a ``GithubSuiteResult``:

* ``status`` ``green`` (every part passed), ``red`` (a part failed a test: ``failed_ids`` / ``full_recheck`` say what
  the PC re-checks) or ``fallback`` (the suite runs on the PC; ``reason`` is ``refused``, ``send-failed``,
  ``dispatch-failed``, ``poll-failed``, ``no-places``, ``not-started`` or ``lost``, and ``brain_line`` is the one line for the
  area's brain chat). A fallback is never red.
* The machines are released in every path (a failed ``acquire_machines`` holds none). ``record`` is called once, at
  the end, with ``own_runner``; a failure of it is swallowed. A fallback after the dispatch records ``minutes_used``
  0, or the exception's own ``minutes_used`` attribute when it carries one (``poll`` raises none today).
* No state is kept here: the next call asks GitHub again. Without ``own_repo`` an empty ``remote`` is a
  programming error (the caller has no copy and never calls): ``ValueError``.
* ``own_repo`` (a private repo on its own runners, no copy): nothing is sanitized or sent; ``push`` puts the branch
  on the repo's own origin and dispatch and poll run against ``repo_slug``.

``remote`` is the copy's URL (``https://github.com/<owner>/<name>.git``) or a
bare ``<owner>/<name>`` (a repo whose copy is itself); dispatch and poll run against that repo. ``deps`` holds the
injectable callables, each defaulting to the real one. ``poll`` counts its 10-minute no-start bound from
``clock()`` taken just before the dispatch.
"""

from __future__ import annotations

import math
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable

from . import core_settings, github_run, machine_berths, test_copy_send
from .test_copy_check import git_env
from .util import no_window_flags, now

MAX_BRAIN_LINE = 160
PUSH_SECONDS = 300
_SLUG_RE = re.compile(r"^(?:.*github\.com[:/])?([\w.-]+/[\w.-]+?)(?:\.git)?/?$")


def _record_run(record: dict) -> None:
    from .github_minutes import record_run  # imported when used


    record_run(record)


def _push(root, branch: str, repo_slug: str) -> None:
    """Push ``branch`` to the repo's own origin (forced for that one ref); raises on any failure."""
    subprocess.run(["git", "-C", str(root), "push", "origin", f"+{branch}:refs/heads/{branch}"],
                   capture_output=True, check=True, creationflags=no_window_flags(), env=git_env(),
                   timeout=PUSH_SECONDS)


@dataclass
class Deps:
    send: Callable = test_copy_send.send
    acquire_machines: Callable = machine_berths.acquire_machines
    dispatch: Callable = github_run.dispatch
    poll: Callable = github_run.poll
    record: Callable[[dict], None] = _record_run
    push: Callable = _push
    clock: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class GithubSuiteResult:
    status: str
    reason: str = ""
    failed_ids: list = field(default_factory=list)
    full_recheck: bool = False
    machines: int = 0
    part_seconds: list = field(default_factory=list)
    minutes_used: int = 0
    run_id: int | None = None
    brain_line: str = ""


def copy_slug(remote: str) -> str:
    """``<owner>/<name>`` of the test copy named by ``remote``; ValueError when it holds none."""
    match = _SLUG_RE.match(remote.strip())
    if not match:
        raise ValueError(f"no <owner>/<name> in test copy remote {remote!r}")
    return match.group(1)


def brain_line(repo_name: str, branch: str, reason: str) -> str:
    head, fixed = f"{repo_name} merge ", ": GitHub " + "{}" + ", suite on the PC"
    budget = MAX_BRAIN_LINE - len(head) - len(fixed.format(""))  # room for the branch and the reason together
    if len(branch) + len(reason) > budget:  # #251: the reason (hit names) gives way first, down to what leaves the branch 40
        reason_room = budget - min(len(branch), 40)
        if len(reason) > reason_room:
            reason = reason[: max(reason_room - 3, 0)] + "..."
    if len(branch) + len(reason) > budget:
        branch = branch[: max(budget - len(reason) - 3, 0)] + "..."
    return head + branch + fixed.format(reason)


#: One GitHub machine runs about this many tests in the time a merge wants.
TESTS_PER_MACHINE = 900


def machines_for(tests: int | None, per_merge: int) -> int:
    """Machines a suite of ``tests`` tests asks for: ``per_merge`` when the count is unknown (None, the full suite),
    else one per 900 tests, at least 1 and at most ``per_merge``."""
    if tests is None:
        return per_merge
    return max(1, min(per_merge, math.ceil(tests / TESTS_PER_MACHINE)))


def run_github_suite(*, root, branch: str, repo_slug: str, remote: str, repo_name: str, priority: bool,
                     settings: dict, deps: Deps | None = None, own_repo: bool = False, tests: int | None = None,
                     ids: list[str] | None = None, windows: bool = False) -> GithubSuiteResult:
    if not own_repo and (not remote or not remote.strip()):
        raise ValueError("run_github_suite needs a test copy remote; a repo without a copy never calls it")
    deps = deps or Deps()
    copy = repo_slug if own_repo else copy_slug(remote)
    result = _run(root, branch, remote, copy, repo_name, priority, settings, deps, own_repo, tests, ids, windows)
    _record(deps, result, repo_name, repo_slug, own_repo)
    return result


def _fallback(reason: str, repo_name: str, branch: str, **fields) -> GithubSuiteResult:
    return GithubSuiteResult(status="fallback", reason=reason, brain_line=brain_line(repo_name, branch, reason),
                             **fields)


def _setting(settings: dict, key: str) -> int:
    value = settings.get(key)
    return core_settings.DEFAULTS[key] if value is None else value


def _run(root, branch, remote, copy, repo_name, priority, settings, deps, own_repo, tests=None, ids=None,
         windows=False) -> GithubSuiteResult:
    refusal = None
    try:
        if own_repo:
            deps.push(root, branch, copy)
            sent_ok = True
        else:
            sent = deps.send(root, branch, remote)
            sent_ok, refusal = sent.sent, getattr(sent, "reason", None)
    except Exception:  # noqa: BLE001 - any send or push failure sends the suite to the PC
        return _fallback("send-failed", repo_name, branch)
    if not sent_ok:  # #251: the refusal's own reason follows the word, so the place and the brain line name it
        return _fallback(f"refused: {refusal}" if refusal else "refused", repo_name, branch)
    per_merge = _setting(settings, "github_machines_per_merge")
    try:
        berth = deps.acquire_machines(root, branch, want=machines_for(tests, per_merge), total=_setting(settings, "github_machines_total"),
                                      per_merge=per_merge, priority=priority)
        machines = berth.machines
    except Exception:  # noqa: BLE001 - no places from the line: the PC takes the suite, nothing is held
        return _fallback("no-places", repo_name, branch)
    try:
        try:
            since = deps.clock()
            run_id = deps.dispatch(copy, branch, machines, **({"ids": ids} if ids else {}),
                                   **({"windows": True} if windows else {}))
        except Exception:  # noqa: BLE001
            return _fallback("dispatch-failed", repo_name, branch, machines=machines)
        try:
            polled = deps.poll(copy, run_id, parts=machines, since=since, **({"windows": True} if windows else {}))
        except Exception as exc:  # noqa: BLE001
            return _fallback("poll-failed", repo_name, branch, machines=machines, run_id=run_id,
                             minutes_used=int(getattr(exc, "minutes_used", 0) or 0))
    finally:
        berth.release()
    fields = dict(machines=machines, run_id=polled.get("run_id", run_id),
                  minutes_used=int(polled.get("minutes_used") or 0),
                  part_seconds=[part.get("seconds", 0) for part in polled.get("parts") or []])
    outcome = polled.get("outcome")
    if outcome == "green":
        return GithubSuiteResult(status="green", **fields)
    if outcome == "red-test":
        return GithubSuiteResult(status="red", failed_ids=list(polled.get("failed_ids") or []),
                                 full_recheck=bool(polled.get("full_recheck")), **fields)
    return _fallback("not-started" if outcome == "not-started" else "lost", repo_name, branch, **fields)


def _record(deps: Deps, result: GithubSuiteResult, repo_name: str, repo_slug: str, own_repo: bool) -> None:
    try:
        deps.record({
            "time": now(), "repo": repo_name, "slug": repo_slug, "month": time.strftime("%Y-%m", time.gmtime()),
            "minutes": result.minutes_used, "machines": result.machines,
            "outcome": result.reason.partition(":")[0] if result.status == "fallback" else result.status,
            "run_id": result.run_id,
            "own_runner": own_repo,
        })
    except Exception:  # noqa: BLE001 - a failed record never changes the result
        pass
