"""#155 unit 3: the brain's sweep rule opens the owner's report with the monitor's groups, as было / стало / какие тикеты ушли и куда."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAIN = ROOT / "docs/agents/brain.md"
COLUMNS = ("было", "стало", "какие тикеты ушли и куда")


def _text() -> str:
    return BRAIN.read_text(encoding="utf-8")


def _sweep_paragraph() -> str:
    text = _text()
    start = text.index("- **Sweeps the stuck tickets")
    end = text.index("\n- **", start + 1)
    return text[start:end]


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
