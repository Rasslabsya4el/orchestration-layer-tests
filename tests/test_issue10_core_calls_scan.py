"""#10 row 3: no core module of `tools/agentops/` calls a `Dispatcher` method that only a local module
registers, unless the call sits behind the `_autopilot_module_loaded` guard (`dispatch.py`).

The scan reads the core files named by `core_manifest.txt` (minus the local-module files) with `ast`,
so the next unguarded call fails this suite instead of an AttributeError in a live dispatcher. The
synthetic sources prove the scanner can fail: each one is judged by the same rule.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tools.agentops.autopilot import DISPATCHER_METHODS

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tools" / "agentops" / "core_manifest.txt"

#: the local-module files of tools/agentops/, which are listed in the manifest but are not core.
LOCAL_MODULE_FILES = frozenset({
    "tools/agentops/autopilot.py",
    "tools/agentops/provisional.py",
    "tools/agentops/mods.py",
})

#: the Dispatcher methods only a local module registers: `autopilot.DISPATCHER_METHODS`. `provisional.py`
#: and `mods.py` register commands only (`core.command`), no Dispatcher method.
LOCAL_ONLY_METHODS = frozenset(DISPATCHER_METHODS)

#: the receivers a Dispatcher method is called through in the core: `self` inside Dispatcher, `dispatcher` elsewhere.
RECEIVERS = frozenset({"self", "dispatcher"})

#: the helper that makes an autopilot call safe: a call is guarded when its own function calls it on an earlier line.
GUARD = "_autopilot_module_loaded"

_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def core_scan_files() -> list[Path]:
    """The manifest's tools/agentops/*.py entries, minus the local-module files."""

    listed = [line.strip() for line in MANIFEST.read_text(encoding="utf-8").splitlines()]
    return [ROOT / rel for rel in listed
            if rel.startswith("tools/agentops/") and rel.endswith(".py") and rel not in LOCAL_MODULE_FILES]


def _call_name(node: ast.AST, names: frozenset[str]) -> str | None:
    """The method name when `node` is `self.<name>(...)` or `dispatcher.<name>(...)` with a name in `names`."""

    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    receiver = node.func.value
    if isinstance(receiver, ast.Name) and receiver.id in RECEIVERS and node.func.attr in names:
        return node.func.attr
    return None


def _own_nodes(scope: ast.AST):
    """Every node of `scope` except those inside a nested function, lambda or class."""

    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _SCOPE_NODES):
            stack.extend(ast.iter_child_nodes(node))


def _scopes(tree: ast.Module):
    yield tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def calls_to(source: str, names: frozenset[str], filename: str = "<source>") -> list[tuple[int, str]]:
    """Every call of a name in `names` through a receiver in `RECEIVERS`, as sorted (line, name)."""

    tree = ast.parse(source, filename=filename)
    found = []
    for scope in _scopes(tree):
        for node in _own_nodes(scope):
            name = _call_name(node, names)
            if name is not None:
                found.append((node.lineno, name))
    return sorted(found)


def find_unguarded_calls(source: str, names: frozenset[str] = LOCAL_ONLY_METHODS,
                         filename: str = "<source>") -> list[tuple[int, str]]:
    """The calls of `names` that no `GUARD` call precedes in their own function, as sorted (line, name).
    The rule: a call is guarded when a `.GUARD()` call sits on an earlier line of the same innermost function
    (or of the module, for a call outside any function). A guard in another method does not cover a call."""

    tree = ast.parse(source, filename=filename)
    found = []
    for scope in _scopes(tree):
        own = list(_own_nodes(scope))
        guard_lines = [node.lineno for node in own
                       if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                       and node.func.attr == GUARD]
        for node in own:
            name = _call_name(node, names)
            if name is not None and not any(line < node.lineno for line in guard_lines):
                found.append((node.lineno, name))
    return sorted(found)


# -- the synthetic sources: the scanner must be able to fail -----------------------------------------------

UNGUARDED_SELF = """\
class Dispatcher:
    def stretch(self, goal):
        return self._read_terms(goal)
"""

UNGUARDED_DISPATCHER = """\
def tick(dispatcher, goal):
    return dispatcher._deadline_reached(goal, None)
"""

GUARDED_BEFORE = """\
class Dispatcher:
    def stretch(self, goal):
        if not self._autopilot_module_loaded():
            return False
        return self._read_terms(goal)
"""

GUARD_AFTER = """\
class Dispatcher:
    def stretch(self, goal):
        terms = self._read_terms(goal)
        if not self._autopilot_module_loaded():
            return False
        return terms
"""

GUARD_IN_OTHER_METHOD = """\
class Dispatcher:
    def guard(self):
        return self._autopilot_module_loaded()
    def stretch(self, goal):
        return self._read_terms(goal)
"""

NOT_A_LOCAL_ONLY_NAME = """\
class Dispatcher:
    def stretch(self, goal):
        return self._autopilot_stretch_open(goal, None)
"""


def test_synthetic_unguarded_call_is_reported():
    assert find_unguarded_calls(UNGUARDED_SELF) == [(3, "_read_terms")]


@pytest.mark.parametrize("source, expected", [
    (UNGUARDED_DISPATCHER, [(2, "_deadline_reached")]),
    (GUARDED_BEFORE, []),
    (GUARD_AFTER, [(3, "_read_terms")]),
    (GUARD_IN_OTHER_METHOD, [(5, "_read_terms")]),
    (NOT_A_LOCAL_ONLY_NAME, []),
], ids=["unguarded-dispatcher-receiver", "guard-before-call", "guard-after-call",
        "guard-in-other-method", "name-not-local-only"])
def test_synthetic_sources_are_judged_by_the_guard_rule(source, expected):
    assert find_unguarded_calls(source) == expected


# -- the real core tree ------------------------------------------------------------------------------------

def test_scan_set_is_the_core_manifest_minus_the_local_module_files():
    rels = {path.relative_to(ROOT).as_posix() for path in core_scan_files()}

    assert "tools/agentops/dispatch.py" in rels
    assert "tools/agentops/cli.py" in rels
    assert rels.isdisjoint(LOCAL_MODULE_FILES)


def test_scan_sees_the_guarded_autopilot_calls_in_dispatch():
    """Not vacuous: the scanner reaches the two real autopilot-only calls in dispatch.py (guarded, #10)."""
    dispatch = (ROOT / "tools" / "agentops" / "dispatch.py").read_text(encoding="utf-8")

    names = {name for _, name in calls_to(dispatch, LOCAL_ONLY_METHODS)}

    assert {"_read_terms", "_deadline_reached"} <= names


def test_no_core_module_calls_a_local_only_dispatcher_method_unguarded():
    found = []
    for path in core_scan_files():
        source = path.read_text(encoding="utf-8")
        for line, name in find_unguarded_calls(source, filename=str(path)):
            found.append(f"{path.relative_to(ROOT).as_posix()}:{line} {name}")

    assert found == []
