"""What the head of a session's transcript says about the session itself.

Every Claude Code hook payload carries a ``transcript_path``, and the first
records of that file carry the session's launch facts: ``sessionKind`` (``bg``
for a session started with ``claude --bg``) and ``agentSetting`` (the ``--agent``
it was started with). Nothing else in a payload states them -- ``permission_mode``
reads ``default`` even under an auto ``--permission-mode``, and a background session
does not inherit its launcher's environment (both measured, #213), so the
transcript head is the only place to ask.

A subagent writes its own transcript under the parent session's directory, and
Claude Code copies ``sessionKind`` into every one of its records too (measured,
#213): the same head read answers for a subagent's prompt as for its parent's.

Two hooks ask: ``brain_reset_nudge`` (nudge only the brain) and
``refuse_permission_prompt`` (refuse only where nobody sits). Never raises: a
missing or unreadable transcript simply yields nothing, and each caller falls
back to leaving the session alone.
"""

from __future__ import annotations

import json
import os
import re
from typing import Iterator, Sequence

#: How much of the transcript is read. The launch records are the first few lines, but one of them
#: can be a whole system prompt, so the window is generous.
HEAD_CHUNK_SIZE = 64 * 1024


def head_records(path: object, keys: Sequence[str],
                 chunk_size: int = HEAD_CHUNK_SIZE) -> Iterator[dict]:
    """Every JSON object in the transcript's head whose line mentions one of ``keys``.

    ``keys`` is a plain substring filter applied before parsing, because the head holds a few very
    long lines and only the ones naming a key are worth a ``json.loads``. Yields nothing for a
    missing, unreadable or unparsable file.
    """

    if not isinstance(path, str) or not path:
        return
    try:
        with open(path, "rb") as fh:
            head = fh.read(chunk_size).decode("utf-8", errors="ignore")
    except OSError:
        return
    for line in head.split("\n"):
        if not any(key in line for key in keys):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def is_background(payload: object, chunk_size: int = HEAD_CHUNK_SIZE) -> bool:
    """True when this hook payload comes from a ``claude --bg`` session or a subagent of one.

    False for an interactive session somebody is sitting at, and false when the transcript cannot
    be read at all: an unreadable transcript is not evidence that nobody is there.
    """

    path = payload.get("transcript_path") if isinstance(payload, dict) else None
    for record in head_records(path, ("sessionKind",), chunk_size):
        if record.get("sessionKind") == "bg":
            return True
    return False


# -- background things a session still has running --------------------------------------------------
#
# Record shapes (measured on real transcripts, #311). A background thing is announced by the
# ``tool_result`` of its launch, whose text starts with one fixed phrase and names the id:
#   Agent   "Async agent launched successfully. ... agentId: a92546000dbe92ddd (internal ID ..."
#   Bash    "Command running in background with ID: blcg3vv5o. Output is being written to: ..."
#   Monitor "Monitor started (task b36wayjkg, expires in 30m unless the source ends first; ..."
# It ends with a ``<task-notification>`` naming ``<task-id>`` with a ``<status>`` other than
# ``running`` (raw, unescaped tags in ``queue-operation`` / ``attachment`` / ``user`` lines), with a
# Monitor's expiry notice (``<event>[Monitor expired after 30m ...``, no ``<status>`` at all), or
# with a ``TaskStop`` whose result reads ``Successfully stopped task: <id>``. A foreground call
# never produces one of the launch phrases, so it is never counted.

_LAUNCH_PHRASES = (
    ("Async agent launched", "subagent", re.compile(r"agentId:\s*([A-Za-z0-9_-]+)")),
    ("Command running in background with ID:", "shell",
     re.compile(r"with ID:\s*([A-Za-z0-9_-]+)")),
    ("Monitor started (task ", "monitor", re.compile(r"\(task\s+([A-Za-z0-9_-]+)")),
)
_STOPPED_RE = re.compile(r"Successfully stopped task:\s*([A-Za-z0-9_-]+)")
_NOTIFICATION_RE = re.compile(r"<task-notification>(.*?)(?=<task-notification>|\Z)", re.S)
_TASK_ID_RE = re.compile(r"<task-id>([^<]+)</task-id>")
_STATUS_RE = re.compile(r"<status>([^<]+)</status>")
_LAUNCH_MARKERS = tuple(p[0] for p in _LAUNCH_PHRASES)


def _result_texts(obj: dict) -> Iterator[str]:
    """The text of every ``tool_result`` block of a ``user`` record."""

    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        body = block.get("content")
        if isinstance(body, str):
            yield body
        elif isinstance(body, list):
            for part in body:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    yield part["text"]


_SESSION_ID_RE = re.compile(r'"sessionId"\s*:\s*"([^"]+)"')
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _own_session(path: str) -> str | None:
    """The transcript's own session id: the file name's, or else the newest ``sessionId`` in the file.

    Claude Code names a transcript after its session id. When the name is not one, the last record
    that names a session gives it. None when neither exists.
    """

    stem = os.path.splitext(os.path.basename(path))[0]
    if _UUID_RE.fullmatch(stem):
        return stem
    newest: str | None = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                found = _SESSION_ID_RE.findall(line)
                if found:
                    newest = found[-1]
    except OSError:
        pass
    return newest


def live_things(path: object) -> dict[str, int]:
    """Background things the session launched that have not ended: subagents, shells, monitors.

    Streams the transcript line by line and parses only lines that can hold a launch or a stop
    result; notifications are matched on the raw line. Never raises: an unreadable file is all zeros.
    """

    launched: dict[str, tuple[str, object]] = {}
    ended: set[str] = set()
    own: str | None = None
    if isinstance(path, str) and path:
        own = _own_session(path)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "task-notification" in line and '"tool_result"' not in line:
                        for block in _NOTIFICATION_RE.findall(line):
                            m = _TASK_ID_RE.search(block)
                            if not m:
                                continue
                            status = _STATUS_RE.search(block)
                            if (status and status.group(1).strip() != "running") or \
                                    "[Monitor expired" in block:
                                ended.add(m.group(1).strip())
                    elif '"tool_result"' in line and (
                            "Successfully stopped task" in line
                            or any(marker in line for marker in _LAUNCH_MARKERS)):
                        try:
                            obj = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(obj, dict):
                            continue
                        for text in _result_texts(obj):
                            head = text.lstrip()[:60]
                            if "Successfully stopped task" in head:
                                stopped = _STOPPED_RE.search(text[:200])
                                if stopped:
                                    ended.add(stopped.group(1))
                                continue
                            for phrase, kind, id_re in _LAUNCH_PHRASES:
                                if head.startswith(phrase):
                                    m = id_re.search(text)
                                    if m:
                                        launched[m.group(1)] = (kind, obj.get("sessionId"))
                                    break
        except OSError:
            pass
    counts = {"subagent": 0, "shell": 0, "monitor": 0}
    for task_id, (kind, sid) in launched.items():
        if task_id not in ended and (own is None or sid is None or sid == own):
            counts[kind] += 1
    return counts
