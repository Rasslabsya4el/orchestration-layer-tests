"""The token meter's log: one JSON line per run into `artifacts/runs/agentops.jsonl`
(requirement 2 of issue #46)."""

from __future__ import annotations

import time
from pathlib import Path

from .models import credit_multiplier
from .util import append_jsonl

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG = ROOT / "artifacts" / "runs" / "agentops.jsonl"


def record_run(*, kind: str, model: str, outcome: str, wall_s: float, tokens_in: int = 0, tokens_out: int = 0,
               issue: int | None = None, session: str | None = None, log_path: Path | None = None) -> dict:
    """Append `{ts, kind, issue, model, tokens_in, tokens_out, credits_multiplier, wall_s, outcome,
    session}` and return the record written.

    `session` (issue #66) is the opencode session/agent id, when the caller has one -- it is how
    `tools.agentops.outcomes.routes` joins an outcome line back to this log's tokens for a route
    that went through the opencode bridge (worker/research). Optional and defaulted to `None` so
    every existing call site and fixture keeps working unchanged."""
    record = {
        "ts": int(time.time()),
        "kind": kind,
        "issue": issue,
        "model": model,
        "tokens_in": int(tokens_in),
        "tokens_out": int(tokens_out),
        "credits_multiplier": credit_multiplier(model),
        "wall_s": round(float(wall_s), 1),
        "outcome": outcome,
        "session": session,
    }
    append_jsonl(log_path or DEFAULT_LOG, record)
    return record
