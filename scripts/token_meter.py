"""Token meter: Claude cost per closed issue and the four refactor numbers (#47).

Read-only report over Claude Code transcripts. No product run. Streams every JSONL file
line by line -- a main chat can be 5-14 MB -- and never loads a whole file into memory.

    python scripts/token_meter.py                          # last 14 days, grouped by issue
    python scripts/token_meter.py --by model --since 2026-09-01 --until 2026-09-26
    python scripts/token_meter.py --top 10
    python scripts/token_meter.py --orch-threshold         # ORCH's T = B + N x X (#60)

Inputs (see the issue for the exact rules):
- ``~/.claude/projects/<project>/*.jsonl`` (main chats) and ``*/subagents/*.jsonl`` (subagent
  transcripts, one file per agent, paired with a ``.meta.json`` that carries ``agentType``).
  One turn = one distinct ``message.id`` of an assistant line (lines repeat per content
  block; deduped here). Context of a turn = input + cache_read + cache_creation tokens.
  ``system``/``compact_boundary`` lines count compactions.
- ``artifacts/runs/agentops.jsonl`` (codex-sale runs from the bridge). Optional: the section
  is empty when the file does not exist yet.
- ``config/model_prices.json``: USD per million tokens per model and kind, each entry with
  its source URL and date. A model missing from the table prices as ``None`` and is reported,
  never guessed.

Attribution: a subagent transcript belongs to issue N when its first prompt (first ``user``
line) names exactly one ``#N``. A main-chat turn belongs to the issue named in the latest
``user`` line (this covers cross-session messages too, which arrive as ``user`` lines) at or
before that turn that names exactly one ``#N``; a message naming zero or more-than-one issue
never changes the running attribution. Everything else is "unattributed" and its share is
always printed, never folded into a total silently.

Multi-issue ORCH sessions (#64): a dispatcher-started ORCH session can hold several issues at
once (D104: one ORCH, several subagents in parallel). For such a session:

- A subagent transcript is attributed to the first ``#N`` in its first prompt that is ALSO
  one of its parent ORCH session's held issues (an ORCH worker prompt often repeats every
  held issue in a "code map" aside, so the plain "exactly one #N" rule goes ambiguous on it);
  when no held issue is known, or none of the ``#N`` in the prompt is held, the plain rule
  above still applies.
- The ORCH session's OWN turns split their cost/context over its held issues: equal shares,
  unless a turn has a cheaper, better signal -- the latest ``user`` line at or before it names
  a strict subset of the held issues (e.g. a dispatcher "claimed #56" or "closed #54" line),
  in which case only that subset splits the share. A turn whose latest line names one held
  issue unambiguously keeps the plain single-issue rule (the best signal). Reported ``#N``
  rows include this ORCH share; the four ``--by issue`` rows for one ORCH session's held
  issues, summed, equal that ORCH session's own cost/context (within rounding).
- A session's held issues come from (highest priority first): ``--orch-session-json``'s
  ``held`` list (keyed by its ``session`` field, an 8-char session-id prefix),
  ``--dispatch-out``'s ``started orch (<session>) ...`` lines (every ``#N`` on that line),
  the session's own custom title (e.g. ``"orch #54 #55 #56 #57"``), and its first prompt
  (e.g. ``"Take every ready issue ... #54, #55, #56, #57"``) -- the last two are the cheap
  signals available even when the first two files are stale or missing. On top of whichever of
  these wins, every issue the session's own transcript claimed (``_claimed_issues``, #193, e.g.
  a ``dispatch.out`` "claimed #N" line has none of the four above but the transcript's own
  ``gh issue edit N --add-assignee`` calls do) is ADDED to its held issues -- for an ended
  session as for a live one (#199): the sources above only ever name what a session was
  *offered* at the start, not what it claimed afterward.

Brain sessions (the agent type ``brain``, or a session titled the installation's own brain
chat title, ``config/orchestration.json``'s ``brain_chat_title``) get their own ``brain`` row
under ``--by issue`` instead of falling into UNATTRIBUTED; they carry no issue number of their
own (the brain never executes, #45).
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Callable, Iterable, Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.agentops.core_format import human_duration  # noqa: E402
from tools.agentops.core_settings import load as _load_settings, tickets_repo as _tickets_repo_of  # noqa: E402
from tools.agentops.util import no_window_flags  # noqa: E402

_SETTINGS = _load_settings()

DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects" / _SETTINGS["claude_projects_dir"]
DEFAULT_PRICES_PATH = ROOT / "config" / "model_prices.json"
DEFAULT_CLAUDE_MD = ROOT / "CLAUDE.md"
DEFAULT_AGENTOPS = ROOT / "artifacts" / "runs" / "agentops.jsonl"
DEFAULT_ORCH_SESSION_JSON = ROOT / "artifacts" / "runs" / "orch_session.json"
DEFAULT_DISPATCH_OUT = ROOT / "artifacts" / "runs" / "dispatch.out"
DEFAULT_REPO = _tickets_repo_of()
DEFAULT_CONTEXT_LINE = _SETTINGS["context_line"]

ISSUE_RE = re.compile(r"#(\d+)")

TOKEN_KINDS = ("input", "cache_write", "cache_read", "output")


# ------------------------------------------------------------------------ data model ----


@dataclass
class Turn:
    """One deduped assistant turn (main-chat or subagent)."""

    session_id: str
    agent_id: str | None  # None for a main-chat turn
    agent_type: str | None  # subagent type from its meta.json, else None
    message_id: str
    model: str | None
    timestamp: datetime | None
    input_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    output_tokens: int
    issue: int | None  # attributed issue number, or None ("unattributed")
    title: str | None  # owning session's custom title, if any
    turn_index: int  # order within its own transcript file, 0-based
    issue_shares: dict[int, float] | None = None  # multi-issue ORCH split (#64); sums to 1.0
    # Part of cache_creation_input_tokens written with the 1-hour TTL (#637), from the transcript's
    # usage.cache_creation.ephemeral_1h_input_tokens; the rest is the 5-minute write. 0 when the
    # object is absent (the whole write then prices as 5m, as before).
    cache_creation_1h_tokens: int = 0

    @property
    def context(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    @property
    def is_subagent(self) -> bool:
        return self.agent_id is not None


# ------------------------------------------------------------------------- text/issue ----


def message_text(message: dict) -> str:
    """Flatten a message's content (string or content-block list) to plain text."""

    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return " ".join(parts)
    return ""


#: #136: user records that are not the owner's words (a peer's message, a harness task notice).
_PEER_RECORD_MARKS = ("<cross-session-message", "<task-notification")


def owner_issue(text: str) -> int | None:
    """``single_issue`` of an owner's message; None for a peer's message or a task notice (#136)."""

    return None if text.lstrip().startswith(_PEER_RECORD_MARKS) else single_issue(text)


def single_issue(text: str) -> int | None:
    """The one issue number named in ``text``, or None if zero or more than one distinct."""

    numbers = {int(n) for n in ISSUE_RE.findall(text)}
    if len(numbers) == 1:
        return next(iter(numbers))
    return None


def all_issues(text: str) -> list[int]:
    """Every distinct issue number named in ``text``, in order of first appearance (#64)."""

    seen: list[int] = []
    for raw in ISSUE_RE.findall(text):
        n = int(raw)
        if n not in seen:
            seen.append(n)
    return seen


#: literal signal for a brain session (issue #64): the chat title never varies, unlike ORCH's
#: (which carries its held issues) or the configurable --brain-title heuristic used for the
#: weekly context target below, which this deliberately does not reuse (see the module
#: docstring: it must not reclassify an ordinary chat that merely mentions "opus" once).
BRAIN_TITLE_LITERAL = _SETTINGS["brain_chat_title"].lower()
#: earlier titles of the brain chat (``brain_chat_title_aliases``, #536): past sessions keep them in
#: their transcripts, so their rows still count as the brain's.
BRAIN_TITLE_ALIASES = (BRAIN_TITLE_LITERAL,) + tuple(
    t.lower() for t in _SETTINGS.get("brain_chat_title_aliases", ())
)


def is_brain_turn(turn: "Turn") -> bool:
    """True for a turn belonging to a brain session (agent type ``brain``, or the chat titled
    the installation's own brain chat title, ``config/orchestration.json``'s
    ``brain_chat_title``); #64 requirement 3."""

    if turn.agent_type == "brain":
        return True
    return is_brain_title(turn.title)


def is_brain_title(title: str | None) -> bool:
    """True when a session title is the brain chat's own title or one of its aliases (#143)."""

    return bool(title) and any(t in title.lower() for t in BRAIN_TITLE_ALIASES)


def parse_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------- transcript scan ----


def _read_jsonl(path: Path) -> Iterator[dict]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _usage_turn(
    obj: dict,
    *,
    session_id: str,
    agent_id: str | None,
    agent_type: str | None,
    issue: int | None,
    title: str | None,
    turn_index: int,
    issue_shares: dict[int, float] | None = None,
) -> Turn | None:
    message = obj.get("message") or {}
    message_id = message.get("id")
    if not message_id:
        return None
    usage = message.get("usage") or {}
    return Turn(
        session_id=session_id,
        agent_id=agent_id,
        agent_type=agent_type,
        message_id=message_id,
        model=message.get("model"),
        timestamp=parse_timestamp(obj.get("timestamp")),
        input_tokens=int(usage.get("input_tokens") or 0),
        cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        cache_read_input_tokens=int(usage.get("cache_read_input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        issue=issue,
        title=title,
        turn_index=turn_index,
        issue_shares=issue_shares,
        cache_creation_1h_tokens=cache_creation_1h(usage),
    )


def cache_creation_1h(usage: dict) -> int:
    """Tokens of ``usage``'s cache write with the 1-hour TTL (#637); 0 when the
    ``cache_creation`` object is absent, and never above the whole write."""

    detail = usage.get("cache_creation")
    if not isinstance(detail, dict):
        return 0
    total = int(usage.get("cache_creation_input_tokens") or 0)
    return min(int(detail.get("ephemeral_1h_input_tokens") or 0), total)


def _update_usage(turn: Turn, message: dict) -> None:
    """Overwrite ``turn``'s usage fields from a later line of the same ``message.id`` (#259):
    a subagent's (and, per row 3, apparently also a main transcript's) streamed message
    carries only the tokens written so far on its first line, the whole turn's usage on its
    last -- so the last line seen for a given ``message.id`` wins, every kind, while the
    turn's other fields (timestamp, model, issue, position) stay those of its first line. A
    later line with no ``usage`` at all (a pure content continuation) carries no signal and
    is skipped, not treated as zero."""

    usage = message.get("usage")
    if not usage:
        return
    turn.input_tokens = int(usage.get("input_tokens") or 0)
    turn.cache_creation_input_tokens = int(usage.get("cache_creation_input_tokens") or 0)
    turn.cache_read_input_tokens = int(usage.get("cache_read_input_tokens") or 0)
    turn.output_tokens = int(usage.get("output_tokens") or 0)
    turn.cache_creation_1h_tokens = cache_creation_1h(usage)


def is_real_turn(message: dict) -> bool:
    """A transcript message that is a real model turn: a model other than `<synthetic>`/None, and
    not all-zero usage (a dispatcher start that died at 0 tokens is not) (#949: shared readers)."""

    model = message.get("model")
    if not model or model == "<synthetic>":
        return False
    usage = message.get("usage") or {}
    tokens = sum(int(usage.get(k) or 0) for k in (
        "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return tokens != 0


@dataclass
class MainTranscript:
    turns: list[Turn]
    title: str | None
    compactions: int
    # filled only when parse_main_transcript gets a `window` (#949): the real turns' lines inside
    # it, and the Skill tool_use blocks inside it as (message id, block id) keys, in file order
    window_real_turns: int = 0
    window_skill_keys: list[tuple] = field(default_factory=list)
    # the session's display title (#47): its custom title, else its first user message (#143)
    display_title: str | None = None


def _split_for_turn(
    current_issue: int | None,
    current_subset: list[int] | None,
    held_issues: list[int],
) -> tuple[int | None, dict[int, float] | None]:
    """One ORCH turn's attribution (#64 requirement 2): a single unambiguous held issue wins
    outright; otherwise its share splits equally over the latest named subset of held issues,
    or over all of them when none was named."""

    if current_issue is not None and (not held_issues or current_issue in held_issues):
        return current_issue, None
    if not held_issues:
        return None, None
    subset = current_subset if current_subset else held_issues
    if len(subset) == 1:
        return subset[0], None
    share = 1.0 / len(subset)
    return None, {n: share for n in subset}


def parse_main_transcript(
    path: Path,
    *,
    held_issues: list[int] | None = None,
    window: tuple[datetime, datetime] | None = None,
) -> MainTranscript:
    """Stream one top-level session ``.jsonl`` file: dedupe, attribute, count compactions.

    ``held_issues`` (#64): when this session is a multi-issue ORCH session (as resolved by
    ``resolve_held_issues``), its own turns split their share over these issues instead of
    going unattributed; see ``_split_for_turn``.

    ``window`` (#949): ``(since, until)``; when given, the same pass also records the window facts
    (``MainTranscript.window_*``) that the skill count needs, so the file is read only once.
    """

    held_issues = held_issues or []
    session_id = path.stem
    seen_ids: dict[str, Turn] = {}
    turns: list[Turn] = []
    title: str | None = None
    first_message: str | None = None  # fallback "title" when no custom-title exists
    current_issue: int | None = None
    current_subset: list[int] | None = None
    compactions = 0
    turn_index = 0
    window_real_turns = 0
    window_skill_keys: list[tuple] = []

    for obj in _read_jsonl(path):
        kind = obj.get("type")
        if kind == "custom-title":
            title = obj.get("customTitle") or title
            continue
        if kind == "system" and obj.get("subtype") == "compact_boundary":
            compactions += 1
            continue
        if kind == "user":
            text = message_text(obj.get("message") or {})
            if first_message is None and text:
                first_message = text
            # #136: a peer's message or a task notice names someone else's ticket: not this chat's
            issue = owner_issue(text)
            if issue is not None:
                current_issue = issue
            if held_issues:
                subset = [n for n in all_issues(text) if n in held_issues]
                if subset:
                    current_subset = subset
            continue
        if kind != "assistant":
            continue
        message = obj.get("message") or {}
        if window is not None:
            ts = parse_timestamp(obj.get("timestamp"))
            if ts is not None and window[0] <= ts <= window[1]:
                if is_real_turn(message):
                    window_real_turns += 1
                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        if (isinstance(block, dict) and block.get("type") == "tool_use"
                                and block.get("name") == "Skill"):
                            window_skill_keys.append((message.get("id"), block.get("id")))
        message_id = message.get("id")
        if not message_id:
            continue
        existing = seen_ids.get(message_id)
        if existing is not None:
            _update_usage(existing, message)
            continue
        resolved_issue, shares = _split_for_turn(current_issue, current_subset, held_issues)
        turn = _usage_turn(
            obj,
            session_id=session_id,
            agent_id=None,
            agent_type=None,
            issue=resolved_issue,
            title=None,  # backfilled below once the whole file (and its title) is known
            turn_index=turn_index,
            issue_shares=shares,
        )
        if turn is not None:
            seen_ids[message_id] = turn
            turns.append(turn)
            turn_index += 1

    # "chats named by title via the session metadata if available, else by first message"
    # (issue #47): the display title used for chat classification falls back to the
    # transcript's own first user message when no custom-title line was ever written.
    display_title = title if title is not None else first_message
    for turn in turns:
        turn.title = display_title
    return MainTranscript(turns=turns, title=title, compactions=compactions,
                          window_real_turns=window_real_turns, window_skill_keys=window_skill_keys,
                          display_title=display_title)


def _parent_issue_at(parent_turns: list[Turn] | None, when: datetime | None) -> int | None:
    """The issue its parent session is attributed to at ``when``: the ``.issue`` of the last parent
    turn stamped at or before it, skipping turns with no issue (#143)."""

    issue: int | None = None
    for turn in parent_turns or []:
        if turn.issue is not None and turn.timestamp is not None and when is not None \
                and turn.timestamp <= when:
            issue = turn.issue
    return issue


def _subagent_issue(text: str, held_issues: list[int], parent_turns: list[Turn] | None,
                    when: datetime | None) -> int | None:
    """A subagent's issue from its first prompt (#64 requirement 1, #143): the first held ``#N``;
    else the one ``#N`` named; else, when several are named, the parent's issue at that moment if it
    is one of them, else the first ``#N`` named."""

    named = all_issues(text)
    for n in named:
        if n in held_issues:
            return n
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        attributed = _parent_issue_at(parent_turns, when)
        return attributed if attributed in named else named[0]
    return None


def parse_subagent_transcript(
    path: Path,
    meta: dict,
    *,
    held_issues: list[int] | None = None,
    parent_turns: list[Turn] | None = None,
    parent_title: str | None = None,
) -> list[Turn]:
    """Stream one ``subagents/agent-*.jsonl`` file: dedupe, attribute from its first prompt.

    ``held_issues`` (#64 requirement 1): when its parent ORCH session held issues, the first
    ``#N`` in the first prompt that is one of them wins, even if the prompt also names other
    (non-held) issues elsewhere -- a worker prompt's own "code map" often repeats every held
    issue, which makes the plain "exactly one #N" rule go ambiguous on it. Falls back to that
    plain rule when no held issue is known, or none of the prompt's ``#N`` is held.

    ``parent_turns`` / ``parent_title`` (#143): the owning main session's turns and display title.
    A prompt naming several ``#N`` with none held goes to the parent's issue at that moment when it
    is among them; a subagent with no issue of a brain-titled parent takes the parent's title, so
    it is counted as the brain's (``is_brain_turn``).
    """

    agent_id = meta.get("agentId") or path.stem
    agent_type = meta.get("agentType")
    # a subagent lives under <session>/subagents/<file>; its owning main session is the
    # grandparent directory name.
    session_id = path.parent.parent.name
    held_issues = held_issues or []

    seen_ids: dict[str, Turn] = {}
    turns: list[Turn] = []
    issue: int | None = None
    first_prompt_seen = False
    turn_index = 0

    for obj in _read_jsonl(path):
        kind = obj.get("type")
        if kind == "user" and not first_prompt_seen:
            text = message_text(obj.get("message") or {})
            issue = _subagent_issue(text, held_issues, parent_turns,
                                    parse_timestamp(obj.get("timestamp")))
            first_prompt_seen = True
            continue
        if kind != "assistant":
            continue
        message = obj.get("message") or {}
        message_id = message.get("id")
        if not message_id:
            continue
        existing = seen_ids.get(message_id)
        if existing is not None:
            _update_usage(existing, message)
            continue
        turn = _usage_turn(
            obj,
            session_id=session_id,
            agent_id=agent_id,
            agent_type=agent_type,
            issue=issue,
            title=None,
            turn_index=turn_index,
        )
        if turn is not None:
            seen_ids[message_id] = turn
            turns.append(turn)
            turn_index += 1
    if issue is None and is_brain_title(parent_title):
        # no issue of its own under a brain chat: the brain's share (#143)
        for turn in turns:
            turn.title = parent_title
    return turns


def _peek_title_and_first_message(path: Path) -> tuple[str | None, str | None]:
    """Cheap prefix read of one main transcript: stops as soon as both are found (#64), so
    ``resolve_held_issues`` can be computed before the full attribution pass over the file."""

    title: str | None = None
    first_message: str | None = None
    for obj in _read_jsonl(path):
        kind = obj.get("type")
        if kind == "custom-title" and title is None:
            title = obj.get("customTitle")
        elif kind == "user" and first_message is None:
            text = message_text(obj.get("message") or {})
            if text:
                first_message = text
        if title is not None and first_message is not None:
            break
    return title, first_message


def resolve_held_issues(
    session_id: str,
    title: str | None,
    first_message: str | None,
    *,
    dispatch_offers: dict[str, list[int]],
    orch_session_held: dict[str, list[int]],
    claimed_issues: set[int] | None = None,
) -> list[int]:
    """The issues one main session holds (#64 requirement 1/2), highest-priority signal first:
    ``orch_session.json``'s ``held`` list, ``dispatch.out``'s "started orch (<id>) ..." line,
    the session's own custom title, then its first prompt -- see the module docstring. Every
    issue in ``claimed_issues`` (#199: the session's own transcript claim calls, ``#193``'s
    ``_claimed_issues``) is ADDED to whichever of the above wins, never replacing it -- this is
    how an ORCH session's issues taken after its start (not just what it was offered) end up
    held, for an ended session as for a live one."""

    key = session_id[:8]
    if key in orch_session_held:
        held = list(orch_session_held[key])
    elif key in dispatch_offers:
        held = list(dispatch_offers[key])
    else:
        # title / first-prompt are cheap but noisier signals: only trust them when they name
        # MORE THAN ONE issue -- a single #N here is the ordinary "one issue this chat is
        # about" case already handled by single_issue, not a multi-issue ORCH session (#64).
        held = []
        if title:
            nums = all_issues(title)
            if len(nums) > 1:
                held = nums
        if not held and first_message:
            nums = all_issues(first_message)
            if len(nums) > 1:
                held = nums

    for n in sorted(claimed_issues or ()):
        if n not in held:
            held.append(n)
    return held


def _modified_since(paths: list[Path], since: datetime | None) -> list[Path]:
    """`paths` whose mtime is at or after `since` (all of them when `since` is None). A transcript
    last written before a window's start holds no turn inside it: a turn is stamped when it is
    written, and the file's mtime is never earlier than its last write (#949)."""

    if since is None:
        return paths
    lo = since.timestamp()
    return [p for p in paths if p.stat().st_mtime >= lo]


def discover_main_transcripts(projects_dir: Path, *, since: datetime | None = None) -> list[Path]:
    if not projects_dir.exists():
        return []
    return _modified_since(sorted(projects_dir.glob("*.jsonl")), since)


def discover_subagent_transcripts(projects_dir: Path, *,
                                  since: datetime | None = None) -> list[tuple[Path, dict]]:
    if not projects_dir.exists():
        return []
    out: list[tuple[Path, dict]] = []
    for jsonl_path in _modified_since(sorted(projects_dir.glob("*/subagents/*.jsonl")), since):
        meta_path = jsonl_path.parent / (jsonl_path.stem + ".meta.json")
        meta: dict = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                meta = {}
        out.append((jsonl_path, meta))
    return out


@dataclass
class ScanResult:
    turns: list[Turn]
    compactions: int
    # per-agent first-turn context, keyed by (session_id, agent_id) -> Turn
    subagent_first_turns: dict[tuple[str, str], Turn] = field(default_factory=dict)
    # each main transcript as parsed by the scan (#949), keyed by path, so a later reader of the
    # same window (outcomes_transcripts.skill_calls_per_orch_session) need not parse it again
    main_transcripts: dict[Path, MainTranscript] = field(default_factory=dict)


def scan_projects_dir(
    projects_dir: Path,
    *,
    dispatch_offers: dict[str, list[int]] | None = None,
    orch_session_held: dict[str, list[int]] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> ScanResult:
    """Every transcript under `projects_dir`. `since` (#949): transcripts last written before it
    are not read at all; with `since` and `until` both given, each main transcript also gets its
    window facts (`MainTranscript.window_*`). Default (no window): every file, as before."""

    dispatch_offers = dispatch_offers or {}
    orch_session_held = orch_session_held or {}
    window = (since, until) if since is not None and until is not None else None
    turns: list[Turn] = []
    compactions = 0
    subagent_first_turns: dict[tuple[str, str], Turn] = {}
    main_transcripts: dict[Path, MainTranscript] = {}

    main_paths = discover_main_transcripts(projects_dir, since=since)
    held_by_session: dict[str, list[int]] = {}
    for path in main_paths:
        title, first_message = _peek_title_and_first_message(path)
        held_by_session[path.stem] = resolve_held_issues(
            path.stem,
            title,
            first_message,
            dispatch_offers=dispatch_offers,
            orch_session_held=orch_session_held,
            claimed_issues=_claimed_issues(path),
        )

    mains_by_session: dict[str, MainTranscript] = {}
    for path in main_paths:
        parsed = parse_main_transcript(path, held_issues=held_by_session.get(path.stem, []), window=window)
        main_transcripts[path] = parsed
        mains_by_session[path.stem] = parsed
        turns.extend(parsed.turns)
        compactions += parsed.compactions

    for path, meta in discover_subagent_transcripts(projects_dir, since=since):
        session_id = path.parent.parent.name
        parent = mains_by_session.get(session_id)
        agent_turns = parse_subagent_transcript(
            path, meta, held_issues=held_by_session.get(session_id, []),
            parent_turns=parent.turns if parent else None,
            parent_title=parent.display_title if parent else None,
        )
        turns.extend(agent_turns)
        if agent_turns:
            first = min(agent_turns, key=lambda t: t.turn_index)
            subagent_first_turns[(first.session_id, first.agent_id or path.stem)] = first

    return ScanResult(turns=turns, compactions=compactions, subagent_first_turns=subagent_first_turns,
                      main_transcripts=main_transcripts)


# ------------------------------------------------------------------------- agentops ----


#: a dispatch.out line naming the ORCH session it started: "...started orch (<id>) offered
#: #54, #55...", or the single-issue form "...started orch #6 (<id>): ...". Either way the
#: session's short id is the parenthesized group; every #N on that same line is what it holds.
DISPATCH_STARTED_RE = re.compile(r"started orch\b.*?\((\w+)\)")


def parse_dispatch_offers(text: str) -> dict[str, list[int]]:
    """session-short-id -> issues named on its dispatch.out "started orch (<id>) ..." line
    (#64 requirement 1); one line, i.e. its latest restart, wins if a session recurs."""

    out: dict[str, list[int]] = {}
    for line in text.splitlines():
        match = DISPATCH_STARTED_RE.search(line)
        if not match:
            continue
        nums = all_issues(line)
        if nums:
            out[match.group(1)] = nums
    return out


def load_dispatch_offers(path: Path) -> dict[str, list[int]]:
    if not path.exists():
        return {}
    return parse_dispatch_offers(path.read_text(encoding="utf-8"))


def parse_orch_session_held(data: dict) -> dict[str, list[int]]:
    """``{"session": "<id>", "held": [54, 55, ...]}`` -> ``{"<id>": [54, 55, ...]}`` (#64
    requirement 1); empty when the file's shape does not match, never guessed."""

    session = data.get("session")
    held = data.get("held")
    if not session or not isinstance(held, list):
        return {}
    try:
        nums = [int(n) for n in held]
    except (TypeError, ValueError):
        return {}
    return {session: nums} if nums else {}


def load_orch_session_held(path: Path) -> dict[str, list[int]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return parse_orch_session_held(data)


def load_agentops(path: Path) -> list[dict]:
    """codex-sale runs from the bridge. Empty when the file does not exist yet (D62)."""

    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


# --------------------------------------------------------------------------- pricing ----


def load_prices(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def turn_tokens_by_kind(turn: Turn) -> dict[str, int]:
    return {
        "input": turn.input_tokens,
        "cache_write": turn.cache_creation_input_tokens,
        "cache_read": turn.cache_read_input_tokens,
        "output": turn.output_tokens,
    }


def turn_price(turn: Turn, price: dict) -> dict:
    """The rate row that applies to ``turn``: the entry itself, or for a model with ``tiers``
    (Haiku 5.5, #637) the first tier while the request's prompt (input + cache read + cache
    write) stays within ``prompt_bound``, the second once it passes it -- every kind, output
    included, at that one tier."""

    tiers = price.get("tiers")
    if not tiers:
        return price
    return tiers["above"] if turn.context > tiers["prompt_bound"] else tiers["up_to"]


def turn_cost(turn: Turn, prices: dict[str, dict]) -> float | None:
    """USD cost of one turn, or None when its model has no price (never guessed). The cache
    write splits into its 5-minute and 1-hour parts; a price row without ``cache_write_1h``
    bills the 1-hour part at the 5-minute rate."""

    price = prices.get(turn.model or "")
    if price is None:
        return None
    rates = turn_price(turn, price)
    one_hour = min(turn.cache_creation_1h_tokens, turn.cache_creation_input_tokens)
    tokens = {
        "input": turn.input_tokens,
        "cache_write": turn.cache_creation_input_tokens - one_hour,
        "cache_write_1h": one_hour,
        "cache_read": turn.cache_read_input_tokens,
        "output": turn.output_tokens,
    }
    total = 0.0
    for kind, count in tokens.items():
        rate = rates.get(kind)
        if rate is None and kind == "cache_write_1h":
            rate = rates.get("cache_write")
        if rate is None:
            return None
        total += count * rate / 1_000_000
    return total


# --------------------------------------------------------------------------- windows ----


def local_tzinfo() -> tzinfo:
    """The machine's own local UTC offset (now's offset; a window edge is never far from now)."""
    return datetime.now().astimezone().tzinfo


_CLOCK_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


def parse_date_arg(raw: str, tz: tzinfo | None = None, today: date | None = None) -> datetime:
    """A ``--since`` / ``--until`` value -> an aware datetime.

    ``Z`` or an explicit offset is kept as given; a date or datetime with no offset is in ``tz``
    (default: the machine's local zone); a bare ``HH:MM[:SS]`` is ``today`` (default: today in
    ``tz``) at that time in ``tz``."""
    tz = tz or local_tzinfo()
    raw = raw.strip()
    if _CLOCK_RE.match(raw):
        day = today or datetime.now(tz).date()
        return datetime.combine(day, time.fromisoformat(raw.zfill(5) if len(raw) < 5 else raw), tzinfo=tz)
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def default_window(now: datetime | None = None, days: int = 14) -> tuple[datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    return now - timedelta(days=days), now


def in_window(turn: Turn, since: datetime, until: datetime) -> bool:
    if turn.timestamp is None:
        return False
    return since <= turn.timestamp <= until


# --------------------------------------------------------------------------- gh fake ----


GhRunner = Callable[[list[str]], str]


def run_gh(args: list[str]) -> str:
    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=True, creationflags=no_window_flags(),
    )
    return result.stdout


def fetch_closed_issues(repo: str = DEFAULT_REPO, *, runner: GhRunner = run_gh) -> list[dict]:
    """``gh issue list --state closed --json number,closedAt``, via an injectable runner."""

    raw = runner(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "closed",
            "--json",
            "number,closedAt",
            "--limit",
            "1000",
        ]
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


# ------------------------------------------------------------------------- the four ----


def claude_md_kb(path: Path = DEFAULT_CLAUDE_MD) -> float | None:
    if not path.exists():
        return None
    return path.stat().st_size / 1024.0


def subagent_first_turn_context_by_type(
    result: ScanResult, *, since: datetime, until: datetime
) -> dict[str, float]:
    """Median first-turn context per subagent ``agentType``, windowed by timestamp."""

    by_type: dict[str, list[int]] = defaultdict(list)
    for turn in result.subagent_first_turns.values():
        if not in_window(turn, since, until):
            continue
        by_type[turn.agent_type or "(unknown)"].append(turn.context)
    return {t: statistics.median(v) for t, v in by_type.items() if v}


def classify_chat(title: str | None, *, brain_pattern: str, orch_pattern: str) -> str | None:
    """"brain" / "ORCH" / None, from the session's custom title (issue #47: "by title... else
    by first message"; this repo's ORCH/brain split is not itself defined anywhere machine-
    readable, so the split is by title substring, configurable with ``--brain-title`` /
    ``--orch-title``; see the token_meter report for what matched."""

    if not title:
        return None
    lowered = title.lower()
    if re.search(orch_pattern, lowered):
        return "ORCH"
    if re.search(brain_pattern, lowered):
        return "brain"
    return None


def subagent_share(
    turns: list[Turn],
    prices: dict[str, dict],
    *,
    session_prefixes: list[str] | None = None,
    orch_pattern: str | None = None,
) -> tuple[list[tuple[str, float, float]], float, float]:
    """Own-thread $ vs subagent $ per session of a set: session ids by prefix, or sessions whose
    title matches ``orch_pattern``. Returns (rows (session, own, sub), own total, sub total);
    turns of an unpriced model add nothing (never guessed). All time, no window (#344)."""

    own: dict[str, float] = {}
    sub: dict[str, float] = {}
    # subagent turns carry no title of their own: a session's title is its main thread's
    titles = {t.session_id: t.title for t in turns if not t.is_subagent and t.title}
    for t in turns:
        if session_prefixes is not None:
            if not any(t.session_id.startswith(p) for p in session_prefixes):
                continue
        elif classify_chat(titles.get(t.session_id), brain_pattern=r"$^", orch_pattern=orch_pattern or "orch") != "ORCH":
            continue
        cost = turn_cost(t, prices)
        if cost is None:
            continue
        (sub if t.is_subagent else own)[t.session_id] = (sub if t.is_subagent else own).get(t.session_id, 0.0) + cost
    ids = sorted(set(own) | set(sub))
    rows = [(i, own.get(i, 0.0), sub.get(i, 0.0)) for i in ids]
    return rows, sum(own.values()), sum(sub.values())


def render_subagent_share(rows: list[tuple[str, float, float]], own_total: float, sub_total: float) -> str:
    lines = [f"{'session':<10} {'own $':>10} {'subagents $':>12}"]
    for sid, o, s in rows:
        lines.append(f"{sid[:8]:<10} {o:>10.2f} {s:>12.2f}")
    total = own_total + sub_total
    share = 100.0 * sub_total / total if total else 0.0
    lines.append(f"own ${own_total:.2f}  subagents ${sub_total:.2f}  share {share:.1f} %")
    return chr(10).join(lines)


def week_key(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def weekly_median_context(
    turns: Iterable[Turn],
    *,
    since: datetime,
    until: datetime,
    brain_pattern: str,
    orch_pattern: str,
) -> dict[tuple[str, str], float]:
    """(chat, week) -> median context per turn, main-chat turns only."""

    buckets: dict[tuple[str, str], list[int]] = defaultdict(list)
    for turn in turns:
        if turn.is_subagent:
            continue
        if not in_window(turn, since, until):
            continue
        chat = classify_chat(turn.title, brain_pattern=brain_pattern, orch_pattern=orch_pattern)
        if chat is None:
            continue
        buckets[(chat, week_key(turn.timestamp))].append(turn.context)
    return {k: statistics.median(v) for k, v in buckets.items() if v}


@dataclass
class IssueCost:
    number: int
    closed_at: str | None
    cost_usd: float
    tokens: dict[str, int]
    turns: int
    priced_turns: int
    unpriced_turns: int


def _mapped_shares(shares: dict[int, float], mapped: dict[int, float]) -> dict[int, float] | None:
    """A turn's split shares restricted to the issues a session weight map names, renormalised to
    sum to 1.0; None when the map names none of them (the caller keeps the turn's own split)."""

    kept = {n: mapped[n] for n in shares if n in mapped}
    total = sum(kept.values())
    if not kept or total <= 0:
        return None
    return {n: w / total for n, w in kept.items()}


def cost_per_closed_issue(
    turns: Iterable[Turn],
    closed_issues: list[dict],
    prices: dict[str, dict],
    *,
    since: datetime,
    until: datetime,
    session_issue_weights: dict[str, dict[int, float]] | None = None,
) -> list[IssueCost]:
    """Per-issue cost over the window. A multi-issue split turn is weighted by its own
    ``issue_shares`` (equal over the held issues); ``session_issue_weights`` maps a session id
    (the transcript's ``Turn.session_id``) to ``{issue: share}`` that replaces those equal shares
    for that session's split turns, restricted to the turn's issues and renormalised when one of
    them is missing from the map. ``None`` (the default) keeps the equal split everywhere."""

    closed_by_number = {row["number"]: row.get("closedAt") for row in closed_issues}
    totals: dict[int, dict] = defaultdict(
        lambda: {"cost": 0.0, "tokens": defaultdict(int), "turns": 0, "priced": 0, "unpriced": 0}
    )
    for turn in turns:
        if not in_window(turn, since, until):
            continue
        if turn.issue_shares:
            shares = turn.issue_shares
            mapped = (session_issue_weights or {}).get(turn.session_id)
            if mapped:
                shares = _mapped_shares(shares, mapped) or shares
            weights = {n: w for n, w in shares.items() if n in closed_by_number}
        elif turn.issue is not None and turn.issue in closed_by_number:
            weights = {turn.issue: 1.0}
        else:
            weights = {}
        if not weights:
            continue
        cost = turn_cost(turn, prices)
        tokens = turn_tokens_by_kind(turn)
        for number, weight in weights.items():
            bucket = totals[number]
            bucket["turns"] += 1
            for kind, value in tokens.items():
                bucket["tokens"][kind] += round(value * weight)
            if cost is None:
                bucket["unpriced"] += 1
            else:
                bucket["cost"] += cost * weight
                bucket["priced"] += 1

    out = [
        IssueCost(
            number=number,
            closed_at=closed_by_number.get(number),
            cost_usd=data["cost"],
            tokens=dict(data["tokens"]),
            turns=data["turns"],
            priced_turns=data["priced"],
            unpriced_turns=data["unpriced"],
        )
        for number, data in totals.items()
    ]
    out.sort(key=lambda ic: ic.cost_usd, reverse=True)
    return out


def unattributed_share(turns: list[Turn], *, since: datetime, until: datetime) -> tuple[float, int, int]:
    """(share of context tokens with issue=None, unattributed context, total context)."""

    total = 0
    unattributed = 0
    for turn in turns:
        if not in_window(turn, since, until):
            continue
        total += turn.context
        if turn.issue is None and not turn.issue_shares and not is_brain_turn(turn):
            unattributed += turn.context
    share = (unattributed / total) if total else 0.0
    return share, unattributed, total


# ------------------------------------------------------------------------- grouping ----


def issue_group_weights(turn: Turn) -> dict[str, float]:
    """One turn's ``--by issue`` group key(s) and weight(s), summing to 1.0 (#64): a plain
    single issue or UNATTRIBUTED weighs 1.0 as before; a multi-issue ORCH turn's shares split
    over its ``#N`` keys; a brain turn with no issue of its own gets its own ``brain`` key
    instead of UNATTRIBUTED (requirement 3)."""

    if turn.issue_shares:
        return {f"#{n}": w for n, w in turn.issue_shares.items()}
    if turn.issue is not None:
        return {f"#{turn.issue}": 1.0}
    if is_brain_turn(turn):
        return {"brain": 1.0}
    return {"UNATTRIBUTED": 1.0}


def group_key(turn: Turn, by: str) -> str:
    if by == "issue":
        return f"#{turn.issue}" if turn.issue is not None else "UNATTRIBUTED"
    if by == "session":
        return turn.agent_id or turn.session_id
    if by == "model":
        return turn.model or "(unknown)"
    if by == "day":
        return turn.timestamp.date().isoformat() if turn.timestamp else "(no timestamp)"
    raise ValueError(f"unknown --by {by!r}")


@dataclass
class GroupRow:
    key: str
    turns: int
    context: int
    cost_usd: float
    priced_turns: int
    unpriced_turns: int


def grouped_table(
    turns: list[Turn], prices: dict[str, dict], *, by: str, since: datetime, until: datetime
) -> list[GroupRow]:
    rows: dict[str, dict] = defaultdict(lambda: {"turns": 0, "context": 0, "cost": 0.0, "priced": 0, "unpriced": 0})
    for turn in turns:
        if not in_window(turn, since, until):
            continue
        weights = issue_group_weights(turn) if by == "issue" else {group_key(turn, by): 1.0}
        cost = turn_cost(turn, prices)
        for key, weight in weights.items():
            row = rows[key]
            row["turns"] += 1
            row["context"] += round(turn.context * weight)
            if cost is None:
                row["unpriced"] += 1
            else:
                row["cost"] += cost * weight
                row["priced"] += 1
    out = [
        GroupRow(key=k, turns=v["turns"], context=v["context"], cost_usd=v["cost"], priced_turns=v["priced"], unpriced_turns=v["unpriced"])
        for k, v in rows.items()
    ]
    out.sort(key=lambda r: r.cost_usd, reverse=True)
    return out


# ------------------------------------------------------------------------- rendering ----


def format_usd(value: float) -> str:
    return f"${value:,.2f}"


def render_report(
    *,
    result: ScanResult,
    prices: dict[str, dict],
    missing_price_models: set[str],
    closed_issues: list[dict],
    since: datetime,
    until: datetime,
    by: str,
    top: int,
    brain_pattern: str,
    orch_pattern: str,
    claude_md_path: Path,
    agentops_rows: list[dict],
) -> str:
    lines: list[str] = []
    span = until - since
    lines.append(f"Token meter: {since.date()} .. {until.date()} ({human_duration(span.total_seconds())}), grouped by {by}")
    if missing_price_models:
        lines.append(f"  price missing for: {', '.join(sorted(missing_price_models))} (turns kept, cost omitted)")
    lines.append(f"  agentops.jsonl: {len(agentops_rows)} row(s)" + ("" if agentops_rows else " (file absent -- section empty, as the issue allows)"))
    lines.append("")

    rows = grouped_table(result.turns, prices, by=by, since=since, until=until)
    lines.append(f"{'key':<24} {'turns':>7} {'context':>12} {'cost usd':>12} {'unpriced':>9}")
    for row in rows:
        lines.append(f"{row.key:<24} {row.turns:>7} {row.context:>12,} {format_usd(row.cost_usd):>12} {row.unpriced_turns:>9}")

    share, unattributed_ctx, total_ctx = unattributed_share(result.turns, since=since, until=until)
    lines.append("")
    lines.append(f"Unattributed share: {share * 100:.1f}% of context tokens ({unattributed_ctx:,} of {total_ctx:,})")

    lines.append("")
    lines.append("=== The four refactor numbers ===")

    kb = claude_md_kb(claude_md_path)
    lines.append(f"1. CLAUDE.md size: {kb:.1f} KB" if kb is not None else "1. CLAUDE.md size: file not found")

    lines.append("2. Subagent first-turn context, median per agent type (target <= 30k):")
    by_type = subagent_first_turn_context_by_type(result, since=since, until=until)
    if by_type:
        for agent_type, median in sorted(by_type.items(), key=lambda kv: -kv[1]):
            flag = " ABOVE TARGET" if median > 30_000 else ""
            lines.append(f"   {agent_type:<20} {median:>10,.0f}{flag}")
    else:
        lines.append("   (no subagent turns in the window)")

    lines.append("3. Median context per turn, brain vs ORCH chat, per week (target <= 150k):")
    weekly = weekly_median_context(result.turns, since=since, until=until, brain_pattern=brain_pattern, orch_pattern=orch_pattern)
    if weekly:
        for (chat, week), median in sorted(weekly.items()):
            flag = " ABOVE TARGET" if median > DEFAULT_CONTEXT_LINE else ""
            lines.append(f"   {chat:<6} {week:<10} {median:>10,.0f}{flag}")
    else:
        lines.append(f"   (no session titled to match --brain-title {brain_pattern!r} or --orch-title {orch_pattern!r} in the window)")

    lines.append(f"4. Claude cost per closed issue (top {top} by cost, window {since.date()}..{until.date()}):")
    issue_costs = cost_per_closed_issue(result.turns, closed_issues, prices, since=since, until=until)
    if issue_costs:
        for ic in issue_costs[:top]:
            closed = ic.closed_at or "(closedAt missing)"
            note = f", {ic.unpriced_turns} turn(s) unpriced" if ic.unpriced_turns else ""
            lines.append(f"   #{ic.number:<5} closed {closed:<22} {format_usd(ic.cost_usd):>10}  tokens={ic.tokens}{note}")
    else:
        lines.append("   (no closed issue had attributed turns in the window)")

    return "\n".join(lines)


# -------------------------------------------------------------- ORCH threshold (#60) ----
#
# D104 / grill Q22: ORCH stops taking issues once its context passes T = B + N x X, where B is
# its context right after the start (the first turn), X what one issue adds, N the issues the
# context has taken. Measured here over the ORCH transcripts, never guessed; rerun weekly
# (`--orch-threshold --since <a week ago>`).

ORCH_NAME_RE = re.compile(r"^orch(?:\s|$)")
#: an ORCH claim: `gh issue edit 60 --add-assignee @me` (any shell, any argument order after N).
CLAIM_RE = re.compile(r"gh\s+issue\s+edit\s+(\d+)\b[^\n;&|]*--add-assignee")
#: `for n in 156 157 ...; do ... gh issue edit $n --add-assignee ...; done` (#193): ORCH chains
#: several claims over a shell loop variable, so the literal-digit ``CLAIM_RE`` never matches
#: inside the loop body. Captures the loop variable and its literal number list; the numbers
#: count only when the body actually claims through that same variable.
FOR_LOOP_CLAIM_RE = re.compile(
    r"for\s+(\w+)\s+in\s+((?:\d+\s+)*\d+)\s*;\s*do\b(.*?)\bdone\b", re.DOTALL,
)
#: a transcript whose last turn is younger than this is live: shown, not measured.
LIVE_MINUTES = 5


@dataclass
class OrchProfile:
    session_id: str
    title: str | None
    first_context: int
    last_context: int
    taken: int  # issues claimed in this context, at least 1
    started: datetime | None
    ended: datetime | None

    @property
    def per_issue(self) -> float:
        return max(0, self.last_context - self.first_context) / self.taken


def _quote_state_at(text: str) -> list[bool]:
    """`out[i]` is true when index ``i`` of ``text`` sits inside a single- or double-quoted
    shell string (naive: no backslash-escape handling, good enough for the ORCH commands
    measured here). Used to keep a number quoted as prose inside another command's
    `-b`/`--comment` argument from ever counting as a claim (#193)."""

    out = [False] * (len(text) + 1)
    in_single = in_double = False
    for i, ch in enumerate(text):
        out[i] = in_single or in_double
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
    out[len(text)] = in_single or in_double
    return out


def _command_claims(command: str) -> set[int]:
    """Every issue number a shell command claims: a literal `gh issue edit N --add-assignee`
    outside any quoted string, plus each number of a `for n in N N ...; do ... gh issue edit $n
    --add-assignee ...; done` loop whose body claims through that same loop variable (#193)."""

    quoted = _quote_state_at(command)
    claims = {int(m.group(1)) for m in CLAIM_RE.finditer(command) if not quoted[m.start()]}
    for m in FOR_LOOP_CLAIM_RE.finditer(command):
        if quoted[m.start()]:
            continue
        var, numbers, body = m.group(1), m.group(2), m.group(3)
        var_re = r"\$\{?" + re.escape(var) + r"\b\}?"
        if re.search(r"gh\s+issue\s+edit\s+" + var_re + r"[^\n;&|]*--add-assignee", body):
            claims.update(int(n) for n in numbers.split())
    return claims


def _claimed_issues(path: Path) -> set[int]:
    """Every issue number an ORCH transcript claimed successfully (#193): each `gh issue edit N
    --add-assignee` call counts, however many are chained in one command or looped over a shell
    variable, but only when its own tool result shows the call did not fail (no ``is_error``)."""

    claimed: set[int] = set()
    pending: dict[str, set[int]] = {}
    for obj in _read_jsonl(path):
        kind = obj.get("type")
        content = (obj.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        if kind == "assistant":
            for block in content:
                if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                    continue
                command = str((block.get("input") or {}).get("command") or "")
                nums = _command_claims(command)
                if nums:
                    pending[block.get("id")] = nums
        elif kind == "user":
            for block in content:
                if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                    continue
                nums = pending.pop(block.get("tool_use_id"), None)
                if nums and not block.get("is_error"):
                    claimed.update(nums)
    return claimed


def orch_profile(path: Path) -> OrchProfile | None:
    """One ORCH transcript's first and last context and the issues it claimed; `None` when it is
    not an ORCH session (title) or has no turn."""

    parsed = parse_main_transcript(path)
    if not parsed.title or not ORCH_NAME_RE.match(parsed.title) or not parsed.turns:
        return None
    turns = sorted(parsed.turns, key=lambda t: t.turn_index)
    return OrchProfile(
        session_id=path.stem, title=parsed.title, first_context=turns[0].context,
        last_context=turns[-1].context, taken=max(1, len(_claimed_issues(path))),
        started=turns[0].timestamp, ended=turns[-1].timestamp,
    )


def orch_threshold(profiles: list[OrchProfile]) -> tuple[float, float]:
    """(B, X): medians of the first-turn context and of the context one issue adds."""

    if not profiles:
        raise ValueError("no finished ORCH transcript to measure")
    return (statistics.median(p.first_context for p in profiles),
            statistics.median(p.per_issue for p in profiles))


def _k(tokens: float) -> str:
    return f"{tokens / 1000:.1f}k"


DISPATCH_ENDED_RE = re.compile(r"\borch (\w+) ended")


def parse_dispatch_ended(text: str) -> set[str]:
    """Short ids of the ORCH sessions the dispatcher's log says ended (#734)."""
    return {m.group(1) for m in DISPATCH_ENDED_RE.finditer(text)}


def load_dispatch_ended(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return parse_dispatch_ended(path.read_text(encoding="utf-8", errors="replace"))


def render_orch_threshold(projects_dir: Path, *, since: datetime, until: datetime,
                          now: datetime | None = None, ended_ids: set[str] | None = None,
                          context_line: int | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    ended_ids = ended_ids or set()
    line = float("inf") if context_line is None else context_line  # None: no cap (the CLI passes it)
    profiles = [p for p in (orch_profile(path) for path in discover_main_transcripts(projects_dir)) if p]
    profiles = [p for p in profiles if p.started and since <= p.started <= until]
    live = [p for p in profiles if p.ended and now - p.ended < timedelta(minutes=LIVE_MINUTES)
            and p.session_id[:8] not in ended_ids]
    finished = [p for p in profiles if p not in live]
    lines = [f"ORCH context threshold (#60): {len(finished)} finished ORCH contexts, "
             f"{since.date()} .. {until.date()}", "",
             f"{'session':<10} {'title':<22} {'taken':>5} {'first':>8} {'last':>8} {'per issue':>9}"]
    for p in sorted(finished, key=lambda p: p.started):
        lines.append(f"{p.session_id[:8]:<10} {(p.title or '')[:22]:<22} {p.taken:>5} "
                     f"{_k(p.first_context):>8} {_k(p.last_context):>8} {_k(p.per_issue):>9}")
    if not finished:
        lines.append("(none)")
        return "\n".join(lines)
    b, x = orch_threshold(finished)
    lines += ["", f"B (median context right after the start) = {_k(b)}",
              f"X (median context one issue adds)        = {_k(x)}",
              "T = B + N x X: " + ", ".join(f"N={n} {_k(min(b + n * x, line))}" for n in range(1, 7))]
    for p in live:
        t = min(b + p.taken * x, line)
        if p.last_context >= line:
            verdict = f"over the context line {_k(line)}: take nothing new, finish or park, end"
        elif p.last_context > t:
            verdict = "over T: take nothing new, finish or park, end"
        else:
            verdict = "under T"
        lines.append(f"live ORCH {p.session_id[:8]} ({p.title}): context {_k(p.last_context)}, "
                     f"{p.taken} issue(s) taken, T({p.taken}) = {_k(t)}: {verdict}")
    return "\n".join(lines)


# ------------------------------------------------------------------------------- cli ----


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--projects-dir", type=Path, default=DEFAULT_PROJECTS_DIR)
    parser.add_argument("--prices", type=Path, default=DEFAULT_PRICES_PATH)
    parser.add_argument("--claude-md", type=Path, default=DEFAULT_CLAUDE_MD)
    parser.add_argument("--agentops", type=Path, default=DEFAULT_AGENTOPS)
    parser.add_argument(
        "--orch-session-json", type=Path, default=DEFAULT_ORCH_SESSION_JSON,
        help="dispatcher's live snapshot of the ORCH session it holds ({session, held: [...]}); #64",
    )
    parser.add_argument(
        "--dispatch-out", type=Path, default=DEFAULT_DISPATCH_OUT,
        help="dispatcher log; its 'started orch (<id>) ...' lines give a past ORCH session's held issues; #64",
    )
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--since", type=str, default=None, help="ISO date/datetime or HH:MM, local clock unless it carries Z or an offset; default now-14d")
    parser.add_argument("--until", type=str, default=None, help="ISO date/datetime or HH:MM, local clock unless it carries Z or an offset; default now")
    parser.add_argument("--by", choices=["issue", "session", "model", "day"], default="issue")
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--brain-title", default=r"opus|fable", help="regex matched against a session's custom title")
    parser.add_argument("--orch-title", default=r"orch", help="regex matched against a session's custom title")
    parser.add_argument("--subagent-share", action="store_true",
                        help="own-thread $ / subagent $ / share over a session set: --sessions or --orch-title (#344)")
    parser.add_argument("--sessions", default=None,
                        help="comma-separated session id prefixes for --subagent-share")
    parser.add_argument("--orch-threshold", action="store_true",
                        help="print ORCH's context threshold T = B + N x X (#60) and the live ORCH's context")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    since = parse_date_arg(args.since) if args.since else None
    until = parse_date_arg(args.until) if args.until else None
    if since is None or until is None:
        default_since, default_until = default_window()
        since = since or default_since
        until = until or default_until

    if args.subagent_share:
        prices = load_prices(args.prices)
        scanned = scan_projects_dir(args.projects_dir)
        prefixes = [x.strip() for x in args.sessions.split(",") if x.strip()] if args.sessions else None
        rows, own_total, sub_total = subagent_share(
            scanned.turns, prices, session_prefixes=prefixes, orch_pattern=args.orch_title
        )
        print(render_subagent_share(rows, own_total, sub_total))
        return 0

    if args.orch_threshold:
        print(render_orch_threshold(args.projects_dir, since=since, until=until,
                                    ended_ids=load_dispatch_ended(args.dispatch_out),
                                    context_line=DEFAULT_CONTEXT_LINE))
        return 0

    prices = load_prices(args.prices)
    dispatch_offers = load_dispatch_offers(args.dispatch_out)
    orch_session_held = load_orch_session_held(args.orch_session_json)
    result = scan_projects_dir(
        args.projects_dir, dispatch_offers=dispatch_offers, orch_session_held=orch_session_held
    )
    missing_price_models = {t.model for t in result.turns if t.model and t.model not in prices}
    closed_issues = fetch_closed_issues(args.repo)
    agentops_rows = load_agentops(args.agentops)

    report = render_report(
        result=result,
        prices=prices,
        missing_price_models=missing_price_models,
        closed_issues=closed_issues,
        since=since,
        until=until,
        by=args.by,
        top=args.top,
        brain_pattern=args.brain_title,
        orch_pattern=args.orch_title,
        claude_md_path=args.claude_md,
        agentops_rows=agentops_rows,
    )
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
