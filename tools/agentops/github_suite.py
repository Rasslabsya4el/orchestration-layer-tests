"""One merge's suite on GitHub machines, or the reason it goes to the PC.

``run_github_suite(...)`` sends the branch to the repo's test copy, takes GitHub machines from the machine-wide line
(blocking until the line gives them), dispatches the copy's ``tests.yml`` split into that many parts, polls it, and
returns a ``GithubSuiteResult``:

* ``status`` ``green`` (every part passed), ``red`` (a part failed a test: ``failed_ids`` / ``full_recheck`` say what
  the PC re-checks) or ``fallback`` (the suite runs on the PC; ``reason`` is ``refused``, ``send-failed``,
  ``dispatch-failed: <exception text>`` (#267), ``poll-failed``, ``no-places``, ``not-started`` or ``lost``, and ``brain_line`` is the one line for the
  area's brain chat). A fallback is never red.
* The machines are released in every path (a failed ``acquire_machines`` holds none). ``record`` is called once, at
  the end, with ``own_runner``; a failure of it is swallowed. A fallback after the dispatch records ``minutes_used``
  0, or the exception's own ``minutes_used`` attribute when it carries one (``poll`` raises none today).
* No state is kept here: the next call asks GitHub again. Without ``own_repo`` an empty ``remote`` is a
  programming error (the caller has no copy and never calls): ``ValueError``.
* ``own_repo`` (a private repo on its own runners, no copy): nothing is sanitized or sent; ``push`` puts the branch
  on the repo's own origin and dispatch and poll run against ``repo_slug``.
* #281: the record's ``minutes`` is the billed minutes: on an own-runner line the Windows part's minutes count twice
  (GitHub bills a private repo's Windows minutes at 2x); a copy's line keeps the wall minutes (its Windows minutes
  are free). ``GithubSuiteResult.minutes_used`` stays the wall minutes.

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
#: The part name `github_run` gives the `test (windows)` job (#255): the one part GitHub bills at 2x on own runners.
WINDOWS_PART = "windows"
_SLUG_RE = re.compile(r"^(?:.*github\.com[:/])?([\w.-]+/[\w.-]+?)(?:\.git)?/?$")


def _record_run(record: dict) -> None:
    from .github_minutes import record_run  # imported when used


    record_run(record)


def _push(root, branch: str, repo_slug: str) -> None:
    """Push ``branch`` to the repo's own origin (forced for that one ref); raises on any failure."""
    subprocess.run(["git", "-C", str(root), "push", "origin", f"+{branch}:refs/heads/{branch}"],
                   capture_output=True, check=True, creationflags=no_window_flags(), env=git_env(),
                   timeout=PUSH_SECONDS)


TREE_CHECK_SECONDS = 60


def _in_tree(root, branch: str, path: str) -> bool:
    """#271: whether ``path`` is a file of ``branch``'s tree in the git repo at ``root``; any failed check raises."""
    done = subprocess.run(["git", "-C", str(root), "ls-tree", "--name-only", branch, "--", path], capture_output=True,
                          creationflags=no_window_flags(), env=git_env(), timeout=TREE_CHECK_SECONDS)
    if done.returncode != 0:
        raise RuntimeError(done.stderr.decode("utf-8", "replace").strip() or f"git ls-tree rc={done.returncode}")
    return bool(done.stdout.strip())


def _real_ids(root, branch: str, ids: list, in_tree: Callable) -> list:
    """#271: ``ids`` without those whose test file is not in the branch's tree; a failed check keeps the id."""
    kept = []
    for test_id in ids:
        try:
            present = in_tree(root, branch, str(test_id).split("::", 1)[0])
        except (RuntimeError, OSError, subprocess.SubprocessError):  # a real id is never dropped on a guess
            present = True
        if present:
            kept.append(test_id)
    return kept


#: #341: the plugin's message for ids sent but not collected (``test_shard_plugin``): the count it lost, then the first
#: ids (at most ten) after the second colon.
_NOT_COLLECTED_RE = re.compile(r"ids sent but not collected: (\d+) of \d+: (\S[^\n]*)")
#: a node id starts a new entry after ``, `` when a path-like token (``x.py`` or ``x.py::``) follows
_NEXT_ID_RE = re.compile(r", (?=[\w./\\-]+(?:::|\.py\b))")


def _not_collected_messages(log: str) -> list[tuple[int, list[str]]]:
    """Each ``ids sent but not collected`` message of ``log`` (every part prints its own): ``(count, ids named)``. The
    ids are joined by ``, ``; the separator line's ``====`` fill after the last id is not part of it."""
    found = []
    for match in _NOT_COLLECTED_RE.finditer(log or ""):
        rest = re.split(r"\s+=+", match.group(2), maxsplit=1)[0].rstrip()
        found.append((int(match.group(1)), _NEXT_ID_RE.split(rest)))
    return found


def not_collected_ids(log: str) -> list[str]:
    """#352 row 8: the ids of every ``ids sent but not collected`` message in a log (each names at most the first ten),
    united in message order, each id once; ``[]`` when there is none."""
    return list(dict.fromkeys(i for _n, ids in _not_collected_messages(log) for i in ids))


def not_collected_count(log: str) -> int:
    """#352 row 8: the ids all those messages say were lost (their ``N``, summed); above ``len(not_collected_ids)`` when
    a message named fewer ids than it lost."""
    return sum(n for n, _ids in _not_collected_messages(log))


def first_not_collected(log: str) -> str:
    """#341: the first id of a part's ``ids sent but not collected`` message in its log, or ``""`` when there is none."""
    return next(iter(not_collected_ids(log)), "")


def _failed_log(copy: str, run_id: int) -> str:
    """The log of a red run's failed steps (``gh run view --log-failed``), read only for a red with no failed id."""
    return github_run.default_gh(["run", "view", str(run_id), "-R", copy, "--log-failed"])


def _not_collected(deps: "Deps", copy: str, run_id: int) -> tuple[list[str], int]:
    """#341, #352: the uncollected ids of a red run's log and the count its messages say; a failed read names none
    (the reason stays as it was)."""
    try:
        log = deps.failed_log(copy, run_id)
        return not_collected_ids(log), not_collected_count(log)
    except Exception:  # noqa: BLE001 - a log that cannot be read never changes the result
        return [], 0


@dataclass
class Deps:
    send: Callable = test_copy_send.send
    acquire_machines: Callable = machine_berths.acquire_machines
    dispatch: Callable = github_run.dispatch
    poll: Callable = github_run.poll
    record: Callable[[dict], None] = _record_run
    push: Callable = _push
    clock: Callable[[], float] = time.monotonic
    in_tree: Callable = _in_tree
    failed_log: Callable = _failed_log


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
    windows_minutes: int = 0  # #281: the Windows part's whole minutes (0 when it did not run)
    not_collected: str = ""  # #341: the first id of a red run's ``ids sent but not collected`` message ("" when none)
    not_collected_ids: list = field(default_factory=list)  # #352: every id those messages name (at most ten each)
    not_collected_count: int = 0  # #352: how many ids those messages say were lost (above the ids named when cut at ten)
    jobs: list = field(default_factory=list)  # #345: each job the reader read: {name, conclusion, failed_ids}
    start_seconds: dict | None = None  # #352: {"linux", "windows"} start-to-first-test seconds of the read (None: no read)


def _windows_minutes(parts: list | None) -> int:
    """#281: the Windows part's minutes, rounded up per part as `github_run` counts them."""
    return sum(math.ceil(int(part.get("seconds") or 0) / 60)
               for part in parts or [] if part.get("part") == WINDOWS_PART)


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
                     ids: list[str] | None = None, windows: bool = False,
                     skip: list[str] | None = None) -> GithubSuiteResult:
    if not own_repo and (not remote or not remote.strip()):
        raise ValueError("run_github_suite needs a test copy remote; a repo without a copy never calls it")
    deps = deps or Deps()
    copy = repo_slug if own_repo else copy_slug(remote)
    result = _run(root, branch, remote, copy, repo_name, priority, settings, deps, own_repo, tests, ids, windows, skip)
    _record(deps, result, repo_name, repo_slug, own_repo)
    return result


#: #267, #345: the longest text of a fallback or red reason; a longer one is cut at it, and a job list past it
#: reads ``and <k> more``.
REASON_MAX = 300


def reason_text(exc: BaseException, *, with_class: bool = False) -> str:
    """#267, #345: the exception's first line of text, cut to ``REASON_MAX``; its class name when it has no text.
    With ``with_class``: ``<class>: <first line>`` (the class alone with no text), cut to ``REASON_MAX``."""
    lines = str(exc).strip().splitlines()
    line = lines[0] if lines else ""
    if with_class:
        return (type(exc).__name__ + (f": {line}" if line else ""))[:REASON_MAX]
    return (line or type(exc).__name__)[:REASON_MAX]


def _count(n: int) -> str:
    return f"{n} job" + ("" if n == 1 else "s")


def job_reason(code: str, jobs: list | None) -> str:
    """#345: the reason of a GitHub result the reader judged with jobs in view: ``<code>: job <name>
    <conclusion or "no conclusion">, ...; ids found: <n>`` over the jobs whose conclusion is not success, cut at
    ``REASON_MAX`` with ``and <k> more``. With every job a success: ``<code>: <k> jobs read, all success; ...``.
    With no job read, ``<code>`` alone (the reason as it was before #345)."""
    jobs = list(jobs or [])
    if not jobs:
        return code
    head = f"{code}: "
    ids_found_text = f"; ids found: {sum(len(job.get('failed_ids') or []) for job in jobs)}"
    failing_jobs = [f"job {job.get('name')} {job.get('conclusion') or 'no conclusion'}"
                    for job in jobs if job.get("conclusion") != "success"]
    if not failing_jobs:
        return f"{head}{_count(len(jobs))} read, all success{ids_found_text}"

    def more(k: int) -> str:
        return f" and {k} more" if k else ""

    kept: list[str] = []
    for item in failing_jobs:
        candidate = kept + [item]
        if len(head + ", ".join(candidate) + more(len(failing_jobs) - len(candidate)) + ids_found_text) > REASON_MAX:
            break
        kept = candidate
    if not kept:
        return f"{head}{_count(len(failing_jobs))} not success{ids_found_text}"
    return f"{head}{', '.join(kept)}{more(len(failing_jobs) - len(kept))}{ids_found_text}"


def _fallback(reason: str, repo_name: str, branch: str, **fields) -> GithubSuiteResult:
    return GithubSuiteResult(status="fallback", reason=reason, brain_line=brain_line(repo_name, branch, reason),
                             **fields)


def _setting(settings: dict, key: str) -> int:
    value = settings.get(key)
    return core_settings.DEFAULTS[key] if value is None else value


def _run(root, branch, remote, copy, repo_name, priority, settings, deps, own_repo, tests=None, ids=None,
         windows=False, skip=None) -> GithubSuiteResult:
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
                                   **({"skip": skip} if skip else {}),
                                   **({"windows": True} if windows else {}))
        except Exception as exc:  # noqa: BLE001 - #267: the exception's text is kept in the reason and the merge.log line
            return _fallback(f"dispatch-failed: {reason_text(exc)}", repo_name, branch, machines=machines)
        try:
            polled = deps.poll(copy, run_id, parts=machines, since=since, **({"windows": True} if windows else {}))
        except Exception as exc:  # noqa: BLE001
            return _fallback("poll-failed", repo_name, branch, machines=machines, run_id=run_id,
                             minutes_used=int(getattr(exc, "minutes_used", 0) or 0))
    finally:
        berth.release()
    fields = dict(machines=machines, run_id=polled.get("run_id", run_id),
                  minutes_used=int(polled.get("minutes_used") or 0),
                  part_seconds=[part.get("seconds", 0) for part in polled.get("parts") or []],
                  windows_minutes=_windows_minutes(polled.get("parts")), jobs=list(polled.get("jobs") or []),
                  start_seconds=polled.get("start_seconds"))
    outcome = polled.get("outcome")
    if outcome == "green":
        return GithubSuiteResult(status="green", **fields)
    if outcome == "red-test":
        polled_ids = list(polled.get("failed_ids") or [])
        failed_ids = _real_ids(root, branch, polled_ids, deps.in_tree)
        full_recheck = bool(polled.get("full_recheck")) or (bool(polled_ids) and not failed_ids)
        uncollected, lost = _not_collected(deps, copy, fields["run_id"]) if full_recheck else ([], 0)  # #341
        return GithubSuiteResult(status="red", failed_ids=failed_ids, full_recheck=full_recheck,
                                 not_collected=uncollected[0] if uncollected else "", not_collected_ids=uncollected,
                                 not_collected_count=lost, **fields)
    # #345: the reason names the jobs the reader read (none read: the bare code, as before)
    return _fallback(job_reason("not-started" if outcome == "not-started" else "lost", polled.get("jobs")),
                     repo_name, branch, **fields)


def _record(deps: Deps, result: GithubSuiteResult, repo_name: str, repo_slug: str, own_repo: bool) -> None:
    # #281: billed minutes. Only an own-runner line is billed: GitHub bills a private repo's Windows part at 2x, while a
    # public test copy's Windows minutes are free and its line stays at the wall minutes.
    billed = result.minutes_used + (result.windows_minutes if own_repo else 0)
    try:
        deps.record({
            "time": now(), "repo": repo_name, "slug": repo_slug, "month": time.strftime("%Y-%m", time.gmtime()),
            "minutes": billed, "machines": result.machines,
            "outcome": result.reason.partition(":")[0] if result.status == "fallback" else result.status,
            "run_id": result.run_id,
            "own_runner": own_repo,
            # #352: the machines' start-to-first-test seconds from the run's own read; null where no such job was read
            "start_seconds": result.start_seconds or {"linux": None, "windows": None},
        })
    except Exception:  # noqa: BLE001 - a failed record never changes the result
        pass
