"""One outcome line per delegation, and the routing table recomputed from it (issue #66).

Requirement 1: whoever judges a report (ORCH for its subagents, the brain for its researchers)
writes one line -- issue, route (agent type + model, or the opencode model), a class from the
fixed set below, and a one-line reason, plus an optional session/agent id for the join. Appended
to a JSONL next to the metering log of `tools/agentops/metering.py` (`metering.DEFAULT_LOG`'s own
directory; this module reuses `metering.DEFAULT_LOG` and `util.append_jsonl`, its path helper --
it does not touch `scripts/token_meter.py`, which issue #64 is changing at the same time).

Requirement 2: `routes()` groups outcome lines by route and reports delegations, accepted/redone/
wrong-fact percentages, and a median $ and tokens joined by session, from two places:
  - `tools/agentops/metering.py`'s own log (the opencode bridge's worker/research runs, #46):
    tokens are there; $ is not -- that log carries `codex.sale` credit multipliers, not USD, so a
    route joined only there reports tokens and "-" for $ (the gap the issue allows noting).
  - a Claude Code subagent (or main-session) transcript under `scripts/token_meter.
    DEFAULT_PROJECTS_DIR`, read through `scripts/token_meter.py` (imported read-only, the same way
    `tools/agentops/monitor.py` already does it) for routes like `worker/sonnet-5`: both $ (via
    `config/model_prices.json`) and tokens.
A session with neither gives "-" for both; a route with no session on any of its outcome lines
gives "-" for both (no join was possible), never a guess.
"""

from __future__ import annotations

import json
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any

from .metering import DEFAULT_LOG as METERING_LOG
from .util import append_jsonl

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "scripts"
AGENTS_DIR = ROOT / ".claude" / "agents"

#: the fixed set (requirement 1); an unknown class is rejected, never silently accepted.
OUTCOME_CLASSES = ("accepted", "redone", "wrong-fact", "abandoned")

#: the second-check verdict of issue #131 (`outcome --check`): did a second helper on another model
#: agree with the first answer. Optional on a row; an unknown value is rejected, never stored.
OUTCOME_CHECKS = ("agree", "disagree")

DEFAULT_OUTCOMES_LOG = METERING_LOG.with_name("agentops_outcomes.jsonl")

#: opencode models a route may name directly, unchanged (issue #166 requirement 4).
OPENCODE_MODELS = ("luna", "terra")

#: built-in agent types that carry no `.claude/agents/<name>.md` definition of their own.
BUILTIN_AGENT_TYPES = ("general-purpose", "claude-code-guide", "Explore", "Plan")

#: agent types removed from `.claude/agents/` (issue #253: `opus5-*` left Opus 5), kept here
#: with the canonical model their old `model: claude-opus-5` frontmatter named so an old
#: transcript's route still folds -- old transcripts still carry Opus 5 (issue #166).
RETIRED_AGENT_DEFAULT_MODELS = {
    "opus5-low": "opus-5", "opus5-medium": "opus-5", "opus5-high": "opus-5",
    "opus5-xhigh": "opus-5", "opus5-max": "opus-5",
}

#: every spelling this repo has seen for the four canonical models, folded to the canonical
#: name (`--route`'s own free text, and a definition's `model:` frontmatter value alike).
MODEL_SPELLINGS = {
    "sonnet-5": "sonnet-5", "sonnet5": "sonnet-5", "sonnet": "sonnet-5",
    "haiku-4.5": "haiku-4.5", "haiku4.5": "haiku-4.5", "haiku": "haiku-4.5",
    "opus-5.5": "opus-5.5", "opus5.5": "opus-5.5",
    "opus-5": "opus-5", "opus5": "opus-5",
    "claude-opus-5": "opus-5", "claude-opus-5-5": "opus-5.5",
    "claude-sonnet-5": "sonnet-5",
    "sonnet-5.5": "sonnet-5.5", "sonnet5.5": "sonnet-5.5", "sonnet-5-5": "sonnet-5.5",
    "claude-sonnet-5-5": "sonnet-5.5",
    "haiku-5.5": "haiku-5.5", "haiku5.5": "haiku-5.5", "haiku-5-5": "haiku-5.5",
    "claude-haiku-5-5": "haiku-5.5",
}

#: the merge of the pin of the four Sonnet roles to claude-sonnet-5-5 (4542bab); earlier rows were Sonnet 5.
SONNET_55_SINCE = 1790624259

#: the pin of researcher-find to claude-haiku-5-5 (#635, read from the clock as the branch was cut);
#: a row logged bare before it ran Haiku 4.5 (the `haiku` alias of the 2.1.289 sessions).
HAIKU_55_SINCE = 1791539460

#: the pin of the plain `worker` to claude-haiku-5-5 (#676); a row logged bare before it ran Sonnet 5.5.
#: the value is the clock time ORCH read right before the merge (`date +%s`).
WORKER_HAIKU_55_SINCE = 1791813859


class UnknownOutcomeClass(ValueError):
    pass


class UnknownOutcomeCheck(ValueError):
    pass


class UnknownRoute(ValueError):
    """`--route` names neither a known agent type nor an opencode model (issue #166
    requirement: "refused, with the known names printed")."""


def _known_agent_types(agents_dir: Path) -> set[str]:
    """File stems under `.claude/agents/` plus the built-ins Claude Code ships with no
    definition file of its own."""

    stems: set[str] = set()
    if agents_dir.is_dir():
        for path in agents_dir.glob("*.md"):
            stems.add(path.stem)
    stems.update(BUILTIN_AGENT_TYPES)
    stems.update(RETIRED_AGENT_DEFAULT_MODELS)
    return stems


def _agent_default_model(agent_type: str, agents_dir: Path) -> str | None:
    """The canonical model an agent type's own definition names, or `None` when the
    definition names no model (`model: inherit`, `general-purpose`,
    `claude-code-guide`, or no definition file at all)."""

    path = agents_dir / f"{agent_type}.md"
    if not path.is_file():
        return RETIRED_AGENT_DEFAULT_MODELS.get(agent_type)
    text = path.read_text(encoding="utf-8")
    frontmatter = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not frontmatter:
        return None
    match = re.search(r"^model:\s*(\S+)\s*$", frontmatter.group(1), re.MULTILINE)
    if not match:
        return None
    raw = match.group(1).strip()
    if raw == "inherit":
        return None
    return MODEL_SPELLINGS.get(raw)


def _unknown_route_message(bad: str, known_agents: set[str]) -> str:
    names = ", ".join(sorted(known_agents) + list(OPENCODE_MODELS))
    return f"unknown route {bad!r}; known names: {names}"


def canonicalize_route(route: str, *, agents_dir: Path | None = None) -> str:
    """One canonical name per route, `<agent type>/<model>` (issue #166): a bare agent type
    gets its definition's own model (bare when the definition names none); a model given
    explicitly is folded to its canonical spelling regardless of the definition's own model.
    Raises `UnknownRoute`, with the known names, for a route that is neither a known agent
    type nor an opencode model."""

    agents_dir = agents_dir if agents_dir is not None else AGENTS_DIR
    known_agents = _known_agent_types(agents_dir)
    normalized = route.strip().replace(":", "/", 1)

    if "/" not in normalized:
        if normalized in OPENCODE_MODELS:
            return normalized
        if normalized in known_agents:
            model = _agent_default_model(normalized, agents_dir)
            return f"{normalized}/{model}" if model else normalized
        raise UnknownRoute(_unknown_route_message(normalized, known_agents))

    agent_part, model_part = normalized.split("/", 1)
    if agent_part not in known_agents:
        raise UnknownRoute(_unknown_route_message(agent_part, known_agents))
    canonical_model = MODEL_SPELLINGS.get(model_part)
    if canonical_model is None:
        raise UnknownRoute(_unknown_route_message(model_part, known_agents))
    return f"{agent_part}/{canonical_model}"


def _fold_route(route: str, agents_dir: Path | None = None, ts: int | None = None) -> str:
    """The same canonicalization as `canonicalize_route`, but read-time and lenient: a route
    already logged that is neither a known agent type nor an opencode model (a literal
    opencode model id such as `gpt-5.6-terra`) is kept as-is rather than raised on -- `routes`
    folds the log, it never rewrites or rejects it.

    With the row's `ts`, a role pinned to Sonnet 5.5 reads as `sonnet-5` before `SONNET_55_SINCE`
    when logged bare, and as `sonnet-5.5` after it when logged bare or as `sonnet-5`; a role pinned
    to Haiku 5.5 reads as `haiku-4.5` before `HAIKU_55_SINCE` when logged bare (#635)."""

    agents_dir = agents_dir if agents_dir is not None else AGENTS_DIR
    try:
        folded = canonicalize_route(route, agents_dir=agents_dir)
    except UnknownRoute:
        return route
    if not isinstance(ts, (int, float)) or "/" not in folded:
        return folded
    role, model = folded.split("/", 1)
    default = _agent_default_model(role, agents_dir)
    bare = "/" not in route.strip().replace(":", "/", 1)
    if default == "haiku-5.5" and role == "worker":
        return f"{role}/sonnet-5.5" if (bare and ts < WORKER_HAIKU_55_SINCE) else folded
    if default == "haiku-5.5":
        return f"{role}/haiku-4.5" if (bare and ts < HAIKU_55_SINCE) else folded
    if default != "sonnet-5.5":
        return folded
    if ts < SONNET_55_SINCE:
        return f"{role}/sonnet-5" if bare else folded
    return f"{role}/sonnet-5.5" if (bare or model == "sonnet-5") else folded


def _token_meter():
    """Import `scripts/token_meter.py` read-only (`scripts/` has no `__init__.py`, so it goes on
    `sys.path` the same way `tools/agentops/monitor.py` already does it)."""

    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import token_meter

    return token_meter


def record_outcome(*, issue: int | None, route: str, cls: str, why: str, session: str | None = None,
                   unit: str | None = None, check: str | None = None,
                   log_path: Path | None = None, agents_dir: Path | None = None) -> dict:
    """Append `{ts, issue, route, class, why, session}`; raises `UnknownOutcomeClass` for a class
    outside `OUTCOME_CLASSES` (requirement 1: "Reject an unknown class"), `UnknownOutcomeCheck` for a
    `check` outside `OUTCOME_CHECKS`, and `UnknownRoute` for a `route` that is neither a known agent
    type nor an opencode model -- in every case nothing is appended, `route` is canonicalized
    (`canonicalize_route`) before the write either way.

    `unit` (e.g. "2/3" -- this delegation's slice of a sliced issue) and `check` ("agree" or
    "disagree", issue #131) are added as their own fields only when given, so a row without them
    stays exactly as before."""

    if cls not in OUTCOME_CLASSES:
        raise UnknownOutcomeClass(f"unknown outcome class {cls!r}; must be one of {OUTCOME_CLASSES}")
    if check is not None and check not in OUTCOME_CHECKS:
        raise UnknownOutcomeCheck(f"unknown outcome check {check!r}; must be one of {OUTCOME_CHECKS}")
    route = canonicalize_route(route, agents_dir=agents_dir)
    record = {
        "ts": int(time.time()),
        "issue": issue,
        "route": route,
        "class": cls,
        "why": why,
        "session": session,
    }
    if unit is not None:
        record["unit"] = unit
    if check is not None:
        record["check"] = check
    append_jsonl(log_path or DEFAULT_OUTCOMES_LOG, record)
    return record


def load_jsonl(path: Path) -> list[dict]:
    """Every line of a JSONL log as a dict; `[]` when the file does not exist yet (both this
    module's own log and the metering log are optional until something has been written)."""

    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    rows = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def parse_since(raw: str, tz: tzinfo | None = None) -> float:
    """`--since DATE` (a bare date or an ISO timestamp) as a UTC epoch, for comparison against a
    record's own `ts` (an `int(time.time())` epoch, always UTC). The same rule as
    `scripts/token_meter.parse_date_arg`: `Z` or an explicit offset is kept as given; no offset
    means `tz` (default: the machine's local zone)."""

    raw = raw.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz or datetime.now().astimezone().tzinfo)
    return dt.timestamp()


def _find_transcript(session_id: str, projects_dir: Path) -> tuple[Path | None, Path | None]:
    """A main-chat transcript `<projects_dir>/<session_id>.jsonl` (no meta), or a subagent one
    `<projects_dir>/*/subagents/agent-<session_id>.jsonl` (paired `.meta.json`, if present).
    `(None, None)` when neither exists -- the caller reports "-", never guesses a path."""

    main_path = projects_dir / f"{session_id}.jsonl"
    if main_path.is_file():
        return main_path, None
    matches = sorted(projects_dir.glob(f"*/subagents/agent-{session_id}.jsonl"))
    if not matches:
        return None, None
    jsonl_path = matches[0]
    meta_path = jsonl_path.with_name(jsonl_path.stem + ".meta.json")
    return jsonl_path, (meta_path if meta_path.is_file() else None)


def claude_session_usage(session_id: str, *, projects_dir: Path,
                         prices: dict[str, dict]) -> tuple[float | None, int | None]:
    """(USD cost, total tokens) for one Claude Code session/agent id, read from its own transcript
    via `scripts/token_meter.py`. `(None, None)` when no transcript is found; a transcript with no
    priced turn at all gives `(None, tokens)` -- tokens are always countable, $ is not (a model
    missing from `config/model_prices.json` prices as `None`, never guessed, same rule as
    `token_meter.turn_cost`)."""

    jsonl_path, meta_path = _find_transcript(session_id, projects_dir)
    if jsonl_path is None:
        return None, None
    token_meter = _token_meter()
    if meta_path is None and jsonl_path.parent == projects_dir:
        turns = token_meter.parse_main_transcript(jsonl_path).turns
    else:
        meta: dict = {}
        if meta_path is not None:
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
        turns = token_meter.parse_subagent_transcript(jsonl_path, meta)
    if not turns:
        return None, None
    total_tokens = sum(
        t.input_tokens + t.output_tokens + t.cache_creation_input_tokens + t.cache_read_input_tokens
        for t in turns
    )
    cost_total = 0.0
    any_priced = False
    for turn in turns:
        cost = token_meter.turn_cost(turn, prices)
        if cost is not None:
            cost_total += cost
            any_priced = True
    return (cost_total if any_priced else None), total_tokens


def _join_one(session_id: str, *, metering_by_session: dict[str, dict], projects_dir: Path,
             prices: dict[str, dict]) -> tuple[float | None, int | None]:
    """One session's (cost, tokens), preferring an exact match in the metering log (the opencode
    bridge's own runs -- tokens only, no USD there) and falling back to a Claude transcript."""

    metered = metering_by_session.get(session_id)
    if metered is not None:
        tokens = int(metered.get("tokens_in") or 0) + int(metered.get("tokens_out") or 0)
        return None, tokens  # codex.sale credits, not USD (the gap this issue allows noting)
    return claude_session_usage(session_id, projects_dir=projects_dir, prices=prices)


def routes(*, outcomes: list[dict] | None = None, metering_records: list[dict] | None = None,
          outcomes_path: Path | None = None, metering_path: Path | None = None,
          projects_dir: Path | None = None, prices: dict[str, dict] | None = None,
          since: float | None = None) -> list[dict]:
    """Requirement 2: one row per route -- `delegations`, `accepted_pct`, `redone_pct`,
    `wrong_fact_pct`, `median_cost_usd` (`None` = "-"), `median_tokens` (`None` = "-"). Routes are
    sorted for a stable, readable table."""

    if outcomes is None:
        outcomes = load_jsonl(outcomes_path or DEFAULT_OUTCOMES_LOG)
    if since is not None:
        outcomes = [o for o in outcomes if (o.get("ts") or 0) >= since]

    if metering_records is None:
        metering_records = load_jsonl(metering_path or METERING_LOG)
    metering_by_session = {r["session"]: r for r in metering_records if r.get("session")}

    token_meter = _token_meter()
    projects_dir = projects_dir if projects_dir is not None else token_meter.DEFAULT_PROJECTS_DIR
    prices = prices if prices is not None else token_meter.load_prices(token_meter.DEFAULT_PRICES_PATH)

    by_route: dict[str, list[dict]] = defaultdict(list)
    for o in outcomes:
        by_route[_fold_route(o.get("route") or "-", ts=o.get("ts"))].append(o)

    rows = []
    for route, items in sorted(by_route.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        n = len(items)
        counts = Counter(i.get("class") for i in items)
        costs: list[float] = []
        tokens: list[int] = []
        for item in items:
            session = item.get("session")
            if not session:
                continue
            cost, tok = _join_one(session, metering_by_session=metering_by_session,
                                  projects_dir=projects_dir, prices=prices)
            if cost is not None:
                costs.append(cost)
            if tok is not None:
                tokens.append(tok)
        checked = sum(1 for i in items if i.get("check"))
        disagree = sum(1 for i in items if i.get("check") == "disagree")
        rows.append({
            "route": route,
            "delegations": n,
            "accepted_pct": 100.0 * counts.get("accepted", 0) / n,
            "redone_pct": 100.0 * counts.get("redone", 0) / n,
            "wrong_fact_pct": 100.0 * counts.get("wrong-fact", 0) / n,
            "disagree_pct": 100.0 * disagree / checked if checked else None,
            "median_cost_usd": statistics.median(costs) if costs else None,
            "median_tokens": statistics.median(tokens) if tokens else None,
        })
    return rows


def format_routes_table(rows: list[dict]) -> str:
    """Human-readable table (requirement 2); "-" for a `None` median, as the issue asks."""

    if not rows:
        return "no outcome lines"
    header = f"{'route':<24}{'delegations':>12}{'accepted %':>12}{'redone %':>10}{'wrong-fact %':>14}" \
             f"{'disagree %':>11}{'median $':>10}{'median tokens':>15}"
    lines = [header, "-" * len(header)]
    for row in rows:
        disagree = row.get("disagree_pct")  # issue #131: None (no checked row) renders "-"
        disagree_cell = "-" if disagree is None else f"{disagree:.0f}%"
        cost = "-" if row["median_cost_usd"] is None else f"${row['median_cost_usd']:.2f}"
        tokens = "-" if row["median_tokens"] is None else f"{row['median_tokens']:.0f}"
        lines.append(
            f"{row['route']:<24}{row['delegations']:>12}{row['accepted_pct']:>11.0f}%"
            f"{row['redone_pct']:>9.0f}%{row['wrong_fact_pct']:>13.0f}%{disagree_cell:>11}"
            f"{cost:>10}{tokens:>15}"
        )
    return "\n".join(lines)


__all__ = [
    "DEFAULT_OUTCOMES_LOG",
    "OUTCOME_CHECKS",
    "OUTCOME_CLASSES",
    "UnknownOutcomeCheck",
    "UnknownOutcomeClass",
    "UnknownRoute",
    "canonicalize_route",
    "claude_session_usage",
    "format_routes_table",
    "load_jsonl",
    "parse_since",
    "record_outcome",
    "routes",
]
