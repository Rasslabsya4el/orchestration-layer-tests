"""#31 unit 2: the brain's protocol, the porting doc and the standard ticket name `target add`; rows 6 and 7."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAIN_REFERENCE = ROOT / "docs" / "agents" / "brain-reference.md"
PORTING = ROOT / "docs" / "agents" / "PORTING.md"
TEMPLATE = ROOT / "config" / "core_templates" / "new_target_ticket.md"
MANIFEST = ROOT / "tools" / "agentops" / "core_manifest.txt"
SECTION = "## A new target in one command"
COMMAND = re.compile(r"\btarget add\b")


def _section_lines(text: str) -> list[str]:
    """The lines of the porting doc's new section: from its heading up to the next `## ` heading."""
    lines = text.splitlines()
    start = lines.index(SECTION)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return lines[start:end]


def test_target_add_is_named_in_the_brain_protocol():
    assert COMMAND.search(BRAIN_REFERENCE.read_text(encoding="utf-8"))


def test_target_add_is_named_in_the_porting_doc():
    assert COMMAND.search(PORTING.read_text(encoding="utf-8"))


def test_target_add_is_named_in_the_template():
    assert COMMAND.search(TEMPLATE.read_text(encoding="utf-8"))


def test_the_porting_section_has_at_least_four_lesson_bullets():
    bullets = [line for line in _section_lines(PORTING.read_text(encoding="utf-8")) if line.startswith("- ")]
    assert len(bullets) >= 4, bullets


def test_the_template_is_a_manifest_file_and_exists():
    assert TEMPLATE.is_file()
    manifest = [line.strip() for line in MANIFEST.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")]
    assert "config/core_templates/new_target_ticket.md" in manifest


def test_the_template_formats_with_folder_and_title_without_key_error():
    text = TEMPLATE.read_text(encoding="utf-8")
    rendered = text.format(folder="C:/Coding/Example repo", title="пример")
    assert "C:/Coding/Example repo" in rendered
    assert "пример" in rendered
    assert "{folder}" not in rendered and "{title}" not in rendered
