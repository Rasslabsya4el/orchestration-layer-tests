"""Issue #98: the state-claim Stop hook (`tools/hooks/state_claim_check.py`, #81) matches only the
reply's prose. Fenced code, inline code and quoted lines are dropped first; a Russian stem matches
a whole word that starts with it, except a noun form ("закрытие", "собрание"); an English stem
matches a whole word; a claim word
right after "не" / "ни", inside quotes, or in a paragraph with a disclaimer does not block.

Seam: the hook run as a subprocess the way Claude Code runs it (stdin payload, stdout decision).
Transcript records have the real shape of test_issue81_state_claim_hook.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOOK_SCRIPT = PROJECT_ROOT / "tools" / "hooks" / "state_claim_check.py"
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "issue98_state_claim_replies.json"
LOG_NAME = "state_claim.log"
SESSION = "98-session"


@pytest.fixture(autouse=True)
def _log_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOPS_STATE_CLAIM_LOG", str(tmp_path / LOG_NAME))


def _owner(text: str = "проверь ПК") -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _text(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": text}]}}


def _run(tmp_path: Path, text: str) -> subprocess.CompletedProcess:
    """One owner prompt, one assistant text record with no tool in the turn, then the hook."""
    transcript = tmp_path / "transcript.jsonl"
    records = [_owner(), _text(text)]
    transcript.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                          encoding="utf-8")
    payload = {"hook_event_name": "Stop", "session_id": SESSION, "stop_hook_active": False,
               "transcript_path": str(transcript)}
    return subprocess.run([sys.executable, str(HOOK_SCRIPT)], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=20)


def _blocked(tmp_path: Path, text: str) -> bool:
    proc = _run(tmp_path, text)
    assert proc.returncode == 0, proc.stderr
    return bool(proc.stdout.strip())


# -- row 1: the 17 replies the log holds (session 726a7a65) block at most once ----------------------

def test_row1_replay_of_the_17_night_replies_blocks_at_most_once(tmp_path):
    replies = json.loads(FIXTURE.read_text(encoding="utf-8"))["replies"]
    assert len(replies) == 17
    blocked = [r["log_ts"] for r in replies if _blocked(tmp_path, r["text"])]
    assert len(blocked) <= 1, f"blocked {len(blocked)} of 17: {blocked}"


# -- row 2: a bare claim in prose, no tool in the turn, still blocks ------------------------------

@pytest.mark.parametrize("text, word", [
    # the short state forms of the stem "закрыт"
    ("#18 закрыт.", "закрыт"),
    ("#18 закрыта, сверил по дате.", "закрыта"),
    ("Всё закрыто.", "закрыто"),
    ("Оба закрыты.", "закрыты"),
    # one form per Russian stem class that blocked before #98: adjective, participle, verb
    ("Все задачи закрытые.", "закрытые"),
    ("Стенд собранный.", "собранный"),
    ("Пересобранный стенд.", "пересобранный"),
    ("ПК свободен.", "свободен"),
    ("Бот запущенный.", "запущенный"),
    ("Файлы перенесли.", "перенесли"),
    ("Заметку перенёсла Ира.", "перенёсла"),
    ("Готово.", "готово"),
    ("Сделано.", "сделано"),
    ("Сборка работает.", "работает"),
    ("PC можно пользоваться", "можно пользоваться"),
    ("Пересобран в 03:32.", "пересобран"),
    # the English stems, each a whole word
    ("Теперь #18 done.", "done"),
    ("Сборка built.", "built"),
    ("ПК free.", "free"),
    ("Файл moved.", "moved"),
    ("Бот running.", "running"),
    ("Ветка landed.", "landed"),
])
def test_row2_a_bare_claim_in_prose_blocks(tmp_path, text, word):
    assert _blocked(tmp_path, text)
    rows = [json.loads(line) for line in (tmp_path / LOG_NAME).read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["word"] == word


# -- row 3: the cases that must not block ----------------------------------------------------------

@pytest.mark.parametrize("text", [
    # noun forms of the Russian stem are not a state
    "Ждём до его закрытия.",
    "Ждём до закрытия и после закрытия.",
    "Ждём собрание и перенесение задачи.",
    # a copy block: the claim word is inside a fenced code block
    "Текст для копирования:\n\n```text\n#18 закрыт вчера\n```\n",
    "Текст для копирования:\n\n```text\n#18 закрыт вчера, до его закрытия.\n```",
    # disclaimer in the same paragraph exempts the whole paragraph
    "Закрыты ещё два, сам не проверял.",
    "Сам не проверял: #18 закрыт.",
    "Not done, not checked.",
    # a claim word right after не or ни
    "Задача #18 не закрыт.",
    "Сборка не работает.",
    "Ни закрыт, ни готово.",
    # a claim word inside quotes (« » or " ")
    "В записке стоит «закрыт», цитата.",
    'В записке стоит "закрыт", цитата.',
    # inline code and a quoted line
    "Записка: `#18 закрыт`.",
    "> #18 закрыт вчера.",
    # English whole words only
    "Lost freedom, undone work.",
    "Freedom is a word.",
])
def test_row3_a_non_claim_or_an_exempt_claim_gives_no_output(tmp_path, text):
    assert not _blocked(tmp_path, text), text
    assert not (tmp_path / LOG_NAME).exists()


def test_row3_disclaimer_in_another_paragraph_does_not_exempt(tmp_path):
    assert _blocked(tmp_path, "#18 закрыт.\n\nСам не проверял.")


def test_row3_claim_outside_quotes_in_the_same_reply_still_blocks(tmp_path):
    assert _blocked(tmp_path, "В записке «закрыт», а #18 закрыт на самом деле.")


def test_row3_claim_after_a_negated_word_still_blocks_when_another_claim_stands(tmp_path):
    assert _blocked(tmp_path, "Задача #18 не закрыт, а #19 закрыт.")


@pytest.mark.parametrize("text", [
    # the prose claim before the fence
    "Итог: #18 закрыт.\n```text\n#18 закрыт вчера\n```",
    # the prose claim after the fence, on its own line
    "Копия:\n```text\n#18 закрыт вчера\n```\nИтог: #18 закрыт.",
    # the prose claim glued to the closing fence
    "Копия:\n```text\n#18 закрыт вчера\n```#18 закрыт.",
])
def test_row3_a_real_claim_next_to_a_fenced_block_still_blocks(tmp_path, text):
    assert _blocked(tmp_path, text)
