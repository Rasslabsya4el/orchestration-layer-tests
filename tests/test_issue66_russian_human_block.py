"""#66: the five fields under `## For the human` are written in Russian; the guard accepts that and still refuses a
ticket with no human block."""
import re
from pathlib import Path

from tools.agentops.ticket_shape import check

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".claude" / "skills" / "write-ticket" / "SKILL.md"
TEMPLATE = ROOT / "config" / "core_templates" / "new_target_ticket.md"
CYR = re.compile(r"[а-яА-ЯёЁ]")

RU_BODY = """## For the human
- **What:** сумма доставки показывается на странице корзины.
- **Goal:** покупатели доводят заказ до конца, потому что итог не удивляет их; слово владельца магазина.
- **Plan:** корзина спрашивает тот же расчёт, что и страница оплаты.
- **Result:** брошенных корзин на оплате меньше 30 %; показывает тест на трёх индексах.
- **Undo:** two-way door (one revert); touches каждый заказ через корзину.

## For the agent
spec
Class: none (test fixture)
"""


def _fields(text: str) -> dict[str, str]:
    block = text.split("## For the human")[1].split("## For the agent")[0]
    return {m.group(1): m.group(2) for m in re.finditer(r"^- \*\*(\w+):\*\*[ \t]*(.*)$", block, re.M)}


def test_row1_skill_says_russian_and_its_example_fields_are_russian():
    text = SKILL.read_text(encoding="utf-8")
    assert "Plain English" not in text
    assert "in Russian" in text
    example = text.split("## Example")[1]
    fields = _fields(example)
    assert list(fields) == ["What", "Goal", "Plan", "Result", "Undo"]
    assert all(CYR.search(v) for v in fields.values())


def test_row2_guard_accepts_a_russian_human_block():
    assert check("Checkout shows the shipping cost before payment", RU_BODY) == []


def test_row2_guard_still_refuses_a_ticket_without_a_human_block():
    assert check("A title", "## For the agent\nspec\n") != []


def test_template_human_block_is_russian_with_english_field_names():
    fields = _fields(TEMPLATE.read_text(encoding="utf-8"))
    assert list(fields) == ["What", "Goal", "Plan", "Result", "Undo"]
    assert all(CYR.search(v) for v in fields.values())
