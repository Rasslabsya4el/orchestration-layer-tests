"""#72 unit 2: the core holds no word of Poe's L2 leak pattern; the skill counter's extra watched roles
come from the settings file (`skill_watched_roles`), not from a literal in the core.

Synthetic transcripts under tmp_path and a synthetic settings file; the real ~/.claude and the real
config/orchestration.json are never changed (the settings path is patched per test).
"""
import json
import re
from pathlib import Path

import pytest

from tools.agentops import core_settings, skill_counter

REPO = Path(__file__).resolve().parents[1]
PROJECT = "C--Coding-orchestration-layer"
SESSION_ROLE = "session"
#: Poe's L2 leak pattern (C:/Coding/poe-junkroan-v2/config/core_leak_patterns.json), spelled here as a literal.
POE_L2 = r"keymaster|runs submit|runs\.py|machine_load|run_queue|\bslots?\b(?!\s*=)|filler|laptop|test runs"


def _write_settings(path: Path, extra: dict) -> Path:
    data = {k: "x" for k in core_settings.REQUIRED_KEYS}
    data.update(extra)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _main_session(root: Path, name: str, setting: str):
    """A main session whose first record is the agent-setting record, with one diagnosing-bugs call in the window."""
    root.joinpath(PROJECT).mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "agent-setting", "agentSetting": setting, "sessionId": name},
        {"type": "assistant", "timestamp": "2026-09-20T10:00:00.000Z",
         "message": {"content": [{"type": "tool_use", "name": "Skill",
                                  "input": {"skill": "mattpocock-skills:diagnosing-bugs"}}]}},
    ]
    (root / PROJECT / f"{name}.jsonl").write_text("\n".join(json.dumps(r) for r in lines) + "\n", encoding="utf-8")


@pytest.fixture
def bare_root(tmp_path, monkeypatch):
    """A fake home with an empty project folder and no merge log."""
    home = tmp_path / "home"
    root = home / ".claude" / "projects"
    (root / PROJECT).mkdir(parents=True)
    monkeypatch.setattr(skill_counter, "MERGE_LOG", tmp_path / "no-merge.log")
    return root


def test_settings_key_makes_the_named_setting_a_watched_role(bare_root, tmp_path, monkeypatch):
    settings = _write_settings(tmp_path / "orchestration.json", {"skill_watched_roles": ["keymaster"]})
    monkeypatch.setattr(core_settings, "SETTINGS_PATH", settings)
    _main_session(bare_root, "km", "keymaster")
    t = skill_counter.tally(bare_root, since="2026-09-11", until="2026-10-15")
    assert t.calls == {("mattpocock-skills:diagnosing-bugs", "keymaster"): 1}
    text = skill_counter.render(t, since="2026-09-11", until="2026-10-15", issue=None)
    assert "| skill | total | keymaster | researcher-find | researcher-how |" in text


def test_absent_settings_key_leaves_the_session_a_plain_session(bare_root, tmp_path, monkeypatch):
    settings = _write_settings(tmp_path / "orchestration.json", {})
    monkeypatch.setattr(core_settings, "SETTINGS_PATH", settings)
    _main_session(bare_root, "km", "keymaster")
    t = skill_counter.tally(bare_root, since="2026-09-11", until="2026-10-15")
    assert t.calls == {("mattpocock-skills:diagnosing-bugs", SESSION_ROLE): 1}
    text = skill_counter.render(t, since="2026-09-11", until="2026-10-15", issue=None)
    assert "keymaster" not in text


def test_core_manifest_files_under_agentops_and_hooks_hold_no_poe_l2_word():
    manifest = [line.strip() for line in (REPO / "tools" / "agentops" / "core_manifest.txt").read_text(
        encoding="utf-8").splitlines() if line.strip() and not line.strip().startswith("#")]
    checked = [p for p in manifest if p.startswith(("tools/agentops/", "tools/hooks/"))]
    assert checked, "the manifest names no tools/agentops or tools/hooks file"
    pattern = re.compile(POE_L2, re.IGNORECASE)
    hits = []
    for rel in checked:
        for lineno, line in enumerate((REPO / rel).read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            # the manifest's own path lines under mods/ name the monitor's files: core_check exempts them the same way
            if rel.endswith("core_manifest.txt") and line.strip().startswith("mods/"):
                continue
            if pattern.search(line):
                hits.append(f"{rel}:{lineno}")
    assert hits == []
