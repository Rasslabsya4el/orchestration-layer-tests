"""#232 unit 4c (D119): the merge's wiring to GitHub: whether a merge's suite may go there, and the one call that runs it.

``plan_for(settings, root)`` -> ``(Plan | None, note)``: a ``Plan`` only when ``github_suite`` is true and the repo has a
test copy (``test_copy_remote``, a public copy: its own runs are free) or ``github_own_runners`` (a private repo on its
own repo's runners, which counts free minutes: ``github_minutes.over_budget`` true gives ``(None, note)``, the note
for the merge log). Without a plan, or for a light run, the merge is exactly today's PC path.

``run(plan, ...)`` runs ``github_suite.run_github_suite`` for the merged tree in ``suite_dir`` while keeping the merge
lock alive (a thread calls ``alive`` and touches the output file every ``ALIVE_SECONDS``), and never raises: any
failure is a ``fallback`` result. ``DEPS`` (tests only) is passed to it as its injectable callables.

The delivery of a fallback's one brain line is ``append_brain_line``: the detached merge process has no way to a
brain chat, so the line is appended to ``brain_lines.log`` next to the merge log (one JSON line: time, repo, line).
"""

from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from . import github_minutes, github_suite
from .core_format import human_duration
from .test_copy_check import git_env
from .util import append_jsonl, no_window_flags, now

ALIVE_SECONDS = 20.0
BRAIN_LINES = "brain_lines.log"
#: tests only: the ``github_suite.Deps`` the merge hands to ``run_github_suite`` (None = the real ones).
DEPS: github_suite.Deps | None = None


@dataclass(frozen=True)
class Plan:
    remote: str       # the test copy's URL; "" for a repo on its own runners
    own_repo: bool
    repo_slug: str    # settings ``repo``
    repo_name: str    # the repo's folder name


def plan_for(settings: dict, root, now=None) -> tuple[Plan | None, str]:
    if settings.get("github_suite") is not True:
        return None, ""
    remote = str(settings.get("test_copy_remote") or "").strip()
    own = settings.get("github_own_runners") is True and not remote
    if not remote and not own:
        return None, ""
    name = Path(root).resolve().name if str(root) else ""
    if own:
        over, reason = github_minutes.over_budget(name, now)
        if over:
            return None, f"own-runners repo over free minutes: {reason}"
    return Plan(remote=remote, own_repo=own, repo_slug=str(settings.get("repo") or ""), repo_name=name), ""


def ref_name(branch: str) -> str:
    """The branch name a merge's suite takes on the test copy (or the repo's origin)."""
    return "merge-suite/" + (re.sub(r"[^A-Za-z0-9._/-]", "-", branch.removeprefix("origin/")).strip("/-") or "x")


def _fallback(plan: Plan, ticket: str, reason: str) -> github_suite.GithubSuiteResult:
    return github_suite.GithubSuiteResult(status="fallback", reason=reason, brain_line=_line(plan, ticket, reason))


def _line(plan: Plan, ticket: str, reason: str) -> str:
    """``<repo> merge <ticket>: GitHub <reason>, suite on the PC``; ``ticket`` is ``#<n>`` or the merge's branch."""
    return github_suite.brain_line(plan.repo_name, ticket.removeprefix("origin/"), reason)


def ticket_of(branch: str, issues=()) -> str:
    """What the brain line calls the merge: the first issue number the merge knows (``#<n>``), else its branch."""
    return f"#{issues[0]}" if issues else branch


def _kept_alive(call, alive, output_path, every: float):
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = call()
        except BaseException as exc:  # noqa: BLE001 - handed to the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    while thread.is_alive():
        thread.join(every)
        if thread.is_alive():
            for beat in (alive, lambda: Path(output_path).touch()):
                try:  # a failed beat never ends the wait: the only exits are the thread's own end and its result
                    beat()
                except Exception:  # noqa: BLE001
                    pass
    if "error" in box:
        raise box["error"]
    return box["value"]


def _git(root, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True, env=git_env(),
                   creationflags=no_window_flags(), timeout=60)


def run(plan: Plan, *, cwd, suite_dir, sha: str, branch: str, priority: bool, settings: dict, alive,
        output_path, every: float | None = None, ticket: str = "", tests: int | None = None,
        ids: list[str] | None = None, windows: bool = False,
        skip: list[str] | None = None) -> github_suite.GithubSuiteResult:
    """The suite of the merge commit ``sha`` (checked out in ``suite_dir``) on GitHub. Never raises. An own-repo run
    pushes a ref ``merge-suite/<branch>`` to the repo's origin and deletes it again, best effort, whatever the outcome."""
    ref = ref_name(branch)
    ticket = ticket or branch
    every = ALIVE_SECONDS if every is None else every
    made_ref = False
    try:
        if plan.own_repo:  # the repo's own origin takes a branch at the merge commit
            _git(cwd, "update-ref", f"refs/heads/{ref}", sha)
            made_ref = True
        return _kept_alive(
            lambda: github_suite.run_github_suite(
                root=suite_dir if not plan.own_repo else cwd, branch=ref, repo_slug=plan.repo_slug,
                remote=plan.remote, repo_name=plan.repo_name, priority=priority, settings=settings, deps=DEPS,
                own_repo=plan.own_repo, tests=tests, ids=ids, windows=windows, skip=skip),
            alive, output_path, every)
    except Exception as exc:  # noqa: BLE001 - GitHub never changes a merge's result: the PC runs the suite
        return _fallback(plan, ticket, _error_reason(exc))
    finally:
        if made_ref:
            for args in (("push", "origin", "--delete", ref), ("update-ref", "-d", f"refs/heads/{ref}")):
                try:
                    _git(cwd, *args)
                except Exception:  # noqa: BLE001 - a clean-up never changes the result
                    pass


def _error_reason(exc: BaseException) -> str:
    """#345: ``error: <class>: <first line of the message>`` (the class alone with no message), at most REASON_MAX."""
    return ("error: " + github_suite.reason_text(exc, with_class=True))[: github_suite.REASON_MAX]


def pc_reason(result: github_suite.GithubSuiteResult) -> str:
    """Why a GitHub result ends on the PC: a fallback's reason; a red's reason names the jobs its reader read (#345)."""
    if result.status == "fallback":
        return result.reason
    if not result.jobs:  # no job read: the failed ids the reader kept, if any, are all there is to count
        return f"red: no job read; ids found: {len(result.failed_ids)}"
    return github_suite.job_reason("red", result.jobs)


def goes_to_pc(result: github_suite.GithubSuiteResult) -> bool:
    return result.status == "fallback" or (result.status == "red" and (result.full_recheck or not result.failed_ids))


def brain_line_for(plan: Plan, ticket: str, result: github_suite.GithubSuiteResult) -> str:
    return _line(plan, ticket, pc_reason(result))


def parts_text(seconds: list) -> str:
    return "/".join(human_duration(s) for s in seconds)


def place_text(result: github_suite.GithubSuiteResult) -> str:
    """``suite on GitHub: <k> machines, parts <s1>/<s2>..., <m> min used, run <id>`` (durations in a human unit)."""
    parts = f", parts {parts_text(result.part_seconds)}" if result.part_seconds else ""
    return (f"suite on GitHub: {result.machines} machines{parts}, {result.minutes_used} min used, "
            f"run {result.run_id if result.run_id is not None else '-'}")


def pc_place_text(reason: str) -> str:
    return f"suite on the PC (GitHub {reason})"


def output_text(result: github_suite.GithubSuiteResult, place: str) -> str:
    """The suite output of a run GitHub judged: the place line, and for a red the failed ids in pytest's own summary
    form, so the merge reads them as it reads a PC run's."""
    lines = [place]
    if result.status == "red":
        lines += ["=========================== short test summary info ============================"]
        lines += [f"FAILED {i}" for i in result.failed_ids]
        lines += [f"{len(result.failed_ids)} failed on GitHub"]
    return "\n".join(lines) + "\n"


def append_brain_line(log_dir, plan: Plan, line: str) -> None:
    """Best effort, never raises: the line for the area's brain chat, kept in ``<log_dir>/brain_lines.log``."""
    try:
        append_jsonl(Path(log_dir) / BRAIN_LINES, {"time": now(), "repo": plan.repo_name, "line": line})
    except Exception:  # noqa: BLE001
        pass
