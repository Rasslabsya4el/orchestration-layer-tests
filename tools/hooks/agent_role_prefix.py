"""Subagent-row role prefix hook (#1145).

A `PreToolUse` hook (matcher `Agent|Task`). The app shows a subagent row by the `description` of its
`Agent` / `Task` call; this hook rewrites that description to `<subagent_type>: <description>` when it
does not already start with `<subagent_type>:`, so every row reads `<role>: <ticket title>` and no
session has to remember it.

The host lets a PreToolUse hook rewrite tool input: `hookSpecificOutput.updatedInput` (with
`permissionDecision: allow`) replaces the call's whole input, so every other field is copied. A call
with no `subagent_type` (or no description) is left as is.

Never raises: any error, bad stdin or unexpected shape exits 0 with no output -- a broken hook must
never block a subagent start.
"""

from __future__ import annotations

import json
import sys


def run(payload: dict) -> dict | None:
    if payload.get("tool_name") not in ("Agent", "Task"):
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    role = tool_input.get("subagent_type")
    description = tool_input.get("description")
    if not isinstance(role, str) or not role or not isinstance(description, str) or not description.strip():
        return None
    if description.lstrip().lower().startswith(f"{role.lower()}:"):
        return None
    updated = dict(tool_input)
    updated["description"] = f"{role}: {description}"
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "subagent row reads <role>: <title> (#1145)",
            "updatedInput": updated,
        }
    }


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            return 0
        output = run(payload)
        if output is not None:
            print(json.dumps(output))
    except Exception as exc:  # fail open: never block a subagent start on an internal error
        print(f"agent_role_prefix: internal error, failing open: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
