"""SubagentStop check (issue #104).

When a ``worker`` or ``worker-strong`` subagent stops, its final report is checked against its own transcript
(``agent_transcript_path``; the main session's ``transcript_path`` is never read):

- a report line ``tdd:``, ``diagnosis:``, ``writing-for-agents:``, ``domain-modeling:`` or ``codebase-design:``
  that claims a call (any value but a no-claim form such as ``not a defect``, ``n/a``, ``none`` or ``skipped (...)``)
  with no Skill ``tool_use`` naming that skill (with or without a ``mattpocock-skills:`` prefix) blocks the stop;
- a unit with an Edit, Write or MultiEdit of a code file (not ``.md``, not under a scratchpad or tmp path) and no
  ``tdd:`` / ``tdd skipped`` line, and no Skill ``tdd`` call, blocks the stop.

Rules (issue #104): other agent types are never checked; ``stop_hook_active`` passes (one block per subagent, never a
loop); the final message is ``last_assistant_message``, falling back to the last assistant text block of the
subagent's transcript, and only its last 3000 characters are read; all problems of one stop go into one block.

Every block is one JSON line in ``var/agentops/subagent_skill_check.log`` (``AGENTOPS_SUBAGENT_SKILL_CHECK_LOG``
overrides the path for tests). Never raises: any error, a missing or broken transcript, or bad stdin leaves the stop
alone (exit 0, no output).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
LOG_PATH = _ROOT / "var" / "agentops" / "subagent_skill_check.log"

CHECKED_ROLES = frozenset({"worker", "worker-strong"})
TAIL_CHARS = 3000
CODE_SUFFIXES = (".py", ".js", ".ts", ".tsx", ".ps1", ".sh", ".html", ".css", ".json")
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit"})

#: report line label -> the skill its "called" form claims
SKILL_OF_LABEL = {
    "tdd": "tdd",
    "diagnosis": "diagnosing-bugs",
    "writing-for-agents": "writing-for-agents",
    "domain-modeling": "domain-modeling",
    "codebase-design": "codebase-design",
}

_LINE_RE = re.compile(
    r"(?im)^\W*(tdd|diagnosis|writing-for-agents|domain-modeling|codebase-design)\s*:[ \t]*(.*)$")
#: a tdd line anywhere in the report text (not only at line start), and only as "called" or "skipped"
_TDD_PRESENT_RE = re.compile(r"(?i)\btdd\W*(?:called\b|skipped\b)")
#: the explicit list of no-claim values, matched at the start of the value after its leading markup is stripped
_NO_CLAIM_RE = re.compile(
    r"^(skipped|n/a|na|none|not a defect|no defect|not applicable|not run|not needed"
    r"|not called|not used|not invoked)\b",
    re.IGNORECASE)
_LEADING_MARKUP_RE = re.compile(r"^\W+")
_FENCES = ("```", "~~~")

REASON_HEADER = "The stop is held once; fix the final report, then end again:"


def _blocks(obj: dict) -> list[dict]:
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)]


def _read_records(path: str) -> list[dict]:
    """The transcript's JSON objects. A malformed line raises ValueError; a missing file raises OSError."""
    records: list[dict] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            obj = json.loads(line)
            if isinstance(obj, dict):
                records.append(obj)
    return records


def _final_text(payload: dict, records: list[dict]) -> str:
    message = payload.get("last_assistant_message")
    if isinstance(message, str) and message.strip():
        return message
    last = ""
    for obj in records:
        if obj.get("type") != "assistant":
            continue
        for block in _blocks(obj):
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                last = block["text"]
    return last


def _tool_uses(records: list[dict]) -> list[dict]:
    return [b for obj in records if obj.get("type") == "assistant"
            for b in _blocks(obj) if b.get("type") == "tool_use"]


def _skill_names(records: list[dict]) -> set[str]:
    """The skills called in the transcript, lower-cased, without a ``prefix:``."""
    names: set[str] = set()
    for block in _tool_uses(records):
        if block.get("name") != "Skill":
            continue
        inp = block.get("input")
        skill = inp.get("skill") if isinstance(inp, dict) else None
        if isinstance(skill, str) and skill.strip():
            names.add(skill.strip().rsplit(":", 1)[-1].strip().lower())
    return names


def _is_code_path(path: str) -> bool:
    lowered = path.lower().replace("\\", "/")
    if not lowered.endswith(CODE_SUFFIXES):
        return False
    return not any("scratchpad" in part or part in ("tmp", "temp") for part in lowered.split("/"))


def _code_edits(records: list[dict]) -> list[str]:
    paths: list[str] = []
    for block in _tool_uses(records):
        if block.get("name") not in EDIT_TOOLS:
            continue
        inp = block.get("input")
        path = inp.get("file_path") if isinstance(inp, dict) else None
        if isinstance(path, str) and _is_code_path(path):
            paths.append(path)
    return paths


def _report_text(tail: str) -> str:
    """The lines that can be report lines: not inside a fenced code block, not a blockquote line."""
    kept: list[str] = []
    fence: str | None = None
    for line in tail.splitlines():
        stripped = line.lstrip()
        marker = stripped[:3]
        if fence is None and marker in _FENCES:
            fence = marker
            continue
        if fence is not None:
            if marker == fence:
                fence = None
            continue
        if stripped.startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept)


def _problems(tail: str, skills: set[str], edits: list[str]) -> list[tuple[str, str]]:
    """(problem id, sentence for the reason). The id is the skill the report claims, or ``tdd`` for a silent edit."""
    report = _report_text(tail)
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _LINE_RE.finditer(report):
        label = match.group(1).lower()
        value = _LEADING_MARKUP_RE.sub("", match.group(2).strip())
        if _NO_CLAIM_RE.match(value):
            continue
        skill = SKILL_OF_LABEL[label]
        if skill in skills or skill in seen:
            continue
        seen.add(skill)
        found.append((skill, f"`{label}:` in the report claims the skill `{skill}`, and this subagent never called "
                             f"it. Call the skill `{skill}` for this unit, or correct the line to "
                             f"`{label}: skipped (why)`."))
    if edits and "tdd" not in skills and not _TDD_PRESENT_RE.search(report):
        found.append(("tdd", f"this unit edits code (`{edits[0]}`) and the report has no `tdd:` line and this "
                             f"subagent never called the skill `tdd`. Call the skill `tdd`, then report it, or end "
                             f"the report with `tdd: skipped (why)`."))
    return found


def _log(payload: dict, problems: list[str]) -> None:
    """Append one JSON line for a block; never raises."""
    try:
        path = Path(os.environ.get("AGENTOPS_SUBAGENT_SKILL_CHECK_LOG") or LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": payload.get("session_id"),
               "agent_id": payload.get("agent_id"), "agent_type": payload.get("agent_type"),
               "problems": problems}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


# the role(s) each rule binds (#320): ("worker",) is the CHECKED_ROLES gate above (worker, worker-strong), which
# run() applies by agent_type before any rule; this declaration does not change that gate
RULE_ROLES: dict[str, tuple[str, ...]] = {"skill_check": ("worker",)}


def run(payload: dict) -> dict | None:
    if payload.get("stop_hook_active"):
        return None
    role = payload.get("agent_type")
    if not isinstance(role, str) or role not in CHECKED_ROLES:
        return None
    path = payload.get("agent_transcript_path")
    if not isinstance(path, str) or not path:
        return None
    try:
        records = _read_records(path)
    except (OSError, ValueError):
        return None
    tail = _final_text(payload, records)[-TAIL_CHARS:]
    found = _problems(tail, _skill_names(records), _code_edits(records))
    if not found:
        return None
    _log(payload, [skill for skill, _ in found])
    reason = "\n".join([REASON_HEADER] + [f"- {sentence}" for _, sentence in found])
    return {"decision": "block", "reason": reason}


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
