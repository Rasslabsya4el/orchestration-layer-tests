"""Brain reset nudge hook (issue #93).

Reads a Claude Code hook JSON payload from stdin and, depending on
``hook_event_name``, may print a ``hookSpecificOutput`` JSON object that
nudges the brain to propose the clear hand-off (docs/agents/brain.md
"Reset"). Never raises: any error, bad stdin, or ``gh`` failure results in a
silent exit 0.

Events handled:

- ``UserPromptSubmit``: reads the transcript's tail, finds the last assistant
  message with a ``usage`` block, and if the summed token count is at or
  above the installation's context-line target (``config/orchestration.json``
  ``context_line``), emits a context-size reminder.
- ``PostToolUse`` (matcher ``Bash``): reads the tool call's own output
  (``tool_response`` stdout/stderr) for gh's own success line
  (``✓ Closed issue <repo>#N``, the same shape ``scripts/cost_levers.py``
  ``session_gh_closes`` reads). A close is credited only from that line, never
  from the text of the command: the string ``gh issue close N`` inside a
  comment/issue body or a heredoc raises nothing, and a command whose call
  failed (no success line in the output) raises nothing either (#196). Every
  issue number the output credits is checked for a ``wayfinder:`` label; one
  nudge names every wayfinder ticket a single call closed.

Only the brain (an interactive chat) is nudged. A background session
(``sessionKind`` ``bg``: ORCH started by the dispatcher, or another background
role) or one started with another ``--agent`` is skipped, so ORCH never reads
a reminder it could take as a cue to park early. Every nudge and every skip is
appended to ``var/agentops/brain_nudge.log`` (``AGENTOPS_HOOK_LOG`` overrides).

- ``UserPromptSubmit`` also carries the first-prompt brain-title check
  (issue #221): a target's first brain chat, opened as a plain chat by a
  desktop app that "offers no way to pick an agent for a new chat" (#221),
  reads no role file and sets no model of its own. The chat's own title is
  read from its transcript's ``custom-title`` record, the app's own write on
  a rename (the same record ``scripts/token_meter.py``'s
  ``parse_main_transcript`` already reads to classify a chat by title). On
  the first prompt of a transcript with no earlier assistant turn, a title
  equal to ``brain_chat_title`` (``config/orchestration.json``, read the same
  way this hook already reads ``context_line``, ``tools/agentops/core_settings.py``)
  injects, once, that this chat is the brain and to read
  ``docs/agents/brain.md``; it also names the core's own brain row
  (``docs/agents/core-block.md``, "Roles") next to the model this
  transcript's own ``model`` attachment record names, when that model is not
  the primary one (Fable 5.1) -- no Claude Code hook payload or transcript
  record carries a chat's *effort*, so the note only compares by model name,
  and treats the fallback model alone (Opus 5.5) as worth a check too, since
  its own required ``xhigh`` cannot be confirmed this way.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from tools.agentops.core_settings import load as _load_settings, tickets_repo as _tickets_repo_of  # noqa: E402
from tools.agentops.util import no_window_flags  # noqa: E402
from tools.hooks.transcript import HEAD_CHUNK_SIZE, head_records, live_things  # noqa: E402

# The same regex `scripts/cost_levers.py` `session_gh_closes` reads gh's own success line
# through (`CLOSED_OUTPUT_RE`): reused here rather than reinvented, per #196.
from cost_levers import CLOSED_OUTPUT_RE as _CLOSE_RE  # noqa: E402

TAIL_CHUNK_SIZE = 256 * 1024
#: the brain-title check (issue #221) reads the settings once at import time.
BRAIN_CHAT_TITLE = _load_settings()["brain_chat_title"]
GH_TIMEOUT_SECONDS = 5
LOG_PATH = Path(__file__).resolve().parents[2] / "var" / "agentops" / "brain_nudge.log"
#: the core's own brain row (issue #221): read fresh from the one place it is written
#: (``docs/agents/core-block.md``, "Roles"), never duplicated here as a literal string.
CORE_BLOCK_PATH = _ROOT / "docs" / "agents" / "core-block.md"
#: the brain row's whole model/effort cell (e.g. "Fable 5.1 high (fallback Opus 5.5 xhigh)"), and
#: within it the primary model name up to its effort word (e.g. "Fable 5.1").
_BRAIN_ROW_RE = re.compile(r"\|\s*brain\s*\|[^|]*\|\s*([^|]+?)\s*\|\s*talks to the owner")
_BRAIN_ROW_PRIMARY_RE = re.compile(r"^([A-Za-z0-9 .\-]+?)\s+(?:low|medium|high|xhigh|max)\b")


def _sizes() -> tuple[int, int]:
    """``(context_line, context_ceiling)`` read from the settings at call time (issue #311).

    ``AGENTOPS_HOOK_SETTINGS`` points at another settings file (tests only): one way, every test.
    """
    settings = _load_settings(os.environ.get("AGENTOPS_HOOK_SETTINGS") or None)
    return int(settings["context_line"]), int(settings["context_ceiling"])


def _nudge_text(n: int, line: int, ceiling: int, live: dict) -> tuple[str, str]:
    """``(kind, text)`` for a size at or over the line: ``wait``, ``ceiling`` or ``line``."""
    k, line_k, ceiling_k = round(n / 1000), round(line / 1000), round(ceiling / 1000)
    over_ceiling = n >= ceiling
    total = sum(live.values())
    if total:
        parts = ", ".join(f"{live[key]} {label}" for key, label in (
            ("subagent", "subagent(s)"), ("shell", "background shell(s)"), ("monitor", "monitor(s)"))
            if live.get(key))
        text = (
            f"Context is {k}k, over the context {'ceiling' if over_ceiling else 'line'} "
            f"({ceiling_k if over_ceiling else line_k}k). {total} thing(s) of this session live "
            f"({live['subagent']} subagent(s), {live['shell']} background shell(s), "
            f"{live['monitor']} monitor(s)) -- wait for them; write no hand-off yet. "
            f"Open every reply to the owner with one line: the context size, that it is over the "
            f"context {'ceiling' if over_ceiling else 'line'}, and that you are waiting for {parts}."
        )
        if over_ceiling:
            text += " Start nothing new."
        return "wait", text
    if over_ceiling:
        return "ceiling", (
            f"Context is {k}k, over the context ceiling ({ceiling_k}k): do nothing but the hand-off "
            "now, whatever is in work, ending with one ```text copy block that holds only the next chat's "
            "start line. What the next chat must do first goes into that same line as its arguments, never a separate "
            'sentence in prose. A pending owner decision goes in as `ask the owner: <decision>`, never as a blank for the owner to fill in. Start nothing (docs/agents/brain.md "Reset"). Never clear yourself.'
        )
    return "line", (
        f"Context is {k}k, over the context line ({line_k}k). If you are the brain: take no new work. "
        "A small thing (an answer, a ticket to write) is done; anything else goes into a ticket and "
        "the reply is the hand-off, ending with one ```text copy block that holds only the next chat's start line. What the next chat must do first goes into that same line as its arguments, never a separate sentence in prose. A pending owner decision goes in as `ask the owner: <decision>`, never as a blank for the owner to fill in. "
        "A long task is cut at its clean point (a grill round recorded), "
        "the hand-off is given, and the task goes on in a new chat. Every reply to the owner opens "
        "with one line: the context size, that it is over the context line, the step after which the "
        "clear comes. Start a subagent or background task only to finish the step in work "
        '(docs/agents/brain.md "Reset"). Never clear yourself.'
    )


def _read_transcript_tail(path: str, chunk_size: int = TAIL_CHUNK_SIZE) -> str:
    """Return the tail of the transcript file as text.

    Reads at most ``chunk_size`` bytes from the end of the file. If the read
    did not start at byte 0, the first (possibly partial) line is dropped.
    """
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size <= chunk_size:
            f.seek(0)
            data = f.read()
            drop_first = False
        else:
            f.seek(size - chunk_size)
            data = f.read()
            drop_first = True
    text = data.decode("utf-8", errors="ignore")
    lines = text.split("\n")
    if drop_first and lines:
        lines = lines[1:]
    return "\n".join(lines)


def _last_assistant_usage_total(transcript_path: str) -> int | None:
    """Sum input/cache tokens of the last assistant message with usage."""
    tail = _read_transcript_tail(transcript_path)
    last_usage = None
    for line in tail.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if isinstance(usage, dict):
            last_usage = usage
    if last_usage is None:
        return None
    return (
        (last_usage.get("input_tokens") or 0)
        + (last_usage.get("cache_creation_input_tokens") or 0)
        + (last_usage.get("cache_read_input_tokens") or 0)
    )


def _not_brain(payload: dict) -> str | None:
    """Why this session is not the brain, or None when it is.

    The brain is an interactive chat: no ``agent_type`` other than ``brain`` in
    the payload, and no ``sessionKind: bg`` or other ``agentSetting`` in the
    transcript's head (both written there for ``claude --bg --agent orch``).
    """
    agent_type = payload.get("agent_type")
    if isinstance(agent_type, str) and agent_type and agent_type != "brain":
        return f"agent {agent_type}"
    for obj in head_records(payload.get("transcript_path"), ("sessionKind", "agentSetting"),
                            HEAD_CHUNK_SIZE):
        if obj.get("sessionKind") == "bg":
            return "background session"
        setting = obj.get("agentSetting")
        if isinstance(setting, str) and setting and setting != "brain":
            return f"agent {setting}"
    return None


def _log(payload: dict, event: str, action: str, **fields) -> None:
    """Append one JSON line per nudge or skip; never raises."""
    try:
        path = Path(os.environ.get("AGENTOPS_HOOK_LOG") or LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": payload.get("session_id"),
               "event": event, "action": action, **fields}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _chat_title(transcript_path: str) -> str | None:
    """The chat's own title, the last ``custom-title`` record in the transcript's head (a
    rename overwrites the one before it, so the last one read wins; the app writes this record
    on every rename, issue #221)."""
    title = None
    for obj in head_records(transcript_path, ("custom-title",), HEAD_CHUNK_SIZE):
        if obj.get("type") != "custom-title":
            continue
        value = obj.get("customTitle")
        if isinstance(value, str) and value:
            title = value
    return title


def _has_assistant_turn(transcript_path: str) -> bool:
    """True once the transcript's head holds an assistant record: the first prompt of a chat
    has none yet (issue #221's "first prompt" gate). A plain substring key (not the exact
    ``"type":"assistant"`` spacing) so this holds regardless of the transcript's own JSON
    whitespace, the same tolerance ``head_records`` already gives ``sessionKind``/``agentSetting``."""
    for obj in head_records(transcript_path, ("assistant",), HEAD_CHUNK_SIZE):
        if obj.get("type") == "assistant":
            return True
    return False


def _session_model(transcript_path: str) -> str | None:
    """The chat's own model, its transcript's ``model`` attachment record (``identity.marketingName``,
    e.g. ``"Opus 5.5"``); the first one found (a session does not change model mid-transcript)."""
    for obj in head_records(transcript_path, ("marketingName",), HEAD_CHUNK_SIZE):
        attachment = obj.get("attachment")
        if not isinstance(attachment, dict) or attachment.get("type") != "model":
            continue
        identity = attachment.get("identity")
        if isinstance(identity, dict):
            name = identity.get("marketingName")
            if isinstance(name, str) and name:
                return name
    return None


def _core_brain_row() -> tuple[str, str] | None:
    """The core's own brain row's whole model/effort cell, and within it the primary model's own
    marketing name (``docs/agents/core-block.md``, "Roles"), parsed fresh every call so this hook
    never duplicates the literal "Fable 5.1 high (fallback Opus 5.5 xhigh)" (issue #221)."""
    try:
        text = CORE_BLOCK_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    row_match = _BRAIN_ROW_RE.search(text)
    if not row_match:
        return None
    cell = row_match.group(1).strip()
    primary_match = _BRAIN_ROW_PRIMARY_RE.match(cell)
    if not primary_match:
        return None
    return primary_match.group(1).strip(), cell


def _brain_title_context(transcript_path: str) -> str | None:
    """Issue #221: at the first prompt of a chat titled ``BRAIN_CHAT_TITLE``, the text to inject
    (told it is the brain, and once more when its model is not the core's primary one). ``None``
    when the title does not match, or this is not the first prompt."""
    if _chat_title(transcript_path) != BRAIN_CHAT_TITLE:
        return None
    if _has_assistant_turn(transcript_path):
        return None
    lines = [
        f'This chat is titled {BRAIN_CHAT_TITLE!r}, the brain\'s own title '
        '(config/orchestration.json "brain_chat_title"): you are the brain. Read '
        'docs/agents/brain.md.'
    ]
    core_row = _core_brain_row()
    model = _session_model(transcript_path)
    if core_row is not None and model is not None and model != core_row[0]:
        _primary, core_cell = core_row
        lines.append(
            f"This chat's own model is {model!r}; the core's brain row "
            f'(docs/agents/core-block.md, "Roles") is {core_cell}: no hook can read a chat\'s '
            "effort, so check the model and effort match by hand."
        )
    return "\n\n".join(lines)


#: issue #65: the reply check is the block between these two markers in ``docs/agents/brain.md``,
#: read at run time (never copied into this file); at most ``REPLY_CHECK_MAX_TOKENS`` by chars / 2.5.
REPLY_CHECK_START = "<!-- reply-check:start -->"
REPLY_CHECK_END = "<!-- reply-check:end -->"
REPLY_CHECK_MAX_TOKENS = 450
BRAIN_MD_PATH = _ROOT / "docs" / "agents" / "brain.md"


def _brain_titles() -> set[str]:
    """The brain chats' titles: ``brain_chat_title`` and every ``brain_chats`` value, read from the
    settings at call time (``AGENTOPS_HOOK_SETTINGS`` points at another file in tests)."""
    try:
        settings = _load_settings(os.environ.get("AGENTOPS_HOOK_SETTINGS") or None)
    except RuntimeError:  # core_settings.load raises RuntimeError for a missing or invalid file
        return {BRAIN_CHAT_TITLE}
    titles = {settings["brain_chat_title"]}
    chats = settings.get("brain_chats")
    if isinstance(chats, dict):
        titles.update(value for value in chats.values() if isinstance(value, str) and value)
    return titles


def _reply_check_block() -> tuple[str | None, str]:
    """``(block, "")`` for the text between the reply-check markers, or ``(None, why)`` for a missing
    file, missing markers, an empty block, or a block over the token cap ("too long")."""
    path = Path(os.environ.get("AGENTOPS_BRAIN_MD") or BRAIN_MD_PATH)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None, "missing file"
    start = text.find(REPLY_CHECK_START)
    end = text.find(REPLY_CHECK_END, start + len(REPLY_CHECK_START)) if start >= 0 else -1
    if start < 0 or end < 0:
        return None, "missing markers"
    block = text[start + len(REPLY_CHECK_START):end].strip()
    if not block:
        return None, "empty block"
    if len(block) / 2.5 > REPLY_CHECK_MAX_TOKENS:
        return None, "too long"
    return block, ""


def _last_chat_title(transcript_path: str) -> str | None:
    """Issue #65: the chat's title as the LAST ``custom-title`` record anywhere in the transcript.

    A desktop chat writes its first custom-title past the 64 KiB head (measured at about byte
    96,000) and every rename after it, up to about 1 MB, so the head-only ``_chat_title`` misses
    the title on every prompt after the first. Streams the file in binary, keeps the lines that
    name the record, and lets the last ``customTitle`` win. Never raises: a missing or unreadable
    file gives the title read so far (None when none)."""
    title = None
    try:
        with open(transcript_path, "rb") as fh:
            for raw in fh:
                if b'"custom-title"' not in raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "custom-title":
                    continue
                value = obj.get("customTitle")
                if isinstance(value, str) and value:
                    title = value
    except (OSError, TypeError):
        return title
    return title


def _reply_check_text(payload: dict, transcript_path: str) -> str | None:
    """Issue #65: the reply check for every owner prompt of a brain chat (title in ``_brain_titles``)."""
    if _last_chat_title(transcript_path) not in _brain_titles():
        return None
    block, why = _reply_check_block()
    if block is None:
        _log(payload, "UserPromptSubmit", "skipped", why=f"reply-check {why}")
        return None
    _log(payload, "UserPromptSubmit", "reply-check", chars=len(block))
    return block


def handle_user_prompt_submit(payload: dict) -> dict | None:
    transcript_path = payload.get("transcript_path")
    if not transcript_path or not isinstance(transcript_path, str):
        return None
    why = _not_brain(payload)
    parts: list[str] = []
    if why is None:
        title_context = _brain_title_context(transcript_path)
        if title_context is not None:
            parts.append(title_context)
        reply_check = _reply_check_text(payload, transcript_path)
        if reply_check is not None:
            parts.append(reply_check)
    n = _last_assistant_usage_total(transcript_path)
    context_line, context_ceiling = _sizes()
    if n is not None and n >= context_line:
        k = round(n / 1000)
        if why:
            _log(payload, "UserPromptSubmit", "skipped", context_k=k, why=why)
        else:
            live = live_things(transcript_path)
            kind, text = _nudge_text(n, context_line, context_ceiling, live)
            _log(payload, "UserPromptSubmit", "nudged", context_k=k, live=sum(live.values()), text=kind)
            parts.append(text)
    if not parts:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n\n".join(parts),
        }
    }


def _gh_labels(n: int, timeout: int = GH_TIMEOUT_SECONDS) -> list[str] | None:
    """Return the label names on issue ``n``, or None on any failure.

    Injectable for tests: set ``AGENTOPS_HOOK_GH`` to an alternate command
    (e.g. ``python path/to/stub.py``) that is invoked in place of ``gh`` with
    the same arguments (``issue view <n> --json labels``).
    """
    try:  # #323: the tickets repo; AGENTOPS_HOOK_SETTINGS names another settings file (tests only)
        repo = _tickets_repo_of(os.environ.get("AGENTOPS_HOOK_SETTINGS") or None)
    except RuntimeError:  # the settings are unreadable: no repo to ask
        return None
    override = os.environ.get("AGENTOPS_HOOK_GH")
    # posix=False: on Windows, paths contain backslashes that posix-mode
    # shlex would treat as escape characters.
    base_cmd = shlex.split(override, posix=False) if override else ["gh"]
    args = base_cmd + ["issue", "view", str(n), "--repo", repo, "--json", "labels"]
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        obj = json.loads(result.stdout)
    except ValueError:
        return None
    labels = obj.get("labels") if isinstance(obj, dict) else None
    if not isinstance(labels, list):
        return None
    names = []
    for label in labels:
        if isinstance(label, dict) and isinstance(label.get("name"), str):
            names.append(label["name"])
    return names


def _tool_response_text(payload: dict) -> str:
    """The tool call's own output (stdout + stderr), never the command text.

    ``tool_response`` is the Bash/PowerShell result Claude Code hands a
    ``PostToolUse`` hook (``stdout``/``stderr`` strings); a bare string
    ``tool_response`` (unlikely, but tolerated) is used as-is.
    """
    response = payload.get("tool_response")
    if isinstance(response, dict):
        parts = []
        for key in ("stdout", "stderr"):
            value = response.get(key)
            if isinstance(value, str):
                parts.append(value)
        return "\n".join(parts)
    if isinstance(response, str):
        return response
    return ""


def handle_post_tool_use(payload: dict) -> dict | None:
    # A close is credited only from gh's own success line in the call's own output
    # (``_CLOSE_RE`` = ``cost_levers.CLOSED_OUTPUT_RE``), never from the text of the
    # command: `gh issue close N` quoted in a comment/issue body or a heredoc raises
    # nothing, and a failed close (no success line) raises nothing either (#196).
    text = _tool_response_text(payload)
    numbers = sorted({int(n) for n in _CLOSE_RE.findall(text)})
    if not numbers:
        # Log the skip only for a call whose text mentions a close (the #196 case: a quoted
        # `gh issue close N`, or a failed close) -- never one line per ordinary Bash call.
        tool_input = payload.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if isinstance(command, str) and "gh issue close" in command:
            _log(payload, "PostToolUse", "skipped", why="no gh close success line in output")
        return None
    why = _not_brain(payload)
    if why:
        _log(payload, "PostToolUse", "skipped", issues=numbers, why=why)
        return None
    wayfinder_ns = []
    for n in numbers:
        labels = _gh_labels(n)
        if labels and any(label.startswith("wayfinder:") for label in labels):
            wayfinder_ns.append(n)
    if not wayfinder_ns:
        _log(payload, "PostToolUse", "skipped", issues=numbers, why="no wayfinder label")
        return None
    _log(payload, "PostToolUse", "nudged", issues=wayfinder_ns)
    names = ", ".join(f"#{n}" for n in wayfinder_ns)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                f"A wayfinder ticket closed ({names}): one decision ticket per "
                "session. Once its follow-ups are done (the owner's edits, "
                "commits), end with the clear hand-off, "
                'docs/agents/brain.md "Reset".'
            ),
        }
    }


def run(payload: dict) -> dict | None:
    event = payload.get("hook_event_name")
    if event == "UserPromptSubmit":
        return handle_user_prompt_submit(payload)
    if event == "PostToolUse":
        return handle_post_tool_use(payload)
    return None


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
