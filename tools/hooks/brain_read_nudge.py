"""Brain read nudge hook (issues #131, #178).

A PreToolUse hook with two nudges, both ``hookSpecificOutput.additionalContext``, neither ever a refusal
(no ``permissionDecision``); any error leaves the call alone (exit 0, silent).

- Read run (#131): when a brain chat's trailing run of read calls reaches the third one, it hands the rest to a
  helper: one nudge per run, none on the fourth read.
- Untagged call (#178): a Bash or PowerShell call of a brain chat (title in ``brain_reset_nudge._brain_titles``)
  that has no purpose tag in force since the owner's last message and does not start with a ``# purpose: <word>``
  line is told it is counted untagged and which words exist. The tag state is ``purpose_tag.TagState``, the very
  one ``purposes`` counts with. One nudge per owner message: a marker file ``untagged_nudge/<session>.txt`` next
  to the hook log holds the key of the owner message nudged last. Every nudge, and every skip of a brain chat that
  would have been nudged, is a hook-log row; another agent, a background session, a subagent call or a chat that
  is not a brain are silent and unlogged. An owner message outside the transcript tail leaves the state unknown:
  a logged skip.

The run is counted by ``tools.hooks.brain_read_run`` over the transcript's last megabyte. The
brain test (``_not_brain``) and the log (``_log``) are ``brain_reset_nudge``'s own. A background
session, another agent or a call that is not a read is silent and unlogged (a skip line per read
of every ORCH and worker call would flood the log).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from tools.agentops.purpose_tag import (  # noqa: E402
    PURPOSE_WORDS, SHELL_TOOLS, TagState, is_owner_message, tag_of)
from tools.hooks.brain_read_run import READ, classify, count_run  # noqa: E402
from tools.hooks.brain_reset_nudge import (  # noqa: E402
    LOG_PATH, _brain_titles, _last_chat_title, _log, _not_brain)
from tools.hooks.session_role import fires  # noqa: E402

#: issue #320: the roles each rule binds (``tools/hooks/session_role.py``); both emit paths are guarded by ``fires``.
RULE_ROLES: dict[str, tuple[str, ...]] = {
    "third-read-nudge": ("brain",),
    "untagged-call-nudge": ("brain",),
}
TAIL_BYTES = 1024 * 1024
NUDGE_AT = 3
#: the settings key (config/orchestration.json) naming the repo's neutral status scripts, by path suffix.
SETTINGS_KEY = "read_run_neutral_scripts"
NUDGE_TEXT = ("third read in a row: hand the rest to researcher-find (where, list, grep) or "
              "researcher-how (how, why) with the issue number")
UNTAGGED_TEXT = ("This call is counted untagged: no `# purpose: <word>` tag since the owner's last message.\n"
                 "Start the next Bash or PowerShell call of this stretch with `# purpose: <word>`.\n"
                 "Words: " + ", ".join(PURPOSE_WORDS) + ".")


def _tail(path: object) -> tuple[list[dict], bool]:
    """``(records, truncated)``: the transcript's JSON objects in its last ``TAIL_BYTES``, and whether the
    file is longer than that (the first, partial, line is then dropped).

    Lines that do not parse are skipped. Yields nothing for a missing or unreadable file.
    """
    if not isinstance(path, str) or not path:
        return [], False
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            start = max(0, fh.tell() - TAIL_BYTES)
            fh.seek(start)
            data = fh.read()
    except OSError:
        return [], False
    if start > 0:
        newline = data.find(b"\n")
        data = data[newline + 1:] if newline >= 0 else b""
    records = []
    for line in data.split(b"\n"):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records, start > 0


def _tail_records(path: object) -> list[dict]:
    """The transcript's JSON objects in its last ``TAIL_BYTES`` (see ``_tail``)."""
    return _tail(path)[0]


def neutral_scripts_from_settings() -> tuple[str, ...]:
    """The repo's neutral status scripts: the path suffixes in ``read_run_neutral_scripts`` of
    ``config/orchestration.json`` under the project dir (env ``CLAUDE_PROJECT_DIR``, else the repo root).

    A missing file, a missing key, bad JSON or a value that is not a list gives ``()``; non-string and
    empty entries are dropped. Stdlib only: this hook imports no repo settings module.
    """
    project = os.environ.get("CLAUDE_PROJECT_DIR") or str(_ROOT)
    try:
        with open(Path(project) / "config" / "orchestration.json", encoding="utf-8") as fh:
            settings = json.load(fh)
    except (OSError, ValueError):
        return ()
    value = settings.get(SETTINGS_KEY) if isinstance(settings, dict) else None
    if not isinstance(value, list):
        return ()
    return tuple(suffix for suffix in value if isinstance(suffix, str) and suffix)


def _rule_fires_for(rule: str, payload: dict) -> bool:
    """True when the session's role is one ``RULE_ROLES[rule]`` binds (the brain titles are the settings')."""
    return fires(RULE_ROLES[rule], payload, titles=tuple(_brain_titles()))


def _read_nudge(payload: dict) -> str | None:
    tool_input = payload.get("tool_input") or {}
    neutral = neutral_scripts_from_settings()
    if classify(payload.get("tool_name", ""), tool_input, neutral) != READ:
        return None
    if _not_brain(payload) or not _rule_fires_for("third-read-nudge", payload):
        return None
    reads = count_run(_tail_records(payload.get("transcript_path")),
                      payload.get("tool_use_id"), neutral) + 1
    if reads != NUDGE_AT:
        return None
    _log(payload, "PreToolUse", "read-nudge", reads=reads)
    return NUDGE_TEXT


def state_for_call(records: list[dict], tool_use_id: object, tool_input: object,
                   tool_name: str = "Bash") -> TagState:
    """The tag state once this shell call is issued: the records before the call (those up to its own
    ``tool_use`` record when it is already written, all of them when not), then the call's own first line."""
    state = TagState()
    for obj in records:
        kind = obj.get("type")
        if kind == "user":
            state.see_user(obj)
        elif kind == "assistant":
            message = obj.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if tool_use_id and block.get("id") == tool_use_id:
                    state.see_tool_use(tool_name, tool_input)
                    return state
                state.see_tool_use(block.get("name"), block.get("input"))
    state.see_tool_use(tool_name, tool_input)
    return state


def _last_owner_key(records: list[dict]) -> str | None:
    """A key of the last owner message in ``records``: its ``uuid``, else its timestamp and message."""
    key = None
    for obj in records:
        if obj.get("type") == "user" and is_owner_message(obj):
            uuid = obj.get("uuid")
            key = uuid if isinstance(uuid, str) and uuid else f"{obj.get('timestamp')}|{json.dumps(obj.get('message'))}"
    return key


def _marker_path(payload: dict) -> Path:
    session = payload.get("session_id") or Path(str(payload.get("transcript_path"))).stem
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(session))
    return Path(os.environ.get("AGENTOPS_HOOK_LOG") or LOG_PATH).parent / "untagged_nudge" / f"{safe}.txt"


def _untagged_nudge(payload: dict) -> str | None:
    name = payload.get("tool_name")
    if name not in SHELL_TOOLS or payload.get("agent_id"):
        return None
    tool_input = payload.get("tool_input") or {}
    if tag_of(tool_input) in PURPOSE_WORDS:
        return None
    if _not_brain(payload) or not _rule_fires_for("untagged-call-nudge", payload):
        return None
    path = payload.get("transcript_path")
    # the title check stays beside the role gate: the autopilot (``--agent brain``, no ``custom-title``) is the brain
    # by role but was never told about purpose tags, and #320 keeps that as it was
    if not isinstance(path, str) or _last_chat_title(path) not in _brain_titles():
        return None
    records, truncated = _tail(path)
    owner = _last_owner_key(records)
    if owner is None and truncated:
        _log(payload, "PreToolUse", "untagged-skip", why="owner message outside tail")
        return None
    if state_for_call(records, payload.get("tool_use_id"), tool_input, name).in_force:
        return None
    marker = _marker_path(payload)
    key = owner or "-"
    try:
        if marker.read_text(encoding="utf-8") == key:
            _log(payload, "PreToolUse", "untagged-skip", why="already nudged")
            return None
    except OSError:
        pass
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(key, encoding="utf-8")
    except OSError:
        return None
    _log(payload, "PreToolUse", "untagged-nudge", tool=name)
    return UNTAGGED_TEXT


def run(payload: dict) -> dict | None:
    if payload.get("hook_event_name") != "PreToolUse":
        return None
    texts = []
    for nudge in (_read_nudge, _untagged_nudge):
        try:
            text = nudge(payload)
        except Exception:
            text = None
        if text:
            texts.append(text)
    if not texts:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "\n".join(texts)}}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
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
