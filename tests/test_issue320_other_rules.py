"""Issue #320 unit 3: every other hook declares the role(s) its rules bind, in ``RULE_ROLES``.

Seams: each hook module's module-level ``RULE_ROLES`` (read through importlib) and ``subagent_skill_check.run``
(the decision the gate by ``agent_type`` makes). The table below is the declaration as agreed in the issue; the
expected names and roles come from it, not from the hook code.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from tools.hooks import session_role

HOOKS_DIR = Path(__file__).resolve().parents[1] / "tools" / "hooks"
# worker_context_line imports its sibling brain_reset_nudge by its bare name, so the hooks dir must be importable.
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

RED_LINES = tuple(f"red_line_{n}" for n in range(1, 8))

#: module -> (rule name -> roles), the declaration this unit makes
EXPECTED: dict[str, dict[str, tuple[str, ...]]] = {
    "tracker_guard": {
        # fires only for a session that is not the brain (run() reads _not_brain)
        "label_refusal": ("orch", "worker", "reviewer", "researcher", "unblocker"),
        "parked_in_title": ("any",),
        "ticket_shape": ("any",),
        "shared_temp_body": ("any",),
        "issue_create_area": ("any",),
    },
    "permission_guard": {name: ("any",) for name in RED_LINES},
    "agent_role_prefix": {"subagent_row_prefix": ("any",)},
    # acts on unattended (--bg) sessions by its own check (is_background), so it stays "any"
    "refuse_permission_prompt": {"refusal": ("any",)},
    "state_claim_check": {"claim_without_check": ("brain",)},
    "autopilot_checkpoint": {"session_start_context": ("brain",)},
    "autopilot_commit_guard": {"commit_refusal": ("brain",)},
    "subagent_skill_check": {"skill_check": ("worker",)},
    "worker_git_guard": {"stash_refusal": ("worker",)},
    "worker_process_guard": {"process_start_refusal": ("worker",)},
    "worker_context_line": {"context_line_hand_back": ("worker",)},
}


def _rule_roles(module: str) -> dict:
    return importlib.import_module(f"tools.hooks.{module}").RULE_ROLES


@pytest.mark.parametrize("module", sorted(EXPECTED))
def test_rule_roles_is_a_non_empty_dict_of_known_roles(module):
    rules = _rule_roles(module)
    assert isinstance(rules, dict) and rules, f"{module}: RULE_ROLES must be a non-empty dict"
    for name, roles in rules.items():
        assert isinstance(name, str) and name, f"{module}: rule name {name!r} must be a non-empty string"
        assert isinstance(roles, tuple) and roles, f"{module}.{name}: roles must be a non-empty tuple"
        for role in roles:
            assert role in session_role.ROLES, f"{module}.{name}: {role!r} is not in ROLES"


@pytest.mark.parametrize("module", sorted(EXPECTED))
def test_rule_names_and_roles_match_the_declaration(module):
    assert _rule_roles(module) == EXPECTED[module]


def test_subagent_skill_check_still_does_not_act_for_a_non_worker_agent_type(tmp_path):
    # the same gate as tests/test_issue104_subagent_skill_check.py: a non-worker agent_type is never read
    hook = importlib.import_module("tools.hooks.subagent_skill_check")
    missing = str(tmp_path / "never-created.jsonl")
    for agent_type in ("orch", "brain", "reviewer", "researcher", "unblocker"):
        payload = {"agent_type": agent_type, "agent_transcript_path": missing, "agent_id": "a-320"}
        assert hook.run(payload) is None, agent_type
