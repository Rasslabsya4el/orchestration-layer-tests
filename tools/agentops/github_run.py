"""One run of the test copy's `tests.yml`, driven from the PC through `gh` (#232, D119).

Three calls cross this seam:

* ``dispatch(repo, branch, parts, gh)`` starts the workflow on ``branch`` split into ``parts`` and returns the new
  run id: of the ``workflow_dispatch`` runs of that branch created after the dispatch time (minus 5 s of clock
  skew), the oldest.
* ``poll(repo, run_id, gh, ...)`` reads the run (``gh run view``, at most once a minute) until it is settled and
  returns a result record ``{run_id, parts, minutes_used, outcome, failed_ids, full_recheck, jobs}``. ``jobs`` (#345)
  lists one ``{name, conclusion, failed_ids}`` per job of the last read, as the reader saw it: ``name`` the job's
  name, ``conclusion`` its conclusion as read (``""`` while none), ``failed_ids`` the ids its log named (``[]`` in
  every outcome but ``red-test``). A job read ``completed`` with no conclusion is read again, up to
  ``_CONCLUSION_READS`` further reads one poll interval apart; it is ``lost`` only if still empty after them.
  ``parts`` lists one
  ``{part, state, started, ended, seconds}`` per ``test`` job (a job concluded ``skipped`` is none, and
  ``test (windows)`` is one only when ``windows`` was asked, #255); ``state`` is ``queued`` / ``running`` /
  ``success`` / ``failure`` (the pytest step failed) / ``lost`` (any other end: setup, checkout, install, runner
  lost, cancelled).
  ``minutes_used`` is the minutes the run has cost up to the poll's last read: every part that has a ``started``
  time counts its seconds (``ended`` minus ``started``, or the last read's time minus ``started`` for a part still
  running, so a lost or early-ended poll counts it too), rounded up to whole minutes per part, plus the plan job
  (and any other job that is not a part) as its own minutes, rounded up the same way, at least 1 once started.
  A queued part counts 0. ``outcome`` is:

  - ``green``: every part succeeded (and, when ``parts`` is given, that many parts ran);
  - ``red-test``: no part was lost and a part failed in its pytest step; the PC re-checks the tests named in
    ``failed_ids`` (read through ``failed_ids_by_job``, flattened by ``_flat_ids``); when that list is empty (no id
    named, e.g. a collection error without a path line) or its log cannot be read, ``full_recheck`` is True and the PC
    re-runs everything. ``failed_ids`` is ``[]`` and
    ``full_recheck`` False in every other outcome;
  - ``lost``: a part (or the plan job, or the run) ended for a non-test reason, or the run outran ``run_timeout_s``.
    ``lost`` wins over ``red-test``: the suite then runs whole on the PC anyway. A lost part ends the poll at once;
  - ``not-started``: no part had a ``startedAt`` within ``start_timeout_s`` (10 min) of the dispatch.
  ``start_seconds`` (#352) is ``start_to_first_test_seconds`` of the same read: ``{"linux": s|None, "windows": s|None}``.
* ``failed_test_ids(repo, run_id, gh)`` reads the node ids of failed tests from ``gh run view --log-failed``
  (``FAILED <id> - <message>`` or ``FAILED <id>``; ``ERROR <id>`` likewise, or ``ERROR <path>.py`` for a collection
  error with no ``::``, whose path is its id; an id may hold spaces inside brackets; only lines at the start of the
  output line count, #345); ``failed_ids_by_job`` gives the same per job.

``gh`` is ONE injectable callable ``gh(args: list[str]) -> str`` returning stdout and raising on a failed call;
the default goes through ``gh_call.run_gh`` (no console window). The clock, the sleep and the wall clock are
injectable, so a test never waits real time. Standard library plus ``.util`` and ``.gh_call`` only.
"""

from __future__ import annotations

import base64
import gzip
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from .util import AgentOpsError

#: ``gh(args)`` or, for a call that carries a body on stdin, ``gh(args, input=body)``.
Gh = Callable[..., str]

WORKFLOW = "tests.yml"
#: `poll` reads the run at most this often.
POLL_INTERVAL_S = 60.0
#: No part started this long after the dispatch: the outcome is `not-started`.
START_TIMEOUT_S = 600.0
#: A run that is still not settled this long after the dispatch is read as `lost` (a job runs 6 h at most).
RUN_TIMEOUT_S = 3 * 3600.0
#: `failed_test_ids` returns at most this many ids.
MAX_FAILED_IDS = 200
#: `dispatch` looks for the new run this many times, `_FIND_WAIT_S` apart.
_FIND_TRIES = 6
_FIND_WAIT_S = 5.0
#: A run created this much before the dispatch time still counts (clock skew between the PC and GitHub).
_SKEW_S = 5.0
#: Consecutive failed `gh run view` calls before `poll` gives up.
_MAX_VIEW_FAILURES = 5
#: A job seen completed with no conclusion yet (#345) is read again this many times, one poll interval apart; still
#: empty after them, it is `lost` as before.
_CONCLUSION_READS = 3

#: The `<job>\t<step>\t<timestamp> ` prefix `gh run view --log-failed` puts on every line.
_LOG_PREFIX_RE = re.compile(r"^[^\t]*\t[^\t]*\t\S+ ")
#: pytest's short summary line, at the start of the line (#345): `FAILED <path>.py::<name>` (the `::` is required), or
#: `ERROR <path>.py::<name>` or `ERROR <path>.py` (a collection error has no `::`; its path is its id), each then
#: ` - <message>` or the end of the line. An indented line is a test's own output (a nested run's, or an echo), never
#: a result.
_FAILED_RE = re.compile(r"^(?:FAILED (?P<failed>\S+\.py::.+?)|ERROR (?P<error>\S+\.py(?:::.+?)?))(?: - .*)?$")
#: pytest's `short test summary info` header line, at the start of the line (#271: the outer run's is the job's last;
#: #345: a nested run's header is indented and resets nothing).
_SUMMARY_RE = re.compile(r"^=+ short test summary info =+\s*$")
_PART_RE = re.compile(r"^test\b\s*(?:\((.*)\))?\s*$")


#: GitHub's cap on the whole of a `workflow_dispatch` payload's inputs (65,535 characters; from GitHub's changelog,
#: not re-verified offline). Past it the API refuses the dispatch, so `dispatch` raises before sending.
MAX_INPUTS_CHARS = 65535


def default_gh(args: list[str], input: str | None = None) -> str:
    """Run the real ``gh`` through the repo's adapter and return stdout; a failed call raises. ``input`` is the
    stdin body (no command-line size limit applies to it)."""

    from .gh_call import run_gh

    return run_gh(args, input=input, check=True).stdout


def _parse_time(value: str | None) -> datetime | None:
    """A GitHub timestamp, or None for an absent one and for Go's zero time (a job that never started)."""

    if not value or value.startswith("0001-"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _stamp(moment: datetime | None) -> str | None:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def encode_ids(ids: list[str]) -> str:
    """The ``ids`` input: base64 (standard, one line) of the gzip of the node ids joined by newlines (utf-8)."""

    return base64.b64encode(gzip.compress("\n".join(ids).encode("utf-8"))).decode("ascii")


def dispatch_inputs(branch: str, parts: int, ids: list[str] | None = None, windows: bool = False,
                    skip: list[str] | None = None) -> dict[str, str]:
    """The ``workflow_dispatch`` inputs ``dispatch`` sends for one run (#271: the one place their size is read)."""

    inputs = {"ref": branch, "parts": str(parts)}
    if ids:
        inputs["ids"] = encode_ids(ids)
    if windows:
        inputs["windows"] = "true"
    if skip:
        inputs["skip"] = encode_ids(skip)
    return inputs


def inputs_fit(branch: str, parts: int, ids: list[str] | None = None, windows: bool = False,
               skip: list[str] | None = None) -> bool:
    """#271: whether ``dispatch`` with these arguments stays within ``MAX_INPUTS_CHARS``; the size it checks, computed
    by ``dispatch_inputs`` too, so the caller's decision and the dispatch cannot drift."""

    return sum(len(value) for value in dispatch_inputs(branch, parts, ids, windows, skip).values()) <= MAX_INPUTS_CHARS


def dispatch(repo: str, branch: str, parts: int, gh: Gh = default_gh, *,
             wall: Callable[[], datetime] = _utc_now, sleep: Callable[[float], None] = time.sleep,
             ids: list[str] | None = None, windows: bool = False, skip: list[str] | None = None) -> int:
    """Start ``tests.yml`` on ``branch`` split into ``parts`` and return the new run's id. The inputs go as JSON on
    stdin (``gh workflow run --json``), never as ``-f`` argv: a 1,500-id selection is ~40,000 encoded characters, past
    the 32,767-character Windows command line (#267). A non-empty ``ids`` sends ``ids=<encode_ids>`` (the run keeps
    only those tests); ``windows`` sends ``windows=true`` (the PC-only files run in a ``test (windows)`` job); a
    non-empty ``skip`` sends ``skip=<encode_ids>`` (#260: tests the Windows job leaves out, the merge runs them on the
    PC). Raises ``AgentOpsError`` when the inputs are over ``MAX_INPUTS_CHARS`` (nothing is sent, never a truncated
    list) and when no run created after the dispatch shows up in the list."""

    sent_at = wall()
    inputs = dispatch_inputs(branch, parts, ids, windows, skip)
    size = sum(len(value) for value in inputs.values())
    if size > MAX_INPUTS_CHARS:
        raise AgentOpsError(f"the inputs of {WORKFLOW} on {branch} are {size:,} characters, over GitHub's limit of "
                            f"{MAX_INPUTS_CHARS:,} per dispatch ({len(ids or [])} ids, {len(skip or [])} skipped)")
    gh(["workflow", "run", WORKFLOW, "-R", repo, "--ref", branch, "--json"],
       input=json.dumps(inputs))  # gh --json: the body IS the inputs map
    threshold = sent_at - timedelta(seconds=_SKEW_S)
    for attempt in range(_FIND_TRIES):
        listed = json.loads(gh(["run", "list", "-R", repo, "--workflow", WORKFLOW, "--branch", branch,
                                "--json", "databaseId,status,createdAt,event", "--limit", "5"]) or "[]")
        fresh = [(created, run["databaseId"]) for run in listed
                 if run.get("event") == "workflow_dispatch"
                 and (created := _parse_time(run.get("createdAt"))) and created >= threshold]
        if fresh:
            return min(fresh)[1]
        if attempt + 1 < _FIND_TRIES:
            sleep(_FIND_WAIT_S)
    raise AgentOpsError(f"no run of {WORKFLOW} on {branch} in {repo} appeared after the dispatch")


def _part_name(job_name: str) -> int | str | None:
    """``test (3)`` -> 3, ``test (arm)`` -> ``arm``, ``test`` -> ``test``; None for a job that is not a part."""

    match = _PART_RE.match(job_name.strip())
    if not match:
        return None
    label = (match.group(1) or "test").strip()
    return int(label) if label.isdigit() else label


def _is_pytest_step(name: str) -> bool:
    """The step that runs pytest (the workflow's `pytest part`): its name has `pytest` in it, and it is not the
    install step (`Run pip install pytest ...`) nor a `Post ...` clean-up step, which name pytest too."""

    lowered = name.lower()
    return "pytest" in lowered and "install" not in lowered and not lowered.startswith("post ")


def _pytest_step_failed(job: dict) -> bool:
    return any(_is_pytest_step(str(step.get("name", ""))) and step.get("conclusion") == "failure"
               for step in job.get("steps") or [])


def _job_seconds(job: dict, now: datetime) -> tuple[datetime | None, datetime | None, int]:
    """(started, ended, seconds) of a job; a started job that has not ended counts up to ``now``."""

    started = _parse_time(job.get("startedAt"))
    ended = _parse_time(job.get("completedAt")) if job.get("status") == "completed" else None
    if started is None:
        return None, ended, 0
    return started, ended, max(0, round(((ended or now) - started).total_seconds()))


def _part_record(name: int | str, job: dict, now: datetime) -> dict:
    started, ended, seconds = _job_seconds(job, now)
    if job.get("status") == "completed":
        conclusion = job.get("conclusion")
        if conclusion == "success":
            state = "success"
        elif conclusion == "failure" and _pytest_step_failed(job):
            state = "failure"
        else:
            state = "lost"
    else:
        state = "running" if started else "queued"
    return {"part": name, "state": state, "started": _stamp(started), "ended": _stamp(ended), "seconds": seconds}


def _parts_of(view: dict, now: datetime, windows: bool = False) -> list[dict]:
    """The parts of a run (#255): a job concluded `skipped` is not one, and the windows part is one only when
    ``windows`` was requested."""
    parts = []
    for job in view.get("jobs") or []:
        if job.get("conclusion") == "skipped":
            continue
        name = _part_name(str(job.get("name", "")))
        if name is None or (name == "windows" and not windows):
            continue
        parts.append(_part_record(name, job, now))
    return parts


def _minutes_used(view: dict, parts: list[dict], now: datetime) -> int:
    """Whole minutes per started part, plus the jobs that are not parts (the plan job) at 1 minute at least."""

    others = 0
    for job in view.get("jobs") or []:
        if _part_name(str(job.get("name", ""))) is None:
            started, _, seconds = _job_seconds(job, now)
            others += max(1, math.ceil(seconds / 60)) if started else 0
    return sum(math.ceil(part["seconds"] / 60) for part in parts) + others


def _non_part_job_lost(view: dict) -> bool:
    """A job that is not a part (the plan job) ended without success: the matrix never ran."""

    return any(_part_name(str(job.get("name", ""))) is None and job.get("status") == "completed"
               and job.get("conclusion") not in ("success", "skipped") for job in view.get("jobs") or [])


def _jobs_all_done(view: dict, parts: list[dict], expected: int | None) -> bool:
    """Every job is done: the plan job and all ``expected`` parts are completed. GitHub can keep the run itself
    not-completed for many minutes after that, so the jobs decide. Without ``expected`` the jobs cannot be known to
    be all there: False."""

    if expected is None or len(parts) != expected:
        return False
    others = [job for job in view.get("jobs") or [] if _part_name(str(job.get("name", ""))) is None]
    if not others or any(job.get("status") != "completed" for job in others):
        return False
    return all(part["state"] in ("success", "failure", "lost") for part in parts)


def _settled_outcome(view: dict, parts: list[dict], expected: int | None) -> str | None:
    """The outcome once the read shows one, else None (keep polling). A lost part ends the poll at once; the rest
    wait for the whole run, or for every job to be completed when the run's own status lags behind."""

    states = [part["state"] for part in parts]
    if "lost" in states or _non_part_job_lost(view):
        return "lost"
    if view.get("status") != "completed" and not _jobs_all_done(view, parts, expected):
        return None
    if "failure" in states:
        return "red-test"
    if not parts or (expected is not None and len(parts) != expected) or any(s != "success" for s in states):
        return "lost"
    return "green"


def median_seconds(values: list[int]) -> int | None:
    """The median of whole seconds, rounded up for an even count (as `github_minutes.median_merge_minutes`); None when
    there is none."""

    if not values:
        return None
    tail = sorted(values)
    mid = len(tail) // 2
    if len(tail) % 2:
        return tail[mid]
    return math.ceil((tail[mid - 1] + tail[mid]) / 2)


def start_to_first_test_seconds(run_json: dict) -> dict:
    """#352: the seconds from the run's start (``createdAt``) to each test job's first pytest step (``startedAt``): the
    wait for a machine plus its setup. ``run_json`` is the parsed ``gh run view --json createdAt,jobs``. A job whose
    name holds ``windows`` is the windows kind, every other job the linux kind; a job with no pytest step started (the
    plan job, a job that never got its machine) is left out. The value of a kind is the median of its jobs; None when
    the run has no such job."""

    created = _parse_time(run_json.get("createdAt"))
    per_kind: dict[str, list[int]] = {"linux": [], "windows": []}
    for job in run_json.get("jobs") or []:
        pytest_steps = [step for step in job.get("steps") or [] if _is_pytest_step(str(step.get("name", "")))]
        started = _parse_time(pytest_steps[0].get("startedAt")) if pytest_steps else None
        if created is None or started is None:
            continue
        kind = "windows" if "windows" in str(job.get("name", "")).lower() else "linux"
        per_kind[kind].append(max(0, round((started - created).total_seconds())))
    return {kind: median_seconds(values) for kind, values in per_kind.items()}


def _has_unconcluded_job(view: dict) -> bool:
    """A job that is completed with no conclusion yet (#345): GitHub can show it so for a moment; `poll` reads again."""

    return any(job.get("status") == "completed" and not job.get("conclusion") for job in view.get("jobs") or [])


def poll(repo: str, run_id: int, gh: Gh = default_gh, *, parts: int | None = None,
         interval_s: float = POLL_INTERVAL_S, start_timeout_s: float = START_TIMEOUT_S,
         run_timeout_s: float = RUN_TIMEOUT_S, since: float | None = None,
         clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
         wall: Callable[[], datetime] = _utc_now, windows: bool = False) -> dict:
    """Read the run every ``interval_s`` until it is settled; return the result record (module docstring).

    ``parts`` is the number of parts asked for (a green run with fewer is ``lost``); with ``windows`` the run has one
    more job, ``test (windows)``, which counts as a part (green needs it green, its failed ids are collected).
    ``since`` is the ``clock()``
    value of the dispatch (default: the first read). Raises ``AgentOpsError`` after ``_MAX_VIEW_FAILURES`` failed
    reads in a row; one failed read is retried at the next interval."""

    begin = clock() if since is None else since
    expected = parts + 1 if windows and parts is not None else parts
    failures = 0
    unconcluded_reads = 0  # consecutive reads that showed a job completed with no conclusion (#345)
    while True:
        try:
            view = json.loads(gh(["run", "view", str(run_id), "-R", repo, "--json", "status,conclusion,jobs,createdAt"]))
        except Exception as exc:  # noqa: BLE001 - a flaky read is retried, a dead one raises below
            failures += 1
            if failures >= _MAX_VIEW_FAILURES:
                raise AgentOpsError(f"gh run view {run_id} in {repo} failed {failures} times in a row: {exc}") from exc
            sleep(interval_s)
            continue
        failures = 0
        if _has_unconcluded_job(view):
            if unconcluded_reads < _CONCLUSION_READS:
                unconcluded_reads += 1
                sleep(interval_s)
                continue
        else:
            unconcluded_reads = 0
        now = wall()
        records = _parts_of(view, now, windows)
        outcome = _settled_outcome(view, records, expected)
        if outcome is None:
            elapsed = clock() - begin
            if not any(part["started"] for part in records) and elapsed >= start_timeout_s:
                outcome = "not-started"
            elif elapsed >= run_timeout_s:
                outcome = "lost"
        if outcome is not None:
            by_job: dict[str, list[str]] = {}
            if outcome == "red-test":
                try:
                    by_job = failed_ids_by_job(repo, run_id, gh)
                except Exception:  # noqa: BLE001 - an unreadable log is a full re-check on the PC
                    by_job = {}
            failed_ids = _flat_ids(by_job)
            jobs = [{"name": str(job.get("name", "")), "conclusion": job.get("conclusion") or "",
                     "failed_ids": by_job.get(str(job.get("name", "")), [])} for job in view.get("jobs") or []]
            return {"run_id": run_id, "parts": records, "minutes_used": _minutes_used(view, records, now),
                    "outcome": outcome, "failed_ids": failed_ids,
                    "full_recheck": outcome == "red-test" and not failed_ids, "jobs": jobs,
                    "start_seconds": start_to_first_test_seconds(view)}  # #352: from this same read
        sleep(interval_s)


def failed_ids_by_job(repo: str, run_id: int, gh: Gh = default_gh) -> dict[str, list[str]]:
    """The node ids of the failed tests of a red run's log, per job name (#345): ``FAILED`` and ``ERROR`` lines
    (``<id>`` then ``- <message>`` or the end of the line) at the start of a line, without repeats, in first-seen order.

    Only a line at the start of the output line counts: an indented line is a test's own output (a nested pytest's
    summary, its ``ERROR`` lines, an echo) and adds nothing. Per job only the ids after its last top-level ``short test
    summary info`` header count: the outer pytest prints its summary last, and an inner pytest's lines (a test run on a
    temp project) sit above it in the captured output; an indented, nested header resets nothing (#271, #345). A job
    with ids and no header keeps all of them. The ids are not capped here (``failed_test_ids`` caps them)."""

    log = gh(["run", "view", str(run_id), "-R", repo, "--log-failed"])
    every: dict[str, list[str]] = {}  # job -> every id of its log
    block: dict[str, list[str]] = {}  # job -> the ids after its last summary header; absent = no header
    for raw in log.splitlines():
        text = raw.rstrip()
        job = text.split("\t", 1)[0] if _LOG_PREFIX_RE.match(text) else ""
        body = _LOG_PREFIX_RE.sub("", text, count=1)
        if _SUMMARY_RE.match(body):
            block[job] = []
            continue
        match = _FAILED_RE.match(body)
        if match:
            test_id = (match.group("failed") or match.group("error")).rstrip()
            every.setdefault(job, []).append(test_id)
            if job in block:
                block[job].append(test_id)
    return {job: list(dict.fromkeys(block.get(job, found))) for job, found in every.items()}


def _flat_ids(by_job: dict[str, list[str]]) -> list[str]:
    """The ids of every job, grouped by job in first-seen order, without repeats, at most ``MAX_FAILED_IDS``."""

    ids: dict[str, None] = {}
    for found in by_job.values():
        for test_id in found:
            ids.setdefault(test_id)
    return list(ids)[:MAX_FAILED_IDS]


def failed_test_ids(repo: str, run_id: int, gh: Gh = default_gh) -> list[str]:
    """The node ids of the failed tests in a red run's log, grouped by job in first-seen order, without repeats, at
    most ``MAX_FAILED_IDS``; the rules of ``failed_ids_by_job``."""

    return _flat_ids(failed_ids_by_job(repo, run_id, gh))
