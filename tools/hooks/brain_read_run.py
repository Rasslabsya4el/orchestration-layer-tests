"""The brain's trailing run of read turns (issue #131), as pure functions over transcript records.

``classify`` says what one tool call is for this count: ``"read"`` (it only looks at the repo or
the trackers), ``"neutral"`` (an orchestration status command: neither counts nor resets) or
``"other"`` (anything that writes or delegates: it resets the run).

``count_run`` walks a transcript's JSON records and returns how many read calls end the
transcript, the run a nudge hook counts. It never raises on odd records: anything it does not
recognise is skipped.

Stdlib only; imports nothing from the rest of ``tools``.
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple

READ = "read"
NEUTRAL = "neutral"
OTHER = "other"

_READ_TOOLS = frozenset({"Read", "Grep", "Glob"})

# A command segment is read-only when its leading words are one of these. ``gh issue list`` and
# ``gh pr list`` are counted reads; the status scripts of a repo are neutral (see ``_is_neutral``),
# named by the settings, not by this module.
_READ_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("cat",),
    ("head",),
    ("tail",),
    ("sed", "-n"),
    ("grep",),
    ("rg",),
    ("ls",),
    ("find",),
    ("git", "log"),
    ("git", "show"),
    ("git", "diff"),
    ("gh", "issue", "view"),
    ("gh", "issue", "list"),
    ("gh", "pr", "view"),
    ("gh", "pr", "list"),
    ("gh", "api"),
)

_FIND_WRITERS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir"})

# The operators that end a segment; searched in the quote-masked command, so never inside quotes.
_OPERATOR = re.compile(r"\|\||&&|\||;")

# Redirections that read nothing: blanked out before a segment is judged.
_HARMLESS_REDIRECT = re.compile(
    r"&>\s*/dev/null|\d?>\s*/dev/null|\d?>&\d|\d?>\s*nul\b", re.IGNORECASE)


class _Segment(NamedTuple):
    tokens: list[str]
    redirect: bool  # an unquoted ``>`` or ``>>`` left after the harmless ones are blanked


class _Event(NamedTuple):
    """A tool call (its id and verdict), or a real prompt: no id and verdict ``other``, which ends a run."""
    call_id: str | None
    verdict: str


def _mask_quotes(command: str) -> str:
    """``command`` with every character inside quotes, quotes included, replaced by ``_``.

    Operators and ``>`` are searched in the masked text, so a ``|`` or ``>`` inside quotes is never
    one. An unclosed quote masks the rest of the line.
    """
    out: list[str] = []
    quote = ""
    for ch in command:
        if quote:
            out.append("_")
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
            out.append("_")
        else:
            out.append(ch)
    return "".join(out)


def _segments(command: str) -> list[_Segment]:
    """The command's segments as tokens, a leading ``cd <path> &&`` segment dropped."""
    masked = _mask_quotes(command)
    text = command
    for hit in list(_HARMLESS_REDIRECT.finditer(masked)):
        blank = " " * (hit.end() - hit.start())
        text = text[:hit.start()] + blank + text[hit.end():]
        masked = masked[:hit.start()] + blank + masked[hit.end():]
    spans: list[tuple[int, int]] = []
    start = 0
    for op in _OPERATOR.finditer(masked):
        spans.append((start, op.start()))
        start = op.end()
    spans.append((start, len(masked)))
    segments = []
    for start, end in spans:
        tokens = text[start:end].split()
        if tokens:
            segments.append(_Segment(tokens, ">" in masked[start:end]))
    if len(segments) > 1 and segments[0].tokens[0] == "cd":
        segments = segments[1:]
    return segments


def _gh_api_writes(tokens: list[str]) -> bool:
    """True when a ``gh api`` segment sends anything but a GET."""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-X", "--method"):
            if i + 1 >= len(tokens) or tokens[i + 1].upper() != "GET":
                return True
            i += 2
            continue
        if tok.startswith("-X") and len(tok) > 2:
            if tok[2:].upper() != "GET":
                return True
        if tok.startswith("--method="):
            if tok.split("=", 1)[1].upper() != "GET":
                return True
        if tok[:2] in ("-f", "-F") or tok.startswith(("--field", "--raw-field", "--input")):
            return True
        i += 1
    return False


def _is_read_segment(tokens: list[str]) -> bool:
    for prefix in _READ_PREFIXES:
        if tuple(tokens[: len(prefix)]) == prefix:
            if prefix == ("find",) and _FIND_WRITERS.intersection(tokens):
                return False
            if prefix == ("gh", "api") and _gh_api_writes(tokens):
                return False
            return True
    return False


def _is_neutral(tokens: list[str], neutral_scripts: tuple[str, ...] = ()) -> bool:
    """``python[3] -m tools.agentops ...`` or ``python[3] <path> ...`` where ``<path>`` ends with one of
    ``neutral_scripts`` (path suffixes from the settings, e.g. ``scripts/<name>.py``)."""
    if len(tokens) < 2 or tokens[0] not in ("python", "python3"):
        return False
    if tokens[1] == "-m":
        return len(tokens) >= 3 and tokens[2] == "tools.agentops"
    path = tokens[1].replace("\\", "/")
    return any(suffix and path.endswith(suffix.replace("\\", "/")) for suffix in neutral_scripts)


def _segment_verdict(segment: _Segment, neutral_scripts: tuple[str, ...] = ()) -> str:
    if segment.redirect:
        return OTHER
    if _is_neutral(segment.tokens, neutral_scripts):
        return NEUTRAL
    return READ if _is_read_segment(segment.tokens) else OTHER


def _classify_bash(command: str, neutral_scripts: tuple[str, ...] = ()) -> str:
    """Over all segments: any ``other`` makes the command ``other``; else any ``read`` makes it ``read``."""
    verdicts = {_segment_verdict(segment, neutral_scripts) for segment in _segments(command)}
    if not verdicts or OTHER in verdicts:
        return OTHER
    return READ if READ in verdicts else NEUTRAL


def classify(tool_name: str, tool_input: dict, neutral_scripts: tuple[str, ...] = ()) -> str:
    """``"read"``, ``"neutral"`` or ``"other"`` for one tool call (see the module docstring).

    ``neutral_scripts``: path suffixes of the repo's own status scripts, read from the settings
    (``read_run_neutral_scripts``); a ``python[3] <path>`` call whose path ends with one is neutral.
    With the default ``()`` only ``python -m tools.agentops`` is neutral.
    """
    if tool_name in _READ_TOOLS:
        return READ
    if tool_name != "Bash":
        return OTHER
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return OTHER
    return _classify_bash(command, tuple(neutral_scripts))


def _content(record: dict) -> Any:
    """The record's ``message.content``, or None when it has none."""
    message = record.get("message")
    return message.get("content") if isinstance(message, dict) else None


def _is_real_prompt(record: dict) -> bool:
    """A user record that a person typed: a plain string or a text block, never a tool result."""
    content = _content(record)
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return False
    return any(isinstance(b, dict) and b.get("type") == "text" for b in content)


def _events(records: list[Any], neutral_scripts: tuple[str, ...] = ()) -> list[_Event]:
    """Tool calls and real prompts, in order."""
    events: list[_Event] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("type") == "user":
            if _is_real_prompt(record):
                events.append(_Event(None, OTHER))
            continue
        content = _content(record)
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                verdict = classify(block.get("name", ""), block.get("input") or {}, neutral_scripts)
                events.append(_Event(block.get("id"), verdict))
    return events


def count_run(records: list[dict], current_id: str | None,
              neutral_scripts: tuple[str, ...] = ()) -> int:
    """Reads in the trailing run of read calls, not counting the call ``current_id``.

    Only calls before ``current_id`` are considered when it is among the calls (so parallel calls
    in one message count exactly); otherwise every call is. Neutral calls are skipped; a read adds
    one; the first other call or real prompt, nearest the end, ends the run. ``neutral_scripts`` is
    passed to ``classify``.
    """
    events = _events(records, tuple(neutral_scripts))
    cut = len(events)
    if current_id is not None:
        for index, event in enumerate(events):
            if event.call_id == current_id:
                cut = index
                break
    count = 0
    for event in reversed(events[:cut]):
        if event.verdict == NEUTRAL:
            continue
        if event.verdict == READ:
            count += 1
            continue
        break
    return count
