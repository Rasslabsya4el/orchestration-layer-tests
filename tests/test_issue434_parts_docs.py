"""#434: a finished ticket part reaches main on its own merge; no doc says a ticket reaches main by one merge."""
import re
from pathlib import Path

from tools.agentops.monitor_agents import cut_table_total

ROOT = Path(__file__).resolve().parents[1]
ORCH_FILES = [ROOT / "docs/agents/orch.md", ROOT / ".claude/agents/orch.md"]


def one_line(path):
    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))


def test_no_agent_doc_says_an_issue_reaches_main_by_one_merge():
    paths = [*(ROOT / "docs/agents").glob("*.md"), *(ROOT / ".claude/agents").glob("*.md")]
    hits = [str(p.relative_to(ROOT)) for p in paths
            if re.search(r"(issue|ticket) (goes into|reaches) `?main`? by one merge", one_line(p))]
    assert hits == []


def test_the_unit_cut_table_ends_with_a_part_column_in_both_orch_files():
    for p in ORCH_FILES:
        assert re.search(r"`needs`[^`]*(\([^)]*\))?, `part`", one_line(p)), p


def test_the_monitor_still_reads_a_cut_table_that_has_the_part_column():
    comment = ("| unit | outcome | files it edits | its test | needs | part |\n|---|---|---|---|---|---|\n"
               "| 1 of 3 (a) | x | f | t | none | 1 |\n| 2 of 3 (b) | y | g | u | none | 1 |\n| 3 of 3 (c) | z | h | v | 1 | 2 |\n")
    assert cut_table_total([comment]) == 3


def test_orch_md_lands_each_ticket_part_on_its_own_merge_and_keeps_the_issue_assigned():
    text = one_line(ROOT / "docs/agents/orch.md")
    assert "each ticket part goes into `main` by one merge, the suite run once per part" in text
    assert "stays open and assigned" in text
    assert "Only the last ticket part's merge parks the issue" in text


def test_the_glossary_names_the_ticket_part_apart_from_the_suite_part():
    text = (ROOT / "GLOSSARY.md").read_text(encoding="utf-8")
    assert "**Ticket part**:" in text and "**Part**:" in text
