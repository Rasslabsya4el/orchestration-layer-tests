"""#343 unit 4: the test map built on GitHub and installed on the PC.

``build_on_github(root=, branch=, sha=, settings=, map_dir=, deps=)`` sends the branch to the repo's test copy (the
merge suite's way: ``test_copy_send.send``, or the repo's own origin when ``github_own_runners`` is set and no copy
is named), dispatches the copy's ``map.yml`` with the input ``ref``, polls the run to its end, and downloads the
artifact ``test-map`` (files ``testmondata``, ``reads/**``, ``meta.json``). It returns a ``MapBuildResult``:

* ``green``: the run succeeded and the artifact's meta says green; the map is installed into ``map_dir``, its
  ``meta.json`` stamped with the working repo's sha (``map_commit``, ``last_green_commit``) and ``total``;
* ``red``: the run failed and the artifact's meta says ``green`` is False; nothing is installed; ``failed_ids`` are
  read from that meta (a red meta with no ids is the ``red without ids`` fallback). A red build holds the merge line,
  as the PC build does today;
* ``fallback`` (never red): ``refused: ...``, ``send-failed``, ``no-places`` (no test copy and no own runners, or a
  remote with no ``<owner>/<name>``), ``dispatch-failed: ...``, ``poll-failed``, ``lost`` (a cancelled or timed-out
  run), ``no-artifact``, ``download-failed: ...``, ``wrong-sha`` (the artifact was not built from the copy commit the
  send pushed), ``run-failed-no-map`` (the run failed and its artifact does not say green is False),
  ``red without ids`` (it does, but names no failed test),
  ``install-failed: ...``.

The install is a swap: the artifact is extracted into a sibling temporary folder of ``map_dir`` first, and only a
complete, readable map replaces the old one (renamed aside, then the new folder renamed in). A failed or partial
download leaves ``map_dir`` exactly as it was.

Every run that reached the dispatch (and every fallback) is recorded once through ``github_suite._record_run`` with
``kind`` ``map-build``, the way the merge suite's record is written; a failed record is named in
``MapBuildResult.record_error`` and never changes the result. Every seam is injectable through ``Deps``: the
``gh`` callable, the clock, the wall clock, the sleep, the send, the artifact lookup, the download and the extract.
No repo name, path or test name is written here.

Stdlib, ``github_run``, ``github_suite``, ``test_copy_send``, ``gh_call`` and ``util`` only.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import time
import uuid
import zipfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import gh_call, github_run, github_suite, test_copy_send, workflow_sync
from .util import AgentOpsError, now

MAP_WORKFLOW = workflow_sync.MAP_WORKFLOW
MAP_ARTIFACT = "test-map"
MAP_KIND = "map-build"
META_FILE = "meta.json"
#: The map build is one GitHub job, so it takes one machine in the record.
MAP_MACHINES = 1
_TEXT_MAX = 300
_REMOTE_KEY = "test_copy_remote"
_OWN_KEY = "github_own_runners"


@dataclass(frozen=True)
class MapBuildResult:
    status: str
    reason: str = ""
    minutes_used: int = 0
    built_at: str | None = None
    green: bool = False
    run_id: int | None = None
    failed_ids: list = field(default_factory=list)
    record_error: str = ""  # the first line of a failed minutes record; "" when the record was written


def _first_line(exc: BaseException) -> str:
    lines = str(exc).strip().splitlines()
    return (lines[0] if lines else type(exc).__name__)[:_TEXT_MAX]


def dispatch_map(repo: str, branch: str, gh: Callable, *, wall: Callable = github_run._utc_now,
                 sleep: Callable[[float], None] = time.sleep) -> int:
    """Start ``map.yml`` on ``branch`` with the input ``ref`` (JSON on stdin) and return the new run's id: the oldest
    ``workflow_dispatch`` run of that branch created after the dispatch (minus the clock skew). Raises
    ``AgentOpsError`` when no such run shows up. Before the dispatch the registry's workflow files are made to exist
    on the repo's live default branch (``workflow_sync.ensure_workflows``, #343 rule 1); its failure propagates."""
    workflow_sync.prepare_dispatch(repo, gh)
    sent_at = wall()
    gh(["workflow", "run", MAP_WORKFLOW, "-R", repo, "--ref", branch, "--json"], input=json.dumps({"ref": branch}))
    threshold = sent_at - timedelta(seconds=github_run._SKEW_S)
    for attempt in range(github_run._FIND_TRIES):
        listed = json.loads(gh(["run", "list", "-R", repo, "--workflow", MAP_WORKFLOW, "--branch", branch,
                                "--json", "databaseId,status,createdAt,event", "--limit", "5"]) or "[]")
        fresh = [(created, run["databaseId"]) for run in listed
                 if run.get("event") == "workflow_dispatch"
                 and (created := github_run._parse_time(run.get("createdAt"))) and created >= threshold]
        if fresh:
            return min(fresh)[1]
        if attempt + 1 < github_run._FIND_TRIES:
            sleep(github_run._FIND_WAIT_S)
    raise AgentOpsError(f"no run of {MAP_WORKFLOW} on {branch} in {repo} appeared after the dispatch")


def _minutes(view: dict, at: datetime) -> int:
    """Whole minutes per started job (a running job counts up to ``at``), at least 1 for a started job."""
    total = 0
    for job in view.get("jobs") or []:
        started = github_run._parse_time(job.get("startedAt"))
        if started is None:
            continue
        ended = github_run._parse_time(job.get("completedAt")) or at
        total += max(1, math.ceil(max(0, (ended - started).total_seconds()) / 60))
    return total


def poll_map(repo: str, run_id: int, gh: Callable, *, since: float | None = None,
             clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
             wall: Callable[[], datetime] = github_run._utc_now) -> dict:
    """Read the run every ``POLL_INTERVAL_S`` until it is completed; return ``{run_id, outcome, minutes_used}`` with
    outcome ``green`` (conclusion success), ``red`` (conclusion failure) or ``lost`` (any other conclusion, a run that
    never started within ``START_TIMEOUT_S``, or one still running past ``RUN_TIMEOUT_S``). Raises ``AgentOpsError``
    after ``_MAX_VIEW_FAILURES`` failed reads in a row."""
    begin = clock() if since is None else since
    failures = 0
    while True:
        try:
            view = json.loads(gh(["run", "view", str(run_id), "-R", repo, "--json", "status,conclusion,jobs"]))
        except Exception as exc:  # noqa: BLE001 - a flaky read is retried, a dead one raises below
            failures += 1
            if failures >= github_run._MAX_VIEW_FAILURES:
                raise AgentOpsError(f"gh run view {run_id} in {repo} failed {failures} times in a row: {exc}") from exc
            sleep(github_run.POLL_INTERVAL_S)
            continue
        failures = 0
        minutes = _minutes(view, wall())
        if view.get("status") == "completed":
            conclusion = view.get("conclusion")
            outcome = {"success": "green", "failure": "red"}.get(conclusion, "lost")
            return {"run_id": run_id, "outcome": outcome, "minutes_used": minutes}
        elapsed = clock() - begin
        started_any = any(github_run._parse_time(job.get("startedAt")) for job in view.get("jobs") or [])
        if elapsed >= github_run.RUN_TIMEOUT_S or (not started_any and elapsed >= github_run.START_TIMEOUT_S):
            return {"run_id": run_id, "outcome": "lost", "minutes_used": minutes}
        sleep(github_run.POLL_INTERVAL_S)


def find_artifact(repo: str, run_id: int, gh: Callable) -> int | None:
    """The id of the run's ``test-map`` artifact when it exists and has not expired, else None."""
    listed = json.loads(gh(["api", f"repos/{repo}/actions/runs/{run_id}/artifacts"]) or "{}")
    for artifact in listed.get("artifacts") or []:
        if artifact.get("name") == MAP_ARTIFACT and not artifact.get("expired"):
            return int(artifact["id"])
    return None


def download_zip(repo: str, artifact_id: int, archive: Path) -> None:
    """The artifact's zip, written whole to ``archive`` (raw bytes through the gh adapter); raises when gh does."""
    body = gh_call.run_gh(["api", f"repos/{repo}/actions/artifacts/{artifact_id}/zip"], text=False,
                          check=True).stdout
    Path(archive).write_bytes(body)


def extract_zip(archive: Path, staging: Path) -> None:
    """Extract the artifact zip into the new folder ``staging``; a zip that does not open or has a bad member raises."""
    staging.mkdir(parents=True)
    with zipfile.ZipFile(archive) as zf:
        if zf.testzip() is not None:
            raise ValueError("the artifact zip has a corrupt member")
        zf.extractall(staging)


def read_meta(folder: Path) -> dict:
    """``meta.json`` of an extracted map; raises when it is absent, not JSON, or not a dict."""
    data = json.loads((Path(folder) / META_FILE).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("meta.json is not an object")
    return data


def _stamp_map(folder: Path, meta: dict, sha: str) -> None:
    """Rewrite the staged ``meta.json`` for this repo: ``map_commit`` (and ``last_green_commit`` on a green map) is the
    working repo's ``sha``, not the copy's; ``total`` is ``total``, else ``tests``. The test selection reads these keys."""
    total = meta.get("total")
    if total is None:
        total = meta.get("tests")
    stamped = {**meta, "map_commit": sha, "total": int(total or 0)}
    if meta.get("green") is True:
        stamped["last_green_commit"] = sha
    (Path(folder) / META_FILE).write_text(json.dumps(stamped), encoding="utf-8")


#: Files of the old map folder that never go into the new one: the old database's own sidecars (a stale ``-wal`` / ``-shm``
#: beside the new ``testmondata`` would be replayed into it).
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _carry_state(old: Path, new: Path) -> None:
    """Copy the files of ``old`` that ``new`` does not hold into it: the map folder also keeps the map run's own state
    (its lock, the pending shas, the build start, the red marker, saved build outputs), and the swap must not drop it.
    Never the old database's sidecars."""
    for entry in old.iterdir():
        if (entry.is_file() and not entry.name.endswith(_SIDECAR_SUFFIXES)
                and not (new / entry.name).exists()):
            shutil.copy2(entry, new / entry.name)


def _install(staged: Path, map_dir: Path) -> None:
    """Swap ``staged`` in as ``map_dir``: the old folder is renamed aside first and put back if the new one cannot be
    renamed in, so ``map_dir`` is always whole. Then the old folder's run-state files go into the new one where it holds
    none (after the swap, so a file written into the old folder until the rename is not lost), and the old folder is
    deleted."""
    map_dir.parent.mkdir(parents=True, exist_ok=True)
    old = map_dir.parent / f"{map_dir.name}.old-{uuid.uuid4().hex[:8]}"
    had_old = map_dir.exists()
    if had_old:
        os.rename(map_dir, old)
    try:
        os.rename(staged, map_dir)
    except OSError:
        if had_old:
            os.rename(old, map_dir)
        raise
    if had_old:
        if old.is_dir():
            _carry_state(old, map_dir)
        shutil.rmtree(old, ignore_errors=True)


@dataclass
class Deps:
    send: Callable = test_copy_send.send
    push: Callable = github_suite._push
    dispatch: Callable = dispatch_map
    poll: Callable = poll_map
    find_artifact: Callable = find_artifact
    download: Callable = download_zip
    extract: Callable = extract_zip
    record: Callable[[dict], None] = github_suite._record_run
    gh: Callable = github_run.default_gh
    clock: Callable[[], float] = time.monotonic
    wall: Callable[[], datetime] = github_run._utc_now
    sleep: Callable[[float], None] = time.sleep


def _fetch(repo: str, run_id: int, work: Path, staging: Path, deps: Deps) -> Path | None:
    """Download the run's artifact zip into ``work`` and extract it into ``staging`` (a sibling of the map folder);
    returns ``staging``, or None when there is no artifact."""
    artifact_id = deps.find_artifact(repo, run_id, deps.gh)
    if artifact_id is None:
        return None
    archive = work / "test-map.zip"
    deps.download(repo, artifact_id, archive)
    deps.extract(archive, staging)
    return staging


def _artifact(repo: str, run_id: int, work: Path, staging: Path, deps: Deps) -> tuple[Path | None, dict | None]:
    """The run's artifact extracted into ``staging`` and its meta read: ``(folder, meta)``, or ``(None, None)`` when the
    run has none. Raises when the download, the extract or the read fails."""
    staged = _fetch(repo, run_id, work, staging, deps)
    if staged is None:
        return None, None
    return staged, read_meta(staged)


def _fallback(reason: str, **fields) -> MapBuildResult:
    return MapBuildResult(status="fallback", reason=reason, **fields)


def is_own(settings: dict) -> bool:
    """The repo's own origin takes the run: own runners are on and no test copy is named."""
    return settings.get(_OWN_KEY) is True and not str(settings.get(_REMOTE_KEY) or "").strip()


def _place(settings: dict, remote: str, own: bool) -> str:
    """The ``<owner>/<name>`` the run goes to: the repo's own, or the test copy's; ValueError when neither is named."""
    return str(settings.get("repo") or "") if own else github_suite.copy_slug(remote)


def _other_copy(meta: dict, copy_sha: str | None) -> bool:
    """True when the artifact names a sha and the copy commit the send pushed is known, and the two differ."""
    built = str(meta.get("sha") or "")
    return bool(built and copy_sha and built != copy_sha)


def build_on_github(*, root, branch: str, sha: str, settings: dict, map_dir, deps: Deps | None = None,
                    repo_name: str | None = None) -> MapBuildResult:
    """Build the test map for ``branch`` on GitHub and install it into ``map_dir`` on green (see the module docstring).
    ``repo_name`` is the repo's folder name the minutes are counted under (``merge_github.Plan.repo_name``); without it
    the record keeps the basename of the settings' ``repo``."""
    deps = deps or Deps()
    result = _build(root, branch, sha, settings, Path(map_dir), deps)
    record_error = _record(deps, result, settings, is_own(settings), sha, repo_name)
    return replace(result, record_error=record_error)


def _build(root, branch: str, sha: str, settings: dict, map_dir: Path, deps: Deps) -> MapBuildResult:
    remote = str(settings.get(_REMOTE_KEY) or "").strip()
    own = is_own(settings)
    try:
        repo = _place(settings, remote, own)
    except ValueError:  # no test copy named and no own runners, or a remote with no <owner>/<name>
        return _fallback("no-places")
    try:
        if own:
            deps.push(root, branch, repo)
            sent_ok, refusal, copy_sha = True, None, sha  # the pushed branch is the working commit itself
        else:
            sent = deps.send(root, branch, remote)
            sent_ok, refusal, copy_sha = sent.sent, getattr(sent, "reason", None), getattr(sent, "sha", None)
    except Exception:  # noqa: BLE001 - any send or push failure sends the build to the fallback
        return _fallback("send-failed")
    if not sent_ok:
        return _fallback(f"refused: {refusal}" if refusal else "refused")
    since = deps.clock()
    try:
        run_id = deps.dispatch(repo, branch, deps.gh, wall=deps.wall, sleep=deps.sleep)
    except Exception as exc:  # noqa: BLE001 - the exception's first line is kept in the reason and the record
        return _fallback(f"dispatch-failed: {_first_line(exc)}")
    try:
        polled = deps.poll(repo, run_id, deps.gh, since=since, clock=deps.clock, sleep=deps.sleep, wall=deps.wall)
    except Exception:  # noqa: BLE001
        return _fallback("poll-failed", run_id=run_id)
    minutes = int(polled.get("minutes_used") or 0)
    outcome = polled.get("outcome")
    if outcome not in ("green", "red"):
        return _fallback("lost", minutes_used=minutes, run_id=run_id)
    tag = uuid.uuid4().hex[:8]
    work = map_dir.parent / f"{map_dir.name}.build-{tag}"
    staging = map_dir.parent / f"{map_dir.name}.map-{tag}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        return _from_artifact(outcome, map_dir, repo, run_id, minutes, copy_sha, sha, deps, work, staging)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)  # gone already when installed; a fallback leaves none behind


def _from_artifact(outcome: str, map_dir: Path, repo: str, run_id: int, minutes: int, copy_sha: str | None,
                   sha: str, deps: Deps, work: Path, staging: Path) -> MapBuildResult:
    """The result of a finished run (``green`` or ``red``) from its artifact: every read or check that fails is a fallback."""
    try:
        staged, meta = _artifact(repo, run_id, work, staging, deps)
    except Exception as exc:  # noqa: BLE001 - a bad or missing artifact never touches the old map
        return _fallback(f"download-failed: {_first_line(exc)}", minutes_used=minutes, run_id=run_id)
    if staged is None:
        return _fallback("no-artifact", minutes_used=minutes, run_id=run_id)
    if _other_copy(meta, copy_sha):
        return _fallback("wrong-sha", minutes_used=minutes, run_id=run_id)
    if outcome == "green":
        return _green_result(map_dir, staged, meta, sha, minutes, run_id)
    return _red_result(meta, minutes, run_id)


def _green_result(map_dir: Path, staged: Path, meta: dict, sha: str, minutes: int, run_id: int) -> MapBuildResult:
    built_at = meta.get("built_at")
    if not isinstance(built_at, str) or meta.get("green") is not True or not (staged / "testmondata").is_file():
        return _fallback("download-failed: the artifact is not a green map", minutes_used=minutes, run_id=run_id)
    try:
        _stamp_map(staged, meta, sha)
        _install(staged, map_dir)
    except (OSError, ValueError) as exc:
        return _fallback(f"install-failed: {_first_line(exc)}", minutes_used=minutes, run_id=run_id)
    return MapBuildResult(status="green", minutes_used=minutes, built_at=built_at, green=True, run_id=run_id)


def _red_result(meta: dict, minutes: int, run_id: int) -> MapBuildResult:
    """Red only when the artifact's meta says ``green`` is False; a failed run whose artifact says anything else is no map."""
    if meta.get("green") is not False:
        return _fallback("run-failed-no-map", minutes_used=minutes, run_id=run_id)
    built_at = meta.get("built_at") if isinstance(meta.get("built_at"), str) else None
    ids = meta.get("failed_ids")
    failed_ids = [str(test_id) for test_id in ids] if isinstance(ids, list) else []
    if not failed_ids:  # a red run that names no failing test is not a proven code red: the PC builds, no marker
        return _fallback("red without ids", minutes_used=minutes, run_id=run_id)
    return MapBuildResult(status="red", minutes_used=minutes, built_at=built_at, green=False, run_id=run_id,
                          failed_ids=failed_ids)


def _record(deps: Deps, result: MapBuildResult, settings: dict, own: bool, sha: str, repo_name: str | None = None) -> str:
    """One ``map-build`` record per build, written like the merge suite's. Returns "" when it was written, else the
    first line of why it was not; a failed record never changes the result."""
    slug = str(settings.get("repo") or "")
    outcome = result.reason.partition(":")[0] if result.status == "fallback" else result.status
    try:
        deps.record({
            "time": now(), "repo": repo_name or Path(slug).name, "slug": slug, "month": time.strftime("%Y-%m", time.gmtime()),
            "minutes": result.minutes_used, "machines": MAP_MACHINES if result.run_id is not None else 0,
            "outcome": outcome, "run_id": result.run_id, "own_runner": own, "kind": MAP_KIND, "sha": sha,
        })
    except Exception as exc:  # noqa: BLE001 - a failed record is named in the result, never raised
        return _first_line(exc)
    return ""
