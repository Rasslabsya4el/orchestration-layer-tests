"""CODING_STANDARDS.md is the one file the code review's Standards axis reads.

It stays short, every rule line names its source, every named file exists, and its core part is a verbatim copy of
the core file (listed in the leak check's manifest) because a review sub-agent follows no `@` import."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STANDARDS = REPO_ROOT / "CODING_STANDARDS.md"
CORE_FILE = "docs/agents/core-standards.md"


def _rules(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("- ")]


def _source(rule: str) -> str | None:
    """The trailing parenthesis group of a rule line, or None when the line does not end with one."""
    if not rule.endswith(")"):
        return None
    depth = 0
    for i in range(len(rule) - 1, -1, -1):
        depth += {")": 1, "(": -1}.get(rule[i], 0)
        if depth == 0:
            return rule[i + 1 : -1]
    return None


def test_file_exists_and_is_under_120_lines():
    assert STANDARDS.is_file()
    assert len(STANDARDS.read_text(encoding="utf-8").splitlines()) < 120


def test_every_rule_line_ends_with_its_source():
    rules = _rules(STANDARDS.read_text(encoding="utf-8"))
    assert rules
    without = [r for r in rules if not _source(r)]
    assert without == []


def test_every_named_source_file_exists():
    missing = []
    for rule in _rules(STANDARDS.read_text(encoding="utf-8")):
        for path in re.findall(r"[\w./-]+\.(?:md|py|ini|txt)", _source(rule) or ""):
            if not (REPO_ROOT / path).is_file():
                missing.append(path)
    assert missing == []


def test_core_part_is_a_verbatim_copy_of_the_core_file():
    core_rules = _rules((REPO_ROOT / CORE_FILE).read_text(encoding="utf-8"))
    part_one = STANDARDS.read_text(encoding="utf-8").split("## 2.", 1)[0]
    assert core_rules
    assert _rules(part_one) == core_rules


def test_core_file_is_in_the_leak_check_manifest():
    manifest = (REPO_ROOT / "tools" / "agentops" / "core_manifest.txt").read_text(encoding="utf-8").splitlines()
    assert CORE_FILE in [line.strip() for line in manifest]
