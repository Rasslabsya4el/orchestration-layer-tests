"""Issue 105 (unit 1): a parked ticket comes back by itself once the event its `Parked:` line names has happened.

``ParkedReturn(repo, gh, now, target_core, log).run_once() -> list[Flip]`` makes ONE ``gh.list_parked()`` call and,
for each open parked issue whose `Parked:` line is one of the three machine forms and whose event holds, adds
``ready-for-agent``, removes ``parked`` and posts one comment (first line ``<!-- parked-return -->``). A human wait
(``needs-info``, ``ready-for-human``, ``wontfix``) and a free-text line are never flipped; an event not yet reached
writes nothing.

The three forms (the rest of the line after ``Parked:``, stripped, one trailing ``.`` allowed, keywords any case):
``until YYYY-MM-DD HH:MM`` (local naive; ``until YYYY-MM-DD`` alone is 00:00), ``<hours> h after core <sha> reaches
<owner/repo>``, ``until #N closes`` or ``until owner/repo#N closes``.

Seams: ``now() -> datetime`` (naive local), ``target_core(owner_repo) -> (marker_sha, reached_at) | None``,
``gh`` (``list_parked``, ``issue_state``, ``add_label``, ``remove_label``, ``comment``), ``log(str)``, and
``is_ancestor(older, newer) -> bool`` (default: ``git merge-base --is-ancestor`` in this repo's root).
``GhRunner(repo, run)`` is the ``gh`` seam over a ``run(args, stdin) -> CompletedProcess`` callable.
``default_target_core(settings, home_root)`` reads a target's ``core_version`` and the time it got it.

Standard library plus ``.util`` only."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .util import no_window_flags

LABEL = "parked"
READY = "ready-for-agent"
HUMAN_WAITS = frozenset({"needs-info", "ready-for-human", "wontfix"})
MARKER = "<!-- parked-return -->"
REPO_ROOT = Path(__file__).parents[2]
CONFIG_REL = Path("config") / "orchestration.json"
GIT_TIMEOUT_S = 15

PARKED_LINE_RE = re.compile(r"^\s*parked:", re.I)
DATE_RE = re.compile(r"until\s+(\d{4})-(\d{2})-(\d{2})(?:\s+(\d{2}):(\d{2}))?", re.I)
DATE_ANY_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:\s+(\d{2}):(\d{2}))?")
CORE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*h\s+after\s+core\s+([0-9a-f]{7,40})\s+reaches\s+([\w.-]+/[\w.-]+)", re.I)
ISSUE_RE = re.compile(r"until\s+(?:([\w.-]+/[\w.-]+))?#(\d+)\s+closes", re.I)


@dataclass(frozen=True)
class Form:
    kind: str                      # "date" | "core" | "issue"
    at: datetime | None = None     # date: the local naive time
    hours: float | None = None     # core: hours after the reach
    sha: str | None = None         # core: the commit the target must hold or descend from
    owner_repo: str | None = None  # core: the target repo; issue: the other repo, None = the pass's own repo
    number: int | None = None      # issue: the issue number that must close


@dataclass(frozen=True)
class Flip:
    number: int
    event: str


def parse_parked_line(body: str | None) -> Form | None:
    """The machine form of the body's `Parked:` line, or None (no such line in the first 3 non-blank lines, free
    text, or a malformed date)."""
    line = _parked_line(body or "")
    if line is None:
        return None
    rest = line.split(":", 1)[1].strip()
    if rest.endswith("."):
        rest = rest[:-1].rstrip()

    m = DATE_RE.fullmatch(rest)
    if m is not None:
        year, month, day, hh, mm = m.groups()
        try:
            at = datetime(int(year), int(month), int(day), int(hh or 0), int(mm or 0))
        except ValueError:
            return None
        return Form(kind="date", at=at)

    m = CORE_RE.fullmatch(rest)
    if m is not None:
        hours, sha, owner_repo = m.groups()
        return Form(kind="core", hours=float(hours), sha=sha, owner_repo=owner_repo)

    m = ISSUE_RE.fullmatch(rest)
    if m is not None:
        owner_repo, number = m.groups()
        return Form(kind="issue", owner_repo=owner_repo, number=int(number))

    return None


def _parked_line(body: str) -> str | None:
    """The first of the body's first 3 non-blank lines that starts with `Parked:` (leading spaces ok)."""
    for line in [ln for ln in body.splitlines() if ln.strip()][:3]:
        if PARKED_LINE_RE.match(line):
            return line
    return None


def parked_line(body: str | None) -> str | None:
    """The body's `Parked:` line, or None: the public face of `_parked_line` for the sweep (#187)."""
    return _parked_line(body or "")


def parked_dates(line: str) -> list[datetime]:
    """Every `YYYY-MM-DD` (optionally followed by ` HH:MM`) anywhere in a `Parked:` line, local naive; an impossible
    date is skipped (#187)."""
    dates: list[datetime] = []
    for m in DATE_ANY_RE.finditer(line):
        year, month, day, hh, mm = m.groups()
        try:
            dates.append(datetime(int(year), int(month), int(day), int(hh or 0), int(mm or 0)))
        except ValueError:
            continue
    return dates


def _hours_text(hours: float) -> str:
    """A human unit (#105 principle 7): minutes under 90 min, then hours and minutes (`2 h 15 min`, `24 h`)."""
    total = round(hours * 60)
    if total < 90:
        return f"{total} min"
    h, m = divmod(total, 60)
    return f"{h} h" if m == 0 else f"{h} h {m} min"


def _label_list(labels) -> list[str]:
    """Label names in order, from `[{"name": ...}]` (gh) or a list of names (the seam)."""
    names: list[str] = []
    for item in labels or []:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str):
            names.append(name)
    return names


class ParkedReturn:
    def __init__(self, repo, gh, now=None, target_core=None, log=None, is_ancestor=None, commit_time=None, home=None):
        self.repo = repo
        #: #323: the code repo (the home whose core is its own sha); the issues' repo (`repo`) may be another one.
        self.home = home if home is not None else repo
        self.gh = gh
        self.now = now or datetime.now
        self.target_core = target_core or (lambda owner_repo: None)
        self.log = log or (lambda text: None)
        self.is_ancestor = is_ancestor or git_is_ancestor
        #: the home repo holds its own core from the sha's own commit: its hours count from that commit, since its HEAD moves on.
        self.commit_time = commit_time or _home_commit_time
        self._unknown_core_logged: set[int] = set()

    def run_once(self) -> list[Flip]:
        """One pass: one list call, then each parked issue checked alone; never raises."""
        try:
            issues = self.gh.list_parked()
            now = self.now()
        except Exception as exc:  # noqa: BLE001 -- a failed pass writes nothing and is logged
            self.log(f"parked return: pass not run: {exc}")
            return []
        flips: list[Flip] = []
        for issue in issues or []:
            try:
                flip = self._one(issue, now)
            except Exception as exc:  # noqa: BLE001 -- one issue's failure never stops the others
                self.log(f"parked return: #{issue.get('number')} check failed: {exc}")
                continue
            if flip is not None:
                flips.append(flip)
        return flips

    def _one(self, issue: dict, now: datetime) -> Flip | None:
        number = issue["number"]
        if set(_label_list(issue.get("labels"))) & HUMAN_WAITS:
            return None
        form = parse_parked_line(issue.get("body"))
        if form is None:
            return None
        event = self._reached(form, now, number)
        if event is None:
            return None
        if not self.gh.add_label(number, READY):
            self.log(f"parked return: #{number} not returned: add {READY} failed")
            return None
        if not self.gh.remove_label(number, LABEL):
            self.log(f"parked return: #{number} not returned: remove {LABEL} failed")
            return None
        if not self.gh.comment(number, f"{MARKER}\n{event}"):
            self.log(f"parked return: #{number} comment failed")
        self.log(f"parked return: #{number} {event}")
        return Flip(number=number, event=event)

    def reached(self, form: Form, number: int, now: datetime | None = None) -> str | None:
        """#470: the public face of `_reached` for the dispatcher's ready-queue gate: the event sentence when the
        form's event has happened, else None. An event that cannot be read raises (its `gh` read raises)."""
        return self._reached(form, self.now() if now is None else now, number)

    def _reached(self, form: Form, now: datetime, number: int) -> str | None:
        """The event sentence when the form's event has happened, else None."""
        if form.kind == "date":
            if now < form.at:
                return None
            return f"The event this ticket waited for has happened: {form.at:%Y-%m-%d %H:%M} has passed."
        if form.kind == "core":
            got = self.target_core(form.owner_repo)
            if got is None:
                if number not in self._unknown_core_logged:
                    self._unknown_core_logged.add(number)
                    self.log(f"parked return: #{number} waits: core of {form.owner_repo} unknown")
                return None
            if not got:
                return None
            marker, reached_at = got
            if not marker or not self.is_ancestor(form.sha, marker):
                return None
            if form.owner_repo == self.home:
                reached_at = self.commit_time(form.sha)
                if reached_at is None:
                    return None
            if now < reached_at + timedelta(hours=form.hours):
                return None
            return (f"The event this ticket waited for has happened: core {form.sha} reached {form.owner_repo} "
                    f"{_hours_text(form.hours)} ago.")
        if form.kind == "issue":
            owner = form.owner_repo or self.repo
            if self.gh.issue_state(owner, form.number) != "CLOSED":
                return None
            ref = f"#{form.number}" if form.owner_repo is None else f"{form.owner_repo}#{form.number}"
            return f"The event this ticket waited for has happened: {ref} is closed."
        return None


class GhRunner:
    """The `gh` seam of `ParkedReturn` over ``run(args, stdin) -> CompletedProcess`` (``GhCli._run``'s shape).
    A failed call (rc != 0, or the call itself raising) gives False / None / []; nothing is raised."""

    def __init__(self, repo: str, run: Callable):
        self.repo = repo
        self._run = run

    def _call(self, args: list[str], stdin: str | None = None):
        try:
            return self._run(args, stdin)
        except (OSError, subprocess.SubprocessError):
            return None

    def _ok(self, args: list[str], stdin: str | None = None) -> bool:
        res = self._call(args, stdin)
        return res is not None and res.returncode == 0

    def list_parked(self) -> list[dict]:
        res = self._call(["issue", "list", "-R", self.repo, "--state", "open", "--label", LABEL,
                          "--limit", "200", "--json", "number,body,labels"])
        if res is None or res.returncode:
            return []
        try:
            data = json.loads(res.stdout or "[]")
        except ValueError:
            return []
        if not isinstance(data, list):
            return []
        return [{"number": item["number"], "body": item.get("body") or "",
                 "labels": _label_list(item.get("labels"))}
                for item in data if isinstance(item, dict) and isinstance(item.get("number"), int)]

    def issue_state(self, owner_repo: str, n: int) -> str | None:
        res = self._call(["issue", "view", str(n), "-R", owner_repo, "--json", "state", "-q", ".state"])
        if res is None or res.returncode:
            return None
        state = (res.stdout or "").strip()
        return state if state in ("OPEN", "CLOSED") else None

    def add_label(self, n: int, label: str) -> bool:
        return self._ok(["issue", "edit", str(n), "-R", self.repo, "--add-label", label])

    def remove_label(self, n: int, label: str) -> bool:
        return self._ok(["issue", "edit", str(n), "-R", self.repo, "--remove-label", label])

    def comment(self, n: int, body: str) -> bool:
        return self._ok(["issue", "comment", str(n), "-R", self.repo, "--body-file", "-"], body)


def git_is_ancestor(older: str, newer: str, cwd: Path = REPO_ROOT) -> bool:
    """`git merge-base --is-ancestor older newer` in ``cwd``; a failed or refused call is False."""
    if not older or not newer or older.startswith("-") or newer.startswith("-"):
        return False
    try:
        proc = subprocess.run(["git", "-C", str(cwd), "merge-base", "--is-ancestor", older, newer],
                              capture_output=True, creationflags=no_window_flags(), timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def default_target_core(settings: dict, home_root: Path) -> Callable[[str], tuple[str, datetime] | None]:
    """The real `target_core` seam. A target: its `config/orchestration.json` `core_version` and the committer time of
    the last commit that touched that key (never earlier than the reach). The home repo (``settings["repo"]``): its own
    HEAD and the committer time of that HEAD. Any failure is None."""
    home_root = Path(home_root)

    def target_core(owner_repo: str) -> tuple[str, datetime] | None:
        try:
            if owner_repo == settings.get("repo"):
                head = _git_text(home_root, "rev-parse", "HEAD")
                if not head:
                    return None
                return head, _committed_at(home_root, "HEAD")
            folder = _target_folder(settings, home_root, owner_repo)
            if folder is None:
                return None
            marker = _core_marker(folder)
            if marker is None:
                return None
            return marker, _committed_at(folder, "-G", "core_version", "--", CONFIG_REL.as_posix())
        except Exception:  # noqa: BLE001 -- an unknown core is no reach, never a crash
            return None

    return target_core


def _home_commit_time(sha: str) -> datetime | None:
    """The committer time (naive local) of `sha` in this repo; None when it is not a commit here."""
    try:
        return _committed_at(REPO_ROOT, sha)
    except Exception:  # noqa: BLE001 -- an unknown commit is no reach
        return None


def _target_folder(settings: dict, home_root: Path, owner_repo: str) -> Path | None:
    for entry in settings.get("targets") or []:
        if isinstance(entry, dict) and entry.get("repo") == owner_repo and entry.get("folder"):
            folder = Path(str(entry["folder"]))
            return folder if folder.is_absolute() else home_root / folder
    return None


def _core_marker(folder: Path) -> str | None:
    cfg = json.loads((folder / CONFIG_REL).read_text(encoding="utf-8"))
    marker = cfg.get("core_version") if isinstance(cfg, dict) else None
    return marker if isinstance(marker, str) and marker else None


def _committed_at(folder: Path, *rev_args: str) -> datetime:
    """The committer time (naive local) of the newest commit `git log -n1` picks with ``rev_args``; raises when none."""
    stamp = _git_text(folder, "log", "-n1", "--format=%cI", *(rev_args or ("HEAD",)))
    if not stamp:
        raise RuntimeError(f"no commit found in {folder}")
    return datetime.fromisoformat(stamp).astimezone().replace(tzinfo=None)


def _git_text(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=no_window_flags(), timeout=GIT_TIMEOUT_S)
    return proc.stdout.strip() if proc.returncode == 0 else ""
