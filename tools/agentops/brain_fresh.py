"""#138: is a brain session sitting after a `/clear` with no owner message yet?

`awaiting_owner_message(transcript)` streams one Claude Code session `.jsonl` and answers True exactly when
the transcript holds a `/clear` command and no owner message after the last one. A missing or unreadable
file is False. Read-only; the transcript parser and `message_text` live in `scripts/token_meter.py`, which
is imported on first use (the same way `monitor_agents.py` imports it), never at import time.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "scripts"

_CLEAR_MARK = "<command-name>/clear</command-name>"

# Records a user-type line can carry that are not the owner typing: the dispatcher's and peers' messages,
# hook and local-command output, task notifications, and the /clear stub itself.
_NOT_OWNER_PREFIXES = (
    "<cross-session-message",
    "<task-notification",
    "<local-command",
    "Another Claude session sent a message",
)


def _token_meter():
    """`scripts/token_meter.py`, imported on the first call that needs it."""

    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import token_meter

    return token_meter


def _is_owner_text(text: str) -> bool:
    """Not a peer's, a notice's or a local command's text. The owner's own start line is a slash command
    (`<command-name>/wayfinder`): it counts; the `/clear` stub is told apart before this is asked."""

    # an empty text is a tool_result-only record of the turn the dispatcher's message started: not the owner
    return bool(text.strip()) and not text.lstrip().startswith(_NOT_OWNER_PREFIXES)


def _is_image_only(message: object) -> bool:
    """A user record with no text whose content holds a block that is not a tool_result (an owner's image)."""

    content = message.get("content") if isinstance(message, dict) else None
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") not in ("tool_result", "text") for b in content)


def awaiting_owner_message(transcript: Path) -> bool:
    """True when the last `/clear` in ``transcript`` has no owner message after it; False otherwise."""

    try:
        token_meter = _token_meter()
        awaiting = False
        for obj in token_meter._read_jsonl(transcript):
            if not isinstance(obj, dict):
                continue
            kind = obj.get("type")
            if kind == "user":
                message = obj.get("message")
                text = token_meter.message_text(message) if isinstance(message, dict) else ""
                if _CLEAR_MARK in text:
                    awaiting = True
                elif awaiting and (_is_owner_text(text) or _is_image_only(message)):
                    awaiting = False
            elif kind == "attachment" and awaiting:
                attachment = obj.get("attachment")
                if isinstance(attachment, dict) and attachment.get("type") == "queued_command":
                    prompt = attachment.get("prompt")
                    if isinstance(prompt, str) and _is_owner_text(prompt):
                        awaiting = False
        return awaiting
    except (OSError, ValueError):
        return False
