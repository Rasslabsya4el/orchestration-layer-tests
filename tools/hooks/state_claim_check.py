"""State-claim Stop hook (issue #81).

When the brain's chat ends a reply with a word that claims a state ("готово", "собрано", "свободен",
"можно пользоваться", "перенесено", "работает", "done", "free", ...) but the same turn ran no reading
tool, the hook stops the reply once: it prints ``{"decision": "block", "reason": ...}`` and Claude
Code makes the brain read the state and name the tool, or drop the claim.

Rules (issue #81):

- only the brain (an interactive chat; ``_not_brain`` from ``brain_reset_nudge``, imported, not copied);
- never when the payload's ``stop_hook_active`` is set (one stop per turn, never a loop);
- the turn is the assistant records after the last owner prompt (a real user record: no
  ``tool_result`` block); its final text block is the reply's last words;
- a reading tool is any ``tool_use`` in the turn whose name is not a messaging, editing or
  bookkeeping tool (``_NOT_READING``) and does not start with ``mcp__ccd_turn__``.
- a claim is matched in the reply's prose only (issue #98): not in code spans or ``>`` lines, as
  a whole word (a Russian noun form such as "закрытие" is no claim), not after "не" / "ни", not in
  «…» or "…", and not in a paragraph that states a disclaimer ("не проверял", "not checked").

Every block is one JSON line in ``var/agentops/state_claim.log`` (``AGENTOPS_STATE_CLAIM_LOG``
overrides the path for tests). Never raises: any error, a broken transcript line, a missing
``transcript_path`` or bad stdin leaves the reply alone (exit 0, no output).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from tools.hooks.brain_reset_nudge import _not_brain  # noqa: E402

LOG_PATH = _ROOT / "var" / "agentops" / "state_claim.log"

#: the Russian claim stems (#81). A whole word that starts with a stem claims, whatever its ending
#: (the short forms "закрыт", "закрыта", "закрыто", "закрыты" and the adjective and verb forms
#: "закрытый", "собранный", "перенесла", "перенёсли"). Only a noun form is no claim (#98): the stem
#: followed by "и" ("закрытие", "закрытия", "собрание") or "ени" ("перенесение").
_RU_CLAIM_STEMS = ("закрыт", "собран", "пересобран", "свободен", "запущен", "перенес", "перенёс",
                   "готово", "сделано", "работает", "можно пользоваться")
#: the English claim words; a whole word each ("free" is not "freedom").
_EN_CLAIMS = ("done", "built", "free", "moved", "running", "landed")
_CLAIM_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(s) for s in _RU_CLAIM_STEMS) + r")(?!и|ени)[а-яё]*(?!\w)"
    r"|(?<!\w)(?:" + "|".join(_EN_CLAIMS) + r")(?!\w)",
    re.IGNORECASE)

#: the reply's prose only (issue #98): fenced code, inline code and quoted lines state nothing.
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_QUOTE_LINE_RE = re.compile(r"^[ \t]*>.*$", re.MULTILINE)
_PARAGRAPH_RE = re.compile(r"\n\s*\n")
#: a claim word inside «…» or "…" names the word and claims nothing.
_QUOTED_RE = re.compile(r"«[^»]*»|\"[^\"\n]*\"")
#: "не" or "ни" right before the claim word: "не работает", "не закрыт".
_NEGATED_RE = re.compile(r"(?<!\w)(?:не|ни)\s+$", re.IGNORECASE)
#: a disclaimer anywhere in a paragraph exempts the whole paragraph.
_DISCLAIMER_RE = re.compile(r"(?<!\w)(?:не\s+проверял[аи]?|not\s+checked)(?!\w)", re.IGNORECASE)

#: tools that report, delegate or edit, but read no state of the work.
_NOT_READING = frozenset({"SendMessage", "Agent", "Task", "Write", "Edit", "MultiEdit",
                          "NotebookEdit", "Skill", "ToolSearch"})
_NOT_READING_PREFIX = "mcp__ccd_turn__"

REASON = ("The reply claims a state with no check in this turn. Keep the reply above as it is and add "
          "one line: the fact you read with a tool, naming the tool, or the claim dropped; "
          "do not repeat the reply.")


def _is_reading(name: object) -> bool:
    if not isinstance(name, str) or not name:
        return False
    return name not in _NOT_READING and not name.startswith(_NOT_READING_PREFIX)


def _blocks(obj: dict) -> list[dict]:
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)]


def _is_owner_prompt(obj: dict) -> bool:
    """A user record with no ``tool_result`` block: what the owner typed."""
    return not any(b.get("type") == "tool_result" for b in _blocks(obj))


def _last_turn(path: str) -> tuple[str | None, bool]:
    """The final text of the last turn (None when it has no text) and whether a reading tool ran
    in it. A malformed line raises ValueError: the caller leaves the reply alone."""
    final_text: str | None = None
    reads = False
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            obj = json.loads(line)
            if not isinstance(obj, dict):
                continue
            kind = obj.get("type")
            if kind == "user":
                if _is_owner_prompt(obj):
                    final_text = None
                    reads = False
                continue
            if kind != "assistant":
                continue
            for block in _blocks(obj):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    final_text = block["text"]
                elif block.get("type") == "tool_use" and _is_reading(block.get("name")):
                    reads = True
    return final_text, reads


def _claim_word(text: str) -> str | None:
    """The first claim word of the reply's prose that no exemption covers (issue #98), lowercased;
    None when there is none. Exempt: inside a fenced or inline code span or a quoted (`>`) line,
    right after "не" / "ни", inside «…» or "…", or in a paragraph that states a disclaimer."""
    prose = _QUOTE_LINE_RE.sub("", _INLINE_CODE_RE.sub(" ", _FENCE_RE.sub("\n", text)))
    for paragraph in _PARAGRAPH_RE.split(prose):
        if _DISCLAIMER_RE.search(paragraph):
            continue
        quoted = [m.span() for m in _QUOTED_RE.finditer(paragraph)]
        for match in _CLAIM_RE.finditer(paragraph):
            start = match.start()
            if any(a <= start < b for a, b in quoted):
                continue
            if _NEGATED_RE.search(paragraph, 0, start):
                continue
            return match.group(0).lower()
    return None


def _log(session: object, word: str) -> None:
    """Append one JSON line for a block; never raises."""
    try:
        path = Path(os.environ.get("AGENTOPS_STATE_CLAIM_LOG") or LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": session, "word": word}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


# the role(s) each rule binds (#320): the brain only; run() keeps its own _not_brain gate
RULE_ROLES: dict[str, tuple[str, ...]] = {"claim_without_check": ("brain",)}


def run(payload: dict) -> dict | None:
    if payload.get("stop_hook_active"):
        return None
    if _not_brain(payload) is not None:
        return None
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return None
    try:
        text, reads = _last_turn(path)
    except (OSError, ValueError):
        return None
    if text is None or reads:
        return None
    word = _claim_word(text)
    if word is None:
        return None
    _log(payload.get("session_id"), word)
    return {"decision": "block", "reason": REASON}


def main() -> int:
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
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
