"""#363 unit 1: the brain role's step 2 and the architect-rules extract.

Checks the live brain role file's step 2 ("The whole") against the text the ticket gives verbatim (the live
docs/agents/brain.md is byte-identical to the draft brain.md it was taken from), and that
docs/research/2026-10-24-brain-replays-4/architect-rules.md holds exactly that file's first line and its three
named sections, byte for byte, with nothing else. docs/research/ is never copied into the test copy (#231), so the
architect-rules tests skip on CI when that file is absent.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIVE = REPO / "docs" / "agents" / "brain.md"
ARCH = REPO / "docs" / "research" / "2026-10-24-brain-replays-4" / "architect-rules.md"
ARCH_SKIP_ON_CI = pytest.mark.skipif(
    not ARCH.is_file() and bool(os.environ.get("CI")),
    reason="architect-rules.md is under docs/research/, which the GitHub test copy never takes (#231)",
)

FIRST_LINE = "# The brain: the owner's chat"
SECTIONS = [
    "## The job",
    "## From the owner's words to tickets",
    "## Defects: the class, not the case",
]

STEP2_TEXT = (
    "2. **The whole.** Find every place the problem lives and everything that already does part of it: each repo, role,\n"
    "   process, tool, page, rule, open ticket, ADR and `GLOSSARY.md` term. The goal's own word covers every instance of\n"
    "   it (\"fully\", \"all tests\", \"every repo\"): list each instance in the project before setting the scope; the list is\n"
    "   the scope, and an instance left out carries its reason or goes to the owner as a question. What exists is reused\n"
    "   or replaced on purpose: a step another repo's plan already specifies is built once in the core, driven by\n"
    "   settings, and each repo's ticket sets only its values and runs it. An open ticket or decision that pulls the\n"
    "   other way is settled in the design.\n"
)


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _step2_block(draft: str) -> str:
    """The draft's step 2: from its line to the line before step 3, the line ends kept."""
    lines = draft.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith("2. **The whole.**"))
    end = next(i for i, line in enumerate(lines) if line.startswith("3. **The questions.**"))
    return "".join(lines[start:end])


def _sections(text: str) -> dict[str, str]:
    """Each `## ` section keyed by its heading line: the heading through the line before the next
    `## ` heading, trailing blank lines dropped."""
    lines = text.split("\n")
    heads = [i for i, line in enumerate(lines) if line.startswith("## ")]
    out: dict[str, str] = {}
    for n, start in enumerate(heads):
        stop = heads[n + 1] if n + 1 < len(heads) else len(lines)
        body = lines[start:stop]
        while body and body[-1] == "":
            body.pop()
        out[lines[start]] = "\n".join(body) + "\n"
    return out


def _draft_section_text(draft: str, heading: str) -> str:
    return _sections(draft)[heading]


def test_step2_of_draft_equals_ticket_text():
    draft = _read(LIVE)
    assert _step2_block(draft) == STEP2_TEXT


@ARCH_SKIP_ON_CI
def test_architect_rules_holds_first_line_and_three_sections_byte_for_byte():
    draft = _read(LIVE)
    arch = _read(ARCH)

    assert draft.split("\n", 1)[0] == FIRST_LINE
    assert arch.split("\n", 1)[0] == FIRST_LINE

    for heading in SECTIONS:
        assert _sections(arch)[heading] == _draft_section_text(draft, heading), heading

    expected_body = "\n".join(_draft_section_text(draft, h) for h in SECTIONS)
    assert arch == FIRST_LINE + "\n\n" + expected_body


@ARCH_SKIP_ON_CI
def test_architect_rules_has_no_other_section_heading():
    arch = _read(ARCH)
    assert [line for line in arch.split("\n") if line.startswith("## ")] == SECTIONS
