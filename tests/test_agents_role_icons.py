"""Every agent in .claude/agents/ has a role icon in the pane (#15).

The pane's role-icon map is `ROLE_ICON` in mods/pob-slots/hooks/kit.tsx; a role that is not in it draws the `bot`
icon, by the one rule `ROLE_ICON.get(role) ?? 'bot'` (no icon rule keyed on a name pattern). The stems other than
brain, orch and keymaster are either in the map or fall to that bot icon. The claude-code test environment has no fs,
so the directory is read here.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "mods" / "pob-slots" / "hooks" / "kit.tsx"

MAP_RE = re.compile(r"ROLE_ICON = new Map<string, string>\(\[(.*?)\]\)", re.S)
PAIR_RE = re.compile(r"\['([^']+)',\s*'([^']+)'\]")


def _role_map() -> dict[str, str]:
    text = KIT.read_text(encoding="utf-8")
    match = MAP_RE.search(text)
    assert match, "ROLE_ICON map not found in kit.tsx"
    return dict(PAIR_RE.findall(match.group(1)))


def test_role_map_has_the_six_named_roles_with_lowercase_keys():
    role_map = _role_map()
    assert role_map == {
        "worker": "wrench",
        "worker-strong": "hammer",
        "researcher-find": "list",
        "researcher-how": "book-open",
        "reviewer-standards": "search.octicons",
        "reviewer-spec": "eye.octicons",
    }
    assert all(key == key.lower() for key in role_map), "ROLE_ICON keys must be lowercase (case-insensitive lookup)"


def test_the_icon_rule_maps_every_other_role_to_bot_with_no_name_pattern():
    text = KIT.read_text(encoding="utf-8")
    assert "ROLE_ICON.get(role) ?? 'bot'" in text, "an unknown role must draw bot: the icon rule is ROLE_ICON.get(role) ?? 'bot'"
    assert "effort" not in text, "kit.tsx names an effort rule again"
    assert "microscope" not in text, "kit.tsx still carries the microscope icon"
