"""Issue #77 row 4: no file under mods/pob-slots/ holds a control byte (0x00-0x08, 0x0B, 0x0C, 0x0E-0x1F).

A byte scan, so it runs on any machine (the mod runner is not needed). `node_modules`, `__pycache__` and known binary
formats are skipped; every other file is read as bytes and every byte is checked.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MOD = REPO / "mods" / "pob-slots"
SKIP_DIRS = {"node_modules", "__pycache__"}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".woff", ".woff2", ".ttf", ".otf", ".zip", ".gz", ".pdf", ".pyc"}
CONTROL = set(range(0x00, 0x09)) | {0x0B, 0x0C} | set(range(0x0E, 0x20))


def _files() -> list[Path]:
    out = []
    for p in sorted(MOD.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(MOD).parts
        if any(part in SKIP_DIRS for part in rel[:-1]) or p.suffix.lower() in BINARY_SUFFIXES:
            continue
        out.append(p)
    return out


def test_no_control_bytes_under_the_pob_slots_mod():
    hits = []
    for p in _files():
        data = p.read_bytes()
        for i, b in enumerate(data):
            if b in CONTROL:
                line = data.count(b"\n", 0, i) + 1
                hits.append(f"{p.relative_to(REPO).as_posix()}:{line} 0x{b:02x}")
    assert hits == [], "control bytes in the mod: " + "; ".join(hits)
