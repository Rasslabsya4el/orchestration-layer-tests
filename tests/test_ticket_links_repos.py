"""#176 (bare-n-and-repo-limit): the ticket-links mod reads its list of core repos from a constant, not from a file.

The constant must hold the home repo and every target repo of config/orchestration.json, and the mod's hooks may
read no file, clock or model at run time (a grep over them holds 0 hits).
"""
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MOD_HOOKS = ROOT / "mods" / "ticket-links" / "hooks"
REPOS_TS = MOD_HOOKS / "repos.ts"
CONFIG = ROOT / "config" / "orchestration.json"
FORBIDDEN = ("$.fs", "$.process", "$.clock", "$.model")

# repos.ts is kept out of the public test copy (it lists repos); the merge runs these tests on the PC, where it exists.
pytestmark = pytest.mark.skipif(not REPOS_TS.exists(), reason="repos.ts is kept out of the public test copy")


def _core_repos_from_ts() -> list[str]:
    text = REPOS_TS.read_text(encoding="utf-8")
    m = re.search(r"export\s+const\s+CORE_REPOS\b[^=]*=\s*\[(.*?)\]", text, re.DOTALL)
    assert m, "repos.ts does not export CORE_REPOS as an array literal"
    return [a or b for a, b in re.findall(r"'([^'\n]+)'|\"([^\"\n]+)\"", m.group(1))]


def _core_repos_from_config() -> list[str]:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    return [cfg["repo"], *(t["repo"] for t in cfg["targets"])]


def test_core_repos_equal_home_and_targets_of_config():
    ts = _core_repos_from_ts()
    cfg = _core_repos_from_config()
    assert len(ts) == len(set(r.lower() for r in ts)), f"CORE_REPOS holds a repo twice: {ts}"
    assert {r.lower() for r in ts} == {r.lower() for r in cfg}, f"CORE_REPOS {ts} != config {cfg}"


def test_mod_hooks_read_no_file_clock_or_model():
    files = sorted(p for p in MOD_HOOKS.glob("*.tsx")) + [REPOS_TS]
    assert files, "no hooks found"
    hits = []
    for path in files:
        if path.name == "draw.ts":
            continue
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN:
            if token in text:
                hits.append(f"{path.name}: {token}")
    assert hits == [], f"run-time access in the mod's hooks: {hits}"
