"""Brain state inject hook (issue #316).

A hook for the brain's own chat, registered on `SessionStart` (sources `startup`, `clear`, `compact`),
`UserPromptSubmit` and `PreToolUse`. A cleared brain chat starts blind: `docs/agents/brain-reference.md`, "The clear and the hand-off", tells it to
read its hand-off entry in `docs/STATE.md` first, but nothing makes it do so. This hook puts that entry in front of it.

The chat's title is the stdin payload's `session_title` field, the same field `tools/hooks/autopilot_checkpoint.py`
reads (measured on #116, 2026-09-27: present from the first `SessionStart`, before any transcript exists). The title is
a brain title when it equals `brain_chat_title` or any `brain_chats` value of `config/orchestration.json`, read from the
project dir (env `CLAUDE_PROJECT_DIR`, else the payload's `cwd`). A title that is not a brain title, and a background
session (`sessionKind` == `bg` in the payload), get no output.

Measured in #316 row 6 (the live clear of "Brain: orch layer", 2026-10-23, transcript 23efb267): the chat's
`custom-title` record is after the first user record and before the first tool call, so the title probably was not in
the `SessionStart` payload. Whether it was is not measured yet; the payload log below records it.

Fallback (#316 unit 3): the same script also runs on `UserPromptSubmit` and `PreToolUse`. There the title is the
payload's `session_title` when it is a non-empty str, else the last `custom-title` record's `customTitle` in the first
256 KB of the file at the payload's `transcript_path`; a `bg` session is one whose payload or that head says
`sessionKind` == `bg`. A session is injected once: its marker `<dir>/<session_id>.json` (dir `var/agentops/brain_inject/`,
env `AGENTOPS_BRAIN_INJECT_DIR`) holds {"injected", "tries"}; a non-brain session stops trying after 5 events. The
`SessionStart` inject writes the marker too, and `compact` re-injects whatever the marker says (its title falls back to the transcript head too).

For the brain it runs `git fetch origin` (at most 10 s, no console window) and reads `docs/STATE.md` from
`origin/<default branch>` (the branch from `origin/HEAD`, else `main`). When the fetch fails it reads the
working copy and the injected text says so. It injects the whole first line starting `Updated (brain)` as
`hookSpecificOutput.additionalContext`, after one line that says this chat is the brain.

Logs: every injection, skip (with `why`: `event`, `source`, `bg`, `no-title`, `not-brain-title`) and error is one line of
``var/agentops/brain_nudge.log`` (``AGENTOPS_HOOK_LOG`` overrides). Every non-bg `SessionStart` also appends its raw
payload (every string cut at 500 chars, with its sorted keys) to ``var/agentops/session_start_payloads.jsonl``
(``AGENTOPS_SESSION_PAYLOAD_LOG`` overrides).

Rules mode (#348): `brain_state_inject.py rules` (the script's one argument) is a second mode for the same sessions
and events. It injects its own `additionalContext` string: the head line `RULES_HEAD_PREFIX + <source>` and the whole
text of `docs/agents/brain.md`, read from `origin/<default branch>` WITHOUT a `git fetch` of its own (the hand-off mode
fetches in parallel, and two fetches at once contend for the repo's locks), else from the working copy. Same title
gate, `bg` and autopilot skips, fallback events and `compact` re-injection as the hand-off mode; its own marker
`<dir>/<session_id>.rules.json`, so one mode's success never stops the other; every log row carries `mode: rules`.
Claude Code caps a hook string at 10,000 characters: when the head line, a newline and the file exceed it, the string is
the head line and `docs/agents/brain.md is <N> characters, over the 10,000-character hook cap: read it whole now`, and
one `rules-over-cap` row (with `chars`) is logged. No argument is the hand-off mode, as before.

Never raises: any error exits 0 silently and appends one line to ``var/agentops/brain_nudge.log``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

#: this hook runs as a plain ``python tools/hooks/brain_state_inject.py``: the repo root goes on ``sys.path`` so the
#: shared role resolver imports (the same line ``brain_reset_nudge.py`` has).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.hooks.session_role import fires  # noqa: E402

#: issue #320: state injection stays title-gated as before (the checks of ``_session_start`` / ``_fallback`` run
#: first): the autopilot (``agentSetting: brain``, no title) is the brain by role but gets no injection, as it never did.
#: The roles the rule binds (``tools/hooks/session_role.py``); the injection is guarded by ``fires``.
RULE_ROLES: dict[str, tuple[str, ...]] = {
    "hand-off-injection": ("brain",),
    "rules-injection": ("brain",),
}
FETCH_TIMEOUT_SECONDS = 10
#: fetch 10 s + two local git calls of 2 s stay under the 15 s hook timeout in the settings files.
GIT_TIMEOUT_SECONDS = 2
#: `tools.agentops.util.no_window_flags`, copied rather than imported: this hook runs standalone under a plain
#: `python` with no `tools` package on `sys.path` (Claude Code invokes it directly). No console window for `git`.
_NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_SESSION_START_SOURCES = frozenset({"startup", "clear", "compact"})
_FALLBACK_EVENTS = frozenset({"UserPromptSubmit", "PreToolUse"})
MAX_TRIES = 5
TRANSCRIPT_HEAD_BYTES = 256 * 1024
PAYLOAD_STRING_CUT = 500
BRAIN_LINE_PREFIX = "Updated (brain)"
STATE_PATH = "docs/STATE.md"
HEAD_TEXT = ("This chat is the brain; its hand-off entry from docs/STATE.md follows "
             "(docs/agents/brain-reference.md, The start of a chat)")
RULES_MODE = "rules"
RULES_PATH = "docs/agents/brain.md"
RULES_HEAD_PREFIX = f"This chat is the brain; its rule file {RULES_PATH} follows, read from "
#: Claude Code cuts a hook string past this many characters (https://code.claude.com/docs/en/hooks).
HOOK_CAP = 10_000
WORKING_COPY_SOURCE = "the working copy"
NO_ENTRY_TEXT = "docs/STATE.md has no brain entry"
FETCH_FAILED_NOTE = "git fetch failed, read from the working copy"
_VAR_AGENTOPS = Path(__file__).resolve().parents[2] / "var" / "agentops"
LOG_PATH = _VAR_AGENTOPS / "brain_nudge.log"
PAYLOAD_LOG_PATH = _VAR_AGENTOPS / "session_start_payloads.jsonl"
INJECT_DIR = _VAR_AGENTOPS / "brain_inject"
_ORIGIN_PREFIX = "refs/remotes/origin/"


def _append(path: Path, row: dict) -> None:
    """Append one JSON line; never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _event(payload: dict) -> str:
    """The payload's `hook_event_name`; an unreadable payload (no event) is logged as `SessionStart`, the event
    this log was first written for."""
    event = payload.get("hook_event_name")
    return event if isinstance(event, str) and event else "SessionStart"


#: the mode this process runs in: "" (hand-off) or `RULES_MODE`; set once by `run`. A rules row says so in `mode`.
_mode = ""


def _row(payload: dict, action: str, **fields) -> dict:
    mode = {"mode": _mode} if _mode else {}
    return {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": payload.get("session_id"),
            "event": _event(payload), "action": action, **mode, **fields}


def _log(payload: dict, action: str, **fields) -> None:
    """One row of ``brain_nudge.log`` (the same row shape as `brain_reset_nudge.py`'s log writer); never raises."""
    _append(Path(os.environ.get("AGENTOPS_HOOK_LOG") or LOG_PATH), _row(payload, action, **fields))


def _skip(payload: dict, why: str) -> None:
    _log(payload, "skip", why=why)
    return None


def _cut(value):
    """The payload with every string cut at `PAYLOAD_STRING_CUT` characters, at any depth."""
    if isinstance(value, str):
        return value[:PAYLOAD_STRING_CUT]
    if isinstance(value, dict):
        return {key: _cut(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_cut(item) for item in value]
    return value


def _log_payload(payload: dict) -> None:
    """The raw SessionStart payload, one row, for measuring what a real clear sends."""
    path = Path(os.environ.get("AGENTOPS_SESSION_PAYLOAD_LOG") or PAYLOAD_LOG_PATH)
    _append(path, _row(payload, "payload", keys=sorted(payload), raw=_cut(payload)))


def _project_dir(payload: dict) -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env)
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return Path(cwd)
    return Path(__file__).resolve().parents[2]


def _brain_titles(project: Path) -> set[str]:
    """`brain_chat_title` and every `brain_chats` value of `config/orchestration.json`; empty when the file is
    missing or not valid JSON (then no chat is a brain chat)."""
    try:
        with open(project / "config" / "orchestration.json", encoding="utf-8") as fh:
            settings = json.load(fh)
    except (OSError, ValueError):
        return set()
    if not isinstance(settings, dict):
        return set()
    titles: set[str] = set()
    title = settings.get("brain_chat_title")
    if isinstance(title, str) and title:
        titles.add(title)
    chats = settings.get("brain_chats")
    if isinstance(chats, dict):
        titles.update(value for value in chats.values() if isinstance(value, str) and value)
    return titles


def _payload_title(payload: dict) -> str | None:
    title = payload.get("session_title")
    return title if isinstance(title, str) and title else None


def _transcript_head(path_value: object) -> tuple[str | None, bool]:
    """The last `custom-title` record's `customTitle` and whether a `sessionKind` == `bg` record appears, both in
    the first `TRANSCRIPT_HEAD_BYTES` of the transcript; `(None, False)` when it cannot be read. Lines that are not
    JSON are ignored (the cut may split the last one)."""
    if not isinstance(path_value, str) or not path_value:
        return None, False
    try:
        with open(path_value, "rb") as fh:
            head = fh.read(TRANSCRIPT_HEAD_BYTES)
    except OSError:
        return None, False
    title: str | None = None
    bg = False
    for line in head.decode("utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("type") == "custom-title" and isinstance(record.get("customTitle"), str):
            title = record["customTitle"]
        if record.get("sessionKind") == "bg":
            bg = True
    return title, bg


def _marker_path(payload: dict) -> Path | None:
    session = payload.get("session_id")
    if not isinstance(session, str) or not session:
        return None
    base = Path(os.environ.get("AGENTOPS_BRAIN_INJECT_DIR") or INJECT_DIR)
    return base / (f"{session}.{_mode}.json" if _mode else f"{session}.json")


def _read_marker(path: Path) -> tuple[bool, int]:
    """(injected, tries) from the marker file; a missing or unreadable marker is (False, 0)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, 0
    if not isinstance(data, dict):
        return False, 0
    tries = data.get("tries")
    if not isinstance(tries, int) or isinstance(tries, bool):
        tries = 0
    return data.get("injected") is True, tries


def _write_marker(payload: dict, path: Path, injected: bool, tries: int) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"injected": injected, "tries": tries}), encoding="utf-8")
    except Exception as exc:
        _log(payload, "error", why=f"marker: {type(exc).__name__}: {exc}")


def _git(project: Path, *args: str, timeout: int = GIT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess | None:
    """The finished `git` call, or `None` when it could not run or ran past `timeout`."""
    try:
        return subprocess.run(["git", *args], cwd=project, capture_output=True, timeout=timeout,
                              creationflags=_NO_WINDOW_FLAGS)
    except (OSError, subprocess.SubprocessError):
        return None


def _default_branch(project: Path) -> str:
    """The branch `origin/HEAD` points at (`refs/remotes/origin/<branch>`), else `main`."""
    result = _git(project, "symbolic-ref", f"{_ORIGIN_PREFIX}HEAD")
    if result is not None and result.returncode == 0:
        ref = result.stdout.decode("utf-8", errors="replace").strip()
        if ref.startswith(_ORIGIN_PREFIX) and len(ref) > len(_ORIGIN_PREFIX):
            return ref[len(_ORIGIN_PREFIX):]
    return "main"


def _origin_text(project: Path, branch: str, path: str = STATE_PATH) -> str | None:
    result = _git(project, "show", f"origin/{branch}:{path}")
    if result is None or result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def _working_copy_text(project: Path, path: str = STATE_PATH) -> str | None:
    try:
        return (project / path).read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None


def _brain_line(text: str | None) -> str | None:
    """The whole first line of `text` that starts with `Updated (brain)`, or `None`."""
    if text is None:
        return None
    for line in text.splitlines():
        if line.startswith(BRAIN_LINE_PREFIX):
            return line
    return None


def _inject(payload: dict, event: str, project: Path) -> dict:
    """The brain's hand-off entry as `additionalContext` of `event`; logs one `injected` row. The one inject body
    both the SessionStart path and the fallback path use."""
    fetch = _git(project, "fetch", "origin", timeout=FETCH_TIMEOUT_SECONDS)
    fetched = fetch is not None and fetch.returncode == 0
    notes: list[str] = []
    if fetched:
        branch = _default_branch(project)
        text = _origin_text(project, branch)
        if text is None:
            notes.append(f"origin/{branch} has no {STATE_PATH}, read from the working copy")
            text = _working_copy_text(project)
    else:
        notes.append(FETCH_FAILED_NOTE)
        text = _working_copy_text(project)

    entry = _brain_line(text)
    context = f"{HEAD_TEXT}\n{entry}" if entry is not None else NO_ENTRY_TEXT
    for note in notes:
        context = f"{context}\n{note}"
    _log(payload, "injected", entry=entry is not None, fetch="ok" if fetched else "failed")
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": context,
        }
    }


def _inject_rules(payload: dict, event: str, project: Path) -> dict:
    """The brain's rule file as `additionalContext` of `event`, never over `HOOK_CAP` characters; logs one
    `injected` row (and a `rules-over-cap` row first when the file does not fit). No `git fetch` here."""
    branch = _default_branch(project)
    text = _origin_text(project, branch, RULES_PATH)
    source = f"origin/{branch}"
    if text is None:
        text = _working_copy_text(project, RULES_PATH)
        source = WORKING_COPY_SOURCE
    if text is None:
        context = f"{RULES_PATH} could not be read from origin/{branch} or the working copy"
    else:
        head = f"{RULES_HEAD_PREFIX}{source}"
        context = f"{head}\n{text}"
        if len(context) > HOOK_CAP:
            _log(payload, "rules-over-cap", chars=len(text), source=source)
            context = (f"{head}\n{RULES_PATH} is {len(text)} characters, over the {HOOK_CAP:,}-character "
                       f"hook cap: read it whole now")
    _log(payload, "injected", file=text is not None, source=source if text is not None else None)
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": context,
        }
    }


def _inject_for_mode(payload: dict, event: str, project: Path) -> dict:
    inject = _inject_rules if _mode == RULES_MODE else _inject
    return inject(payload, event, project)


def _rule_name() -> str:
    return "rules-injection" if _mode == RULES_MODE else "hand-off-injection"


def _session_start(payload: dict) -> dict | None:
    source = payload.get("source")
    if source not in _SESSION_START_SOURCES:
        return _skip(payload, "source")
    if payload.get("sessionKind") == "bg":
        return _skip(payload, "bg")
    if not _mode:  # one raw-payload row per SessionStart, written by the hand-off mode
        _log_payload(payload)
    project = _project_dir(payload)
    # a clear or startup has no custom-title record yet (the fallback covers it); a compact has one in the transcript
    title = _payload_title(payload) or _transcript_head(payload.get("transcript_path"))[0] or None
    if title is None:
        return _skip(payload, "no-title")
    titles = _brain_titles(project)
    if title not in titles:
        return _skip(payload, "not-brain-title")
    if not fires(RULE_ROLES[_rule_name()], payload, titles=tuple(titles)):
        return _skip(payload, "role")
    output = _inject_for_mode(payload, "SessionStart", project)
    path = _marker_path(payload)
    if path is not None:
        _write_marker(payload, path, True, _read_marker(path)[1])
    return output


def _fallback(payload: dict) -> dict | None:
    """`UserPromptSubmit` / `PreToolUse`: inject once, at the first event whose title is a brain title."""
    event = _event(payload)
    path = _marker_path(payload)
    if path is None:
        _log(payload, "error", why="no session_id")
        return None
    injected, tries = _read_marker(path)
    if injected or tries >= MAX_TRIES:
        return None
    tries += 1
    _write_marker(payload, path, False, tries)
    project = _project_dir(payload)
    transcript_title, transcript_bg = _transcript_head(payload.get("transcript_path"))
    title = _payload_title(payload) or transcript_title or None
    if payload.get("sessionKind") == "bg" or transcript_bg:
        return _skip(payload, "bg")
    if title is None:
        return _skip(payload, "no-title")
    titles = _brain_titles(project)
    if title not in titles:
        return _skip(payload, "not-brain-title")
    if not fires(RULE_ROLES[_rule_name()], payload, titles=tuple(titles)):
        return _skip(payload, "role")
    output = _inject_for_mode(payload, event, project)
    _write_marker(payload, path, True, tries)
    return output


def run(payload: dict, mode: str = "") -> dict | None:
    """`mode` is "" (the hand-off entry) or `RULES_MODE` (the rule file)."""
    global _mode
    _mode = mode
    event = payload.get("hook_event_name")
    if event == "SessionStart":
        return _session_start(payload)
    if event in _FALLBACK_EVENTS:
        return _fallback(payload)
    return _skip(payload, "event")


def main() -> int:
    payload: dict = {}
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            raise ValueError("stdin payload is not a JSON object")
        payload = loaded
        mode = RULES_MODE if sys.argv[1:2] == [RULES_MODE] else ""
        output = run(payload, mode)
        if output is not None:
            print(json.dumps(output))
    except Exception as exc:
        _log(payload, "error", why=f"{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
