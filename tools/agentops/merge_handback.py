"""Hand-back poster for a detached merge result (issue #664). Stdlib only.

One comment and one `ready-for-agent` label per still-open issue the merge covered, through the
core's own `GhCli`; a gh failure on one issue never stops the next.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from tools.agentops.core_format import human_duration
from tools.agentops.target_check import NOTE_PREFIX

MARKER = "<!-- merge result -->"
READY_LABEL = "ready-for-agent"
SILENT_REASONS = ("nothing-to-merge", "already-queued")


def _branch_ids(record: dict) -> str:
    """The failing test ids of a ``red-on-branch`` result, from the merge's printed line; "" for any other reason."""
    if record.get("reason") != "red-on-branch":
        return ""
    found = re.search(r"red-on-branch: (.+?) \(the output:", str(record.get("output") or ""), re.S)
    return found.group(1).strip() if found else ""


def _body(record: dict, result_file: str, branch: str) -> str:
    sha = str(record.get("pushed_sha") or "")
    failed = record.get("failed")
    tail = (f"after {human_duration(record.get('in_line_s'))} in line and a suite of "
            f"{human_duration(record.get('suite_s'))}, {'-' if failed is None else failed} failed. "
            f"Result {result_file}; suite output {record.get('suite_output') or '-'}.")
    if record.get("rc") == 0 and sha:
        line = f"Merge of {branch} landed on main {sha[:12]} {tail}"
    else:
        line = f"Merge of {branch} did not land ({record.get('reason')}) {tail}"
        taken = re.search(r"^(silent for .+)$", str(record.get("output") or ""), re.M)
        if record.get("reason") == "silent-holder" and taken:  # #62: the line was taken from this merge
            line += f"\n{taken.group(1)}"
        ids = _branch_ids(record)
        if ids:  # a branch that left the line on its own failing tests: what its worker fixes
            line += f"\nFailed on this branch alone, re-run alone too: {ids}"
    targets = record.get("core_targets") or []  # issue #28: the home's core update per target, verbatim
    if targets:
        line += f"\ntargets: {'; '.join(targets)}"
    notes = re.findall(rf"^{re.escape(NOTE_PREFIX)}(.+?)\s*$", str(record.get("output") or ""), re.M)
    for note in notes:  # #93: one line per target with no leak-words file, named in the merge result's output
        line += f"\n{NOTE_PREFIX}{note}"
    if record.get("rc") == 0 and sha:  # #149: the monitor install of a landed merge
        installed = re.search(r"^(monitor (?:installed|install failed.*?))\s*$", str(record.get("output") or ""), re.M)
        if installed:
            line += f"\n{installed.group(1)}"
    return f"{MARKER}\n{line}\n"


def _earlier_merge(record: dict) -> tuple[dict, str] | None:
    """The ended merge an `already-queued` answer names ("... result <file>"), with its file; None when the
    file is not there yet (that merge still runs) or is no ended merge result (#874)."""
    found = re.search(r"already queued: .*?result (.+?\.json)\s*$", str(record.get("output") or ""), re.M)
    if not found:
        return None
    try:
        earlier = json.loads(Path(found.group(1)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if earlier.get("kind") != "merge" or earlier.get("reason") in SILENT_REASONS or "rc" not in earlier:
        return None
    return earlier, found.group(1)


FOREIGN_REF = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")


def _default_gh_for_repo(repo: str) -> Any:
    from tools.agentops.dispatch import GhCli
    return GhCli(repo=repo)


def _post_one(gh: Any, number: int, body: str) -> bool:
    """Comment and re-label one issue when it is still open; gh errors are swallowed. True when posted to."""
    try:
        if not gh.issue_status(number).open:
            return False
    except Exception:
        return False
    try:
        gh.comment(number, body)
    except Exception:
        pass
    try:
        gh.add_label(number, READY_LABEL)
    except Exception:
        pass
    return True


def _body_of(record: dict, result_file: str, branch: str) -> tuple[dict, str, str, list[int], list[str]] | None:
    """The record whose result is posted (an `already-queued` answer posts the earlier merge's), its body, and the
    own and foreign issues to post to; None when there is nothing to post."""
    issues = list(record.get("issues") or [])
    foreign = list(record.get("foreign_issues") or [])
    if record.get("kind") != "merge" or not (issues or foreign):
        return None
    if record.get("reason") == "already-queued":
        earlier = _earlier_merge(record)
        if earlier is None:
            return None
        record, result_file = earlier[0], earlier[1]
        issues = [n for n in issues if n not in (record.get("issues") or [])]
        foreign = [r for r in foreign if r not in (record.get("foreign_issues") or [])]
    elif record.get("reason") in SILENT_REASONS:
        return None
    return record, result_file, _body(record, result_file, branch), issues, foreign


def post_foreign_result(record: dict, result_file: str, branch: str,
                        gh_for_repo: Callable[[str], Any] | None = None) -> list[str]:
    """Post the result to every open issue of ANOTHER repo the record names (`foreign_issues`, each
    `owner/repo#N`), through `gh_for_repo(owner/repo)`; returns the refs posted to. A malformed ref, a closed
    issue or a gh failure on one never stops the next."""
    prepared = _body_of(record, result_file, branch)
    if prepared is None:
        return []
    body, foreign = prepared[2], prepared[4]
    factory = gh_for_repo or _default_gh_for_repo
    posted: list[str] = []
    for ref in foreign:
        found = FOREIGN_REF.match(str(ref))
        if not found:
            continue
        try:
            gh = factory(found.group(1))
        except Exception:
            continue
        if _post_one(gh, int(found.group(2)), body):
            posted.append(str(ref))
    return posted


def post_merge_result(record: dict, result_file: str, branch: str, gh: Any,
                      gh_for_repo: Callable[[str], Any] | None = None) -> list[int]:
    """Post the result to every open issue of the record; returns the issues posted to. An `already-queued`
    answer posts the earlier queued merge's own result (landed or not) on the asking issues that merge did
    not already comment on. The record's `foreign_issues` (#145) are posted through `gh_for_repo`."""

    prepared = _body_of(record, result_file, branch)
    if prepared is None:
        return []
    posted = [int(n) for n in prepared[3] if _post_one(gh, int(n), prepared[2])]
    post_foreign_result(record, result_file, branch, gh_for_repo)
    return posted
