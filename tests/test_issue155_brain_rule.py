"""#155 unit 3: the brain's sweep rule opens the owner's report with the monitor's groups, as было / стало / какие тикеты ушли и куда.

#348 unit 3: the rule moved from the bullet "Sweeps the stuck tickets" of docs/agents/brain.md to the section "The sweep" of docs/agents/brain-reference.md.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAIN = ROOT / "docs/agents/brain-reference.md"
COLUMNS = ("было", "стало", "какие тикеты ушли и куда")


def _text() -> str:
    return BRAIN.read_text(encoding="utf-8")


def _sweep_paragraph() -> str:
    lines = _text().splitlines()
    start = lines.index("## The sweep")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("## "))
    return "\n".join(lines[start:end])


def test_sweep_paragraph_names_the_three_report_columns():
    paragraph = _sweep_paragraph()
    for column in COLUMNS:
        assert column in paragraph


def test_sweep_paragraph_names_sweep_list_and_the_monitor_group():
    paragraph = _sweep_paragraph()
    assert "sweep list" in paragraph
    assert "«По тикетам»" in paragraph


def test_sweep_paragraph_opens_the_report_and_names_each_changed_ticket_once():
    paragraph = _sweep_paragraph()
    assert "opens with one table" in paragraph
    assert "named once" in paragraph
    assert "before and after" in paragraph
