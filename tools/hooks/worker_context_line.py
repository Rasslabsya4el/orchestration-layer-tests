"""Worker context-line hook (issue #186).

Reads a Claude Code ``PostToolUse`` hook JSON payload from stdin. A subagent
cannot see its own context, so this hook finds the subagent's own transcript,
sums the last assistant turn's usage the same way
``tools/hooks/brain_reset_nudge.py`` does for the brain, and past the
installation's context line (``config/orchestration.json`` ``context_line``,
one key shared by every role) prints a ``hookSpecificOutput`` JSON object that
tells the worker to finish the step in hand and hand back, per
``.claude/agents/worker.md`` / ``worker-strong.md``.

Anthropic's docs state a subagent hook's input carries ``agent_id`` and
``agent_type``, but not whether ``transcript_path`` is then the subagent's own
transcript or its parent's. Both are handled: when ``transcript_path`` already
lies under a ``subagents`` folder it is used as-is; otherwise the subagent's
file is ``<dir of transcript_path>/<session id stem>/subagents/agent-<agent_id
>.jsonl`` -- the layout ``scripts/token_meter.py`` reads through its own glob
``*/subagents/*.jsonl`` (confirmed against the fixtures under
``tests/fixtures/token_meter*/projects/*/subagents/agent-*.jsonl``: one
``agent-<id>.jsonl`` per subagent spawn, sitting under
``<projects_dir>/<session_id>/subagents/``, with the main session's own
transcript at ``<projects_dir>/<session_id>.jsonl``).

Never raises: any error, bad stdin, or missing file results in a silent exit
0, and no tool call is ever failed by this hook. Every call appends one line
to ``var/agentops/worker_context_line.log`` (``AGENTOPS_HOOK_LOG`` overrides).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools" / "hooks"))

from tools.agentops.core_settings import load as _load_settings  # noqa: E402
from brain_reset_nudge import _last_assistant_usage_total  # noqa: E402

try:
    CONTEXT_LIMIT = int(_load_settings()["context_line"])
except Exception:  # the hook never fails a tool call: a broken settings file falls back to 150k
    CONTEXT_LIMIT = 150000
LOG_PATH = Path(__file__).resolve().parents[2] / "var" / "agentops" / "worker_context_line.log"


def _subagent_transcript_path(payload: dict) -> str | None:
    """The subagent's own transcript file, or None when it cannot be found.

    ``transcript_path`` itself when it already lies under a ``subagents``
    folder; otherwise built from the parent session's directory and the
    payload's ``agent_id``.
    """
    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    path = Path(transcript_path)
    if "subagents" in path.parts:
        return str(path)
    agent_id = payload.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        return None
    session_dir = path.parent
    session_id_stem = path.stem
    return str(session_dir / session_id_stem / "subagents" / f"agent-{agent_id}.jsonl")


def _log(payload: dict, context_k: int | None, told: bool) -> None:
    """Append one JSON line per call; never raises."""
    try:
        path = Path(os.environ.get("AGENTOPS_HOOK_LOG") or LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "agent_id": payload.get("agent_id"),
            "agent_type": payload.get("agent_type"),
            "context_k": context_k,
            "told": told,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


# the role(s) each rule binds (#320): bound by wiring in the worker frontmatter (.claude/agents/worker.md and
# worker-strong.md), so no fires() gate here: a payload without agent_type must not turn the guard off
RULE_ROLES: dict[str, tuple[str, ...]] = {"context_line_hand_back": ("worker",)}


def run(payload: dict) -> dict | None:
    subagent_path = _subagent_transcript_path(payload)
    if not subagent_path:
        _log(payload, None, False)
        return None
    try:
        n = _last_assistant_usage_total(subagent_path)
    except OSError:
        _log(payload, None, False)
        return None
    if n is None:
        _log(payload, None, False)
        return None
    k = round(n / 1000)
    if n < CONTEXT_LIMIT:
        _log(payload, k, False)
        return None
    _log(payload, k, True)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                f"Context is {k}k, past the line ({round(CONTEXT_LIMIT / 1000)}k). "
                "Finish the step in hand, do not start a new one, and hand back: "
                "run the tests that step touches, commit, then end with a report "
                "whose first line is `Context hand-back` (worker.md)."
            ),
        }
    }


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        output = run(payload)
    except Exception:
        return 0
    if output is not None:
        try:
            print(json.dumps(output))
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
