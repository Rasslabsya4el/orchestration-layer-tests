"""ORCH dispatcher (issues #50, #60, #191): as few live ORCH background sessions as possible, each
taking every ready issue it can and working them in parallel through its own subagents (D104, no
cap; D110 replaces D104's "exactly one session" with "a further one starts only when every live
session is finishing").

    python -m tools.agentops dispatch [--once] [--max-hours 4]

Launched detached and meant to run for ever. Every tick (`TICK_SECONDS`):

1. Adopt any live `orch ...` session this process does not yet track (a restarted dispatcher, #58,
   #191: every live one, not just the oldest).
2. One shared `gh` read of the claimed issues (open, `ready-for-agent`, assigned) attributes new
   claims to sessions: a finishing session's `held` is frozen (D110), so a claim not already held
   by a frozen session belongs to the one session still taking new work (at most one, by
   construction), but only if it was offered that issue or the issue's latest `assigned` event is
   at or after the session's start (#378); otherwise the assignee is stale, removed, and the issue
   goes the ordinary way (wake / offer).
3. Each live session is polled (`claude agents --json --all`): `stopped` -> ended; `done` or idle
   -> ended unless (#1072) it is not finishing, holds no claimed issue and an issue it parked carries
   its own `parked on merge <result file>` checkpoint younger than the merge ceiling (logged once as
   `idle, waiting on merge of #N`; the merge result's wake follows as for any new candidate), or its subagents still run (a background ORCH ends its turn while they work and is
   woken by their notifications; #977: a subagent whose latest notification is interim -- it stopped
   with background work of its own still running -- counts as running for up to 30 min) or, idle in a live state, it still holds a claimed issue (it
   waits on the owner: left alone, logged once, and never counted as finishing). At the end EVERY
   held issue is resolved on its own (`_resolve_outcome`: closed / parked / `needs-info`, or the
   no-checkpoint path for an issue still claimed; #977: a note of someone else that names no ORCH
   session does not hide the session's own newest checkpoint, so the issue is `released`). Over `--max-hours` (counted per session) ->
   stop, and comment + unassign each held issue still claimed. While polling, a session becomes
   finishing (final) the moment it says so itself (`python -m tools.agentops finishing`, D110) or
   its context, read from its transcript, is at or over `context_line`; a finishing session is
   never woken and never offered an issue again. A finishing session still listed working/busy whose
   last transcript turn is over 10 min old, with no subagent running, is ended too (#727).
4. A further session starts when at least one issue is eligible (`GhCli.ready_for_agent_issues`,
   an issue with an open native blocker dropped (#53) or labelled `in-cloud` (#121) or `parked` (#1074), runs-daemon
   results first, then oldest) AND every live session is finishing (vacuously true with none live);
   `ClaudeCli.start`, `build_prompt`.
5. A live session that is not ended and not finishing this tick is checked for a candidate it does
   not hold (`_eligible_candidates`, same filter as step 4): a new one gets one wake this tick
   (`ClaudeCli.wake`, naming every new candidate at once, and the exact `finishing` command with
   this session's id); an unclaimed one is woken again only after `WAKE_COOLDOWN_SECONDS`, capped
   at `MAX_WAKES_PER_ISSUE` per issue per session (#137).

Every live session and its held issues are written to `STATE_FILE` every tick, for whoever shows
them (#191: the old top-level keys mirror the one non-finishing session, plus a `sessions` list).

This file is the core's dispatcher and knows nothing of the installation it runs in (ADR 0109): the
local modules the settings file names are loaded by `extensions.py` on the first `Dispatcher` built
(and by the CLI before it parses), and join the tick before or after step 1/2, the ready queue of
steps 2 and 3, the `dispatch` flags, and the classes below. With none named the loop above is the
whole behaviour.

`GhCli` and `ClaudeCli` are the only things that touch the world (gh, the real `claude.exe`); both
are injected so the tests run the whole loop with neither. Events are appended with
`util.append_jsonl` to `artifacts/runs/events.jsonl`, where every other part of the installation
reads them.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AbstractSet, Any, Callable, Iterable, Mapping, Sequence

from . import claude_bg
from .comment_markers import MACHINE_PREFIXES, ORCH_FINISHING, first_line
from .brain_chat_lookup import (
    BrainChatAway, FallbackLogger, Resolved, _folder_key, home_alert_text, read_home_brain_title, resolve_brain_chat,
)
from .brain_fresh import awaiting_owner_message
from .brain_notice import MAX_ATTEMPTS, NEEDS_INFO_WORD, UNBLOCKED, BrainNotifier, IssueRead, brain_target
from .detach import ENV_RESULTS_DIR, RESULTS_DIR, result_path_for
from .core_check import MANIFEST_PATH, read_manifest
from .core_format import human_duration
from .core_issue import _EXTENSION_RE, _PATH_TOKEN_RE, find_open_core_issue, write_core_issue
from .core_settings import DEFAULTS as _SETTING_DEFAULTS, load as _load_settings, optional_nested
from .orch_silence import silence_verdict, silent_orch_limit_seconds
from .extensions import registry
from . import priority_rank
from .gh_call import run_gh
from .loop_stop import LoopStatus, loop_status
from .losses_daily import LossesDaily, default_compute
from .orphans import ProcRow, real_process_table, snapshot_owned_pids, sweep_orphans, sweep_stale_whole_disk_searches
from .stop import stop_processes
from .unblocker_start import UnblockerStarts, answer_after, answer_key, limbo_line, way_line
from .parked_return import GhRunner, ParkedReturn, default_target_core
from .util import (
    SESSION_ID_ENV_VARS, append_jsonl, holder_is_live, human_duration, no_window_flags,
    now as _now_str, pid_alive, read_pid_lock,
)
from .wake import judge_reply
from .worktree import ROOT, main_checkout_root

#: the installation's own repo slug, read from `config/orchestration.json` (issue #147).
REPO = _load_settings()["repo"]
#: the part after the slash (issue #214): carried in every ORCH session's own name
#: (`orch <REPO_NAME> #N #M`) so a wake by name is never ambiguous between two repos' dispatchers
#: sharing one Claude Code install.
REPO_NAME = REPO.rsplit("/", 1)[-1]
#: D110: a session is finishing once its context, read from its own transcript, is at or over this.
CONTEXT_LINE = _load_settings()["context_line"]
#: #1120: past `context_line` a session only finishes what it holds; it parks a ticket for context only past this.
CONTEXT_HARD_LINE = _load_settings()["context_hard_line"]
#: #4: the most ready tickets one ORCH session is offered at its start (oldest first); the rest wait.
ORCH_OFFER_MAX = _load_settings()["orch_offer_max"]


def checked_offer_max(value: object) -> tuple[int, bool]:
    """#106: `orch_offer_max` as the dispatcher uses it, and whether `value` was good. Only an int >= 1
    is (a bool is not: `True` would read as 1); anything else reads as the default."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value, True
    return _SETTING_DEFAULTS["orch_offer_max"], False
#: #906: a live Monitor (a watch the session armed) keeps a session from being ended, but only while
#: the session's last transcript record is younger than this (`watch_hold_cap_minutes`, default 60):
#: a Monitor forgotten for good must never keep a session alive for ever. 60 min is well above the
#: longest merge / job watch seen (#902: ~11 min) and bounded by the Monitor tool's own timeout.
WATCH_HOLD_CAP_SECONDS = float(_load_settings().get("watch_hold_cap_minutes", 60)) * 60
#: #265: an ORCH session silent (no timed record of its own or of a running subagent) this long while a background
#: shell has no end record is woken once (not finishing) or ended (finishing, or still silent this long after the
#: wake): `silent_orch_minutes`, default 30. A shell never keeps a session alive past it.
SILENT_ORCH_LIMIT_SECONDS = silent_orch_limit_seconds(_load_settings())
ORCH_DOC = "docs/agents/orch.md"
UNBLOCKER_DOC = ".claude/agents/unblocker.md"


def area_labels() -> list[str]:
    """#319: the area labels of `config/orchestration.json` (`areas`); empty = the one-area rule is off."""

    return list(_load_settings().get("areas") or [])


def area_problem(labels, areas: list[str]) -> str | None:
    """`None` when `labels` hold exactly one of `areas` (or `areas` is empty), else why not."""

    if not areas:
        return None
    held = [a for a in areas if a in labels]
    if not held:
        return "no area"
    if len(held) > 1:
        return "two areas: " + ", ".join(held)
    return None
EVENTS_FILE = ROOT / "artifacts" / "runs" / "events.jsonl"
#: D110: one durable mark file per session id, written by `python -m tools.agentops finishing`.
FINISHING_DIR = ROOT / "artifacts" / "runs" / "finishing"
TICK_SECONDS = 30.0
#: #200: `_retaken_claims` reads an issue's events at most this often per session.
RETAKEN_READ_SECONDS = 60.0
#: #34: how long one tick waits for the auto core update before it defers the ORCH start; well under
#: `TICK_SECONDS`, so a core update held behind the merge line never stands the loop still. Defined in
#: `core_update.py` (#49: the home merge's push to a target waits the same bound).
from .core_update import CORE_UPDATE_WAIT_S  # noqa: E402
#: #727: a finishing session whose last turn is older than this, with no subagent running, is ended
#: though the listing still says working/busy (a shell of its own, left alive, keeps it "busy").
FINISHING_IDLE_END_SECONDS = 600
#: #977: a subagent whose latest task notification is interim (it stopped with background work of its
#: own still running) counts as running for at most this long from that notification: every wait is
#: bounded (the session of the case waited on a 25 min deadline).
INTERIM_SUBAGENT_CAP_SECONDS = 30 * 60
#: #766: an idle session holding claims with no subagent running: woken once at this mark (not
#: finishing), ended this long after the wake, or at this mark when finishing.
IDLE_END_SECONDS = 600
#: #1072: an idle, non-finishing session whose parked issue waits on a live merge is kept (not ended)
#: while that issue's parking checkpoint is younger than this: the merge lock's own ceiling
#: (`merge_lock_max_age_min`, default 30 min, `merge.py`) plus 5 min.
MERGE_WAIT_CEILING_SECONDS = (float(_load_settings().get("merge_lock_max_age_min", 30)) + 5) * 60
DEFAULT_MAX_HOURS = 4.0
#: An issue is not re-dispatched a fourth consecutive time; the third failure labels `needs-info`.
MAX_CONSECUTIVE_NO_CHECKPOINT = 3
#: #931: a session whose transcript ends in an API error (an outage, a spent limit, an auth error) is
#: not the ticket's fault: no new session starts for this many seconds after it.
OUTAGE_WAIT_S = 600
NEEDS_INFO_LABEL = "needs-info"
READY_LABEL = "ready-for-agent"
#: #154: the closed-`ready-for-agent` sweep runs at most this often (`self.clock()` seconds); the first one
#: on the first normal-mode tick, a failed one retried at the next interval.
CLOSED_LABEL_SWEEP_SECONDS = 600.0
#: #1074: an issue waiting on a named event; orthogonal to the role labels, never offered while it carries it.
PARKED_LABEL = "parked"
#: #874: a merge result relabels an issue only when it carries none of these (someone parked it on purpose).
RELABEL_SKIP_LABELS = frozenset({"needs-info", "ready-for-human", "wontfix", "needs-triage"})
RELABEL_EVERY_SECONDS = 60.0
#: #850: markers of the machine's own comments (`merge_handback.MARKER`, `runs_marker.RUNS_DAEMON_MARKER`).
MACHINE_COMMENT_MARKERS = ("<!-- merge result -->", "<!-- runs-daemon result -->", "<!-- merge never started -->")
#: #179: first line of the dispatcher's one comment on a ticket whose merge never started; a restart reads it back.
MERGE_NEVER_STARTED_MARKER = "<!-- merge never started -->"
#: #852: the dispatcher's own alarm comment (it names the session id but is nobody's checkpoint).
ALARM_COMMENT_TEXT = "ended without a closing or parking checkpoint"
#: #852: words of a parking or closing checkpoint, from the protocol's own checkpoints
#: (`docs/agents/orch.md` lines 86, 173, 225, 237, 246-249); a claim, cut or progress note has none.
PARKING_WORDS = (
    r"\bpark(?:ed|ing)?\b", r"\bneeds-info\b", r"\bclos(?:ing|ed)\b", r"\bfinishing\b",
    r"\bhands? back\b", r"\bjobs? still (?:queued|running)\b",
)
_PARKING_RE = re.compile("|".join(PARKING_WORDS), re.IGNORECASE)
#: #852: another session's act named inside a checkpoint ("the first ORCH f50e4a1b parked it") is not this one's.
_OTHER_ACT_RE = re.compile(r"\bORCH\s+(?:session\s+)?[0-9a-f]{4,}\s+(?:\w+\s+)?(?:parked|closed|finished)\b", re.IGNORECASE)


def _parks_or_closes(body: str) -> bool:
    """#852: a checkpoint's HEADLINE (its first non-empty line, others' acts removed) holds a parking or
    closing word; a cut table further down that says `needs-info` as a plan does not count."""

    head = next((l for l in body.splitlines() if l.strip()), "")
    return _PARKING_RE.search(_OTHER_ACT_RE.sub("", head)) is not None
#: #852: a comment of the repo's two other agent roles (it opens with the role, or is the autopilot's verdict).
_OTHER_ROLE_RE = re.compile(
    r"^\W*(?:the\s+)?(?:brain|key" r"master|judged by the brain|close accepted)\b|autopilot #\d+", re.IGNORECASE)
#: #852: the author session a checkpoint names (`ORCH <id>` / `ORCH session <id>`).
_ORCH_AUTHOR_RE = re.compile(r"\bORCH\s+(?:session\s+)?([0-9a-f]{4,})", re.IGNORECASE)

#: #137: a candidate the live session does not hold is not woken again within this cooldown, and
#: never more than `MAX_WAKES_PER_ISSUE` times by the same session.
WAKE_COOLDOWN_SECONDS = 600.0
MAX_WAKES_PER_ISSUE = 3

#: #229: an issue offered at a session's start gets no wake in the first stretch after the start --
#: a start to its claims measured 1 min 40 s to 2 min 20 s (#191's number 10), so 3 min covers it
#: without waiting a full `WAKE_COOLDOWN_SECONDS`. After this grace the rules above apply unchanged,
#: and that first wake counts as wake 1. An issue never offered to this session is not covered by
#: this grace (woken about as today, from the moment it becomes a candidate).
WAKE_GRACE_SECONDS = 180.0

#: #213 safety net: the listing's own words for a session stopped on a prompt nobody will answer.
#: Seen for a tracked session, it is reported to the brain -- the refusal above should have made it
#: impossible, so one of these is a defect worth a message, not a routine wait.
PERMISSION_WAIT = "permission prompt"
BLOCKED_STATE = "blocked"
#: the same stuck session is reported at most this often (and never before the tick after the one
#: that first saw it, so the message carries a wait a human can read).
PERMISSION_REPORT_COOLDOWN = 900.0

#: #225 acceptance row 5: how often a tick pays for a whole-disk `find`/`grep`/`rg` sweep.
WHOLE_DISK_SWEEP_INTERVAL_SECONDS = 60.0

#: #225's own follow-up (the OPEN RISK): how often a tick pays for a live snapshot of "whose chain
#: reaches me" for every session still alive, folded into `_Current.owned_pids`. Same cadence as the
#: whole-disk sweep above -- one throttled `ps -eW` + CIM read is already the cost either pays.
OWNED_PIDS_SNAPSHOT_INTERVAL_SECONDS = 60.0

#: the one-shot wake sender (#123): `claude.exe -p --model haiku --output-format json
#: --allowedTools=SendMessage --name dispatcher`.
WAKE_MODEL = "haiku"
WAKE_SENDER_NAME = "dispatcher"

#: States `claude agents --json --all` reports for a session that is still running (measured
#: 2026-09-26: `working` while busy or waiting; `done` after its turn; `stopped` after
#: `claude stop`). Any other state ends the session (#58, 1b). A missing state counts as live:
#: an unknown shape is never killed, it waits for `--max-hours`.
LIVE_STATES = frozenset({"working", None})
#: #1130: a restart also adopts an ORCH that is `blocked` (a permission prompt or a question to the owner);
#: the poll then ends it by its own rule. `stopped` and `failed` are never adopted.
ORCH_ADOPT_STATES = LIVE_STATES | frozenset({BLOCKED_STATE})
#: `build_command`'s `--name` ("orch #54 #55 #56"; the older "orch #N" too): adoption, #58.
_ORCH_NAME_RE = re.compile(r"^orch(?:\s|$)")
_ISSUE_RE = re.compile(r"#(\d+)")
#: how many issue numbers the session name shows (the prompt lists them all).
NAME_SHOWS = 5
#: the live session and its held issues, for whoever shows them (#52).
STATE_FILE = ROOT / "artifacts" / "runs" / "orch_session.json"
#: what the brain chat was told about a parked, closed or needs-info issue (`brain_notice.py`), so a restart repeats nothing.
BRAIN_NOTICE_FILE = ROOT / "artifacts" / "runs" / "brain_notices.json"
#: #209: the unblocker sessions started, one per trigger (`unblocker_start.py`), so a restart repeats none.
UNBLOCKER_START_FILE = ROOT / "artifacts" / "runs" / "unblocker_starts.json"
#: the day the brain chat was last sent the losses note (`losses_daily.py`), so a restart repeats nothing.
LOSSES_DAILY_FILE = ROOT / "artifacts" / "runs" / "losses_daily.json"
#: #28 unit 3: the failed-target notice's state (results read, lines held for a chat that is away).
CORE_NOTICE_FILE = ROOT / "artifacts" / "runs" / "core_failure_notices.json"
#: a result older than this when there is no usable state file is history (not announced); a newer one is.
CORE_NOTICE_HISTORY_SECONDS = 6 * 3600.0
#: #42: a reason already announced for one repo in one brain chat is not announced again within this 6 h.
CORE_REASON_QUIET_SECONDS = 6 * 3600.0
_LEGACY_CORE_LINE = re.compile(r"^core update failed in (.*?) after merge (.*?): (.*)$")


def _held_core_row(row: dict) -> dict | None:
    """A held row of the state file: this unit's fields, or the `line` a row held before #42 is parsed back into them."""
    if "repo" in row:
        return dict(row)
    found = _LEGACY_CORE_LINE.match(str(row.get("line") or ""))
    if not found:
        return None
    return {"target": row.get("target"), "repo": found.group(1), "branch": found.group(2),
            "reason": found.group(3), "attempts": int(row.get("attempts") or 0)}
#: the losses note's computation reads session transcripts (about a minute per repo): `LossesDaily.check` is
#: called from the tick, throttled here.
LOSSES_DAILY_CHECK_SECONDS = 3600.0
#: the only outcomes the brain chat is told; `needs_info` (#213: a question left for the brain) joins `parked` and
#: `closed`, the rest are not.
BRAIN_NOTICE_WORDS = frozenset({"parked", "closed", NEEDS_INFO_WORD})
#: #213: a live session's `needs-info` issue is read again at most this often (300 s, i.e. 5 minutes), to tell the brain
#: of a checkpoint newer than the last notice; every other classified issue is read once until a session claims it again.
NEEDS_INFO_REREAD_SECONDS = 300
#: #209: an unblocker start is read for its brain-answer at most this often per ticket.
UNBLOCKER_READ_EVERY_SECONDS = 60.0
#: #209/#222: a start this old with no brain-answer after it is the limbo case: its line goes to the brain chat once
#: and its record is done (never read again).
UNBLOCKER_LIMBO_SECONDS = 1800.0
#: a listed session in one of these states is not a live brain chat.
BRAIN_DEAD_STATES = frozenset({"stopped", "dead"})
#: how many parent links `_autopilot_live_for` follows up from an issue.
BRAIN_NOTICE_ANCESTOR_DEPTH = 6
BRAIN_NOTICE_AUTOPILOT_LABEL = "autopilot"
#: #1129: the goal the dispatcher gives up on (`autopilot.py` `AUTOPILOT_STUCK_LABEL`, the same string).
BRAIN_NOTICE_AUTOPILOT_STUCK_LABEL = "autopilot-stuck"
#: the autopilot's idle waits (#280), beside the ORCH state file: a wait outlives a dispatcher restart.
AUTOPILOT_WAITS_FILE = ROOT / "artifacts" / "runs" / "autopilot_waits.json"
#: #175: the busy-PC start hold's count and cap latch, so a dispatcher restart keeps them.
ADMISSION_HOLD_FILE = ROOT / "artifacts" / "runs" / "admission_hold.json"
#: one dispatcher at a time (#201): pid + start time of whoever runs the loop.
#: #357: anchored on the repo's MAIN checkout, not the running one -- a loop started from a worktree
#: would otherwise take a lock file of its own and run beside the main checkout's loop.
LOCK_PATH = main_checkout_root(ROOT) / "var" / "agentops" / "dispatcher.lock"
#: `--ensure`'s detached start appends here, same file the loop itself logs to.
DISPATCH_OUT = ROOT / "artifacts" / "runs" / "dispatch.out"

#: dotted numeric version directories under `%APPDATA%\Claude\claude-code\`.
_VERSION_RE = re.compile(r"^(\d+(?:\.\d+)*)$")


# -- the no-checkpoint core issue -----------------------------------------------------------------

def _scrub_non_manifest_paths(text: str, manifest: frozenset[str]) -> str:
    """Every path-like token in ``text`` that does not name a file of the core manifest becomes
    ``<path>`` -- ``write_core_issue`` refuses any such token outright (its own
    ``_path_like_tokens`` / ``_validate_paths``), so a log line built from this dispatcher's own
    event file (a path no core repo carries) must be scrubbed before the call."""

    def repl(match: re.Match) -> str:
        raw = match.group(0)
        if "/" not in raw and "\\" not in raw and not _EXTENSION_RE.search(raw):
            return raw
        return raw if raw.replace("\\", "/") in manifest else "<path>"

    return _PATH_TOKEN_RE.sub(repl, text)


def _default_core_issue_writer(*, role: str, kind: str, what: str, error: str, log: str, did: str,
                               session: str, caught_by: str | None = None,
                               what_human: str | None = None) -> None:
    """The dispatcher's default core-issue writer seam: looks up an already-open core issue naming
    the same defect first (by the English `what`), so a repeat becomes a comment rather than a new
    issue. `what_human` is the Russian What line the issue body shows."""

    same = find_open_core_issue(kind=kind, what=what)
    write_core_issue(role=role, kind=kind, what=what, error=error, log=log, did=did,
                     session=session, same=same, caught_by=caught_by, what_human=what_human)


def _default_stream_keeper():
    """The real entry point's stream keeper: the settings' repo, the real `gh`, the dispatcher's log."""

    from .streams import StreamKeeper
    return StreamKeeper(REPO, log=_log)


def _log(message: str) -> None:
    sys.stderr.write(f"{_now_str()} orch-dispatch: {message}\n")
    sys.stderr.flush()


#: A gh failure's text that says the call waits or fails on a rate limit (primary, secondary or the abuse check).
_RATE_LIMIT_TEXT_RE = re.compile(r"rate limit|RATE_LIMIT|submitted too quickly", re.I)


def _issues_tag(numbers: Iterable[int] = ()) -> str:
    """The ` [issues #N #M]` suffix of a log line, naming the issues it holds back in the order given; "" with none."""

    found = [int(n) for n in numbers]
    return "" if not found else " [issues" + "".join(f" #{n}" for n in found) + "]"


def _rate_limit_tag(result: subprocess.CompletedProcess, numbers: Iterable[int] = ()) -> str:
    """The suffix of a failed call's log line: the issues it was about, when its text is a rate limit; "" otherwise."""

    if not _RATE_LIMIT_TEXT_RE.search(str(result.stderr or "")):
        return ""
    return _issues_tag(numbers)


# -- finding claude.exe (requirement 5, shared with agents.cmd / agents.ps1) ---------------------
#
# The desktop app is an MSIX package: `%APPDATA%\Claude\claude-code\<ver>\claude.exe` only exists
# for a process running INSIDE the app's container (Claude Code's own Bash sees it that way); a
# plain terminal gets "not recognized". The real file for everyone else is under the package's
# `LocalCache`: `%LOCALAPPDATA%\Packages\Claude_<publisher>\LocalCache\Roaming\Claude\claude-code\
# <ver>\claude.exe` (measured on this machine: versions 2.1.275 and 2.1.280 both present). So the
# order is: `CLAUDE_EXE` -> newest under the package's LocalCache -> newest under `%APPDATA%`
# (still tried, for a process already inside the container) -> `claude` on PATH.

def _version_key(name: str) -> tuple:
    match = _VERSION_RE.match(name)
    if not match:
        return (-1,)
    return tuple(int(part) for part in match.group(1).split("."))


def _newest_version_exe(base: Path) -> Path | None:
    if not base.is_dir():
        return None
    versions = [p for p in base.iterdir() if p.is_dir() and _version_key(p.name) != (-1,)]
    if not versions:
        return None
    versions.sort(key=lambda p: _version_key(p.name), reverse=True)
    for version in versions:
        # flat (`<ver>/claude.exe`) before 2026-10-02, one level deeper (`<ver>/<hash>/claude.exe`) since
        flat = version / "claude.exe"
        if flat.is_file():
            return flat
        deeper = [p for p in version.glob("*/claude.exe") if p.is_file()]
        if deeper:
            return max(deeper, key=lambda p: p.stat().st_mtime)
    return None


def _innermost_frame(exc: BaseException) -> str:
    """` at <file>:<line> in <function>` of the frame that raised, or "" without a traceback."""

    import traceback

    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return ""
    frame = frames[-1]
    return f" at {Path(frame.filename).name}:{frame.lineno} in {frame.name}"


def _packages_claude_code_dirs(local_appdata: str) -> list[Path]:
    """Every `Packages\\Claude_*\\LocalCache\\Roaming\\Claude\\claude-code` under
    `%LOCALAPPDATA%` -- globbed rather than a fixed publisher id, since that id is
    installation-specific (`Claude_pzs8sxrjxfjjc` on this machine, per the lead)."""

    packages = Path(local_appdata) / "Packages"
    if not packages.is_dir():
        return []
    return sorted(packages.glob("Claude_*/LocalCache/Roaming/Claude/claude-code"))


class ClaudeExeNotFound(RuntimeError):
    """`ClaudeCli` could not look claude.exe up (#611): a plain `Exception`, so a tick that hits it is
    a failed tick and the loop goes on, where `find_claude_exe`'s own `SystemExit` would end it."""


def find_claude_exe(env: dict[str, str] | None = None) -> Path:
    """`CLAUDE_EXE` overrides everywhere; else the newest version under the MSIX package's
    `LocalCache` (where a plain terminal finds it), then under the app-container `%APPDATA%`
    (where Claude Code's own Bash finds it), then `claude` on PATH."""

    import os
    import shutil

    env = env if env is not None else os.environ
    override = env.get("CLAUDE_EXE")
    if override:
        return Path(override)

    local_appdata = env.get("LOCALAPPDATA")
    if local_appdata:
        for base in _packages_claude_code_dirs(local_appdata):
            found = _newest_version_exe(base)
            if found is not None:
                return found

    appdata = env.get("APPDATA")
    if appdata:
        found = _newest_version_exe(Path(appdata) / "Claude" / "claude-code")
        if found is not None:
            return found

    on_path = shutil.which("claude")
    if on_path:
        return Path(on_path)

    searched = ["CLAUDE_EXE (unset)"]
    if local_appdata:
        searched += [str(b) for b in _packages_claude_code_dirs(local_appdata)] or [
            str(Path(local_appdata) / "Packages" / "Claude_*" / "LocalCache" / "Roaming" / "Claude"
                / "claude-code")]
    if appdata:
        searched.append(str(Path(appdata) / "Claude" / "claude-code"))
    searched.append("claude on PATH")
    raise SystemExit("no claude.exe found; searched " + "; ".join(searched)
                            + " (set CLAUDE_EXE to override)")


# -- gh -------------------------------------------------------------------------------------------

@dataclass(slots=True)
class IssueSummary:
    """One candidate issue: enough to rank it (requirement 2's ordering)."""

    number: int
    created_at: str
    #: the body of the issue's latest comment, `""` when it has none. The core reads nothing out of
    #: it; a local module orders the queue on it (`extensions.py`, the `candidates` point).
    latest_comment: str = ""
    #: Open issues this one is natively blocked by (`gh issue list --json blockedBy`, #53). That
    #: field also lists CLOSED blockers, so only nodes whose state is not CLOSED land here.
    open_blockers: tuple[int, ...] = ()
    #: the issue's labels, for a local module that keeps a label of its own out of the offer.
    labels: tuple[str, ...] = ()
    #: `createdAt` of the issue's latest comment and the issue's `updatedAt` (`""` when unknown):
    #: what `_eligible_candidates` compares against a parked issue's `ParkMark` (#359).
    latest_comment_at: str = ""
    updated_at: str = ""
    #: #924: every comment as `(createdAt, body)`, in listing order: what the loop-stop counter reads.
    comments: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ParkMark:
    """#359: where a parked issue stood when its session let it go: `comment_at` = `createdAt` of
    its last comment (the parking checkpoint), `updated_at` = its `updatedAt` right after the
    dispatcher's own unassign. A comment after `comment_at` or any change after `updated_at` (a
    label event) puts the issue back into the pool."""

    comment_at: str
    updated_at: str
    #: #818: the checkpoint says "parked on merge": the merge's `<!-- merge result -->` comment is
    #: awaited, so the issue is pending though no job of its own is.
    on_merge: bool = False
    #: #5: `createdAt` of the checkpoint that names the event the ticket waits for (`names_event`); `""` when
    #: none does. `finishing_only`: the session's newest comment is its finishing note and no checkpoint
    #: names an event, so there is nothing to wait for.
    reason_at: str = ""
    finishing_only: bool = False


@dataclass(slots=True)
class IssueStatus:
    """What `_resolve_outcome` needs to read back after a session ends (requirement 3)."""

    number: int
    open: bool
    labels: tuple[str, ...]
    assignees: tuple[str, ...]


#: `gh issue list --json` fields of a candidate. `blockedBy` is `{"nodes": [{number, state, ...}],
#: "totalCount"}` and counts closed blockers too (checked on #53 with gh 2.101), hence the state filter.
CANDIDATE_FIELDS = "number,createdAt,updatedAt,comments,blockedBy,labels"


def parse_candidates(data: Any) -> list[IssueSummary]:
    """`gh issue list --json CANDIDATE_FIELDS` output -> `IssueSummary`s (no filtering here)."""

    out: list[IssueSummary] = []
    for item in data if isinstance(data, list) else []:
        comments = item.get("comments") or []
        latest = comments[-1] if comments else None
        body = (latest or {}).get("body") or ""
        nodes = ((item.get("blockedBy") or {}).get("nodes")) or []
        blockers = tuple(sorted(int(n["number"]) for n in nodes
                                if str(n.get("state") or "").upper() != "CLOSED" and "number" in n))
        out.append(IssueSummary(
            number=int(item["number"]), created_at=str(item.get("createdAt") or ""),
            latest_comment=body, open_blockers=blockers,
            labels=tuple(str(label.get("name", "")) for label in item.get("labels") or []),
            latest_comment_at=str((latest or {}).get("createdAt") or ""),
            updated_at=str(item.get("updatedAt") or ""),
            comments=tuple((str(c.get("createdAt") or ""), str(c.get("body") or "")) for c in comments),
        ))
    return out


_RESULT_FILE_RE = re.compile(r"Result (\S+?\.json)\b")  #: the result comment's `Result <path>.json;`
_PARK_FILE_RE = re.compile(r"(?:result file|parked on merge)\s+(\S+?\.json)\b", re.IGNORECASE)  #: the park's file
_MERGE_FILE_RE = re.compile(r"(?<!\d)\d{8}T\d{6}-merge-[^\s`'\")]+\.json")  #: #208: a merge result file by its name, any path prefix
_MERGE_OF_RE = re.compile(r"merge of\s+([\w.-]+)", re.IGNORECASE)  #: #208: `merge of <branch>`
_REQUEUED_RE = re.compile(r"\bre-?queued\b", re.IGNORECASE)  #: #208: `re-queued` / `requeued`


def _result_basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _branch_names(text: str) -> set[str]:
    """#208: the branches a text names as `merge of <branch>`. A token with no digit and no hyphen is prose
    (`the merge of the branch`) and names none."""

    names: set[str] = set()
    for token in _MERGE_OF_RE.findall(text):
        token = token.rstrip(".")
        if any(ch.isdigit() or ch == "-" for ch in token):
            names.add(token.casefold())
    return names


def _waits_on_same_merge(result: dict, park: dict) -> bool:
    """#67 + #208: the park waits on the result's own merge unless it names another one: a result file with another
    basename, another branch (`merge of <branch>`), or a re-queue (`re-queued` / `requeued`). A park naming the
    same file, or no file, is the same merge (#874: a result that landed before the park was written is still
    caught). Basenames, `\\` and `/` alike, case-insensitive."""

    park_body = str(park.get("body") or "")
    result_body = str(result.get("body") or "")
    if _REQUEUED_RE.search(park_body):  # #208: a re-queued merge is another merge
        return False
    result_branches = _branch_names(result_body)
    if result_branches and _branch_names(park_body) - result_branches:  # #208: another branch
        return False
    park_files = {_result_basename(path).casefold()  # #208: any path, any merge-file shape
                  for pattern in (_PARK_FILE_RE, _MERGE_FILE_RE) for path in pattern.findall(park_body)}
    if not park_files:  # the writer names no file: the same merge
        return True
    result_named = _RESULT_FILE_RE.search(result_body)
    if result_named is None:  # the writer always names its file; an unreadable one stays unseen, never dropped
        return True
    return park_files <= {_result_basename(result_named.group(1)).casefold()}


def _unseen_merge_result(comments: list[dict]) -> dict | None:
    """#874: the merge-result comment nobody has acted on: the newest comment itself, or the one right
    before a "parked on merge" checkpoint (written after the merge was queued, so it never saw a
    result that landed first). `None` otherwise."""

    def is_result(c: dict) -> bool:
        #: #990: a result is a comment whose first line is the marker; a comment that only quotes it is not one
        return str(c.get("body") or "").lstrip().startswith("<!-- merge result -->")

    if not comments:
        return None
    if is_result(comments[-1]):
        return comments[-1]
    if len(comments) >= 2 and is_result(comments[-2]) and (
            "parked on merge" in str(comments[-1].get("body") or "").lower()
            or _PARK_FILE_RE.search(str(comments[-1].get("body") or ""))):
        return comments[-2] if _waits_on_same_merge(comments[-2], comments[-1]) else None  # #67
    return None


def checkpoint_holder_of(comments: list[dict], session_ids: tuple[str, ...]) -> tuple[str | None, bool]:
    """#918: the live session (one of `session_ids`) that posted the issue's newest ORCH checkpoint
    (a comment whose first line names `ORCH <id8>`; machine comments and the dispatcher's alarm are
    skipped), and whether a later comment by someone else (neither machine nor alarm) followed it.
    `(None, False)` when that newest checkpoint is not a live session's or there is none."""

    ids = [i for i in session_ids if i]
    newer_foreign = False
    for c in reversed(comments):
        body = str(c.get("body") or "")
        if any(m in body for m in MACHINE_COMMENT_MARKERS) or ALARM_COMMENT_TEXT in body:
            continue
        first = body.lstrip().splitlines()[0] if body.strip() else ""
        m = _ORCH_AUTHOR_RE.search(first)
        if m is None:
            newer_foreign = True
            continue
        who = m.group(1).lower()
        for i in ids:
            if i.lower().startswith(who) or who.startswith(i.lower()):
                return i, newer_foreign
        return None, False
    return None, False


@dataclass(frozen=True, slots=True)
class MergeWait:
    """#1072: a session's parking checkpoint `parked on merge <result file>`: when it was written, the
    result file it names, and whether the merge's `<!-- merge result -->` comment is already on the issue."""

    comment_at: datetime
    result_file: str
    result_posted: bool


_PARKED_ON_MERGE_RE = re.compile(r"parked on merge\s+(\S+)", re.IGNORECASE)


def _is_merge_result(comment: dict) -> bool:
    return str(comment.get("body") or "").lstrip().startswith("<!-- merge result -->")


def checkpoint_merge_wait(comments: list[dict], session_ids: tuple[str, ...]) -> MergeWait | None:
    """#1072: the session's newest own comment (a comment naming one of `session_ids`; machine comments
    and the dispatcher's alarm are skipped) when it is a `parked on merge` checkpoint, else `None`.
    `result_posted`: a merge-result comment follows it, or sits right before it and the park names no
    other result file (#874: a result that landed before the checkpoint was written; #67: a result the
    park's newer merge already answered does not count)."""

    ids = [i for i in session_ids if i]
    for index in range(len(comments) - 1, -1, -1):
        body = str(comments[index].get("body") or "")
        if any(m in body for m in MACHINE_COMMENT_MARKERS) or ALARM_COMMENT_TEXT in body:
            continue
        if not any(i in body for i in ids):
            continue
        found = _PARKED_ON_MERGE_RE.search(body)
        at = _parse_iso(str(comments[index].get("createdAt") or ""))
        if found is None or at is None:
            return None
        posted = (any(_is_merge_result(c) for c in comments[index + 1:])
                  or (index >= 1 and _is_merge_result(comments[index - 1])
                     and _waits_on_same_merge(comments[index - 1], comments[index])))  # #67: not a result already answered
        return MergeWait(comment_at=at, result_file=found.group(1).strip("`'\".,;:()<>"), result_posted=posted)
    return None


def is_finishing_note(body: str) -> bool:
    """#5: the ORCH finishing note (`finishing_note`): `ORCH <id8>, finishing: ...` (#196: the headline says the
    session is still working; the old `finishing: parked` headline of #1054 is still recognised)."""

    line = first_line(body or "")
    return bool(line and ORCH_FINISHING.match(line.lstrip("#*_ \t")))


def names_event(body: str) -> bool:
    """#5: a checkpoint that names the event a ticket waits for: `parked on merge ...` or a
    `<!-- parked-on-run: ... -->` line. The finishing note, a merge / daemon result comment and the
    dispatcher's alarm never do (machine comments are skipped the way `checkpoint_holder_of` skips them)."""

    text = body or ""
    if is_finishing_note(text) or ALARM_COMMENT_TEXT in text or any(m in text for m in MACHINE_COMMENT_MARKERS):
        return False
    return "parked on merge" in text.lower() or (first_line(text) or "").startswith(MACHINE_PREFIXES)


def park_reason_index(comments: list[dict]) -> int | None:
    """#5: index of the newest comment that `names_event`; `None` when none does."""

    for index in range(len(comments) - 1, -1, -1):
        if names_event(str(comments[index].get("body") or "")):
            return index
    return None


def _result_file_path(path_text: str) -> Path:
    path = Path(path_text.strip("`'\".,;:()<>"))
    return path if path.is_absolute() else ROOT / path


def merge_landing(comments: list[dict], reason_at: str) -> str | None:
    """#5: whether the merge the ticket is parked on has ended, whatever the order of the park and the
    result: the rc (`"0"`, `"1"`, `"?"` when unreadable) of its result file, or of the `<!-- merge result -->`
    comment at or after the park / right before it for the same merge (#67); `None` while it still runs, when
    the park (the comment made at `reason_at`) names no merge, or `reason_at` is empty."""

    if not reason_at:
        return None
    index = next((i for i in range(len(comments) - 1, -1, -1)
                  if str(comments[i].get("createdAt") or "") == reason_at
                  and names_event(str(comments[i].get("body") or ""))), None)
    if index is None:
        return None
    park_body = str(comments[index].get("body") or "")
    if "merge" not in park_body.lower():
        return None
    result = next((c for c in comments[index + 1:] if _is_merge_result(c)), None)
    if result is None and index >= 1 and _is_merge_result(comments[index - 1]) \
            and _waits_on_same_merge(comments[index - 1], comments[index]):
        result = comments[index - 1]
    named = _PARK_FILE_RE.search(park_body)
    rc: str | None = None
    if named is not None:
        try:
            path = _result_file_path(named.group(1))
            if path.is_file():
                value = json.loads(path.read_text(encoding="utf-8")).get("rc")
                rc = str(value) if value is not None else "?"
        except (OSError, ValueError, AttributeError):
            pass
    if rc is None and result is None:
        return None
    if rc is None:
        rc = "0" if "landed on main" in str(result.get("body") or "") else "1"
    return rc


_MERGE_RESULT_NAME_RE = re.compile(r"^\d{8}T\d{6}-merge-(.+)\.json$")


def merge_branch_of_result(path_text: str) -> str | None:
    """#179: the branch a merge result file belongs to, read from its name `<stamp>-merge-<branch>.json`
    (`detach.result_path_for`); `None` for any other name."""

    found = _MERGE_RESULT_NAME_RE.match(_result_basename(path_text.strip("`'\".,;:()<>")))
    return found.group(1) if found else None


@dataclass(frozen=True, slots=True)
class MergeNeverStarted:
    """#179: the newest park of a ticket names a merge result file, no result is in, and the grace is over."""

    park_at: datetime
    result_file: str
    branch: str


def stalled_merge_park(comments: list[dict], now: datetime, grace_s: float) -> MergeNeverStarted | None:
    """#179: the ticket's newest park (`park_reason_index`) names a merge result file `<stamp>-merge-<branch>.json`
    that has no result (`merge_landing` is `None`: no file, no `<!-- merge result -->` comment), no comment of
    the dispatcher's own (`MERGE_NEVER_STARTED_MARKER`) follows it, and `grace_s` seconds have passed since the
    park comment. `None` otherwise. The trace of the branch's merge is the caller's to look up."""

    index = park_reason_index(comments)
    if index is None:
        return None
    park_at_text = str(comments[index].get("createdAt") or "")
    park_at = _parse_iso(park_at_text)
    named = _PARK_FILE_RE.search(str(comments[index].get("body") or ""))
    if park_at is None or named is None or "merge" not in str(comments[index].get("body") or "").lower():
        return None
    branch = merge_branch_of_result(named.group(1))
    if branch is None or (now - park_at).total_seconds() < grace_s:
        return None
    if any(MERGE_NEVER_STARTED_MARKER in str(c.get("body") or "") for c in comments[index + 1:]):
        return None
    if merge_landing(comments, park_at_text) is not None:
        return None
    return MergeNeverStarted(park_at=park_at, result_file=named.group(1).strip("`'\".,;:()<>"), branch=branch)


def _result_label(branch: str) -> str:
    """#179: the branch as `detach.result_path_for` writes it into a result file's name (`claude/x` -> `claude_x`)."""

    name = result_path_for("merge", branch, Path(".")).name
    return name[name.index("-merge-") + len("-merge-"):-len(".json")]


def _live_merge_entry(info: object, pid: object, label: str) -> bool:
    """A live merge entry (queue ticket, holding sidecar, lock) whose raw branch reads as `label` once sanitized
    the way the result file's name is: the tickets keep the raw branch, the park only knows the label."""

    return (isinstance(info, dict) and info.get("kind") == "merge" and isinstance(info.get("branch"), str)
            and _result_label(info["branch"]) == label and isinstance(pid, int) and pid_alive(pid))


def merge_trace_of(branch: str, *, since: float, lock_path: Path, suite_dir: Path,
                   merge_log: Path | None = None) -> str | None:
    """#179: a trace that the merge of `branch` (as the result file's name spells it, `merge_branch_of_result`) runs or waits, as a short phrase; `None` when there is none.
    A merge leaves, in the order it goes: a ticket `<lock>.queue/<time_ns>-<pid>-<n>` (`merge.MergeLock._take`)
    while it waits for the line, the `<lock>.holding` sidecar and the lock file itself while it holds the line,
    and a suite output `<suite_dir>/<stamp>-<n>-[check-]<branch>.out` from the suite's start (`since` = the
    park's epoch seconds: an older output is an earlier attempt). A ticket, sidecar or lock of a dead pid is no
    trace. The detached run's result file comes at the end, so it is no trace.
    #202: two more traces cover the gaps in that list. A batch member has no suite output of its own, the suite
    output is named after the batch head: a queue ticket of the branch whose claim (`<lock>.batch/<ticket>.claim`)
    names the pid that holds the lock counts as having suite output while the head's suite output exists
    (`merge.MergeLock.claim` writes the claim; the lock file names the head), though the member's own waiter is gone.
    And between the push to main and the result file the merge has left the line and the lock: a final `merge.log`
    line (`merge_log`, this installation's by default) that names the branch, alone or in a `batch of N:` line,
    written since the park, is a trace (`_final_line_of`), until the result file or the `<!-- merge result -->`
    comment ends the check."""

    def read(path: Path) -> object:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    queue = lock_path.with_name(lock_path.name + ".queue")
    try:
        tickets = sorted(queue.iterdir())
    except OSError:
        tickets = []
    for ticket in tickets:
        parts = ticket.name.split("-")
        pid = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None
        if _live_merge_entry(read(ticket), pid, branch):
            return f"queue entry {ticket.name}"
    held = read(lock_path.with_name(lock_path.name + ".holding"))
    if isinstance(held, dict) and _live_merge_entry(held.get("info"), held.get("pid"), branch):
        return f"holding sidecar of pid {held['pid']}"
    holder = read(lock_path)
    if isinstance(holder, dict) and _live_merge_entry(holder, holder.get("pid"), branch):
        return f"merge lock held by pid {holder['pid']}"
    from .merge import _safe_branch_name  # the suite output's own sanitizer (`merge._new_suite_output_path`)

    def suite_output_of(name_of: str) -> str | None:
        name = re.compile(rf"^\d{{8}}T\d{{6}}-\d+-(?:check-)?{re.escape(_safe_branch_name(name_of))}\.out$")
        try:
            outputs = list(suite_dir.iterdir())
        except OSError:
            outputs = []
        for out in outputs:
            try:
                if name.match(out.name) and out.stat().st_mtime >= since:
                    return out.name
            except OSError:
                continue
        return None

    own = suite_output_of(branch)
    if own is not None:
        return f"suite output {own}"
    batch_dir = lock_path.with_name(lock_path.name + ".batch")
    if isinstance(holder, dict) and isinstance(holder.get("branch"), str) and pid_alive(holder.get("pid")):
        head_out = suite_output_of(_result_label(holder["branch"]))
        for ticket in tickets if head_out is not None else ():  # #202: a member of the head's batch
            info = read(ticket)
            claim = read(batch_dir / f"{ticket.name}.claim")
            if (isinstance(info, dict) and info.get("kind") == "merge" and isinstance(info.get("branch"), str)
                    and _result_label(info["branch"]) == branch and isinstance(claim, dict)
                    and claim.get("pid") == holder.get("pid")):
                return f"suite output {head_out} of the batch head {holder['branch']} (claim {ticket.name})"
    final = _final_line_of(branch, since,
                           merge_log if merge_log is not None else ROOT / "var" / "agentops" / "merge.log")
    return f"merge.log line `{final}`" if final is not None else None


#: #202: the tail of `merge.log` the trace check reads (a park is minutes to hours old, a line is ~300 bytes).
_MERGE_LOG_TAIL_BYTES = 262144
_MERGE_LOG_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
#: #202: a line whose reason (third field) says the branch is still in the line: no final outcome yet, so it
#: hides an earlier final line of the same branch (`merge._append_left_in_line_log`, `_append_member_log`).
_MERGE_LOG_WAITING = ("left in line", "re-queued", "cut for a Priority")
#: #202: a line that records no outcome of any merge (`merge._append_plain_log`, `map_run`, the line poll).
_MERGE_LOG_NOTES = ("priority ", "blockers not read", "blocker cycle", "issue state not read", "line poll",
                    "silent-holder", "map-run", "detached result")


def _final_line_of(branch: str, since: float, log_path: Path) -> str | None:
    """#202: the time of the newest `merge.log` line, written at or after `since` (epoch seconds), that ends the
    merge of `branch`: its branch field (`<branch>` or `batch of N: a, b`, the raw names) reads as `branch` once
    sanitized like a result file's name, and its reason is any outcome (`pushed`, `red-suite`, `ejected: ...`,
    `red-on-branch`, `lock-timeout` ...) except a waiting one (`_MERGE_LOG_WAITING`) or a note
    (`_MERGE_LOG_NOTES`). The newest such line decides: a branch put back in line has no final line. `None` when
    there is none or the log cannot be read."""

    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - _MERGE_LOG_TAIL_BYTES))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        fields = [f.strip() for f in line.split(" | ")]
        if len(fields) < 3:
            continue
        try:
            at = time.mktime(time.strptime(fields[0], _MERGE_LOG_TIME_FORMAT))
        except (ValueError, OverflowError):
            continue
        if at < since - 1:
            break  # older than the park: an earlier attempt, and the lines above it are older still
        names = fields[1].split(": ", 1)[1] if fields[1].startswith("batch of ") and ": " in fields[1] else fields[1]
        if not any(_result_label(n.strip()) == branch for n in names.split(",") if n.strip()):
            continue
        if fields[1] == "map-run" or fields[2].startswith(_MERGE_LOG_NOTES):
            continue
        return None if fields[2].startswith(_MERGE_LOG_WAITING) else fields[0]
    return None


#: #1113: how long a one-point `gh` read (the claims, the cloud lane issue, the autopilot goals) is
#: reused inside the dispatcher. Those change on the human time scale; every write this process makes
#: drops the memo at once (`ReadMemo.drop`), so a dispatcher's own write is never read back stale.
READ_MEMO_SECONDS = 90.0
#: #1113: the `ReadMemo.read` ttl of a read that lasts one tick (the ready-for-agent list).
TICK_SCOPED = None
#: #1113: the `issue` verbs that change an issue; any of them drops every memoized read.
ISSUE_WRITE_VERBS = ("edit", "comment", "close", "reopen", "create")


class ReadMemo:
    """#1113: memo of `gh` reads for one dispatcher. A read is kept under its exact argument list: a
    tick-scoped read (`ttl` `TICK_SCOPED`) lasts until the next `start_tick`, a timed read for `ttl`
    seconds of `clock`. Only a successful read (rc 0) is kept. Inactive until the first `start_tick`, so
    a one-shot CLI call is never served from a memo."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: dict[tuple[str, ...], tuple[float, float | None, Any]] = {}
        self._active = False

    def start_tick(self) -> None:
        """A dispatcher tick begins: the tick-scoped reads are dropped, the timed ones kept."""
        self._active = True
        self._entries = {key: entry for key, entry in self._entries.items() if entry[1] is not None}

    def drop(self) -> None:
        self._entries.clear()

    def read(self, args: list[str], ttl: float | None, run: Callable[[], Any]) -> Any:
        if not self._active:
            return run()
        key = tuple(args)
        now = self._clock()
        hit = self._entries.get(key)
        if hit is not None and (hit[1] is None or now - hit[0] < hit[1]):
            return hit[2]
        result = run()
        if result.returncode == 0:
            self._entries[key] = (now, ttl, result)
        return result


class GhCli:
    """The only `gh` calls the dispatcher makes. Reads are not cached by default; the ones that come
    up on every tick go through `self.memo` (#1113), and a write of an issue drops it."""

    def __init__(self, repo: str = REPO, cwd: Path = ROOT, timeout: float = 60.0,
                 runner: Callable[[list[str], str | None], subprocess.CompletedProcess] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.repo = repo
        self.cwd = Path(cwd)
        self.timeout = timeout
        #: `(args, stdin) -> CompletedProcess`; injected so a test drives the real calls with a fake
        #: `gh` (#121). `None` is the real process.
        self._runner = runner
        self.memo = ReadMemo(clock)

    def _run(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        """One `gh` process. The seam a local module's own `gh` read goes through too, when it
        contributes a method here (`extensions.py`, the `method` point). Every write of an issue
        (`comment`, `remove_assignee`, `add_label`, `remove_label` and the local modules' ones) drops
        the read memo (#1113)."""

        try:
            if self._runner is not None:
                return self._runner(args, stdin)
            return run_gh(args, cwd=self.cwd, input=stdin, timeout=self.timeout)
        finally:
            if len(args) > 1 and args[0] == "issue" and args[1] in ISSUE_WRITE_VERBS:
                self.memo.drop()

    def _read(self, args: list[str], ttl: float | None) -> subprocess.CompletedProcess:
        """#1113: a read through the memo (`ReadMemo.read`), `ttl` as there."""

        return self.memo.read(args, ttl, lambda: self._run(args))

    def ready_for_agent_issues(self) -> list[IssueSummary]:
        """Open, labelled `ready-for-agent`, unassigned -- requirement 2's candidate pool. Read at most
        once per tick (#1113): every caller of one tick shares the read."""

        result = self._read([
            "issue", "list", "-R", self.repo, "--state", "open", "--label", READY_LABEL,
            "--search", "no:assignee", "--json", CANDIDATE_FIELDS, "--limit", "200",
        ], TICK_SCOPED)
        if result.returncode != 0:
            _log(f"gh issue list failed: {(result.stderr or '').strip()[:200]}")
            return []
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            _log(f"gh issue list: invalid JSON ({exc})")
            return []
        return parse_candidates(data)

    def unlabelled_open_issues(self) -> list[dict]:
        """#874: open, unassigned issues WITHOUT `ready-for-agent`, raw (`number`, `labels`,
        `comments`); `[]` when `gh` fails. The pool `_relabel_merge_results` reads."""

        result = self._run([
            "issue", "list", "-R", self.repo, "--state", "open",
            "--search", f"no:assignee -label:{READY_LABEL}",
            "--json", "number,labels,comments", "--limit", "100",
        ])
        if result.returncode != 0:
            _log(f"gh issue list (unlabelled) failed: {(result.stderr or '').strip()[:200]}")
            return []
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            _log(f"gh issue list (unlabelled): invalid JSON ({exc})")
            return []
        return [i for i in data if isinstance(i, dict)] if isinstance(data, list) else []

    def claimed_issues(self) -> list[int] | None:
        """Open, `ready-for-agent` issues assigned to this account -- the ones some ORCH holds
        right now (one account for all agents). `None` when `gh` fails, never a false empty."""

        result = self._read([
            "issue", "list", "-R", self.repo, "--state", "open", "--label", READY_LABEL,
            "--assignee", "@me", "--json", "number", "--limit", "200",
        ], READ_MEMO_SECONDS)
        if result.returncode != 0:
            _log(f"gh issue list --assignee failed: {(result.stderr or '').strip()[:200]}")
            return None
        try:
            return [int(item["number"]) for item in json.loads(result.stdout)]
        except (ValueError, KeyError, TypeError) as exc:
            _log(f"gh issue list --assignee: invalid JSON ({exc})")
            return None

    def issue_status(self, number: int) -> IssueStatus:
        result = self._run([
            "issue", "view", str(number), "-R", self.repo, "--json", "state,labels,assignees",
        ])
        if result.returncode != 0:
            raise RuntimeError(f"gh issue view {number} failed: {(result.stderr or '').strip()[:200]}")
        data = json.loads(result.stdout)
        labels = tuple(label.get("name", "") for label in data.get("labels") or [])
        assignees = tuple(a.get("login", "") for a in data.get("assignees") or [])
        state = str(data.get("state") or "").upper()
        return IssueStatus(number=number, open=(state != "CLOSED"), labels=labels, assignees=assignees)

    def issue_title_and_last_comment(self, number: int) -> IssueRead | None:
        """The issue's title, its newest comment's body (`""` with none), the newest comment's id (the parking
        checkpoint's, `None` with no comment) and the time it was closed (`None` while open); `None` when `gh`
        fails. The brain notice's read (#951: the two ids tell one park, one close, from another). The read
        also carries the label names as `.labels` (#1109: the notice's chat is the issue's area's), one call."""

        result = self._run(["issue", "view", str(number), "-R", self.repo,
                            "--json", "title,comments,closedAt,labels"])
        if result.returncode != 0:
            _log(f"gh issue view {number} (title, comments) failed: {(result.stderr or '').strip()[:200]}{_rate_limit_tag(result, (number,))}")
            return None
        try:
            data = json.loads(result.stdout)
            comments = data.get("comments") or []
            last = comments[-1] if comments else {}
            park_id = str(last.get("id") or last.get("createdAt") or "") or None
            labels = [str(label.get("name") or "") for label in data.get("labels") or []]
            return IssueRead(str(data.get("title") or ""), str(last.get("body") or ""), park_id,
                             str(data.get("closedAt") or "") or None, labels, str(last.get("url") or ""))
        except (ValueError, AttributeError, TypeError) as exc:
            _log(f"gh issue view {number} (title, comments): invalid JSON ({exc})")
            return None

    def issue_comments(self, number: int) -> list[dict] | None:
        """#209: every comment of the issue as `{"id", "createdAt", "body"}`, in the order GitHub lists them; `None`
        when `gh` fails. The unblocker line's read: one call, only for an issue with an unblocker start record."""

        result = self._run(["issue", "view", str(number), "-R", self.repo, "--json", "comments"])
        if result.returncode != 0:
            _log(f"gh issue view {number} (comments) failed: {(result.stderr or '').strip()[:200]}{_rate_limit_tag(result, (number,))}")
            return None
        try:
            return [{"id": str(c.get("id") or ""), "createdAt": str(c.get("createdAt") or ""),
                     "body": str(c.get("body") or "")} for c in json.loads(result.stdout).get("comments") or []]
        except (ValueError, AttributeError, TypeError) as exc:
            _log(f"gh issue view {number} (comments): invalid JSON ({exc})")
            return None

    def parent_issue(self, number: int) -> int | None:
        """The native parent of the issue (`gh api .../issues/<n>/parent`); `None` when it has none.
        Raises `RuntimeError` when `gh` fails for another reason -- never read as "no parent"."""

        result = self._run(["api", f"repos/{self.repo}/issues/{number}/parent"])
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            if "404" in detail or "not found" in detail.lower():
                return None
            raise RuntimeError(f"gh api issues/{number}/parent failed: {detail[:200]}")
        try:
            value = json.loads(result.stdout).get("number")
        except (ValueError, AttributeError) as exc:
            raise RuntimeError(f"gh api issues/{number}/parent: invalid JSON ({exc})") from None
        return int(value) if value else None

    def last_assigned_at(self, number: int) -> datetime | None:
        """#378: when the issue was last assigned (its latest `assigned` event); `None` when it has
        none. Raises `RuntimeError` when `gh` fails -- the caller never reads that as "never"."""

        result = self._run([
            "api", f"repos/{self.repo}/issues/{number}/events", "--paginate",
            "--jq", '.[] | select(.event == "assigned") | .created_at',
        ])
        if result.returncode != 0:
            raise RuntimeError(f"gh api issues/{number}/events failed: {(result.stderr or '').strip()[:200]}")
        times = [t for line in (result.stdout or "").splitlines() if (t := _parse_iso(line.strip())) is not None]
        return max(times) if times else None

    def last_comment_at(self, number: int) -> datetime | None:
        """#650: when the issue's latest comment was created; `None` when it has none. Raises
        `RuntimeError` when `gh` fails -- the caller never reads that as "never"."""

        result = self._run([
            "api", f"repos/{self.repo}/issues/{number}/comments", "--paginate",
            "--jq", ".[] | .created_at",
        ])
        if result.returncode != 0:
            raise RuntimeError(f"gh api issues/{number}/comments failed: {(result.stderr or '').strip()[:200]}")
        times = [t for line in (result.stdout or "").splitlines() if (t := _parse_iso(line.strip())) is not None]
        return max(times) if times else None

    def parking_mark(self, number: int, end_at: str = "", session_ids: tuple[str, ...] = ()) -> ParkMark | None:
        """The issue's parking checkpoint and `updatedAt` now (#359); `None` when `gh` fails.
        #439: every comment is by the same gh account, so only the text tells the session's own
        checkpoint apart: when `session_ids` is given, `comment_at` is the `createdAt` of the newest
        comment whose body names one of them (a checkpoint's first line is `ORCH <session id>`),
        never a later answer by someone else, however late the session ended. With no such comment
        (or no ids) the rule below holds. Otherwise `comment_at` is the `createdAt` of the last comment created at or before `end_at` (the
        session's last transcript record, `last_timed_record_at`), so an answer written after the
        session's last action but before the dispatcher read its end is NOT absorbed into the
        checkpoint. `end_at == ""` (no transcript): the last comment, whatever its time."""

        result = self._run(["issue", "view", str(number), "-R", self.repo, "--json", "comments,updatedAt"])
        if result.returncode != 0:
            _log(f"gh issue view {number} (parking mark) failed: {(result.stderr or '').strip()[:200]}{_rate_limit_tag(result, (number,))}")
            return None
        try:
            data = json.loads(result.stdout)
            comments = data.get("comments") or []
            end = _parse_iso(end_at) if end_at else None
            if end is not None:
                comments = [c for c in comments
                            if (t := _parse_iso(str(c.get("createdAt") or ""))) is not None and t <= end]
            #: #5: the park reason is the ticket's newest checkpoint that names an event (any session's),
            #: never the finishing note or a result comment; only `comment_at` is the session's own
            reason_index = park_reason_index(comments)
            reason = comments[reason_index] if reason_index is not None else {}
            own = [c for c in comments if any(i and i in str(c.get("body") or "") for i in session_ids)]
            if own:
                comments = own
            last = comments[-1] if comments else {}
            last_body = str(last.get("body") or "")
            return ParkMark(comment_at=str(last.get("createdAt") or ""),
                            updated_at=str(data.get("updatedAt") or ""),
                            on_merge="parked on merge" in last_body.lower()
                            or "parked on merge" in str(reason.get("body") or "").lower(),
                            reason_at=str(reason.get("createdAt") or ""),
                            finishing_only=reason_index is None and is_finishing_note(last_body))
        except (ValueError, AttributeError, TypeError) as exc:
            _log(f"gh issue view {number} (parking mark): invalid JSON ({exc})")
            return None

    def last_comment_names(self, number: int, session_ids: tuple[str, ...]) -> bool | None:
        """#832: whether the issue's newest comment that counts names one of `session_ids` (a session's
        checkpoint first line is `ORCH <session id>`); `None` when `gh` fails or has no usable answer.
        Machine comments (#850), the dispatcher's own alarm and (#977) a comment that names no ORCH
        session and is no other role's verdict (a note from the autopilot, a human) are walked past; a
        comment naming ANOTHER ORCH session, or opening as another role (`_OTHER_ROLE_RE`), stops the walk."""

        result = self._run(["issue", "view", str(number), "-R", self.repo, "--json", "comments"])
        if result.returncode != 0:
            _log(f"gh issue view {number} (last comment) failed: {(result.stderr or '').strip()[:200]}{_rate_limit_tag(result, (number,))}")
            return None
        try:
            comments = json.loads(result.stdout).get("comments") or []
            if not comments:
                return False
            # #850: machine comments (the merge line's, the runs daemon's) after the session's own
            # newest comment do not count: the session parked/closed and the machine answered.
            for c in reversed(comments):
                body = str(c.get("body") or "")
                if any(m in body for m in MACHINE_COMMENT_MARKERS) or ALARM_COMMENT_TEXT in body:
                    continue
                if any(i and i in body for i in session_ids):
                    return True
                if _ORCH_AUTHOR_RE.search(body) or _OTHER_ROLE_RE.search(body):
                    return False
            return False
        except (ValueError, AttributeError, TypeError) as exc:
            _log(f"gh issue view {number} (last comment): invalid JSON ({exc})")
            return None

    def checkpoint_holder(self, number: int, session_ids: tuple[str, ...]) -> tuple[str | None, bool]:
        """#918: `checkpoint_holder_of` over the issue's comments; `(None, False)` when `gh` fails."""

        result = self._run(["issue", "view", str(number), "-R", self.repo, "--json", "comments"])
        if result.returncode != 0:
            _log(f"gh issue view {number} (checkpoint holder) failed: {(result.stderr or '').strip()[:200]}{_rate_limit_tag(result, (number,))}")
            return None, False
        try:
            return checkpoint_holder_of(json.loads(result.stdout).get("comments") or [], session_ids)
        except (ValueError, AttributeError, TypeError) as exc:
            _log(f"gh issue view {number} (checkpoint holder): invalid JSON ({exc})")
            return None, False

    def own_checkpoint_parks(self, number: int, session_ids: tuple[str, ...], held_from: str = "",
                             held_until: str = "", other_ids: tuple[str, ...] | None = None) -> bool | None:
        """#852: whether the session's NEWEST OWN comment on the issue is a parking or closing
        checkpoint (words in `PARKING_WORDS`), whatever others wrote after it. Own = a comment that
        names one of `session_ids` and is not another session's checkpoint; when none does, the
        newest comment created inside the holding window (after `held_from`, up to `held_until`)
        that is no machine or other-role comment and names no other ORCH session. `None`
        when `gh` fails; `False` when there is no own comment.

        #997: a comment signed `ORCH <id>` is another session's only when `<id>` prefix-matches one of
        `other_ids` (the sessions the dispatcher knows besides this one); an id it knows nobody by
        (a session that signed itself wrongly) is read as this session's own when it sits inside the
        holding window. `other_ids=None` keeps the older rule: any id that is not this session's is foreign."""

        result = self._run(["issue", "view", str(number), "-R", self.repo, "--json", "comments"])
        if result.returncode != 0:
            _log(f"gh issue view {number} (own checkpoint) failed: {(result.stderr or '').strip()[:200]}{_rate_limit_tag(result, (number,))}")
            return None
        try:
            comments = json.loads(result.stdout).get("comments") or []
            lo = _parse_iso(held_from) if held_from else None
            hi = _parse_iso(held_until) if held_until else None
            ids = [i for i in session_ids if i]
            named: list[str] = []
            windowed: list[str] = []
            for c in comments:
                body = str(c.get("body") or "")
                if any(m in body for m in MACHINE_COMMENT_MARKERS) or ALARM_COMMENT_TEXT in body:
                    continue
                at = _parse_iso(str(c.get("createdAt") or ""))
                if hi is not None and at is not None and at > hi:
                    continue
                author = _ORCH_AUTHOR_RE.search(body)
                def _same(a: str, i: str) -> bool:
                    a, i = a.lower(), i.lower()
                    return i.startswith(a) or a.startswith(i)

                foreign = False
                if author is not None and not any(_same(author.group(1), i) for i in ids):
                    foreign = other_ids is None or any(_same(author.group(1), o) for o in other_ids if o)
                if any(i in body for i in ids) and not foreign:
                    named.append(body)
                elif (not foreign and lo is not None and at is not None and at > lo
                      and not _OTHER_ROLE_RE.search(body)):
                    windowed.append(body)
            # Named and windowed comments are both the session's own; the newest of them decides.
            own = named[-1] if named else (windowed[-1] if windowed else "")
            return bool(own and _parks_or_closes(own))
        except (ValueError, AttributeError, TypeError) as exc:
            _log(f"gh issue view {number} (own checkpoint): invalid JSON ({exc})")
            return None

    def comment(self, number: int, body: str) -> bool:
        result = self._run(["issue", "comment", str(number), "-R", self.repo, "--body-file", "-"], stdin=body)
        if result.returncode != 0:
            _log(f"gh issue comment {number} failed: {(result.stderr or '').strip()[:200]}{_rate_limit_tag(result, (number,))}")
            return False
        return True

    def remove_assignee(self, number: int, login: str = "@me") -> bool:
        result = self._run(["issue", "edit", str(number), "-R", self.repo, "--remove-assignee", login])
        if result.returncode != 0:
            _log(f"gh issue edit {number} --remove-assignee failed: {(result.stderr or '').strip()[:200]}{_rate_limit_tag(result, (number,))}")
            return False
        return True

    def last_comment_body(self, number: int) -> str | None:
        """#442: the body of the issue's last comment; `None` when it has none or `gh` fails."""

        result = self._run(["issue", "view", str(number), "-R", self.repo, "--json", "comments"])
        if result.returncode != 0:
            _log(f"gh issue view {number} (last comment) failed: {(result.stderr or '').strip()[:200]}{_rate_limit_tag(result, (number,))}")
            return None
        try:
            comments = json.loads(result.stdout).get("comments") or []
            return str((comments[-1] if comments else {}).get("body") or "") or None
        except (ValueError, AttributeError, TypeError) as exc:
            _log(f"gh issue view {number} (last comment): invalid JSON ({exc})")
            return None

    def merge_wait(self, number: int, session_ids: tuple[str, ...]) -> MergeWait | None:
        """#1072: the session's `parked on merge` checkpoint on the issue (`checkpoint_merge_wait`);
        `None` when it has none or `gh` fails."""

        result = self._run(["issue", "view", str(number), "-R", self.repo, "--json", "comments"])
        if result.returncode != 0:
            _log(f"gh issue view {number} (merge wait) failed: {(result.stderr or '').strip()[:200]}{_rate_limit_tag(result, (number,))}")
            return None
        try:
            return checkpoint_merge_wait(json.loads(result.stdout).get("comments") or [], session_ids)
        except (ValueError, AttributeError, TypeError) as exc:
            _log(f"gh issue view {number} (merge wait): invalid JSON ({exc})")
            return None

    def add_label(self, number: int, label: str) -> bool:
        result = self._run(["issue", "edit", str(number), "-R", self.repo, "--add-label", label])
        if result.returncode != 0:
            _log(f"gh issue edit {number} --add-label {label} failed: {(result.stderr or '').strip()[:200]}{_rate_limit_tag(result, (number,))}")
            return False
        return True

    def remove_label(self, number: int, label: str) -> bool:
        result = self._run(["issue", "edit", str(number), "-R", self.repo, "--remove-label", label])
        if result.returncode != 0:
            _log(f"gh issue edit {number} --remove-label {label} failed: {(result.stderr or '').strip()[:200]}{_rate_limit_tag(result, (number,))}")
            return False
        return True

    def closed_ready_issues(self) -> list[int] | None:
        """#154: the closed issues that still carry `ready-for-agent` (one read, `--limit 1000`); `None`
        when `gh` fails or answers garbage, never a false empty. Read by the once-per-process sweep."""

        result = self._run([
            "issue", "list", "-R", self.repo, "--state", "closed", "--label", READY_LABEL,
            "--json", "number", "--limit", "1000",
        ])
        if result.returncode != 0:
            _log(f"gh issue list (closed, {READY_LABEL}) failed: {(result.stderr or '').strip()[:200]}{_rate_limit_tag(result, ())}")
            return None
        try:
            return [int(item["number"]) for item in json.loads(result.stdout)]
        except (ValueError, KeyError, TypeError) as exc:
            _log(f"gh issue list (closed, {READY_LABEL}): invalid JSON ({exc})")
            return None


# -- claude.exe -------------------------------------------------------------------------------------

def parked_by_text(issue: int, parked_by: dict[int, dict[str, str]] | None) -> str:
    """#599: `parked by <session> at <checkpoint date>, continue branch orch-<N>` for an issue a
    session parked before; `""` for one nobody parked."""

    info = (parked_by or {}).get(issue)
    if not info:
        return ""
    return f"parked by {info['session']} at {info['at']}, continue branch orch-{issue}"


def _named_issues(issues: list[int] | tuple[int, ...], parked_by: dict[int, dict[str, str]] | None) -> str:
    out = []
    for n in issues:
        note = parked_by_text(n, parked_by)
        out.append(f"#{n} ({note})" if note else f"#{n}")
    return ", ".join(out)


def build_prompt(offered: list[int] | tuple[int, ...],
                 parked_by: dict[int, dict[str, str]] | None = None) -> str:
    """The first turn of every ORCH session (#60, D104): take every ready issue. The list is the
    ready, unblocked queue in dispatch order at start time -- ORCH skips only one whose code area
    overlaps an issue it already holds, or that the machine cannot take yet, and re-reads the queue
    whenever an issue ends."""

    listed = _named_issues(offered, parked_by) or "none"
    return f"Take every ready issue (ready now, in dispatch order: {listed}). Follow {ORCH_DOC}."


def build_wake_message(issues: list[int] | tuple[int, ...], session: str | None = None,
                       parked_by: dict[int, dict[str, str]] | None = None) -> str:
    """#137: what the dispatcher tells a live ORCH session about new ready issues it does not hold
    -- treated like a subagent's report (`orch.md` 57-62): re-read the queue, claim what fits.

    D110 (#191): a woken session that decides to take nothing says so in the same turn, with the
    exact command the wake prints -- `session` is this session's own id (`_Current.session`), since
    a session may not otherwise be able to name itself (`find_own_session_id`)."""

    names = _named_issues(issues, parked_by)
    verb = "is" if len(issues) == 1 else "are"
    finishing = ""
    if session:
        finishing = (f" If you take none of them (over your threshold, at your context line, or "
                     f"what you hold will bring you to it), say so this turn: `python -m "
                     f"tools.agentops finishing --session {session}`.")
    return (f"Dispatcher: {names} {verb} ready-for-agent and unclaimed. Re-read the ready queue "
            f"and claim what you can ({ORCH_DOC}).{finishing}")


def build_command(offered: list[int] | tuple[int, ...], exe: Path | str = "claude.exe",
                  parked_by: dict[int, dict[str, str]] | None = None, tag: str = "") -> list[str]:
    """#812: `tag` (the real start passes a fresh 4-hex one) ends the name as ` ~<tag>`, so two
    sessions started with the same offer never share a name and SendMessage by name is never
    refused as ambiguous; the suffix holds no `#`+digits and follows the issue list, so every
    reader of the name (`orch` prefix, `#N` findall) is unaffected.

    The exact command line, as an argv list (never shell-quoted: the caller passes it straight
    to `subprocess`, no `shell=True`). The name lists the issues offered at start; which ones the
    session actually holds is tracked from GitHub every tick (`Dispatcher._refresh_claims`). #214:
    the name carries `REPO_NAME` (`orch <REPO_NAME> #N #M`) so a wake by name is never ambiguous
    between two repos' dispatchers; every reader that parses this name matches on the leading
    `orch` and on `#`-prefixed digits, so a repo name with digits or dashes never leaks into the
    issue list -- nothing of one repo is written into a core file, `REPO_NAME` comes straight from
    the settings file's `repo`."""

    offered = list(offered)
    shown = " ".join(f"#{n}" for n in offered[:NAME_SHOWS])
    more = f" +{len(offered) - NAME_SHOWS}" if len(offered) > NAME_SHOWS else ""
    name = f"orch {REPO_NAME} {shown}{more}".strip() + (f" ~{tag}" if tag else "")
    return claude_bg.build_argv(exe, name=name, agent="orch", effort="high",
                                prompt=build_prompt(offered, parked_by))


def build_unblocker_command(number: int, exe: Path | str = "claude.exe") -> list[str]:
    """#209: the `claude --bg` command of the unblocker for ticket `number`, built the way an ORCH's is (same
    builder; `ClaudeCli.start_unblocker` spawns it on the same path). The name `unblocker <REPO_NAME> #N` does not
    start with `orch`, so every reader of ORCH names (adoption, wake, finishing) passes it by."""

    prompt = f"Unblock #{number} ({REPO}#{number}). Follow {UNBLOCKER_DOC}."
    return claude_bg.build_argv(exe, name=f"unblocker {REPO_NAME} #{number}", agent="unblocker", effort="high",
                                prompt=prompt)


def build_wake_prompt(chat: str, message: str) -> str:
    """The one-shot wake sender's whole turn (#123): call `SendMessage` once and stop.

    #951: `to` and `message` are each one JSON string on a line of its own (`to=...`, `message=...`),
    so no character of a title or a comment line (a quote, a backtick, a `%`, a newline) can end the
    value; the sender decodes the string and passes the decoded text."""

    return "\n".join([
        "Call the SendMessage tool once. Its two arguments are each one JSON string on its own line below; "
        "decode the string (\\n is a newline, \\\" a quote mark, \\\\ a backslash) and pass the decoded text "
        "exactly, nothing added, nothing removed.",
        f"to={json.dumps(chat, ensure_ascii=False)}",
        f"message={json.dumps(message, ensure_ascii=False)}",
        "Do nothing else: no Bash, no files, no other tools. Then print the tool result verbatim and stop.",
    ])


def build_permission_block_message(session: str, waited: str, name: str = "",
                                   refused: int = 0) -> str:
    """#213 safety net: what the brain is told about a session stuck on a permission prompt. The
    wait is already rendered for a human by the caller (`util.human_duration`). #285: the hook
    sentence is written only when the events log holds no refusal of this session in the wait;
    otherwise the count is given."""

    shown = f"{name} ({session})" if name else session
    head = (f"Dispatcher: session {shown} is stopped on a permission prompt and has waited "
            f"{waited}. Nobody is sitting at it, so it will not move on its own: ")
    if refused > 0:
        hook = f"the refusal hook refused {refused} call(s) of it in that time. "
    else:
        hook = "the refusal hook did not answer this call. "
    return head + hook + "Look at what it asked for and take it from there."


#: #285: how much of a transcript's end is read to learn how the session's last turn ended.
TRANSCRIPT_TAIL_BYTES = 256 * 1024


def _parse_iso(text: str) -> datetime | None:
    """An ISO-8601 time (`...Z` or offset, with or without fractions) as an aware datetime."""

    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def last_timed_record_at(path: Path, tail_bytes: int = TRANSCRIPT_TAIL_BYTES) -> str | None:
    """#359: the `timestamp` of a transcript's last timed record -- the session's last activity,
    the end time the parking checkpoint is cut at. `None` when the file is missing or has none."""

    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            raw = fh.read()
    except OSError:
        return None
    lines = raw.split(b"\n")
    if size > tail_bytes:
        lines = lines[1:]
    last = None
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("timestamp"):
            last = str(rec["timestamp"])
    return last


def last_timed_record_type(path: Path, tail_bytes: int = TRANSCRIPT_TAIL_BYTES) -> str | None:
    """#285: the type of a transcript's last TIMED record (one with a `timestamp`), read from the
    last `tail_bytes` of the file only. `"tool_use_pending"`: the last timed assistant record holds
    a `tool_use` whose id has no `tool_result` after it. `"turn_end"`: the assistant's final text
    (no tool call) or a `turn_duration` system record. Otherwise the record's own `type`.
    `None` when the file is missing or holds no timed record."""

    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            raw = fh.read()
    except OSError:
        return None
    lines = raw.split(b"\n")
    if size > tail_bytes:
        lines = lines[1:]  # the first line is cut mid-record
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("timestamp"):
            records.append(rec)
    if not records:
        return None
    last = records[-1]
    kind = last.get("type")
    if kind == "system":
        return "turn_end" if last.get("subtype") == "turn_duration" else "system"
    if kind != "assistant":
        return str(kind)
    content = (last.get("message") or {}).get("content")
    blocks = content if isinstance(content, list) else []
    has_call = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks)
    # this is the last timed record, so no timed `tool_result` follows its call: it is pending
    return "tool_use_pending" if has_call else "turn_end"


def last_api_error(path: Path, tail_bytes: int = TRANSCRIPT_TAIL_BYTES) -> str | None:
    """#931: the error of a transcript that ends in an API error (an outage, a spent limit, an auth
    error): the `error` of its last `assistant` record when that record has `isApiErrorMessage` true
    (`"api_error"` when it has no `error`), else None. Read from the last `tail_bytes` of the file
    only; None when the file is missing or holds no assistant record."""

    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            raw = fh.read()
    except OSError:
        return None
    lines = raw.split(b"\n")
    if size > tail_bytes:
        lines = lines[1:]  # the first line is cut mid-record
    last: dict[str, Any] | None = None
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("type") == "assistant":
            last = rec
    if last is None or last.get("isApiErrorMessage") is not True:
        return None
    error = last.get("error")
    return str(error) if error else "api_error"


def build_wake_command(chat: str, message: str, exe: Path | str = "claude.exe") -> list[str]:
    """The exact wake command line (#123's working command, measured): a headless one-shot `-p`
    sender, `--allowedTools=SendMessage`, named `dispatcher` so the woken chat sees who sent it. The
    caller runs this with the full `exe` path and a `cwd` outside the repo (#123 traps 1 and 2)."""

    return [str(exe), "-p", "--model", WAKE_MODEL, "--output-format", "json",
            "--allowedTools=SendMessage", "--name", WAKE_SENDER_NAME, build_wake_prompt(chat, message)]


class ClaudeCli:
    """The only `claude.exe` calls the dispatcher makes."""

    def __init__(self, exe: Path | str | None = None, cwd: Path = ROOT, timeout: float = 60.0,
                 resolver: Callable[[], Path] | None = None) -> None:
        #: #611: a fixed `exe` (tests, an explicit choice) is used as is; with none, every call
        #: looks claude.exe up again, so a desktop-app update that removes the old version folder
        #: costs nothing. A lookup that finds nothing raises `ClaudeExeNotFound`.
        self._fixed_exe = exe
        self._resolver = resolver if resolver is not None else find_claude_exe
        self.cwd = Path(cwd)
        self.timeout = timeout

    @property
    def exe(self) -> Path | str:
        if self._fixed_exe is not None:
            return self._fixed_exe
        try:
            return self._resolver()
        except SystemExit as exc:
            raise ClaudeExeNotFound(str(exc)) from None

    @exe.setter
    def exe(self, value: Path | str | None) -> None:
        self._fixed_exe = value

    def resolve_exe(self) -> Path | str:
        """The callable form of `exe`, for a holder that must not keep the path (#611)."""

        return self.exe

    def start(self, offered: list[int], parked_by: dict[int, dict[str, str]] | None = None) -> tuple[str, list[str]]:
        """Launch one ORCH session offered `offered`; returns `(session_id, command)`. Nothing marks
        it as unattended: `--bg` is the mark, and the `PermissionRequest` hook reads it out of the
        session's own transcript (#213), so the child's environment is the dispatcher's own."""

        cmd = build_command(offered, self.exe, parked_by, tag=uuid.uuid4().hex[:4])
        return claude_bg.start_session(cmd, cwd=self.cwd, timeout=self.timeout), cmd

    def start_unblocker(self, number: int) -> tuple[str, list[str]]:
        """#209: launch one unblocker session on ticket `number`; returns `(session_id, command)`. The same spawn
        path as `start`: `--bg`, this repo's folder, no console window of its own."""

        cmd = build_unblocker_command(number, self.exe)
        return claude_bg.start_session(cmd, cwd=self.cwd, timeout=self.timeout, who=f" for unblocker #{number}"), cmd

    def wake(self, chat: str, message: str) -> tuple[bool, str]:
        """One-shot wake of a named chat session (#123, #127 numbers item 4/5; #137: the dispatcher's
        wake of a live ORCH session about a new ready issue uses this same method, `target` in that
        caller's own words is `chat` here). `cwd` is outside the repo (trap 2); `self.exe` is already
        the full resolved path (trap 1). Delivery is judged by `tools.agentops.wake.judge_reply`
        (#302): rc 0, no `is_error`/`permission_denials`, and `"success":true` or a failure-free
        "sent"/"delivered" in `result`."""

        wake_cwd = Path(tempfile.gettempdir())
        try:
            cmd = build_wake_command(chat, message, self.exe)
            result = subprocess.run(cmd, cwd=str(wake_cwd), capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=self.timeout,
                                    creationflags=no_window_flags())
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        return judge_reply(result.returncode, result.stdout, chat, message)

    def list_sessions(self) -> list[dict[str, Any]]:
        return claude_bg.list_sessions(self.exe, cwd=self.cwd, timeout=self.timeout, log=_log)

    def stop(self, session_id: str) -> bool:
        """`claude stop <id>`: marks the conversation stopped (resumable with `claude attach`), but
        -- measured 2026-09-26, Claude Code 2.1.280 -- does NOT end the background process: `claude
        agents --json` keeps listing the same pid, and `tasklist` shows it alive, after this returns
        0 (#86). Callers that need the process gone use `end_session`, not this alone."""

        return claude_bg.stop_session(self.exe, session_id, cwd=self.cwd, timeout=self.timeout, log=_log)

    def end_process(self, pid: int) -> bool:
        """End a session's process by pid (#86: no `claude` subcommand ends a *running* background
        session -- `rm` is documented for already-exited ones only). `taskkill /T` also ends any
        children; a pid already gone (race, or `not found`) counts as success."""

        result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=self.timeout,
                                creationflags=no_window_flags())
        if result.returncode == 0:
            return True
        detail = (result.stderr or result.stdout or "").strip()
        if "not found" in detail.lower():
            return True
        _log(f"taskkill /PID {pid} failed: {detail[:200]}")
        return False


class GuardError(RuntimeError):
    """`end_session` refuses: the freshest listing does not show this id as a `background` session
    (#86 requirement 2) -- never an interactive session (a human's own chat, the brain's)."""


def end_session(claude: "ClaudeCli", session_id: str, *, run_stop: bool = True,
                log: Callable[[str], None] = _log) -> bool:
    """Really end a background session (#86). `claude stop` (kept when `run_stop`, for its own
    effect: the conversation stays resumable) does not end the process, so afterwards the session's
    pid -- read from the freshest `claude agents --json`, right here, immediately before acting, so a
    reused pid is never touched -- is ended directly. Returns `False` (and logs) only when the
    process is confirmably still alive afterwards; a session already gone from the listing, or listed
    with no pid, counts as ended.

    Raises `GuardError` and ends nothing unless the freshest listing shows this id as `kind ==
    "background"`: never an interactive session. This is a second, independent check -- the caller
    (`Dispatcher`) only ever passes `session_id` it tracks as its own `current.session`, which it
    only ever set from `ClaudeCli.start`'s return value or from `_adopt_live`'s own `background` +
    `orch ...`-name filter; this guard never scans the whole listing by name.
    """

    if run_stop and not claude.stop(session_id):
        log(f"claude stop {session_id} failed")

    session = next((s for s in claude.list_sessions() if s.get("id") == session_id), None)
    if session is None:
        return True  # already gone: nothing left to end

    kind = session.get("kind")  # a row without a kind is never taken for background
    if kind != "background":
        raise GuardError(f"refusing to end {session_id}: kind={kind!r} (never interactive)")

    pid = session.get("pid")
    if not pid:
        return True  # no process left to end

    if claude.end_process(pid):
        return True
    log(f"claude end {session_id} failed (pid {pid} still alive)")
    return False


# -- finishing (D110, #191) ------------------------------------------------------------------------
#
# A session says it is finishing with one command, `python -m tools.agentops finishing [--session
# <id>]`. It may not know its own dispatcher-assigned id (`_Current.session`, the short id printed
# by `claude --bg`), so the command finds it: an env var Claude Code might one day set, else the
# one live `orch ...` background session whose `cwd` matches this repo. When neither works it is a
# hard error -- the wake message always prints the exact command with the id filled in
# (`build_wake_message`), so this fallback only matters for a session that decides to finish on its
# own, unprompted by a wake.

def session_matches_root(session: dict[str, Any], root: Path) -> bool:
    """#208: whether a `list_sessions()` (`claude agents --json --all`) entry belongs to the
    dispatcher rooted at `root` -- the one rule `_adopt_live` and `find_own_session_id` both apply
    before picking a session by name, so an `orch ...` session of a different repo on the same
    machine is never adopted or woken. Paths are normalised (case, slash style, a trailing
    separator) before comparing, so two spellings of the same folder are still the same root.

    A listing entry with no `cwd` (older `claude` builds may omit it) is given the verdict
    `find_own_session_id` already gave before this fix: not filtered out -- counted as belonging."""

    cwd = session.get("cwd")
    if cwd is None:
        return True
    return str(Path(cwd).resolve()).casefold() == str(Path(root).resolve()).casefold()


def find_own_session_id(claude: "ClaudeCli", *, env: dict[str, str] | None = None,
                        cwd: Path = ROOT) -> str:
    """This session's own dispatcher id, for `finishing` when `--session` is not given. Raises
    `SystemExit` with one line when it cannot be told apart from another live ORCH session."""

    import os

    env = env if env is not None else os.environ
    for var in SESSION_ID_ENV_VARS:
        value = env.get(var)
        if value:
            return value

    matches: list[str] = []
    for session in claude.list_sessions():
        name = str(session.get("name") or "")
        if not _ORCH_NAME_RE.match(name):
            continue
        if session.get("kind", "background") != "background" or session.get("state") not in LIVE_STATES:
            continue
        if not session_matches_root(session, cwd):
            continue
        if session.get("id"):
            matches.append(str(session["id"]))
    if len(matches) == 1:
        return matches[0]
    raise SystemExit(
        "cannot tell which ORCH session this is (0 or >1 live 'orch ...' sessions match this repo's "
        "cwd): pass --session <id> -- the dispatcher's wake message prints the exact command with it."
    )


def mark_finishing(session: str, *, finishing_dir: Path = FINISHING_DIR,
                   now: Callable[[], str] = _now_str) -> Path:
    """The durable mark `Dispatcher._update_finishing` looks for: one file per session id, never
    removed (finishing is final, D110)."""

    finishing_dir = Path(finishing_dir)
    finishing_dir.mkdir(parents=True, exist_ok=True)
    path = finishing_dir / f"{session}.json"
    path.write_text(json.dumps({"session": session, "at": now()}), encoding="utf-8")
    return path


def holding_window_from_events(events: list[dict[str, Any]], issue: int, session: str,
                               end_at: str = "") -> tuple[str, str]:
    """#852: (from, until) of the time `session` held `issue`: from the `claimed` event of the issue at
    or after the session's `orch_start` (else that start), until `end_at` (`""` when unknown)."""

    start = ""
    claimed = ""
    for event in events:
        if event.get("kind") == "orch_start" and event.get("session") == session:
            start = str(event.get("ts") or "")
        elif (event.get("kind") == "issue_taken" and event.get("issue") == issue
              and event.get("how") == "claimed" and start and str(event.get("ts") or "") >= start):
            claimed = str(event.get("ts") or "")
    return (claimed or start), end_at


def read_held_issues(session: str, state_file: Path = STATE_FILE) -> tuple[list[int], tuple[str, ...], tuple[str, ...]]:
    """#1054: from the dispatcher's state file (`Dispatcher._write_state`): the issues `session` holds
    (`sessions[].held`), the ids this session goes by (its id and its transcript uuid) and the ids of every
    OTHER session in the file. A missing or garbled file, or no such session, is no held issues."""

    try:
        data = json.loads(Path(state_file).read_text(encoding="utf-8"))
        rows = [r for r in (data.get("sessions") or []) if isinstance(r, dict)]
        mine = [r for r in rows if session in (r.get("session"), r.get("session_uuid"))]
        if not mine:
            return [], (session,), ()
        ids = {session} | {str(r[k]) for r in mine for k in ("session", "session_uuid") if r.get(k)}
        others = {str(r[k]) for r in rows if r not in mine for k in ("session", "session_uuid") if r.get(k)}
        held = sorted({int(n) for r in mine for n in (r.get("held") or [])})
        return held, tuple(sorted(ids)), tuple(sorted(others - ids))
    except (OSError, ValueError, TypeError, AttributeError):
        return [], (session,), ()


def park_held_issues(session: str, *, gh: Any, state_file: Path = STATE_FILE,
                     events_file: Path = EVENTS_FILE, log: Callable[[str], None] = _log) -> list[int]:
    """#1054: `finishing` writes ONE still-working note on every OPEN issue the session still holds whose newest own
    comment does not park or close (`GhCli.own_checkpoint_parks`, the same read the end path makes): the
    session's last checkpoint above stays the hand-off, and the dispatcher's end path finds a parking
    checkpoint instead of raising the "ended without" alarm. The note changes nothing else (no unassign, no
    label: a subagent may still run and the session may close the issue later; a later closing comment of
    its own is newer and decides). Returns the issues noted; a failing `gh` is no note and one log line."""

    held, ids, others = read_held_issues(session, state_file)
    if not held:
        return []
    events: list[dict[str, Any]] = []
    try:
        for line in Path(events_file).read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    noted: list[int] = []
    for number in held:
        try:
            if not gh.issue_status(number).open:
                continue
            held_from, held_until = holding_window_from_events(events, number, session)
            parks = gh.own_checkpoint_parks(number, ids, held_from, held_until, others)
            if parks is None:
                log(f"finishing: could not read the comments of #{number}; no still-working note")
                continue
            if parks:
                continue
            if gh.comment(number, finishing_note(session, ids)):
                noted.append(number)
                print(f"still-working note on #{number}")
            else:
                log(f"finishing: could not post the still-working note on #{number}")
        except Exception as exc:
            log(f"finishing: still-working note on #{number} failed ({exc!r})")
    return noted


def finishing_note(session: str, ids: tuple[str, ...] = ()) -> str:
    """#1054: the note's text: headline passes `_parks_or_closes`, the session id makes it the session's own."""

    full = max((i for i in ids if i), key=len, default=session)
    return (f"ORCH {session[:8]}, finishing: still working this issue, takes nothing new; "
            "if the session ends, the next one continues from the last checkpoint above\n"
            "<!-- parked-on-run: this session is finishing; the session that claims the issue continues "
            "from the last checkpoint -->\n"
            f"Session id: {full}")


def run_finishing_command(session: str | None = None, *, finishing_dir: Path = FINISHING_DIR,
                          claude: "ClaudeCli | None" = None, log: Callable[[str], None] = _log,
                          gh: Any = None, state_file: Path = STATE_FILE,
                          events_file: Path = EVENTS_FILE) -> str:
    """`python -m tools.agentops finishing [--session <id>]` (D110): the real CLI's body. #1054: after the mark,
    a still-working note on every open issue the session still holds whose last checkpoint does not park."""

    sid = session
    if not sid:
        claude = claude if claude is not None else ClaudeCli()
        sid = find_own_session_id(claude)
    mark_finishing(sid, finishing_dir=finishing_dir)
    log(f"marked {sid} as finishing")
    print(f"finishing: {sid}")
    try:
        park_held_issues(sid, gh=gh if gh is not None else GhCli(), state_file=state_file,
                         events_file=events_file, log=log)
    except Exception as exc:  # finishing is final: the mark is written, the notes are best effort
        log(f"finishing: still-working notes failed ({exc!r})")
    return sid


def run_autopilot_stretch_command(issue: int, *, claude: "ClaudeCli | None" = None, gh: Any = None,
                                  events_file: Path = EVENTS_FILE) -> tuple[int, str]:
    """`python -m tools.agentops autopilot-stretch --issue N` (#1142): ORCH's one question before a message
    to the brain chat. Returns `(rc, text)`: rc 0 with the name of the autopilot stretch live on the issue or
    one of its ancestors (`Dispatcher._autopilot_live_for`, the walk #1129 gave the notifier), or with `""` when
    none is; rc 1 with the reason when a GitHub read failed, which ORCH reads as "send the message as today".
    No state file and no log: the dispatcher's own files are never touched by a read from an ORCH session.
    #10: without the `.autopilot` module the answer is `(0, "")` and the one line `autopilot module not
    loaded` goes to STDERR, never stdout (ORCH reads a printed name on stdout as "do not report")."""

    try:
        dispatcher = Dispatcher(claude=claude if claude is not None else ClaudeCli(),
                                gh=gh if gh is not None else GhCli(),
                                events_file=events_file, state_file=None, log=lambda line: None)
        name = dispatcher._autopilot_live_for(issue)
        if not dispatcher._autopilot_module_loaded():
            print("autopilot module not loaded", file=sys.stderr)
    except Exception as exc:  # a failed read is the fallback, never a crash of the caller's turn
        return 1, f"autopilot-stretch: #{issue} not read ({type(exc).__name__}: {str(exc)[:200]})"
    return 0, name or ""


# -- running subagents of a session ---------------------------------------------------------------
#
# A background ORCH that hands its issues to background subagents ends its turn while they work
# and is woken by their `<task-notification>`s; `claude agents` then reports it `done` although it
# is not finished. So before ending a `done` (or idle) session the dispatcher counts its running
# subagents from the transcripts: a subagent is `<session>/subagents/agent-<id>.jsonl`, and it is
# over once the parent transcript holds a task notification for `<id>` with any status but
# `running` (the rule of #52, widened here to failed / killed endings). #977: except an INTERIM
# notification (`completed` plus the harness note that the subagent stopped with background work of
# its own still running): the subagent counts as running while that is its LATEST notification, for
# at most `INTERIM_SUBAGENT_CAP_SECONDS` from its timestamp; a later notification without the note
# ends it.

#: #977: the words of the harness note on an interim notification (parent transcript line 851 of the
#: case: "This agent stopped with background work of its own still running. It may resume ...").
INTERIM_NOTE_MARK = "stopped with background work of its own still running"
#: #136: one notification may list SEVERAL ids (the harness's "N background agents didn't finish before
#: the previous session ended") with a single status; the ids sit side by side, split by a newline (a
#: literal `\n` in the raw JSONL line), and the status after them takes every one.
_TASK_GROUP_RE = re.compile(
    r"((?:<task-id>[^<]+</task-id>(?:\\n|\s)*)+).*?<status>([^<]+)</status>", re.S)
_TASK_ID_RE = re.compile(r"<task-id>([^<]+)</task-id>")
#: #150: the ids of one notification come as a group (as `_TASK_GROUP_RE`), so every id of it takes the
#: status and the note that follow the group; the note is what sits after the status, before the next id.
_NOTIFICATION_PART_RE = re.compile(
    r"((?:<task-id>[^<]+</task-id>(?:\\n|\s)*)+).*?<status>([^<]+)</status>(.*?)(?=<task-id>|$)", re.S)
_NOTE_RE = re.compile(r"<note>(.*?)</note>", re.S)


def default_projects_dir(root: Path = ROOT) -> Path:
    """`~/.claude/projects/<root with every non-alphanumeric character as '-'>`, Claude Code's own
    naming (e.g. `C:\\Repo\\demo` -> `C--Repo-demo`)."""

    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(root))


def _is_nested_subagent(sub_dir: Path, agent_id: str) -> bool:
    """#1110: a subagent started by another subagent reports to that parent subagent, never to the
    session. Its `agent-<id>.meta.json` names a `parentAgentId` or a `spawnDepth` >= 2. No meta file,
    an unreadable or bad one, or depth 1: not nested (judged as before)."""

    try:
        meta = json.loads((sub_dir / f"agent-{agent_id}.meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(meta, dict):
        return False
    depth = meta.get("spawnDepth")
    nested_depth = isinstance(depth, int) and not isinstance(depth, bool) and depth >= 2
    return bool(meta.get("parentAgentId")) or nested_depth


def running_subagent_ids(projects_dir: Path, session_uuid: str | None,
                         now: datetime | None = None) -> set[str]:
    """Ids of this session's subagents with no terminal task notification yet (empty when the
    transcripts are not there: an unknown session never blocks its own ending). #977: a subagent
    whose latest notification is interim counts as running for `INTERIM_SUBAGENT_CAP_SECONDS` from
    that notification's timestamp (`now`, an aware datetime, defaults to the real clock). #1110: a
    subagent started by another subagent (`_is_nested_subagent`) never counts: it reports to its
    parent subagent, which has its own notification here."""

    if not session_uuid:
        return set()
    sub_dir = Path(projects_dir) / session_uuid / "subagents"
    if not sub_dir.is_dir():
        return set()
    started = {p.stem.removeprefix("agent-") for p in sub_dir.glob("agent-*.jsonl")}
    if not started:
        return set()
    parent = Path(projects_dir) / f"{session_uuid}.jsonl"
    over = ended_task_ids(parent)
    interim = interim_task_times(parent)
    clock_now = now or datetime.now(timezone.utc)
    for task_id, at in interim.items():
        if at is not None and (clock_now - at).total_seconds() < INTERIM_SUBAGENT_CAP_SECONDS:
            over.discard(task_id)
    return {agent_id for agent_id in started - over if not _is_nested_subagent(sub_dir, agent_id)}


_MONITOR_STARTED_RE = re.compile(r"Monitor started \(task ([A-Za-z0-9_-]+)")
_MONITOR_EXPIRY_RE = re.compile(r"Monitor started \(task ([A-Za-z0-9_-]+), expires in (\d+)m")
_TASK_STOP_RE = re.compile(r'"name"\s*:\s*"TaskStop"\s*,\s*"input"\s*:\s*\{\s*"task_id"\s*:\s*"([^"]+)"')


def running_watch_ids(projects_dir: Path, session_uuid: str | None,
                      now: datetime | None = None) -> set[str]:
    """#906: ids of the Monitors this session armed that have not ended. A start is the tool result
    `Monitor started (task <id>, ...)` in the parent transcript; it ends with a terminal
    `<task-notification>` for the id or a `TaskStop` call naming it. Empty when the transcript is not
    there: an unknown session never blocks its own ending. #919: a start line naming its own expiry
    ("expires in <n>m") stops counting once n+1 minutes have passed since that record's timestamp
    (`now`, an aware datetime, defaults to the real clock)."""

    if not session_uuid:
        return set()
    parent = Path(projects_dir) / f"{session_uuid}.jsonl"
    if not parent.is_file():
        return set()
    started: set[str] = set()
    stopped: set[str] = set()
    expired: set[str] = set()
    with parent.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "Monitor started" in line:
                started.update(_MONITOR_STARTED_RE.findall(line))
                expiries = _MONITOR_EXPIRY_RE.findall(line)
                if expiries:
                    when = _record_time(line)
                    if when is not None:
                        clock_now = now or datetime.now(timezone.utc)
                        for task, minutes in expiries:
                            if (clock_now - when).total_seconds() >= (int(minutes) + 1) * 60:
                                expired.add(task)
            if "TaskStop" in line:
                stopped.update(_TASK_STOP_RE.findall(line))
    if not started:
        return set()
    return started - stopped - expired - ended_task_ids(parent)


_SHELL_STARTED_RE = re.compile(r"Command running in background with ID: ([A-Za-z0-9_-]+)")


def running_shell_ids(projects_dir: Path, session_uuid: str | None) -> set[str]:
    """#196: ids of the background Bash shells (a Bash call with `run_in_background: true`) this session
    started that have not ended. A start is the tool result `Command running in background with ID:
    <id>.` in the parent transcript; it ends like a watch: a terminal `<task-notification>` for the id
    or a `TaskStop` call naming it. A shell with no end record counts as running, however old its
    start. Empty when the transcript is not there."""

    if not session_uuid:
        return set()
    parent = Path(projects_dir) / f"{session_uuid}.jsonl"
    if not parent.is_file():
        return set()
    started: set[str] = set()
    stopped: set[str] = set()
    with parent.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "Command running in background" in line and '"tool_result"' in line:
                started.update(_SHELL_STARTED_RE.findall(line))
            if "TaskStop" in line:
                stopped.update(_TASK_STOP_RE.findall(line))
    if not started:
        return set()
    return started - stopped - ended_task_ids(parent)


def _record_time(line: str) -> datetime | None:
    """The `timestamp` field of one transcript record line, or None."""

    try:
        stamp = json.loads(line).get("timestamp")
    except (ValueError, AttributeError):
        return None
    return _parse_iso(stamp) if isinstance(stamp, str) else None


def ended_task_ids(parent: Path) -> set[str]:
    """Task ids with a terminal `<task-notification>` anywhere in a session transcript. Matched on
    the raw line, not on `user` lines only: Claude Code 2.1.280 stores the notification in
    `queue-operation` (`content`) and `attachment` (`attachment.prompt`) lines (measured on #60's
    own transcript); the tags are never JSON-escaped, so the regex works on the raw text."""

    over: set[str] = set()
    if not parent.is_file():
        return over
    with parent.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "task-notification" not in line:
                continue
            for ids, status in _TASK_GROUP_RE.findall(line):
                if status.strip() != "running":
                    over.update(i.strip() for i in _TASK_ID_RE.findall(ids))
    return over


def interim_task_times(parent: Path) -> dict[str, datetime | None]:
    """#977: task ids whose LATEST notification in a session transcript is interim (the harness note
    that the task stopped with background work of its own still running), with the timestamp of that
    record (None when the record has none). A later notification of the id without the note drops it.
    Same raw-line reading as `ended_task_ids`."""

    latest: dict[str, datetime | None] = {}
    if not parent.is_file():
        return {}
    with parent.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "task-notification" not in line:
                continue
            at = None
            for ids, status, rest in _NOTIFICATION_PART_RE.findall(line):
                note = _NOTE_RE.search(rest)
                interim = (status.strip() != "running" and note is not None
                           and INTERIM_NOTE_MARK in note.group(1))
                for task_id in (i.strip() for i in _TASK_ID_RE.findall(ids)):
                    if not interim:
                        latest.pop(task_id, None)
                        continue
                    if at is None:
                        at = _record_time(line)
                    latest[task_id] = at
    return latest


# -- the dispatcher --------------------------------------------------------------------------------

def _ended_outcome(status: IssueStatus) -> str | None:
    """What a session left behind when GitHub shows it ended an issue: closed, `needs-info`, or
    parked; `None` while the issue is still claimed. This reads who holds the issue, not its label:
    ORCH unassigns only when it parks (a session that fell stays assigned), so an open issue with no
    assignee was let go by the session itself and is `parked` whatever labels it carries by then --
    even `ready-for-agent`, put back by an answer that landed before this end was read (#226). A
    `no_checkpoint` ending is left for an open issue still assigned at the end."""

    if not status.open:
        return "closed"
    if NEEDS_INFO_LABEL in status.labels:
        return "needs_info"
    if not status.assignees:
        return "parked"
    return None


def settings_pending_job(number: int, *, settings: Mapping[str, Any] | None = None, timeout: float = 20.0) -> bool:
    """#818: whether a job of the issue is pending, by the settings key `pending_job_command`: a shell
    command template with `{issue}` that exits 0 when one is pending. Pending (True) when the key is
    absent or empty, the command exits other than 1, fails, or times out; only exit code 1 says none."""

    try:
        if settings is None:
            settings = _load_settings()
        template = settings.get("pending_job_command")
        if not isinstance(template, str) or not template.strip():
            return True
        result = subprocess.run(template.replace("{issue}", str(int(number))), shell=True, cwd=str(ROOT),
                                capture_output=True, timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.returncode != 1
    except Exception:
        return True


def machine_boot_time() -> float | None:
    """#782: the machine's boot time (epoch seconds), or None when it cannot be read; never raises."""

    try:
        import psutil
        return float(psutil.boot_time())
    except Exception:
        return None


@dataclass(slots=True)
class _Current:
    """One live ORCH session and the issues it holds (#60; D110, #191: more than one may live)."""

    session: str
    started_at: float
    offered: tuple[int, ...] = ()
    #: Claude Code's `sessionId` (the transcript name), learned from the first listing.
    session_uuid: str | None = None
    #: this session's own `claude.exe` pid (#225), read from `claude agents --json`'s own `pid`
    #: field the same way `end_session` does, refreshed on every poll while it is still live. Kept
    #: after the session ends -- `_sweep_orphans` passes it on as the tie a stopped process must
    #: chain up to (never a missing-parent guess); `None` when a listing never carried one.
    claude_pid: int | None = None
    #: #225's own follow-up (the OPEN RISK): pids seen, on a throttled tick while this session was
    #: still alive, whose chain reached `claude_pid` (`snapshot_owned_pids`, orphans.py) -- folded in
    #: (union, never shrunk) each time. Handed to `sweep_orphans` at the end as `recorded_owned_pids`:
    #: ending the session can kill the very bridge rows a fresh chain walk would need, so this is the
    #: only tie left for such a pid by then.
    owned_pids: frozenset[int] = frozenset()
    #: the session's `--name` (`orch #54 #55 ...`), the #137 wake's `target` -- set at start from
    #: `build_command`'s name, or at adoption from the live listing's own `name` field.
    name: str = ""
    #: every issue this session claimed, as seen by the per-tick read; resolved one by one at the end.
    #: Frozen the tick `finishing` becomes true: a claim seen afterwards is never added here (D110).
    held: set[int] = field(default_factory=set)
    #: #336: issues of the frozen `held` seen NOT claimed on a tick after this session became
    #: finishing (it parked or closed them); one later seen assigned again belongs to whoever
    #: claimed it. Kept in `held` for the end-of-session outcome, but never counted as still held.
    let_go: set[int] = field(default_factory=set)
    #: issues already claimed before this session started (a human's claim): never counted as held.
    preexisting: frozenset[int] = frozenset()
    #: this tick's read of the session's still-claimed issues; `None` when `gh` failed.
    claimed_now: frozenset[int] | None = None
    #: the last "left alone" reason logged, so the same wait is logged once, not every tick.
    wait_logged: str | None = None
    #: #137: wakes sent this session, per issue number, and the clock time of the last one --
    #: caps at `MAX_WAKES_PER_ISSUE` and paces at `WAKE_COOLDOWN_SECONDS`.
    wake_counts: dict[int, int] = field(default_factory=dict)
    wake_last: dict[int, float] = field(default_factory=dict)
    #: #200: the clock time of the last events read `_retaken_claims` made per issue (a negative
    #: result stands for `RETAKEN_READ_SECONDS`).
    retaken_read_at: dict[int, float] = field(default_factory=dict)
    #: #106: the `held` count the last "wake skipped: no room" line was logged for; `None` = none standing.
    wake_full_logged: int | None = None
    #: #213 safety net: the last poll that found this session moving, the clock time the wait on a
    #: permission prompt began (cleared as soon as it moves again), and when the brain was told.
    last_moving_at: float | None = None
    blocked_since: float | None = None
    blocked_reported: float | None = None
    #: D110: true once this session said so (`python -m tools.agentops finishing`) or its context is
    #: at or over `context_line`. Final -- never reset. Never woken, never offered an issue, and a
    #: session waiting on a permission prompt is NOT finishing merely for being idle.
    finishing: bool = False
    #: #766: the idle clock. `idle_since` is the clock time the session's turn ended (read from its
    #: transcript at the first idle poll, else that poll); `idle_woken_at` / `idle_woken_wall` are the
    #: clock and wall time of the one wake sent into the idle. Both reset when it is not idle.
    idle_since: float | None = None
    idle_woken_at: float | None = None
    idle_woken_wall: datetime | None = None
    #: #1072: issues this session was kept idle for (parked on a live merge); `_maybe_wake` wakes it for
    #: one without the #918 "my own newest checkpoint" skip. `merge_result_seen`: the clock time the
    #: merge result (file or comment) was first seen for such an issue.
    merge_waited: set[int] = field(default_factory=set)
    merge_result_seen: dict[int, float] = field(default_factory=dict)
    #: #43: issues this session was woken about and has not claimed yet (the latest offer wins). A claim of
    #: one belongs to this session even once it is finishing: the freeze of `held` (D110) excepts them.
    woken: set[int] = field(default_factory=set)
    #: #265: the wall time of the one wake sent for this session's silence (`None`: none sent yet); read from the
    #: events file once (`silent_loaded`), so a restarted dispatcher does not wake the same silence twice.
    silent_woken: datetime | None = None
    silent_loaded: bool = False

    def claim_pool(self) -> list[int]:
        """#265: the issues this session is told about when it is silent: what it claims now, else what it held
        and did not let go, else what it was offered (an empty set falls through to the next)."""
        return sorted(self.claimed_now or (self.held - self.let_go) or set(self.offered))


@dataclass(slots=True)
class TickReport:
    #: the session started this tick (its id), with the issues it was offered.
    started: str | None = None
    offered: tuple[int, ...] = ()
    #: the session that ended this tick and each held issue's outcome
    #: (`closed` / `parked` / `needs_info` / `no_checkpoint` / `timeout` / `unreadable`).
    ended: str | None = None
    outcomes: dict[int, str] = field(default_factory=dict)
    why: str | None = None
    #: #137: issue numbers named in this tick's wake, if any was sent.
    woke: tuple[int, ...] = ()
    #: what a local module's tick step left behind, under the name that module chose; readable as
    #: `report.<name>` too (`extensions.py`, the `tick` point). The core writes nothing here.
    extras: dict[str, Any] = field(default_factory=dict)

    def __getattr__(self, name: str) -> Any:
        if name != "extras":
            try:
                return self.extras[name]
            except KeyError:
                pass
        raise AttributeError(name)


#: #298: a start `claude --bg` refuses is reminded (one line) at most this often while it repeats.
START_REFUSED_REMIND_SECONDS = 30 * 60
#: #299: at most this many messages to the brain's chat per start in any `START_REFUSED_CAP_WINDOW_SECONDS`.
START_REFUSED_CAP_MESSAGES = 3
START_REFUSED_CAP_WINDOW_SECONDS = 30 * 60
#: #611: failed ticks in a row for this long are reported once (a core issue).
FAILED_TICKS_ALERT_SECONDS = 10 * 60


@dataclass
class _RefusedStart:
    """One start (`"orch"` or `"autopilot #N"`) that `claude --bg` keeps refusing with one text."""

    text: str
    first_at: float
    last_line_at: float
    ticks: int = 1


def _real_admission(previous_wait: bool | None, waited_s: float = 0.0, hold_cap_s: float | None = None, *,
                    settings: Mapping[str, Any] | None = None) -> Any:
    """#898: the load gate of the installation, named by the settings key `start_admission` as
    `module:function`; the function takes `previous_wait=` and returns an object with `.go` and `.line()`.
    No key = no gate: the lookup raises and the caller reads any failure as go."""
    import importlib

    spec = (settings if settings is not None else _load_settings()).get("start_admission")
    if not isinstance(spec, str) or ":" not in spec:
        raise LookupError("no start_admission in the settings")
    module, _, name = spec.partition(":")
    return getattr(importlib.import_module(module), name)(
        previous_wait=previous_wait, waited_s=waited_s, hold_cap_s=hold_cap_s)


class Dispatcher:
    """As few live ORCH sessions as possible, ever (D110 replaces D104's "exactly one" with "a
    further one starts only when every live session is finishing"); each session holds every issue
    it can take, through its own subagents."""

    def __init__(
        self, *, claude: ClaudeCli, gh: GhCli, max_hours: float = DEFAULT_MAX_HOURS,
        events_file: Path = EVENTS_FILE, state_file: Path | None = STATE_FILE,
        projects_dir: Path | None = None, finishing_dir: Path = FINISHING_DIR,
        watch_hold_cap: float = WATCH_HOLD_CAP_SECONDS,
        silent_limit: float = SILENT_ORCH_LIMIT_SECONDS,
        autopilot_waits_file: Path | None = None,
        admission_hold_file: Path | None = None,
        context_line: int = CONTEXT_LINE, context_hard_line: int = CONTEXT_HARD_LINE,
        orch_offer_max: int = ORCH_OFFER_MAX, clock: Callable[[], float] = time.monotonic,
        timestamp: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        log: Callable[[str], None] = _log,
        boot_time: Callable[[], float | None] = machine_boot_time,
        dry_run: bool = False,
        orphan_sweep: Callable[..., list] | None = None,
        whole_disk_sweep: Callable[[], list] | None = None,
        owned_pids_snapshot: Callable[[list[int]], dict[int, frozenset[int]]] | None = None,
        collector_ensure: Callable[[], Any] | None = None,
        core_issue_writer: Callable[..., None] | None = None,
        stream_keeper: object | None = None,
        has_pending_job: Callable[[int], bool] | None = None,
        brain_notice_file: Path | None = None, brain_title: str | None = None,
        unblocker_start_file: Path | None = None,
        losses_daily_file: Path | None = None,
        core_update: Callable[[], str | None] | None = None,
        pending_core_update: Callable[[], str | None] | None = None,
        merge_lock_path: Path | None = None,
        merge_trace: Callable[[str, str, float], str | None] | None = None,
        core_update_wait_s: float = CORE_UPDATE_WAIT_S,
        core_notice_file: Path | None = None,
        home_gh_runner: Callable[[list[str]], Any] | None = None,
        core_root: Path | None = None,
        code_fingerprint: Callable[[], str] | None = None,
        start_fingerprint: str | None = None,
        brain_lines_file: Path | None = None,
    ) -> None:
        #: #141: the fingerprint of the installation's own code (`code_fingerprint` in production)
        #: and the value it had when this process started; `run` exits after a tick once they
        #: differ. `None` (every test's default) keeps the loop as it was.
        self.code_fingerprint = code_fingerprint
        self.start_fingerprint = start_fingerprint
        #: #89: the target folder whose pending core-update mark the tick reads; `None` (every test's
        #: default) takes no pending update at all.
        self.core_root = Path(core_root) if core_root is not None else None
        self._pending_core_job: _CoreUpdateJob | None = None
        #: #188: how the last finished update attempt ended while a mark stands: "waits" (a working ORCH or the
        #: merge line: the hold stays), "released" (any other cause: starts and offers go on), None (taken / none yet)
        self._core_attempt: str | None = None
        self._core_released_sha: str | None = None  # the mark sha a non-waiting refusal released
        self._pending_core_logged: set[str] = set()
        #: #148: the `until` values whose "waits for the rate limit" line was logged (one line per distinct reset)
        self._rate_wait_logged: set[float] = set()
        #: #1115: the auto core update, run before every ORCH start (`apply_auto_core_update` in
        #: production). `None` (every test's default) runs none, so no test reaches a real home.
        self.core_update = core_update
        #: #188 rule 5: the update the pending-mark retry runs, a waiter in the merge line that keeps its place across
        #: ticks (`apply_auto_core_update_in_line` in production); `None` runs `core_update` there too. The
        #: before-start update stays the bounded `core_update`: a start never queues behind the whole line.
        self.pending_core_update = pending_core_update if pending_core_update is not None else core_update
        #: #34: the update runs in a thread (its commit may wait for the merge line); the tick waits
        #: `core_update_wait_s` for it, then defers the start and goes on with its other duties.
        self._core_update_job: _CoreUpdateJob | None = None
        self.core_update_wait_s = core_update_wait_s
        self.merge_lock_path = merge_lock_path if merge_lock_path is not None             else ROOT / "var" / "locks" / "merge.lock"
        #: #179: `(branch, result file, park epoch seconds) -> trace phrase or None`: whether the merge a ticket is
        #: parked on runs or waits (`merge_trace_of` on this installation's lock and suite folders by default).
        self.merge_trace = merge_trace if merge_trace is not None else (
            lambda branch, result_file, since: merge_trace_of(
                branch, since=since, lock_path=self.merge_lock_path,
                suite_dir=ROOT / "var" / "agentops" / "merge_suite",
                merge_log=ROOT / "var" / "agentops" / "merge.log"))
        #: #818: True when the issue has a job of its own queued or running; a parked issue with
        #: none is offered again (nothing newer can ever appear). `None` (every test's default)
        #: keeps the old rule: a parked issue waits for something newer.
        self.has_pending_job = has_pending_job
        #: The installation's own modules (ADR 0109, extension point 4), loaded on the first
        #: dispatcher built rather than when this file is imported: everything they extend then
        #: exists, so a module may import the class it joins, and merely importing the dispatcher
        #: (the `claude.exe` finder is shared) pulls none of them in. Idempotent; a no-op when the
        #: settings file names none -- the loop below is then the whole dispatcher.
        registry.load()
        self.claude = claude
        self.gh = gh
        #: #154: `self.clock()` value from which the next sweep of closed issues that still carry
        #: `ready-for-agent` is due; `None` = the first one is due at once.
        self._closed_label_next_at: float | None = None
        #: #122: the timeline reader a Priority ticket's mark is read through; `None` builds the real one
        #: (`core_table.GhIssueReader`) on first use, a test injects a stub.
        self.timeline_reader: Any | None = None
        self.max_hours = max_hours
        self.events_file = Path(events_file)
        self.state_file = Path(state_file) if state_file is not None else None
        #: #280: `None` (every test's default) keeps the autopilot's waits in memory only.
        self.autopilot_waits_file = Path(autopilot_waits_file) if autopilot_waits_file is not None else None
        self.projects_dir = Path(projects_dir) if projects_dir is not None else default_projects_dir()
        self.finishing_dir = Path(finishing_dir)
        self.context_line = context_line
        self.context_hard_line = context_hard_line
        self.orch_offer_max, offer_max_ok = checked_offer_max(orch_offer_max)
        self.watch_hold_cap = watch_hold_cap
        self.silent_limit = silent_limit
        #: the no-checkpoint path's core issue writer, injectable so tests use a recorded
        #: stand-in; the default looks up a same-defect duplicate first, then calls `write_core_issue`.
        # None (the default) writes no core issue: only the real entry point below wires the
        # tracker-writing writer in, so no Dispatcher built by a test can reach a real tracker.
        self.core_issue_writer = core_issue_writer
        #: closes a map's stream with its last ticket and reopens it with a new one (`streams.py`);
        #: None (every test's default) runs none, so no Dispatcher built by a test writes to a tracker.
        self.stream_keeper = stream_keeper
        self.clock = clock
        self.timestamp = timestamp
        self.log = log
        if not offer_max_ok:  # #106: one line at start, naming the bad value
            self.log(f"warning: orch_offer_max {orch_offer_max!r} is not an int >= 1; using the default "
                     f"{self.orch_offer_max}")
        self.boot_time = boot_time
        #: #69: the brain chat lookup's state. `away`: per title, when its news was first held with no chat
        #: found (one home alert per held batch); `_since_text`: the same moment as a human clock (HH:MM);
        #: `_batch_sources`: the distinct news sources that looked for the chat in the batch.
        self.away = BrainChatAway(self.clock)
        self.fallback = FallbackLogger()
        self._since_text: dict[str, str] = {}
        self._batch_sources: dict[str, set[str]] = {}
        #: #138: the titles whose news is held because the resolved chat awaits its owner after a `/clear`
        #: (one log line per hold start; cleared when the chat is no longer awaiting)
        self._owner_hold: set[str] = set()
        #: the `gh` runner the home's brain chat title is read through (`read_home_brain_title`)
        self.home_gh_runner: Callable[[list[str]], Any] = home_gh_runner if home_gh_runner is not None else run_gh
        #: #782: wall-clock start of each session the dispatcher has ended or found dead.
        self._start_wall: dict[str, float] = {}
        self._restart_logged: set[str] = set()
        self.dry_run = dry_run
        #: #298: start key -> the refusal in progress (see `_note_refused_start`).
        self.refused_starts: dict[str, _RefusedStart] = {}
        #: #299: start key -> when each message about its refusals went to the brain's chat, and
        #: whether the cap line was written since the last of them (kept across refusals).
        self.refused_messages: dict[str, list[float]] = {}
        self.refused_cap_told: set[str] = set()
        #: #611: when the current run of failed ticks began (None after a success), and whether it was reported.
        self._failed_since: float | None = None
        self._failed_alerted = False
        #: #217: called with a session's wall-clock start time right after `end_session`, to stop
        #: shells it left running with no live parent. `None` (every test's default construction,
        #: and any caller that never passes it) means no sweep at all -- never `real_process_table`
        #: / `taskkill_killer` on the real machine; only `run_dispatch`'s real construction site
        #: passes `sweep_orphans` itself.
        self.orphan_sweep = orphan_sweep
        #: #225 acceptance row 5: called with no arguments on a throttled tick (see
        #: `_maybe_sweep_whole_disk_searches`); `None` (every test's default) means no sweep at all.
        self.whole_disk_sweep = whole_disk_sweep
        #: `self.clock()` value of the last whole-disk sweep; 0 so the very first eligible tick sweeps.
        self._last_whole_disk_sweep_at: float = 0.0
        #: #225's own follow-up (the OPEN RISK): called with every live session's `claude_pid` on a
        #: throttled tick, returning `{claude_pid: frozenset[int]}` -- folded into each `_Current`'s
        #: `owned_pids`. `None` (every test's default) means no snapshot at all -- never
        #: `real_process_table`/`real_msys_table` on the real machine; only `run_dispatch`'s real
        #: construction site passes `snapshot_owned_pids` itself.
        self.owned_pids_snapshot = owned_pids_snapshot
        #: issue #186: the monitor collector's keeper (`ensure_collector`, run by `collector_tick` as an `after`
        #: step). `None` (every test's default) runs none, so no Dispatcher built by a test starts or stops a
        #: collector; only `run_dispatch`'s real construction site passes `ensure_collector` itself.
        self.collector_ensure = collector_ensure
        #: `self.clock()` value of the last owned-pids snapshot; `-inf` (never a bare `0.0`, which a
        #: fake clock's own start value could equal) so the first eligible tick always runs it.
        self._last_owned_pids_snapshot_at: float = float("-inf")
        #: D110: every live session this process tracks, oldest first; `current` below is a
        #: back-compat view for callers written against D104's one-session model.
        self.sessions: list[_Current] = []
        self._claims_read: list[int] | None = None
        self._freed_now: set[int] = set()
        self._stale_read_logged: dict[int, float] = {}
        #: #50: the clock time of the last `events not readable, kept` line, per issue (once an hour).
        self._events_unreadable_logged: dict[int, float] = {}
        #: #50: `pid_alive` for a session's recorded `claude.exe` pid; injectable so a test sets it.
        self.pid_alive: Callable[[int], bool] = pid_alive
        #: #50: issue -> {session id: its `claude.exe` pid (or None)} for every session that held or was
        #: offered it, kept in the state file (`holders`) so a restart and a session that left
        #: `self.sessions` still count: a ticket is freed as "its session is gone" only when none is alive.
        self._holders: dict[int, dict[str, int | None]] = {}
        self._holders_loaded = False
        #: #898: the load gate before a NEW ORCH start; `admission(previous_wait)` returns an
        #: `Admission`. Injectable so a test needs no samples file; the default reads the real one.
        self.admission: Callable[[bool | None], Any] = _real_admission
        self._admission_wait: bool | None = None
        self._admission_logged: float | None = None
        self._admission_wait_since: float | None = None  # #907: clock of the first wait of the current unbroken run
        self._admission_last_wait_at: float | None = None  # #1083: clock of the last "wait" answer of that run
        #: #152: a hold-cap go latches until an ORCH actually starts (or no issue is ready): a start a core
        #: update defers is retried without a new PC-busy hold.
        self._admission_released: bool = False
        #: #175: `None` (every test's default) keeps the hold's count in memory only; a path keeps it across a restart.
        self.admission_hold_file = Path(admission_hold_file) if admission_hold_file is not None else None
        self._admission_saved: tuple | None = None
        self._load_admission_hold()
        #: sessions this process already ended: never adopted again, even if `claude stop` failed.
        self.ended_sessions: set[str] = set()
        #: #975: the issues each session in `ended_sessions` held when it was marked ended.
        self._ended_held: dict[str, frozenset[int]] = {}
        #: #975: the sessions the still-listed end step already tried (one attempt each).
        self._end_attempted: set[str] = set()
        #: #442: the state file's dead sessions are resolved once, on the first tick.
        self._dead_sessions_resolved = False
        #: the blocked candidates logged last, so the same skip is logged once, not every tick (#53).
        self._logged_blocked: tuple = ()
        self._logged_loop_stopped: tuple = ()
        #: #209: the stopped tickets (3 failed attempts in a row) of the last candidate read, number -> status:
        #: never offered to ORCH; `_maybe_start_unblockers` starts one unblocker session for each new trigger.
        self._loop_stopped: dict[int, LoopStatus] = {}
        #: #209: the unblocker sessions started (a ticket and its newest failed attempt), kept across a restart;
        #: no file (every test's default) keeps them in memory. They never join `self.sessions` (ORCH's).
        self.unblocker_starts = UnblockerStarts(unblocker_start_file, log=lambda text: self.log(text))
        #: #209: ticket -> `self.clock()` of the last read of its thread for the unblocker's brain-answer.
        self._unblocker_read_at: dict[int, float] = {}
        self._logged_needs_info: tuple = ()
        #: #359: parked issues and their checkpoint, kept out of the pool until something newer shows.
        self._park_marks: dict[int, ParkMark] = {}
        self._logged_parked: set[int] = set()
        #: #874: (issue, createdAt of its merge result) already relabelled; the last read's clock value.
        self._relabelled: set[tuple[int, str]] = set()
        self._last_relabel_at: float = float("-inf")
        #: #359: session name -> its end time (`_remember_end`), the cut of a parking checkpoint.
        self._end_at: dict[str, str] = {}
        #: #931: session name -> the API error its transcript ends with (`_note_api_error`); an issue
        #: it held resolves as `outage` (`_resolve_outcome`).
        self._api_error: dict[str, str] = {}
        #: #931: no new session starts before this `self.clock()` value (`OUTAGE_WAIT_S` after an outage).
        self._outage_until: float = 0.0
        #: #599: issue -> {"session", "at"} of the session that parked it; persisted in the state
        #: file (`_write_state`) so it survives that session's end and a dispatcher restart.
        self._parked_by: dict[int, dict[str, str]] = self._read_parked_by()
        self._logged_area: set = set()  # #319: (number, why) already logged
        self._logged_mark_reads: set = set()  # #122: (number, failure kind) already logged
        #: #192: every ready issue seen (`_eligible_candidates`) and not yet taken (claimed or
        #: offered to a newly started session) -- one `issue_ready_seen` event when it enters this
        #: set, one `issue_taken` when it leaves through being taken, none when it merely drops off
        #: the ready queue (closed, blocked, parked meanwhile).
        self._ready_seen: set[int] = set()
        #: the one message to the brain chat when a session parks or closes a ticket (`brain_notice.py`);
        #: `None` (every test's default) sends nothing. `_notice_class`: issue -> what a live session's
        #: held issue was last read as, so it is not read again every tick.
        self._brain_notifier: BrainNotifier | None = (
            self._build_brain_notifier(Path(brain_notice_file), brain_title) if brain_notice_file is not None
            else None)
        self._notice_class: dict[int, str] = {}
        #: #213: when a live session's `needs-info` issue was last read (`self.clock()`), for its re-read window.
        self._needs_info_read_at: dict[int, float] = {}
        #: the daily losses note to the brain chat (`losses_daily.py`); `None` (every test's default) sends nothing.
        #: `_last_losses_check_at` throttles its checks to one an hour.
        self._losses_daily: LossesDaily | None = (
            self._build_losses_daily(Path(losses_daily_file), brain_title) if losses_daily_file is not None
            else None)
        self._last_losses_check_at: float | None = None
        #: #105: the parked-return pass (`_maybe_return_parked`), built once on first use.
        self._parked_return: ParkedReturn | None = None
        #: #28 unit 3: the failed-target notice (`_notify_core_failures`) of a merge result's `failed:` lines; `None`
        #: file (every test's default) sends nothing. `_core_notice_seen`: the result files already read;
        #: `_core_notice_pending`: lines held for a chat not live or for a retry; `_core_notice_away`: chats logged away.
        self._core_notice_file: Path | None = Path(core_notice_file) if core_notice_file is not None else None
        self._core_results_dir: Path = Path(os.environ.get(ENV_RESULTS_DIR) or RESULTS_DIR)
        self._core_notice_seen: set[str] = set()
        self._core_notice_pending: list[dict[str, Any]] = []
        #: #42: `_core_notice_announced`: one row per chat, repo and reason announced, with its clock time (`at`).
        self._core_notice_announced: list[dict[str, Any]] = []
        self._core_notice_away: set[str] = set()
        #: #74 unit 2: the empty-map notice (`_note_empty_map`): `_empty_map_sent` the map commits (sha8) already told,
        #: `_empty_map_pending` the held ones ({"map", "target", "attempts"}). Both live in the core notice state file.
        self._empty_map_sent: set[str] = set()
        self._empty_map_pending: list[dict[str, Any]] = []
        if self._core_notice_file is not None:
            self._core_notice_load()
        #: #241: the merge's brain lines (`brain_lines.log`, `merge_github.append_brain_line`); `None` (every test's
        #: default) reads none. `_brain_lines`: {"offset": bytes told through the last newline, "attempts": failed sends
        #: of the batch at the offset}. Kept in the state file (`brain_lines` key), read here so a restart tells nothing twice.
        self._brain_lines_file: Path | None = Path(brain_lines_file) if brain_lines_file is not None else None
        self._brain_lines: dict[str, int] = {"offset": 0, "attempts": 0}
        if self._brain_lines_file is not None:
            self._load_brain_lines_state()
        #: whatever the local modules keep per dispatcher (`extensions.py`, the `setup` point).
        registry.run_setup(self)

    @property
    def current(self) -> _Current | None:
        """D104 back-compat: the one live session, when at most one exists. `None` with zero or
        several live -- a caller (or test) written for one session at a time never sees a wrong one."""

        return self.sessions[0] if len(self.sessions) == 1 else None

    # -- one tick ------------------------------------------------------------------------------

    def tick(self) -> TickReport:
        """One tick (D110, #191): the local steps registered `before` first, then adopt any live
        `orch ...` session not yet tracked, one shared `gh` read of the claims (distributed over
        every live session), poll every live session (a finishing one included, to the end -- it is
        just never woken or offered an issue), then start a further session when eligible. The steps
        registered `after` run beside all of this, every tick: they never affect ORCH and ORCH never
        affects them."""

        report = TickReport()
        self._load_holders()
        if isinstance(self.gh, GhCli):
            self.gh.memo.start_tick()  # #1113: the tick-scoped reads (the ready list) start over
        mode = self._machine_mode_and_keep()
        self._machine_mode_now = mode  # #186: the collector step reads it (it starts and stops nothing outside "normal")
        registry.run_ticks(self, report, phase="before")
        if mode != "normal":
            # Machine mode play (D117, #779) or an unreadable mode file: start nothing, wake nothing,
            # hand nothing back; the loop itself stays alive and the keeper pass above did its work.
            registry.run_ticks(self, report, phase="after")
            self._write_state()
            return report
        #: a session adopted THIS tick is polled from the next one (mirrors the pre-#191 rule that a
        #: session just started or just ended is never also polled the same tick).
        self._resolve_dead_sessions()
        self._end_listed_ended_sessions()
        adopted_now = set(self._adopt_live())
        self._refresh_claims()
        self._free_stale_assignees()
        self._relabel_merge_results()
        self._sweep_closed_ready_labels()
        self._notify_core_failures()
        self._tell_brain_lines()  # #241: after the mode check above, so a play mode wakes nothing
        self._notify_live_outcomes()
        self._maybe_return_parked()
        self._maybe_losses_daily()
        any_ended = False
        for cur in list(self.sessions):
            if cur.session in adopted_now:
                continue
            ended = self._poll_session(cur)
            if ended is not None:
                report.ended = cur.session
                report.why, report.outcomes = ended
                self.sessions.remove(cur)
                any_ended = True
            elif not cur.finishing:
                self._maybe_wake(cur, report)
        #: a session that just ended is replaced no earlier than the NEXT tick, so a fresh `gh` read
        #: always separates "it ended" from "start one" (the stale-candidate trap: a fixed ready
        #: queue does not itself drop an issue this tick's ending just closed).
        self._maybe_start_unblockers()  # #209: no ORCH gate, no ORCH list
        self._tell_unblocker_ways()
        if not any_ended:
            self._maybe_start(report)
        self._maybe_update_owned_pids()
        self._maybe_sweep_whole_disk_searches()
        self._track_ready_waits(report)
        if self.stream_keeper is not None:
            self.stream_keeper.tick()
        registry.run_ticks(self, report, phase="after")
        self._write_state()
        return report

    def _machine_mode_and_keep(self) -> str:
        """Read the machine mode once and run one keeper pass for it (never raises). An unreadable mode
        file returns "unreadable" (the caller starts nothing) and runs no pass: there is no mode to keep."""
        from . import hold, machine_mode
        read = machine_mode.read_gate()  # the loop never crashes on the mode file
        if read.mode is None:
            if not getattr(self, "_mode_unreadable_logged", False):
                self._mode_unreadable_logged = True
                self.log(f"machine mode unreadable, starting nothing this tick: {read.error}")
            return "unreadable"
        mode = read.mode
        self._mode_unreadable_logged = False
        keeper = getattr(self, "keeper", None)
        if keeper is not None and keeper.alive():
            return mode  # #779 unit 10: the keeper thread runs the pass on its own cadence
        try:
            hold.keeper_pass(mode, ROOT)
        except Exception as exc:  # noqa: BLE001
            self.log(f"keeper pass failed ({mode}): {exc}")
        return mode

    # -- the live sessions -----------------------------------------------------------------------

    def _refresh_claims(self) -> None:
        """One `gh` read per tick, shared over every live session: the open, `ready-for-agent`,
        assigned issues. A finishing session's `held` is frozen (D110): a claim it does not already
        hold is never added to it, so it belongs instead to the one session still taking new work
        (at most one is ever not finishing, by construction) -- a claim not accounted for by any
        frozen session's `held`. #43: the one exception is an issue the finishing session was woken
        about (`woken`): a claim of it is that session's, said finishing or not. `held` keeps every
        one ever seen, so an issue closed or parked since is still resolved at the end."""

        self._claims_read = None
        self._freed_now = set()
        if not self.sessions:
            return
        claims = self.gh.claimed_issues()
        self._claims_read = claims
        if claims is None:
            for cur in self.sessions:
                cur.claimed_now = None
            return
        claims_set = frozenset(claims)
        frozen_held: set[int] = set()
        for cur in self.sessions:
            if cur.finishing:
                gone = {n for n in cur.held if n not in claims_set} - cur.let_go
                for n in sorted(gone):
                    self._note_parked_by(n, cur.session, self.timestamp().isoformat(timespec="seconds"),
                                         keep_date=True)
                cur.let_go |= {n for n in cur.held if n not in claims_set}
                elsewhere: set[int] = set()
                for other in self.sessions:
                    if other is not cur:
                        elsewhere |= other.held - other.let_go
                woken_claim = (claims_set & cur.woken) - cur.held - elsewhere
                if woken_claim:
                    self.log(f"orch {cur.session} claimed {', '.join(f'#{n}' for n in sorted(woken_claim))}")
                    cur.held |= woken_claim
                    self._hand_off_held(cur, woken_claim)
                cur.woken -= cur.held
                frozen_held |= cur.held - cur.let_go
        for cur in self.sessions:
            if cur.finishing:
                cur.claimed_now = claims_set & (cur.held - cur.let_go)
                continue
            cur.preexisting -= self._retaken_claims(cur, claims_set)
            mine = (claims_set - cur.preexisting) - (frozen_held - cur.held)
            mine -= self._stale_claims(cur, mine - cur.held - set(cur.offered))
            new = mine - cur.held
            if new:
                self.log(f"orch {cur.session} claimed {', '.join(f'#{n}' for n in sorted(new))}")
            cur.held |= mine
            self._hand_off_held(cur, new)
            # #514: a re-claim (claimed again after a tick without it) by the session still taking
            # work makes a let-go issue a held one again
            cur.let_go -= mine - (cur.claimed_now or set())
            cur.claimed_now = mine

    def _retaken_claims(self, cur: "_Current", claims_set: frozenset[int]) -> frozenset[int]:
        """#200: of the issues claimed before `cur` started (its `preexisting`), those it was offered
        or woken about and that were assigned again since it started, while no other live session
        holds them: the holder let them go and `cur` took them over, so they are `cur`'s claim. A
        failed or empty events read leaves the issue where it was this tick."""

        held_elsewhere: set[int] = set()
        for other in self.sessions:
            if other is not cur and other.session not in self.ended_sessions:
                held_elsewhere |= other.held - other.let_go
        now = self.clock()
        asked = {n for n in (cur.preexisting & claims_set & (set(cur.offered) | cur.woken)) - held_elsewhere
                 if now - cur.retaken_read_at.get(n, -RETAKEN_READ_SECONDS) >= RETAKEN_READ_SECONDS}
        if not asked:
            return frozenset()
        started = self.timestamp().timestamp() - (self.clock() - cur.started_at)
        retaken: set[int] = set()
        for n in sorted(asked):
            cur.retaken_read_at[n] = now
            try:
                assigned = self.gh.last_assigned_at(n)
            except (RuntimeError, ValueError):
                continue
            if assigned is not None and assigned.timestamp() >= started:
                retaken.add(n)
        return frozenset(retaken)

    def _held_by_other_live(self, issue: int, session: str, *, wide: bool = False) -> bool:
        """#336, #200: another tracked live session has `issue` in its `held` (let go or not).
        `wide`: also in its `claimed_now`, `offered` or `woken` -- any stake in it."""

        for o in self.sessions:
            if o.session == session or o.session in self.ended_sessions:
                continue
            if issue in o.held or (wide and (issue in (o.claimed_now or ()) or issue in o.offered
                                             or issue in o.woken)):
                return True
        return False

    def _claimed_after_release(self, issue: int, session: str) -> bool:
        """#200: another live session assigned `issue` after `session` let it go (its latest
        `assigned` event is newer than the release `_note_parked_by` recorded): that claim is not
        the ending session's to strip. False when the release time or the events are unknown."""

        note = self._parked_by.get(issue)
        if not note or note.get("session") != session:
            return False
        if not self._held_by_other_live(issue, session, wide=True):
            return False
        released = _parse_iso(note.get("at", ""))
        if released is None:
            return False
        try:
            assigned = self.gh.last_assigned_at(issue)
        except (RuntimeError, ValueError):
            return False
        return assigned is not None and assigned > released

    def _told_once(self, kind: str, session: str, issue: int | None = None) -> bool:
        """#694: true when this (kind, session[, issue]) message was already sent -- the events file
        holds the mark, so a dispatcher restart does not repeat it."""

        for event in self._read_events():
            if (event.get("kind") == kind and event.get("session") == session
                    and event.get("issue") == issue):
                return True
        return False

    def _tell_session(self, cur: "_Current", kind: str, message: str, issue: int | None = None) -> None:
        target = cur.name or cur.session
        try:
            ok, detail = self.claude.wake(target, message)
        except Exception as exc:  # a wake that raises is a wake that failed
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        self._append_event({
            "ts": self.timestamp().isoformat(timespec="seconds"), "kind": kind,
            "session": cur.session, "issue": issue, "target": target, "ok": ok,
        })
        if not ok:
            self.log(f"{kind} message to {target} failed: {detail}")

    def _hand_off_held(self, cur: "_Current", new: set[int]) -> None:
        """#694: an issue `cur` newly claimed leaves every other live session's `held` in the same
        tick (one issue is never held by two live sessions), and that session is told once per
        (session, issue) to stop all work on it."""

        for n in sorted(new):
            for old in self.sessions:
                if old is cur or n not in old.held:
                    continue
                old.held.discard(n)
                old.let_go.discard(n)
                old.woken.discard(n)
                if old.claimed_now:
                    old.claimed_now = frozenset(old.claimed_now - {n})
                if self._told_once("orch_handoff_told", old.session, n):
                    continue
                self._tell_session(old, "orch_handoff_told", (
                    f"#{n} is now held by {cur.session}: post nothing on it, queue no merge for it, "
                    f"start no subagent for it, stop your own monitors and runs on it."), issue=n)

    def _events_unreadable(self, n: int) -> None:
        """#50: an issue whose `assigned` events read empty or failed is kept this tick and never
        unassigned on that read: a claim whose event is not listed yet is not known to be old. Logged
        at most once an hour per issue."""

        last = self._events_unreadable_logged.get(n)
        if last is None or self.clock() - last >= 3600:
            self._events_unreadable_logged[n] = self.clock()
            self.log(f"#{n}: events not readable, kept")

    def _free_stale_assignees(self) -> None:
        """#650: an open `ready-for-agent` issue with an assignee is stale (assigned by a session that
        is gone) when no live session holds, was offered or just claimed it, its latest `assigned`
        event is older than 2 ticks, and its latest comment is older than 2 ticks too (a session may
        be writing its checkpoint before the release). Session start times play no part. It is
        unassigned, so it is offered again. Separate from the claim attribution (`preexisting` keeps
        its meaning). #50: an issue whose events list is empty or whose read failed is kept this tick
        (`_events_unreadable`), and a comment read that fails leaves the issue alone this tick, logged
        at most once an hour."""

        claims = self._claims_read if self.sessions else self.gh.claimed_issues()
        if claims is not None:
            for n in [n for n in self._holders if n not in claims]:
                del self._holders[n]  # no longer assigned: nobody holds it
        if not claims:
            return
        busy: set[int] = set()
        for cur in self.sessions:
            busy |= cur.held | set(cur.offered) | (cur.claimed_now or set())
        now_wall = self.timestamp().timestamp()
        window = 2 * TICK_SECONDS
        throttle = self._stale_read_logged
        for n in sorted(set(claims) - busy - self._freed_now):
            holders = self._holders.get(n, {})
            #: #50: a session that held or was offered the issue and left `self.sessions` while its
            #: process is alive, or whose pid is not recorded (not known gone), still holds it
            if any(pid is None or self.pid_alive(pid) for pid in holders.values()):
                continue
            try:
                assigned = self.gh.last_assigned_at(n)
            except (RuntimeError, ValueError):
                self._events_unreadable(n)
                continue
            if assigned is None:
                self._events_unreadable(n)
                continue
            if now_wall - assigned.timestamp() < window:
                continue
            try:
                commented = self.gh.last_comment_at(n)
            except (RuntimeError, ValueError) as exc:
                last = throttle.get(n)
                if last is None or self.clock() - last >= 3600:
                    throttle[n] = self.clock()
                    self.log(f"orch-dispatch: #{n} comment read failed ({exc}); left alone")
                continue
            if commented is not None and now_wall - commented.timestamp() < window:
                continue
            try:
                body = self.gh.last_comment_body(n) or ""
            except (RuntimeError, ValueError) as exc:
                last = throttle.get(n)
                if last is None or self.clock() - last >= 3600:
                    throttle[n] = self.clock()
                    self.log(f"orch-dispatch: #{n} comment read failed ({exc}); left alone")
                continue
            # a hand-over: the latest comment (a checkpoint, or a takeover note "ORCH <id> takes #N
            # over ...") names a live session, so the issue is its own (#903)
            if any(cur.session in body for cur in self.sessions):
                continue
            # #918: the newest ORCH checkpoint was posted by a live session: later machine or foreign
            # comments do not take the issue from it
            if self.gh.checkpoint_holder(n, tuple(cur.session for cur in self.sessions))[0]:
                continue
            if self.gh.remove_assignee(n):
                who = f"session {', '.join(sorted(holders))} gone" if holders else "its session is gone"
                self.log(f"orch-dispatch: #{n} stale assignee ({who}), unassigned")

    def _stale_claims(self, cur: "_Current", unproven: set[int]) -> set[int]:
        """#378: of the new claims `cur` was NOT offered, those assigned before it started: nobody's
        claim (an issue assigned by a session that has since ended, closed, then reopened with the
        assignee still set). Each is unassigned here, so it goes the ordinary way for a ready
        unassigned issue (wake / offer). An issue whose event read failed is neither held nor
        unassigned this tick. #43: an issue another live session holds, was offered (or woken about) or
        just claimed is that session's, never unassigned and never `cur`'s; an issue with no `assigned`
        event at all is not known yet, left unclaimed and read again next tick. #50: the same for an
        events list that is empty or unreadable (`_events_unreadable`), never unassigned on that read.
        Returns every issue to leave out of `cur`'s claims."""

        if not unproven:
            return set()
        started = self.timestamp().timestamp() - (self.clock() - cur.started_at)
        out: set[int] = set()
        others: set[int] = set()
        for other in self.sessions:
            if other is not cur:
                #: an issue the other session let go (parked) is nobody's again, offered or not
                others |= ((other.held | set(other.offered)) - other.let_go) | (other.claimed_now or set())
        out |= unproven & others
        for n in sorted(unproven - others):
            try:
                assigned = self.gh.last_assigned_at(n)
            except (RuntimeError, ValueError):
                self._events_unreadable(n)  # #50: a failed read keeps the issue this tick, never unassigned
                out.add(n)
                continue
            if assigned is None:
                self._events_unreadable(n)  # #43, #50: no event listed yet is "not known", not "nobody's"
                out.add(n)
                continue
            if assigned.timestamp() >= started:
                continue
            out.add(n)
            if self.gh.remove_assignee(n):
                self._freed_now.add(n)
                self.log(f"orch-dispatch: #{n} stale assignee (no claim since {cur.session} started), unassigned")
        return out

    def _log_wait(self, cur: "_Current", reason: str) -> None:
        if cur.wait_logged != reason:
            self.log(f"orch session {cur.session} {reason}")
            cur.wait_logged = reason

    def _finishing_marked(self, session: str, session_uuid: str | None = None) -> bool:
        """The mark `finishing` writes is named by whatever id the session found (`find_own_session_id`
        returns the env var's full uuid first), so it is read under the short id or the uuid (#726)."""

        names = [n for n in (session, session_uuid) if n]
        return any((self.finishing_dir / f"{n}.json").is_file() for n in names)

    def _token_meter(self):
        """Import `scripts/token_meter.py` read-only (`outcomes.py`'s own pattern: `scripts/` has
        no `__init__.py`, so it goes on `sys.path`, never imported as a package)."""

        scripts_dir = ROOT / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        import token_meter

        return token_meter

    def _session_context(self, session_uuid: str) -> int | None:
        """This session's current context, read from its own transcript (D110: no Claude usage
        involved, `scripts/token_meter.py:116-118`); `None` when the transcript is not there yet or
        holds no turn."""

        try:
            tm = self._token_meter()
            path = Path(self.projects_dir) / f"{session_uuid}.jsonl"
            if not path.is_file():
                return None
            parsed = tm.parse_main_transcript(path)
            if not parsed.turns:
                return None
            return max(parsed.turns, key=lambda t: t.turn_index).context
        except Exception as exc:
            self.log(f"could not read {session_uuid}'s context ({exc!r})")
            return None

    def _update_finishing(self, cur: "_Current") -> None:
        """D110: a session becomes finishing the moment it says so, or its context is at or over
        `context_line`. Final -- called only while `not cur.finishing`."""

        marked = self._finishing_marked(cur.session, cur.session_uuid)
        context = self._session_context(cur.session_uuid) if cur.session_uuid else None
        over_line = context is not None and context >= self.context_line
        if not (marked or over_line):
            return
        cur.finishing = True
        why = "said so" if marked else f"context {context} >= {self.context_line}"
        self.log(f"orch {cur.session} is finishing ({why})")
        if not self._told_once("orch_finishing_told", cur.session):
            self._tell_session(cur, "orch_finishing_told", (
                f"You are finishing (context {context} >= {self.context_line}). Take nothing new; "
                f"finish what you hold. Park a ticket for context only past {self.context_hard_line // 1000}k "
                f"(your context is {context}), or for a real wait (merge, run, blocker). "
                f"Say `python -m tools.agentops finishing --session {cur.session}` now. "
                f"An issue you let go is not yours to touch.") if over_line and not marked else (
                f"You are finishing (you said so). Take nothing new; finish what you hold. Park a ticket "
                f"for context only past {self.context_hard_line // 1000}k, or for a real wait. Say `python -m "
                f"tools.agentops finishing --session {cur.session}` now. An issue you let go is not yours to touch."))
        self._append_event({
            "ts": self.timestamp().isoformat(timespec="seconds"), "kind": "orch_finishing",
            "session": cur.session, "why": why,
        })

    def _live_watches(self, cur: "_Current") -> set[str]:
        """#906: the session's running Monitors, counted only while its last transcript record is
        younger than `watch_hold_cap` (a forgotten Monitor never holds a session for ever)."""

        return self._session_watches(cur.session, cur.session_uuid)

    def _session_watches(self, session: str, session_uuid: str | None) -> set[str]:
        """#906, by session id and transcript uuid (`_live_watches` and #975's end step share it)."""

        watches = running_watch_ids(self.projects_dir, session_uuid)
        if not watches:
            return watches
        last = last_timed_record_at(Path(self.projects_dir) / f"{session_uuid}.jsonl")
        last_at = _parse_iso(last) if last else None
        if last_at is not None and self._wall_age(last_at) > self.watch_hold_cap:
            self.log(f"orch {session} has {len(watches)} watch(es) but no activity for over "
                     f"{self.watch_hold_cap / 60:.0f} min; not holding it for them")
            return set()
        return watches

    def _live_shells(self, session_uuid: str | None) -> set[str]:
        """#196: the session's running background Bash shells (no age cap: the session's own `max_hours`
        timeout bounds a shell that never reports an end)."""

        return running_shell_ids(self.projects_dir, session_uuid)

    def _stood_since(self, since_clock: float) -> float:
        """#779 rule 5: seconds of [`since_clock`, now] (this dispatcher's clock) the machine stood in play."""
        span = self.clock() - since_clock
        if span <= 0:
            return 0.0
        from . import machine_mode
        wall = self.timestamp().timestamp()
        return machine_mode.stood_between(wall - span, wall)

    def _wall_age(self, at: datetime) -> float:
        """Seconds since the wall time `at`, minus the time the machine stood in play (#779 rule 5)."""
        from . import machine_mode
        ts = self.timestamp()
        return (ts - at).total_seconds() - machine_mode.stood_between(at.timestamp(), ts.timestamp())

    def _sweep_closed_ready_labels(self) -> None:
        """#154: takes `ready-for-agent` off every closed issue that still carries it: one `gh` list, then
        one remove per issue listed. At most once per `CLOSED_LABEL_SWEEP_SECONDS` (the first on the first
        normal-mode tick), so an issue closed unheld later is swept at the next interval. The interval is
        set before the list is read, so a failed list (logged by `GhCli`, one line) is retried at the next
        interval and never breaks the tick. A `gh` seam without `GhCli`'s list is not swept."""

        if not isinstance(self.gh, GhCli):
            return
        now = self.clock()
        if self._closed_label_next_at is not None and now < self._closed_label_next_at:
            return
        self._closed_label_next_at = now + CLOSED_LABEL_SWEEP_SECONDS
        try:
            numbers = self.gh.closed_ready_issues() or []
        except Exception as exc:  # noqa: BLE001 -- the sweep never breaks the tick
            self.log(f"closed-label sweep: gh list failed ({exc!r})")
            return
        for number in numbers:
            self.gh.remove_label(number, READY_LABEL)

    def _poll_session(self, cur: "_Current") -> tuple[str, dict[int, str]] | None:
        elapsed_hours = (self.clock() - cur.started_at - self._stood_since(cur.started_at)) / 3600.0
        if elapsed_hours > self.max_hours:
            return "timeout", self._timeout(cur)

        sessions = self.claude.list_sessions()
        session = next((s for s in sessions if s.get("id") == cur.session), None)
        if session is None:
            self.log(f"orch session {cur.session} is gone; treating as done")
            return "gone", self._finish(cur, "gone")
        if session.get("sessionId"):
            cur.session_uuid = str(session["sessionId"])
        if session.get("pid"):
            cur.claude_pid = int(session["pid"])

        last_type = self._check_permission_block(cur, session)
        if not cur.finishing:
            self._update_finishing(cur)

        state = session.get("state")
        status = session.get("status")
        if state == "stopped":
            return "stopped", self._finish(cur, "stopped", stop=False)
        silent_end = self._silence_step(cur, state)
        if silent_end is not None:
            return silent_end
        if state not in LIVE_STATES or status == "idle":
            # `done` / idle: either finished, or it ended its turn while its subagents work (it is
            # woken by their notifications), or it waits on the owner (a permission prompt) -- never
            # finishing merely for being idle.
            # #1021: a `failed` row's subagents never keep it live (ORCH e3042291, 14.10: "failed with
            # 1 subagent(s) running" until the dispatcher was restarted); watches still do.
            running = (set() if state == "failed"
                       else running_subagent_ids(self.projects_dir, cur.session_uuid, now=self.timestamp()))
            if running:
                self._idle_reset(cur)
                self._log_wait(cur, f"is {state or status} with {len(running)} subagent(s) running")
                return None
            watches = self._live_watches(cur)
            if watches:
                self._idle_reset(cur)
                self._log_wait(cur, f"is {state or status} with {len(watches)} watch(es) running")
                return None
            shells = self._live_shells(cur.session_uuid)
            if shells:
                self._idle_reset(cur)
                self._log_wait(cur, f"is {state or status} with {len(shells)} background shell(s) running")
                return None
            waiting = self._merge_waits(cur, state)
            if waiting:
                self._idle_reset(cur)
                self._log_wait(cur, "idle, waiting on merge of " + ", ".join(f"#{n}" for n in waiting))
                return None
            if state not in LIVE_STATES:
                if state == BLOCKED_STATE and last_type == "turn_end":
                    # #285: `blocked` in the listing, but the transcript ends with the end of a
                    # turn: no prompt, a finished session.
                    return "done", self._finish(cur, "done")
                return str(state), self._finish(cur, str(state))
            # Idle in a live state (#58): it ended once no issue of its own is still claimed.
            if cur.claimed_now is None:
                self._log_wait(cur, "is idle and its claims are unreadable")
                return None
            if not cur.claimed_now:
                return "idle", self._finish(cur, "idle")
            held = ", ".join(f"#{n}" for n in sorted(cur.claimed_now))
            if str(session.get("waitingFor") or "").strip():
                # the listing says it waits for a permission or an answer: a human's, not a clock's
                self._idle_reset(cur)
                self._log_wait(cur, f"looks idle while holding {held} (state={state!r}); waiting on the owner")
                return None
            return self._idle_clock(cur, held)
        self._idle_reset(cur)
        if cur.finishing and cur.session_uuid:
            # #727: finishing, still listed working/busy: end it when its last turn is old and no
            # subagent runs. #196: nor a watch, nor a background shell of its own.
            last = last_timed_record_at(Path(self.projects_dir) / f"{cur.session_uuid}.jsonl")
            last_at = _parse_iso(last) if last else None
            if last_at is not None:
                age = self._wall_age(last_at)
                if age > FINISHING_IDLE_END_SECONDS and not running_subagent_ids(
                        self.projects_dir, cur.session_uuid, now=self.timestamp()) and not self._live_watches(cur) and not self._live_shells(cur.session_uuid):
                    self.log(f"orch {cur.session} is finishing and its last turn is {age / 60:.0f} min "
                             f"old with no subagent, watch or background shell running; ending it")
                    return "idle-timeout", self._finish(cur, "finishing and idle 10 min")
        return None

    def _silence_step(self, cur: "_Current", state: Any) -> tuple[str, dict[int, str]] | None:
        """#265: a session silent for `silent_limit` -- no timed record in its own transcript or in a running
        subagent's -- that has a background shell with no end record. Finishing: ended at this poll (the #727 end's
        path). Not finishing: woken once (naming its shells and its claims); ended `silent_limit` after the wake
        when it wrote nothing since. A live watch keeps its own cap (`_session_watches`) and holds it as before; a
        `failed` row's subagents never count (#1021). One log line and one brain notice per wake and per end."""

        if not cur.session_uuid or state == "failed":
            return None
        last = last_timed_record_at(Path(self.projects_dir) / f"{cur.session_uuid}.jsonl")
        last_at = _parse_iso(last) if last else None
        if last_at is None or self._wall_age(last_at) < self.silent_limit:
            return None  # the cheap read: only a session whose own transcript is old is looked at further
        if self._live_watches(cur):
            return None
        shells = self._live_shells(cur.session_uuid)
        if not shells:
            return None
        now = self.timestamp()
        if not cur.silent_loaded:
            cur.silent_loaded = True
            for event in self._read_events():
                if event.get("kind") == "orch_silent_wake" and event.get("session") == cur.session:
                    cur.silent_woken = _parse_iso(str(event.get("at"))) or cur.silent_woken
        sub_last = []
        for agent_id in running_subagent_ids(self.projects_dir, cur.session_uuid, now=now):
            stamp = last_timed_record_at(Path(self.projects_dir) / cur.session_uuid / "subagents"
                                         / f"agent-{agent_id}.jsonl")
            at = _parse_iso(stamp) if stamp else None
            if at is not None:
                sub_last.append(self._stood_shift(at, now))
        verdict = silence_verdict(
            now=now.timestamp(), own_last_record=self._stood_shift(last_at, now), subagent_last_records=sub_last,
            finishing=cur.finishing,
            woken_at=self._stood_shift(cur.silent_woken, now) if cur.silent_woken else None,
            limit_seconds=self.silent_limit, shells=shells)
        if verdict.action == "none":
            return None
        minutes = int(verdict.silent_seconds // 60)
        name = cur.name if cur.name.startswith("orch") else f"orch {cur.name or cur.session}"
        shells_text = ", ".join(verdict.shells)
        if verdict.action == "end":
            line = f"{name} silent {minutes} min (shells: {shells_text}); ended"
            self.log(line)
            self._tell_silent_to_brain(cur, line, "ended")
            return "idle-timeout", self._finish(cur, f"silent {minutes} min with background shells")
        claims = ", ".join(f"#{n}" for n in cur.claim_pool()) or "none"
        target = cur.name or cur.session
        try:
            ok, detail = self.claude.wake(target, (
                f"Dispatcher: you have been silent {minutes} min; these shells have no end notice: "
                f"{shells_text}; you claim {claims}. Finish, park, or say what you wait on "
                f"({ORCH_DOC}). If you write nothing in {self.silent_limit / 60:.0f} min you are ended and "
                f"these issues go back to the pool."))
        except Exception as exc:  # a wake that raises is a wake that failed
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        if not ok:
            self.log(f"silent wake to {target} failed: {detail}; trying again at the next poll")
            return None
        # the delivery wrote its own timed record into the session's transcript: the wake time is read after it
        # returned, so that record is not newer than it and is never taken for the session's answer
        cur.silent_woken = self.timestamp()
        self._append_event({
            "ts": cur.silent_woken.isoformat(timespec="seconds"), "at": cur.silent_woken.isoformat(),
            "kind": "orch_silent_wake", "session": cur.session, "issue": None, "target": target, "ok": True,
        })
        line = f"{name} silent {minutes} min (shells: {shells_text}); woken"
        self.log(line)
        self._tell_silent_to_brain(cur, line, "woken")
        return None

    def _stood_shift(self, at: datetime, now: datetime) -> float:
        """The wall time `at` as a number moved forward by the time the machine stood in play since it (#779 rule 5),
        so `now - shifted` is the age the other clocks of this class read."""
        from . import machine_mode
        return at.timestamp() + machine_mode.stood_between(at.timestamp(), now.timestamp())

    def _tell_silent_to_brain(self, cur: "_Current", line: str, word: str) -> None:
        """#265: the one brain notice of a wake or an end by silence, through the notifier's own-line path (the
        #209 unblocked line's: its chat, hold while no brain chat is live, once-only record). It is keyed by the
        session and the word, and told on the first issue the session claims (its area routes the chat); a session
        with no issue has none to route by, so only the log line is written. Never raises."""

        if self._brain_notifier is None:
            return
        pool = cur.claim_pool()
        if not pool:
            return
        number = pool[0]
        claims = ", ".join(f"#{n}" for n in pool)
        key = f"silent:{cur.session}:{word}"
        try:
            self._brain_notifier.observe(
                {number: UNBLOCKED}, lines={number: (key, f"#{number}: {line} (claims {claims})")})
        except Exception as exc:  # noqa: BLE001 -- a notice never stops a tick
            self.log(f"silent notice failed: {type(exc).__name__}: {exc}")

    def _merge_waits(self, cur: "_Current", state: Any) -> list[int]:
        """#1072: the issues this idle session is kept for: it is not finishing, holds no claimed issue,
        is `working` or `done` (its turn ended), and an issue it held and parked carries its own
        `parked on merge <result file>` checkpoint younger than `MERGE_WAIT_CEILING_SECONDS`. Once the
        merge result is there (the file exists or the result comment is posted) the session is kept one
        more tick so `_maybe_wake` can name the issue; no wake in that time and it is ended as before."""

        if cur.finishing or cur.claimed_now is None or cur.claimed_now:
            return []
        if state not in LIVE_STATES and state != "done":
            return []
        ids = tuple(i for i in (cur.session, cur.session_uuid) if i)
        now = self.clock()
        waiting: list[int] = []
        for n in sorted(cur.held):
            try:
                wait = self.gh.merge_wait(n, ids)
            except Exception:  # a failed read never keeps a session alive
                wait = None
            if wait is None:
                cur.merge_result_seen.pop(n, None)
                continue
            if self._wall_age(wait.comment_at) >= MERGE_WAIT_CEILING_SECONDS:
                continue
            if wait.result_posted or (wait.result_file and Path(wait.result_file).is_file()):
                first = cur.merge_result_seen.setdefault(n, now)
                if now - first >= TICK_SECONDS:
                    continue
            waiting.append(n)
        cur.merge_waited |= set(waiting)
        return waiting

    @staticmethod
    def _idle_reset(cur: "_Current") -> None:
        """#766: the session is not idle (it works, a subagent runs, or it waits on a human): the
        idle clock and its one wake start over."""

        cur.idle_since = cur.idle_woken_at = cur.idle_woken_wall = None

    def _idle_clock(self, cur: "_Current", held: str) -> tuple[str, dict[int, str]] | None:
        """#766: an idle session in a live state that holds claims and runs no subagent is never left
        alone longer than `IDLE_END_SECONDS`. Finishing: ended once it has been idle that long. Not
        finishing: woken once at that mark (naming what it holds and each issue's last comment), ended
        `IDLE_END_SECONDS` after the wake if still idle. Its issues resolve as for any ended session."""

        now = self.clock()
        last_at = None
        if cur.session_uuid:
            last = last_timed_record_at(Path(self.projects_dir) / f"{cur.session_uuid}.jsonl")
            last_at = _parse_iso(last) if last else None
        if (cur.idle_woken_wall is not None and last_at is not None
                and last_at > cur.idle_woken_wall):
            self._idle_reset(cur)  # it answered the wake (its transcript moved): the clock restarts
        if cur.idle_since is None:
            age = max(0.0, self._wall_age(last_at)) if last_at else 0.0
            cur.idle_since = now - age
            self._log_wait(cur, f"is idle while holding {held}; idle clock {IDLE_END_SECONDS // 60} min")
        if cur.idle_woken_at is not None:
            due = now - cur.idle_woken_at - self._stood_since(cur.idle_woken_at) >= IDLE_END_SECONDS
        elif now - cur.idle_since - self._stood_since(cur.idle_since) >= IDLE_END_SECONDS:
            if cur.finishing:
                due = True
            else:
                self._wake_idle(cur, now)
                return None
        else:
            return None
        if not due:
            return None
        self.log(f"orch {cur.session} stayed idle {IDLE_END_SECONDS // 60} min holding {held}"
                 f"{'' if cur.finishing else ' after the wake'}; ending it")
        return "idle-timeout", self._finish(cur, "idle 10 min holding claims")

    def _wake_idle(self, cur: "_Current", now: float) -> None:
        """#766: the one wake into a session idle holding claims: names each issue and when it was
        last commented (a merge result or a job's end lands there)."""

        parts: list[str] = []
        for n in sorted(cur.claimed_now or ()):
            try:
                at = self.gh.last_comment_at(n)
            except (RuntimeError, ValueError):
                at = None
            parts.append(f"#{n} (last comment {at.astimezone().strftime('%H:%M') if at else 'none'})")
        cur.idle_woken_at = now
        cur.idle_woken_wall = self.timestamp()
        self._tell_session(cur, "orch_idle_wake", (
            f"Dispatcher: you ended your turn {IDLE_END_SECONDS // 60} min ago holding "
            f"{', '.join(parts)} with no subagent running. Re-read each issue (a merge result or a "
            f"job's end may have landed since its last comment) and work it, park it or close it "
            f"({ORCH_DOC}). If you do nothing, you are ended in {IDLE_END_SECONDS // 60} min and "
            f"these issues go back to the pool."))
        self.log(f"woke {cur.name or cur.session}: idle {IDLE_END_SECONDS // 60} min holding "
                 f"{', '.join(f'#{n}' for n in sorted(cur.claimed_now or ()))}")

    def _refusals_in_wait(self, cur: "_Current", wait_seconds: float) -> int:
        """#285: `permission_refused` lines of this session (its id or its uuid, the hook writes
        the uuid) whose `ts` (local time, `util.now`) falls inside the last `wait_seconds`."""

        ids = {cur.session, cur.session_uuid} - {None}
        since = self.timestamp() - timedelta(seconds=wait_seconds)
        count = 0
        for event in self._read_events():
            if event.get("kind") != "permission_refused" or event.get("session") not in ids:
                continue
            try:
                at = datetime.strptime(str(event.get("ts")), "%Y-%m-%d %H:%M:%S").astimezone()
            except ValueError:
                continue
            if at >= since:
                count += 1
        return count

    def _check_permission_block(self, cur: "_Current", session: dict[str, Any]) -> str | None:
        """#213 safety net. A tracked session whose listing row is `blocked` or `waitingFor` a
        permission prompt is waiting for a human who is not there. It is reported to the brain on
        the very poll that sees it (so always within 2 ticks, and before the state machine below
        may end the session), then at most once per `PERMISSION_REPORT_COOLDOWN` while it stays
        stuck. The wait is counted from the last poll that found it moving, so the first message
        carries a real duration rather than zero. A session that moves again starts over.

        #285: the listing alone is not enough -- the transcript's last timed record must be a tool
        call with no result (a missing transcript keeps the listing's reading). Returns the type of
        that last record (`None` when unreadable)."""

        waiting_for = str(session.get("waitingFor") or "").strip().lower()
        listed = waiting_for == PERMISSION_WAIT or session.get("state") == BLOCKED_STATE
        last_type: str | None = None
        if listed and cur.session_uuid:
            last_type = last_timed_record_type(Path(self.projects_dir) / f"{cur.session_uuid}.jsonl")
        stuck = listed and (last_type is None or last_type == "tool_use_pending")
        now = self.clock()
        if not stuck:
            cur.last_moving_at = now
            cur.blocked_since = None
            cur.blocked_reported = None
            return last_type
        if cur.blocked_since is None:
            cur.blocked_since = cur.last_moving_at if cur.last_moving_at is not None else cur.started_at
        last = cur.blocked_reported
        if last is not None and (now - last - self._stood_since(last)) < PERMISSION_REPORT_COOLDOWN:
            return last_type
        cur.blocked_reported = now
        blocked_for = now - cur.blocked_since - self._stood_since(cur.blocked_since)
        waited = human_duration(blocked_for)
        refused = self._refusals_in_wait(cur, now - cur.blocked_since)
        # #1109: one message to each brain chat of the issues the session holds (its title when it holds none)
        for target in self._targets_of(cur.held):
            target = self._brain_wake_name(target)  # #69: the chat the lookup resolves (its name is what is woken)
            ok, detail = self.claude.wake(target, build_permission_block_message(
                cur.session, waited, cur.name, refused))
            self._append_event({
                "ts": self.timestamp().isoformat(timespec="seconds"), "kind": "permission_blocked",
                "session": cur.session, "waited_seconds": round(blocked_for, 1),
                "waited": waited, "target": target, "ok": ok,
                "state": session.get("state"), "waiting_for": session.get("waitingFor"),
                "last_record": last_type, "refused": refused,
            })
            if ok:
                self.log(f"told {target} that {cur.session} is stuck on a permission prompt ({waited})")
            else:
                self.log(f"could not tell {target} about {cur.session} stuck on a permission prompt: "
                         f"{detail}")
        return last_type

    def _mark_reader(self):
        """#122: the timeline reader of a Priority ticket's mark: the injected one, else `core_table`'s
        `GhIssueReader` over this dispatcher's own `gh` (imported here: `core_table` puts the scripts
        folder on the path at import)."""

        if self.timeline_reader is None:
            from .core_table import GhIssueReader

            def gh_text(argv: list[str]) -> str:
                result = self.gh._run(argv)
                if result.returncode != 0:
                    raise RuntimeError(f"gh {' '.join(argv[:2])} failed: {(result.stderr or '').strip()[:200]}")
                return result.stdout or ""

            self.timeline_reader = GhIssueReader(self.gh.repo, gh_runner=gh_text)
        return self.timeline_reader

    def _priority_first(self, candidates: list[IssueSummary]) -> list[IssueSummary]:
        """#122: the Priority tickets (`priority` or `flow-fix`) first, in `priority_rank` order; the rest
        keep the order they came in. The class is derived once per candidate; one timeline read per
        Priority ticket, none for the rest."""

        classes = [priority_rank.priority_class(c.labels) for c in candidates]
        if not any(classes):
            return candidates
        reader = self._mark_reader()
        keyed = []
        rest = []
        for c, cls in zip(candidates, classes):
            if cls is None:
                rest.append(c)
                continue
            keyed.append((priority_rank.rank_key(cls, self._mark_of(c.number, cls, reader)), c))
        keyed.sort(key=lambda pair: pair[0])  # stable: equal keys keep the oldest-first order
        return [c for _, c in keyed] + rest

    def _mark_of(self, number: int, cls: str, reader) -> datetime | None:
        """#122: the mark time of a Priority ticket's class label, or None when its timeline read fails: a
        failed read never breaks the tick, the ticket ranks last in its class. One line per ticket per
        failure kind (`_logged_mark_reads`)."""

        try:
            return priority_rank.mark_time(number, cls, reader)
        except Exception as exc:  # noqa: BLE001 - any read failure (gh, or a paginated JSON that does not parse)
            kind = type(exc).__name__
            if (number, kind) not in self._logged_mark_reads:
                self._logged_mark_reads.add((number, kind))
                text = " ".join(str(exc).split())[:160]
                self.log(f"priority mark of #{number} unread ({kind}: {text}); ranked last in its class")
            return None

    def _eligible_candidates(self, *, offering: bool = False) -> list[IssueSummary]:
        """The ready queue as `_maybe_start` offers it: unassigned (the `gh` call itself), open,
        `ready-for-agent`, minus the natively blocked ones (#53), the `parked` ones (#1074) and whatever a local module holds
        back or reorders (`extensions.py`, the `candidates` point). Shared with `_maybe_wake` (#137)
        so a live session is woken about exactly what a fresh ORCH would take.

        `offering` marks the one call that is about to start a session: only it orders the queue
        (oldest first) and logs what it skipped, once per changed set."""

        candidates = self.gh.ready_for_agent_issues()
        if offering:
            candidates.sort(key=lambda c: c.created_at)
        candidates = registry.run_candidates(self, candidates, offering=offering)
        candidates = self._drop_still_parked(candidates, offering=offering)
        parked = [c for c in candidates if NEEDS_INFO_LABEL in c.labels]
        candidates = [c for c in candidates if NEEDS_INFO_LABEL not in c.labels]
        if offering:
            parked_key = tuple(c.number for c in parked)
            if parked and parked_key != self._logged_needs_info:
                self.log("skipped as needs-info: " + ", ".join(f"#{c.number}" for c in parked))
            self._logged_needs_info = parked_key
        areas = area_labels()
        if areas:
            kept = []
            for c in candidates:
                why = area_problem(c.labels, areas)
                if why is None:
                    kept.append(c)
                elif offering and (c.number, why) not in self._logged_area:
                    self._logged_area.add((c.number, why))
                    self.log(f"skipped #{c.number}: {why}")
            candidates = kept
        blocked =[c for c in candidates if c.open_blockers]
        candidates = [c for c in candidates if not c.open_blockers]
        # #1074: a `parked` issue waits on a named event; it is dropped like a natively blocked one.
        candidates = [c for c in candidates if PARKED_LABEL not in c.labels]
        if offering:
            blocked_key = tuple((c.number, c.open_blockers) for c in blocked)
            if blocked and blocked_key != self._logged_blocked:
                self.log("skipped as blocked: " + ", ".join(
                    f"#{c.number} (by {', '.join(f'#{b}' for b in c.open_blockers)})" for c in blocked))
            self._logged_blocked = blocked_key
        # #924, #209: a ticket with three failed attempts in a row (send-backs, merges that did not land)
        # is not offered until an answer, a landed merge or an accepted comment comes after them.
        now = self.timestamp()
        thread = {c.number: [{"createdAt": at, "body": body} for at, body in c.comments] for c in candidates}
        statuses = {c.number: loop_status(thread[c.number], now) for c in candidates}
        # #209: a stopped ticket is never offered to ORCH; the dispatcher starts an unblocker on it.
        stopped = [c for c in candidates if statuses[c.number].stopped]
        self._loop_stopped = {c.number: statuses[c.number] for c in stopped}
        candidates = [c for c in candidates if not statuses[c.number].stopped]
        if offering:
            stopped_key = tuple((c.number, statuses[c.number].reason()) for c in stopped)
            if stopped and stopped_key != self._logged_loop_stopped:
                self.log("skipped as loop-stopped: " + ", ".join(
                    f"#{c.number} ({statuses[c.number].reason()})" for c in stopped))
            self._logged_loop_stopped = stopped_key
        if offering:  # #122: after the drops, so a dropped Priority ticket costs no timeline read
            candidates = self._priority_first(candidates)
        return candidates

    def _maybe_start_unblockers(self) -> None:
        """#209: one background `unblocker` session per trigger: a ticket in the ready queue with 3 failed attempts
        in a row since the newest answer. The trigger's key is the ticket and the `createdAt` of its newest failed
        attempt (`unblocker_starts`, kept across a restart), so a second pass and a restarted dispatcher start none.
        The ORCH start gates (a live ORCH, the offer and refusal caps, admission, outage) do not hold it: it builds
        nothing. It claims nothing and stays out of `self.sessions`: its ending is no ORCH ending. A refused start
        is told like an ORCH's (`_note_refused_start`) and tried again next pass; it is recorded once it went through."""

        self._eligible_candidates()  # refreshes `_loop_stopped`
        for number, status in sorted(self._loop_stopped.items()):
            if self.unblocker_starts.started(number, status.last_at):
                continue
            if self.dry_run:
                self.log(f"dry-run: would start unblocker for #{number} ({status.reason()})")
                continue
            try:
                session, _cmd = self.claude.start_unblocker(number)
            except RuntimeError as exc:
                self._note_refused_start("unblocker", exc, [number])
                continue
            stamp = self.timestamp().isoformat(timespec="seconds")
            self.unblocker_starts.record(number, status.last_at, session, stamp)
            self._unblocker_read_at[number] = self.clock()  # its brain-answer is first looked for a minute on
            self._append_event({"ts": stamp, "kind": "unblocker_start", "issue": number, "session": session,
                                "failed_attempts": status.count})
            self.log(f"started unblocker for #{number} ({status.reason()})")
            self._note_start_went_through("unblocker")

    def _tell_unblocker_ways(self) -> None:
        """#209: the unblocker's `<!-- brain-answer -->` on a ticket this dispatcher started one for is told ONCE to
        the area's brain chat, as one line through the brain notifier (`BrainNotifier.observe` with its `lines`: the
        chat, the send, the hold and the once-only record are its). Only tickets with a start record not yet told
        are read, each at most once a minute; the record keeps what was told, so a second pass and a restart tell
        nothing more. #222: a start older than 30 minutes with no answer after it (the limbo case) logs one
        `ended without a brain-answer` line and marks `limbo`; its one line to the brain chat ("the unblocker wrote
        no way in 30 min") is told the same way, keyed `limbo:<start>`, and then the record is done: never read
        again, no second line, no second start. A limbo line the notifier did not confirm is retried with no read.
        Never raises: a notice never stops a tick."""

        if self._brain_notifier is None or self.dry_run:
            return
        found: dict[int, tuple[str, str]] = {}
        now = self.timestamp()
        for number, record in self.unblocker_starts.untold():
            if record.get("limbo"):  # #222: logged already; its line is only retried, never read again
                found[number] = limbo_line(number, record)
                continue
            last = self._unblocker_read_at.get(number)
            if last is not None and self.clock() - last < UNBLOCKER_READ_EVERY_SECONDS:
                continue
            self._unblocker_read_at[number] = self.clock()
            try:
                comments = self.gh.issue_comments(number)
            except Exception as exc:  # noqa: BLE001 -- read again after the interval
                self.log(f"unblocker line: #{number} comments not read ({type(exc).__name__}: {exc})")
                continue
            if comments is None:
                continue
            answer = answer_after(comments, record.get("at", ""))
            if answer is None:
                started = _parse_iso(record.get("at", ""))
                if started is not None and (now - started).total_seconds() > UNBLOCKER_LIMBO_SECONDS:
                    self.log(f"unblocker for #{number} ended without a brain-answer")
                    self.unblocker_starts.mark(number, limbo="1")
                    found[number] = limbo_line(number, record)
                continue
            found[number] = (answer_key(answer), way_line(number, str(answer.get("body") or "")))
        try:
            if not found:
                return
            self._brain_notifier.observe({n: UNBLOCKED for n in found}, lines=found)
            for number, (key, _line) in found.items():
                if self._brain_notifier.knows(number, key):
                    self.unblocker_starts.mark(number, told=key)
        except Exception as exc:  # noqa: BLE001 -- a notice never stops a tick
            self.log(f"unblocker line failed: {type(exc).__name__}: {exc}")

    def _relabel_merge_results(self) -> None:
        """#874: a park step can erase a merge result that arrived first (`merge_handback.add_label`
        is a no-op while the label is still there). An open, unassigned issue with no
        `ready-for-agent` whose NEWEST comment is a `<!-- merge result -->` comment (landed or
        conflict) gets the label back, once per result comment. Closed issues are never listed;
        an issue the owner or a session labelled otherwise (`needs-info` ...) is left alone; an `autopilot`
        goal is left alone too (#990), whatever its comments say."""

        if self.dry_run:
            return
        now = self.clock()
        if now - self._last_relabel_at < RELABEL_EVERY_SECONDS:
            return
        self._last_relabel_at = now
        try:
            for item in self.gh.unlabelled_open_issues():
                labels = {str(l.get("name", "")) for l in item.get("labels") or []}
                comments = item.get("comments") or []
                if (not comments or labels & RELABEL_SKIP_LABELS or READY_LABEL in labels
                        or BRAIN_NOTICE_AUTOPILOT_LABEL in labels):
                    continue
                number = int(item["number"])
                why = self._repool_why(number, self._park_marks.get(number), comments)
                if why is not None and self.gh.add_label(number, READY_LABEL):  # #5: its merge ended
                    del self._park_marks[number]
                    self.log(why)
                    continue
                if self._return_unstarted_merge_park(number, labels, comments):  # #179: its merge never started
                    continue
                last = _unseen_merge_result(comments)
                if last is None:
                    continue
                key = (int(item["number"]), str(last.get("createdAt") or ""))
                if key in self._relabelled:
                    continue
                self._relabelled.add(key)
                if self.gh.add_label(key[0], READY_LABEL):
                    self.log(f"#{key[0]}: a merge result is its newest comment; `{READY_LABEL}` put back")
        except Exception as exc:  # a failed read never stops a tick
            self.log(f"merge-result relabel failed: {exc}")

    def _return_unstarted_merge_park(self, number: int, labels: set[str], comments: list[dict]) -> bool:
        """#179: a ticket whose newest park names a merge result file (`stalled_merge_park`: no result, past
        `merge_trace_grace_min` minutes) and whose branch's merge leaves no trace (`self.merge_trace`: not
        running, not in the line) gets `ready-for-agent` back, loses `parked`, and one comment, once per park
        (the comment is read back from the ticket, so a restart posts none again). A probe that raises counts
        as a trace. True when the ticket was put back."""

        if any(number in cur.held for cur in self.sessions):
            return False
        try:
            grace_min = float(_load_settings().get("merge_trace_grace_min"))
        except (RuntimeError, TypeError, ValueError):
            grace_min = float(_SETTING_DEFAULTS["merge_trace_grace_min"])
        stalled = stalled_merge_park(comments, self.timestamp(), grace_min * 60.0)
        if stalled is None:
            return False
        try:
            if self.merge_trace(stalled.branch, stalled.result_file, stalled.park_at.timestamp()) is not None:
                return False
        except Exception as exc:  # an unreadable trace is no proof that the merge is gone
            self.log(f"#{number}: merge trace of {stalled.branch} unreadable ({exc}); left parked")
            return False
        if not self.gh.add_label(number, READY_LABEL):
            return False
        if PARKED_LABEL in labels:
            self.gh.remove_label(number, PARKED_LABEL)
        self._park_marks.pop(number, None)
        text = (f"the merge this ticket is parked on ({stalled.result_file}) never started: no result, no suite "
                f"output, no lock or queue entry for {stalled.branch} after {grace_min:g} min; back to ready")
        self.gh.comment(number, MERGE_NEVER_STARTED_MARKER + "\n" + text)
        self.log(f"#{number}: {text}")
        return True

    def _drop_still_parked(self, candidates: list[IssueSummary], *, offering: bool) -> list[IssueSummary]:
        """#359: a parked issue (`_park_marks`) stays out of the pool until a comment after its
        parking checkpoint or a later change (a label event) shows; then the mark is dropped and
        the issue is offered like any other. A candidate with no times (`""`) is offered."""

        kept = []
        for c in candidates:
            mark = self._park_marks.get(c.number)
            why = self._repool_why(c.number, mark, [{"createdAt": at, "body": body} for at, body in c.comments],
                                   finishing=True)
            if why is not None:  # #5: the merge it waited on ended, or only the finishing note parked it
                del self._park_marks[c.number]
                self.log(why)
                kept.append(c)
            elif mark is None or not (c.latest_comment_at or c.updated_at):
                kept.append(c)
            elif c.latest_comment_at > mark.comment_at or c.updated_at > mark.updated_at:
                del self._park_marks[c.number]
                self.log(f"#{c.number}: something newer than its parking checkpoint; back in the pool")
                kept.append(c)
            elif not mark.on_merge and not self._pending_job(c.number):
                del self._park_marks[c.number]
                self.log(f"#{c.number}: parked, but no job of its own is pending; back in the pool")
                kept.append(c)
            elif offering and c.number not in self._logged_parked:
                self._logged_parked.add(c.number)
                self.log(f"#{c.number}: parked, nothing newer than its checkpoint; kept out of the pool")
        return kept

    def _repool_why(self, number: int, mark: ParkMark | None, comments: list[dict], *,
                    finishing: bool = False) -> str | None:
        """#5: the log line for a ticket whose session ended and whose park is over: its merge landed or
        failed (result file or `<!-- merge result -->` comment, whatever their order), or (`finishing`) only
        the finishing note parked it and no earlier checkpoint names an event. `None` otherwise. A ticket a
        live session holds is that session's (#1072 wakes it, nothing re-pools it)."""

        if mark is None or any(number in cur.held for cur in self.sessions):
            return None
        rc = merge_landing(comments, mark.reason_at)
        if rc is not None:
            return f"#{number}: its merge landed (rc {rc}) and its session ended; back in the pool"
        if finishing and mark.finishing_only:
            return f"#{number}: only its finishing note parks it, nothing to wait for; back in the pool"
        return None

    def _pending_job(self, number: int) -> bool:
        """#818: a job of the issue is queued or running; unknown (no reader, or it fails) counts as pending."""

        if self.has_pending_job is None:
            return True
        try:
            return bool(self.has_pending_job(number))
        except Exception:
            return True

    def _track_ready_waits(self, report: TickReport) -> None:
        """#192: `issue_ready_seen` the first tick an issue is ready (listed by
        `_eligible_candidates`: open, `ready-for-agent`, unassigned, not natively blocked), then
        `issue_taken` the first tick after that it is held by a session (`_refresh_claims`, already
        run this tick) or offered to a session `_maybe_start` just started (`report.offered`, only
        when `report.started` is set). An issue that merely drops off the ready queue (closed,
        parked, newly blocked) is forgotten with no `issue_taken` -- it was never taken up."""

        ready_now = {c.number for c in self._eligible_candidates()}
        ts = self.timestamp().isoformat(timespec="seconds")
        for number in sorted(ready_now - self._ready_seen):
            self._append_event({"ts": ts, "kind": "issue_ready_seen", "issue": number})
            self._ready_seen.add(number)

        held_now = {n for cur in self.sessions for n in cur.held}
        offered_now = set(report.offered) if report.started else set()
        for number in sorted(self._ready_seen):
            if number in held_now:
                how = "claimed"
            elif number in offered_now:
                how = "offered"
            elif number not in ready_now:
                self._ready_seen.discard(number)
                continue
            else:
                continue
            self._append_event({"ts": ts, "kind": "issue_taken", "issue": number, "how": how})
            self._ready_seen.discard(number)

    def _maybe_wake(self, cur: "_Current", report: TickReport) -> None:
        """#137: a live session sleeps between its subagents' reports and never re-reads the ready
        queue on its own; nudge it about a candidate it does not already hold. Never called for a
        finishing session (D110). At most one wake per tick (naming every new candidate at once),
        then a `WAKE_COOLDOWN_SECONDS` cooldown per issue, capped at `MAX_WAKES_PER_ISSUE` wakes per
        issue for this session."""

        if self._core_pending_holds():  # #188 rule 1: no issue is offered to a live ORCH while the core update waits
            return
        now = self.clock()
        target = cur.name or cur.session
        # #106: a woken session is handed no more than its start cap leaves room for, oldest first
        held = len(cur.claimed_now) if cur.claimed_now is not None else len(cur.held - cur.let_go)
        room = self.orch_offer_max - held
        if room <= 0:
            if cur.wake_full_logged != held:
                cur.wake_full_logged = held
                self.log(f"wake of {target} skipped: holds {held} of {self.orch_offer_max}")
            return
        cur.wake_full_logged = None
        due: list[int] = []
        for candidate in sorted(self._eligible_candidates(), key=lambda c: c.created_at):
            number = candidate.number
            # #919: held only while still assigned to this account (claimed this tick); an unassigned
            # ready issue the session parked goes on to the remaining checks. An unread claim list
            # (claimed_now None) keeps the old skip.
            if number in cur.held and (cur.claimed_now is None or number in cur.claimed_now):
                continue
            if number in cur.offered and (now - cur.started_at) <= WAKE_GRACE_SECONDS:
                continue
            count = cur.wake_counts.get(number, 0)
            if count >= MAX_WAKES_PER_ISSUE:
                continue
            last = cur.wake_last.get(number)
            if last is not None and (now - last) < WAKE_COOLDOWN_SECONDS:
                continue
            # #918: this session posted the issue's newest ORCH checkpoint and nobody else wrote since
            holder, newer = self.gh.checkpoint_holder(number, (cur.session,))
            if holder and not newer and not self._kept_for_merge(cur, number):  # #1072
                continue
            due.append(number)
        if not due:
            return
        due = due[:room]  # #106: the cap applies after the cooldown and checkpoint skips

        message = build_wake_message(due, session=cur.session, parked_by=self._parked_by_for(due) or None)
        ok, detail = self.claude.wake(target, message)
        for number in due:
            cur.wake_counts[number] = cur.wake_counts.get(number, 0) + 1
            cur.wake_last[number] = now
            cur.merge_waited.discard(number)  # #1072: the wake it was kept for; the #918 skip applies again
        named = ", ".join(f"#{n}" for n in due)
        self._append_event({
            "ts": self.timestamp().isoformat(timespec="seconds"), "kind": "orch_wake",
            "issues": list(due), "session": cur.session, "target": target, "ok": ok,
        })
        if ok:
            self._note_offer(cur, due, wake=True)
            self.log(f"woke {target} about {named}")
        else:
            self.log(f"wake of {target} about {named} failed: {detail}")
        report.woke = tuple(due)

    def _note_offer(self, cur: "_Current", numbers: Iterable[int], *, wake: bool = False) -> None:
        """#43: `numbers` were just offered to `cur` (at its start) or told to it in a wake. The latest
        offer wins: the issues leave every other session's `woken`. A wake also joins the issues to
        `cur.offered` and `cur.woken`, so a claim of one is `cur`'s claim, and no stale check takes it."""

        numbers = tuple(numbers)
        for other in self.sessions:
            if other is not cur:
                other.woken.difference_update(numbers)
        if wake:
            cur.offered = tuple(dict.fromkeys((*cur.offered, *numbers)))
            cur.woken.update(numbers)
        self._write_state()  # #50: the offer is on disk at the moment it is made, not at the tick's end

    def _kept_for_merge(self, cur: "_Current", number: int) -> bool:
        """#1072: `cur` was kept idle for `number`'s merge and its newest own checkpoint on it is still
        `parked on merge`; one that is no longer (it parked the issue again some other way) drops the
        note, so the #918 own-checkpoint skip applies to the issue again."""

        if number not in cur.merge_waited:
            return False
        try:
            wait = self.gh.merge_wait(number, tuple(i for i in (cur.session, cur.session_uuid) if i))
        except Exception:
            wait = None
        if wait is None:
            cur.merge_waited.discard(number)
        return wait is not None

    def _sweep_orphans(self, cur: "_Current") -> None:
        """#225 (was #217): right after `end_session`, stop the shells `cur`'s process tree may have
        left running -- `find /`, `grep`, `rg`, `tail`, `sleep`, a `bash -c "until ..."` loop -- but
        only the ones that actually chain up to `cur`'s own `claude.exe` pid (`cur.claude_pid`,
        recorded while it was still live). A process whose chain reaches any OTHER still-live
        session's `claude.exe` (passed here as `live_claude_pids`) is never touched, and a missing
        Windows parent alone is never enough (#225: under Git Bash it proves nothing). Only when
        `self.orphan_sweep` is set (`run_dispatch`'s real construction site; every test's default is
        `None`, so a test never touches a real process or pays a real PowerShell/`ps` query).
        `cur.started_at` is `self.clock()`-monotonic (see `_Current`), so it is converted to a
        wall-clock estimate the same way here every time: `time.time() - (self.clock() -
        cur.started_at)`, matching the real process table's `CreationDate` unit.

        Must never break session-end bookkeeping: `subprocess.run(timeout=...)` can raise
        `TimeoutExpired`, a missing `powershell`/`taskkill`/`ps` raises `OSError`, a malformed row can
        raise `ValueError`/`TypeError` -- any exception here is logged in one line and swallowed, so
        `_finish` / `_timeout` still resolve every held issue."""

        if self.orphan_sweep is None:
            return
        session_started_at = time.time() - (self.clock() - cur.started_at)
        live_claude_pids = [s.claude_pid for s in self.sessions if s is not cur and s.claude_pid]
        try:
            self.orphan_sweep(session_started_at, ended_claude_pid=cur.claude_pid,
                              live_claude_pids=live_claude_pids,
                              recorded_owned_pids=cur.owned_pids, ended_session=cur.session)
        except Exception as exc:  # the orphan sweep must never hide an issue's own outcome
            self.log(f"orphan sweep for {cur.session} failed ({exc!r})")

    def _maybe_update_owned_pids(self) -> None:
        """#225's own follow-up (the OPEN RISK): on a throttled tick, while every session in
        `self.sessions` is still alive, snapshot "whose chain reaches me" for each one's
        `claude_pid` and fold it (union, never shrunk) into that session's `owned_pids` -- the tie
        `_sweep_orphans` falls back to when ending the session has already killed the bridge rows a
        fresh chain walk at that point would need. `self.owned_pids_snapshot` is `None` by default
        (every test), so a test never touches a real process; a session with no `claude_pid` yet is
        skipped this tick, picked up once a later poll learns one."""

        if self.owned_pids_snapshot is None or not self.sessions:
            return
        claude_pids = [s.claude_pid for s in self.sessions if s.claude_pid is not None]
        if not claude_pids:
            return
        now = self.clock()
        if now - self._last_owned_pids_snapshot_at < OWNED_PIDS_SNAPSHOT_INTERVAL_SECONDS:
            return
        self._last_owned_pids_snapshot_at = now
        try:
            snapshot = self.owned_pids_snapshot(claude_pids)
        except Exception as exc:  # a failed snapshot must never break the dispatcher's own tick
            self.log(f"owned-pids snapshot failed ({exc!r})")
            return
        for s in self.sessions:
            if s.claude_pid is not None:
                s.owned_pids = s.owned_pids | snapshot.get(s.claude_pid, frozenset())

    def _maybe_sweep_whole_disk_searches(self) -> None:
        """#225 acceptance row 5: a whole-disk `find`/`grep`/`rg` (rooted at `/`, `/c`, `C:\\`, a
        drive root) tied to any `claude.exe` (any session, live) and older than the sweep's own
        floor is against the project rule for every agent -- stopped on the dispatcher's own tick,
        throttled to `WHOLE_DISK_SWEEP_INTERVAL_SECONDS` so it never runs (and never pays its own
        `ps`/CIM query) every tick. `self.whole_disk_sweep` is `None` by default (every test), so a
        test never touches a real process."""

        if self.whole_disk_sweep is None:
            return
        now = self.clock()
        if now - self._last_whole_disk_sweep_at < WHOLE_DISK_SWEEP_INTERVAL_SECONDS:
            return
        self._last_whole_disk_sweep_at = now
        try:
            self.whole_disk_sweep()
        except Exception as exc:  # a stray search must never break the dispatcher's own tick
            self.log(f"whole-disk search sweep failed ({exc!r})")

    def _finish(self, cur: "_Current", why: str, *, stop: bool = True) -> dict[int, str]:
        self.ended_sessions.add(cur.session)
        self._ended_held[cur.session] = frozenset(cur.held)
        end_session(self.claude, cur.session, run_stop=stop, log=self.log)
        self._append_event({
            "ts": self.timestamp().isoformat(timespec="seconds"), "kind": "orch_session_end",
            "session": cur.session,
        })
        self._remember_end(cur)
        self._note_api_error(cur)
        self._sweep_orphans(cur)
        outcomes: dict[int, str] = {}
        for issue in sorted(cur.held):
            if self._held_elsewhere(issue, cur):
                continue
            try:
                outcomes[issue] = self._resolve_outcome(issue, cur.session, let_go=issue in cur.let_go)
            except Exception as exc:  # one unreadable issue must not hide the others
                self.log(f"#{issue}: could not read the issue back ({exc!r})")
                outcomes[issue] = "unreadable"
            self._append_event({
                "ts": self.timestamp().isoformat(timespec="seconds"), "kind": "orch_end",
                "issue": issue, "session": cur.session, "outcome": outcomes[issue], "why": why,
            })
        if not cur.held:
            self._append_event({
                "ts": self.timestamp().isoformat(timespec="seconds"), "kind": "orch_end",
                "issue": None, "session": cur.session, "outcome": "nothing_claimed", "why": why,
            })
        summary = self._end_summary(outcomes, cur)
        self.log(f"orch {cur.session} ended ({why}): {summary}")
        self._notify_brain(outcomes, cur)
        self._tell_half_integration(cur)
        return outcomes

    def _timeout(self, cur: "_Current") -> dict[int, str]:
        self.ended_sessions.add(cur.session)
        self._ended_held[cur.session] = frozenset(cur.held)
        end_session(self.claude, cur.session, log=self.log)
        self._append_event({
            "ts": self.timestamp().isoformat(timespec="seconds"), "kind": "orch_session_end",
            "session": cur.session,
        })
        self._remember_end(cur)
        self._sweep_orphans(cur)
        outcomes: dict[int, str] = {}
        for issue in sorted(cur.held):
            if self._held_elsewhere(issue, cur):
                continue
            try:
                status = self.gh.issue_status(issue)
            except Exception as exc:
                self.log(f"#{issue}: could not read the issue back ({exc!r})")
                outcomes[issue] = "unreadable"
                continue
            ended = _ended_outcome(status)
            if ended is not None:
                self._release_ended(status, ended, cur.session)
                outcomes[issue] = ended
                continue
            if issue in cur.let_go:
                self._release_ended(status, "parked", cur.session)
                outcomes[issue] = "parked"
                continue
            self.gh.comment(issue, f"ORCH session `{cur.session}` ran past `--max-hours` "
                                   f"({self.max_hours}) and was stopped.")
            self.gh.remove_assignee(issue)
            outcomes[issue] = "timeout"
            self._append_event({
                "ts": self.timestamp().isoformat(timespec="seconds"), "kind": "orch_timeout",
                "issue": issue, "session": cur.session,
            })
        self.log(f"orch {cur.session} timed out after {self.max_hours} h: " + self._end_summary(outcomes, cur))
        self._notify_brain(outcomes, cur)
        self._tell_half_integration(cur)
        return outcomes

    def _tell_half_integration(self, cur: "_Current") -> None:
        """#196: an ended session that leaves the main checkout in a half merge (MERGE_HEAD) or a half rebase: ONE
        line to the area's brain chat and the same line as a comment on each held issue. Nothing is aborted. No
        half state, or a failed read, is no line; never raises."""

        try:
            from .merge import half_integration
            kind = half_integration(Path(self.claude.cwd))
            if kind is None:
                return
            held = sorted(cur.held)
            named = ", ".join(f"#{n}" for n in held) if held else "none"
            line = (f"ORCH {cur.session[:8]} ended with a half {kind} in the main checkout; "
                    f"held issues {named}; check before a core update")
            for target in self._targets_of(held):
                if not self._brain_is_live(target, "half merge notice"):
                    self.log(f"half {kind} line not told to {target}: chat not live")
                    continue
                try:
                    self._send_to_brain(target, line)
                except Exception as exc:  # noqa: BLE001 -- a failed wake is one log line
                    self.log(f"half {kind} line to {target} failed ({exc!r})")
            for n in held:
                try:
                    self.gh.comment(n, line)
                except Exception as exc:  # noqa: BLE001 -- one failed comment must not skip the other issues
                    self.log(f"half {kind} line comment on #{n} failed ({exc!r})")
        except Exception as exc:  # noqa: BLE001 -- the end path never raises for a notice
            self.log(f"half merge check failed ({type(exc).__name__}: {exc})")

    def _end_summary(self, outcomes: dict[int, str], cur: "_Current") -> str:
        """#999: the end line's words. A held issue names its outcome; an offered one the session
        never claimed but that now reads closed is named `closed #N without a claim` (ORCH closed it
        without the assignee). Only an empty list of both reads `no issue claimed`."""

        parts = [f"#{n} {o}" for n, o in outcomes.items()]
        parts += [f"closed #{n} without a claim" for n in self._closed_unclaimed(cur)]
        return ", ".join(parts) or "no issue claimed"

    def _closed_unclaimed(self, cur: "_Current") -> list[int]:
        """#999: the issues offered to `cur` that it never held and that `gh` reads as closed, in offer
        order. One `issue_status` read each (the same read `_resolve_outcome` uses); a read that fails
        is logged and skipped. Nothing here changes `held`, a label or an assignee."""

        closed: list[int] = []
        for number in cur.offered:
            if number in cur.held:
                continue
            try:
                status = self.gh.issue_status(number)
            except Exception as exc:  # one unreadable offer must not hide the rest of the line
                self.log(f"#{number}: could not read the issue back ({exc!r})")
                continue
            if not status.open:
                closed.append(number)
        return closed

    # -- the brain chat's notice of a parked, closed or needs-info issue ---------------------------------------

    def _build_brain_notifier(self, state_path: Path, brain_title: str | None) -> BrainNotifier:
        """The notifier of `brain_notice.py` with this dispatcher's own seams. `self.gh`, `self.claude`
        and `self.log` are read at call time, so a test that swaps one afterwards is still heard."""

        settings = _load_settings()
        title = brain_title if brain_title is not None else str(settings["brain_chat_title"])
        return BrainNotifier(
            state_path=state_path, brain_title=title, brain_chats=settings.get("brain_chats") or {},
            send=self._send_to_brain, issue_info=lambda n: self.gh.issue_title_and_last_comment(n),
            autopilot_live_for=self._autopilot_live_for,
            brain_live=lambda: self._brain_is_live(title, "issue notice"),
            brain_live_for=lambda target: self._brain_is_live(target, "issue notice"),
            log=lambda text: self.log(text), now=lambda: self.timestamp())

    def _maybe_return_parked(self) -> None:
        """#105: a parked ticket whose `Parked:` event has happened goes back to ready, within one pass (one tick).
        Only with the real `gh` (`GhCli`), on that `GhCli`'s repo; the pass is built once, then reused.
        Never raises: a failed build or pass is one log line and the tick goes on (`ParkedReturn` logs its own)."""

        if not isinstance(self.gh, GhCli):
            return
        try:
            if self._parked_return is None:
                repo = self.gh.repo
                self._parked_return = ParkedReturn(repo, GhRunner(repo, self.gh._run), datetime.now,
                                                   default_target_core(_load_settings(), ROOT), self.log)
            self._parked_return.run_once()
        except Exception as exc:  # noqa: BLE001 -- the parked return never stops a tick
            self.log(f"parked return failed: {type(exc).__name__}: {exc}")

    def _build_losses_daily(self, state_path: Path, brain_title: str | None) -> LossesDaily:
        """The daily losses note of `losses_daily.py` with this dispatcher's own seams: the wake of
        `_send_to_brain`, the clock of `timestamp`, the repos and their settings of `default_compute`. The note goes
        to the chat of the `loss_note_area` label (`brain_target`), else to the title chat; its liveness is that
        chat's."""

        settings = dict(_load_settings())
        if brain_title is not None:
            settings["brain_chat_title"] = brain_title
        area = str(settings.get("loss_note_area") or "")
        title = brain_target(settings, [area] if area else [])
        return LossesDaily(
            state_path=state_path, brain_title=title, send=self._send_to_brain,
            compute=lambda: default_compute(_load_settings(), self.timestamp()),
            brain_live=lambda: self._brain_is_live(title, "daily losses"), log=lambda text: self.log(text),
            now=lambda: self.timestamp())

    def _maybe_losses_daily(self) -> None:
        """Once an hour at most (`LOSSES_DAILY_CHECK_SECONDS`): `LossesDaily.check`, which starts the day's
        computation in the background and sends its lines once it finished. Never raises: a failed check is one
        log line and the tick goes on."""

        if self._losses_daily is None:
            return
        now = self.clock()
        if self._last_losses_check_at is not None and now - self._last_losses_check_at < LOSSES_DAILY_CHECK_SECONDS:
            return
        self._last_losses_check_at = now
        try:
            self._losses_daily.check()
        except Exception as exc:  # noqa: BLE001 -- the losses note never stops a tick
            self.log(f"losses daily failed: {type(exc).__name__}: {exc}")

    def _targets_of(self, numbers: Iterable[int], labels: Mapping[int, Sequence[str]] | None = None) -> list[str]:
        """#1109: the brain chats the news of these issues goes to, each once, in issue order: an issue's area chat
        (`brain_chats`) when its labels map one, else `brain_chat_title`. An issue's labels come from `labels` when
        the caller already holds them, else from one `gh issue view` (`_read_labels`). No issue: the title alone."""

        settings = _load_settings()
        targets: list[str] = []
        for number in sorted(set(numbers)):
            known = labels.get(number) if labels is not None else None
            target = brain_target(settings, known if known is not None else self._read_labels(number))
            if target not in targets:
                targets.append(target)
        return targets or [str(settings["brain_chat_title"])]

    def _read_labels(self, number: int) -> tuple[str, ...]:
        """One `gh issue view` for the label names of `number`; a failed read is one log line and no labels."""
        try:
            return tuple(self.gh.issue_status(number).labels)
        except Exception as exc:  # a failed read sends the issue's news to the title, never stops the tick
            self.log(f"#{number} labels not read ({type(exc).__name__}: {exc}); its news goes to the title")
            return ()

    def _send_to_brain(self, title: str, message: str) -> None:
        """One wake of the brain chat with the whole message; raises when the wake fails, so the
        notifier keeps the batch for its one retry."""

        woke = self._brain_wake_name(title)
        ok, detail = self.claude.wake(woke, message)
        if not ok:
            raise RuntimeError(f"wake of {woke} failed: {detail}")
        # #55: the batch's issue numbers are the `#N` heads of its lines (`brain_notice.format_line`), so a resend matches its failure
        numbers = sorted({int(n) for n in re.findall(r"^#(\d+)[ :]", message, re.M)})
        named = f" ({' '.join(f'#{n}' for n in numbers)})" if numbers else ""
        kinds = "parked, closed or needing a decision" if ": needs a decision" in message else "parked or closed"
        self.log(f"told {woke}: {len(message.splitlines())} issue(s) {kinds}{named}")

    # -- the brain chat's notice of a failed core target (#28 unit 3) ------------------------------

    def _core_result_names(self) -> list[str]:
        """The merge result files of the results folder by name (`detach.run_and_record` writes `<stamp>-merge-<branch>.json`)."""
        if not self._core_results_dir.is_dir():
            return []
        return sorted(p.name for p in self._core_results_dir.glob("*-merge-*.json"))

    def _core_history_names(self) -> set[str]:
        """The result files whose mtime is more than `CORE_NOTICE_HISTORY_SECONDS` ago, or that vanished meanwhile."""
        cutoff = time.time() - CORE_NOTICE_HISTORY_SECONDS
        old: set[str] = set()
        for name in self._core_result_names():
            try:
                if (self._core_results_dir / name).stat().st_mtime < cutoff:
                    old.add(name)
            except OSError:
                old.add(name)
        return old

    def _core_notice_load(self) -> None:
        """The state file (`seen`, `pending`). With no usable file, the results older than
        `CORE_NOTICE_HISTORY_SECONDS` count as seen (history); a newer one is announced once."""
        path = self._core_notice_file
        raw: Any = None
        if path is not None and path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self.log(f"core failure notice: state file unreadable ({type(exc).__name__}), starting from now")
        if not isinstance(raw, dict):
            self._core_notice_seen = self._core_history_names()
            self._core_notice_save()
            return
        self._core_notice_seen = {str(n) for n in raw.get("seen") or []}
        self._core_notice_pending = [row for row in (_held_core_row(p) for p in raw.get("pending") or []
                                                     if isinstance(p, dict)) if row]
        self._core_notice_announced = [dict(a) for a in raw.get("announced") or [] if isinstance(a, dict)]
        self._empty_map_sent = {str(s) for s in raw.get("empty_maps") or []}
        self._empty_map_pending = [dict(p) for p in raw.get("empty_map_pending") or []
                                   if isinstance(p, dict) and isinstance(p.get("map"), str) and isinstance(p.get("target"), str)]

    def _core_notice_save(self) -> None:
        path = self._core_notice_file
        if path is None:
            return
        if self._core_results_dir.is_dir():  # `seen` keeps only the results still in the folder
            self._core_notice_seen &= set(self._core_result_names())
        quiet_from = time.time() - CORE_REASON_QUIET_SECONDS
        self._core_notice_announced = [a for a in self._core_notice_announced if float(a.get("at") or 0) >= quiet_from]
        data = {"seen": sorted(self._core_notice_seen), "pending": self._core_notice_pending,
                "announced": self._core_notice_announced, "empty_maps": sorted(self._empty_map_sent),
                "empty_map_pending": self._empty_map_pending}
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            self.log(f"core failure notice: state file not written ({type(exc).__name__}: {exc})")

    @staticmethod
    def _core_failure_rows(path: Path) -> tuple[list[int], list[dict[str, str]]]:
        """One merge result's issues and one row per `failed:` target of its `core_targets` (#42: repo, reason and
        the branch, grouped into one line per repo and reason when the notice is sent). A `refused:` or `taken:` line
        gives none, and neither does a record of another kind. The branch is the token after `merge` in the record's
        argv; the file name's sanitized label is the fallback."""
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("kind") != "merge":
            return [], []
        argv = record.get("argv") if isinstance(record.get("argv"), list) else []
        branch = str(argv[argv.index("merge") + 1]) if "merge" in argv and argv.index("merge") + 1 < len(argv) else ""
        if not branch or branch.startswith("-"):
            _, _, label = path.name.partition("-merge-")
            branch = label[:-len(".json")] if label.endswith(".json") else label
        rows: list[dict[str, str]] = []
        for entry in record.get("core_targets") or []:
            found = re.match(r"^(.*?): failed: (.*)$", str(entry))
            if found:
                rows.append({"repo": found.group(1), "reason": found.group(2), "branch": branch})
        return [int(n) for n in record.get("issues") or []], rows

    def _notify_core_failures(self) -> None:
        """#28 unit 3: a merge result with a `failed:` line in `core_targets` puts one line per such target to the
        brain chat of the merged issues' areas (`_targets_of`; the title when the merge names no issue). Each result
        is read once. The lines wait as the parked and closed notices do: a chat not live keeps them with no attempt
        spent, a failed wake is retried once. Never raises: a failure is one log line and the tick goes on."""

        if self._core_notice_file is None or self.dry_run:
            return
        changed = False
        try:
            for name in self._core_result_names():
                if name in self._core_notice_seen:
                    continue
                self._core_notice_seen.add(name)
                changed = True
                self._note_empty_map(self._core_results_dir / name)  # #74 unit 2: a map that holds 0 tests
                try:
                    issues, rows = self._core_failure_rows(self._core_results_dir / name)
                except (OSError, ValueError) as exc:
                    self.log(f"core failure notice: {name} not read ({type(exc).__name__}: {exc})")
                    continue
                if not rows:
                    continue
                for target in self._targets_of(issues):
                    self._core_notice_pending.extend({**row, "target": target, "attempts": 0} for row in rows)
            changed = self._flush_empty_map_notices() or changed
            changed = self._flush_core_notices() or changed
        except Exception as exc:  # noqa: BLE001 -- a notice never stops a tick
            self.log(f"core failure notice failed: {type(exc).__name__}: {exc}")
        if changed:
            self._core_notice_save()

    #: #74 unit 2: the suite line of a merge whose map holds 0 tests (`test_select._empty_map`), its map as sha8.
    _EMPTY_MAP_LINE = re.compile(r"^full suite: map holds 0 tests \(map ([0-9a-f]{8})\)$")

    def _note_empty_map(self, path: Path) -> None:
        """#74 unit 2: a merge result whose selection reads ``full suite: map holds 0 tests (map <sha8>)`` holds one
        notice for that map commit, unless that map was told already or is held. Never raises: a result that
        cannot be read is one log line."""
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.log(f"empty map notice: {path.name} not read ({type(exc).__name__}: {exc})")
            return
        found = self._EMPTY_MAP_LINE.match(str(record.get("selection") or "")) if isinstance(record, dict) else None
        if found is None:
            return
        sha8 = found.group(1)
        if sha8 in self._empty_map_sent or any(p["map"] == sha8 for p in self._empty_map_pending):
            return
        self._empty_map_pending.append({"map": sha8, "target": self._targets_of(())[0], "attempts": 0})

    def _flush_empty_map_notices(self) -> bool:
        """#74 unit 2: one wake per held empty-map notice, to its chat once that chat is live (a chat not live holds
        it, no attempt spent). A failed wake spends one attempt; the second failure drops the notice with one log line.
        Returns whether the held notices changed."""
        changed = False
        for p in list(self._empty_map_pending):
            if not self._brain_is_live(p["target"]):
                continue
            changed = True
            try:
                self._send_to_brain(p["target"], f"test map {p['map']} holds 0 tests: every merge runs the full suite "
                                                 f"until the next green full run")
            except Exception as exc:  # the wake failed: one retry, then the notice is dropped
                p["attempts"] = int(p.get("attempts", 0)) + 1
                if p["attempts"] >= MAX_ATTEMPTS:
                    self._empty_map_pending.remove(p)
                    self.log(f"empty map notice: send for map {p['map']} failed ({type(exc).__name__}: {exc}); dropped")
                else:
                    self.log(f"empty map notice: send for map {p['map']} failed ({type(exc).__name__}: {exc}); "
                             f"kept for one retry")
                continue
            self._empty_map_sent.add(p["map"])
            self._empty_map_pending.remove(p)
        return changed

    def _flush_core_notices(self) -> bool:
        """One wake per target chat that is live, carrying each of its held lines. A chat not live holds its lines
        (one log line per away period); a failed wake spends one attempt, and the second failure drops the lines.
        Returns whether the held lines changed."""

        changed = False
        for target in sorted({str(p["target"]) for p in self._core_notice_pending}):
            rows = [p for p in self._core_notice_pending if p["target"] == target]
            if not self._brain_is_live(target, "core failure notice"):
                if target not in self._core_notice_away:
                    self._core_notice_away.add(target)
                    self.log(f"core failure notice: brain chat {target} not live, {len(rows)} line(s) kept until it is live")
                continue
            self._core_notice_away.discard(target)
            changed = True
            groups: dict[tuple[str, str], list[str]] = {}
            for p in rows:
                groups.setdefault((str(p["repo"]), str(p["reason"])), []).append(str(p["branch"]))
            quiet_from = time.time() - CORE_REASON_QUIET_SECONDS
            fresh = {key: branches for key, branches in groups.items()
                     if not self._announced_recently(target, key, quiet_from)}
            if len(fresh) < len(groups):
                self.log(f"core failure notice: {len(groups) - len(fresh)} reason(s) already announced to {target} "
                         f"in the last 6 h, nothing sent for them")
            lines = [f"core update failed in {repo} after {len(branches)} merge(s) ({', '.join(branches)}): {reason}"
                     for (repo, reason), branches in fresh.items()]
            try:
                if lines:
                    self._send_core_notice(target, "\n".join(lines))
            except Exception as exc:  # the wake failed: one retry, then the lines are dropped
                for p in rows:
                    p["attempts"] = int(p.get("attempts", 0)) + 1
                kept = [p for p in rows if p["attempts"] < MAX_ATTEMPTS]
                self.log(f"core failure notice: send to {target} failed ({type(exc).__name__}: {exc}); "
                         f"{len(kept)} kept for one retry, {len(rows) - len(kept)} dropped")
                self._core_notice_pending = [p for p in self._core_notice_pending if p["target"] != target] + kept
                continue
            self._note_announced(target, fresh, time.time())
            self._core_notice_pending = [p for p in self._core_notice_pending if p["target"] != target]
        return changed

    def _announced_recently(self, target: str, key: tuple[str, str], quiet_from: float) -> bool:
        """#42: whether this chat got this (repo, reason) announced at or after `quiet_from` (clock time)."""
        return any(a.get("target") == target and (a.get("repo"), a.get("reason")) == key
                   and float(a.get("at") or 0) >= quiet_from for a in self._core_notice_announced)

    def _note_announced(self, target: str, keys: Iterable[tuple[str, str]], at: float) -> None:
        """#42: the (repo, reason) pairs a wake to `target` just carried, each with its clock time."""
        sent = {(target, repo, reason) for repo, reason in keys}
        if not sent:
            return
        kept = [a for a in self._core_notice_announced if (a.get("target"), a.get("repo"), a.get("reason")) not in sent]
        self._core_notice_announced = kept + [{"target": t, "repo": r, "reason": s, "at": at} for t, r, s in sent]

    def _send_core_notice(self, title: str, message: str) -> None:
        """One wake of a brain chat with its failed-target lines; raises when the wake fails (the lines are kept)."""
        woke = self._brain_wake_name(title)
        ok, detail = self.claude.wake(woke, message)
        if not ok:
            raise RuntimeError(f"wake of {woke} failed: {detail}")
        self.log(f"told {woke}: {len(message.splitlines())} core update failure line(s)")

    # -- the brain chat of this repo (#69) -------------------------------------------------------

    def _resolve_brain_chat(self, title: str) -> Resolved:
        """The chat the news of `title` goes to: the live row named `title`, else an alias from the settings, else
        the one live interactive row in this repo's folder (`resolve_brain_chat`). #946: another repo's folder
        never counts."""
        settings = _load_settings()
        aliases = settings.get("brain_chat_title_aliases") or []
        rows = self.claude.list_sessions()
        folder = str(self.claude.cwd)
        resolved = resolve_brain_chat(rows, title, aliases, folder, BRAIN_DEAD_STATES)
        own = str(settings.get("brain_chat_title") or "")
        if resolved.how in ("exact", "alias") or not own or own == title:
            return resolved
        # rule 3: an area chat that is not live falls back to the repo's own chat (its title, its aliases, its folder)
        return resolve_brain_chat(rows, own, aliases, folder, BRAIN_DEAD_STATES)

    def _note_fallback(self, title: str, resolved: Resolved) -> None:
        """The one log line of a folder fallback, once per changed name (`FallbackLogger`)."""
        line = self.fallback.line(title, resolved)
        if line is not None:
            self.log(line)

    def _brain_wake_name(self, title: str) -> str:
        """The name a wake for `title` goes to: the chat the lookup resolves, else `title` itself."""
        resolved = self._resolve_brain_chat(title)
        self._note_fallback(title, resolved)
        return resolved.name if resolved.name is not None else title

    def _brain_is_live(self, title: str, source: str = "brain notice") -> bool:
        """Whether news for `title` can be told now. A look that finds no chat starts
        the away clock, adds `source` to the batch's sources and, once the batch is 30 min old, tells the home's
        brain chat once (`_home_alert`). A chat found clears the batch. #138: a chat found that awaits its owner
        after a `/clear` is held (`_held_for_owner`): False, and neither the away clock nor the batch is touched."""

        resolved = self._resolve_brain_chat(title)
        self._note_fallback(title, resolved)
        if self._held_for_owner(title, resolved):
            return False
        self._owner_hold.discard(title)
        if resolved.name is not None:
            self.away.note(title, True)
            self._since_text.pop(title, None)
            self._batch_sources.pop(title, None)
            return True
        self.away.note(title, False)
        self._since_text.setdefault(title, self.timestamp().astimezone().strftime("%H:%M"))
        self._batch_sources.setdefault(title, set()).add(source)
        if self.away.home_alert_due(title):
            self._home_alert(title, resolved, self._since_text[title], self._batch_sources[title])
        return False

    def _session_id_of(self, resolved: Resolved) -> str | None:
        """The `sessionId` of the live row the lookup resolved to `resolved.name`: for a folder fallback the live
        interactive row in this repo's folder, as `resolve_brain_chat` counts it; else the first live row of that
        name. None when there is no such row or it has no `sessionId`."""

        key = _folder_key(str(self.claude.cwd))
        for row in self.claude.list_sessions():
            if row.get("name") != resolved.name or row.get("state") in BRAIN_DEAD_STATES:
                continue
            if resolved.how == "folder" and not (row.get("kind") == "interactive"
                                                 and _folder_key(row.get("cwd")) == key):
                continue
            return row.get("sessionId") or None
        return None

    def _held_for_owner(self, title: str, resolved: Resolved) -> bool:
        """#138: True while the chat `resolved` names sits after a `/clear` with no owner message yet
        (`awaiting_owner_message` on its transcript): the dispatcher's news then waits. One log line per hold start
        (`_owner_hold` remembers the title). No chat, no `sessionId`, or any read error is not a hold: today's
        behaviour."""

        if resolved.name is None:
            return False
        try:
            session_id = self._session_id_of(resolved)
            if not session_id:
                return False
            if not awaiting_owner_message(Path(self.projects_dir) / f"{session_id}.jsonl"):
                return False
        except Exception:  # noqa: BLE001 -- an unreadable row or transcript is not a hold
            return False
        if title not in self._owner_hold:
            self._owner_hold.add(title)
            self.log(f"brain chat {resolved.name} just cleared, no owner message yet: news held")
        return True

    def _home_alert(self, title: str, resolved: Resolved, since_text: str, sources: set[str]) -> None:
        """#69: one line to the home's brain chat that `title` has no chat and news is held. The home itself (repo ==
        home) has no higher chat: the line goes to this log only. Never raises: a failure is one log line."""

        text = ""
        try:
            settings = _load_settings()
            repo = str(settings["repo"])
            home = str(settings.get("home") or repo)
            text = home_alert_text(repo, title, resolved.count, len(sources), since_text)
            if home == repo:
                self.log(text)
                return
            home_title = read_home_brain_title(home, self.home_gh_runner)
            if home_title is None:
                self.log(f"home brain chat title not read; alert kept in this log: {text}")
                return
            ok, detail = self.claude.wake(home_title, text)
            if ok:
                self.log(f"told {home_title}: {text}")
            else:
                self.log(f"could not tell {home_title}: {detail}")
        except Exception as exc:  # the alert never stops a tick
            kept = f"; alert kept in this log: {text}" if text else ""
            self.log(f"home brain chat alert failed ({type(exc).__name__}: {exc}){kept}")

    def _autopilot_live_for(self, number: int) -> str | None:
        """The name of the autopilot stretch live on the issue or one of its ancestors (native parent links,
        at most `BRAIN_NOTICE_ANCESTOR_DEPTH` up), `None` otherwise. A stretch is live from its goal's start
        to its end (#1129), not only while one `autopilot #K` session is working: a live session of that
        name counts, and so does the goal the dispatcher keeps starting sessions on (see
        `_autopilot_stretch_open`), which holds the stretch between two sessions. A failed `gh` read raises
        (the notifier retries later)."""

        if not self._autopilot_module_loaded():
            return None
        current: int | None = number
        for _ in range(BRAIN_NOTICE_ANCESTOR_DEPTH):
            if current is None:
                return None
            status = self.gh.issue_status(current)
            if BRAIN_NOTICE_AUTOPILOT_LABEL in status.labels:
                name = f"autopilot #{current}"
                if self._autopilot_session_live(name) or self._autopilot_stretch_open(current, status):
                    return name
            current = self.gh.parent_issue(current)
        return None

    def _autopilot_session_live(self, name: str) -> bool:
        return any(str(s.get("name") or "") == name and s.get("state") in LIVE_STATES
                   for s in self.claude.list_sessions())

    def _autopilot_module_loaded(self) -> bool:
        """#10: the autopilot methods exist on this dispatcher only when `.autopilot` is in `local_modules`
        (`autopilot.py`, `DISPATCHER_METHODS`). Without it no autopilot stretch exists: the core answers "none"."""

        return hasattr(self, "_read_terms") and hasattr(self, "_deadline_reached")

    def _autopilot_stretch_open(self, goal: int, status: IssueStatus) -> bool:
        """#1129: the stretch on a labelled goal is open while the goal is what `autopilot_goals` reads
        (open, `autopilot`, not `autopilot-stuck`, `autopilot.py`) and its `Autopilot terms` deadline, if any,
        has not passed (`_tick_autopilot` starts no session past it). A goal stopped by the owner or by its
        own stop rule drops the label, and a goal done or stuck drops out of that set: the stretch has ended.
        #10: no stretch at all without the `.autopilot` module (`_autopilot_module_loaded`)."""

        if not self._autopilot_module_loaded() or not status.open or BRAIN_NOTICE_AUTOPILOT_STUCK_LABEL in status.labels:
            return False
        terms = self._read_terms(goal)
        return terms is None or terms.deadline is None or not self._deadline_reached(goal, terms)

    def _notify_brain(self, outcomes: Mapping[int, str], cur: "_Current | None" = None) -> None:
        """Hand the parked, closed and needs-info ones of `outcomes` to the notifier (and any retry it still
        holds). Never raises: the dispatcher's own flow is the same with or without it."""

        if self._brain_notifier is None:
            return
        if cur is not None:
            for n in cur.held:
                self._notice_class.pop(n, None)
        try:
            self._brain_notifier.observe({n: o for n, o in outcomes.items() if o in BRAIN_NOTICE_WORDS})
        except Exception as exc:  # noqa: BLE001 -- a notice never stops a tick
            self.log(f"brain notice failed: {type(exc).__name__}: {exc}")

    def _notify_live_outcomes(self) -> None:
        """Once per tick: an issue a LIVE session holds that is no longer claimed (it left the claimed
        set) is read once and, when closed, parked or `needs-info` by the same rule as at a session's end
        (`_ended_outcome`), handed to the notifier. An issue already classified is not read again until
        a session claims it again; the notifier's own record keeps a repeat at no message. #213: a
        `needs-info` one is read again every `NEEDS_INFO_REREAD_SECONDS`, so a checkpoint newer than the last
        notice (a new question) is told too."""

        if self._brain_notifier is None:
            return
        found: dict[int, str] = {}
        for cur in self.sessions:
            if cur.claimed_now is None:
                continue
            for n in cur.claimed_now:
                self._notice_class.pop(n, None)
            for n in sorted(cur.held - cur.claimed_now):
                if n in found:
                    continue
                known = self._notice_class.get(n)
                if known == NEEDS_INFO_WORD:
                    if self.clock() - self._needs_info_read_at.get(n, 0.0) < NEEDS_INFO_REREAD_SECONDS:
                        continue
                elif known is not None:
                    continue
                try:
                    ended = _ended_outcome(self.gh.issue_status(n))
                except Exception:  # noqa: BLE001 -- read again next tick
                    continue
                if ended is not None:
                    self._notice_class[n] = ended
                    if ended == NEEDS_INFO_WORD:
                        self._needs_info_read_at[n] = self.clock()
                    found[n] = ended
        self._notify_brain(found)

    # -- what a finished session left behind, per issue ------------------------------------------

    def _held_elsewhere(self, issue: int, cur: "_Current") -> bool:
        """#336, #514: another tracked live session has `issue` in its `held`, let go or not: the
        ended session's end never comments on it or unassigns it; it is resolved at the holder's
        own end (its `held` keeps it)."""

        for other in self.sessions:
            if other is not cur and other.session not in self.ended_sessions and issue in other.held:
                if issue in other.let_go:
                    self.log(f"#{issue}: held by live session {other.session} (let go earlier); "
                             f"resolved at its end")
                else:
                    self.log(f"#{issue}: held by live session {other.session}; "
                             f"left alone at the end of {cur.session}")
                return True
        return False

    def _note_parked_by(self, issue: int, session: str, at: str, *, keep_date: bool = False) -> None:
        """#599: remember `session` as the one that parked `issue` (the time its outcome was
        resolved, or the parking checkpoint's date once `_release_ended` reads it). `keep_date`:
        the same session's earlier note keeps its date."""

        old = self._parked_by.get(issue)
        if keep_date and old and old.get("session") == session:
            return
        self._parked_by[issue] = {"session": session, "at": at}

    def _parked_by_for(self, issues: Any) -> dict[int, dict[str, str]]:
        # a finishing live session's held issue that is not claimed now (offered again) was let go
        # by it, even on the tick the session became finishing (before `_refresh_claims` sees it)
        for cur in self.sessions:
            if not cur.finishing or cur.session in self.ended_sessions or cur.claimed_now is None:
                continue
            for n in issues:
                if n in cur.held and n not in cur.claimed_now:
                    self._note_parked_by(n, cur.session, self.timestamp().isoformat(timespec="seconds"),
                                         keep_date=True)
        return {n: self._parked_by[n] for n in issues if n in self._parked_by}

    def _read_parked_by(self) -> dict[int, dict[str, str]]:
        path = getattr(self, "state_file", None)
        if path is None or not Path(path).is_file():
            return {}
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8")).get("parked_by") or {}
            return {int(k): {"session": str(v["session"]), "at": str(v["at"])} for k, v in raw.items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return {}

    def _remember_end(self, cur: "_Current") -> None:
        """#359: the session's end time as the dispatcher knows it: the `timestamp` of the last
        timed record of its transcript (`last_timed_record_at`), read before its issues are
        resolved. Not the time this poll saw it ended: an answer between the two must stay newer
        than the parking checkpoint. `""` when there is no transcript."""

        end_at = ""
        if cur.session_uuid:
            end_at = last_timed_record_at(Path(self.projects_dir) / f"{cur.session_uuid}.jsonl") or ""
        self._end_at[cur.session] = end_at
        self._start_wall[cur.session] = self.timestamp().timestamp() - (self.clock() - cur.started_at)

    def _note_api_error(self, cur: "_Current") -> None:
        """#931: a session whose transcript ends in an API error (an outage, a spent limit, an auth
        error) is not the ticket's fault. Its held issues go back to the pool with no comment and no
        streak (`_resolve_outcome`), and no new session starts for `OUTAGE_WAIT_S`. A failed read is
        no error; a session that held nothing still waits. One log line per such ending."""

        err: str | None = None
        if cur.session_uuid:
            try:
                err = last_api_error(Path(self.projects_dir) / f"{cur.session_uuid}.jsonl")
            except Exception:  # an unreadable transcript is no evidence of an outage
                err = None
        if not err:
            return
        self._api_error[cur.session] = err
        self._outage_until = self.clock() + OUTAGE_WAIT_S
        self.log(f"orch {cur.session} ended by an API error ({err}); no new session for "
                 f"{OUTAGE_WAIT_S // 60} min")

    def _release_ended(self, status: IssueStatus, outcome: str, session: str = "") -> None:
        """#359: a session's end leaves no open issue it held assigned: a parked / `needs-info`
        one that still shows the assignee loses it (`--remove-assignee`), and a parked one is marked
        (`ParkMark`) so the pool offers it again only after something newer than its checkpoint."""

        if outcome == "closed":
            self._parked_by.pop(status.number, None)
        if outcome not in ("parked", "needs_info"):
            return
        # #336: one this session let go and another live session now holds is that one's claim.
        if self._held_by_other_live(status.number, session):
            return
        if status.assignees and self._claimed_after_release(status.number, session):
            self.log(f"#{status.number}: assigned again after {session} let it go; left assigned")
            return
        if status.assignees:
            self.gh.remove_assignee(status.number)
        self._note_parked_by(status.number, session, self.timestamp().isoformat(timespec="seconds"))
        if outcome == "parked":
            self._logged_parked.discard(status.number)
            ids = {session} | {c.session_uuid for c in self.sessions if c.session == session and c.session_uuid}
            mark = self.gh.parking_mark(status.number, self._end_at.get(session, ""),
                                        tuple(sorted(i for i in ids if i)))
            if mark is not None:
                self._park_marks[status.number] = mark
                if mark.comment_at:
                    self._parked_by[status.number]["at"] = mark.comment_at

    def _resolve_outcome(self, issue: int, session: str, *, let_go: bool = False) -> str:
        status = self.gh.issue_status(issue)
        if not status.open and READY_LABEL in status.labels:
            self.gh.remove_label(issue, READY_LABEL)  # #154: a closed issue sheds ready-for-agent
        ended = _ended_outcome(status)
        if ended is not None:
            self._release_ended(status, ended, session)
            return ended
        if let_go:
            # #336: the session parked it (it left the claimed set) and a later claim is another
            # session's: no comment, no streak. #359: it still shows the assignee: unassign it.
            self._release_ended(status, "parked", session)
            return "parked"

        # Still assigned and `ready-for-agent`: the session ended without a closing or parking
        # checkpoint for THIS issue.
        boot = self._restart_boot_time(session)
        if boot is not None:
            # #782: the machine booted after the session started: the restart killed it. The issue
            # goes back to the pool; no comment, no streak, no core issue.
            self.gh.remove_assignee(issue)
            if session not in self._restart_logged:
                self._restart_logged.add(session)
                at = datetime.fromtimestamp(boot).strftime("%H:%M:%S")
                self.log(f"orch {session} ended (machine restart at {at})")
            return "machine_restart"
        if session in self._api_error:
            # #931: the session's transcript ends in an API error: an outage, not the ticket's fault.
            # Back to the pool; no comment, no streak, no core issue, the label stays.
            self.gh.remove_assignee(issue)
            return "outage"
        ids = {session} | {c.session_uuid for c in self.sessions if c.session == session and c.session_uuid}
        held_from, held_until = self._holding_window(issue, session)
        others = {n for c in self.sessions if c.session != session for n in (c.session, c.session_uuid) if n}
        others |= {v["session"] for v in self._parked_by.values() if v.get("session") and v["session"] not in ids}
        if (self.gh.last_comment_names(issue, tuple(sorted(i for i in ids if i)))
                or self.gh.own_checkpoint_parks(issue, tuple(sorted(i for i in ids if i)),
                                                held_from, held_until, tuple(sorted(others)))):
            # #852: or its newest own comment is a parking / closing checkpoint, whatever follows it.
            # #832: the session's own progress checkpoint is the issue's last comment: it waits on a
            # run/monitor/worker. Release it (label kept, no comment, no streak, no core issue, no ParkMark).
            self.gh.remove_assignee(issue)
            return "released"
        self.gh.comment(issue, f"ORCH session {session} ended without a closing or parking checkpoint")
        self.gh.remove_assignee(issue)
        streak = self._consecutive_no_checkpoint(issue) + 1
        if streak >= MAX_CONSECUTIVE_NO_CHECKPOINT:
            self.gh.remove_label(issue, READY_LABEL)
            self.gh.add_label(issue, NEEDS_INFO_LABEL)
            self.log(f"#{issue}: {streak} consecutive no-checkpoint endings; labelled {NEEDS_INFO_LABEL}")
        self._write_no_checkpoint_core_issue(issue, session)
        return "no_checkpoint"

    def _holding_window(self, issue: int, session: str) -> tuple[str, str]:
        """#852: (from, until) of the time the session held the issue: from the `claimed` event of
        the issue at or after the session's `orch_start` (else that start), until the session's end
        (`""` when unknown)."""

        return holding_window_from_events(self._read_events(), issue, session, self._end_at.get(session, ""))

    def _restart_boot_time(self, session: str) -> float | None:
        """#782: the boot time when it is later than the session's start, else None (also when
        either is unknown or the read fails)."""

        try:
            start = self._start_wall.get(session)
            boot = self.boot_time()
            if start is not None and boot is not None and float(boot) > start:
                return float(boot)
        except Exception:
            pass
        return None

    def _write_no_checkpoint_core_issue(self, issue: int, session: str) -> None:
        """The same defect at the home -- a session ended holding an issue with no closing
        or parking checkpoint. A failure here (any exception) is logged and never stops the loop;
        the comment/unassign/needs-info handling above has already run regardless."""

        if self.core_issue_writer is None:
            return
        try:
            manifest = frozenset(read_manifest(MANIFEST_PATH))
            log_text = _scrub_non_manifest_paths(
                "\n".join(self._recent_session_log_lines(session)), manifest)
            self.core_issue_writer(
                role="dispatcher", kind="broke",
                what=f"{ORCH_DOC}: Working an issue -- Checkpoint",
                what_human="сессия исполнителя закончилась, оставив задачу без итоговой или отложенной "
                           "отметки о ходе работы",
                error=f"ORCH session {session} ended holding #{issue} with no closing or parking "
                     f"checkpoint",
                log=log_text, did="parked", session=session,
                # #355: a `broke` needs caught-by; the dispatcher reads it at a later session's end
                caught_by="later-run",
            )
        except Exception as exc:
            self.log(f"core issue write failed ({exc!r})")

    def _recent_session_log_lines(self, session: str) -> list[str]:
        """Up to 10 of this dispatcher's own recent log/event lines about ``session``."""

        lines = []
        for event in self._read_events():
            if event.get("session") != session:
                continue
            fields = {k: v for k, v in event.items() if k != "session"}
            lines.append(json.dumps(fields, sort_keys=True))
        return lines[-10:]

    def _consecutive_no_checkpoint(self, issue: int) -> int:
        """How many times in a row this issue's last `orch_end` outcomes were `no_checkpoint`,
        read from the events log rather than kept in memory -- the dispatcher may restart."""

        count = 0
        for event in reversed(self._read_events()):
            if event.get("kind") != "orch_end" or event.get("issue") != issue:
                continue
            if event.get("outcome") in ("machine_restart", "released", "outage"):  # #782/#832/#931: neither counts nor breaks a streak
                continue
            if event.get("outcome") == "no_checkpoint":
                count += 1
            else:
                break
        return count

    # -- starting a session ------------------------------------------------------------------------

    def _adopted_claims(
        self, session: str, offered: tuple[int, ...]
    ) -> tuple[set[int], frozenset[int], float | None]:
        """What an adopted session holds, without taking another chat's claim for its own (the
        account is shared: an issue assigned before the session started is never held, #60).
        The state file this dispatcher wrote for the same session restores `held` and
        `preexisting` exactly; without it, every issue claimed now but not named in the session's
        name counts as someone else's (a later claim of the session is then not tracked: it keeps
        its own checkpoint, only the dispatcher's end-of-session read skips it).

        #229: the same state file's `started_at_wall` restores the session's own start time (in
        this process's monotonic clock), so an offered issue's wake grace (`WAKE_GRACE_SECONDS`)
        counts from the session's start, not from this restart's adoption. `None` when the state
        file does not match or predates this field -- the caller falls back to the live listing's
        `startedAt`."""

        entry = self._persisted_entry(session)
        if entry is not None and isinstance(entry.get("preexisting"), list):
            started_at = None
            wall = entry.get("started_at_wall")
            if isinstance(wall, (int, float)):
                started_at = self.clock() - (self.timestamp().timestamp() - float(wall))
            return ({int(n) for n in entry.get("held") or []},
                    frozenset(int(n) for n in entry["preexisting"]),
                    started_at)
        claims = self.gh.claimed_issues() or []
        return set(), frozenset(n for n in claims if n not in offered), None

    def _resolve_dead_sessions(self) -> None:
        """#442: once, on the first tick: every session the state file names that is not live now
        (a reboot or crash took it with the machine) has each issue it held resolved like an ended
        session's. An issue still assigned whose last comment is a checkpoint of that session is
        unassigned (no park mark: it must be offered again); one without such a checkpoint takes
        `_resolve_outcome`'s no-checkpoint path; a closed / parked / `needs-info` one is left as
        `_resolve_outcome` reads it. An issue a live session holds is never touched (it is adopted).
        Their claims are gone before `_maybe_start` reads `preexisting`, so they are offered."""

        if self._dead_sessions_resolved:
            return
        self._dead_sessions_resolved = True
        if self.state_file is None or not self.state_file.is_file() or self.dry_run:
            return
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
            listed = [s for s in state.get("sessions") or [] if isinstance(s, dict) and s.get("session")]
            live_ids = {str(s.get("id")) for s in self.claude.list_sessions()
                        if s.get("id") and s.get("state") in LIVE_STATES}
        except Exception as exc:
            self.log(f"dead-session resolution skipped ({exc!r})")
            return
        live_held: set[int] = set()
        for entry in listed:
            if str(entry["session"]) in live_ids:
                live_held |= {int(n) for n in entry.get("held") or []}
        for entry in listed:
            session = str(entry["session"])
            if session in live_ids or session in self.ended_sessions:
                continue
            self.ended_sessions.add(session)
            self._ended_held[session] = frozenset(int(n) for n in entry.get("held") or [])
            with contextlib.suppress(Exception):
                self._start_wall[session] = float(entry["started_at_wall"])
            for issue in sorted({int(n) for n in entry.get("held") or []} - live_held):
                try:
                    outcome = self._resolve_dead_issue(issue, session)
                except Exception as exc:  # one unreadable issue must not hide the others
                    self.log(f"#{issue}: could not read the issue back ({exc!r})")
                    outcome = "unreadable"
                self._append_event({
                    "ts": self.timestamp().isoformat(timespec="seconds"), "kind": "orch_end",
                    "issue": issue, "session": session, "outcome": outcome, "why": "dead_at_restart",
                })
                self.log(f"#{issue}: session {session} was not live after the restart: {outcome}")

    def _end_listed_ended_sessions(self) -> None:
        """#975: a session this dispatcher marked ended (`ended_sessions`: `_finish`, `_timeout`,
        `_resolve_dead_sessions`) whose process is still listed is ended through `end_session` -- never
        a raw kill -- once no issue it held is still open and assigned to it, no subagent runs and no
        watch is live. Only this repo's background ORCH sessions qualify (`_is_this_repo_orch`): an
        interactive chat, the autopilot (`autopilot #N`) and another repo's ORCH session are never
        touched, and neither is a session still working and busy (only an idle or done one ends). Each
        session is attempted once per dispatcher (`end_session` logs its own failure); never fatal.

        #975 unit 2: a listed idle background ORCH session of this repo that THIS process did not mark
        ended (a session an earlier dispatcher process parked; `ended_sessions` is empty after a restart)
        and that this process does not track either is also a candidate. Its held set is the `#N` in its
        name plus the `issue` of each `orch_end` event the events log holds for it (`_listed_held`); an
        empty held set is never ended. The same guards apply."""

        try:
            listed = self.claude.list_sessions()
        except Exception as exc:
            self.log(f"still-listed session check skipped ({exc!r})")
            return
        tracked = {c.session for c in self.sessions}
        for row in listed:
            session = str(row.get("id") or "")
            if not session or session in self._end_attempted:
                continue
            if row.get("kind") != "background" or not row.get("pid") or not self._is_this_repo_orch(row):
                continue
            if row.get("state") in LIVE_STATES and row.get("status") != "idle":
                continue  # still working and busy: not finished, whatever the dispatcher marked
            after_restart = session not in self.ended_sessions
            if after_restart:
                if session in tracked:
                    continue
                held = self._listed_held(row, session)
                if not held:
                    continue
            else:
                held = self._ended_held.get(session, frozenset())
            if self._held_still_open(held):
                continue
            session_uuid = str(row["sessionId"]) if row.get("sessionId") else None
            if running_subagent_ids(self.projects_dir, session_uuid, now=self.timestamp()) or self._session_watches(session, session_uuid) or self._live_shells(session_uuid):
                continue
            self._end_attempted.add(session)
            held_txt = ", ".join(f"#{n}" for n in sorted(held)) or "no issue"
            started = row.get("startedAt")
            age = None if not started else self.timestamp().timestamp() - float(started) / 1000.0
            age_txt = human_duration(max(0.0, age)) if age is not None else "unknown"
            if after_restart:
                self.log(f"orch {session} is still listed after a restart (parked by an earlier dispatcher): "
                         f"ending it (held {held_txt}, age {age_txt})")
            else:
                self.log(f"orch {session} was marked ended but is still listed: ending it "
                         f"(held {held_txt}, age {age_txt})")
            try:
                end_session(self.claude, session, log=self.log)
            except Exception as exc:  # a refused or failed end must never break the tick
                self.log(f"orch {session}: ending it failed ({exc!r})")

    def _is_this_repo_orch(self, row: dict[str, Any]) -> bool:
        """#975: a listing row is this repo's ORCH session: an `orch ...` name and this dispatcher's
        `cwd` (`session_matches_root`, the rule adoption uses). A row with no `cwd` is this repo's only
        when its name carries this repo's name (`orch <REPO_NAME> ...`)."""

        name = str(row.get("name") or "")
        if not _ORCH_NAME_RE.match(name) or not session_matches_root(row, self.claude.cwd):
            return False
        return row.get("cwd") is not None or name.startswith(f"orch {REPO_NAME}")

    def _listed_held(self, row: dict[str, Any], session: str) -> frozenset[int]:
        """#975 unit 2: the issues a listed session this process did not mark ended held: the `#N` in
        its name and the `issue` of every `orch_end` event the events log holds for that session. An
        unreadable events log leaves the name's numbers alone (never fatal)."""

        held = {int(n) for n in _ISSUE_RE.findall(str(row.get("name") or ""))}
        try:
            events = self._read_events()
        except OSError as exc:
            self.log(f"orch {session}: events log unreadable ({exc!r}), held taken from its name")
            events = []
        for event in events:
            if event.get("kind") == "orch_end" and event.get("session") == session and event.get("issue") is not None:
                held.add(int(event["issue"]))
        return frozenset(held)

    def _held_still_open(self, held: frozenset[int]) -> bool:
        """#975: whether one of `held` is still open and assigned (GitHub read now). An unreadable
        issue counts as still held: the session is not ended on a read that failed."""

        for issue in sorted(held):
            try:
                status = self.gh.issue_status(issue)
            except Exception:
                return True
            if status.open and status.assignees:
                return True
        return False

    def _resolve_dead_issue(self, issue: int, session: str) -> str:
        status = self.gh.issue_status(issue)
        if _ended_outcome(status) is None:
            body = self.gh.last_comment_body(issue) or ""
            if re.match(r"\W*Checkpoint\b", body) and session in body:
                self.gh.remove_assignee(issue)
                return "parked"
        return self._resolve_outcome(issue, session)

    def _adopt_live(self) -> list[str]:
        """Every live `orch ...` session this dispatcher does not yet track (a restart, #58; D110,
        #191: every one, not just the oldest -- each keeps its own held set and finishing mark).
        #208: a session whose `cwd` is another repo's root is never adopted -- `session_matches_root`
        is the one place this rule lives, shared with `find_own_session_id`."""

        tracked = {c.session for c in self.sessions}
        live = []
        for s in self.claude.list_sessions():
            name = str(s.get("name") or "")
            if (_ORCH_NAME_RE.match(name) and s.get("id") and s.get("kind", "background") == "background"
                    and s.get("state") in ORCH_ADOPT_STATES and str(s["id"]) not in self.ended_sessions
                    and str(s["id"]) not in tracked and session_matches_root(s, self.claude.cwd)):
                pid = int(s["pid"]) if s.get("pid") else None
                live.append((s.get("startedAt") or 0, str(s["id"]), name, s.get("sessionId"), pid))
        if not live:
            return []
        live.sort()
        adopted: list[str] = []
        for started_ms, session, name, session_uuid, claude_pid in live:
            age = 0.0
            if started_ms:
                age = max(0.0, self.timestamp().timestamp() - float(started_ms) / 1000.0)
            offered = tuple(int(n) for n in _ISSUE_RE.findall(name))
            entry = self._persisted_entry(session) or {}
            #: #50: the offers and wakes the previous dispatcher wrote down join the name's issues
            try:
                persisted = tuple(int(n) for n in entry.get("offered") or [])
                woken = {int(n) for n in entry.get("woken") or []}
            except (TypeError, ValueError):
                persisted, woken = (), set()
            offered = tuple(dict.fromkeys((*offered, *persisted)))
            held, preexisting, started_override = self._adopted_claims(session, offered)
            woken -= held
            held, preexisting, taken = self._drop_issues_held_elsewhere(held, preexisting)
            finishing = self._finishing_marked(session, str(session_uuid) if session_uuid else None)
            started_at = started_override if started_override is not None else self.clock() - age
            cur = _Current(session=session, started_at=started_at, offered=offered,
                           session_uuid=str(session_uuid) if session_uuid else None, name=name,
                           held=held, preexisting=preexisting, finishing=finishing,
                           claude_pid=claude_pid, woken=woken)
            self.sessions.append(cur)
            adopted.append(session)
            self._append_event({
                "ts": self.timestamp().isoformat(timespec="seconds"), "kind": "orch_adopt",
                "session": session, "name": name, "finishing": finishing,
            })
            self.log(f"adopted live {name} ({session}), running {age / 60.0:.0f} min"
                     + (" (finishing)" if finishing else ""))
            if taken:
                self._wake_returning_session(cur, taken)
        return adopted

    def _drop_issues_held_elsewhere(
        self, held: set[int], preexisting: frozenset[int]
    ) -> tuple[set[int], frozenset[int], dict[int, "_Current"]]:
        """#442 row 4: a session adopted from the state file (a "dead" one that turns up live)
        never takes back an issue a tracked session holds now. Those issues leave its `held` and
        join its `preexisting`, so `_refresh_claims` does not hand them back either. Returns the
        issue -> current holder map for the wake."""

        taken: dict[int, "_Current"] = {}
        # the state file may not match this session (it was rewritten without it): its own name
        # offers it the issues, so what is claimed now counts too, not only what the file held
        for n in sorted(held | set(self.gh.claimed_issues() or [])):
            holder = next((c for c in self.sessions if n in c.held), None)
            if holder is not None:
                taken[n] = holder
        return held - set(taken), preexisting | frozenset(taken), taken

    def _wake_returning_session(self, cur: "_Current", taken: dict[int, "_Current"]) -> None:
        """#442 row 4: one wake, at adoption (so never repeated), naming each issue and its holder."""

        lines = []
        for n, holder in taken.items():
            try:
                at = self.gh.last_assigned_at(n)
            except (RuntimeError, ValueError):
                at = None
            since = at.astimezone().strftime("%Y-%m-%d %H:%M:%S") if at is not None else "an unknown time"
            lines.append(f"#{n} is held by {holder.session} since {since}: leave it")
        target = cur.name or cur.session
        try:
            ok, detail = self.claude.wake(target, "\n".join(lines))
        except Exception as exc:  # a wake that raises is a wake that failed
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        self._append_event({
            "ts": self.timestamp().isoformat(timespec="seconds"), "kind": "orch_wake_taken",
            "issues": list(taken), "session": cur.session, "target": target, "ok": ok,
        })
        self.log(f"{target} came back holding " + ", ".join(f"#{n}" for n in taken)
                 + (": woken" if ok else f": wake failed ({detail})"))

    def _maybe_start(self, report: TickReport) -> None:
        """D110: a further session starts only when at least one issue is eligible and every live
        session is finishing (vacuously true with none live)."""

        self._core_update_reached = False
        self._take_pending_core_update()
        self._core_hold_step()
        self._maybe_start_inner(report)
        self._tell_core_hold_notice()
        job = self._core_update_job
        if not self._core_update_reached and job is not None and job.done.is_set():
            if job.error is not None:
                self.log(f"core update before orch start failed (start not taken this tick): {job.error!r}")
            self._core_update_job = None  # #34: a finished update no start used is stale: run again before the next start

    def _maybe_start_inner(self, report: TickReport) -> None:
        # #931 outage, live session not finishing, no candidates: a skipped tick leaves the hold's
        # count untouched (#1083); `_admission_go` restarts it when the last "wait" is older than the cap.
        if self.clock() < self._outage_until:  # #931: an API-error ending waits OUTAGE_WAIT_S
            return
        if self.sessions and not all(cur.finishing for cur in self.sessions):
            return
        # #4: the queue is oldest first; a session is offered at most `orch_offer_max` of it, the rest wait.
        candidates = self._eligible_candidates(offering=True)[:self.orch_offer_max]
        if not candidates:
            self._admission_released = False  # #152: no issue ready any more: the latch ends
            self._save_admission_hold()
            return
        offered = tuple(c.number for c in candidates)
        if self.dry_run:
            self.log(f"dry-run: would start orch offered {', '.join(f'#{n}' for n in offered)}")
            return
        if self._hold_start_for_core():
            return
        if not self._admission_go(offered, priority=any(priority_rank.priority_class(c.labels) for c in candidates)):
            return
        if not self._core_update_before_start():
            return
        preexisting = frozenset(self.gh.claimed_issues() or ())
        try:
            notes = self._parked_by_for(offered)
            kwargs: dict[str, Any] = {"parked_by": notes} if notes else {}
            session, cmd = self.claude.start(list(offered), **kwargs)
        except RuntimeError as exc:
            self._note_refused_start("orch", exc, offered, {c.number: c.labels for c in candidates})
            return
        name = cmd[3] if len(cmd) > 3 else ""  # `build_command`'s `--name` argument (#137's target)
        cur = _Current(session=session, started_at=self.clock(), offered=offered, name=name,
                       preexisting=preexisting)
        self.sessions.append(cur)
        self._admission_released = False  # #152: an ORCH actually started: the release is used up
        self._save_admission_hold()
        self._note_offer(cur, offered)
        self._append_event({
            "ts": self.timestamp().isoformat(timespec="seconds"), "kind": "orch_start",
            "issues": list(offered), "session": session,
        })
        self.log(f"started orch ({session}) offered {', '.join(f'#{n}' for n in offered)}: {' '.join(cmd)}")
        report.started = session
        report.offered = offered
        self._note_start_went_through("orch")

    def _take_pending_core_update(self) -> None:
        """#89: a refused home push left a mark in this target; the first tick with no ORCH takes the
        core update, through the same callable and thread as the before-start path, independent of the
        candidates and of the PC-busy gate. "Taken" is the callable's own message: `apply_auto_core_update`
        returns `core update (auto): ...` for a taken update (an already current target included) and
        `core update (auto) refused|failed ...` otherwise, so the mark goes only on the first form; a
        refusal or failure keeps it for the next tick. Never raises."""
        if self.core_root is None or self.core_update is None or self.dry_run:
            return
        from .core_pending import clear_hold, clear_mark, mark_waits, read_mark

        job = self._pending_core_job
        if job is None:
            mark = read_mark(self.core_root)
            if mark is None:
                clear_hold(self.core_root)  # #117: no mark, no hold
                self._core_attempt = None
                self._core_released_sha = None
                return
            if mark_waits(mark, self.timestamp().timestamp()):
                self._log_rate_wait(mark)  # #148: no attempt before the rate limit resets
                return
            if any(not cur.finishing for cur in self.sessions):  # #188: finishing sessions do not block
                return
            self._sync_state_file()  # #117: the gate reads this tick's sessions, not the previous tick's file
            running = self._core_update_job
            if running is not None and not running.done.is_set():
                return
            from .core_update import blocking_orch_live

            if blocking_orch_live(self.core_root):
                return
            job = self._pending_core_job = _CoreUpdateJob(self.pending_core_update)
        job.done.wait(self.core_update_wait_s)
        if not job.done.is_set():
            return  # still in the merge line: the next tick finds the job and waits on it again
        self._pending_core_job = None
        if job.error is not None:
            self._leave_rate_limit_mark(job.error)
            cause = repr(job.error)
            if cause not in self._pending_core_logged:
                self._pending_core_logged.add(cause)
                self.log(f"pending core update failed: {cause}")
            self._note_core_refusal(cause)
            return
        if job.result is None or (isinstance(job.result, str) and job.result.startswith("core update (auto): ")):
            # None: this repo's update_rule is not `auto`, so a mark means nothing here
            clear_mark(self.core_root)
            self._pending_core_logged.clear()
            self._core_attempt = None
            self._core_released_sha = None
        else:
            self._note_core_refusal(str(job.result))

    def _log_rate_wait(self, mark: dict) -> None:
        """#148: one log line per distinct `until` of a mark that waits for the GitHub rate limit."""
        until = float(mark["until"])
        if until in self._rate_wait_logged:
            return
        self._rate_wait_logged.add(until)
        stamp = time.strftime("%H:%M:%S", time.localtime(until))
        self.log(f"core update waits for the GitHub rate limit until {stamp}: no attempt before it")

    def _mark_waits(self) -> bool:
        """#148: True when this target's pending mark waits for the rate limit (its `until` is ahead)."""
        if self.core_root is None:
            return False
        from .core_pending import mark_waits, read_mark

        return mark_waits(read_mark(self.core_root), self.timestamp().timestamp())

    def _leave_rate_limit_mark(self, error: BaseException) -> None:
        """#148: an update that raised `CoreUpdateRateLimited` leaves the pending mark with the limit's reset time
        (the standing mark's sha, else the target's recorded core version). Never raises."""
        from .core_update import CoreUpdateRateLimited

        if isinstance(error, CoreUpdateRateLimited) and self.core_root is not None:
            _write_rate_limit_mark(Path(self.core_root), error.until)

    def _sync_state_file(self) -> None:
        """#117: when this dispatcher's state file is the target's `orch_session.json` (what `is_orch_live` and the
        core update's own guard read), rewrite it now, so a session that ended earlier this tick is not still listed."""
        from .core_update import ORCH_STATE_ENTRY

        if self.state_file is None or self.core_root is None:
            return
        if Path(self.state_file).resolve() == (Path(self.core_root) / ORCH_STATE_ENTRY).resolve():
            self._write_state()

    def _core_pending_holds(self) -> bool:
        """#188 rule 1: True while a pending core mark stands in an `auto` target and no non-waiting refusal has
        released it: no further ORCH starts and no issue is offered to a live one. No time cap ends it; what ends it
        is the update landing (the mark goes), a mark that waits for the rate limit (#148), or a refusal for any other
        cause than a working ORCH or the merge line (rule 3: starts go on, the mark stays). A target with no
        `core_update` or `core_root` (and the dry run) never holds. Reads only; never raises."""
        if self.core_root is None or self.core_update is None or self.dry_run:
            return False
        if _load_settings().get("update_rule") != "auto":
            return False
        from .core_pending import mark_waits, read_mark

        mark = read_mark(self.core_root)
        if mark is None or mark_waits(mark, self.timestamp().timestamp()):
            return False
        released = self._core_released_sha
        if released is None:  # #188 rule 3: a restart keeps the release the hold file recorded
            from .core_pending import read_hold

            released = (read_hold(self.core_root) or {}).get("released_sha")
        return released != str(mark.get("sha"))

    def _core_hold_step(self) -> bool:
        """#117, #188: the hold's bookkeeping for this tick; True when the start must wait for the core. The hold
        is kept in `artifacts/runs/core_hold.json` beside the mark, so a restart neither restarts its clock nor
        repeats its lines; it lives while the mark stands (a newer sha overwriting the mark is the same hold) and
        `clear_mark` removes it. First tick: writes the hold and logs one line. Past `core_hold_max_min` of it
        (machine-mode play time not counted): one notice to the brain chat per hold, naming the sessions that
        block and since when; the hold itself goes on. With no session live and no update in the merge line the
        start is not held unless the last attempt ended on a working ORCH or the merge line: the before-start
        update then takes the core first."""
        if not self._core_pending_holds():
            return False
        if not self.sessions and self._pending_core_job is None and self._core_attempt != "waits":
            return False
        from . import machine_mode
        from .core_pending import read_hold, read_mark, write_hold

        mark = read_mark(self.core_root) or {}
        sha8 = str(mark.get("sha") or "?")[:8]
        now = self.timestamp().timestamp()
        hold = read_hold(self.core_root)
        if hold is None or not isinstance(hold.get("since"), (int, float)):
            write_hold(self.core_root, {"since": now, "capped": False, "notice": None, "attempts": 0})
            self.log(f"core waits: orch starts and offers held ({len(self.sessions)} live, mark {sha8})")
            return True
        if hold.get("capped") or hold.get("ended"):
            return True
        waited = now - hold["since"] - machine_mode.stood_between(hold["since"], now)
        if waited < self._core_hold_cap_minutes() * 60:
            return True
        span = human_duration(waited)  # principle 7: the hold has no cap, so it may read "10 h 40 min"
        minutes = int(waited // 60)
        who = ", ".join(f"{cur.name or cur.session} ({'finishing' if cur.finishing else 'working'})"
                        for cur in self.sessions) or "the merge line"
        since = datetime.fromtimestamp(hold["since"]).strftime("%Y-%m-%d %H:%M")
        hold.update(capped=True, minutes=minutes, sha8=sha8)
        if not hold.get("noticed"):  # #188: at most one brain notice per hold, whichever comes first
            hold.update(noticed=True, notice=f"core waits: held {span} (since {since}), no ORCH start or offer "
                                             f"until the core update lands (mark {sha8}); blocked by {who}")
        write_hold(self.core_root, hold)
        self.log(f"core waits: held {span}, the core update is still pending ({who})")
        return True

    def _hold_start_for_core(self) -> bool:
        """#117: True when this start must wait for the core (`_core_hold_step`)."""
        return self._core_hold_step()

    def _note_core_refusal(self, text: str) -> None:
        """#188: an update attempt that ended without taking the core. A working ORCH or a busy merge line keeps
        the hold (the next tick tries again); any other cause (rule 3: uncommitted core paths, a failed update, the
        rate limit) releases it for this mark, with one notice if something had been held. Never raises."""
        if self.core_root is None:
            return
        if any(cause in text for cause in _CORE_WAIT_CAUSES):
            self._core_attempt = "waits"
            return
        self._core_attempt = "released"
        from .core_pending import mark_waits, read_hold, read_mark, write_hold

        mark = read_mark(self.core_root)
        if mark is None or mark_waits(mark, self.timestamp().timestamp()):
            return  # no mark, or the rate limit's own wait (#148): nothing to release
        self._core_released_sha = str(mark.get("sha"))
        cause = text.split("refused: ", 1)[-1][:160]
        hold = read_hold(self.core_root)
        if hold is not None and hold.get("released_sha") == self._core_released_sha:
            return  # already recorded and told for this mark: one line, one notice
        held = hold is not None  # a hold file means something was held; without one the release is silent
        hold = dict(hold) if held else {"since": self.timestamp().timestamp(), "capped": False, "notice": None,
                                        "attempts": 0}
        hold["released_sha"] = self._core_released_sha  # a restart reads the release back from here
        if held and not hold.get("noticed"):  # at most one brain notice per hold
            hold.update(noticed=True, notice=f"core waits: update refused ({cause}); ORCH starts and offers go on "
                                             f"with the old core (mark {str(mark.get('sha') or '?')[:8]})")
        hold["released"] = True
        write_hold(self.core_root, hold)
        self.log(f"core waits: hold released, the update was refused for another cause ({cause})")

    @staticmethod
    def _core_hold_cap_minutes() -> float:
        value = _load_settings().get("core_hold_max_min")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
        return float(_SETTING_DEFAULTS["core_hold_max_min"])

    def _tell_core_hold_notice(self) -> None:
        """#117, #188: the hold file's one pending notice (`notice`: the cap moment, or a release) goes to the brain
        chat through the notifier's own seams (`_brain_is_live`, `_send_to_brain`) because `BrainNotifier.observe`
        takes issue numbers with a parked or closed word and builds its text from the issue; it cannot carry a
        one-off text. A chat not live waits; a failed wake spends one attempt and, at `MAX_ATTEMPTS`, drops the
        notice. Never raises."""
        if self.core_root is None or self.dry_run:
            return
        from .core_pending import read_hold, write_hold

        hold = read_hold(self.core_root)
        if hold is None or not hold.get("notice"):
            return
        try:
            target = self._targets_of(())[0]
            if not self._brain_is_live(target, "core hold notice"):
                return
            self._send_to_brain(target, str(hold["notice"]))
            hold["notice"] = None
        except Exception as exc:  # noqa: BLE001 -- a notice never stops a tick
            hold["attempts"] = int(hold.get("attempts") or 0) + 1
            dropped = hold["attempts"] >= MAX_ATTEMPTS
            if dropped:
                hold["notice"] = None
            self.log(f"core hold notice: send failed ({type(exc).__name__}: {exc}); "
                     f"{'dropped' if dropped else 'kept for one retry'}")
        write_hold(self.core_root, hold)

    # -- the merge's brain lines (#241) -----------------------------------------------------------

    def _load_brain_lines_state(self) -> None:
        """#241: the offset and attempts the state file holds (`brain_lines`). No file or no such key: from the start
        (the lines already in the file are told once); an unreadable file: one log line, from the start."""
        if self.state_file is None or not self.state_file.is_file():
            return
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
            row = state.get("brain_lines") if isinstance(state, dict) else None
            if isinstance(row, dict):
                self._brain_lines = {"offset": max(0, int(row.get("offset") or 0)),
                                       "attempts": max(0, int(row.get("attempts") or 0))}
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            self.log(f"brain lines: state not read from {self.state_file.name} ({type(exc).__name__}); "
                     f"reading from the start")

    def _tell_brain_lines(self) -> None:
        """#241: the lines the merge wrote to `brain_lines.log` (`merge_github.append_brain_line`) go to this repo's brain
        chat (`brain_target` with no area label), each once, several in one message, one line each as written. Only
        whole lines are read: a torn last line waits for its newline. A chat not live waits with nothing spent; a failed
        wake spends one attempt and at `MAX_ATTEMPTS` the batch is dropped with one log line naming its count. An
        unreadable line is skipped with one log line when the offset passes it. Never raises; `--dry-run` sends nothing."""
        if self._brain_lines_file is None or self.dry_run:
            return
        path = self._brain_lines_file
        try:
            size = path.stat().st_size if path.is_file() else 0
            offset = self._brain_lines["offset"]
            if size < offset:
                self.log(f"brain lines: {path.name} is shorter than the offset told ({size} < {offset} bytes); "
                         f"reading it from its start")
                self._brain_lines = {"offset": 0, "attempts": 0}
                offset = 0
            if size <= offset:
                return
            with path.open("rb") as fh:
                fh.seek(offset)
                data = fh.read()
            cut = data.rfind(b"\n")
            if cut < 0:
                return  # a torn line only: not read until it is whole
            chunk = data[:cut + 1]
            end = offset + len(chunk)
            texts: list[str] = []
            bad: list[str] = []
            for raw in chunk.split(b"\n")[:-1]:
                try:
                    row = json.loads(raw.decode("utf-8"))
                    if not isinstance(row, dict) or not isinstance(row.get("line"), str):
                        raise ValueError("no line field")
                except (UnicodeDecodeError, ValueError) as exc:  # json.JSONDecodeError is a ValueError
                    bad.append(f"{type(exc).__name__}: {exc}")
                    continue
                texts.append(row["line"])
            if not texts:
                self._commit_brain_lines(end, bad)
                return
            target = self._targets_of(())[0]
            if not self._brain_is_live(target, "brain lines"):
                return  # the lines wait; nothing is spent
            try:
                self._send_brain_lines(target, texts)
            except Exception as exc:  # noqa: BLE001 -- a failed wake spends one attempt, never stops the tick
                self._brain_lines["attempts"] = int(self._brain_lines.get("attempts") or 0) + 1
                if self._brain_lines["attempts"] >= MAX_ATTEMPTS:
                    self.log(f"brain lines: send to {target} failed ({type(exc).__name__}: {exc}); "
                             f"{len(texts)} line(s) dropped")
                    self._commit_brain_lines(end, bad)
                    return
                self.log(f"brain lines: send to {target} failed ({type(exc).__name__}: {exc}); "
                         f"{len(texts)} line(s) kept for one retry")
                self._write_state()
                return
            self._commit_brain_lines(end, bad)
        except Exception as exc:  # noqa: BLE001 -- a notice never stops a tick
            self.log(f"brain lines: not told ({type(exc).__name__}: {exc})")

    def _commit_brain_lines(self, offset: int, bad: list[str]) -> None:
        """#241: the offset moves to `offset` (the lines up to it are told or dropped), the attempts start over, each
        unreadable line is named once, and the state file is written at once (a restart tells nothing twice)."""
        for reason in bad:
            self.log(f"brain lines: an unreadable line of {self._brain_lines_file.name} skipped ({reason})")
        self._brain_lines = {"offset": offset, "attempts": 0}
        self._write_state()

    def _send_brain_lines(self, title: str, texts: list[str]) -> None:
        """One wake of the brain chat with its lines, one per line; raises when the wake fails (the batch is kept)."""
        woke = self._brain_wake_name(title)
        ok, detail = self.claude.wake(woke, "\n".join(texts))
        if not ok:
            raise RuntimeError(f"wake of {woke} failed: {detail}")
        self.log(f"told {woke}: {len(texts)} brain line(s) from {self._brain_lines_file.name}")

    def _core_update_before_start(self) -> bool:
        """#1115: the auto core update runs before every ORCH start, so the session reads the updated
        core files. Its own refusal or failure is a log line (`apply_auto_core_update` prints it); an
        exception that escapes it is logged here and never blocks the start.

        #34: the update runs in a thread, because its commit waits in the merge line (FIFO, no
        ceiling while the holder makes progress). The tick waits `core_update_wait_s` for it; True
        when it finished (the start goes on), False when it is still running: the start is deferred
        to a later tick with one log line naming the wait and the holder pid."""
        self._core_update_reached = True
        if self.core_update is None:
            return True
        job = self._core_update_job
        if job is None:
            if self._mark_waits():
                return True  # #148: no attempt before the rate limit resets; the start goes on
            job = self._core_update_job = _CoreUpdateJob(self.core_update)
        job.done.wait(self.core_update_wait_s)
        if not job.done.is_set():
            if not job.wait_logged:
                job.wait_logged = True
                pid = self._merge_line_holder_pid()
                what = (f"waiting for the merge line (holder pid {pid})" if pid is not None
                        else "waiting for the merge line (holder pid unknown, queued behind others)")
                self.log(f"core update before orch start {what}: orch start deferred, "
                         f"the tick goes on and retries")
            return False
        self._core_update_job = None
        if job.error is not None:
            self._leave_rate_limit_mark(job.error)
            self.log(f"core update before orch start failed: {job.error!r}")
            self._note_core_refusal(repr(job.error))
        elif isinstance(job.result, str) and not job.result.startswith("core update (auto): "):
            self._note_core_refusal(job.result)
            if self._core_attempt == "waits" and self._core_pending_holds():
                return False  # #188 rule 1: a working ORCH or the merge line still blocks the core: no start
        return True

    def _merge_line_holder_pid(self) -> int | None:
        """The pid named by the merge lock's holder file, or None (no holder, unreadable)."""
        try:
            from .merge import MergeLock
            holder = MergeLock(self.merge_lock_path)._read_holder()
            pid = holder.get("pid") if isinstance(holder, dict) else None
            return int(pid) if pid is not None else None
        except Exception:  # noqa: BLE001 - a log detail never breaks the tick
            return None

    def _load_admission_hold(self) -> None:
        """#175: rebuild the hold's count and latch from the file, each wall-clock time mapped to this
        dispatcher's own clock. A missing file is silent; an unreadable or corrupt one, or a time in the future,
        gives count 0 and no latch, with one log line naming the reason."""
        path = self.admission_hold_file
        if path is None:
            return
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:  # ValueError: bytes that are not UTF-8
            self.log(f"admission hold: {path.name} unreadable ({exc!r}); starting with no count")
            return
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not an object")
            now_wall = self.timestamp()
            now = self.clock()
            mapped: dict[str, float | None] = {}
            for key in ("wait_since", "last_wait_at"):
                if data.get(key) is None:
                    mapped[key] = None
                    continue
                wall = datetime.fromisoformat(data[key])
                if wall.tzinfo is None:
                    wall = wall.replace(tzinfo=timezone.utc)
                if wall > now_wall:
                    raise ValueError(f"{key} is in the future")
                mapped[key] = now - (now_wall - wall).total_seconds()
            released = data.get("released", False)
            if not isinstance(released, bool):
                raise ValueError("released is not a boolean")
            if (mapped["wait_since"] is None) != (mapped["last_wait_at"] is None):
                raise ValueError("wait_since and last_wait_at do not go together")
        except (ValueError, TypeError) as exc:
            self.log(f"admission hold: {path.name} unusable ({exc}); starting with no count")
            return
        self._admission_wait_since = mapped["wait_since"]
        self._admission_last_wait_at = mapped["last_wait_at"]
        self._admission_released = released
        self._admission_saved = (self._admission_wait_since, self._admission_last_wait_at, released)

    def _save_admission_hold(self) -> None:
        """#175: write the three hold values (wall-clock UTC) when they changed; a write error is logged, never raised."""
        path = self.admission_hold_file
        if path is None:
            return
        state = (self._admission_wait_since, self._admission_last_wait_at, self._admission_released)
        if state == self._admission_saved:
            return
        try:
            now_wall = self.timestamp()
            now = self.clock()

            def wall(value: float | None) -> str | None:
                return None if value is None else (now_wall - timedelta(seconds=now - value)).isoformat()

            payload = {"wait_since": wall(state[0]), "last_wait_at": wall(state[1]), "released": state[2]}
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
            os.replace(tmp, path)
            self._admission_saved = state
        except Exception as exc:  # noqa: BLE001 - a state file never breaks the tick
            self.log(f"admission hold: could not write {path.name} ({exc!r})")

    def _admission_go(self, offered: Iterable[int] = (), *, priority: bool = False) -> bool:
        """#898: False while the PC is too busy for a new ORCH; one log line per 5 min. Any failure
        of the check is a go. The held line names the `offered` issues the hold keeps back.
        #122: an offer holding a Priority ticket (`priority` True) is a go without the hook, and counts
        as any go (#907): the hold's count restarts.
        #152: while `_admission_released` is latched (a hold-cap go no ORCH start has used yet) it is a go at
        once, with no admission call and no `start held` line."""
        if self._admission_released:
            return True
        if priority:
            self._admission_wait = False
            self._admission_logged = None
            self._admission_wait_since = None
            self._admission_last_wait_at = None
            self._save_admission_hold()
            return True
        now = self.clock()
        try:
            cap = _load_settings().get("start_admission_hold_cap_s")
            cap = float(cap) if cap is not None else None
            # #1083: the count survives skipped ticks; it restarts at 0 only when the last "wait" is older
            # than the cap (a hold from an old day never counts, #971).
            if (cap is not None and self._admission_last_wait_at is not None
                    and now - self._admission_last_wait_at > cap):
                self._admission_wait_since = None
            waited = 0.0 if self._admission_wait_since is None else max(0.0, now - self._admission_wait_since)
            import inspect

            if "waited_s" in inspect.signature(self.admission).parameters:
                adm = self.admission(self._admission_wait, waited_s=waited, hold_cap_s=cap)
            else:
                adm = self.admission(self._admission_wait)
            go = bool(adm.go)
        except Exception:
            self._admission_wait_since = None  # #971: a check that fails is a go, so no count is left
            self._admission_last_wait_at = None
            self._save_admission_hold()
            return True
        self._admission_wait = not go
        if go:
            self._admission_logged = None
            self._admission_wait_since = None  # #907: any go restarts the count at 0
            self._admission_last_wait_at = None
            if getattr(adm, "reason", None) == "hold cap":
                self._admission_released = True  # #152: latched until an ORCH starts
                self.log(f"orch-dispatch: start by hold cap, {adm.line()}")
            self._save_admission_hold()
            return True
        if self._admission_wait_since is None:
            self._admission_wait_since = now
        self._admission_last_wait_at = now  # #1083: the last "wait" answer, for the age check above
        self._save_admission_hold()
        if self._admission_logged is None or now - self._admission_logged >= 300:
            self._admission_logged = now
            self.log(f"orch-dispatch: start held, {adm.line()}{_issues_tag(offered)}")
        return False

    def _note_refused_start(self, key: str, exc: Exception, numbers: Iterable[int] = (),
                            labels: Mapping[int, Sequence[str]] | None = None) -> None:
        """#298: a refused start is reported once (log + the brain's chat), stays quiet while the
        same text repeats (one reminder per `START_REFUSED_REMIND_SECONDS`), and a new text is a new
        refusal. The start itself is still tried every tick by the caller. #1109: the report goes once to
        each brain chat of `numbers` (the issues the start concerns; their labels in `labels` when known)."""

        text = str(exc)
        now = self.clock()
        cur = self.refused_starts.get(key)
        if cur is not None and cur.text == text:
            cur.ticks += 1
            if (now - cur.last_line_at) >= START_REFUSED_REMIND_SECONDS:
                cur.last_line_at = now
                self.log(f"{key} start still refused after {cur.ticks} ticks, "
                         f"{human_duration(now - cur.first_at)} since the first")
            return
        self.refused_starts[key] = _RefusedStart(text=text, first_at=now, last_line_at=now)
        target = _load_settings()["brain_chat_title"]
        sent = [t for t in self.refused_messages.get(key, [])
                if (now - t) < START_REFUSED_CAP_WINDOW_SECONDS]
        self.refused_messages[key] = sent
        if len(sent) >= START_REFUSED_CAP_MESSAGES:  # #299: the cap is per start, whatever the text
            self.log(f"{key} start refused ({text}); retrying every tick quietly")
            if key not in self.refused_cap_told:
                self.refused_cap_told.add(key)
                self.log(f"{key} start: {START_REFUSED_CAP_MESSAGES} messages to {target} in "
                         f"{human_duration(START_REFUSED_CAP_WINDOW_SECONDS)}; further refusals "
                         f"are kept in this log only")
            return
        sent.append(now)
        self.refused_cap_told.discard(key)
        targets = self._targets_of(numbers, labels)
        self.log(f"{key} start refused ({text}); telling {', '.join(targets)}, retrying every tick quietly")
        message = (f"The dispatcher's `{key}` start is refused by `claude --bg` in {self.claude.cwd}: "
                   f"{text} This step is the owner's own: relay it to him, never do it yourself.")
        for target in targets:
            try:
                target = self._brain_wake_name(target)  # #69: the chat the lookup resolves
                ok, detail = self.claude.wake(target, message)
            except Exception as exc:  # #299: a wake that raises is a wake that failed
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            self._append_event({
                "ts": self.timestamp().isoformat(timespec="seconds"), "kind": "start_refused",
                "start": key, "text": text, "target": target, "ok": ok,
            })
            if not ok:
                self.log(f"could not tell {target} that the {key} start is refused: {detail}")

    def _note_start_went_through(self, key: str) -> None:
        cur = self.refused_starts.pop(key, None)
        if cur is not None:
            self.log(f"{key} start refusal is over after {human_duration(self.clock() - cur.first_at)} "
                     f"({cur.ticks} ticks)")

    # -- the state file (#52) and events ----------------------------------------------------------

    def _record_holders(self) -> None:
        """#50: every live session is on record as a holder of each issue it holds, was offered or was
        woken about, with its `claude.exe` pid (kept when the pid is not known yet)."""

        for cur in self.sessions:
            for n in cur.held | set(cur.offered) | cur.woken:
                held = self._holders.setdefault(n, {})
                if cur.claude_pid is not None or cur.session not in held:
                    held[cur.session] = cur.claude_pid

    def _load_holders(self) -> None:
        """#50: once, before the first write: the holders the previous dispatcher left in the state
        file (`holders`, and each session entry's `held` / `offered` / `woken` with its `pid`)."""

        if self._holders_loaded:
            return
        self._holders_loaded = True
        if self.state_file is None or not self.state_file.is_file():
            return
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
            for entry in state.get("sessions") or []:
                pid = entry.get("pid")
                for key in ("held", "offered", "woken"):
                    for n in entry.get(key) or []:
                        self._holders.setdefault(int(n), {})[str(entry["session"])] = (
                            int(pid) if pid else None)
            for n, who in (state.get("holders") or {}).items():
                for session, pid in who.items():
                    self._holders.setdefault(int(n), {})[str(session)] = int(pid) if pid else None
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            self.log(f"holders not read from {self.state_file.name} ({exc!r})")

    def _persisted_entry(self, session: str) -> dict[str, Any] | None:
        """#50: the state file's entry for `session` (the previous dispatcher's), or `None`."""

        if self.state_file is None or not self.state_file.is_file():
            return None
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        listed = state.get("sessions")
        if isinstance(listed, list):
            # #650: every live session has its own entry; the top-level keys carry nobody once two
            # sessions are not finishing
            return next((e for e in listed if isinstance(e, dict) and e.get("session") == session), None)
        return state if state.get("session") == session else None

    def _write_state(self) -> None:
        """`artifacts/runs/orch_session.json`: every live session and the issues it holds, for
        whoever shows them under the ORCH row. D110, #191: the old top-level keys still mirror the
        one non-finishing session (`None` when there is none, e.g. every live session is finishing,
        or several are not -- never a wrong one), so a reader written for one session at a time keeps
        working; `sessions` is the full list, each with its own `finishing` flag. Rewritten every
        tick; never fatal."""

        if self.state_file is None:
            return
        self._load_holders()
        self._record_holders()
        non_finishing = next((c for c in self.sessions if not c.finishing), None) \
            if sum(1 for c in self.sessions if not c.finishing) == 1 else None
        data: dict[str, Any] = {"updated": self.timestamp().isoformat(timespec="seconds"), "session": None}
        if non_finishing is not None:
            cur = non_finishing
            data.update({
                "session": cur.session, "session_uuid": cur.session_uuid, "offered": list(cur.offered),
                "held": sorted(cur.held), "preexisting": sorted(cur.preexisting),
                "claimed_now": sorted(cur.claimed_now) if cur.claimed_now is not None else None,
                "started_at_wall": self.timestamp().timestamp() - (self.clock() - cur.started_at),
            })
        data["parked_by"] = {str(n): v for n, v in sorted(self._parked_by.items())}
        data["holders"] = {str(n): dict(v) for n, v in sorted(self._holders.items())}
        if self._brain_lines_file is not None:  # #241: the brain lines' offset survives a restart
            data["brain_lines"] = dict(self._brain_lines)
        data["sessions"] = [
            {
                "session": cur.session, "session_uuid": cur.session_uuid, "offered": list(cur.offered),
                "woken": sorted(cur.woken), "pid": cur.claude_pid,
                "held": sorted(cur.held), "preexisting": sorted(cur.preexisting),
                "claimed_now": sorted(cur.claimed_now) if cur.claimed_now is not None else None,
                "finishing": cur.finishing,
                "started_at_wall": self.timestamp().timestamp() - (self.clock() - cur.started_at),
            }
            for cur in self.sessions
        ]
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self.state_file)
        except Exception as exc:
            self.log(f"could not write {self.state_file.name} ({exc!r})")

    def _read_events(self) -> list[dict[str, Any]]:
        if not self.events_file.is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in self.events_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def _append_event(self, event: dict[str, Any]) -> None:
        try:
            append_jsonl(self.events_file, event)
        except Exception as exc:  # a logging failure must never break the loop
            self.log(f"could not append an event ({exc!r}): {event}")

    # -- the loop ---------------------------------------------------------------------------------

    def _note_failed_tick(self, exc: BaseException) -> None:
        """#611 rule 2: ticks failing in a row for `FAILED_TICKS_ALERT_SECONDS` write one core issue
        (the `gh`-based writer: the brain's wake needs claude.exe, the very thing that may be lost),
        and not again until a tick succeeds."""

        now = self.clock()
        if self._failed_since is None:
            self._failed_since = now
        if self._failed_alerted or (now - self._failed_since) < FAILED_TICKS_ALERT_SECONDS:
            return
        self._failed_alerted = True
        if self.core_issue_writer is None:
            return
        try:
            self.core_issue_writer(
                role="dispatcher", kind="broke",
                what=f"{ORCH_DOC}: every dispatcher tick fails",
                what_human="диспетчер подряд не может выполнять свою работу: каждая попытка падает",
                error=f"ticks have failed in a row for {human_duration(now - self._failed_since)}; "
                      f"last: {exc!r}{_innermost_frame(exc)}",
                log="", did="kept retrying every tick", session="-", caught_by="later-run")
        except Exception as write_exc:
            self.log(f"core issue write failed ({write_exc!r})")

    def _code_fingerprint_now(self) -> str | None:
        """#141: the current fingerprint, `None` (logged) when the function itself fails."""
        try:
            return self.code_fingerprint()
        except Exception as exc:
            self.log(f"fingerprint failed ({exc!r}{_innermost_frame(exc)}); continuing")
            return None

    def run(self, *, ticks: int | None = None, interval: float = TICK_SECONDS,
           sleep: Callable[[float], None] = time.sleep) -> int:
        done = 0
        start_fp = self.start_fingerprint
        if start_fp is None and self.code_fingerprint is not None:
            start_fp = self._code_fingerprint_now()
        while ticks is None or done < ticks:
            try:
                self.tick()
                self._failed_since = None
                self._failed_alerted = False
            except Exception as exc:  # one bad tick must not end the dispatcher
                self.log(f"tick failed ({exc!r}{_innermost_frame(exc)}); continuing")
                self._note_failed_tick(exc)
            done += 1
            if self.code_fingerprint is not None:
                # #141: between a tick and the sleep, never inside one: the keepalive restarts us
                now_fp = self._code_fingerprint_now()
                if now_fp is not None:
                    if start_fp is None:
                        start_fp = now_fp  # no baseline yet (its start check failed): take this one
                    elif now_fp != start_fp:
                        self.log(f"code changed since start ({start_fp[:8]} -> {now_fp[:8]}): "
                                 f"exiting for a restart")
                        return 0
            if ticks is not None and done >= ticks:
                break
            sleep(max(0.0, min(interval, TICK_SECONDS)))
        return 0


# -- CLI ------------------------------------------------------------------------------------------

def apply_auto_core_update(root: Path = ROOT, *, settings: dict | None = None, gh_runner=None,
                           lock_wait_s: float | None = CORE_UPDATE_WAIT_S) -> str | None:
    """Issue #153: `update_rule == "auto"` takes the core's latest change once, before the first
    tick -- every change runs here first. One log line either way; never fatal to the dispatcher
    (a home-read failure is reported, not a crash of the loop). #1115: the dispatcher calls it again
    before every ORCH start; returns the log line it printed, or None when the rule is not `auto`.
    #49: the update takes the target's merge lock before it writes; `lock_wait_s` bounds that wait and a
    wait that ran out is the refusal `merge running` (0 files written)."""

    from .core_pending import mark_waits, read_mark
    from .core_update import CoreUpdateError, CoreUpdateRateLimited, run_core_update

    data = settings if settings is not None else _load_settings()
    if data.get("update_rule") != "auto":
        return None
    mark = read_mark(root)
    if mark_waits(mark, time.time()):  # #148: no attempt before the GitHub rate limit resets
        stamp = time.strftime("%H:%M:%S", time.localtime(float(mark["until"])))
        message = f"core update (auto) waits for the GitHub rate limit until {stamp}: no attempt"
        print(message)
        return message
    try:
        result = run_core_update(root, gh_runner=gh_runner, lock_wait_s=lock_wait_s)
        message = f"core update (auto): {result.message}"
    except CoreUpdateRateLimited as exc:  # #148: the mark carries the reset, no retry runs before it
        _write_rate_limit_mark(root, exc.until, data)
        message = f"core update (auto) refused: {exc}"
    except CoreUpdateError as exc:
        message = f"core update (auto) refused: {exc}"
    except Exception as exc:  # a home-read failure must not block the dispatcher's own work
        message = f"core update (auto) failed: {exc!r}"
    print(message)
    events_file = root / "artifacts" / "runs" / "events.jsonl"
    try:
        append_jsonl(events_file, {"ts": _now_str(), "kind": "core_update_auto", "message": message})
    except Exception:
        pass
    return message


def apply_auto_core_update_in_line(root: Path = ROOT, *, settings: dict | None = None, gh_runner=None) -> str | None:
    """#188 rule 5: the pending-mark retry's form of `apply_auto_core_update` (the before-start update stays bounded). The update is one more waiter in the merge
    line: its ticket stands until its turn comes (after the holder at the time it joined, ahead of the merges queued
    later) and the wait ends only at the lock's own age ceiling (`merge_lock_max_age_min`), not after
    `CORE_UPDATE_WAIT_S`. The job thread keeps that one attempt across ticks, so the tick never blocks on it."""

    return apply_auto_core_update(root, settings=settings, gh_runner=gh_runner, lock_wait_s=None)


def _write_rate_limit_mark(root: Path, until: float | None, settings: dict | None = None) -> None:
    """#148: the pending mark for an update that failed on the GitHub rate limit: the standing mark's sha, else the
    target's recorded `core_version` (the home head cannot be read under the limit), with `until`, the limit's reset
    time. Never raises: a mark that cannot be written changes nothing."""
    try:
        from .core_pending import read_mark, write_mark

        sha = (read_mark(root) or {}).get("sha")
        if not sha:
            data = settings
            if data is None:
                try:
                    data = json.loads((root / "config" / "orchestration.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    data = {}
            sha = data.get("core_version") or "unknown"
        write_mark(root, str(sha), until=until)
    except Exception:  # noqa: BLE001 -- a mark never changes the update's line (#148)
        pass


#: #188: the causes of a refused core update that the dispatcher waits out (a working ORCH, a merge in the line)
_CORE_WAIT_CAUSES = ("an ORCH session is live", "merge running")


class _CoreUpdateJob:
    """#34: one run of the auto core update in a daemon thread. `done` is set when it ended; `error`
    is the exception that escaped it (the tick logs it, on its own thread)."""

    def __init__(self, fn: Callable[[], Any]) -> None:
        import threading

        self.done = threading.Event()
        self.error: BaseException | None = None
        self.wait_logged = False
        self.result: Any = None
        self._fn = fn
        threading.Thread(target=self._run, name="core-update", daemon=True).start()

    def _run(self) -> None:
        try:
            self.result = self._fn()
        except BaseException as exc:  # noqa: BLE001
            self.error = exc
        finally:
            self.done.set()


class DispatcherLockHeld(RuntimeError):
    """Another dispatcher process already holds `LOCK_PATH` (or the lock path given)."""

    def __init__(self, pid: int | None, started: str | None = None):
        self.pid = pid
        self.started = started
        super().__init__(f"dispatcher already live (pid {pid}, started {started})")


def dispatcher_status(lock_path: Path = LOCK_PATH) -> tuple[bool, int | None]:
    """``(alive, pid)`` for whoever holds ``lock_path``. A lock left by a process that is gone
    (crash, reboot, `kill`) reads as not alive -- the same "exited holder" reading the merge lock
    uses (`merge.py`'s `MergeLock`, issue #195), through the same `pid_alive` (issue #201: no
    second process-alive check written here). A pid alive but reused by a later, unrelated
    process (issue #235) also reads as not alive: `holder_is_live` checks the pid's own
    creation time against the lock's `started`."""
    holder = read_pid_lock(lock_path)
    pid = holder.get("pid") if holder else None
    started = holder.get("started") if holder else None
    return (holder_is_live(pid, started), pid)


def _head_commit() -> str:
    """#905: short sha of the repo's HEAD (8 chars), `""` when git fails."""
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short=8", "HEAD"],
                             capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def code_fingerprint(root: Path = ROOT) -> str:
    """#141: sha256 hex over the sorted relative paths and bytes of `tools/agentops/**/*.py` under
    `root` (`__pycache__` skipped). Not the git HEAD: a commit that touches no code keeps it."""
    base = Path(root) / "tools" / "agentops"
    files = sorted(
        ((path.relative_to(root).as_posix(), path) for path in base.rglob("*.py")
         if "__pycache__" not in path.relative_to(base).parts),
        key=lambda item: item[0])
    h = hashlib.sha256()
    for rel, path in files:
        h.update(rel.encode("utf-8") + b"\0")
        h.update(path.read_bytes() + b"\0")
    return h.hexdigest()


@contextlib.contextmanager
def dispatcher_lock(lock_path: Path = LOCK_PATH):
    """One dispatcher at a time (issue #201). Mirrors `merge.py`'s `MergeLock.acquire`: a holder
    whose pid is gone is taken over at once (the stale file removed, creation retried immediately,
    no sleep); a live holder raises `DispatcherLockHeld` at once -- the dispatcher never waits for
    another one to end, it just refuses to start beside it. Released on the way out, whatever the
    loop inside does (including an exception)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, json.dumps({"pid": os.getpid(), "started": _now_str(),
                                         "commit": _head_commit()}).encode())
            finally:
                os.close(fd)
            break
        except FileExistsError:
            holder = read_pid_lock(lock_path)
            pid = holder.get("pid") if holder else None
            started = holder.get("started") if holder else None
            if holder_is_live(pid, started):
                raise DispatcherLockHeld(pid, started)
            _log(f"stale lock of pid {pid} taken")
            try:
                lock_path.unlink()
            except OSError:
                pass
            continue  # a dead holder is taken over at once, no wait
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


def _wmi_launch_ps1_text(command_line: str, cwd: str) -> str:
    """The `.ps1` text for one windowless WMI `Win32_Process.Create`, the exact mechanism
    `scripts/launch_hidden.ps1` uses (`Win32_ProcessStartup.ShowWindow = 0`). Prints
    `rc=<ReturnValue> pid=<ProcessId>` on its own stdout line, the only output
    `launch_detached_dispatcher` parses. PowerShell single-quoted strings need no backslash
    escaping (a Windows path's own backslashes pass through literally); only an embedded single
    quote needs doubling."""
    def _lit(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    return (
        "$startup = New-CimInstance -ClassName Win32_ProcessStartup "
        "-Property @{ ShowWindow = [uint16]0 } -ClientOnly\n"
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
        "@{ CommandLine = " + _lit(command_line) + "; CurrentDirectory = " + _lit(cwd)
        + "; ProcessStartupInformation = $startup }\n"
        "\"rc=$($r.ReturnValue) pid=$($r.ProcessId)\"\n"
    )


_WMI_PID_RE = re.compile(r"rc=(-?\d+)\s+pid=(\d+)")


_WMI_LAUNCH_TIMEOUT_S = 60

#: the only programs `_launch_via_wmi` treats as windowless (#261): a console program started
#: through WMI lands on the owner's desktop whatever desktop is asked for -- this repo's own
#: measurement (`test_wmi_standin_is_not_in_any_job`) only ever proved that clean for `pythonw`.
_WMI_WINDOWLESS_PROGRAMS = {"pythonw.exe", "pythonw"}


def _command_line_program(command_line: str) -> str:
    """The program name (basename, lower-cased) `command_line` would start: the first token,
    unquoted if quoted (`"C:\\...\\pythonw.exe" -c ...` -> `pythonw.exe`), whitespace-split
    otherwise (`python.exe -c ...` -> `python.exe`)."""
    text = command_line.strip()
    if text.startswith('"'):
        end = text.find('"', 1)
        token = text[1:end] if end != -1 else text[1:]
    else:
        token = text.split(None, 1)[0] if text else ""
    return Path(token).name.lower()


def _launch_via_wmi(command_line: str, cwd: Path, *, run: Callable[..., Any] | None = None,
                    timeout: float = _WMI_LAUNCH_TIMEOUT_S,
                    env: Mapping[str, str] | None = None) -> int:
    """Runs `command_line` windowless through WMI `Win32_Process.Create` (`_wmi_launch_ps1_text`),
    returns its pid. `run` is injected so a test never starts the real `powershell` call; it must
    return an object with `.stdout` (text), the way `subprocess.run(capture_output=True,
    text=True)` does. Factored out of `launch_detached_dispatcher` so #248's own test can launch a
    harmless stand-in command through the exact same code path the real dispatcher launch uses,
    without ever building the real `-m tools.agentops dispatch` command line.

    #261: while pytest is executing (`env` -- `os.environ` by default, injectable so a test can
    read its own fake one -- carries `PYTEST_CURRENT_TEST`), this refuses to start anything whose program
    is not windowless (`pythonw.exe`/`pythonw` only, case-insensitive, quoted or not, a full path
    allowed): the HARD RULE is that a console program started through WMI lands on the owner's
    desktop whatever desktop is asked for, and no test may risk that live. The refusal raises
    `OSError` before `run`/`subprocess.run` is ever called -- the runner is not called at all.
    Outside a test this starts whatever it is given, unchanged.

    #257: the script goes to `powershell` as `-EncodedCommand` (base64 of its UTF-16LE text) --
    no temp `.ps1` file, no `-File`, no execution-policy dependence, no ANSI misreading of a
    non-ASCII path. `-NonInteractive` keeps a hung prompt from blocking. `timeout=60` (real calls;
    a test overrides `run`) turns a hung WMI call into `subprocess.TimeoutExpired`, itself turned
    into `OSError` here -- the one and only way this starts anything: a failed or timed-out start
    raises, `run_ensure` propagates that as a non-zero exit, and no second (CreateProcess)
    mechanism is tried."""
    env_map = env if env is not None else os.environ
    if env_map.get("PYTEST_CURRENT_TEST") is not None:
        program = _command_line_program(command_line)
        if program not in _WMI_WINDOWLESS_PROGRAMS:
            raise OSError(
                f"_launch_via_wmi: refused under pytest -- {program!r} is not windowless "
                "(pythonw.exe/pythonw only); a console program started through WMI lands on the "
                "owner's desktop"
            )
    ps1_text = _wmi_launch_ps1_text(command_line, str(cwd))
    encoded = base64.b64encode(ps1_text.encode("utf-16-le")).decode("ascii")
    runner = run or subprocess.run
    try:
        result = runner(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise OSError(
            f"_launch_via_wmi: WMI Win32_Process.Create timed out after {timeout}s"
        ) from exc
    match = _WMI_PID_RE.search(result.stdout or "")
    if match is None or int(match.group(1)) != 0:
        raise OSError(
            f"_launch_via_wmi: WMI Win32_Process.Create failed: "
            f"stdout={result.stdout!r} stderr={getattr(result, 'stderr', None)!r}"
        )
    return int(match.group(2))


def launch_detached_dispatcher(*, cwd: Path = ROOT, out_path: Path = DISPATCH_OUT,
                               python_exe: str | None = None,
                               popen: Callable[..., Any] | None = None,
                               env: Mapping[str, str] | None = None) -> int:
    """The detached, windowless start for the dispatcher (`--ensure`, issue #201), stdout/stderr
    appended to `out_path`. No `--max-hours` is passed: the dispatcher's own default
    (`DEFAULT_MAX_HOURS`) applies, unchanged. `popen` is injected so a test never starts a real
    process; on Windows it is called with the `powershell` command list and must return an object
    with `.stdout` (text) and `.returncode`, the way `subprocess.run(capture_output=True,
    text=True)` does; off Windows it is called with the dispatcher's own command and must return
    an object with `.pid`, the way `subprocess.Popen` does. `env` is forwarded to
    `_launch_via_wmi`'s own `PYTEST_CURRENT_TEST` guard unchanged (#261): the real Windows command
    here is `cmd /c ...`, never windowless on its own, so a test that injects a `popen` stand-in
    under pytest must also pass `env={}` (or otherwise hide `PYTEST_CURRENT_TEST`) to reach it.

    #216/#236 (superseded by #248, kept for the flags' own history): `CREATE_NEW_CONSOLE |
    CREATE_NEW_PROCESS_GROUP` with `STARTUPINFO.wShowWindow = SW_HIDE` plus
    `CREATE_BREAKAWAY_FROM_JOB` gave the dispatcher its own hidden console that survived the
    launching session's job closing -- but only when that job itself carried
    `JOB_OBJECT_LIMIT_BREAKAWAY_OK`. #248 measured live that the dispatcher's real starters (a
    Claude Code session, the Task Scheduler's `pythonw.exe`) sit in a job that does not: 3 of 3
    live starts still showed `IsProcessInJob` true on the dispatcher, `CREATE_BREAKAWAY_FROM_JOB`
    refused every time, and the old fallback silently adopted the child into that job (the
    pre-#236 behaviour, dying with it).

    #248's own fix, measured live: start the dispatcher through WMI `Win32_Process.Create` --
    exactly the mechanism `scripts/launch_hidden.ps1` already uses for every other detached,
    windowless launch in this repo. The new process is a child of `WmiPrvSE.exe` (the WMI
    provider host), never of the caller, so it carries none of the caller's job-object membership
    at all: `IsProcessInJob` measured false, 3 of 3, including the one live trial where the
    caller sat in a `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` job with no `JOB_OBJECT_LIMIT_BREAKAWAY_
    OK` and that job closed right after the caller exited -- the WMI-started stand-in was still
    alive 10 s later, 3 of 3 trials. `Win32_ProcessStartup.ShowWindow = 0` keeps it windowless
    (0 windows measured on an isolated desktop, `CreateDesktopW`, never the real one). WMI's
    `Create` cannot redirect handles, so the command is wrapped in `cmd /c ... >> out 2>&1`."""
    return launch_detached_module(["dispatch"], cwd=cwd, out_path=out_path, python_exe=python_exe,
                                  popen=popen, env=env)


def launch_detached_module(args: list[str], *, cwd: Path = ROOT, out_path: Path,
                           python_exe: str | None = None,
                           popen: Callable[..., Any] | None = None,
                           env: Mapping[str, str] | None = None) -> int:
    """The detached, windowless start of `python -m tools.agentops <args>` (#136 part B: the snapshots'
    `monitor-snapshots run` uses it too), stdout/stderr appended to `out_path`. The body of
    `launch_detached_dispatcher`, which passes `["dispatch"]`; `popen` and `env` mean what they mean there."""
    return _launch_detached(["-m", "tools.agentops", *args], cwd=cwd, out_path=out_path,
                            python_exe=python_exe, popen=popen, env=env)


def _launch_detached(argv_tail: Sequence[str], *, cwd: Path, out_path: Path,
                     python_exe: str | None = None, popen: Callable[..., Any] | None = None,
                     env: Mapping[str, str] | None = None) -> int:
    """The one detached, windowless start (#248, #261) behind `launch_detached_dispatcher` and the
    monitor collector (#142, unit 3): `python <argv_tail>`, stdout/stderr appended to `out_path`.
    The dispatcher's own command line text is unchanged (`"<exe>" -m tools.agentops dispatch`)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    exe = python_exe or sys.executable
    if sys.platform != "win32":
        command = [exe, *argv_tail]
        spawn = popen or subprocess.Popen
        handle = open(out_path, "a", encoding="utf-8")
        kwargs: dict[str, Any] = dict(
            cwd=str(cwd), stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            close_fds=True, start_new_session=True,
        )
        proc = spawn(command, **kwargs)
        return proc.pid

    inner = f'"{exe}" ' + subprocess.list2cmdline(list(argv_tail))
    wrapped = f'cmd /c cd /d "{cwd}" && {inner} >> "{out_path}" 2>&1'
    return _launch_via_wmi(wrapped, cwd, run=popen, env=env)


#: the monitor collector (issue #142, unit 3): `<monitor mod>/bin/collect.py serve`, long-lived; the mod folder is
#: `mods/<monitor.source>` of `config/orchestration.json`. Its liveness record `bin/.cache/collector.lock` holds
#: `{"pid": N}`.
_COLLECTOR_SCRIPT = ("bin", "collect.py")
_COLLECTOR_LOCK = ("bin", ".cache", "collector.lock")


def _collector_mod(base: Path) -> Path | None:
    """`<base>/mods/<monitor.source>`, or None when the repo's config names no monitor source."""
    try:
        source = json.loads((base / "config" / "orchestration.json").read_text(encoding="utf-8"))["monitor"]["source"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return base / "mods" / source if isinstance(source, str) and source else None


_util_pid_alive = pid_alive


def _last_lines(path: Path, count: int = 3) -> str:
    """The last `count` lines of `path` joined by ` | `, one line for the dispatcher's log; a missing file says so."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return f"no {path.name}"
    return " | ".join(lines[-count:]) if lines else f"{path.name} is empty"


#: issue #186: a live collector older than the files it loads is stopped and replaced. The stop's lock wait is
#: `COLLECTOR_STOP_WAIT_S` seconds, polled every `COLLECTOR_POLL_S`; `collector_tick` checks at most every
#: `COLLECTOR_CHECK_INTERVAL_S` seconds of the dispatcher's own clock.
COLLECTOR_STOP_WAIT_S = 10.0
COLLECTOR_POLL_S = 0.5
COLLECTOR_CHECK_INTERVAL_S = 60.0
_NOT_COLLECTOR_LOGGED: set[int] = set()
_REFUSED_LOGGED: set[int] = set()
_AGENTOPS_IMPORT = re.compile(r"from tools\.agentops(?:\.(\w+))? import (\w+)")


def _collector_inputs(mod: Path, base: Path) -> list[Path]:
    """What the collector loads: its own `bin/collect.py`, the other `bin/*.py` of its mod, the `tools/agentops`
    modules collect.py imports, and the add-on file `monitor.addon` names under the root (when that file exists)."""
    files = [mod.joinpath(*_COLLECTOR_SCRIPT), *sorted(mod.joinpath("bin").glob("*.py"))]
    try:  # the `tools/agentops` modules collect.py imports
        source = mod.joinpath(*_COLLECTOR_SCRIPT).read_text(encoding="utf-8", errors="replace")
        for first, second in _AGENTOPS_IMPORT.findall(source):
            path = base / "tools" / "agentops" / f"{first or second}.py"
            if path.is_file() and path not in files:
                files.append(path)
    except OSError:
        pass
    try:
        rel = json.loads((base / "config" / "orchestration.json").read_text(encoding="utf-8"))["monitor"]["addon"]
    except (OSError, ValueError, KeyError, TypeError):
        rel = None
    if isinstance(rel, str) and rel and (base / rel).is_file():
        files.append(base / rel)
    return files


def _newest_input(mod: Path, base: Path) -> tuple[float, str] | None:
    """`(mtime, file name)` of the newest collector input, or None when none can be read."""
    newest: tuple[float, str] | None = None
    for path in _collector_inputs(mod, base):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if newest is None or mtime > newest[0]:
            newest = (mtime, path.name)
    return newest


def _is_mod_collector(command_line: str, script: Path) -> bool:
    """True when `command_line` runs this mod's `collect.py` with the word `serve` (paths compared normalised)."""
    cmd = command_line.replace("\\", "/").lower()
    return script.as_posix().lower() in cmd and "serve" in cmd.split()


def _append_dispatch_line(base: Path, text: str) -> None:
    dispatch_out = base / "artifacts" / "runs" / "dispatch.out"
    dispatch_out.parent.mkdir(parents=True, exist_ok=True)
    with open(dispatch_out, "a", encoding="utf-8") as handle:
        handle.write(f"{_now_str()} orch-dispatch: {text}\n")


def _collector_to_replace(base: Path, mod: Path, pid: int, rows_of: Callable[[], Sequence[ProcRow]]):
    """The newest input `(mtime, name)` when the live collector `pid` is this mod's `collect.py serve` and was
    created before that input; None (leave it alone) otherwise. A pid with no process row is left alone; a pid whose
    command line is not the mod's collector is logged once and left alone."""
    try:
        row = next((r for r in rows_of() if r.pid == pid), None)
    except Exception:  # noqa: BLE001 -- an unreadable process table tells nothing: the live collector is left alone
        return None
    if row is None:
        return None
    if not _is_mod_collector(row.command_line, mod.joinpath(*_COLLECTOR_SCRIPT)):
        if pid not in _NOT_COLLECTOR_LOGGED:  # once per pid per process: the tick step checks every minute
            _NOT_COLLECTOR_LOGGED.add(pid)
            _append_dispatch_line(base, f"collector lock pid {pid} is not the monitor collector "
                                        f"({row.command_line[:120]}); not stopped, nothing started")
        return None
    newest = _newest_input(mod, base)
    if newest is None or newest[0] <= row.create_time:
        return None
    return newest


def _stop_and_wait_for_lock(base: Path, lock: Path, pid: int, *, alive: Callable[[int], bool],
                            stopper: Callable[[list[int]], Any], wait_sleep: Callable[[float], Any],
                            wait_s: float) -> bool:
    """Stops `pid` through `stopper` once, then polls until the lock is gone or its pid is dead, at most `wait_s`
    seconds. True when it cleared; otherwise one log line and False."""
    try:
        outcomes = stopper([pid])
    except Exception as exc:  # noqa: BLE001 -- a failed stop is one log line, the tick goes on
        _append_dispatch_line(base, f"collector pid {pid} stop raised ({exc!r}); nothing started")
        return False
    refused = [o for o in (outcomes or []) if getattr(o, "action", "") == "refused"]
    if refused:  # the safety chain said no: the collector stays, one line per process (the tick checks every minute)
        if pid not in _REFUSED_LOGGED:
            _REFUSED_LOGGED.add(pid)
            _append_dispatch_line(base, f"collector pid {pid} stop refused by the safety chain; nothing started")
        return False
    waited = 0.0
    while True:
        if not lock.exists() or not alive(pid):
            return True
        if waited >= wait_s:
            _append_dispatch_line(base, f"collector pid {pid} did not clear its lock within {wait_s:g} s "
                                        f"of the stop; nothing started this tick")
            return False
        wait_sleep(COLLECTOR_POLL_S)
        waited += COLLECTOR_POLL_S


def ensure_collector(root: Path = ROOT, *, popen: Callable[..., Any] | None = None,
                     pid_alive: Callable[[int], bool] | None = None,
                     out_path: Path | None = None, env: Mapping[str, str] | None = None,
                     sleep: Callable[[float], Any] | None = None,
                     table: Callable[[], Sequence[ProcRow]] | None = None,
                     stopper: Callable[[list[int]], Any] | None = None,
                     lock_wait_s: float = COLLECTOR_STOP_WAIT_S,
                     wait_sleep: Callable[[float], Any] | None = None) -> str:
    """Keeps the monitor collector running (issue #142, rule 1; issue #166, rules 1 and 2; issue #186). Returns `"absent"`
    when the monitor mod's `bin/collect.py` (`mods/<monitor.source>`, `config/orchestration.json`) does not exist
    (nothing starts, no error); `"live"` when the pid in `collector.lock` is alive (nothing starts); otherwise starts
    `[sys.executable, collect.py, "serve"]` detached and windowless through the dispatcher's own
    start helper (`_launch_detached`, cwd=root, stdout/stderr appended to `out_path`, default
    `<root>/artifacts/runs/collector.out`, never dispatch.out). The launcher returns the pid (WMI's
    ProcessId on Windows); `sleep` (default `time.sleep`) waits `monitor.collector_start_check_s`
    seconds (default 10, `core_settings.NESTED_DEFAULTS`), then `pid_alive` checks that pid: alive
    returns `"started"`; dead appends one line to `<root>/artifacts/runs/dispatch.out`,
    `collector died after start: <last 3 lines of collector.out>`, and returns `"died"` (the next
    `--ensure` tries again). A missing, unreadable or dead lock starts it. Never raises: a failure prints one stderr
    line `dispatch --ensure: collector start failed: <err>` and returns `"failed"`.

    Issue #186: a LIVE lock pid that is this mod's `collect.py serve` (its process row in `table`, default
    `real_process_table`) and was created before the newest collector input (`bin/collect.py`, the other `bin/*.py`,
    the add-on file) is stopped once through `stopper` (default `stop_processes`, the one safe stop), waited for (at
    most `lock_wait_s` seconds: the lock gone or its pid dead), and replaced by the start above; one dispatch.out line
    names the old pid, the new pid and the newest file. A younger collector, a pid with no row, and a pid that is not
    the mod's collector are left alone (`"live"`); a pid that is not the mod's collector is also logged once. A lock
    that does not clear within the wait logs one line and starts nothing (`"live"`)."""
    try:
        base = Path(root)
        mod = _collector_mod(base)
        script = mod.joinpath(*_COLLECTOR_SCRIPT) if mod is not None else None
        if script is None or not script.is_file():
            return "absent"
        holder = read_pid_lock(mod.joinpath(*_COLLECTOR_LOCK))
        alive = pid_alive if pid_alive is not None else _util_pid_alive
        try:
            is_live = bool(alive(holder["pid"]))
        except (KeyError, TypeError, ValueError, OSError):
            is_live = False  # no lock, or a lock of another shape or pid: no live collector
        restart = None
        old_pid = None
        if is_live:
            old_pid = holder["pid"]
            rows_of = table if table is not None else real_process_table
            restart = _collector_to_replace(base, mod, old_pid, rows_of)
            if restart is None:
                return "live"
            if stopper is not None:
                stop = stopper
            elif table is not None:
                stop = lambda pids: stop_processes(pids, table=table)  # noqa: E731
            else:
                stop = lambda pids: stop_processes(pids)  # noqa: E731
            wait = wait_sleep if wait_sleep is not None else time.sleep
            if not _stop_and_wait_for_lock(base, mod.joinpath(*_COLLECTOR_LOCK), old_pid, alive=alive,
                                           stopper=stop, wait_sleep=wait, wait_s=lock_wait_s):
                return "live"
        collector_out = out_path if out_path is not None else base / "artifacts" / "runs" / "collector.out"
        pid = _launch_detached([str(script), "serve"], cwd=base, out_path=collector_out, popen=popen, env=env)
        if restart is not None:
            _append_dispatch_line(base, f"collector restarted on new code: old pid {old_pid}, new pid {pid}, "
                                        f"newest file {restart[1]}")
        check_s = float(optional_nested("monitor", "collector_start_check_s", base / "config" / "orchestration.json"))
        (sleep if sleep is not None else time.sleep)(check_s)
        if alive(pid):
            return "started"
        dispatch_out = base / "artifacts" / "runs" / "dispatch.out"
        dispatch_out.parent.mkdir(parents=True, exist_ok=True)
        with open(dispatch_out, "a", encoding="utf-8") as handle:
            handle.write(f"{_now_str()} orch-dispatch: collector died after start: "
                         f"{_last_lines(collector_out)}\n")
        return "died"
    except Exception as exc:  # noqa: BLE001 -- the collector never changes --ensure's own result
        print(f"dispatch --ensure: collector start failed: {exc}", file=sys.stderr)
        return "failed"


def collector_tick(dispatcher: Any, report: Any) -> None:
    """issue #186: the dispatcher's `after` step (registered by `mods.register`): calls `dispatcher.collector_ensure()`
    at most once per `COLLECTOR_CHECK_INTERVAL_S` seconds of the dispatcher's own clock (the first tick is due at once).
    A `None` `collector_ensure` (every test's default) does nothing. Never raises and never changes the tick's result:
    a failure is one log line."""
    try:
        ensure = getattr(dispatcher, "collector_ensure", None)
        if ensure is None or getattr(dispatcher, "_machine_mode_now", "normal") != "normal":
            return  # machine mode play (and an unreadable mode file) starts and stops nothing
        now = dispatcher.clock()
        if now - getattr(dispatcher, "_collector_checked_at", float("-inf")) < COLLECTOR_CHECK_INTERVAL_S:
            return
        dispatcher._collector_checked_at = now
        ensure()
    except Exception as exc:  # noqa: BLE001 -- the collector check never ends a tick
        dispatcher.log(f"collector check failed ({exc!r}); continuing")


def run_ensure(*, lock_path: Path = LOCK_PATH, out_path: Path = DISPATCH_OUT,
               launch: Callable[..., int] | None = None, root: Path = ROOT,
               popen: Callable[..., Any] | None = None,
               env: Mapping[str, str] | None = None,
               snapshots_ensure: Callable[[], int] | None = None) -> int:
    """`dispatch --ensure`: the dispatcher part (`_run_ensure_dispatcher`), then, on every path
    including a live dispatcher, the monitor collector (issue #142, unit 3) through
    `ensure_collector`. The returned code is the dispatcher's alone. One stdout line names the
    collector's result unless it is `"absent"`."""
    rc = _run_ensure_dispatcher(lock_path=lock_path, out_path=out_path, launch=launch, root=root,
                                snapshots_ensure=snapshots_ensure)
    collector = ensure_collector(root, popen=popen, env=env)
    if collector != "absent":
        print(f"dispatch --ensure: collector {collector}")
    return rc


def _run_ensure_dispatcher(*, lock_path: Path = LOCK_PATH, out_path: Path = DISPATCH_OUT,
                           launch: Callable[..., int] | None = None, root: Path = ROOT,
                           snapshots_ensure: Callable[[], int] | None = None) -> int:
    """`dispatch --ensure` (issue #201): starts one detached dispatcher when none is live, does
    nothing when one already is. Exits 0 for both of those (the wanted steady state); #257: a
    failed or timed-out start (`_launch_via_wmi` raising `OSError`) is not a steady state -- one
    line goes to stderr naming it, and this returns 1 so `--ensure`'s own exit code is non-zero
    (the keepalive task, its own caller, tries again in 5 min; no second start mechanism here).

    Issue #289: heals a target ported before the runtime-excludes fix by writing the missing
    `.git/info/exclude` rules here too, before the alive check; a failure here is not a steady
    state either but is never fatal -- one line to stderr, then the ensure logic goes on.

    #136 part B: after the dispatcher part (its line printed, its rc computed), the monitor snapshots
    start (`snapshots_ensure`; default the real `monitor_snapshots.ensure_snapshots`, quiet when live,
    `monitor snapshots: started pid N` when started; with an injected `launch` and no `snapshots_ensure`
    the default is a no-op). Their failure is one stderr line and never changes this function's rc."""
    try:
        from .port import ensure_runtime_excludes
        ensure_runtime_excludes(root)
    except Exception as exc:
        print(f"dispatch --ensure: runtime excludes failed: {exc}", file=sys.stderr)

    alive, pid = dispatcher_status(lock_path)
    rc = 0
    if alive:
        commit = (read_pid_lock(lock_path) or {}).get("commit")
        print(f"live pid {pid} commit {commit}" if commit else f"live pid {pid}")
    else:
        spawn = launch or launch_detached_dispatcher
        try:
            new_pid = spawn(out_path=out_path)
        except OSError as exc:
            print(f"dispatch --ensure: failed to start the dispatcher: {exc}", file=sys.stderr)
            rc = 1
        else:
            print(f"started pid {new_pid}")

    # the snapshots come after the dispatcher part, so their start never delays it; a failure is one
    # stderr line and never the dispatcher's rc. An injected dispatcher `launch` with no
    # `snapshots_ensure` starts no snapshot (a test's stand-in never reaches the real lock or Popen).
    try:
        snapshots = snapshots_ensure
        if snapshots is None and launch is None:
            from .monitor_snapshots import ensure_snapshots
            snapshots = lambda: ensure_snapshots(prefix="monitor snapshots")  # noqa: E731
        if snapshots is not None:
            snapshots()
    except Exception as exc:  # noqa: BLE001 - the snapshots are never the dispatcher's business
        print(f"dispatch --ensure: monitor snapshots failed to start: {exc}", file=sys.stderr)
    return rc


def run_dispatch(*, once: bool = False, max_hours: float = DEFAULT_MAX_HOURS,
                 dry_run: bool = False, args: Any = None, lock_path: Path = LOCK_PATH) -> int:
    """The real dispatcher. `args` is the parsed command line, handed to the local modules so each
    reads its own flag off it (`extensions.py`, the `start` point); `None` runs them with none.
    Refuses to start beside a live dispatcher (issue #201): the lock is taken before anything else
    runs, so a second dispatcher starts nothing and adopts nothing."""
    try:
        with dispatcher_lock(lock_path):
            # #141: the fingerprint before the core update, so the update's rewritten files (the
            # modules loaded now are the old code) make the first tick's check exit once
            try:
                start_fp: str | None = code_fingerprint()
            except Exception as exc:
                print(f"dispatch: code fingerprint failed at start: {exc!r}", file=sys.stderr)
                start_fp = None
            apply_auto_core_update()
            try:
                from .port import ensure_runtime_excludes
                ensure_runtime_excludes(ROOT)
            except Exception as exc:
                print(f"dispatch: runtime excludes failed: {exc}", file=sys.stderr)
            from .merge_github import BRAIN_LINES  # #241: the file the merge writes, beside its merge log
            dispatcher = Dispatcher(claude=ClaudeCli(), gh=GhCli(), max_hours=max_hours,
                                    core_update=apply_auto_core_update,
                                    pending_core_update=apply_auto_core_update_in_line, core_root=ROOT,
                                    autopilot_waits_file=AUTOPILOT_WAITS_FILE,
                                    admission_hold_file=ADMISSION_HOLD_FILE,
                                    core_issue_writer=_default_core_issue_writer,
                                    stream_keeper=_default_stream_keeper(),
                                    has_pending_job=settings_pending_job,
                                    brain_notice_file=BRAIN_NOTICE_FILE, unblocker_start_file=UNBLOCKER_START_FILE,
                                    losses_daily_file=LOSSES_DAILY_FILE,
                                    core_notice_file=CORE_NOTICE_FILE,
                                    code_fingerprint=code_fingerprint, start_fingerprint=start_fp,
                                    brain_lines_file=ROOT / "var" / "agentops" / BRAIN_LINES,
                                    dry_run=dry_run,
                                    orphan_sweep=lambda started_at, *, ended_claude_pid, live_claude_pids,
                                                         recorded_owned_pids, ended_session:
                                        sweep_orphans(session_started_at=started_at, log=_log,
                                                     ended_claude_pid=ended_claude_pid,
                                                     live_claude_pids=live_claude_pids,
                                                     recorded_owned_pids=recorded_owned_pids,
                                                     ended_session=ended_session),
                                    whole_disk_sweep=lambda: sweep_stale_whole_disk_searches(log=_log),
                                    owned_pids_snapshot=snapshot_owned_pids,
                                    collector_ensure=lambda: ensure_collector(ROOT))
            registry.run_start(dispatcher, args)
            from .hold import Keeper
            dispatcher.keeper = Keeper(ROOT, log=_log)
            dispatcher.keeper.start()
            try:
                return dispatcher.run(ticks=1 if once else None)
            finally:
                dispatcher.keeper.stop()
    except DispatcherLockHeld as exc:
        print(str(exc))
        return 3


def main(argv: list[str] | None = None) -> int:
    registry.load()  # before the parser: a local module may add a flag of its own below
    parser = argparse.ArgumentParser(prog="agentops dispatch", description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="run a single tick and exit")
    parser.add_argument("--max-hours", type=float, default=DEFAULT_MAX_HOURS)
    parser.add_argument("--ensure", action="store_true",
                        help="start one detached dispatcher if none is live, else do nothing")
    registry.add_dispatch_flags(parser)
    parser.add_argument("--dry-run", action="store_true",
                        help="log what would be started, without launching it")
    args = parser.parse_args(argv)
    if args.ensure:
        return run_ensure()
    return run_dispatch(once=args.once, max_hours=args.max_hours, dry_run=args.dry_run, args=args)


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "WAKE_MODEL",
    "WAKE_SENDER_NAME",
    "CONTEXT_LINE",
    "FINISHING_DIR",
    "SESSION_ID_ENV_VARS",
    "LOCK_PATH",
    "DISPATCH_OUT",
    "DispatcherLockHeld",
    "dispatcher_lock",
    "dispatcher_status",
    "launch_detached_dispatcher",
    "run_ensure",
    "ensure_collector",
    "collector_tick",
    "apply_auto_core_update",
    "apply_auto_core_update_in_line",
    "ClaudeCli",
    "Dispatcher",
    "GhCli",
    "GuardError",
    "IssueStatus",
    "IssueSummary",
    "LIVE_STATES",
    "MAX_CONSECUTIVE_NO_CHECKPOINT",
    "OUTAGE_WAIT_S",
    "TICK_SECONDS",
    "TickReport",
    "MAX_WAKES_PER_ISSUE",
    "WAKE_COOLDOWN_SECONDS",
    "PERMISSION_WAIT",
    "BLOCKED_STATE",
    "PERMISSION_REPORT_COOLDOWN",
    "build_permission_block_message",
    "last_timed_record_type",
    "build_command",
    "build_prompt",
    "build_wake_command",
    "build_wake_message",
    "build_wake_prompt",
    "default_projects_dir",
    "end_session",
    "ended_task_ids",
    "find_own_session_id",
    "mark_finishing",
    "run_finishing_command",
    "running_subagent_ids",
    "session_matches_root",
    "find_claude_exe",
    "main",
    "run_dispatch",
]
