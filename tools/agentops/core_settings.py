"""Loader for the installation's own identity file (issue #147, half b).

Every script the core ships with reads its identity (repo slug, chat title, absolute
paths, the context-line target) from one file, ``config/orchestration.json``, instead
of holding it in code. Stdlib only, no import of anything under ``src/``, so this module
stays copyable into another repo the same way the rest of ``tools/agentops`` does.

There is no default for any key: a target that has not filled in its own settings file
gets a one-line error naming the file and the missing key, not a silently wrong value.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

#: derived from this module's own location, never stored: tools/agentops/core_settings.py
#: -> tools/agentops -> tools -> the repo root.
ROOT = Path(__file__).resolve().parents[2]

SETTINGS_PATH = ROOT / "config" / "orchestration.json"
#: #343 u6: an env var naming another settings file, read by ``load()`` when no path is given (tests, and their
#: subprocesses, read a copy with ``github_suite`` off); unset = ``SETTINGS_PATH``.
SETTINGS_ENV = "AGENTOPS_CORE_SETTINGS"

#: every key an installation must fill in; no key here has a code default.
REQUIRED_KEYS = (
    "repo",
    "brain_chat_title",
    "context_line",
    "claude_projects_dir",
    "bridge_dir",
    "bridge_lock",
    "local_modules",
)

#: #40: the globs of paths a commit may touch with no full suite (a data commit). The core's own
#: list; a target's ``suite_free_paths`` adds to it and never removes from it (``load`` merges them).
SUITE_FREE_PATHS = ("docs/**/*.md", "config/*.json", "GLOSSARY.md", "CODING_STANDARDS.md", "docs/STATE.md")

#: optional keys: an installation may leave them out and gets this value (never in REQUIRED_KEYS).
DEFAULTS = {
    "context_ceiling": 200000,
    #: #4: the most ready tickets one ORCH session is offered at its start, oldest first; the rest wait.
    "orch_offer_max": 4,
    #: #1120: a parking checkpoint for context is allowed only past this; the soft `context_line` only finishes.
    "context_hard_line": 400000,
    "brain_chat_title_aliases": [],
    "areas": [],
    #: #425: repos whose open ready-for-agent / needs-triage issues the dispatcher mirrors into the tickets repo; empty = no request.
    "mirror_repos": [],
    "watch_hold_cap_minutes": 60,
    #: #265: minutes an ORCH session may stay silent (no timed record of its own or of a running subagent) before
    #: the dispatcher wakes it once, and ends it when it is finishing or still silent a limit after the wake.
    "silent_orch_minutes": 30,
    #: #117: how long a target holding a pending core mark starts no further ORCH (minutes, machine-mode play time
    #: not counted); past it the start goes ahead with one log line and one brain-chat notice.
    "core_hold_max_min": 60,
    #: #179: minutes after a ticket's park on a merge result file before a merge with no trace (no result, no suite
    #: output, no lock or queue entry for its branch) is read as never started and the ticket goes back to ready.
    "merge_trace_grace_min": 15,
    #: #201: the map build's suite run is stopped past this many minutes (its whole process tree with it).
    "map_build_bound_minutes": 120,
    #: #343 (rule 4): a map build starting within this many minutes of the last build's start is coalesced (the
    #: newest main builds later); `heavy_place.decide`.
    "map_build_min_interval_minutes": 30,
    "code_issue_area": "",
    "program_labels": [],
    #: area label -> brain chat title (#1109): news of an issue goes to its area's chat, else to brain_chat_title.
    "brain_chats": {},
    #: the weekly loss table's ceiling: the table marks a mean cost per closed ticket above it (USD, API price).
    "loss_ceiling_usd_per_closed": 5.5,
    #: repos the loss table reads: [{"name", "root", "projects_dir"}]; empty = only this repo.
    "loss_repos": [],
    #: the area label whose brain chat carries the daily losses note (`losses_daily.py`); "" = brain_chat_title.
    "loss_note_area": "",
    #: #72: the agent-setting names the skill counter also tallies as their own role (a main session whose first
    #: records name one); an installation's own roles, e.g. a local extension's on-demand session. [] = none.
    "skill_watched_roles": [],
    #: #231 (D119): path prefixes a test copy never takes, on top of docs/research/, var/, artifacts/ and the leak
    #: patterns file. [] = none.
    "test_copy_exclude": [],
    #: #285: stand-in files of the test copy, {"<path in the copy>": "<tracked repo path of the stand-in>"}: the copy
    #: holds the stand-in's bytes at the first path and never the stand-in's own path. {} = none.
    "test_copy_standins": {},
    #: #231 (D119): the git remote (URL or path) the repo's test copy is pushed to; "" = no test copy, a send is refused.
    "test_copy_remote": "",
    #: #224: heavy suite runs (a full suite or more than 500 selected tests) that may run at once on the whole PC, across
    #: every repo (`machine_berths.py`); the rest wait their turn. 0 = no limit.
    "machine_heavy_runs": 1,
    #: #232 (D119): GitHub machines in use at once across every repo on the PC (`machine_berths.acquire_machines`);
    #: the line counts them with the PC's berth. 0 = no limit and nothing recorded.
    "github_machines_total": 20,
    #: #232 (D119): the most GitHub machines one merge holds; it gets fewer only when fewer are free and nothing
    #: waits ahead of it.
    "github_machines_per_merge": 5,
    #: #232 (D119): free GitHub Actions minutes a month of the private repo on its own runners; past it (used + the
    #: median merge) its suites go to the PC until the 1st (UTC) (`github_minutes.over_budget`).
    "github_free_minutes": 2000,
    #: #232 (D119): true = the merge runs its heavy pytest suite on GitHub machines (`merge_github.py`) and falls
    #: back to the PC; false = every suite runs on the PC as before. Needs `test_copy_remote` or `github_own_runners`.
    "github_suite": False,
    #: #232 (D119): true = a private repo with no test copy runs its suites on its own repo's runners (free minutes
    #: counted by `github_minutes.py`); false = such a repo never goes to GitHub.
    "github_own_runners": False,
    #: #352: measured median of start to first test (Linux part), seconds; re-measure with scripts/counts/github_start.py --write.
    "github_start_seconds_linux": 16,
    #: #352: measured median of start to first test (windows part), seconds; re-measure with scripts/counts/github_start.py --write.
    "github_start_seconds_windows": 26,
    #: #352: a run whose expected PC run time is below this (and below the runner's start time) always stays on the PC
    #: (`place_decision.py`); seconds.
    "place_pc_only_floor_seconds": 120,
    #: #352: expected run time on a GitHub machine = expected PC run time x this ratio (`place_decision.py`).
    "github_speed_ratio": 1.0,
    #: #352: the longest a `test run` PC run, or the PC ids of a merge beside its GitHub run, may take, minutes; a run past it is
    #: killed (`test_run.py`, `merge.py`).
    "test_run_timeout_minutes": 60,
    #: #233: a heavy run on the machine line that has waited more than this many minutes goes next, whatever its
    #: repo (`machine_berths.py`); a non-number or an unreadable file is this default.
    "line_max_wait_minutes": 60,
    #: #249: a live PC holder of the machine line whose output file has not grown for more than this many minutes
    #: (and is not frozen by the hold engine) is ended, its tree stopped, so the next waiter takes the berth.
    "berth_silent_max_minutes": 15,
    #: #340: the loopback port of the monitor page (`tools/agentops/monitor_server.py`, bound to 127.0.0.1 only).
    "monitor_port": 8750,
}

#: optional keys nested under one object: the object's default values, merged under the file's own object
#: (a key the file leaves out gets its default; an object the file leaves out is the defaults alone).
NESTED_DEFAULTS = {
    #: #166: `monitor.collector_start_check_s`: seconds after `dispatch --ensure` starts the monitor collector
    #: before its pid is checked (a dead pid is logged to dispatch.out with the collector's last lines).
    "monitor": {"collector_start_check_s": 10},
}


def optional_nested(name: str, key: str, path: Path | None = None):
    """`settings[name][key]` read from the settings file at ``path`` (default: the installation's own), or
    the nested default when the file, the object or the key is absent or unreadable. Never raises."""
    settings_path = Path(path) if path is not None else SETTINGS_PATH
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    given = data.get(name) if isinstance(data, dict) else None
    if isinstance(given, dict) and key in given:
        return given[key]
    return NESTED_DEFAULTS[name][key]


def load(path: Path | None = None) -> dict:
    """Read and validate the settings file; raise one line naming the file and the problem.

    ``path`` overrides the default location (tests only). With no ``path`` it reads ``config/orchestration.json`` at
    the repo root, unless the env ``AGENTOPS_CORE_SETTINGS`` names another file (a test hook, never set in production).
    """
    settings_path = Path(path) if path is not None else Path(os.environ.get(SETTINGS_ENV) or SETTINGS_PATH)
    if not settings_path.is_file():
        raise RuntimeError(f"missing settings file: {settings_path}")
    try:
        raw = settings_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read settings file: {settings_path} ({exc})") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"invalid JSON in settings file: {settings_path} ({exc})") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"invalid settings file: {settings_path} (expected a JSON object)")
    for key in REQUIRED_KEYS:
        if key not in data:
            raise RuntimeError(f"missing key {key!r} in settings file: {settings_path}")
    for key, value in DEFAULTS.items():
        data.setdefault(key, value)
    for name, defaults in NESTED_DEFAULTS.items():
        given = data.get(name)
        data[name] = {**defaults, **(given if isinstance(given, dict) else {})}
    data["suite_free_paths"] = suite_free_globs(data.get("suite_free_paths"))
    #: #323: where the tickets live (default: the repo itself) and where the notes live (default: the tickets repo).
    data.setdefault("tickets_repo", data["repo"])
    data.setdefault("notes_repo", data["tickets_repo"])
    return data


def tickets_repo(path: Path | None = None) -> str:
    """#323: the `owner/name` every core call about issues passes as `--repo` (setting `tickets_repo`,
    default: the repo itself)."""
    return load(path)["tickets_repo"]


#: #425: an `owner/name` GitHub repo name (letters, digits, `-`, `_`, `.`; one slash).
_OWNER_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def mirror_repos(path: Path | None = None) -> list[str]:
    """#425: the `owner/name` repos the dispatcher mirrors into the tickets repo (setting `mirror_repos`, default
    `[]` = no request). A value that is not a list of `owner/name` strings raises ValueError naming the setting."""
    value = load(path)["mirror_repos"]
    if not isinstance(value, list):
        raise ValueError(f"setting 'mirror_repos' must be a list of owner/name strings, not {type(value).__name__}")
    for entry in value:
        if not isinstance(entry, str) or not _OWNER_NAME.fullmatch(entry):
            raise ValueError(f"setting 'mirror_repos' has an entry that is not owner/name: {entry!r}")
    return list(value)


def notes_repo(path: Path | None = None) -> str:
    """#323: the `owner/name` the research notes, project diaries and State files belong to (setting
    `notes_repo`, default: the tickets repo)."""
    return load(path)["notes_repo"]


def suite_free_globs(extra: object) -> list[str]:
    """The core's ``SUITE_FREE_PATHS`` plus the target's own string globs (``extra``, the settings'
    ``suite_free_paths``; absent or not a list = none). Defaults are never removed; repeats are dropped."""
    merged = list(SUITE_FREE_PATHS)
    if isinstance(extra, list):
        for glob in extra:
            if isinstance(glob, str) and glob not in merged:
                merged.append(glob)
    return merged
