"""#320 unit 4: every rule of every hook names the role(s) it binds, and the count of rules without one is 0.

Seams: the hook wiring sources (``.claude/settings.json``, ``.claude/agents/worker.md``, ``worker-strong.md``,
``tools/agentops/autopilot_settings.json``), read as text for the ``tools/hooks/<name>.py`` commands they carry, and
each hook module's module-level ``RULE_ROLES`` (read through importlib, as ``tests/test_issue320_other_rules.py``
does). ``rules_without_role`` is the counting function; the real hooks and the negative check both go through it.
"""

from __future__ import annotations

import importlib
import re
import sys
import types
from pathlib import Path

from tools.hooks.session_role import ROLES

ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / "tools" / "hooks"
# worker_context_line imports its sibling brain_reset_nudge by its bare name, so the hooks dir must be importable.
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

WIRING_SOURCES = (
    ROOT / ".claude" / "settings.json",
    ROOT / ".claude" / "agents" / "worker.md",
    ROOT / ".claude" / "agents" / "worker-strong.md",
    ROOT / "tools" / "agentops" / "autopilot_settings.json",
)
HOOK_COMMAND = re.compile(r"tools/hooks/([A-Za-z_][A-Za-z0-9_]*)\.py")
#: the helper files of tools/hooks: imported by the hooks, never wired as a command, so not counted as hooks
# state_claim_check: listed wired or not; its Stop wiring is off for the owner's trial and restorable (docs/agents/brain.md restore line)
HELPERS = frozenset({"__init__", "session_role", "transcript", "shell_words", "state_claim_check"})


def wired_hooks() -> set[str]:
    """Hook names (file stems) that a wiring source runs as a command."""
    names: set[str] = set()
    for path in WIRING_SOURCES:
        names.update(HOOK_COMMAND.findall(path.read_text(encoding="utf-8")))
    return names


def hook_files() -> set[str]:
    """Every file of tools/hooks, by stem."""
    return {path.stem for path in HOOKS_DIR.glob("*.py")}


def table_of(module: object) -> dict | None:
    """A module's ``RULE_ROLES``; None when it has none."""
    return getattr(module, "RULE_ROLES", None)


def rules_without_role(declared: dict[str, dict | None]) -> list[str]:
    """``module:rule`` for each rule without a role. A module with no table, or an empty one, is ``module:(no RULE_ROLES)``.
    A rule counts as without a role when its roles is not a non-empty tuple or holds a name outside ``ROLES``."""
    missing: list[str] = []
    for module, table in declared.items():
        if not table:
            missing.append(f"{module}:(no RULE_ROLES)")
            continue
        for rule, roles in table.items():
            if not isinstance(roles, tuple) or not roles or any(role not in ROLES for role in roles):
                missing.append(f"{module}:{rule}")
    return missing


def rule_pairs(declared: dict[str, dict | None]) -> list[tuple[str, str]]:
    """Every (module, rule) pair declared."""
    return [(module, rule) for module, table in declared.items() if table for rule in table]


def _real_hook_tables() -> dict[str, dict | None]:
    return {
        name: table_of(importlib.import_module(f"tools.hooks.{name}")) for name in sorted(wired_hooks())
    }


def test_every_wired_hook_is_a_file_of_the_hooks_folder():
    missing = sorted(wired_hooks() - hook_files())
    assert not missing, f"wired hooks with no file in tools/hooks: {missing}"


def test_every_hook_file_is_either_wired_or_a_listed_helper():
    unexplained = sorted(hook_files() - HELPERS - wired_hooks())
    assert not unexplained, f"tools/hooks files neither wired nor listed as helpers: {unexplained}"


def test_every_wired_hook_is_counted_in_the_discovery():
    tables = _real_hook_tables()
    assert set(tables) == wired_hooks()
    assert hook_files() - HELPERS <= wired_hooks()  # a listed helper may also be wired


def test_count_of_rules_without_a_role_is_zero():
    missing = rules_without_role(_real_hook_tables())
    assert missing == [], f"rules without a role (module:rule): {missing}"


def test_rule_count_is_positive_and_equals_the_sum_over_modules():
    tables = _real_hook_tables()
    total = sum(len(table) for table in tables.values() if table)
    pairs = rule_pairs(tables)
    print(f"\nhook rules declared: {total} across {len(tables)} hook modules")
    assert total > 0
    assert len(pairs) == total


def test_negative_check_reports_exactly_the_three_defects():
    def fake(name: str, table=None) -> types.ModuleType:
        module = types.ModuleType(name)
        if table is not None:
            module.RULE_ROLES = table
        return module

    synthetic = {
        "mod_no_table": fake("mod_no_table"),
        "mod_empty_roles": fake("mod_empty_roles", {"x": ()}),
        "mod_unknown_role": fake("mod_unknown_role", {"y": ("chef",)}),
        "mod_ok": fake("mod_ok", {"z": ("any",)}),
    }
    declared = {name: table_of(module) for name, module in synthetic.items()}
    assert rules_without_role(declared) == [
        "mod_no_table:(no RULE_ROLES)",
        "mod_empty_roles:x",
        "mod_unknown_role:y",
    ]
