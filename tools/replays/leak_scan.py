"""Leak scan of Claude Code session transcripts (issue #349).

A transcript is a .jsonl file, one JSON record per line. Only what a tool
returned is scanned: a record of type ``user`` whose ``message.content`` holds
``tool_result`` blocks. Assistant text and the owner's plain user message are
not scanned. A run's helpers are transcripts of their own, in the ``subagents``
folder next to the session file. A line that cannot be read or parsed is
reported in ``skipped_lines``, never passed silently.
"""

import json
from pathlib import Path
from typing import NamedTuple


class ScanResult(NamedTuple):
    """hits: one dict per (marker, tool_result block) that contains the marker.

    skipped_lines: the 0-based line numbers scan_transcript could not read or
    parse; scan_run gives ``(file, line)`` pairs instead, since a run spans files.
    """

    hits: list
    skipped_lines: list


def _tool_result_text(block: dict) -> str:
    """Return the text a tool_result block carries.

    Content is a string, or a list whose items are plain strings and blocks of
    type ``text``; any other kind of block is ignored.
    """
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def scan_transcript(path: Path, markers: list[str]) -> ScanResult:
    """Scan one transcript for the markers in what its tools returned.

    A hit is ``{"marker": str, "line": int, "tool_use_id": str | None}``, where
    ``line`` is the 0-based physical line number in the file. Matching is a
    case-insensitive substring test (casefold on both sides).
    """
    hits: list[dict] = []
    skipped: list[int] = []
    needles = [(marker, marker.casefold()) for marker in markers]
    with Path(path).open("rb") as fh:
        for line_no, raw in enumerate(fh):
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                skipped.append(line_no)
                continue
            if not isinstance(record, dict) or record.get("type") != "user":
                continue
            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                folded = _tool_result_text(block).casefold()
                for marker, needle in needles:
                    if needle in folded:
                        hits.append(
                            {
                                "marker": marker,
                                "line": line_no,
                                "tool_use_id": block.get("tool_use_id"),
                            }
                        )
    return ScanResult(hits, skipped)


def transcript_files(session_jsonl: Path) -> list[Path]:
    """The session file, then every ``*.jsonl`` in its sibling ``<id>/subagents`` folder, sorted."""
    session_jsonl = Path(session_jsonl)
    subagents = session_jsonl.with_suffix("") / "subagents"
    files = [session_jsonl]
    if subagents.is_dir():
        files.extend(sorted(subagents.glob("*.jsonl")))
    return files


def scan_run(session_jsonl: Path, markers: list[str]) -> ScanResult:
    """Scan a run's session transcript and its subagent transcripts together.

    Each hit gains a ``"file"`` key with the transcript it came from; each
    skipped line is a ``(file, line)`` pair.
    """
    hits: list[dict] = []
    skipped: list[tuple[Path, int]] = []
    for file in transcript_files(session_jsonl):
        part = scan_transcript(file, markers)
        hits.extend({**hit, "file": file} for hit in part.hits)
        skipped.extend((file, line) for line in part.skipped_lines)
    return ScanResult(hits, skipped)


def is_void(result: ScanResult) -> bool:
    """A run is void when any leak hit was found."""
    return len(result.hits) > 0
