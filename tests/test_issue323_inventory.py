"""Every core module that makes an issue call reaches the `tickets_repo` setting, or is listed here with a reason.

A static scan (ast) over the non-test sources under tools/ and scripts/. A module is an issue-call site when a list or
tuple holds the string "issue" or "issues" (a gh argv), or a string outside a docstring holds an issues API path, a
`gh issue` command or a GraphQL issue field. A site reaches the setting when it uses `tickets_repo`: the name, an alias
imported from core_settings, the attribute, or the key string (an import alone is not a use). A site that does not
reach it must be in EXEMPT (not an issue call of this installation, reason required) or in INJECTED (its repo is a
parameter that the named caller fills in from the setting; that caller must reach it).
"""

from __future__ import annotations

import ast
import functools
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("tools", "scripts")

API_PATH = re.compile(
    r"repos/[^\s\"']*/issues|search issues|\bgh issue\b|\bissues?\((?:first|number|states|after|orderBy)\b")

#: a site that makes no issue call of this installation, or whose issues are the core's own home repo's (settings `repo`).
EXEMPT = {
    "tools/agentops/core_issue.py": "the core's home repo (settings `repo`): core issues are filed there, not in the tickets repo",
    "tools/agentops/core_update.py": "updates the core in a target from the home repo (settings `repo`), not the tickets repo",
    "tools/agentops/provisional.py": "its cross-repo search has no --repo on purpose; its other calls reach the setting",
    "scripts/transfer_issues.py": "moves issues between the repos named on its command line (--from-repo, --to-repo, --repo)",
    "scripts/counts/199/flow_fix_missed.py": "one-off count of the core's own core-issue tickets: repo is the settings `repo`",
    "tools/hooks/permission_guard.py": "classifier only: judges the text of a gh command against the red lines, makes no call",
    "tools/agentops/monitor_pipeline.py": "its repo-taking functions have no production caller (tests only); a future caller must pass tickets_repo",
    "tools/agentops/guard.py": "a list of permission deny patterns (text such as `Bash(gh issue delete*)`), makes no call",
    "scripts/counts/126/q5_brain_actions.py": "classifies transcript lines by regex (text of a gh command), makes no gh call",
    "tools/replays/gh_stub.py": "the stand-in for gh in a replay run: answers from recorded files, makes no call, and writes nothing to any tracker (#349)",
    "tools/replays/c_seal.py": "the sealing check runs `gh issue view` inside a sealed clone to prove the stub answers nothing; it must not name a repo (#376)",
    "tools/replays/c_tickets.py": "reads the edit history of a ticket of the repo it is given (default: the repo of the closed tickets being replayed), not a tracker write (#376)",
    "tools/replays/launcher.py": "the replay harness names the case repos itself (-R <case repo>, never the tickets repo) and only counts their issues (#349)",
}

#: a site whose repo is a parameter, mapped to the caller that passes it the setting (the caller must reach it).
INJECTED = {
    "tools/agentops/streams.py": "tools/agentops/dispatch.py",
    "tools/agentops/autopilot.py": "tools/agentops/dispatch.py",
    "tools/agentops/parked_return.py": "tools/agentops/dispatch.py",
}


def issue_lines(tree: ast.AST) -> list[int]:
    """The lines of the issue-call markers in one parsed module (docstrings never count)."""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            docstrings.add(id(node.value))
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            for elt in node.elts:
                if isinstance(elt, ast.Constant) and elt.value in ("issue", "issues"):
                    lines.add(elt.lineno)
        if isinstance(node, (ast.Constant, ast.JoinedStr)) and id(node) not in docstrings:
            if API_PATH.search(_text(node)):
                lines.add(node.lineno)
    return sorted(lines)


def _text(node: ast.AST) -> str:
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else ""
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value for part in node.values
                       if isinstance(part, ast.Constant) and isinstance(part.value, str))
    return ""


def tickets_uses(tree: ast.AST) -> int:
    """How many times the module uses the tickets setting (a name, an alias, an attribute or the key string)."""
    names = {"tickets_repo"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == "core_settings":
            for alias in node.names:
                if alias.name == "tickets_repo":
                    names.add(alias.asname or alias.name)
    uses = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in names:
            uses += 1
        elif isinstance(node, ast.Attribute) and node.attr == "tickets_repo":
            uses += 1
        elif isinstance(node, ast.Constant) and node.value == "tickets_repo":
            uses += 1
    return uses


def sources():
    """(posix path relative to the repo, file) for every non-test .py under the scanned folders."""
    for top in SCAN_DIRS:
        for path in sorted((ROOT / top).rglob("*.py")):
            if "__pycache__" in path.parts or path.name.startswith("test_") or "tests" in path.parts:
                continue
            yield path.relative_to(ROOT).as_posix(), path


@functools.lru_cache(maxsize=None)
def survey() -> tuple[int, dict[str, dict]]:
    """(files scanned, {path: {"lines": issue-call lines, "uses": tickets uses}} for the issue-call sites only)."""
    scanned = 0
    sites: dict[str, dict] = {}
    for rel, path in sources():
        scanned += 1
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        lines = issue_lines(tree)
        if lines:
            sites[rel] = {"lines": lines, "uses": tickets_uses(tree)}
    return scanned, sites


def verdict(sites: dict[str, dict]) -> tuple[list[str], list[str], list[str], list[str]]:
    """(reaching, exempt, injected, uncovered): a site that does not reach is exempt, injected, or uncovered."""
    reaching = sorted(rel for rel, s in sites.items() if s["uses"])
    silent = [rel for rel, s in sites.items() if not s["uses"]]
    exempt = sorted(rel for rel in silent if rel in EXEMPT)
    injected = sorted(rel for rel in silent if rel in INJECTED)
    uncovered = sorted(rel for rel in silent if rel not in EXEMPT and rel not in INJECTED)
    return reaching, exempt, injected, uncovered


def test_every_issue_call_site_reaches_the_tickets_repo_or_is_exempt():
    scanned, sites = survey()
    reaching, exempt, injected, uncovered = verdict(sites)
    assert scanned >= 50 and "tools/agentops/close_issue.py" in sites, "the scan did not see the core's sources"
    summary = (f"scanned {scanned} files, {len(sites)} issue-call sites: {len(reaching)} reach the setting, "
               f"{len(exempt)} exempt, {len(injected)} injected, {len(uncovered)} uncovered")
    listing = "\n".join(
        [summary, "exempt:"] + [f"  {rel}: {EXEMPT[rel]}" for rel in exempt]
        + ["injected:"] + [f"  {rel} <- {INJECTED[rel]}" for rel in injected]
        + ["uncovered:"] + [f"  {rel} (issue-call lines {sites[rel]['lines']})" for rel in uncovered])
    assert uncovered == [], listing


def test_exempt_and_injected_entries_name_a_flagged_site_and_a_reason():
    _, sites = survey()
    for rel, reason in EXEMPT.items():
        assert reason.strip(), f"EXEMPT has no reason for {rel}"
        assert rel in sites, f"EXEMPT names a file with no issue call (stale entry): {rel}"
    for rel, caller in INJECTED.items():
        assert rel in sites, f"INJECTED names a file with no issue call (stale entry): {rel}"
        assert sites.get(caller, {}).get("uses", 0) > 0, f"the caller {caller} of {rel} does not reach the setting"


def test_scanner_sees_argv_and_api_calls_but_not_docstrings_and_counts_uses_not_imports():
    bare = 'def f():\n    """gh issue list in prose"""\n    run(["issue", "list"])\n'
    assert issue_lines(ast.parse(bare)) == [3]
    assert tickets_uses(ast.parse(bare)) == 0
    api = 'path = f"repos/{repo}/issues/{number}/timeline"\n'
    assert issue_lines(ast.parse(api)) == [1]
    aliased = 'from tools.agentops.core_settings import tickets_repo as tr\nrun(["issue", "list", "-R", tr()])\n'
    assert tickets_uses(ast.parse(aliased)) >= 1
    imported_only = 'from tools.agentops.core_settings import tickets_repo\nrun(["issue", "list"])\n'
    assert tickets_uses(ast.parse(imported_only)) == 0
