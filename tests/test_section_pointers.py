"""Issue #366 unit 3: every section pointer in the rule files names a section that exists.

A pointer is a ``<file>.md`` path followed by a quoted section name, as in ``docs/agents/brain.md "Reset"``.
The scan reads the files the issue lists (the hooks, the agentops modules, the agent docs, CLAUDE.md,
GLOSSARY.md, the role files and the skills), resolves each pointed-at file (a bare name against
``docs/agents/`` then the repo root; CLAUDE.md also through its ``@`` import lines) and fails listing every
pointer whose name is not the start of a heading of that file and not the start of a paragraph or list item
in it (a list marker and a leading ``**`` are skipped).

A quoted name that holds ``<...>`` is a literal line of a prompt (``"Worktree: <path>"``), not a section name,
so it is not a pointer.
"""

from __future__ import annotations

import re
from pathlib import Path

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

# `<file>.md`, then (optionally a closing backtick and a comma, then) at least one space, then the quoted name.
# A quote right after `.md` is the end of a Python string, not a pointer: `"a.md", "b.md"` yields nothing.
POINTER_RE = re.compile(r'(?P<file>[\w.][\w./-]*\.md)`?,?\s+"(?P<name>[^"\n]+)"')
IMPORT_RE = re.compile(r"^@(\S+)", re.MULTILINE)
HEADING_RE = re.compile(r"^#{1,6}\s+(.*\S)\s*$")
LIST_MARK_RE = re.compile(r"^(?:[-*+]|\d+\.)\s+")

_starts_cache: dict[Path, list[str]] = {}


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


def test_scan_reads_the_listed_files():
    assert scanned_files(), "no file in the scanned globs; the pointer scan would pass on nothing"


def test_every_section_pointer_names_a_real_section():
    dead = dead_pointers()
    assert not dead, f"{len(dead)} dead section pointer(s):\n" + "\n".join(dead)
