"""Tests for scripts/brain_misread_extract.py: fake transcript folders in tmp_path, no game client, no network."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import brain_misread_extract as bme  # noqa: E402

SINCE = "2026-10-01"
TS = "2026-10-13T08:57:52.907Z"


def _write(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")


def _title(title: str, session: str) -> dict:
    return {"type": "custom-title", "customTitle": title, "sessionId": session}


def _owner(text: str, **flags) -> dict:
    rec = {"type": "user", "message": {"role": "user", "content": text}, "timestamp": TS}
    rec.update(flags)
    return rec


def _assistant(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}, "timestamp": TS}


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def test_brain_chat_marker_message_gets_one_row_with_reply_attached(tmp_path: Path) -> None:
    _write(
        tmp_path / "s1.jsonl",
        [
            _title("Brain: demo", "s1"),
            _owner("вопрос про прогоны"),
            _assistant(_text("one two three"), {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}),
            _assistant(_text("four five")),
            _owner("Я не понимаю что значит cap"),
        ],
    )
    rows, stats = bme.extract_rows(tmp_path, SINCE)
    assert len(rows) == 1
    row = rows[0]
    assert row["chat_title"] == "Brain: demo"
    assert row["session_id"] == "s1"
    assert row["date"] == "2026-10-13"
    assert row["marker"] == "не понимаю"
    assert row["owner_text"] == "Я не понимаю что значит cap"
    assert row["reply_words"] == 5
    assert row["reply_text"] == "one two three\nfour five"
    assert row["prev_owner_text"] == "вопрос про прогоны"
    assert stats["rows"] == 1


def test_non_brain_chat_gives_no_rows(tmp_path: Path) -> None:
    _write(
        tmp_path / "s2.jsonl",
        [
            _title("Some other chat", "s2"),
            _owner("вопрос"),
            _assistant(_text("ответ")),
            _owner("я не понял"),
        ],
    )
    rows, _ = bme.extract_rows(tmp_path, SINCE)
    assert rows == []


def test_sidechain_and_meta_messages_with_marker_give_no_rows(tmp_path: Path) -> None:
    _write(
        tmp_path / "s3.jsonl",
        [
            _title("Brain: demo", "s3"),
            _owner("вопрос"),
            _assistant(_text("ответ")),
            _owner("что значит это", isSidechain=True),
            _owner("объясни пожалуйста", isMeta=True),
        ],
    )
    rows, _ = bme.extract_rows(tmp_path, SINCE)
    assert rows == []


def test_tool_result_list_content_with_marker_gives_no_rows(tmp_path: Path) -> None:
    _write(
        tmp_path / "s4.jsonl",
        [
            _title("Brain: demo", "s4"),
            _owner("вопрос"),
            _assistant(_text("ответ")),
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "я не понял"}],
                },
                "timestamp": TS,
            },
        ],
    )
    rows, _ = bme.extract_rows(tmp_path, SINCE)
    assert rows == []


def _rows_for(tmp_path: Path, owner_msg: str, reply: str = "ответ") -> list[dict]:
    """One brain chat: owner asks, brain answers, owner writes owner_msg. Returns the extracted rows."""
    _write(
        tmp_path / "chat.jsonl",
        [
            _title("Brain: demo", "chat"),
            _owner("вопрос"),
            _assistant(_text(reply)),
            _owner(owner_msg),
        ],
    )
    rows, _ = bme.extract_rows(tmp_path, SINCE)
    return rows


ATTACH_QUOTE = "<!-- attach -->\n> Я не понимаю, что значит это\n> \n> Второй абзац ответа\n\n"


def test_marker_only_inside_attach_quote_gives_no_row(tmp_path: Path) -> None:
    assert _rows_for(tmp_path, ATTACH_QUOTE + "Ок, спасибо") == []


def test_marker_in_own_text_after_attach_quote_gives_row_with_own_text_only(tmp_path: Path) -> None:
    msg = ATTACH_QUOTE + "1. Мне нужен хендоф\n2. Я не понял по итогу ты"
    rows = _rows_for(tmp_path, msg)
    assert len(rows) == 1
    assert rows[0]["marker"] == "не понял"
    assert "<!-- attach" not in rows[0]["owner_text"]
    assert "Второй абзац" not in rows[0]["owner_text"]
    assert rows[0]["owner_text"].startswith("1. Мне нужен хендоф")


@pytest.mark.parametrize(
    ("owner_msg", "expected_marker"),
    [
        ("I am waiting for the run", None),
        ("Wait, why is it red?", "wait"),
        ("Объяснить как это работает?", None),
        ("Объясни как это работает", "объясни"),
        ("Проще можно сказать?", "проще"),
        ("Я не понялa почему", None),
    ],
)
def test_markers_match_as_whole_words(tmp_path: Path, owner_msg: str, expected_marker: str | None) -> None:
    rows = _rows_for(tmp_path, owner_msg)
    if expected_marker is None:
        assert rows == []
    else:
        assert len(rows) == 1 and rows[0]["marker"] == expected_marker


def test_slash_command_with_marker_word_gives_no_row(tmp_path: Path) -> None:
    assert _rows_for(tmp_path, "/mattpocock-skills:wait") == []


def test_long_brief_with_marker_in_middle_gives_no_row(tmp_path: Path) -> None:
    brief = "x" * 1000 + " объясни " + "y" * 1000
    assert len(brief) > 1500
    assert _rows_for(tmp_path, brief) == []


def test_long_brief_with_marker_in_first_300_chars_gives_row(tmp_path: Path) -> None:
    brief = "объясни " + "y" * 2000
    rows = _rows_for(tmp_path, brief)
    assert len(rows) == 1 and rows[0]["marker"] == "объясни"


def test_long_reply_keeps_head_and_tail_and_counts_turns(tmp_path: Path) -> None:
    part_a = "начало " * 200  # 1400 chars
    part_b = "конец " * 200  # 1200 chars
    _write(
        tmp_path / "chat.jsonl",
        [
            _title("Brain: demo", "chat"),
            _owner("вопрос"),
            _assistant(_text(part_a)),
            _assistant(_text(part_b)),
            _owner("Я не понимаю"),
        ],
    )
    rows, _ = bme.extract_rows(tmp_path, SINCE)
    joined = part_a + "\n" + part_b
    assert len(joined) > 1600
    row = rows[0]
    assert row["reply_text"] == joined[:800] + " [...] " + joined[-800:]
    assert row["reply_words"] == 400
    assert row["reply_turns"] == 2


def test_short_reply_kept_whole_with_one_turn(tmp_path: Path) -> None:
    rows = _rows_for(tmp_path, "Я не понимаю", reply="one two three")
    assert rows[0]["reply_text"] == "one two three"
    assert rows[0]["reply_words"] == 3
    assert rows[0]["reply_turns"] == 1
