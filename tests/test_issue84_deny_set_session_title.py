"""#84 row 1: no agent can rename a chat -- the settings refuse `mcp__ccd_session_mgmt__set_session_title`.

The shipped template (`config/core_templates/settings.hooks.json`, core_manifest.txt:23) and this home's own
`.claude/settings.json` both hold the rule in `permissions.deny`. The literal name is the source of truth."""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RENAME_RULE = "mcp__ccd_session_mgmt__set_session_title"


def test_shipped_template_denies_set_session_title():
    template = PROJECT_ROOT / "config" / "core_templates" / "settings.hooks.json"
    deny = json.loads(template.read_text(encoding="utf-8"))["permissions"]["deny"]
    assert RENAME_RULE in deny


def test_home_settings_deny_set_session_title():
    settings = PROJECT_ROOT / ".claude" / "settings.json"
    deny = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["deny"]
    assert RENAME_RULE in deny


def test_autopilot_settings_deny_set_session_title():
    settings = PROJECT_ROOT / "tools" / "agentops" / "autopilot_settings.json"
    deny = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["deny"]
    assert RENAME_RULE in deny
