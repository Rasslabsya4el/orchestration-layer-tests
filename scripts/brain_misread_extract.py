"""Extract every owner message that reads as a misunderstanding in the brain chats' transcripts, with the
brain reply right before it.

A brain chat is a top-level transcript whose first custom-title record starts with "Brain" or
"Оркестровый слой". The owner messages are non-meta, non-sidechain user records with text content (a
string, or a list of text blocks without a tool_result); harness wrappers (task-notification,
system-reminder, local-command, command-name, cross-session-message) are not owner messages. A marker
(case-insensitive substring) makes an owner message a row; the reply is the brain's text blocks of the
assistant records between the owner message before it and it.

    python scripts/brain_misread_extract.py --since 2026-10-01 --out docs/research/2026-10-15-brain-misread-rows.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.agentops.core_format import human_duration  # noqa: E402

DEFAULT_FOLDERS = [
    Path.home() / ".claude" / "projects" / "C--Coding-orchestration-layer",
]
DEFAULT_OUT = ROOT / "docs" / "research" / "2026-10-15-brain-misread-rows.jsonl"

MARKERS = ["не понимаю", "не понял", "что значит", "о чем речь", "о чём речь", "wait", "объясни", "проще"]
BRAIN_TITLE_PREFIXES = ("Brain", "Оркестровый слой")
HARNESS_PREFIXES = ("<task-notification", "<system-reminder", "<local-command", "<command-", "<cross-session-message")
# Whole words only: "waiting" and "объяснить" are not the markers.
MARKER_PATTERNS = [(m, re.compile(r"(?<!\w)" + re.escape(m) + r"(?!\w)", re.IGNORECASE)) for m in MARKERS]
LONG_BRIEF = 1500  # an owner message over this is a brief: its marker counts only inside its first LONG_BRIEF_HEAD
LONG_BRIEF_HEAD = 300
SLASH_COMMAND = re.compile(r"/[\w:\-]+(\s|$)")
ATTACH_LINE = "<!-- attach"
REPLY_FULL_MAX = 1600  # a reply up to this many chars is kept whole
REPLY_HEAD_TAIL = 800


def owner_text(rec: dict) -> str | None:
    """The owner's text of a user record, or None when the record is not an owner message."""
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        if content.lstrip().startswith(HARNESS_PREFIXES):
            return None
        return content
    if isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict)]
        types = [b.get("type") for b in blocks]
        if "tool_result" in types or "text" not in types:
            return None
        return "\n".join(b.get("text") or "" for b in blocks if b.get("type") == "text")
    return None


def assistant_text(rec: dict) -> str | None:
    """The brain's text blocks of one assistant record (tool_use blocks skipped), or None if it has none."""
    if rec.get("type") != "assistant" or rec.get("isSidechain"):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content or None
    if not isinstance(content, list):
        return None
    parts = [b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text"]
    joined = "\n".join(p for p in parts if p)
    return joined or None


def own_text(text: str) -> str:
    """The owner's own typed text: the attach quotes (a "<!-- attach -->" line and the "> " lines that follow it)
    are the brain's words the owner pasted in, so they are dropped."""
    kept = [
        line
        for line in text.split("\n")
        if not line.lstrip().startswith(ATTACH_LINE) and not line.startswith(">")
    ]
    return "\n".join(kept).strip()


def find_marker(text: str) -> str | None:
    """The first marker of MARKERS (list order) that is a whole word in the owner's own text. A slash command is
    never a marker; in a long brief only a marker starting inside its first LONG_BRIEF_HEAD chars counts."""
    if not text or SLASH_COMMAND.match(text.lstrip()):
        return None
    long_brief = len(text) > LONG_BRIEF
    for marker, pattern in MARKER_PATTERNS:
        for match in pattern.finditer(text):
            if long_brief and match.start() >= LONG_BRIEF_HEAD:
                continue
            return marker
    return None


def _iter_records(path: Path):
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _brain_title(path: Path) -> str | None:
    """The first custom-title of a transcript; the stream stops there."""
    for rec in _iter_records(path):
        if rec.get("type") == "custom-title":
            return rec.get("customTitle") or ""
    return None


def _is_brain(title: str | None) -> bool:
    return title is not None and title.startswith(BRAIN_TITLE_PREFIXES)


def _rows_of_chat(path: Path, title: str, since: str) -> list[dict]:
    """Walk one brain chat: events are owner messages and brain text records, in file order."""
    events: list[tuple[str, str, str, str]] = []  # (kind, text, timestamp, raw owner message)
    seen_title = False
    for rec in _iter_records(path):
        if not seen_title:
            if rec.get("type") == "custom-title":
                seen_title = True
            continue
        ts = rec.get("timestamp") or ""
        raw = owner_text(rec)
        if raw is not None:
            events.append(("owner", own_text(raw), ts, raw))
            continue
        atext = assistant_text(rec)
        if atext is not None:
            events.append(("asst", atext, ts, ""))

    rows: list[dict] = []
    for i, (kind, text, ts, raw) in enumerate(events):
        if kind != "owner":
            continue
        marker = find_marker(text)
        if marker is None:
            continue
        date = ts[:10]
        if date < since:
            continue
        j = i - 1
        reply_parts: list[str] = []
        while j >= 0 and events[j][0] == "asst":
            reply_parts.insert(0, events[j][1])
            j -= 1
        k = j
        while k >= 0 and events[k][0] != "owner":
            k -= 1
        prev_owner = (events[k][1] or events[k][3]) if k >= 0 else None
        reply_text = "\n".join(reply_parts)
        if len(reply_text) > REPLY_FULL_MAX:
            shown = reply_text[:REPLY_HEAD_TAIL] + " [...] " + reply_text[-REPLY_HEAD_TAIL:]
        else:
            shown = reply_text
        rows.append(
            {
                "date": date,
                "chat_title": title,
                "session_id": path.stem,
                "marker": marker,
                "owner_text": text[:300],
                "reply_words": len(reply_text.split()),
                "reply_turns": len(reply_parts),
                "reply_text": shown,
                "prev_owner_text": prev_owner[:200] if prev_owner is not None else None,
            }
        )
    return rows


def extract_rows(folder: Path, since: str) -> tuple[list[dict], dict]:
    """Scan the top-level transcripts of one folder: rows of every brain chat, plus the counts."""
    cutoff = datetime.strptime(since, "%Y-%m-%d").timestamp()
    listed = sorted(folder.glob("*.jsonl"))
    recent = [p for p in listed if p.stat().st_mtime >= cutoff]
    rows: list[dict] = []
    brain_chats: list[tuple[str, str]] = []
    scanned = 0
    for path in recent:
        scanned += 1
        title = _brain_title(path)
        if not _is_brain(title):
            continue
        brain_chats.append((title, path.stem))
        rows.extend(_rows_of_chat(path, title, since))
    per_marker = Counter(r["marker"] for r in rows)
    stats = {
        "listed": len(listed),
        "after_mtime": len(recent),
        "scanned": scanned,
        "brain_chats": brain_chats,
        "rows": len(rows),
        "per_marker": per_marker,
    }
    return rows, stats


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--since", default="2026-10-01", help="YYYY-MM-DD: files modified and messages dated from here")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="rows file (jsonl)")
    ap.add_argument("--folder", type=Path, action="append", help="transcript folder (repeatable; default: this repo's)")
    args = ap.parse_args(argv)

    started = time.monotonic()
    folders = args.folder or DEFAULT_FOLDERS
    all_rows: list[dict] = []
    totals = {"listed": 0, "after_mtime": 0, "scanned": 0, "rows": 0}
    brain_chats: list[tuple[str, str]] = []
    per_marker: Counter = Counter()
    for folder in folders:
        rows, stats = extract_rows(folder, args.since)
        all_rows.extend(rows)
        for key in totals:
            totals[key] += stats[key]
        brain_chats.extend(stats["brain_chats"])
        per_marker.update(stats["per_marker"])

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        for row in all_rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    chats = ", ".join(f"{title!r} {stem}" for title, stem in brain_chats) or "none"
    markers = ", ".join(f"{m} {per_marker.get(m, 0)}" for m in MARKERS)
    elapsed = human_duration(time.monotonic() - started)
    print(
        f"files scanned {totals['scanned']} (top-level jsonl {totals['listed']}, modified since {args.since} "
        f"{totals['after_mtime']}); brain chats found {len(brain_chats)}: {chats}; rows written {len(all_rows)} "
        f"to {args.out}; rows per marker: {markers}; wall time {elapsed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
