"""The brain's dollars by purpose: ``python -m tools.agentops purposes``.

The brain tags a stretch of work with one purpose by opening its first Bash or PowerShell call of the stretch
with the line ``# purpose: <word>``; the word is one of ``PURPOSE_WORDS``. A tag holds until the next tag or
the next owner (human user) message, whichever is first; a turn with no tag in force is ``untagged``, and so
is a turn under an unknown word (the word is listed once by name under the table). A subagent's turns count to
the purpose in force at the parent's ``Agent`` call that started it, in their own columns.

The brain turns and their price are the token meter's own (``scan_projects_dir``, ``is_brain_turn``,
``turn_cost``), the very turns ``losses`` adds up as "brain chat", so the total equals that figure to the cent.
Reads the session transcripts only; writes nothing. The one seam is ``purpose_split``; ``run_purposes`` prints it.

Stdlib, plus the token meter in the home's scripts folder.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .core_settings import DEFAULTS, load as load_settings
from .losses import configured_repos, home_repo, window_for
from .losses_cost import _PRICES_PATH, _token_meter
from .purpose_tag import PURPOSE_WORDS, SHELL_TOOLS, UNTAGGED, TagState

_AGENT_ID_TEXT = re.compile(r"agentId:\s*(\w+)")
_AGENT_TOOLS = ("Agent", "Task")


@dataclass
class PurposeRow:
    """One purpose: the brain's own turns and dollars, and the turns and dollars of the subagents it started."""

    turns: int = 0
    usd: float = 0.0
    subagent_turns: int = 0
    subagent_usd: float = 0.0


@dataclass
class PurposeSplit:
    """``rows`` holds every word of ``PURPOSE_WORDS`` and ``untagged``; ``unknown_words`` the words that were
    not on the list, each once, in order of first sight. ``total_usd`` is the brain's own dollars, which is
    the brain figure of ``losses`` for the same window."""

    rows: dict[str, PurposeRow]
    unknown_words: list[str] = field(default_factory=list)

    @property
    def total_usd(self) -> float:
        return sum(row.usd for row in self.rows.values())

    @property
    def total_turns(self) -> int:
        return sum(row.turns for row in self.rows.values())


def _read_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def _blocks(message: dict) -> list[dict]:
    content = message.get("content")
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _agent_id_of(obj: dict, block: dict) -> str | None:
    result = obj.get("toolUseResult")
    if isinstance(result, dict) and isinstance(result.get("agentId"), str):
        return result["agentId"]
    content = block.get("content")
    text = content if isinstance(content, str) else " ".join(
        part.get("text", "") for part in content if isinstance(part, dict)) if isinstance(content, list) else ""
    match = _AGENT_ID_TEXT.search(text)
    return match.group(1) if match else None


@dataclass
class _Walk:
    message_purpose: dict[str, str] = field(default_factory=dict)
    message_unknown: dict[str, str] = field(default_factory=dict)
    subagent_purpose: dict[str, str] = field(default_factory=dict)


def _walk_transcript(path: Path) -> _Walk:
    """The purpose in force at every assistant message of one brain transcript, and the purpose in force at
    the ``Agent`` call that started each subagent. The tag state is ``purpose_tag.TagState``."""
    walk = _Walk()
    state = TagState()
    agent_calls: dict[str, str] = {}
    for obj in _read_jsonl(path):
        kind = obj.get("type")
        message = obj.get("message") or {}
        if kind == "user":
            for block in _blocks(message):
                if block.get("type") == "tool_result" and block.get("tool_use_id") in agent_calls:
                    agent_id = _agent_id_of(obj, block)
                    if agent_id is not None:
                        walk.subagent_purpose[agent_id] = agent_calls[block["tool_use_id"]]
            state.see_user(obj)
            continue
        if kind != "assistant":
            continue
        for block in _blocks(message):
            if block.get("type") != "tool_use":
                continue
            name = block.get("name")
            if name in SHELL_TOOLS:
                state.see_tool_use(name, block.get("input"))
            elif name in _AGENT_TOOLS and isinstance(block.get("id"), str):
                agent_calls[block["id"]] = state.purpose
        message_id = message.get("id")
        if message_id:
            walk.message_purpose[message_id] = state.purpose
            if state.unknown is not None:
                walk.message_unknown[message_id] = state.unknown
    return walk


def purpose_split(projects_dir: Path, since: datetime, until: datetime, prices: dict | None = None) -> PurposeSplit:
    """The brain's dollars of the window ``since``..``until`` (both timezone-aware) split by purpose.

    A brain turn is the one ``losses`` counts as "brain chat" (the token meter's ``is_brain_turn``, no ticket of
    its own); a turn of a subagent that a brain session started is counted in the subagent columns of the
    purpose in force at its parent's ``Agent`` call. ``prices`` is the ``token_meter.load_prices`` shape;
    ``None`` reads the home's ``config/model_prices.json``."""
    meter = _token_meter()
    if prices is None:
        prices = meter.load_prices(_PRICES_PATH)
    scan = meter.scan_projects_dir(projects_dir, since=since, until=until)

    brain_sessions = {turn.session_id for turn in scan.turns if turn.agent_id is None and meter.is_brain_turn(turn)}
    walks = {path.stem: _walk_transcript(path) for path in scan.main_transcripts if path.stem in brain_sessions}

    rows = {word: PurposeRow() for word in (*PURPOSE_WORDS, UNTAGGED)}
    unknown_words: list[str] = []
    for turn in scan.turns:
        if turn.session_id not in walks or not meter.in_window(turn, since, until):
            continue
        cost = meter.turn_cost(turn, prices)
        if cost is None:
            continue
        walk = walks[turn.session_id]
        is_brain = list(meter.issue_group_weights(turn)) == ["brain"]
        if turn.agent_id is None:
            purpose = walk.message_purpose.get(turn.message_id, UNTAGGED)
            word = walk.message_unknown.get(turn.message_id)
        else:
            purpose = walk.subagent_purpose.get(turn.agent_id, UNTAGGED)
            word = None
        if word is not None and is_brain and word not in unknown_words:
            unknown_words.append(word)
        row = rows[purpose]
        if is_brain:
            row.turns += 1
            row.usd += cost
        if turn.agent_id is not None:
            # every subagent turn shows in the subagent columns; the token meter counts one with no ticket of
            # its own under a brain chat as the brain's (#143), so it is in the $ columns too
            row.subagent_turns += 1
            row.subagent_usd += cost
    return PurposeSplit(rows=rows, unknown_words=unknown_words)


# ----------------------------------------------------------------------- the table

def render(split: PurposeSplit, label: str) -> list[str]:
    """The table as lines: a header, one row per purpose, ``untagged``, the total, then the unknown words."""
    total = split.total_usd
    header = ["purpose", "brain turns", "$ at API price", "share of brain $", "subagent turns", "subagent $"]
    table = [header]
    for name, row in split.rows.items():
        share = f"{row.usd / total * 100:.0f}%" if total > 0 else "-"
        table.append([name, str(row.turns), f"${row.usd:,.2f}", share, str(row.subagent_turns),
                      f"${row.subagent_usd:,.2f}"])
    table.append(["total", str(split.total_turns), f"${total:,.2f}", "100%" if total > 0 else "-",
                  str(sum(row.subagent_turns for row in split.rows.values())),
                  f"${sum(row.subagent_usd for row in split.rows.values()):,.2f}"])
    widths = [max(len(line[column]) for line in table) for column in range(len(header))]
    lines = [f"purposes {label} (brain sessions, API price)"]
    for line in table:
        lines.append(" | ".join(cell.ljust(widths[column]) for column, cell in enumerate(line)).rstrip())
    if split.unknown_words:
        lines.append("unknown purpose words, counted as untagged: " + ", ".join(split.unknown_words))
    return lines


def _parse_moment(text: str) -> datetime:
    """A date (local midnight) or an ISO date-time (a naive one in local time) as an aware moment."""
    moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    return moment.astimezone() if moment.tzinfo is None else moment


def _window(args: argparse.Namespace, now: datetime) -> tuple[datetime, datetime, str]:
    """(since, until, label) for ``args``; ValueError for a bad week or a bad mix of options."""
    since_text, until_text = getattr(args, "since", None), getattr(args, "until", None)
    if since_text is None and until_text is None:
        this, _ = window_for(args.week, now)
        return this.since, this.until, this.label
    if args.week is not None:
        raise ValueError("--week and --since/--until are two ways to name one window; give one")
    if since_text is None:
        raise ValueError("--until needs --since")
    try:
        since = _parse_moment(since_text)
        until = _parse_moment(until_text) if until_text is not None else now
    except ValueError as exc:
        raise ValueError(f"not a date or an ISO date-time: {exc}") from exc
    if until <= since:
        raise ValueError("--until is not after --since")
    return since, until, f"{since:%Y-%m-%d %H:%M} .. {until:%Y-%m-%d %H:%M}"


def run_purposes(args: argparse.Namespace, *, settings: dict | None = None, now: datetime | None = None,
                 prices: dict | None = None) -> int:
    """Print the purpose table for ``args`` (``--week`` or ``--since`` / ``--until``) for this repo's brain
    sessions; returns the exit code. The keyword arguments are for tests: the settings, the clock, the prices."""
    cfg = load_settings() if settings is None else {**DEFAULTS, **settings}
    moment = now if now is not None else datetime.now().astimezone()
    try:
        since, until, label = _window(args, moment)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    repo = home_repo(configured_repos(cfg), cfg)
    print("\n".join(render(purpose_split(repo.projects_dir, since, until, prices), label)))
    return 0


def add_subparser(sub) -> argparse.ArgumentParser:
    """Register the ``purposes`` command on the CLI's subparsers."""
    parser = sub.add_parser("purposes", help="the brain's dollars split by the purpose tag of its work")
    parser.add_argument("--week", nargs="?", const=None, default=None,
                        help="an ISO week such as 2026-W42 (default: the week that contains now)")
    parser.add_argument("--since", help="start of the window: a date or an ISO date-time (local time if naive)")
    parser.add_argument("--until", help="end of the window (default: now)")
    return parser
