"""Refuse, never ask, in a session nobody sits at (Claude Code ``PermissionRequest`` hook).

A session the dispatcher started has no human at it. A permission prompt there is
not a question, it is a stall: the session stops until somebody comes back. This
hook turns that stall into an answer the agent can act on at once.

``PermissionRequest`` fires exactly when Claude Code is about to ask a human, and
only then -- never for a call a rule or the auto-mode classifier already settled
(https://code.claude.com/docs/en/hooks#permissionrequest). So denying from here
takes nothing away that ran before: it adds no ``allow`` rule and widens nothing,
it only replaces "wait for a human" with "no".

Which sessions it answers for is read out of the transcript the payload names:
every session started with ``claude --bg`` -- ORCH and every other role nobody
sits at -- writes ``sessionKind: bg`` into its records, and into
its subagents' records too, so one head read covers both the session's own
prompts and its subagents' (``tools/hooks/transcript.py``, measured in #213).
An interactive session -- the brain's chat, the owner's own -- has no such mark:
the hook exits 0 without a decision and the permission flow is exactly what it
was, prompt and all.

Every refusal appends one line to the events log: the session, the tool, and the
first ``CALL_CHARS`` characters of the call.

Reads the hook payload from stdin, writes its decision to stdout as
``hookSpecificOutput``. Never raises: an unreadable payload or a failed event
write leaves the permission flow alone (exit 0).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from tools.agentops.util import append_jsonl, now as _now  # noqa: E402
from tools.hooks.transcript import is_background  # noqa: E402

#: the events log the dispatcher writes too; overridable for a test.
EVENTS_ENV = "AGENTOPS_EVENTS_FILE"
DEFAULT_EVENTS_FILE = _ROOT / "artifacts" / "runs" / "events.jsonl"
#: how much of the call the event line keeps.
CALL_CHARS = 100
EVENT_KIND = "permission_refused"

REFUSAL = (
    "Refused without asking: this session runs unattended, so a permission prompt has nobody to "
    "answer it. Nothing was approved and nothing ran. Take a way that needs no approval -- inside "
    "your own working directory -- or report this step as not done and say what it needed."
)


def is_unattended(payload: dict) -> bool:
    """True when nobody is sitting at the session that raised this prompt: a ``claude --bg``
    session or one of its subagents. The evidence is the transcript's head, the only place a
    payload's session states how it was launched (``tools/hooks/transcript.py``)."""

    return is_background(payload)


def call_text(tool_input: object, limit: int = CALL_CHARS) -> str:
    """The first ``limit`` characters of the call, for the event line. A tool whose input has a
    single obvious command string is rendered as that string; anything else as compact JSON."""

    if isinstance(tool_input, dict):
        for key in ("command", "file_path", "path", "url", "pattern", "prompt"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                return value[:limit]
        try:
            return json.dumps(tool_input, ensure_ascii=False, sort_keys=True)[:limit]
        except (TypeError, ValueError):
            return str(tool_input)[:limit]
    return str(tool_input or "")[:limit]


def build_event(payload: dict) -> dict:
    """The one line a refusal writes: the session, the tool, the first characters of the call."""

    return {
        "ts": _now(),
        "kind": EVENT_KIND,
        "session": str(payload.get("session_id") or ""),
        "tool": str(payload.get("tool_name") or ""),
        "call": call_text(payload.get("tool_input")),
    }


def build_decision(reason: str = REFUSAL) -> dict:
    """Claude Code's own deny shape for this event (the docs' "PermissionRequest decision
    control"): ``behavior: deny`` plus the message the agent reads as the tool's result."""

    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                   "decision": {"behavior": "deny", "message": reason}}}


def events_file(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get(EVENTS_ENV) or DEFAULT_EVENTS_FILE)


def handle(payload: dict, env: dict[str, str] | None = None) -> dict | None:
    """``None`` when this session still asks its human; the deny decision when it does not.
    Writes the event line as a side effect; a failed write never withholds the refusal."""

    if not is_unattended(payload):
        return None
    try:
        append_jsonl(events_file(env), build_event(payload))
    except Exception:  # a log that cannot be written must never hold the session up
        pass
    return build_decision()


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
        decision = handle(payload)
    except Exception:  # an unreadable payload leaves the permission flow exactly as it was
        return 0
    if decision is not None:
        sys.stdout.write(json.dumps(decision))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
