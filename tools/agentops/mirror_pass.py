"""#425 unit 1: the dispatcher's mirror pass over the listed repos (`mirror_repos`).

Each cycle, before the offer, every open issue of a listed repo that carries `ready-for-agent` or `needs-triage` gets one
mirror in the tickets repo: a `mirror`-labelled issue whose first line is `Mirror of <source URL>; the source holds its
history and comments.`, then the source body verbatim. The pair is found by that first line over every state of the
tickets repo's mirrors, so a lost local state cannot make a second mirror. The pass knows no opener, no title and no
repo name; it never changes a label of the source.

`create_mirrors` is the one call the dispatcher makes; every `gh` call goes through the injected runner
(`(args, stdin) -> CompletedProcess`, the shape of `GhCli._run`), so a test never reaches a tracker. Standard library only.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from typing import Any

#: the label every mirror carries, besides its source's role label.
MIRROR_LABEL = "mirror"
#: the role labels a source is read under; a source with `ready-for-agent` gets that label on its mirror, else `needs-triage`.
ROLE_LABELS = ("ready-for-agent", "needs-triage")

_READY = "ready-for-agent"
_TRIAGE = "needs-triage"
#: the `--limit` of every list of this pass.
_LIMIT = "1000"
_LABEL_COLOR = "ededed"
_JSON_SOURCE = "number,title,body,labels,url"
_JSON_MIRROR = "number,body"
_SOURCE_LINE = re.compile(r"Mirror of (\S+); the source holds its history and comments\.")
_ISSUE_URL = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/issues/(\d+)")
_CREATED_NUMBER = re.compile(r"/issues/(\d+)")

Runner = Callable[[list[str], str | None], subprocess.CompletedProcess]


def source_line(url: str) -> str:
    """The first line of a mirror's body: the source's URL and the note that it holds the history."""

    return f"Mirror of {url}; the source holds its history and comments."


def source_url_of(body: str) -> str | None:
    """The source URL on the body's first line when that line has the form `source_line` writes; `None` otherwise."""

    first = (body or "").split("\n", 1)[0].rstrip("\r")
    match = _SOURCE_LINE.fullmatch(first)
    return match.group(1) if match else None


def parse_source_url(url: str) -> tuple[str, int]:
    """`https://github.com/owner/name/issues/7` -> (`owner/name`, 7); raises ValueError for any other text."""

    match = _ISSUE_URL.fullmatch(url)
    if match is None:
        raise ValueError(f"not an issue URL: {url!r}")
    return match.group(1), int(match.group(2))


def mirror_title(repo: str, number: int, title: str) -> str:
    """`<name-after-slash>#<n>: <title>` of the source."""

    return f"{repo.rsplit('/', 1)[-1]}#{number}: {title}"


def create_mirrors(*, tickets_repo: str, mirror_repos: list[str], runner: Runner,
                   log: Callable[[str], Any]) -> list[tuple[str, int]]:
    """One pass. With no listed repo: no call at all. Otherwise the known sources are read from the tickets repo (every
    state), each listed repo's open sources are read under both role labels, and each source with no mirror gets one.
    Returns (source URL, new mirror number) per mirror opened; logs `mirror: <url> -> #<n>` per mirror and
    `mirror: cannot read <repo>: <first stderr line>` once per unreadable repo. An unreadable tickets repo opens nothing,
    since without the known sources a mirror could be a second one."""

    if not mirror_repos:
        return []
    known = _known_sources(tickets_repo, runner, log)
    if known is None:
        return []

    created: list[tuple[str, int]] = []
    label_made = False
    for repo, issue, role in _sources(mirror_repos, runner, log):
        url = _url_of(repo, issue)
        if url in known:
            continue
        if not label_made:
            made = runner(["label", "create", MIRROR_LABEL, "--repo", tickets_repo, "--force", "--color", _LABEL_COLOR],
                          None)
            if made.returncode != 0:
                log(f"mirror: cannot make label {MIRROR_LABEL} in {tickets_repo}: {_first_line(made.stderr)}")
            label_made = True
        number = int(issue["number"])
        body = issue.get("body") or ""
        result = runner(["issue", "create", "--repo", tickets_repo,
                         "--title", mirror_title(repo, number, str(issue.get("title") or "")),
                         "--body-file", "-", "--label", role, "--label", MIRROR_LABEL],
                        source_line(url) + "\n\n" + body)
        if result.returncode != 0:
            log(f"mirror: cannot open a mirror of {url}: {_first_line(result.stderr)}")
            continue
        known.add(url)
        found = _CREATED_NUMBER.findall(result.stdout or "")
        if not found:
            log(f"mirror: {url} opened, its number not read from: {_first_line(result.stdout)}")
            continue
        new = int(found[-1])
        created.append((url, new))
        log(f"mirror: {url} -> #{new}")
    return created


def _known_sources(tickets_repo: str, runner: Runner, log: Callable[[str], Any]) -> set[str] | None:
    """The source URLs the tickets repo's mirrors already name, open and closed alike; `None` when the read fails."""

    result = runner(["issue", "list", "--repo", tickets_repo, "--label", MIRROR_LABEL, "--state", "all",
                     "--limit", _LIMIT, "--json", _JSON_MIRROR], None)
    items, error = _listed(result)
    if items is None:
        log(f"mirror: cannot read {tickets_repo}: {error}")
        return None
    return {url for url in (source_url_of(item.get("body") or "") for item in items) if url}


def _sources(mirror_repos: list[str], runner: Runner,
             log: Callable[[str], Any]) -> list[tuple[str, dict[str, Any], str]]:
    """(repo, source issue, role label) for every readable listed repo, in the setting's order and by number."""

    out: list[tuple[str, dict[str, Any], str]] = []
    for repo in mirror_repos:
        found = _read_repo(repo, runner, log)
        if found is not None:
            out.extend((repo, issue, role) for issue, role in found)
    return out


def _read_repo(repo: str, runner: Runner,
               log: Callable[[str], Any]) -> list[tuple[dict[str, Any], str]] | None:
    """One repo's open sources under both role labels, union by number; `None` (logged once) when either list fails."""

    issues: dict[int, dict[str, Any]] = {}
    roles: dict[int, set[str]] = {}
    for role in ROLE_LABELS:
        result = runner(["issue", "list", "--repo", repo, "--state", "open", "--label", role,
                         "--limit", _LIMIT, "--json", _JSON_SOURCE], None)
        items, error = _listed(result)
        if items is None:
            log(f"mirror: cannot read {repo}: {error}")
            return None
        for item in items:
            number = item.get("number")
            if not isinstance(number, int):
                continue
            issues.setdefault(number, item)
            roles.setdefault(number, set()).add(role)
    out: list[tuple[dict[str, Any], str]] = []
    for number in sorted(issues):
        has_ready = _READY in roles[number] or _READY in _label_names(issues[number])
        out.append((issues[number], _READY if has_ready else _TRIAGE))
    return out


def _listed(result: subprocess.CompletedProcess) -> tuple[list[dict[str, Any]] | None, str]:
    """The objects of a JSON list answer, or (None, the reason): a failed call, a text that is not a JSON list."""

    if result.returncode != 0:
        return None, _first_line(result.stderr)
    try:
        data = json.loads(result.stdout or "[]")
    except ValueError as exc:
        return None, f"invalid JSON ({exc})"
    if not isinstance(data, list):
        return None, "not a JSON list"
    return [item for item in data if isinstance(item, dict)], ""


def _label_names(issue: dict[str, Any]) -> list[str]:
    labels = issue.get("labels") or []
    return [label.get("name") if isinstance(label, dict) else label for label in labels if label]


def _url_of(repo: str, issue: dict[str, Any]) -> str:
    url = issue.get("url")
    return url if isinstance(url, str) and url else f"https://github.com/{repo}/issues/{issue['number']}"


def _first_line(text: str | None) -> str:
    lines = (text or "").strip().splitlines()
    return lines[0] if lines else ""
