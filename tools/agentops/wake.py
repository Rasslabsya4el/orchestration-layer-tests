"""Wake a desktop chat from a script: one headless `claude -p` that calls `SendMessage` once.

A Python script cannot call `SendMessage` itself. Measured in #123 (branch
`research/autopilot-stuck-wake`, `docs/research/2026-09-27-autopilot-stuck-wake.md`): a plain
`subprocess.run` of `claude.exe -p --model haiku --output-format json --allowedTools=SendMessage
--name dispatcher "<call SendMessage once>"` delivers the message and an idle receiver acts 9.5-13.6 s
after launch, for about $0.03, with 0 permission prompts and 0 classifier denials.

Three traps that shape this module, all from #123:

* **A missing target still exits rc 0.** Delivery is judged by :func:`judge_reply` (#302): rc 0, no
  ``is_error``/``permission_denials``, and ``"success":true`` or a failure-free sent/delivered in ``result``.
* **`-p`, never `--bg`.** `--bg` fails the workspace-trust check from 2.1.281; `-p` skips it.
* **cwd outside the repo, and the full path to the exe** (`claude` is not on PATH here).

The one caller today is a background queue daemon's "nearly out of capacity" message to the
brain chat (issue #132 requirement 6); the dispatcher's own `autopilot-stuck` push is the other
user #123 was opened for.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: `claude -p` must finish well inside a daemon tick's patience; #123 measured 9.5-13.6 s to delivery.
TIMEOUT_SECONDS = 180.0
#: What a delivered `SendMessage` leaves in the envelope's `result` text (#123: rc 0 proves nothing).
SUCCESS_MARK = '"success":true'


def find_claude_exe() -> Path:
    """``tools/agentops/dispatch.py``'s own finder, imported rather than copied."""

    try:
        from tools.agentops.dispatch import find_claude_exe as finder
    except ImportError:  # a caller that has not put the repo root on sys.path
        sys.path.insert(0, str(PROJECT_ROOT))
        from tools.agentops.dispatch import find_claude_exe as finder
    return finder()


def build_prompt(chat: str, message: str) -> str:
    """The one instruction the headless session gets: send this, do nothing else."""

    return (
        f"Call the SendMessage tool exactly once, with to={chat!r} and this exact message:\n"
        f"{message}\n"
        "Do nothing else: no files, no other tools, no questions, no summary."
    )


def build_wake_command(chat: str, message: str, exe: Path | str) -> list[str]:
    """The exact argv #123 measured, as a list (no ``shell=True``)."""

    return [
        str(exe), "-p", "--model", "haiku", "--output-format", "json",
        "--allowedTools=SendMessage", "--name", "dispatcher",
        build_prompt(chat, message),
    ]


_DELIVERED_RE = re.compile(r"\b(sent|delivered)\b", re.IGNORECASE)
_DELIVERY_FIELD_RE = re.compile(r"\(delivery:\s*([^;)]*?)\s*(?:;|\))", re.IGNORECASE)
_NEGATED_RE = re.compile(
    r"(?:\bnot|\bnever|n't|\bfailed\s+to|\bunable\s+to|\bcould\s+not)\s+(?:\w+\s+)?(?:be\s+)?(?:sent|delivered|deliver)\b",
    re.IGNORECASE)
_SENT_TO_RE = re.compile(r"^\W*(?:\w+\s+){0,3}(?:sent|delivered)\s+to\b", re.IGNORECASE)
_FAILURE_WORDS = ("fail", "error", "could not", "couldn't", "cannot", "can't", "unable", "not sent",
                  "not delivered", "denied", "no such", "not found", '"success":false')
_ARROW = "→"
#: the delivered phrases of the chat's own replies (#55, seen 2026-10-16 in artifacts/runs/dispatch.out)
_NEW_PHRASES = ("went out", "another claude session")


def _one_line(text: str, limit: int = 400) -> str:
    """A judge detail on one line, at most ``limit`` characters (#55)."""

    return " | ".join(text.splitlines())[:limit]


def _delivered_phrase(result_text: str, chat: str) -> bool:
    """True on the flag without its leading quote, "went out", "another Claude session", or an arrow
    followed by the target title ``chat`` (#55); whitespace squeezed, the title matched whole."""

    spaced = " ".join(result_text.lower().split())
    if 'success":true' in "".join(result_text.split()) or any(p in spaced for p in _NEW_PHRASES):
        return True
    title = " ".join(chat.lower().split())
    return bool(title) and re.search(re.escape(_ARROW) + " " + re.escape(title) + r"(?![\w:])", spaced) is not None


def judge_reply(returncode: int | None, stdout: str, chat: str = "") -> tuple[bool, str]:
    """``(delivered, detail)`` (#302): False on rc != 0, empty/non-JSON stdout, ``is_error`` or
    ``permission_denials``; else True on ``"success":true`` in ``result``, or on a whole-word
    sent/delivered in ``result`` with no failure word (the headless model's own summary varies).
    #55: also True on a failure-free reply holding ``went out``, ``another Claude session`` or an
    arrow to ``chat``; the detail of a rejected reply is one line."""

    text = (stdout or "").strip()
    if returncode not in (None, 0):
        return False, f"claude exited {returncode}: {text[:200]}"
    if not text:
        return False, "claude printed nothing"
    try:
        envelope = json.loads(text)
    except ValueError:
        return False, f"could not parse the claude envelope: {text[:200]}"
    if not isinstance(envelope, dict):
        return False, f"the claude envelope is not a JSON object: {text[:200]}"
    result = envelope.get("result")
    result_text = str(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False))
    if envelope.get("is_error"):
        return False, f"claude reported is_error: {result_text[:200]}"
    if envelope.get("permission_denials"):
        return False, f"claude reported permission_denials: {result_text[:200]}"
    squeezed = "".join(result_text.split())
    if SUCCESS_MARK in result_text or SUCCESS_MARK in squeezed:
        return True, result_text[:2000]
    field = _DELIVERY_FIELD_RE.search(result_text)
    if field:
        if field.group(1).lower() in ("delivered", "queued"):
            return True, result_text[:2000]
        return False, f"SendMessage delivery field says {field.group(1)}: {result_text[:200]}"
    if _NEGATED_RE.search(result_text):
        return False, _one_line(f"SendMessage did not report success: {result_text}")
    low = result_text.lower()
    lows = squeezed.lower()
    if _DELIVERED_RE.search(result_text):
        # a plain "sent to <chat>" is delivered whatever the chat is called; a failure word
        # elsewhere only counts when the sent/delivered word is not followed by "to"
        if _SENT_TO_RE.search(result_text) or not any(w in low or w in lows for w in _FAILURE_WORDS):
            return True, result_text[:2000]
    if _delivered_phrase(result_text, chat) and not any(w in low or w in lows for w in _FAILURE_WORDS):
        return True, result_text[:2000]
    return False, _one_line(f"SendMessage did not report success: {result_text}")


def wake(
    chat: str,
    message: str,
    exe: Path | str | None = None,
    *,
    run: Callable[..., Any] = subprocess.run,
    timeout: float = TIMEOUT_SECONDS,
    cwd: Path | str | None = None,
) -> tuple[bool, str]:
    """Deliver ``message`` to the desktop chat ``chat``. ``(delivered, detail)``; never raises."""

    try:
        target_exe = exe if exe is not None else find_claude_exe()
    except Exception as exc:
        return False, f"could not find claude.exe ({exc!r})"
    folder = Path(cwd) if cwd is not None else Path(tempfile.gettempdir())
    cmd = build_wake_command(chat, message, target_exe)
    try:
        result = run(cmd, cwd=str(folder), capture_output=True, text=True,
                     encoding="utf-8", errors="replace", timeout=timeout)
    except Exception as exc:
        return False, f"claude -p failed to run ({exc!r})"
    ok, detail = judge_reply(getattr(result, "returncode", None), str(getattr(result, "stdout", "") or ""), chat)
    if not ok:
        stderr = str(getattr(result, "stderr", "") or "").strip()
        if stderr:
            detail = f"{detail}; stderr {stderr[:200]}"
        detail = _one_line(detail)  # #55: the whole rejected detail, stderr included, one line
    return ok, detail


__all__ = ["SUCCESS_MARK", "judge_reply", "TIMEOUT_SECONDS", "build_prompt", "build_wake_command", "find_claude_exe", "wake"]
