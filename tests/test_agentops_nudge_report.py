"""nudge-report (issue #311): the per-session row, `never`, the window filter, the medians."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tools.agentops.nudge_report import build_rows, format_report, _parse_bound


def _utc(local_iso: str) -> str:
    return datetime.fromisoformat(local_iso).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _assistant(ts: str, total: int, text: str | None = None) -> dict:
    content = [{"type": "text", "text": text}] if text else [{"type": "tool_use", "id": "x", "name": "Bash"}]
    return {"type": "assistant", "timestamp": _utc(ts),
            "message": {"content": content, "usage": {"input_tokens": 2, "cache_creation_input_tokens": 500,
                                                       "cache_read_input_tokens": total - 502}}}


def _setup(tmp_path: Path, log_rows: list[dict], transcripts: dict[str, list[dict]]) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    (root / "var" / "agentops").mkdir(parents=True)
    (root / "var" / "agentops" / "brain_nudge.log").write_text(
        "\n".join(json.dumps(r) for r in log_rows) + "\n", encoding="utf-8")
    projects = tmp_path / "projects"
    projects.mkdir()
    for sid, records in transcripts.items():
        (projects / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return root, projects


def _nudge(ts: str, sid: str, k: int, event: str = "UserPromptSubmit", action: str = "nudged") -> dict:
    return {"ts": ts, "session": sid, "event": event, "action": action, "context_k": k}


A = "aaaaaaaa-0000-0000-0000-000000000000"
B = "bbbbbbbb-0000-0000-0000-000000000000"
C = "cccccccc-0000-0000-0000-000000000000"


def _fixture(tmp_path: Path):
    log = [
        _nudge("2026-09-28T10:00:00", A, 151),
        _nudge("2026-09-28T10:10:00", A, 160),
        _nudge("2026-09-28T10:05:00", A, 155, event="PostToolUse"),
        _nudge("2026-09-28T10:06:00", A, 0, action="skipped"),
        _nudge("2026-09-28T11:00:00", B, 230),
        _nudge("2026-09-29T09:00:00", C, 170),
    ]
    transcripts = {
        A: [_assistant("2026-09-28T09:00:00", 100_000, "handoff earlier, before any reminder"),
            _assistant("2026-09-28T10:02:00", 152_400, "Пора на КЛИР, сессия переросла порог"),
            _assistant("2026-09-28T10:20:00", 180_499, "later text")],
        B: [_assistant("2026-09-28T11:01:00", 231_000, "no mention here"),
            _assistant("2026-09-28T11:30:00", 240_600)],
        C: [_assistant("2026-09-29T09:01:00", 171_000, "use /clear now")],
    }
    return _setup(tmp_path, log, transcripts)


def test_row_per_session(tmp_path):
    root, projects = _fixture(tmp_path)
    rows = build_rows(root, projects, datetime(2026, 9, 28), datetime(2026, 9, 29))
    assert [r["session"] for r in rows] == ["aaaaaaaa", "bbbbbbbb"]
    a, b = rows
    assert (a["reminders"], a["first_k"], a["names_k"], a["end_k"]) == (2, 151, 152, 180)
    assert (b["reminders"], b["first_k"], b["names_k"], b["end_k"]) == (1, 230, None, 241)


def test_window_filter(tmp_path):
    root, projects = _fixture(tmp_path)
    assert [r["session"] for r in build_rows(root, projects, datetime(2026, 9, 29))] == ["cccccccc"]
    both = build_rows(root, projects, datetime(2026, 9, 28), _parse_bound("2026-09-29", True))
    assert len(both) == 3
    early = build_rows(root, projects, datetime(2026, 9, 28), _parse_bound("2026-09-29T08:00", True))
    assert len(early) == 2


def test_report_never_and_medians(tmp_path):
    root, projects = _fixture(tmp_path)
    rows = build_rows(root, projects, datetime(2026, 9, 28), _parse_bound("2026-09-29", True))
    text = format_report(rows)
    assert "bbbbbbbb  1          230k            never" in text
    assert "median first reminder 170k; median end 180k" in text
    assert "passed 200k before naming the hand-off: 1 of 3" in text
