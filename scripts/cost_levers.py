"""Cost levers: the three lever indicators and the whole cost per closed issue.

    python scripts/cost_levers.py --since <time> [--until <time>]
        [--sessions <file of ORCH session ids>] [--spawns N] [--issues N]

Read-only report over Claude Code transcripts (main chats and their subagents) only, no
network call and no build-strength run of any kind: "the issues a session closed" is counted
from its own main thread's successful ``gh issue close N`` calls, not fetched live. Money to
the cent; every duration prints through ``tools.agentops.core_format.human_duration``.

The set: the finished ``worker`` / ``worker-strong`` subagent spawns of the ORCH sessions
started in [--since, --until] (or named, one id or id prefix per line, in a --sessions
file), the first --spawns of them by start time when that flag is given. A session still
producing turns after --until is "live": every spawn of a live session is left out of the
set and counted apart, but its own main-chat turns up to --until still count towards the
whole-cost total below (that total is not the spawn set, but everything its ORCH sessions
cost: their own thread plus every subagent).

One turn is every jsonl line sharing one assistant ``message.id`` (model/timestamp from the
first line, usage from the LAST line that carries one -- a later line with no usage is
skipped, not treated as zero --, every line's tool-use blocks merged); this matches
``scripts/token_meter.py``'s own turn definition (``_update_usage``) so the two tools price
identically.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from tools.agentops.core_format import human_duration  # noqa: E402
from tools.agentops.dispatch import EVENTS_FILE as DEFAULT_EVENTS_FILE  # noqa: E402
from tools.agentops.merge import MERGE_LOG_PATH as DEFAULT_MERGE_LOG_PATH  # noqa: E402

import token_meter as tm  # noqa: E402

EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}
READ_TOOLS = {"Read", "Grep", "Glob"}
SHELL_TOOLS = {"Bash", "PowerShell"}
SPAWN_AGENT_TYPES = {"worker", "worker-strong"}

PLACES_TO_READ_HEADING = "Places to read"
CONTEXT_HANDBACK_PREFIX = "Context hand-back"

DEFAULT_ORCH_TITLE_PATTERN = r"orch"
DEFAULT_BRAIN_TITLE_PATTERN = r"opus|fable"

EPOCH = datetime.min.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------- one shell call: read? ----
#
# Only "is this shell call a plain read" matters here (an edit-through-shell call, a test
# run, a git/gh/wait call and everything else all fold into "other" for the ratio below), so
# this is a reduced classifier: it must agree with a full read/write/other split on the
# "read" branch, not reproduce every other branch's label.

_SEP = re.compile(r"&&|\|\||;|\||\n")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PS_ENV_ASSIGN = re.compile(r"^\$env:[A-Za-z_]", re.IGNORECASE)
_SKIP_HEADS = {"cd", "export", "set", "set-location", "pushd", "popd", "source", "."}

_WRITE_PATTERNS = [
    re.compile(r"<<-?\s*'?\"?[A-Za-z_]"),  # heredoc
    re.compile(r">>\s*[^\s&|]"),
    re.compile(r"(?<![0-9>])>\s*[^\s&|>]"),
    re.compile(r"\bSet-Content\b", re.IGNORECASE),
    re.compile(r"\bAdd-Content\b", re.IGNORECASE),
    re.compile(r"\bOut-File\b", re.IGNORECASE),
    re.compile(r"\bNew-Item\b", re.IGNORECASE),
    re.compile(r"\bsed\b[^|]*-i\b"),
    re.compile(r"\btee\b"),
    re.compile(r"\bgit apply\b"),
    re.compile(r"\bpatch\b\s+-"),
]
_NULL_SINK = re.compile(r">\s*(/dev/null|\$null|NUL|nul)\b", re.IGNORECASE)

_TEST_HEADS = {"pytest", "py.test"}
_TEST_PATTERNS = [
    re.compile(r"^(python|python3|py|\S*python(\.exe)?)\s+(-\w+\s+)*-m\s+(pytest|unittest)\b"),
    re.compile(r"^pytest\b"),
    re.compile(r"\bpytest\b.*\btests?[/\\]"),
]
_PY_HEADS = {"python", "python3", "py", "python.exe", "uv", "poetry"}
_NODE_HEADS = {"node", "npm", "npx", "pnpm", "yarn"}
_NON_READ_HEADS = _TEST_HEADS | {"git"} | {"gh"} | {
    "sleep", "start-sleep", "timeout", "until", "while", "wait", "waitfor",
} | _PY_HEADS | _NODE_HEADS
_READ_HEADS = {
    "cat", "head", "tail", "grep", "rg", "ls", "find", "wc", "less", "more", "type",
    "get-content", "select-string", "get-childitem", "gc", "dir", "gci", "nl", "column",
    "jq", "diff", "file", "stat", "du", "tree", "resolve-path", "measure-object",
}


def _strip_heredoc_body(cmd: str) -> str:
    m = re.search(r"<<-?\s*'?\"?([A-Za-z_][A-Za-z0-9_]*)'?\"?", cmd)
    return cmd if not m else cmd[: m.end()]


def _head_of(segment: str) -> str:
    seg = segment.strip()
    if not seg:
        return ""
    tok = seg.split()[0].strip("(){}\"'`")
    return tok.lower()


def _effective_segment(cmd: str) -> str:
    """The first segment of a chained shell call that is not cd/export/set/env=."""

    body = _strip_heredoc_body(cmd)
    raw = [s for s in _SEP.split(body) if s.strip()]
    for seg in raw:
        h = _head_of(seg)
        if not h:
            continue
        if h in _SKIP_HEADS or _ENV_ASSIGN.match(seg.strip()) or _PS_ENV_ASSIGN.match(seg.strip()):
            continue
        return seg
    return raw[0] if raw else ""


def _writes_a_file(segment: str) -> bool:
    for pat in _WRITE_PATTERNS:
        for m in pat.finditer(segment):
            tail = segment[m.start(): m.start() + 40]
            if _NULL_SINK.search(tail):
                continue
            return True
    return False


def shell_call_is_read(command: str) -> bool:
    """True when one shell call's own classification is a plain read."""

    seg = _effective_segment(command or "")
    if _writes_a_file(seg):
        return False
    stripped = seg.strip()
    for pat in _TEST_PATTERNS:
        if pat.search(stripped):
            return False
    h = _head_of(seg)
    if h in _NON_READ_HEADS or h.endswith("python") or h.endswith("python.exe"):
        return False
    return h in _READ_HEADS or h == "sed"


def call_kind(name: str, inp: dict) -> str:
    """'edit' / 'read' / 'other' for one tool call (the only three kinds this file needs)."""

    if name in EDIT_TOOLS:
        return "edit"
    if name in READ_TOOLS:
        return "read"
    if name in SHELL_TOOLS and shell_call_is_read(inp.get("command") or ""):
        return "read"
    return "other"


# -------------------------------------------------------------------------- one spawn ----


@dataclass
class SpawnTurn:
    turn_index: int
    message_id: str
    model: str | None
    usage: dict
    timestamp: datetime | None
    tool_calls: list = field(default_factory=list)

    @property
    def context(self) -> int:
        u = self.usage
        return (
            int(u.get("input_tokens") or 0)
            + int(u.get("cache_creation_input_tokens") or 0)
            + int(u.get("cache_read_input_tokens") or 0)
        )


@dataclass
class Spawn:
    session: str
    path: Path
    agent_type: str | None
    issue: int | None
    turns: list  # list[SpawnTurn]
    first_edit: int | None
    first_prompt: str
    last_message: str
    cost: float
    started: datetime | None


def _turn_like(model: str | None, usage: dict) -> tm.Turn:
    return tm.Turn(
        session_id="", agent_id=None, agent_type=None, message_id="", model=model,
        timestamp=None,
        input_tokens=int(usage.get("input_tokens") or 0),
        cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        cache_read_input_tokens=int(usage.get("cache_read_input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        issue=None, title=None, turn_index=0,
        cache_creation_1h_tokens=tm.cache_creation_1h(usage),
    )


def parse_spawn(path: Path) -> tuple[int | None, list[SpawnTurn], str, str]:
    """-> (issue named in the first prompt, turns, first prompt text, last message text)."""

    turns_by_id: dict[str, SpawnTurn] = {}
    text_by_id: dict[str, str] = {}
    order: list[str] = []
    issue: int | None = None
    first_prompt = ""
    first_prompt_seen = False

    for obj in tm._read_jsonl(path):
        kind = obj.get("type")
        if kind == "user" and not first_prompt_seen:
            first_prompt = tm.message_text(obj.get("message") or {})
            nums = tm.all_issues(first_prompt)
            issue = nums[0] if nums else None
            first_prompt_seen = True
            continue
        if kind != "assistant":
            continue
        message = obj.get("message") or {}
        message_id = message.get("id")
        if not message_id:
            continue
        content = message.get("content")
        calls = []
        text_piece = ""
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    calls.append((block.get("name"), block.get("input") or {}))
                elif block.get("type") == "text":
                    text_piece += block.get("text", "")
        if message_id not in turns_by_id:
            usage = message.get("usage") or {}
            if not usage:
                continue
            turns_by_id[message_id] = SpawnTurn(
                turn_index=len(order),
                message_id=message_id,
                model=message.get("model"),
                usage=usage,
                timestamp=tm.parse_timestamp(obj.get("timestamp")),
            )
            order.append(message_id)
            text_by_id[message_id] = ""
        else:
            # #259/#262: a later line sharing this message.id carries the turn's real usage
            # (a subagent's first line only holds the tokens written so far); reuse the token
            # meter's own last-line-wins rule via its own function, not a new copy.
            existing = turns_by_id[message_id]
            shim = _turn_like(existing.model, existing.usage)
            tm._update_usage(shim, message)
            existing.usage = {
                "input_tokens": shim.input_tokens,
                "cache_creation_input_tokens": shim.cache_creation_input_tokens,
                "cache_read_input_tokens": shim.cache_read_input_tokens,
                "output_tokens": shim.output_tokens,
                "cache_creation": {"ephemeral_1h_input_tokens": shim.cache_creation_1h_tokens},
            }
        turns_by_id[message_id].tool_calls.extend(calls)
        text_by_id[message_id] += text_piece

    turns = [turns_by_id[mid] for mid in order]
    last_message = text_by_id[order[-1]] if order else ""
    return issue, turns, first_prompt, last_message


def load_spawn(path: Path, meta: dict, prices: dict) -> Spawn:
    session = path.parent.parent.name
    issue, turns, first_prompt, last_message = parse_spawn(path)
    first_edit = None
    for t in turns:
        if any(n in EDIT_TOOLS for n, _ in t.tool_calls):
            first_edit = t.turn_index
            break
    cost = 0.0
    for t in turns:
        c = tm.turn_cost(_turn_like(t.model, t.usage), prices)
        if c is not None:
            cost += c
    started = turns[0].timestamp if turns else None
    return Spawn(
        session=session, path=path, agent_type=meta.get("agentType"), issue=issue,
        turns=turns, first_edit=first_edit, first_prompt=first_prompt,
        last_message=last_message, cost=cost, started=started,
    )


# ---------------------------------------------------------------------- session scoping ----


def load_sessions_file(path: Path) -> list[str]:
    ids = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ids.append(line)
    return ids


def _resolve_session_path(short_id: str, projects_dir: Path) -> Path | None:
    matches = sorted(projects_dir.glob(short_id + "*.jsonl"))
    return matches[0] if matches else None


def discover_target_sessions(
    projects_dir: Path, since: datetime, until: datetime, sessions_file: Path | None,
) -> list[tuple[str, Path]]:
    """-> [(session_id, main-transcript path), ...], one entry per ORCH session of the set."""

    if sessions_file is not None:
        out = []
        for short_id in load_sessions_file(sessions_file):
            path = _resolve_session_path(short_id, projects_dir)
            if path is not None:
                out.append((path.stem, path))
        return out

    out = []
    for path in tm.discover_main_transcripts(projects_dir):
        title, first_message = tm._peek_title_and_first_message(path)
        chat = tm.classify_chat(
            title, brain_pattern=DEFAULT_BRAIN_TITLE_PATTERN, orch_pattern=DEFAULT_ORCH_TITLE_PATTERN
        )
        if chat != "ORCH":
            continue
        parsed = tm.parse_main_transcript(path)
        if not parsed.turns:
            continue
        first_ts = parsed.turns[0].timestamp
        if first_ts is None or not (since <= first_ts <= until):
            continue
        out.append((path.stem, path))
    return out


def session_is_live(main_path: Path, until: datetime) -> bool:
    """A session is live at ``until`` when its own transcript still has a turn after it."""

    last_ts = last_turn_time(main_path)
    return last_ts is not None and last_ts > until


def last_turn_time(path: Path) -> datetime | None:
    """The time of the last timestamped record of a transcript, ``None`` when it has none."""

    last_ts = None
    for obj in tm._read_jsonl(path):
        ts = tm.parse_timestamp(obj.get("timestamp"))
        if ts is not None:
            last_ts = ts
    return last_ts


def gather_spawns(
    projects_dir: Path,
    target_sessions: list[tuple[str, Path]],
    since: datetime,
    until: datetime,
    prices: dict,
) -> tuple[list[Spawn], int, dict[str, bool]]:
    live = {sid: session_is_live(path, until) for sid, path in target_sessions}
    session_ids = set(live)
    spawns: list[Spawn] = []
    live_excluded = 0
    for jsonl_path, meta in tm.discover_subagent_transcripts(projects_dir):
        if meta.get("agentType") not in SPAWN_AGENT_TYPES:
            continue
        session = jsonl_path.parent.parent.name
        if session not in session_ids:
            continue
        if live.get(session):
            live_excluded += 1
            continue
        spawn = load_spawn(jsonl_path, meta, prices)
        if not spawn.turns:
            continue
        # its session is already in the window (or named explicitly); only the upper bound
        # still applies here, matching a live session's own cut -- a lower bound would wrongly
        # drop a spawn of a session whose OWN start is the window's very edge.
        if spawn.started is None or spawn.started > until:
            continue
        spawns.append(spawn)
    return spawns, live_excluded, live


def apply_spawns_limit(spawns: list[Spawn], limit: int | None) -> list[Spawn]:
    if limit is None:
        return spawns
    ordered = sorted(spawns, key=lambda s: s.started or EPOCH)
    return ordered[:limit]


# ----------------------------------------------------------------------- the indicators ----


def spawns_cost_by_type(spawns: list[Spawn]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for s in spawns:
        d = out.setdefault(s.agent_type or "(unknown)", {"n": 0, "cost": 0.0})
        d["n"] += 1
        d["cost"] += s.cost
    return out


def median_context_at_first_edit(spawns: list[Spawn]) -> dict[str, tuple]:
    """-> {agent_type: (median or None, n_edited, n_no_edit)}."""

    by_type: dict[str, dict] = {}
    for s in spawns:
        d = by_type.setdefault(s.agent_type or "(unknown)", {"ctx": [], "no_edit": 0})
        if s.first_edit is None:
            d["no_edit"] += 1
        else:
            d["ctx"].append(s.turns[s.first_edit].context)
    out = {}
    for atype, d in by_type.items():
        med = statistics.median(d["ctx"]) if d["ctx"] else None
        out[atype] = (med, len(d["ctx"]), d["no_edit"])
    return out


def tool_calls_per_turn(spawns: list[Spawn]) -> tuple[float | None, dict]:
    """The edit-turns and read-turns after each spawn's first edit: calls / turns."""

    counts = {"edit": {"calls": 0, "turns": 0}, "read": {"calls": 0, "turns": 0}}
    for s in spawns:
        if s.first_edit is None:
            continue
        for t in s.turns:
            if t.turn_index < s.first_edit:
                continue
            kinds_here = set()
            for name, inp in t.tool_calls:
                kind = call_kind(name, inp)
                if kind in counts:
                    counts[kind]["calls"] += 1
                    kinds_here.add(kind)
            for kind in kinds_here:
                counts[kind]["turns"] += 1
    total_calls = counts["edit"]["calls"] + counts["read"]["calls"]
    total_turns = counts["edit"]["turns"] + counts["read"]["turns"]
    ratio = (total_calls / total_turns) if total_turns else None
    return ratio, counts


def cost_share_above(spawns: list[Spawn], threshold: int, prices: dict) -> float | None:
    total = 0.0
    above = 0.0
    for s in spawns:
        for t in s.turns:
            c = tm.turn_cost(_turn_like(t.model, t.usage), prices) or 0.0
            total += c
            if t.context > threshold:
                above += c
    return (above / total) if total else None


def places_to_read_count(spawns: list[Spawn]) -> int:
    return sum(1 for s in spawns if PLACES_TO_READ_HEADING in s.first_prompt)


def context_handback(spawns: list[Spawn]) -> tuple[int, list[float]]:
    """-> (n whose last message opens with the heading, minutes to the next spawn on the
    same issue, for each hand-back that has one in the set)."""

    n = sum(1 for s in spawns if s.last_message.lstrip().startswith(CONTEXT_HANDBACK_PREFIX))
    by_issue: dict[int | None, list[Spawn]] = {}
    for s in spawns:
        by_issue.setdefault(s.issue, []).append(s)
    gaps_minutes: list[float] = []
    for issue, rows in by_issue.items():
        if issue is None or len(rows) < 2:
            continue
        rows.sort(key=lambda s: s.started or EPOCH)
        for i, s in enumerate(rows[:-1]):
            if not s.last_message.lstrip().startswith(CONTEXT_HANDBACK_PREFIX):
                continue
            if not s.turns:
                continue
            t0 = s.turns[-1].timestamp
            if t0 is None:
                continue
            # the first spawn that STARTED after this hand-back's last turn; spawns that
            # overlap it in time are not "the next one" (no negative gap).
            nxt = None
            for cand in rows[i + 1:]:
                if not cand.turns:
                    continue
                began = cand.started or cand.turns[0].timestamp
                if began is not None and began > t0 and cand.turns[0].timestamp is not None:
                    nxt = cand
                    break
            if nxt is None:
                continue
            gap = (nxt.turns[0].timestamp - t0).total_seconds() / 60.0
            if gap >= 0:
                gaps_minutes.append(gap)
    return n, gaps_minutes


#: gh's own success line for a close, e.g. ``Closed issue <owner>/<repo>N`` marked with gh's
#: leading check mark. Only a number appearing in a line of this shape, in the call's own tool
#: result, is ever credited -- never a number that merely appears in the command text (a
#: close's own --comment routinely quotes another 'gh issue close N' as prose, e.g. citing a
#: past mistake).
CLOSED_OUTPUT_RE = re.compile(r"✓\s*Closed issue\s+\S+#(\d+)")


#: gh's success line for a reopen, same shape as ``CLOSED_OUTPUT_RE``.
REOPENED_OUTPUT_RE = re.compile(r"✓\s*Reopened issue\s+\S+#(\d+)")


def _tool_result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
        return "\n".join(parts)
    return ""


def session_gh_events(main_path: Path, until: datetime) -> list[tuple[datetime, str, int]]:
    """-> [(timestamp, 'close' | 'reopen', issue number)] of the successful gh calls of one
    session's main thread, in time order, cut at
    --until. A call counts as successful when its own tool result carries no ``is_error``, and
    the numbers it credits are exactly those in that result's own gh success lines
    (``CLOSED_OUTPUT_RE``), never numbers that only appear in the command text."""

    pending: dict[str, datetime | None] = {}
    events: list[tuple[datetime, str, int]] = []
    for obj in tm._read_jsonl(main_path):
        kind = obj.get("type")
        if kind == "assistant":
            message = obj.get("message") or {}
            content = message.get("content")
            ts = tm.parse_timestamp(obj.get("timestamp"))
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    if block.get("name") not in SHELL_TOOLS:
                        continue
                    pending[block.get("id")] = ts
        elif kind == "user":
            message = obj.get("message") or {}
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_use_id = block.get("tool_use_id")
                if tool_use_id not in pending:
                    continue
                ts = pending[tool_use_id]
                if block.get("is_error"):
                    continue
                if ts is not None and ts > until:
                    continue
                text = _tool_result_text(block)
                when = ts if ts is not None else until
                for n in CLOSED_OUTPUT_RE.findall(text):
                    events.append((when, "close", int(n)))
                for n in REOPENED_OUTPUT_RE.findall(text):
                    events.append((when, "reopen", int(n)))
    events.sort(key=lambda e: e[0])
    return events


def _closes_from_events(events: list[tuple[datetime, str, int]]) -> dict[int, datetime]:
    """-> {issue: earliest close after its last reopen}; an issue whose last event is a reopen
    is absent."""

    closes: dict[int, datetime] = {}
    for when, kind, n in events:
        if kind == "reopen":
            closes.pop(n, None)
        elif n not in closes:
            closes[n] = when
    return closes


CUT_TABLE_HEADER_RE = re.compile(r"\|\s*unit\s*\|\s*outcome\s*\|", re.IGNORECASE)
CUT_TABLE_ROW_RE = re.compile(r"^\s*\|\s*(\d+)\s*of\s*(\d+)", re.MULTILINE)
GH_COMMENT_RE = re.compile(r"gh\s+issue\s+comment\s+(\d+)")


def parse_cut_table(command: str) -> tuple[int, set[int], int] | None:
    """-> (issue, {k of the rows}, n) when a shell command posts ORCH's cut table (a markdown
    table headed ``| unit | outcome |`` whose rows start ``k of n``) with ``gh issue comment
    N``; the table may sit inline in --body or in a heredoc of the same command. ``n`` is the
    largest ``n`` the rows name. ``None`` otherwise."""

    m = GH_COMMENT_RE.search(command or "")
    header = CUT_TABLE_HEADER_RE.search(command or "")
    if m is None or header is None:
        return None
    rows = CUT_TABLE_ROW_RE.findall(command[header.end():])
    if not rows:
        return None
    return int(m.group(1)), {int(k) for k, _n in rows}, max(int(n) for _k, n in rows)


def session_cut_tables(main_path: Path, until: datetime) -> list[tuple[datetime, int, set[int], int]]:
    """-> [(timestamp, issue, {k}, n)] of every cut table posted by the shell calls of one
    session's main thread, in time order, cut at --until."""

    out: list[tuple[datetime, int, set[int], int]] = []
    for obj in tm._read_jsonl(main_path):
        if obj.get("type") != "assistant":
            continue
        content = (obj.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        ts = tm.parse_timestamp(obj.get("timestamp"))
        if ts is not None and ts > until:
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") not in SHELL_TOOLS:
                continue
            parsed = parse_cut_table((block.get("input") or {}).get("command") or "")
            if parsed is not None:
                issue, ks, n = parsed
                out.append((ts or until, issue, ks, n))
    out.sort(key=lambda e: e[0])
    return out


def cut_tables_by_issue(
    target_sessions: list[tuple[str, Path]], until: datetime,
) -> dict[int, tuple[set[int], int]]:
    """-> {issue: ({k}, n)} of its latest cut table across the sessions of the set."""

    events: list[tuple[datetime, int, set[int], int]] = []
    for _sid, path in target_sessions:
        events.extend(session_cut_tables(path, until))
    events.sort(key=lambda e: e[0])
    return {issue: (ks, n) for _ts, issue, ks, n in events}


def session_gh_closes(main_path: Path, until: datetime) -> dict[int, datetime]:
    """-> {issue number: its earliest successful 'gh issue close N' call timestamp}, cut at
    --until; a close this session itself reopened later is not counted (the reopen of another
    session is read by ``resolved_closes``)."""

    return _closes_from_events(session_gh_events(main_path, until))


def scan_reopens(
    projects_dir: Path, since: datetime, until: datetime,
) -> list[tuple[datetime, int]]:
    """-> [(timestamp, issue)] of every successful reopen in ANY main-thread transcript of the
    projects dir (a reopen is a fact about the issue, whoever made it). A file last written
    before ``since`` cannot hold one inside the window and is skipped."""

    out: list[tuple[datetime, int]] = []
    for path in tm.discover_main_transcripts(projects_dir):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < since:
            continue
        for when, kind, n in session_gh_events(path, until):
            if kind == "reopen":
                out.append((when, n))
    return out


def resolved_closes(
    target_sessions: list[tuple[str, Path]], until: datetime,
    extra_reopens: list[tuple[datetime, int]] | None = None,
) -> dict[int, tuple[datetime, str]]:
    """-> {issue: (timestamp, session id) of its close}, the events of every session of the set
    read in time order: a reopen drops the issue, a later close (by any session) counts it
    again, for the session of that close. Only a reopen found in these transcripts is seen."""

    events: list[tuple[datetime, str, str, int]] = []
    for when, n in extra_reopens or []:
        events.append((when, "", "reopen", n))
    for sid, path in target_sessions:
        for when, kind, n in session_gh_events(path, until):
            events.append((when, sid, kind, n))
    events.sort(key=lambda e: e[0])
    out: dict[int, tuple[datetime, str]] = {}
    for when, sid, kind, n in events:
        if kind == "reopen":
            out.pop(n, None)
        elif n not in out:
            out[n] = (when, sid)
    return out


def session_close_lists(
    target_sessions: list[tuple[str, Path]], until: datetime,
    extra_reopens: list[tuple[datetime, int]] | None = None,
) -> list[tuple[str, list[int]]]:
    """-> [(session id, [issue numbers it closed, in the order it closed them]), ...], one
    entry per session of the set, by the same rule as ``session_gh_closes``."""

    resolved = resolved_closes(target_sessions, until, extra_reopens)
    out: list[tuple[str, list[int]]] = []
    for sid, _path in target_sessions:
        mine = [(n, ts) for n, (ts, who) in resolved.items() if who == sid]
        out.append((sid, [n for n, _ts in sorted(mine, key=lambda kv: kv[1])]))
    return out


def gh_closed_issues(
    target_sessions: list[tuple[str, Path]], until: datetime, issues_limit: int | None,
    extra_reopens: list[tuple[datetime, int]] | None = None,
) -> dict[int, datetime]:
    """-> {issue number: earliest close timestamp} across every session's own main thread,
    the first --issues of them by that timestamp when the limit is given."""

    closes = {n: ts for n, (ts, _sid) in resolved_closes(target_sessions, until, extra_reopens).items()}
    if issues_limit is None:
        return closes
    ordered = sorted(closes.items(), key=lambda kv: kv[1])[:issues_limit]
    return dict(ordered)


def researcher_find_ratio(
    projects_dir: Path, session_ids: set[str], live: dict[str, bool], prices: dict, n_worker_spawns: int,
) -> tuple[int, float, float | None]:
    """-> (n researcher-find spawns, their total cost, ratio to one worker spawn)."""

    n_rf = 0
    cost_rf = 0.0
    for jsonl_path, meta in tm.discover_subagent_transcripts(projects_dir):
        if meta.get("agentType") != "researcher-find":
            continue
        session = jsonl_path.parent.parent.name
        if session not in session_ids or live.get(session):
            continue
        _issue, turns, _first_prompt, _last = parse_spawn(jsonl_path)
        n_rf += 1
        for t in turns:
            c = tm.turn_cost(_turn_like(t.model, t.usage), prices)
            if c is not None:
                cost_rf += c
    ratio = (n_rf / n_worker_spawns) if n_worker_spawns else None
    return n_rf, cost_rf, ratio


# -------------------------------------------------------------------- the whole cost ----


@dataclass
class WholeCost:
    total_cost: float
    n_closed_issues: int
    mean_per_issue: float | None
    median_per_issue: float | None
    unattributed_share: float
    unattributed_ctx: int
    total_ctx: int
    n_attributed_issues: int = 0


def whole_cost_section(
    projects_dir: Path,
    target_sessions: list[tuple[str, Path]],
    until: datetime,
    prices: dict,
    issues_limit: int | None,
    *,
    since: datetime | None = None,
    dispatch_out: Path | None = None,
    orch_session_json: Path | None = None,
) -> WholeCost:
    session_ids = {sid for sid, _ in target_sessions}
    dispatch_offers = tm.load_dispatch_offers(dispatch_out or tm.DEFAULT_DISPATCH_OUT)
    orch_session_held = tm.load_orch_session_held(orch_session_json or tm.DEFAULT_ORCH_SESSION_JSON)
    result = tm.scan_projects_dir(
        projects_dir, dispatch_offers=dispatch_offers, orch_session_held=orch_session_held
    )
    # every turn of every session of the set counts here, own thread and every subagent alike
    # (unlike the spawn indicators above, which leave a live session's spawns out entirely: a
    # live session's own thread is cut at --until, not dropped, and this total follows the
    # same rule for its subagents rather than losing their cost outright).
    scope_turns = [t for t in result.turns if t.session_id in session_ids]
    # its sessions are already the window (or named explicitly); only the upper bound still
    # applies here, the same "cut, not excluded" rule a live session's own thread gets.
    total_cost = sum(
        tm.turn_cost(t, prices) or 0.0
        for t in scope_turns
        if t.timestamp is not None and t.timestamp <= until
    )
    # "the issues they closed": a successful 'gh issue close N' Bash call in a session's own
    # main thread, up to --until (not GitHub's closed-issue list, not the token meter's own
    # attribution -- that stays for the median row below).
    reopens = scan_reopens(projects_dir, since, until) if since is not None else None
    closes = gh_closed_issues(target_sessions, until, issues_limit, reopens)
    n_issues = len(closes)
    mean = (total_cost / n_issues) if n_issues else None
    closed_issues = [
        {"number": number, "closedAt": ts.isoformat()} for number, ts in closes.items()
    ]
    issue_costs = tm.cost_per_closed_issue(scope_turns, closed_issues, prices, since=EPOCH, until=until)
    median = statistics.median(ic.cost_usd for ic in issue_costs) if issue_costs else None
    share, unattributed_ctx, total_ctx = tm.unattributed_share(scope_turns, since=EPOCH, until=until)
    return WholeCost(
        total_cost=total_cost, n_closed_issues=n_issues, mean_per_issue=mean,
        median_per_issue=median, unattributed_share=share, unattributed_ctx=unattributed_ctx,
        total_ctx=total_ctx, n_attributed_issues=len(issue_costs),
    )


# --------------------------------------------------------- ORCH indicators (merge/events) ----
#
# Ticket 3 of 3 (further-ORCH indicators): conflicts per 20 closed issues, time per
# conflict, waits for a further ORCH, the peak of live ORCH sessions -- from the merge
# command's own log (``tools.agentops.merge``) and the dispatcher's own event stream
# (``tools.agentops.dispatch``), never a transcript.

CONFLICT_RATE_ALERT = 3.0
FURTHER_ORCH_WAIT_ALERT_SECONDS = 2 * 60


def load_jsonl_records(path: Path) -> list[dict]:
    """Every JSONL line of ``path``, in file order; an absent file is an empty list."""
    if not path.exists():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        records.append(json.loads(line))
    return records


#: ``tools.agentops.merge``'s own line: ``"{now()} | {branch} | {reason} | suite {duration} |
#: {n} failed | output {path}"`` (``_append_merge_log``, #212). One line per attempt, whatever
#: the outcome; ``now()`` is ``"%Y-%m-%d %H:%M:%S"``, always this line's own end time -- #212's
#: log carries no separate start time, so both ``ts_start`` and ``ts_end`` below read the same
#: instant (the spec's own call: "its single time is fine as the end").
MERGE_LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| (?P<branch>[^|]+) \| (?P<reason>[^|]+) \| "
    r"suite (?P<suite>[^|]+) \| (?P<failed>\d+) failed \| output (?P<output>.+)$"
)

#: ``MergeResult.reason`` (as written verbatim into #212's log line) -> the outcome vocabulary
#: ``conflict_indicators`` reads (``merged`` / ``conflict`` / ``red_suite``); a reason not
#: listed here (``dirty``, ``fetch-failed``, ``sync-failed``, ``rollback-refused``,
#: ``push-refused``, ...) passes through as-is -- ``conflict_indicators`` only ever looks for
#: ``conflict``, ``merged`` and ``red_suite``.
MERGE_LOG_OUTCOME = {
    "pushed": "merged",
    "conflict": "conflict",
    "rebase-conflict": "conflict",
    "red-suite": "red_suite",
    "red-on-branch": "red_suite",  # a branch sent back for its own failing tests while it waited
}


def _local_tzinfo():
    """The machine's own local UTC offset (a fixed point in time is enough: merge-log lines are
    never far enough apart to cross a DST change within one file read). Its own function so a
    test can monkeypatch a fixed offset instead of depending on the machine's zone."""
    return datetime.now().astimezone().tzinfo


def parse_merge_log_line(line: str) -> dict | None:
    """One line of #212's ``var/agentops/merge.log`` -> ``{ts_start, ts_end, branch, outcome}``
    (``conflict_indicators``'s own record shape); ``None`` for a line that does not match (an
    empty line, a stray write).

    ``tools.agentops.util.now()`` writes this line's timestamp with ``time.strftime`` -- naive
    *local* time, not UTC. Parsed naively and tagged ``+00:00`` it lands hours in the future (or
    past) of a UTC period filter, so it is read as naive local, given the machine's own local
    tzinfo (``_local_tzinfo``), then re-rendered in UTC to match every other timestamp this module
    produces (#192)."""

    match = MERGE_LOG_LINE_RE.match(line.strip())
    if match is None:
        return None
    naive_local = datetime.strptime(match.group("ts"), "%Y-%m-%d %H:%M:%S")
    ts_iso = naive_local.replace(tzinfo=_local_tzinfo()).astimezone(timezone.utc).isoformat()
    branch = match.group("branch").strip()
    reason = match.group("reason").strip()
    outcome = MERGE_LOG_OUTCOME.get(reason, reason)
    return {"ts_start": ts_iso, "ts_end": ts_iso, "branch": branch, "outcome": outcome}


def load_merge_log(path: Path) -> list[dict]:
    """Every attempt of #212's own merge log, parsed into ``conflict_indicators``'s record
    shape; an absent file or an unparsable line is skipped, never raised."""

    if not path.exists():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = parse_merge_log_line(line)
        if record is not None:
            records.append(record)
    return records


def _parse_record_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class ConflictIndicators:
    n_conflicts: int
    per_20_closed: float | None
    median_seconds: float | None
    longest_seconds: float | None
    any_without_green_suite: bool


def conflict_indicators(
    merge_log: list[dict], since: datetime, until: datetime, n_closed_issues: int
) -> ConflictIndicators:
    """Every attempt is grouped by branch and sorted by ``ts_start``; a conflict's time to
    resolve is the next attempt on the same branch's ``ts_end`` minus the conflict's own
    ``ts_end``, counted in the median/longest only when that next attempt is ``merged``. A
    next attempt of ``red_suite`` marks the conflict as ended without a green suite (out of
    the median/longest, still counted in the rate); no later attempt at all still counts in
    the rate alone. Only a conflict whose own ``ts_end`` falls in [since, until] counts."""

    by_branch: dict[str, list[dict]] = {}
    for entry in merge_log:
        by_branch.setdefault(entry["branch"], []).append(entry)
    for entries in by_branch.values():
        entries.sort(key=lambda e: _parse_record_ts(e["ts_start"]))

    n_conflicts = 0
    resolve_seconds: list[float] = []
    any_without_green_suite = False
    for entries in by_branch.values():
        for i, entry in enumerate(entries):
            if entry.get("outcome") != "conflict":
                continue
            ts_end = _parse_record_ts(entry["ts_end"])
            if not (since <= ts_end <= until):
                continue
            n_conflicts += 1
            later = entries[i + 1:]
            if not later:
                continue
            nxt = later[0]
            if nxt.get("outcome") == "merged":
                resolve_seconds.append((_parse_record_ts(nxt["ts_end"]) - ts_end).total_seconds())
            elif nxt.get("outcome") == "red_suite":
                any_without_green_suite = True

    per_20 = (n_conflicts / n_closed_issues * 20) if n_closed_issues else None
    median = statistics.median(resolve_seconds) if resolve_seconds else None
    longest = max(resolve_seconds) if resolve_seconds else None
    return ConflictIndicators(
        n_conflicts=n_conflicts, per_20_closed=per_20, median_seconds=median,
        longest_seconds=longest, any_without_green_suite=any_without_green_suite,
    )


@dataclass
class WaitIndicators:
    n_waited: int
    longest_seconds: float | None


def further_orch_waits(
    events: list[dict], since: datetime, until: datetime,
    threshold_seconds: float = FURTHER_ORCH_WAIT_ALERT_SECONDS,
) -> WaitIndicators:
    """Pairs ``issue_ready_seen`` -> ``issue_taken`` per issue episode (an episode: one
    ``issue_ready_seen`` and the next ``issue_taken`` on the same issue), counting only a
    gap over ``threshold_seconds``. Both ends of a counted pair must fall in [since, until]."""

    by_issue: dict[int, list[dict]] = {}
    for event in events:
        if event.get("kind") not in ("issue_ready_seen", "issue_taken"):
            continue
        ts = _parse_record_ts(event["ts"])
        if not (since <= ts <= until):
            continue
        by_issue.setdefault(event["issue"], []).append({**event, "_ts": ts})

    n_waited = 0
    longest: float | None = None
    for issue_events in by_issue.values():
        issue_events.sort(key=lambda e: e["_ts"])
        pending_seen: dict | None = None
        for event in issue_events:
            if event["kind"] == "issue_ready_seen":
                pending_seen = event
            elif event["kind"] == "issue_taken" and pending_seen is not None:
                gap = (event["_ts"] - pending_seen["_ts"]).total_seconds()
                if gap > threshold_seconds:
                    n_waited += 1
                    if longest is None or gap > longest:
                        longest = gap
                pending_seen = None
    return WaitIndicators(n_waited=n_waited, longest_seconds=longest)


def conflict_alerts(conflicts: ConflictIndicators) -> list[str]:
    """The marked ``>> for the brain:`` reasons, if any: the rate at or over
    ``CONFLICT_RATE_ALERT``, or any conflict in the period that ended without a green
    suite. Empty when neither holds."""

    alerts: list[str] = []
    if conflicts.per_20_closed is not None and conflicts.per_20_closed >= CONFLICT_RATE_ALERT:
        alerts.append(
            f"{conflicts.per_20_closed:.1f} conflicts per 20 closed issues "
            f"(>= {CONFLICT_RATE_ALERT:.1f})"
        )
    if conflicts.any_without_green_suite:
        alerts.append("a conflict ended without a green suite")
    return alerts


@dataclass
class PeakIndicators:
    peak: int
    n_excluded: int
    excluded_ids: list[str] = field(default_factory=list)


def peak_orch_sessions(
    events: list[dict], since: datetime, until: datetime,
    live_sessions: set[str] | None = None,
    last_turns: dict[str, datetime] | None = None,
) -> PeakIndicators:
    """The highest number of ORCH sessions live at once in [since, until]. A session's end is
    its own ``orch_session_end`` if any; else the latest ``orch_end``/``orch_timeout`` naming
    it (both real events carry ``session``); with neither, it is open to ``until`` only when
    ``live_sessions`` (the session id ``artifacts/runs/orch_session.json`` currently holds, via
    ``--orch-session-json``) names it, otherwise it is left out of the peak and counted in
    ``n_excluded`` -- an `orch_start` with no known end and no live record is stale data, not a
    session open forever. A session with an ``orch_start``, no end event and not listed live ends
    at ``last_turns[session]`` (the last turn of its transcript, read by the caller) when the
    mapping holds it; with none it is left out and its id kept in ``excluded_ids``."""

    starts: dict[str, datetime] = {}
    ends: dict[str, datetime] = {}
    last_named: dict[str, datetime] = {}
    for event in events:
        kind = event.get("kind")
        if kind == "orch_start":
            starts[event["session"]] = _parse_record_ts(event["ts"])
        elif kind == "orch_session_end":
            ends[event["session"]] = _parse_record_ts(event["ts"])
        elif kind in ("orch_end", "orch_timeout"):
            session = event.get("session")
            if session is None:
                continue
            ts = _parse_record_ts(event["ts"])
            if session not in last_named or ts > last_named[session]:
                last_named[session] = ts

    live_sessions = live_sessions or set()
    last_turns = last_turns or {}
    excluded_ids: list[str] = []
    points: list[tuple[datetime, int]] = []
    n_excluded = 0
    for session, start in starts.items():
        if session in ends:
            end = ends[session]
        elif session in last_named:
            end = last_named[session]
        elif session in live_sessions:
            end = until
        elif session in last_turns:
            end = last_turns[session]
        else:
            n_excluded += 1
            excluded_ids.append(session)
            continue
        start = max(start, since)
        end = min(end, until)
        if start > end:
            continue
        points.append((start, 1))
        points.append((end, -1))

    points.sort(key=lambda p: (p[0], -p[1]))  # a start before an end at the same instant
    live = 0
    peak = 0
    for _, delta in points:
        live += delta
        peak = max(peak, live)
    return PeakIndicators(peak=peak, n_excluded=n_excluded, excluded_ids=excluded_ids)


# ------------------------------------------------------------------------------ units ----
#
# #254 unit 3 of 3: per-agent-type turn counts, and per-closed-issue unit accounting, over
# the same spawn selection as every section above (``spawns``, already cut to
# worker/worker-strong and to --spawns when given). A unit is read from the spawn's own
# first prompt's ``Unit: <k> of <n>`` line (#254's own convention); a spawn with none is
# "whole-issue" (unsliced), same as one whose line reads ``Unit: 1 of 1``.

UNIT_LINE_RE = re.compile(r"Unit:\s*(\d+)\s*of\s*(\d+)")


def parse_unit_line(first_prompt: str) -> tuple[int, int] | None:
    """-> (k, n) from the first ``Unit: <k> of <n>`` line of a spawn's first prompt, or
    ``None`` when the prompt carries no such line."""

    match = UNIT_LINE_RE.search(first_prompt or "")
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def turns_by_agent_type(spawns: list[Spawn]) -> dict[str, dict]:
    """-> {agent_type: {n, total_turns, mean, median}}, turns being each spawn's own turn
    count (``len(spawn.turns)``)."""

    by_type: dict[str, list[int]] = {}
    for s in spawns:
        by_type.setdefault(s.agent_type or "(unknown)", []).append(len(s.turns))
    out: dict[str, dict] = {}
    for atype, counts in by_type.items():
        out[atype] = {
            "n": len(counts),
            "total_turns": sum(counts),
            "mean": statistics.mean(counts) if counts else None,
            "median": statistics.median(counts) if counts else None,
        }
    return out


def peak_spawns_alive(spawns: list[Spawn]) -> int:
    """The highest number of spawns of ``spawns`` alive at once; a spawn lives from its own
    first record (``spawn.started``) to its own last (its last turn's timestamp) -- the same
    sweep ``peak_orch_sessions`` runs over ORCH sessions."""

    points: list[tuple[datetime, int]] = []
    for s in spawns:
        if not s.turns or s.started is None:
            continue
        end = s.turns[-1].timestamp
        if end is None:
            continue
        points.append((s.started, 1))
        points.append((end, -1))
    points.sort(key=lambda p: (p[0], -p[1]))
    live = 0
    peak = 0
    for _, delta in points:
        live += delta
        peak = max(peak, live)
    return peak


@dataclass
class IssueUnits:
    issue: int
    n_spawns: int
    n_units: int
    peak_alive: int
    whole_issue_spawns: int
    wall_clock_seconds: float | None
    by_spawn: int = 0
    by_orch: int = 0


def units_by_issue(
    spawns: list[Spawn], closes: dict[int, datetime],
    cut_tables: dict[int, tuple[set[int], int]] | None = None,
) -> list[IssueUnits]:
    """-> one ``IssueUnits`` per issue named in ``spawns`` (a code-worker spawn set, already
    cut to worker/worker-strong), sorted by issue number. ``n_units`` is the count of
    distinct ``k`` read off the issue's spawns' ``Unit: <k> of <n>`` lines, or 1 when none of
    them carry one (the issue ran as its own single unit). A spawn is "whole-issue" when it
    carries no ``Unit:`` line at all, or ``Unit: 1 of 1``. The wall clock is the issue's own
    close (``closes``, absent when the issue never closed in the set) minus the earliest
    ``started`` of its spawns. ``cut_tables`` ({issue: ({k}, n)}, ORCH's posted cut table) gives
    the units of an issue as its ``n`` (an issue with a table and a close but no spawn is listed
    too); a unit is by a spawn when a spawn carried that ``Unit: k``, else by ORCH itself."""

    cut_tables = cut_tables or {}
    by_issue: dict[int, list[Spawn]] = {}
    for s in spawns:
        if s.issue is None:
            continue
        by_issue.setdefault(s.issue, []).append(s)
    for issue in cut_tables:
        if issue in closes:
            by_issue.setdefault(issue, [])

    out: list[IssueUnits] = []
    for issue in sorted(by_issue):
        issue_spawns = by_issue[issue]
        units_seen: set[int] = set()
        whole = 0
        for s in issue_spawns:
            unit = parse_unit_line(s.first_prompt)
            if unit is None:
                whole += 1
                continue
            k, n = unit
            units_seen.add(k)
            if (k, n) == (1, 1):
                whole += 1
        starts = [s.started for s in issue_spawns if s.started is not None]
        first_start = min(starts) if starts else None
        close_ts = closes.get(issue)
        wall_clock = (
            (close_ts - first_start).total_seconds()
            if first_start is not None and close_ts is not None
            else None
        )
        if issue in cut_tables:
            n_units = cut_tables[issue][1]
            by_spawn = len(units_seen & set(range(1, n_units + 1)))
            if issue_spawns and not units_seen:
                by_spawn = 1
            by_spawn = min(by_spawn, n_units)
            by_orch = n_units - by_spawn
        else:
            n_units = len(units_seen) if units_seen else 1
            by_spawn, by_orch = n_units, 0
        out.append(IssueUnits(
            issue=issue,
            n_spawns=len(issue_spawns),
            n_units=n_units,
            by_spawn=by_spawn,
            by_orch=by_orch,
            peak_alive=peak_spawns_alive(issue_spawns),
            whole_issue_spawns=whole,
            wall_clock_seconds=wall_clock,
        ))
    return out


# ------------------------------------------------------------------------- rendering ----


def money(x: float | None) -> str:
    return "-" if x is None else f"${x:,.2f}"


def pct(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:.1f} %"


def build_report(
    *,
    projects_dir: Path,
    prices: dict,
    since: datetime,
    until: datetime,
    sessions_file: Path | None,
    spawns_limit: int | None,
    issues_limit: int | None,
    dispatch_out: Path | None = None,
    orch_session_json: Path | None = None,
    merge_log: Path | None = None,
    events_file: Path | None = None,
    tz: tzinfo | None = None,
) -> str:
    target_sessions = discover_target_sessions(projects_dir, since, until, sessions_file)
    session_ids = {sid for sid, _ in target_sessions}
    all_spawns, live_excluded, live = gather_spawns(projects_dir, target_sessions, since, until, prices)
    spawns = apply_spawns_limit(all_spawns, spawns_limit)

    lines: list[str] = []
    lines.append(
        f"Cost levers: {since.astimezone(tz or tm.local_tzinfo()).isoformat()} .. "
        f"{until.astimezone(tz or tm.local_tzinfo()).isoformat()}, "
        f"{len(target_sessions)} ORCH session(s) of the set "
        f"({sum(1 for v in live.values() if v)} live, cut at --until)"
    )
    lines.append("")

    lines.append("Spawns counted, by agent type, with their cost:")
    by_type = spawns_cost_by_type(spawns)
    total_n = sum(d["n"] for d in by_type.values())
    total_cost = sum(d["cost"] for d in by_type.values())
    for atype in sorted(by_type):
        d = by_type[atype]
        lines.append(f"  {atype:<16} {d['n']:>4} spawns  {money(d['cost'])}")
    lines.append(f"  {'all':<16} {total_n:>4} spawns  {money(total_cost)}")
    lines.append(f"  (spawns of a live session, counted apart: {live_excluded})")
    lines.append("")

    lines.append("Median context at the first edit, per agent type (a spawn with no edit apart):")
    for atype, (med, n_edited, n_no_edit) in sorted(median_context_at_first_edit(spawns).items()):
        med_s = f"{med:,.0f}" if med is not None else "-"
        lines.append(f"  {atype:<16} median {med_s:>10} tokens (n={n_edited}, no edit={n_no_edit})")
    lines.append("")

    ratio, counts = tool_calls_per_turn(spawns)
    lines.append(
        f"Tool calls per turn (edit + read turns at/after the first edit): "
        f"{ratio:.2f}" if ratio is not None else "Tool calls per turn: -"
    )
    lines.append(
        f"  edit {counts['edit']['calls']} calls in {counts['edit']['turns']} turns, "
        f"read {counts['read']['calls']} calls in {counts['read']['turns']} turns"
    )
    lines.append("")

    share150 = cost_share_above(spawns, 150_000, prices)
    share100 = cost_share_above(spawns, 100_000, prices)
    lines.append(
        f"Share of the workers' cost on turns above 150k / 100k of context: "
        f"{pct(share150)} / {pct(share100)}"
    )
    lines.append("")

    n_places = places_to_read_count(spawns)
    lines.append(f"Spawns whose prompt holds the heading '{PLACES_TO_READ_HEADING}': {n_places} of {len(spawns)}")
    n_handback, gaps = context_handback(spawns)
    lines.append(
        f"Spawns whose last message starts with '{CONTEXT_HANDBACK_PREFIX}': "
        f"{n_handback} of {len(spawns)}"
    )
    if gaps:
        for g in gaps:
            lines.append(f"  hand-back to the next spawn on the same issue: {human_duration(g * 60)}")
    lines.append("")

    n_worker = sum(1 for s in spawns if s.agent_type in SPAWN_AGENT_TYPES)
    n_rf, cost_rf, rf_ratio = researcher_find_ratio(projects_dir, session_ids, live, prices, n_worker)
    lines.append(
        f"researcher-find spawns per worker spawn: "
        f"{rf_ratio:.2f}" if rf_ratio is not None else "researcher-find spawns per worker spawn: -"
    )
    lines.append(f"  {n_rf} researcher-find spawn(s), cost {money(cost_rf)}")
    lines.append("")

    whole = whole_cost_section(
        projects_dir, target_sessions, until, prices, issues_limit, since=since,
        dispatch_out=dispatch_out, orch_session_json=orch_session_json,
    )
    lines.append(
        f"Headline: The whole cost per closed issue, mean: {money(whole.mean_per_issue)} "
        f"({money(whole.total_cost)} / {whole.n_closed_issues} closed issue(s))"
    )
    reopens = scan_reopens(projects_dir, since, until)
    counted = gh_closed_issues(target_sessions, until, issues_limit, reopens)
    lines.append("Closed issues counted, per ORCH session:")
    for sid, nums in session_close_lists(target_sessions, until, reopens):
        nums = [n for n in nums if n in counted]
        listed = ", ".join(f"#{n}" for n in nums) if nums else "-"
        lines.append(f"  {sid:<20} {len(nums)} closed: {listed}")
    lines.append(
        f"The same, median per issue (token meter's own attribution): {money(whole.median_per_issue)}, "
        f"covers {whole.n_attributed_issues} of {whole.n_closed_issues} closed issue(s), "
        f"unattributed share {pct(whole.unattributed_share)} "
        f"({whole.unattributed_ctx:,} of {whole.total_ctx:,} context tokens)"
    )
    lines.append("")

    merge_log_records = load_merge_log(merge_log or DEFAULT_MERGE_LOG_PATH)
    events = load_jsonl_records(events_file or DEFAULT_EVENTS_FILE)
    orch_session_held = tm.load_orch_session_held(orch_session_json or tm.DEFAULT_ORCH_SESSION_JSON)
    conflicts = conflict_indicators(merge_log_records, since, until, whole.n_closed_issues)
    waits = further_orch_waits(events, since, until)
    peak = peak_orch_sessions(
        events, since, until, live_sessions=set(orch_session_held),
        last_turns=transcript_last_turns(events, projects_dir),
    )

    rate_s = f"{conflicts.per_20_closed:.1f}" if conflicts.per_20_closed is not None else "-"
    lines.append(
        f"Conflicts per 20 closed issues: {rate_s} "
        f"({conflicts.n_conflicts} conflict(s) over {whole.n_closed_issues} closed issue(s))"
    )
    lines.append(
        f"Time per conflict, median / longest: "
        f"{human_duration(conflicts.median_seconds)} / {human_duration(conflicts.longest_seconds)}"
    )
    lines.append(
        f"Issues that waited for a further ORCH: {waits.n_waited} "
        f"(longest {human_duration(waits.longest_seconds)})"
    )
    peak_line = f"The day's peak of live ORCH sessions: {peak.peak}"
    if peak.n_excluded:
        peak_line += (
            f" ({peak.n_excluded} session(s) left out: no end event, not listed live, "
            f"no transcript: {', '.join(peak.excluded_ids)})"
        )
    lines.append(peak_line)

    alerts = conflict_alerts(conflicts)
    if alerts:
        lines.append(">> for the brain: " + "; ".join(alerts))
    lines.append("")

    lines.append("Units: spawns, turns, turns per spawn (mean/median), by agent type:")
    for atype, d in sorted(turns_by_agent_type(spawns).items()):
        mean_s = f"{d['mean']:.1f}" if d["mean"] is not None else "-"
        median_s = f"{d['median']:.1f}" if d["median"] is not None else "-"
        lines.append(
            f"  {atype:<16} {d['n']:>4} spawns  {d['total_turns']:>6} turns  "
            f"{mean_s:>6}/spawn mean  {median_s:>6}/spawn median"
        )
    lines.append("")

    lines.append(
        "Units per closed issue: worker spawns, units, most alive at once, "
        "whole-issue spawns, wall clock:"
    )
    cut_tables = cut_tables_by_issue(target_sessions, until)
    for iu in units_by_issue(spawns, counted, cut_tables):
        lines.append(
            f"  #{iu.issue:<6} {iu.n_spawns:>3} spawns  {iu.n_units:>3} units  "
            f"peak {iu.peak_alive:>2} alive  whole-issue {iu.whole_issue_spawns:>2} spawns  "
            f"wall clock {human_duration(iu.wall_clock_seconds)}  "
            f"units {iu.n_units} ({iu.by_spawn} by a spawn, {iu.by_orch} by ORCH itself)"
        )

    return "\n".join(lines)


def transcript_last_turns(events: list[dict], projects_dir: Path) -> dict[str, datetime]:
    """{session id of an ``orch_start`` with no end event: the last turn of its transcript}; a
    session with no transcript in ``projects_dir`` is absent."""

    ended = {e.get("session") for e in events if e.get("kind") in ("orch_session_end", "orch_end", "orch_timeout")}
    out: dict[str, datetime] = {}
    for event in events:
        session = event.get("session")
        if event.get("kind") != "orch_start" or session in ended or session in out:
            continue
        path = _resolve_session_path(session, projects_dir)
        last = last_turn_time(path) if path is not None else None
        if last is not None:
            out[session] = last
    return out


LOCAL_CLOCK_HELP = "ISO date/datetime or HH:MM, local clock unless it carries Z or an offset"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", type=str, required=True, help=LOCAL_CLOCK_HELP)
    parser.add_argument("--until", type=str, default=None, help=LOCAL_CLOCK_HELP + "; default now")
    parser.add_argument("--sessions", type=Path, default=None, help="file of ORCH session ids, one per line")
    parser.add_argument("--spawns", type=int, default=None, help="the first N spawns of the set, by start time")
    parser.add_argument("--issues", type=int, default=None, help="the first N closed issues, by closedAt")
    parser.add_argument("--projects-dir", type=Path, default=tm.DEFAULT_PROJECTS_DIR)
    parser.add_argument("--prices", type=Path, default=tm.DEFAULT_PRICES_PATH)
    parser.add_argument("--dispatch-out", type=Path, default=tm.DEFAULT_DISPATCH_OUT)
    parser.add_argument("--orch-session-json", type=Path, default=tm.DEFAULT_ORCH_SESSION_JSON)
    parser.add_argument("--merge-log", type=Path, default=DEFAULT_MERGE_LOG_PATH)
    parser.add_argument("--events-file", type=Path, default=DEFAULT_EVENTS_FILE)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    since = tm.parse_date_arg(args.since)
    until = tm.parse_date_arg(args.until) if args.until else datetime.now(timezone.utc)
    prices = tm.load_prices(args.prices)
    report = build_report(
        projects_dir=args.projects_dir,
        prices=prices,
        since=since,
        until=until,
        sessions_file=args.sessions,
        spawns_limit=args.spawns,
        issues_limit=args.issues,
        dispatch_out=args.dispatch_out,
        orch_session_json=args.orch_session_json,
        merge_log=args.merge_log,
        events_file=args.events_file,
    )
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
