"""Transcript facts for `outcomes report` (#646): route drift and Skill calls per ORCH session.

Read-only. Transcripts are found and parsed with the helpers `outcomes.py` and
`scripts/token_meter.py` already use."""

from __future__ import annotations

import json
import re
import statistics
from datetime import datetime
from collections import Counter
from pathlib import Path

from tools.agentops import outcomes


def _models_of(jsonl_path: Path, token_meter) -> set[str]:
    models: set[str] = set()
    for obj in token_meter._read_jsonl(jsonl_path):
        if obj.get("type") != "assistant":
            continue
        model = (obj.get("message") or {}).get("model")
        if model and model != "<synthetic>":
            models.add(model)
    return models


def _canonical_model(raw: str) -> str:
    """A transcript's raw model id folded like a definition's `model:` value
    (`claude-haiku-4-5-20251001` -> `haiku-4.5`); an id it cannot fold stays as it is."""

    name = re.sub(r"-\d{8}$", "", raw)
    if name in outcomes.MODEL_SPELLINGS:
        return outcomes.MODEL_SPELLINGS[name]
    short = re.sub(r"^claude-", "", name)
    short = re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", short)
    return outcomes.MODEL_SPELLINGS.get(short, raw)


def _agent_type(meta_path: Path | None) -> str | None:
    if meta_path is None:
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8")).get("agentType")
    except (OSError, json.JSONDecodeError):
        return None


def route_drift(rows: list[dict], *, projects_dir: Path,
                agents_dir: Path | None = None) -> dict[str, dict]:
    """Per folded route: `spawns` (rows with a transcript), `drift` (spawns whose transcript ran
    a model other than the agent type's own definition; `None` when that definition names no
    model), `no_transcript` (rows without a session or without a file)."""

    token_meter = outcomes._token_meter()
    result: dict[str, dict] = {}
    for row in rows:
        route = outcomes._fold_route(row.get("route") or "-", agents_dir, row.get("ts"))
        entry = result.setdefault(route, {"spawns": 0, "drift": 0, "no_transcript": 0, "_known": False})
        session = row.get("session")
        jsonl_path, meta_path = (outcomes._find_transcript(session, projects_dir)
                                 if session else (None, None))
        if jsonl_path is None:
            entry["no_transcript"] += 1
            continue
        entry["spawns"] += 1
        agent_type = _agent_type(meta_path)
        expected = (outcomes._agent_default_model(agent_type, agents_dir or outcomes.AGENTS_DIR)
                    if agent_type else None)
        if expected is None:
            continue
        entry["_known"] = True
        # models are raw ids (claude-sonnet-5-5); expected is canonical -> fold both
        seen = {_canonical_model(m) for m in _models_of(jsonl_path, token_meter)}
        if any(m != expected for m in seen):
            entry["drift"] += 1
    for entry in result.values():
        if not entry.pop("_known"):
            entry["drift"] = None
    return result


def _real_turns(path: Path, token_meter) -> list[tuple[datetime | None, str]]:
    """(timestamp, model) of each assistant line that is a real turn: a model other than
    `<synthetic>`/None and not all-zero usage (a dispatcher start that died at 0 tokens is not)."""

    out = []
    for obj in token_meter._read_jsonl(path):
        if obj.get("type") != "assistant":
            continue
        message = obj.get("message") or {}
        if not token_meter.is_real_turn(message):
            continue
        out.append((token_meter.parse_timestamp(obj.get("timestamp")), message.get("model")))
    return out


def drift_by_agent_type(*, projects_dir: Path | list[Path], since: datetime, until: datetime,
                        agents_dir: Path | None = None) -> dict[str, dict]:
    """Per `agentType` (meta), over every subagent transcript whose first real turn is in the
    window: `spawns`, `drift` (spawns whose real models differ from the role file's expected
    model; `None` when the role names none) and `models` {folded model: spawns that ran it}.
    Independent of the outcomes log's `session` field."""

    tm = outcomes._token_meter()
    dirs = [projects_dir] if isinstance(projects_dir, Path) else list(projects_dir)
    result: dict[str, dict] = {}
    for d in dirs:
        for path, meta in tm.discover_subagent_transcripts(d, since=since):
            turns = _real_turns(path, tm)
            if not turns or turns[0][0] is None or not (since <= turns[0][0] <= until):
                continue
            agent_type = meta.get("agentType") or "-"
            entry = result.setdefault(agent_type, {"spawns": 0, "drift": 0, "models": Counter()})
            entry["spawns"] += 1
            seen = {_canonical_model(m) for _, m in turns}
            entry["models"].update(seen)
            expected = outcomes._agent_default_model(agent_type, agents_dir or outcomes.AGENTS_DIR)
            if expected is not None and any(m != expected for m in seen):
                entry["drift"] += 1
    for agent_type, entry in result.items():
        entry["models"] = dict(entry["models"])
        if outcomes._agent_default_model(agent_type, agents_dir or outcomes.AGENTS_DIR) is None:
            entry["drift"] = None
    return result


def skill_calls_per_orch_session(*, projects_dir: Path, since: datetime,
                                 until: datetime, scanned=None) -> dict[str, int]:
    """Per ORCH main session with a turn in the window: Skill tool_use blocks inside the window.

    `scanned` (#949): the `token_meter.scan_projects_dir(projects_dir, since=, until=)` result of
    the same window; its main transcripts already carry the window facts, so nothing is read
    again. Without it, each transcript is parsed once here."""

    tm = outcomes._token_meter()
    result: dict[str, int] = {}
    for path in tm.discover_main_transcripts(projects_dir, since=since):
        if scanned is not None:
            parsed = scanned.main_transcripts.get(path)
            if parsed is None:  # not modified in the window: no turn in it
                continue
        else:
            parsed = tm.parse_main_transcript(path, window=(since, until))
        if not parsed.title or not tm.ORCH_NAME_RE.match(parsed.title):
            continue
        if parsed.window_real_turns == 0:
            continue
        result[path.stem] = len(set(parsed.window_skill_keys))
    return result


def median_max(values) -> tuple[float | None, int | None]:
    values = list(values)
    if not values:
        return None, None
    return statistics.median(values), max(values)
