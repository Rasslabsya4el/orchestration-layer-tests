"""Issue #51: every cite in ``docs/agents/core-standards.md`` names files that hold the rule's words.

Each rule line ends with its cites in parentheses. The test names the key phrases of each rule (``PHRASES``, keyed by
the rule's opening words), and checks two things: every cited file holds at least one key phrase, and every key phrase
is held by at least one cited file. A cite that names a file without the rule's words fails, as does a claim whose
words no cited file holds.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_FILE = REPO_ROOT / "docs" / "agents" / "core-standards.md"
CITED_PATH = re.compile(r"[\w./-]+\.(?:md|py|ini|txt)")

PHRASES = {
    "The code carries no ledgers": ("ledgers",),
    "Validation is a test that measures": ("measures the outcome", "schema check"),
    "No parallel registry": ("parallel registry",),
    "A decision becomes an ADR": ("hard to reverse", "commit message"),
    "A new ADR takes the highest number": ("highest number",),
    "Human-facing text writes a duration": ("a glance", "machine files"),
    "A rate is quoted per minute": ("per 100 s",),
    "Each commit carries its before/after numbers": ("before/after numbers",),
    "A red suite is never merged around": ("merged around",),
    "A test that judges built data": ("judges built data",),
    "A term in a report that is not in": ("finding for the brain",),
    "A draft of a comment": ("scratchpad directory",),
    "A process an agent starts": ("opens a console window",),
    "Every wait is bounded": ("background shell",),
    "A skill is called with the Skill tool": ("Skill tool",),
    "A git merge or rebase conflict": ("merge or rebase conflict",),
    "Pocock's skills are used as shipped": ("Pocock", "copied or overridden"),
    "A ticket is opened or rewritten": ("write-ticket",),
    "A subagent never decides": ("never decides what a domain fact means",),
    "A permission-guard refusal is final": ("never retries it in another form",),
    "Enforced by tooling, not reviewed": ("tracker", "permission", "worker", "leak"),
}


def _cited_rules() -> list[tuple[int, str, str]]:
    """(line number, rule text, parenthetical cite group) for every rule line that ends with a cite."""
    found = []
    for number, line in enumerate(CORE_FILE.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.startswith("- ") or not line.endswith(")") or "(" not in line:
            continue
        cite = line[line.rindex("(") + 1 : -1]
        found.append((number, line[2:], cite))
    return found


def _phrases_for(rule: str) -> tuple[str, ...]:
    matches = [phrases for opening, phrases in PHRASES.items() if rule.startswith(opening)]
    assert len(matches) == 1, f"no key phrases named for rule: {rule!r}"
    return matches[0]


@pytest.mark.parametrize(
    ("number", "rule", "cite"),
    _cited_rules(),
    ids=[f"line{number}" for number, _rule, _cite in _cited_rules()],
)
def test_every_cited_file_holds_the_rule_words(number, rule, cite):
    phrases = _phrases_for(rule)
    cited = CITED_PATH.findall(cite)
    assert cited, f"line {number} cites no file"
    texts = {path: (REPO_ROOT / path).read_text(encoding="utf-8").lower() for path in cited}

    without_words = [path for path, text in texts.items() if not any(p.lower() in text for p in phrases)]
    unheld = [p for p in phrases if not any(p.lower() in text for text in texts.values())]

    assert not without_words, f"line {number}: cited files without the rule's words {without_words} (phrases {list(phrases)})"
    assert not unheld, f"line {number}: no cited file holds {unheld}"
