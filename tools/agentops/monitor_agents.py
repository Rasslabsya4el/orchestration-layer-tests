"""The agents panel of the local monitor (moved here from `monitor.py`, E2a): the live sessions of this
repo, their subagents, the ORCH session and the issues it holds, read from the transcripts.

Read-only: the transcripts under the Claude projects folder, `claude agents --json --all`, and the
dispatcher's `orch_session.json`. Stdlib and core modules only. The transcript parsers and the price
arithmetic live in `scripts/token_meter.py`; that module is imported on first use (`_token_meter`), never
at import time, so this file loads in a repo that has no `scripts/` on `sys.path` and no product package.
"""

from __future__ import annotations

import json
import re
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .core_settings import load as _load_settings
from .dispatch import EVENTS_FILE as ORCH_EVENTS_FILE, STATE_FILE as ORCH_STATE_FILE, _TASK_GROUP_RE, _TASK_ID_RE, _record_time, ended_task_ids, find_claude_exe
from .monitor_pipeline import claim_times, session_start
from .outcomes import DEFAULT_OUTCOMES_LOG, load_jsonl
from .util import human_duration, no_window_flags

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "scripts"

# The repo slug and the Claude projects folder come from config/orchestration.json, the same keys
# token_meter reads: the values are unchanged, and no repo name is written into this file.
_SETTINGS = _load_settings()
REPO = _SETTINGS["repo"]
DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects" / _SETTINGS["claude_projects_dir"]
DEFAULT_PRICES_PATH = ROOT / "config" / "model_prices.json"


def _token_meter():
    """`scripts/token_meter.py`, imported on the first call that needs it."""

    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import token_meter

    return token_meter


#: A Claude model id is named by rule (family, version digits joined with a dot, date dropped);
#: only ids the rule cannot read (bare aliases) go through this small table.
_MODEL_ID_RE = re.compile(
    r"^claude-(?P<family>[a-z]+)-(?P<major>\d+)(?:-(?P<minor>\d+))?(?:-\d{8})?$")
_MODEL_ALIASES = {"sonnet": "Sonnet", "opus": "Opus", "haiku": "Haiku", "fable": "Fable"}

_ISSUE_IN_NAME_RE = re.compile(r"#(\d+)")


def model_display_name(model_id: str | None) -> str | None:
    if model_id is None:
        return None
    m = _MODEL_ID_RE.match(model_id)
    if m:
        name = f"{m['family'].title()} {m['major']}"
        return f"{name}.{m['minor']}" if m["minor"] is not None else name
    return _MODEL_ALIASES.get(model_id, model_id)


def display_model(model_id: str | None) -> str | None:
    return model_display_name(model_id)


def _tool_summary(input_: Any, limit: int = 60) -> str:
    """The tool call's own `description`, else a representative field, else the input dumped --
    every case truncated to `limit` chars (requirement 2's "tool name + description or a 60-char
    summary")."""

    if not isinstance(input_, dict):
        return ""
    description = input_.get("description")
    if isinstance(description, str) and description:
        return description[:limit]
    for key in ("command", "file_path", "path", "pattern", "prompt", "url", "cmd", "title"):
        value = input_.get(key)
        if isinstance(value, str) and value:
            return value[:limit]
    try:
        return json.dumps(input_, ensure_ascii=False)[:limit]
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------- transcript scanning (cached) ---
#
# Each cache is keyed by (path, mtime, size): a 3 s refresh against an untouched multi-MB main
# chat re-reads nothing (requirement "stream it; never load a whole file into memory... cache per
# file by (size, mtime) so a 3 s refresh does not re-read whole files").

@dataclass
class MainScan:
    turns: list  # token_meter.Turn
    first_ts: datetime | None
    last_ts: datetime | None
    actions: list[dict[str, str]]  # last N, oldest first
    # #153: the records carrying the file's own sessionId (a side thread's copied parent records do not), and
    # whether the first record names another session (a side thread: its file opens with the parent's records)
    own_first_ts: datetime | None = None
    own_last_ts: datetime | None = None
    side_thread: bool = False


def _cache_key(path: Path) -> tuple[Path, float, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (path, stat.st_mtime, stat.st_size)


class _Cache:
    """A tiny (key -> value) cache with no eviction: one process, a handful of live transcripts."""

    def __init__(self) -> None:
        self._store: dict[Path, tuple[tuple, Any]] = {}

    def get_or_compute(self, path: Path, compute: Callable[[], Any]) -> Any:
        key = _cache_key(path)
        if key is None:
            return compute()
        cached = self._store.get(path)
        if cached is not None and cached[0] == key:
            return cached[1]
        value = compute()
        self._store[path] = (key, value)
        return value


_MAIN_CACHE = _Cache()
_SUBAGENT_CACHE = _Cache()
_COMPLETED_CACHE = _Cache()
#: #136: a subagent record counts as a resume only when it is newer than the ended notification by more than this
#: (a record written in the same instant as the notification is the agent's own last word, not a resume).
RESUME_MARGIN = timedelta(seconds=5)
_ENDED_AT_CACHE = _Cache()
_LAST_RECORD_CACHE = _Cache()
_ANSWERED_CACHE = _Cache()


def scan_main_transcript(path: Path, *, action_limit: int = 5) -> MainScan:
    """One pass over a main-chat transcript (mirrors `token_meter.parse_main_transcript`'s
    dedupe/attribution) that also collects the first/last timestamp seen and the last `tool_use`
    calls, so the monitor does not scan the same multi-MB file three times per refresh."""

    token_meter = _token_meter()
    session_id = path.stem
    seen_ids: set[str] = set()
    turns: list = []
    current_issue: int | None = None
    turn_index = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    actions: deque = deque(maxlen=action_limit)
    first_session_id: str | None = None
    own_first_ts: datetime | None = None
    own_last_ts: datetime | None = None

    for obj in token_meter._read_jsonl(path):
        ts = token_meter.parse_timestamp(obj.get("timestamp"))
        if ts is not None:
            if first_ts is None:
                first_ts = ts
            last_ts = ts
        record_sid = obj.get("sessionId")
        if isinstance(record_sid, str) and record_sid:
            if first_session_id is None:
                first_session_id = record_sid
            if record_sid == session_id and ts is not None:
                if own_first_ts is None:
                    own_first_ts = ts
                own_last_ts = ts
        kind = obj.get("type")
        if kind == "user":
            text = token_meter.message_text(obj.get("message") or {})
            issue = token_meter.owner_issue(text)
            if issue is not None:
                current_issue = issue
            continue
        if kind != "assistant":
            continue
        message = obj.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    actions.append({
                        "tool": block.get("name") or "?",
                        "summary": _tool_summary(block.get("input") or {}),
                    })
        message_id = message.get("id")
        if not message_id or message_id in seen_ids:
            continue
        seen_ids.add(message_id)
        turn = token_meter._usage_turn(
            obj, session_id=session_id, agent_id=None, agent_type=None,
            issue=current_issue, title=None, turn_index=turn_index,
        )
        if turn is not None:
            turns.append(turn)
            turn_index += 1
    return MainScan(turns=turns, first_ts=first_ts, last_ts=last_ts, actions=list(actions),
                    own_first_ts=own_first_ts, own_last_ts=own_last_ts,
                    side_thread=first_session_id is not None and first_session_id != session_id)


def cached_main_scan(path: Path, *, action_limit: int = 5) -> MainScan:
    return _MAIN_CACHE.get_or_compute(path, lambda: scan_main_transcript(path, action_limit=action_limit))


def cached_subagent_turns(path: Path, meta: dict) -> list:
    return _SUBAGENT_CACHE.get_or_compute(path, lambda: _token_meter().parse_subagent_transcript(path, meta))


def completed_agent_ids(parent_path: Path) -> set[str]:
    """Every subagent id this session's own transcript has seen a `<task-notification>` with
    `<status>completed</status>` for (requirement 3: "done when the parent transcript holds its
    completion notification or hand-back"). One pass, cached like the others."""

    def compute() -> set[str]:
        # The notification sits in `queue-operation` / `attachment` lines, not only in `user`
        # ones; the dispatcher's raw-line reader handles every shape (and failed / killed endings).
        return ended_task_ids(parent_path)

    return _COMPLETED_CACHE.get_or_compute(parent_path, compute)


def ended_times(parent: Path) -> dict[str, datetime | None]:
    """#136: per task id, the time of its LAST ended `<task-notification>` in a session transcript, read
    the way `ended_task_ids` reads (same ids, a `running` status ends nothing). None when that record has
    no timestamp: such an end is never resumed."""

    latest: dict[str, datetime | None] = {}
    if not parent.is_file():
        return latest
    with parent.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "task-notification" not in line:
                continue
            ended = [ids for ids, status in _TASK_GROUP_RE.findall(line) if status.strip() != "running"]
            if not ended:
                continue
            at = _record_time(line)
            for ids in ended:
                for task_id in _TASK_ID_RE.findall(ids):
                    latest[task_id.strip()] = at
    return latest


def cached_ended_times(parent_path: Path) -> dict[str, datetime | None]:
    return _ENDED_AT_CACHE.get_or_compute(parent_path, lambda: ended_times(parent_path))


def last_record_time(path: Path) -> datetime | None:
    """#136: the newest `timestamp` of any record of a subagent transcript (aware), None when the file is
    missing or holds none. Cached per (path, mtime, size) like the other readers."""

    if not path.is_file():
        return None

    def compute() -> datetime | None:
        newest: datetime | None = None
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"timestamp"' not in line:
                    continue
                at = _record_time(line)
                if at is not None and (newest is None or at > newest):
                    newest = at
        return newest

    return _LAST_RECORD_CACHE.get_or_compute(path, compute)


def answered_tool_use_ids(parent_path: Path) -> set[str]:
    """Every `tool_use_id` the parent transcript holds a `tool_result` for: a foreground
    subagent ends with that plain answer, never with a `<task-notification>`. Cached per mtime."""

    def compute() -> set[str]:
        token_meter = _token_meter()
        ids: set[str] = set()
        if not parent_path.is_file():
            return ids
        for obj in token_meter._read_jsonl(parent_path):
            content = (obj.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id"):
                    ids.add(block["tool_use_id"])
        return ids

    return _ANSWERED_CACHE.get_or_compute(parent_path, compute)


# ------------------------------------------------------------------------------------- pricing ---

def turn_cost_total(turns: list, prices: dict[str, dict]) -> tuple[float, int]:
    """(summed USD cost of the priced turns, count of turns with no price)."""

    token_meter = _token_meter()
    total = 0.0
    unpriced = 0
    for turn in turns:
        cost = token_meter.turn_cost(turn, prices)
        if cost is None:
            unpriced += 1
        else:
            total += cost
    return total, unpriced


# ------------------------------------------------------------------------------------ sessions ---

def _issue_from_name(name: str | None) -> int | None:
    if not name:
        return None
    numbers = {int(n) for n in _ISSUE_IN_NAME_RE.findall(name)}
    return next(iter(numbers)) if len(numbers) == 1 else None


def _seconds_since(ts: datetime | None, now: datetime) -> float | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (now - ts).total_seconds())


def _started_at(raw: dict) -> datetime | None:
    """#153: the app entry's `startedAt` (epoch milliseconds), or None when absent or not a number."""
    value = raw.get("startedAt")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def build_subagent_row(path: Path, meta: dict, *, prices: dict[str, dict],
                       completed_ids: set[str], now: datetime,
                       answered_ids: set[str] = frozenset(),
                       ended_at: dict[str, datetime | None] | None = None) -> dict[str, Any]:
    agent_id = meta.get("agentId") or path.stem.removeprefix("agent-")
    turns = cached_subagent_turns(path, meta)
    last_turn = turns[-1] if turns else None
    first_turn = turns[0] if turns else None
    cost, unpriced = turn_cost_total(turns, prices)
    ended = agent_id in completed_ids
    if ended and ended_at is not None:
        # #136: an end is void once the subagent wrote a record after it (a SendMessage resume);
        # a later ended notification sets a newer end time and the row is ended again.
        ended_time = ended_at.get(agent_id)
        newest = last_record_time(path)
        if ended_time is not None and newest is not None and newest - ended_time > RESUME_MARGIN:
            ended = False
    running = not ended
    if running and meta.get("requestShape") == "foreground" and meta.get("toolUseId") in answered_ids:
        running = False  # A foreground subagent ends with its tool_result in the parent

    if first_turn is not None:
        end = now if running else (last_turn.timestamp if last_turn else None)
        duration_seconds = None
        if first_turn.timestamp is not None and end is not None:
            duration_seconds = _seconds_since(first_turn.timestamp, end) if running else \
                max(0.0, (end - first_turn.timestamp).total_seconds())
    else:
        duration_seconds = None

    issue = first_turn.issue if first_turn is not None else None
    if issue is None:
        issue = _token_meter().single_issue(str(meta.get("description") or ""))

    return {
        "source": "transcript",  # hidden: not rendered
        "agent_id": agent_id,
        "agent_type": meta.get("agentType"),
        "issue": issue,
        "task": meta.get("description"),
        "model": display_model(last_turn.model if last_turn else None),
        "running": running,
        "context": last_turn.context if last_turn else 0,
        "turns": len(turns),
        "cost_usd": round(cost, 2),
        "cost_unpriced_turns": unpriced,
        "duration_seconds": duration_seconds,
        "duration_human": human_duration(duration_seconds) if duration_seconds is not None else "-",
    }


def build_session_row(raw: dict, *, projects_dir: Path, prices: dict[str, dict],
                      now: datetime) -> dict[str, Any]:
    session_id = raw.get("sessionId") or raw.get("id") or ""
    name = raw.get("name")
    transcript_path = projects_dir / f"{session_id}.jsonl"
    scan = cached_main_scan(transcript_path) if transcript_path.is_file() else MainScan([], None, None, [])
    last_turn = scan.turns[-1] if scan.turns else None
    cost, unpriced = turn_cost_total(scan.turns, prices)

    issue_number = _issue_from_name(name) or (last_turn.issue if last_turn else None)
    issue = {"number": issue_number, "url": f"https://github.com/{REPO}/issues/{issue_number}"} \
        if issue_number is not None else None

    kind = raw.get("kind")
    status = raw.get("status")
    state = raw.get("state")  # background only: "working" / "done"
    display_state = state or status or "unknown"

    subagents_dir = projects_dir / session_id / "subagents"
    completed_ids = completed_agent_ids(transcript_path)
    ended_at = cached_ended_times(transcript_path)
    answered_ids = answered_tool_use_ids(transcript_path)
    subagents = []
    if subagents_dir.is_dir():
        for jsonl_path in sorted(subagents_dir.glob("agent-*.jsonl")):
            meta_path = jsonl_path.parent / (jsonl_path.stem + ".meta.json")
            meta: dict = {}
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    meta = {}
            subagents.append(build_subagent_row(jsonl_path, meta, prices=prices,
                                                completed_ids=completed_ids, now=now,
                                                answered_ids=answered_ids, ended_at=ended_at))
    subagents.sort(key=lambda row: (not row["running"], row["agent_id"]))  # running on top

    # #153: a chat's duration counts from the app's `startedAt`; a side thread's transcript opens with its parent's
    # records, so without it the first own record (then the first record, as before) is the start
    age_seconds = _seconds_since(_started_at(raw) or scan.own_first_ts or scan.first_ts, now)

    return {
        "session_id": session_id,
        "name": name,
        "kind": kind,
        "status": status,
        "state": display_state,
        "issue": issue,
        "model": display_model(last_turn.model if last_turn else None),
        "context": last_turn.context if last_turn else 0,
        "turns": len(scan.turns),
        "cost_usd": round(cost, 2),
        "cost_unpriced_turns": unpriced,
        "age_seconds": age_seconds,
        "age_human": human_duration(age_seconds) if age_seconds is not None else "-",
        "last_actions": scan.actions,
        "subagents": subagents,
    }


_ORCH_NAME_RE = re.compile(r"^orch(?:\s|#|$)")


def _is_orch(row: dict[str, Any]) -> bool:
    return bool(_ORCH_NAME_RE.match(row.get("name") or ""))


def _read_events(path: Path) -> list[dict[str, Any]]:
    """The dispatcher's events file, one JSON object a line; a missing file or a garbled line is no event."""

    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def load_orch_state(path: Path = ORCH_STATE_FILE, events_path: Path = ORCH_EVENTS_FILE) -> dict[str, Any] | None:
    """The dispatcher's `orch_session.json`: the live ORCH session and the issues it holds. #6: `claimed_at` maps
    each held issue number to the time this session claimed it (the events file's `claimed` event), or, for an
    issue it took without a `claimed` event (through `offered`), to the session's start (as
    `dispatch.holding_window_from_events` does); absent when neither is known."""

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not (isinstance(data, dict) and data.get("session")):
        return None
    ids = [data.get("session"), data.get("session_uuid")]
    events = _read_events(events_path)
    claims = claim_times(events, ids)
    start = session_start(events, ids)
    data["claimed_at"] = {int(n): claims.get(int(n)) or start or None for n in data.get("held") or []}
    return data


_UNIT_RE = re.compile(r"\d+/\d+")


def unit_progress(outcomes: list[dict[str, Any]], issue: int) -> dict[str, int] | None:
    """#129, #160: `done/total` of a sliced issue, from its `k/n` outcome rows. `total` is the n of the latest
    such row (by `ts`). A unit k is done when its latest row (of any n, a row of an older cut counts for its k)
    is `accepted` or `redone` (accepted after a send-back); a row with k > total is ignored. None when the
    issue has no `k/n` row or the cut has fewer than two units."""

    rows = [row for row in outcomes
            if row.get("issue") == issue and isinstance(row.get("unit"), str) and _UNIT_RE.fullmatch(row["unit"])]
    if not rows:
        return None
    rows.sort(key=lambda row: row.get("ts") or 0)
    total = int(rows[-1]["unit"].split("/")[1])
    latest_class: dict[int, str | None] = {}
    for row in rows:
        k = int(row["unit"].split("/")[0])
        if k <= total:
            latest_class[k] = row.get("class")
    done = sum(1 for cls in latest_class.values() if cls in ("accepted", "redone"))
    return {"done": done, "total": total} if total >= 2 else None


def units_by_issue(outcomes: list[dict[str, Any]]) -> dict[int, dict[str, int]]:
    """#151: `unit_progress` for every int issue that has a `k/n` row. An issue whose progress is None (a cut
    of fewer than two units) is absent; rows whose `issue` is not an int are skipped."""

    issues = {row["issue"] for row in outcomes
              if isinstance(row.get("issue"), int) and isinstance(row.get("unit"), str)
              and _UNIT_RE.fullmatch(row["unit"])}
    result: dict[int, dict[str, int]] = {}
    for issue in issues:
        progress = unit_progress(outcomes, issue)
        if progress is not None:
            result[issue] = progress
    return result


_CUT_HEADER_RE = re.compile(r"\|\s*unit\s*\|\s*outcome\s*\|", re.IGNORECASE)
_CUT_UNIT_CELL_RE = re.compile(r"\|\s*\d+\s+of\s+(\d+)\b", re.IGNORECASE)


def _cut_table_ns(body: str) -> list[int]:
    """#151: the n of each unit cell (`k of n (name)`) in the table rows that follow a `| unit | outcome |`
    header; a line that is not a table row ends the table."""

    ns: list[int] = []
    in_table = False
    for line in body.splitlines():
        stripped = line.strip()
        if _CUT_HEADER_RE.search(stripped):
            in_table = True
        elif in_table and stripped.startswith("|"):
            match = _CUT_UNIT_CELL_RE.match(stripped)
            if match:
                ns.append(int(match.group(1)))
        else:
            in_table = False
    return ns


def cut_table_total(comment_bodies: list[str]) -> int | None:
    """#151: the n of the newest ORCH cut-table comment (last in the list) that holds a cut table: the largest n
    in its unit cells. None when no comment holds one or that n is below two."""

    for body in reversed(comment_bodies):
        ns = _cut_table_ns(body)
        if ns:
            n = max(ns)
            return n if n >= 2 else None
    return None


def _load_outcome_rows(path: Path) -> list[dict[str, Any]]:
    """The outcome log's dict rows for the `units` column; a missing or unreadable log is no rows (#129)."""

    try:
        return [row for row in load_jsonl(Path(path)) if isinstance(row, dict)]
    except (OSError, ValueError):
        return []


def ticket_units(outcomes: list[dict[str, Any]], held: list[int],
                 cut_totals: dict[int, int] | None = None) -> dict[str, dict[str, int]]:
    """#151 rule 1-2: `units_by_issue` for every issue with `k/n` rows (held or not), and for each held issue with
    no such rows a known cut-table total n >= 2 as `{done: 0, total: n}`. Keys are the issue number as str."""

    units = {str(n): progress for n, progress in units_by_issue(outcomes).items()}
    for n in held:
        total = (cut_totals or {}).get(n)
        if str(n) not in units and isinstance(total, int) and total >= 2:
            units[str(n)] = {"done": 0, "total": total}
    return units


def attach_held_issues(row: dict[str, Any], orch_state: dict[str, Any],
                       outcomes: list[dict[str, Any]] | None = None,
                       cut_totals: dict[int, int] | None = None) -> None:
    """Under the one ORCH row, the issues it holds, each with its own subagents (a subagent
    belongs to the issue its first prompt names); subagents of no held issue stay in `subagents`.
    Each held issue carries `units` (#129): its `unit_progress` over `outcomes`, None without rows.
    The row carries `ticket_units` (#151), the `units` of every ticket with data, see `ticket_units`."""

    held = [int(n) for n in orch_state.get("held") or []]
    claimed = set(orch_state.get("claimed_now") or [])
    by_issue = {n: [] for n in held}
    rest = []
    for sub in row.get("subagents") or []:
        (by_issue[sub["issue"]] if sub.get("issue") in by_issue else rest).append(sub)
    claimed_at = orch_state.get("claimed_at") or {}
    rows = outcomes or []
    row["held_issues"] = [{"number": n, "url": f"https://github.com/{REPO}/issues/{n}",
                           "claimed": n in claimed, "claimed_at": claimed_at.get(n), "subagents": by_issue[n],
                           "units": unit_progress(rows, n)}
                          for n in held]
    row["ticket_units"] = ticket_units(rows, held, cut_totals)
    row["subagents"] = rest


def _matches_state(row: dict[str, Any], orch_state: dict[str, Any]) -> bool:
    session_id = str(row.get("session_id") or "")
    uuid = orch_state.get("session_uuid")
    short = str(orch_state.get("session") or "")
    return bool(session_id) and (session_id == uuid or (short and session_id.startswith(short)))


def _side_thread_idle_dropped(raw: dict, *, projects_dir: Path, now: datetime,
                              idle_drop_s: float) -> bool:
    """#153: a side thread (its transcript opens with another session's records) whose app status is `idle` and
    whose newest own record is older than `idle_drop_s`: it has finished, and its row is dropped like a closed
    chat's. A thread with no own record is not dropped (nothing shows it idle); any other chat never is."""

    session_id = raw.get("sessionId") or raw.get("id") or ""
    transcript_path = projects_dir / f"{session_id}.jsonl"
    if not session_id or raw.get("kind") != "interactive" or raw.get("status") != "idle" \
            or not transcript_path.is_file():
        return False
    scan = cached_main_scan(transcript_path)
    if not scan.side_thread or scan.own_last_ts is None:
        return False
    idle_s = _seconds_since(scan.own_last_ts, now)
    return idle_s is not None and idle_s > idle_drop_s


def panel_agents(sessions_raw: list[dict], *, projects_dir: Path, prices: dict[str, dict],
                 repo_cwd: Path, now: datetime | None = None,
                 orch_state: dict[str, Any] | None = None,
                 outcomes_path: Path = DEFAULT_OUTCOMES_LOG,
                 cut_totals: dict[int, int] | None = None,
                 side_thread_idle_drop_s: float = 600) -> dict[str, Any]:
    """Requirements 2-3: every live session of THIS repo, ORCH background sessions first; the
    dispatcher's live ORCH carries its held issues and their subagents. The outcome log is read once
    per call, and only when there is an ORCH state, for the held issues' `units` (#129) and the ORCH
    row's `ticket_units` (#151). `cut_totals` (issue -> n) is the cut-table total of a held issue with no rows.
    A finished side thread (#153, `side_thread_idle_drop_s`) gets no row, so its subagents go with it."""

    now = now or datetime.now(timezone.utc)
    repo_cwd_resolved = str(Path(repo_cwd).resolve())
    outcomes = _load_outcome_rows(outcomes_path) if orch_state else []
    rows = []
    for raw in sessions_raw:
        cwd = raw.get("cwd")
        if cwd is not None and str(Path(cwd).resolve()) != repo_cwd_resolved:
            continue
        if _side_thread_idle_dropped(raw, projects_dir=projects_dir, now=now, idle_drop_s=side_thread_idle_drop_s):
            continue
        row = build_session_row(raw, projects_dir=projects_dir, prices=prices, now=now)
        if orch_state and _is_orch(row) and _matches_state(row, orch_state):
            attach_held_issues(row, orch_state, outcomes, cut_totals)
        rows.append(row)
    rows.sort(key=lambda row: (not _is_orch(row), row.get("name") or ""))
    return {"sessions": rows}


def live_sessions_raw(exe: Path | str | None = None) -> list[dict]:
    """`claude.exe agents --json --all`, the one real, read-only call this module makes."""

    import subprocess

    resolved_exe = exe if exe is not None else find_claude_exe()
    result = subprocess.run([str(resolved_exe), "agents", "--json", "--all"], cwd=str(ROOT),
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=30, creationflags=no_window_flags())
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return []
    return data if isinstance(data, list) else []
