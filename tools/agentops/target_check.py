"""Issue #73: the merged core text, read with each target's own checks before the home's merge moves main.

One function crosses the seam: ``check_targets(merged_root, targets, home_root)`` returns the hits, empty when every
target accepts the text. A target is an entry of the settings' ``targets`` (``{repo, folder}``); what it contributes is
read from its own folder, so the home holds no copy of any target's rules:

* ``config/core_leak_patterns.json``: ``core_check.run_check`` over the merged tree with that file; a manifest file
  with an L1-L3 hit is reported with each judged line (``core_check.leak_hits``).
* ``GLOSSARY.md`` and ``checked_avoid_words`` (#88): the target names, in the same settings file, the avoid words its
  own check covers (a list of strings). Only a word in that list that the glossary's ``_Avoid_`` list also holds is
  scanned, as a whole word, case-insensitive, over each ``docs/agents/**/*.md`` of the merged tree; backtick code
  spans are not prose and are blanked first. A target with no such key scans no avoid word, whatever its glossary holds.

A folder that does not exist, or holds neither file, contributes nothing. Stdlib only, like the rest of the core.

#93: a target whose folder exists but has no ``config/core_leak_patterns.json`` is not checked for leak words; ``unchecked_targets``
names it in one note line (never a refusal), and ``note_lines`` gives the lines the merge result prints.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .core_check import leak_hits, read_patterns, run_check, PATTERN_KEYS
from .core_targets import _folder_of

PATTERNS_REL = Path("config") / "core_leak_patterns.json"
GLOSSARY_REL = "GLOSSARY.md"
MANIFEST_REL = Path("tools") / "agentops" / "core_manifest.txt"
CHECKED_AVOID_KEY = "checked_avoid_words"
AVOID_PREFIX = "_Avoid_:"
CODE_SPAN = re.compile(r"`[^`]*`")
PAREN = re.compile(r"\([^)]*\)")
NOTE_PREFIX = "target check: "  # #93: the merge result's output line that carries a note (merge.py, merge_handback.py)


@dataclass(frozen=True)
class TargetHit:
    repo: str
    path: str  # repo-relative, "/" separated
    line_no: int
    word: str

    def line(self) -> str:
        return f"{self.repo} rejects core text: {self.path}:{self.line_no} {self.word}"


def check_targets(merged_root: Path, targets: list, home_root: Path, *, scan_avoid: bool = True) -> list[TargetHit]:
    """The hits of ``merged_root`` (a checked-out tree of the merged core) against every target in ``targets``."""
    hits: list[TargetHit] = []
    for entry in targets:
        folder = _folder_of(entry, home_root)
        if folder is None or not folder.is_dir():
            continue
        repo = str(entry.get("repo") or "?")
        hits += [TargetHit(repo, *found) for found in _leak_hits(merged_root, folder / PATTERNS_REL)]
        if scan_avoid:
            checked = checked_avoid_words(folder / PATTERNS_REL)
            hits += [TargetHit(repo, *found) for found in _avoid_hits(merged_root, folder / GLOSSARY_REL, checked)]
    return hits


def unchecked_targets(targets: list, home_root: Path) -> list[str]:
    """#93: one note per target whose folder exists and has no leak-words file: ``<repo>: no leak words, not checked``.
    A missing folder contributes nothing, as in ``check_targets``. A note is never a refusal."""
    notes: list[str] = []
    for entry in targets:
        folder = _folder_of(entry, home_root)
        if folder is None or not folder.is_dir() or (folder / PATTERNS_REL).is_file():
            continue
        notes.append(f"{entry.get('repo') or '?'}: no leak words, not checked")
    return notes


def note_lines(notes: list[str]) -> list[str]:
    """The merge result's output lines for ``notes`` (``NOTE_PREFIX`` + note); the comment source reads them back."""
    return [f"{NOTE_PREFIX}{note}" for note in notes]


def _leak_hits(merged_root: Path, patterns_path: Path) -> list[tuple[str, int, str]]:
    manifest = merged_root / MANIFEST_REL
    if not patterns_path.is_file() or not manifest.is_file():
        return []
    patterns = read_patterns(patterns_path)
    rows, _ok = run_check(merged_root, manifest, patterns_path)
    found: list[tuple[str, int, str]] = []
    for row in rows:
        judged = [key for key in PATTERN_KEYS
                  if (row.l2_judged if key == "L2" and row.l2_judged is not None else row.counts.get(key, 0)) > 0]
        if row.missing or not judged:
            continue
        found += [(row.path, number, word) for number, key, word in leak_hits(merged_root / row.path, row.path, patterns)]
    return found


def avoid_words(glossary_path: Path) -> list[str]:
    """The ``_Avoid_`` words of a glossary, lower-cased, in file order, each once. A line is comma-separated;
    a parenthesised note is dropped; an entry that is a code span is not a word of prose and is left out."""
    words: list[str] = []
    for raw in glossary_path.read_text(encoding="utf-8").splitlines():
        if not raw.startswith(AVOID_PREFIX):
            continue
        for part in PAREN.sub("", raw[len(AVOID_PREFIX):]).split(","):
            word = part.strip().rstrip(".").strip().lower()
            if word and "`" not in word and word not in words:
                words.append(word)
    return words


def checked_avoid_words(patterns_path: Path) -> list[str]:
    """The target's ``checked_avoid_words`` (#88), lower-cased, in file order. A missing file, a missing key or a
    value that is not a list gives none."""
    if not patterns_path.is_file():
        return []
    raw = json.loads(patterns_path.read_text(encoding="utf-8"))
    values = raw.get(CHECKED_AVOID_KEY) if isinstance(raw, dict) else None
    if not isinstance(values, list):
        return []
    return [word.strip().lower() for word in values if isinstance(word, str) and word.strip()]


def _avoid_hits(merged_root: Path, glossary_path: Path, checked: list[str]) -> list[tuple[str, int, str]]:
    """The hits of the checked words that the glossary's ``_Avoid_`` list also holds; a word outside either is not scanned."""
    if not glossary_path.is_file() or not checked:
        return []
    held = set(avoid_words(glossary_path))
    words = list(dict.fromkeys(word for word in checked if word in held))
    patterns = [(word, re.compile(rf"(?<!\w){re.escape(word)}(?!\w)", re.IGNORECASE))
                for word in words]
    found: list[tuple[str, int, str]] = []
    for path in sorted((merged_root / "docs" / "agents").rglob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        prose = CODE_SPAN.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
        rel = path.relative_to(merged_root).as_posix()
        for number, line in enumerate(prose.splitlines(), start=1):
            found += [(rel, number, word) for word, pattern in patterns if pattern.search(line)]
    return found
