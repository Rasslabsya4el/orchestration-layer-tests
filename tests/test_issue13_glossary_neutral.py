"""#13 unit 3: the GLOSSARY.md entries this unit rewrote carry no ``PoB`` (Path of Building, Poe's own tool).

Seam: the text of GLOSSARY.md, read as the entry blocks it is made of (one heading line plus the lines up to the
next blank line). Each entry is found by its heading, so a reordered file still checks the same entries."""
from __future__ import annotations

from pathlib import Path

GLOSSARY = Path(__file__).resolve().parents[1] / "GLOSSARY.md"

# Entries rewritten by unit 3 (heading as written in the file).
EDITED_HEADINGS = [
    "**Owner**:",
    "**Cloud session credits**:",
    "**Autopilot**:",
    "**Hard stop**:",
    "**Provisional decision**:",
    "**Local extension**:",
]


def _entries(text: str) -> dict[str, str]:
    """Map each heading line to its entry text (heading line up to the next blank line)."""
    entries: dict[str, str] = {}
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("**") and line.endswith(":") and line not in entries:
            block = [line]
            for nxt in lines[i + 1:]:
                if nxt.strip() == "":
                    break
                block.append(nxt)
            entries[line] = "\n".join(block)
    return entries


def test_edited_entries_contain_no_pob():
    entries = _entries(GLOSSARY.read_text(encoding="utf-8"))
    missing = [h for h in EDITED_HEADINGS if h not in entries]
    assert not missing, f"entries not found by heading: {missing}"
    dirty = [h for h in EDITED_HEADINGS if "PoB" in entries[h]]
    assert not dirty, f"entries still saying PoB: {dirty}"
