"""Every reason a did-not-land merge record can write has words on the Merge card (#123).

The card's one table is NOT_LANDED_WHY in mods/pob-slots/hooks/register.tsx; a reason not in it reads «не влит: <code>».
The claude-code test environment has no fs, so the reasons are read here, at test time: merge.py's `reason=` writes
(and the tuple returns of its sync helpers), less the ones only a commit writes or a success writes, plus the five
reasons detach.py writes outside merge.py. The card test in mods/pob-slots/test/merge-card.test.tsx lists the same set
(DID_NOT_LAND_REASONS), and this file holds that list and the table to the source.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MERGE = ROOT / "tools" / "agentops" / "merge.py"
DETACH = ROOT / "tools" / "agentops" / "detach.py"
REGISTER = ROOT / "mods" / "pob-slots" / "hooks" / "register.tsx"
CARD_TEST = ROOT / "mods" / "pob-slots" / "test" / "merge-card.test.tsx"

# written only by a commit (its result file is `commit`, never a merge row) or by a success (rc 0)
COMMIT_OR_SUCCESS = {"add-failed", "commit-failed", "nothing-to-commit", "quick-check-red", "pushed", "suite-green"}
# written by detach.py's classify or the merge command's exit code, never as `reason=` in merge.py
DETACH_ONLY = {"setup-error", "lock-timeout", "silent-holder", "failed", "withdrawn", "superseded"}  # withdrawn, superseded: #219 (detach.py maps the marker)

ASSIGN_RE = re.compile(r'reason\s*=\s*"([a-z][a-z-]*)"(?:\s+if\s+\w+\s+else\s+"([a-z][a-z-]*)")?')
TUPLE_RE = re.compile(r'return\s+(?:False|""|\w+),\s*"([a-z][a-z-]*)"')
TABLE_BLOCK_RE = re.compile(r"const NOT_LANDED_WHY = new Map<[^\n]*\(\[(.*?)\n\]\)", re.S)
TABLE_KEY_RE = re.compile(r"\['([a-z][a-z-]*)',\s*(?:\(\)|f)\s*=>")
CARD_LIST_RE = re.compile(r"DID_NOT_LAND_REASONS = \[(.*?)\]", re.S)
ITEM_RE = re.compile(r"'([a-z][a-z-]*)'")


def _merge_reasons() -> set[str]:
    text = MERGE.read_text(encoding="utf-8")
    found: set[str] = set()
    for first, second in ASSIGN_RE.findall(text):
        found.add(first)
        if second:
            found.add(second)
    found.update(TUPLE_RE.findall(text))
    return found


def _expected() -> set[str]:
    return (_merge_reasons() - COMMIT_OR_SUCCESS) | DETACH_ONLY


def test_the_reason_scan_reads_merge_py():
    found = _merge_reasons()
    assert "red-on-branch" in found, "the reason scan found no red-on-branch in merge.py"
    assert len(found) >= 20, f"the reason scan read only {len(found)} values from merge.py: {sorted(found)}"


def test_detach_only_reasons_are_written_by_detach_py():
    text = DETACH.read_text(encoding="utf-8")
    missing = [r for r in sorted(DETACH_ONLY) if f'"{r}"' not in text]
    assert not missing, f"detach.py no longer writes these reasons: {missing}"


def test_card_test_lists_every_reason_merge_and_detach_can_write_on_a_did_not_land_row():
    match = CARD_LIST_RE.search(CARD_TEST.read_text(encoding="utf-8"))
    assert match, "DID_NOT_LAND_REASONS not found in merge-card.test.tsx"
    listed = set(ITEM_RE.findall(match.group(1)))
    expected = _expected()
    assert listed == expected, f"missing from the card test: {sorted(expected - listed)}; extra: {sorted(listed - expected)}"


def test_every_such_reason_has_words_in_the_one_table():
    text = REGISTER.read_text(encoding="utf-8")
    block = TABLE_BLOCK_RE.search(text)
    assert block, "NOT_LANDED_WHY not found in register.tsx"
    keys = set(TABLE_KEY_RE.findall(block.group(1)))
    missing = sorted(_expected() - keys)
    assert not missing, f"no words in NOT_LANDED_WHY (register.tsx) for: {missing}"
