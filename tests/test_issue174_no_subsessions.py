"""Issue #174: the two opencode agents `build_config` hands out turn the sub-session tool (`task`) off,
whatever the repo's own `permission` says.

Measured on opencode 1.18.31 (`opencode debug agent agentops`, no model call): `permission.task: deny`
takes effect at the top level (placed last, it beats a repo's `"*": "allow"`) and at the agent level;
a top-level `tools.task: false` does not (its deny rule sits before the repo's `"*": "allow"`, so the
allow wins and `task` stays on). The key used here is therefore `permission.task`.
"""

from __future__ import annotations

from tools.agentops import ocserver

AGENTS = ("agentops", "agentops-research")


def test_base_allowing_everything_still_denies_the_subsession_tool_in_both_agents():
    cfg = ocserver.build_config(base={"permission": {"*": "allow"}, "provider": {}})

    assert cfg["permission"]["task"] == "deny"
    for name in AGENTS:
        assert cfg["agent"][name]["permission"]["task"] == "deny", name


def test_repo_that_names_task_allow_is_overridden():
    cfg = ocserver.build_config(base={"permission": {"task": "allow", "*": "allow"}, "provider": {}})

    assert cfg["permission"]["task"] == "deny"
    # opencode reads permission rules in key order and the last match wins: the deny must come after "*"
    assert list(cfg["permission"]).index("task") > list(cfg["permission"]).index("*")
    for name in AGENTS:
        assert cfg["agent"][name]["permission"]["task"] == "deny", name


def test_other_permissions_are_unchanged_by_the_subsession_deny():
    cfg = ocserver.build_config(base={"permission": {"*": "allow", "webfetch": "allow"}, "provider": {}})

    assert cfg["permission"]["webfetch"] == "allow"
    assert cfg["permission"]["question"] == "allow"
    assert cfg["permission"]["external_directory"] == "deny"
    assert cfg["agent"]["agentops"]["permission"] == {"webfetch": "deny", "task": "deny"}
    assert cfg["agent"]["agentops-research"]["permission"] == {"webfetch": "allow", "task": "deny"}
