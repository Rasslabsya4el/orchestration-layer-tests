"""The session's role, read once for every hook rule (issue #320).

Every hook rule names the role(s) it binds (``brain``, ``orch``, ``worker``, ``reviewer``, ``researcher``,
``unblocker``, or ``any``). ``fires`` says whether a session of a role runs that rule; a session of another role never
runs it.

Order of the reading:

1. the payload's ``agent_type`` (a subagent's name): ``worker`` / ``worker-strong`` -> worker; a name starting
   ``reviewer`` -> reviewer; starting ``researcher`` -> researcher; ``orch`` -> orch; ``brain`` -> brain;
   ``unblocker`` -> unblocker; any other non-empty name -> unknown;
2. else the ``agentSetting`` of the transcript head: the ``--agent`` a main session was started with
   (``claude --bg --agent orch`` writes no ``agent_type`` into its payload); read through the same name table as step 1;
3. else the chat title: the payload's ``session_title``, else the last ``custom-title`` record of the transcript at
   ``transcript_path`` (a rename writes a new record, so the last one wins). A title equal to ``brain_chat_title``
   or to a ``brain_chats`` value of ``config/orchestration.json`` -> brain;
4. otherwise unknown. A cloud session with no title is unknown, never brain.

An unknown session fires only the ``any`` rules. ``session_role`` never raises: any error reads as unknown.
``fires`` raises ``ValueError`` on a role name that is not in ``ROLES`` (a typo guard).

No side effects on import. The settings file and the transcript are read by the functions below, mirroring
``tools/hooks/brain_state_inject.py`` (``_brain_titles``) and ``tools/hooks/brain_reset_nudge.py``
(``_last_chat_title``), which this module does not import: those files run their own code at import time.
``AGENTOPS_HOOK_SETTINGS`` points the settings read at another file (tests). The transcript head is read through
``tools/hooks/transcript.py`` (``head_records``, stdlib only), so the repo root must be on ``sys.path`` as it is for
``brain_reset_nudge.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from tools.hooks.transcript import head_records

ROLES = ("brain", "orch", "worker", "reviewer", "researcher", "unblocker", "any")

_ROOT = Path(__file__).resolve().parents[2]
_WORKER_NAMES = frozenset({"worker", "worker-strong"})
#: the last bytes of a transcript read first for its title; custom-title records are rewritten at later turns, so
#: the tail holds the newest one. A full scan runs only when the tail holds none.
_TAIL_BYTES = 256 * 1024


def _brain_titles() -> frozenset[str]:
    """``brain_chat_title`` and every ``brain_chats`` value; empty when the settings cannot be read."""
    try:
        path = os.environ.get("AGENTOPS_HOOK_SETTINGS") or str(_ROOT / "config" / "orchestration.json")
        with open(path, encoding="utf-8") as fh:
            settings = json.load(fh)
        titles = {settings.get("brain_chat_title")}
        chats = settings.get("brain_chats")
        if isinstance(chats, dict):
            titles.update(chats.values())
    except Exception:
        return frozenset()
    return frozenset(title for title in titles if isinstance(title, str) and title)


def _custom_title_in(lines) -> str | None:
    """The last ``customTitle`` among the ``custom-title`` records of ``lines`` (bytes); None when there is none."""
    title = None
    for raw in lines:
        if b'"custom-title"' not in raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "custom-title":
            value = obj.get("customTitle")
            if isinstance(value, str) and value:
                title = value
    return title


def _last_custom_title(transcript_path: object) -> str | None:
    """The last ``custom-title`` record's ``customTitle`` in the transcript: the 256 KB tail first, then a full scan
    only when the tail holds none. Raises ``OSError`` for an unreadable file (``session_role`` reads that as unknown)."""
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    with open(transcript_path, "rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        fh.seek(max(0, size - _TAIL_BYTES))
        tail = fh.read()
        if size > _TAIL_BYTES:  # the cut landed inside a line: that first partial line is not a record
            tail = tail.split(b"\n", 1)[1] if b"\n" in tail else b""
        title = _custom_title_in(tail.split(b"\n"))
        if title is None:
            fh.seek(0)
            title = _custom_title_in(fh)
    return title


def _agent_setting(transcript_path: object) -> str | None:
    """The ``agentSetting`` launch fact written in the transcript head; None when none is written or unreadable."""
    for obj in head_records(transcript_path, ("agentSetting",)):
        value = obj.get("agentSetting")
        if isinstance(value, str) and value:
            return value
    return None


def _role_from_name(name: str) -> str:
    """The role of an agent name (an ``agent_type`` or an ``agentSetting``); any other name is unknown."""
    if name in _WORKER_NAMES:
        return "worker"
    if name.startswith("reviewer"):
        return "reviewer"
    if name.startswith("researcher"):
        return "researcher"
    if name == "orch":
        return "orch"
    if name == "brain":
        return "brain"
    if name == "unblocker":
        return "unblocker"
    return "unknown"


def session_role(payload: dict, *, titles: tuple[str, ...] | None = None) -> str:
    """One of ``brain``, ``orch``, ``worker``, ``reviewer``, ``researcher``, ``unblocker``, ``unknown``.

    ``titles`` replaces the brain titles read from the settings (tests); None reads the settings at call time.
    """
    try:
        agent_type = payload.get("agent_type")
        if isinstance(agent_type, str) and agent_type:
            # first on purpose: a subagent's transcript can copy its parent's agentSetting (an ORCH's "orch"),
            # so the payload's own agent_type must win over the transcript head below
            return _role_from_name(agent_type)
        # before the title: a main session started with --agent (the ORCH's "orch", the autopilot's "brain") carries
        # no agent_type in its payload, only this launch fact in its transcript head
        setting = _agent_setting(payload.get("transcript_path"))
        if setting is not None:
            return _role_from_name(setting)
        title = payload.get("session_title")
        if not (isinstance(title, str) and title):
            title = _last_custom_title(payload.get("transcript_path"))
        if title is None:
            return "unknown"
        known = _brain_titles() if titles is None else frozenset(titles)
        return "brain" if title in known else "unknown"
    except Exception:
        return "unknown"


def fires(roles: tuple[str, ...], payload: dict, *, titles: tuple[str, ...] | None = None) -> bool:
    """True when a rule bound to ``roles`` runs for this session: ``any`` always, otherwise the session's own role.
    An unknown session runs only ``any`` rules. Raises ``ValueError`` on a role name that is not in ``ROLES``."""
    for name in roles:
        if name not in ROLES:
            raise ValueError(f"fires: {name!r} is not a role in ROLES {ROLES}")
    if "any" in roles:
        return True
    return session_role(payload, titles=titles) in roles
