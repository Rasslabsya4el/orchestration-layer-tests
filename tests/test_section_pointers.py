"""Issue #366 unit 3 and #369 unit 4: every pointer in the rule files names something that exists.

A section pointer is a ``<file>.md`` path followed by a quoted section name, as in ``docs/agents/brain.md "Reset"``.
The scan reads the files the issue lists (the hooks, the agentops modules, the agent docs, CLAUDE.md,
GLOSSARY.md, the role files and the skills), resolves each pointed-at file (a bare name against
``docs/agents/`` then the repo root; CLAUDE.md also through its ``@`` import lines) and fails listing every
pointer whose name is not the start of a heading of that file and not the start of a paragraph or list item
in it (a list marker and a leading ``**`` are skipped).

#369 adds two pointer kinds, checked by the same scan:

- ``principle N`` (also ``principles 5 and 7``): N must be a principle the core block holds, i.e. one of the lines
  ``N. **...`` of ``docs/agents/core-block.md``.
- ``point N of the <name>'s rule`` and ``point N of <file>.md "<section>"``: the named rule file (or the named
  section's paragraphs and list items) must hold a numbered list item ``N.``.

A quoted name that holds ``<...>`` is a literal line of a prompt (``"Worktree: <path>"``), not a section name,
so it is not a pointer. A pointer-shaped phrase inside a quoted fixture is not scanned: the fixtures live under
``tests/``, which is not in ``SCANNED_GLOBS``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]

SCANNED_GLOBS = (
    "tools/hooks/*.py",
    "tools/agentops/*.py",
    "docs/agents/*.md",
    "docs/agents/local/*.md",
    "CLAUDE.md",
    "GLOSSARY.md",
    ".claude/agents/*.md",
    ".claude/skills/*/SKILL.md",
)

CORE_BLOCK = "docs/agents/core-block.md"

# `<file>.md`, then (optionally a closing backtick and a comma, then) at least one space, then the quoted name.
# A quote right after `.md` is the end of a Python string, not a pointer: `"a.md", "b.md"` yields nothing.
POINTER_RE = re.compile(r'(?P<file>[\w.][\w./-]*\.md)`?,?\s+"(?P<name>[^"\n]+)"')
IMPORT_RE = re.compile(r"^@(\S+)", re.MULTILINE)
HEADING_RE = re.compile(r"^#{1,6}\s+(.*\S)\s*$")
LIST_MARK_RE = re.compile(r"^(?:[-*+]|\d+\.)\s+")
# `principle 5`, `principles 5 and 7`, `principles 5, 7 or 9`.
PRINCIPLE_RE = re.compile(r"\bprinciples?\s+(?P<nums>\d+(?:\s*(?:,|and|or|&)\s*\d+)*)", re.IGNORECASE)
# `point 3 of the autopilot's rule`.
POINT_OF_RULE_RE = re.compile(r"\bpoints?\s+(?P<n>\d+)\s+of\s+(?:the\s+)?(?P<stem>[\w-]+)'s\s+rule", re.IGNORECASE)
# `point 3 of brain.md "Reset"`, `point 3 of `brain.md` "Reset"`, `point 3 of the brain.md "Reset"`.
POINT_OF_SECTION_RE = re.compile(
    r'\bpoints?\s+(?P<n>\d+)\s+of\s+(?:the\s+)?`?(?P<file>[\w][\w./-]*\.md)`?,?\s+"(?P<name>[^"\n]+)"', re.IGNORECASE
)
NUMBERED_ITEM_RE = re.compile(r"^\s*(\d+)\.\s")

_starts_cache: dict[Path, list[str]] = {}

class Problem(NamedTuple):
    file: str
    line: int
    matched: str  # the pointer's matched text
    message: str


def scanned_files() -> list[Path]:
    files: list[Path] = []
    for pattern in SCANNED_GLOBS:
        files.extend(sorted(ROOT.glob(pattern)))
    return [f for f in files if f.is_file()]


def resolve(file_ref: str) -> Path | None:
    if "/" in file_ref:
        candidates = [ROOT / file_ref]
    else:
        candidates = [ROOT / "docs" / "agents" / file_ref, ROOT / file_ref]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def section_starts(path: Path) -> list[str]:
    """Heading texts, and the starts of paragraphs and list items, of a file (CLAUDE.md also through its imports).

    A paragraph start is a non-blank line after a blank line (or after a heading), or a list item. A list marker
    (``- ``, ``1. ``) and a leading ``**`` are skipped.
    """
    if path in _starts_cache:
        return _starts_cache[path]
    sources = [path]
    if path.name == "CLAUDE.md":
        sources += [ROOT / imp for imp in IMPORT_RE.findall(path.read_text(encoding="utf-8"))]
    starts: list[str] = []
    for source in sources:
        block_start = True
        for line in source.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                block_start = True
                continue
            heading = HEADING_RE.match(line)
            if heading:
                starts.append(heading.group(1).strip())
                block_start = True
                continue
            is_item = LIST_MARK_RE.match(stripped) is not None
            if block_start or is_item:
                text = LIST_MARK_RE.sub("", stripped, count=1)
                starts.append(text.lstrip("*").strip())
            block_start = False
    _starts_cache[path] = starts
    return starts


def is_section(name: str, starts: list[str]) -> bool:
    """The name is the start of a heading or of a paragraph or list item (case-sensitive; a trailing ``.``, ``:`` or ``**`` of the name is not part of it)."""
    key = name.strip().rstrip(" .:*")
    return any(start.startswith(key) for start in starts)


def dead_pointers() -> list[str]:
    dead: list[str] = []
    for src in scanned_files():
        rel = src.relative_to(ROOT).as_posix()
        for lineno, line in enumerate(src.read_text(encoding="utf-8").splitlines(), start=1):
            for match in POINTER_RE.finditer(line):
                name = match.group("name").strip()
                if "<" in name or ">" in name:
                    continue
                file_ref = match.group("file")
                where = f'{rel}:{lineno}: {file_ref} "{name}"'
                target = resolve(file_ref)
                if target is None:
                    dead.append(f"{where} (no such file)")
                elif not is_section(name, section_starts(target)):
                    dead.append(where)
    return dead


def principle_numbers(core_text: str) -> set[int]:
    """The principle numbers the core block holds: its lines ``N. **Title.**``."""
    return {int(n) for n in re.findall(r"^(\d+)\. \*\*", core_text, re.MULTILINE)}


def numbered_points(text: str, section: str | None = None) -> set[int] | None:
    """The numbers of the numbered list items of a file, or of one section of it.

    A section starts at the heading (or the line) whose text starts with ``section`` and runs to the next heading.
    None when the section is not in the file.
    """
    lines = text.splitlines()
    if section is None:
        region = lines
    else:
        start = None
        for i, line in enumerate(lines):
            heading = HEADING_RE.match(line)
            text_of_line = heading.group(1) if heading else line.strip().lstrip("*_ ")
            if text_of_line.startswith(section.strip().rstrip(" .:*")):
                start = i + 1
                break
        if start is None:
            return None
        region = []
        for line in lines[start:]:
            if HEADING_RE.match(line):
                break
            region.append(line)
    return {int(m.group(1)) for line in region if (m := NUMBERED_ITEM_RE.match(line))}


def principle_problems(rel: str, text: str, principles: set[int]) -> list[Problem]:
    problems: list[Problem] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in PRINCIPLE_RE.finditer(line):
            for n in re.findall(r"\d+", match.group("nums")):
                if int(n) not in principles:
                    problems.append(Problem(rel, lineno, match.group(0), f"the core block holds no principle {n}"))
    return problems


def point_problems(rel: str, text: str, read_doc) -> list[Problem]:
    """``read_doc(file_ref)`` returns the text of the referenced file, or None when there is no such file."""
    problems: list[Problem] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        # (match, file to read, section name or None for the whole file)
        pointers = [(m, f"{m.group('stem')}.md", None) for m in POINT_OF_RULE_RE.finditer(line)]
        pointers += [(m, m.group("file"), m.group("name").strip()) for m in POINT_OF_SECTION_RE.finditer(line)]
        for match, ref, section in pointers:
            n = int(match.group("n"))
            body = read_doc(ref)
            if body is None:
                problems.append(Problem(rel, lineno, match.group(0), f"no such file {ref}"))
                continue
            points = numbered_points(body, section)
            if points is not None and n not in points:
                where = ref if section is None else f'section "{section}" of {ref}'
                problems.append(Problem(rel, lineno, match.group(0), f"{where} has no numbered point {n}"))
    return problems


def rule_problems() -> list[Problem]:
    """The principle and point pointers of the real repo."""
    core = ROOT / CORE_BLOCK
    principles = principle_numbers(core.read_text(encoding="utf-8"))

    def read_doc(ref: str) -> str | None:
        target = resolve(ref)
        return target.read_text(encoding="utf-8") if target is not None else None

    problems: list[Problem] = []
    for src in scanned_files():
        rel = src.relative_to(ROOT).as_posix()
        text = src.read_text(encoding="utf-8")
        problems += principle_problems(rel, text, principles)
        problems += point_problems(rel, text, read_doc)
    return problems


def test_scan_reads_the_listed_files():
    assert scanned_files(), "no file in the scanned globs; the pointer scan would pass on nothing"


def test_every_section_pointer_names_a_real_section():
    dead = dead_pointers()
    assert not dead, f"{len(dead)} dead section pointer(s):\n" + "\n".join(dead)


# --- #369: principle and point pointers, planted strings ---------------------------------------------------------

CORE_TEXT = "5. **No ceremony.** text\n   more\n7. **Time.** text\n"


def test_planted_see_principle_9_is_reported():
    problems = principle_problems("docs/agents/x.md", "see principle 9", principle_numbers(CORE_TEXT))
    assert len(problems) == 1 and problems[0].file == "docs/agents/x.md" and problems[0].line == 1


def test_planted_principle_5_and_7_pass():
    text = "see principle 5 and (principle 7); principles 5 and 7; principle 7, 5 or 7"
    assert principle_problems("docs/agents/x.md", text, principle_numbers(CORE_TEXT)) == []


def test_planted_principles_list_with_9_is_reported():
    problems = principle_problems("docs/agents/x.md", "principles 5 and 9", principle_numbers(CORE_TEXT))
    assert len(problems) == 1 and "9" in problems[0].message


def test_planted_principle_6_in_core_text_with_only_5_and_7_is_reported():
    problems = principle_problems("docs/agents/orch.md", "(principle 6)", principle_numbers(CORE_TEXT))
    assert len(problems) == 1


# autopilot.md with two numbered points (1, 2) and no numbered point 3 anywhere in the file
AUTOPILOT_TWO_POINTS = "# Autopilot\n\nintro\n\n1. first\n2. second\n\n## Reset\n\n1. a\n2. b\n"
# the same file with a numbered point 3 under its own section "Other", not under "Reset"
AUTOPILOT_WITH_OTHER = AUTOPILOT_TWO_POINTS + "\n## Other\n\n3. c\n"


def _docs_of(text):
    return lambda ref: text if ref == "autopilot.md" else None


def test_planted_point_3_of_autopilot_rule_is_reported():
    text = "see point 3 of the autopilot's rule"
    problems = point_problems("docs/agents/brain-reference.md", text, _docs_of(AUTOPILOT_TWO_POINTS))
    assert len(problems) == 1 and "numbered point 3" in problems[0].message


def test_planted_point_2_of_autopilot_rule_passes():
    text = "see point 2 of the autopilot's rule"
    assert point_problems("docs/agents/brain-reference.md", text, _docs_of(AUTOPILOT_TWO_POINTS)) == []


def test_planted_point_of_missing_rule_file_is_reported():
    problems = point_problems("docs/agents/x.md", "point 1 of the nosuch's rule", _docs_of(AUTOPILOT_TWO_POINTS))
    assert len(problems) == 1 and "no such file" in problems[0].message


def test_planted_point_of_section_is_scoped_to_that_section():
    docs = _docs_of(AUTOPILOT_WITH_OTHER)
    reported = point_problems("docs/agents/x.md", 'see point 3 of autopilot.md "Reset"', docs)
    passing = point_problems("docs/agents/x.md", 'see point 3 of autopilot.md "Other"', docs)
    assert len(reported) == 1 and passing == []


def test_planted_point_of_section_with_backticked_path_is_reported():
    problems = point_problems("docs/agents/x.md", 'see point 3 of `autopilot.md` "Reset"', _docs_of(AUTOPILOT_WITH_OTHER))
    assert len(problems) == 1 and "numbered point 3" in problems[0].message


def test_planted_point_of_section_with_the_is_reported():
    problems = point_problems("docs/agents/x.md", 'see point 3 of the autopilot.md "Reset"', _docs_of(AUTOPILOT_WITH_OTHER))
    assert len(problems) == 1 and "numbered point 3" in problems[0].message


def test_real_repo_principle_and_point_pointers_name_existing_numbers():
    dead = [f"{p.file}:{p.line}: {p.matched}: {p.message}" for p in rule_problems()]
    assert not dead, f"{len(dead)} dead principle/point pointer(s):\n" + "\n".join(dead)
