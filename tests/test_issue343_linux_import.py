"""#343 unit 1 (linux-import): a test module must import on the Linux runner of the GitHub map build.

A test module that reads a Windows-only name at module level (``ctypes.WinDLL`` or ``ctypes.WINFUNCTYPE``
at import, ``import winreg``) fails collection on Linux, before any ``skipif`` mark can act. This module scans
every ``tests/*.py`` with ``ast`` and reports such a use unless a guard comes textually before it: a module-level
``pytest.skip(..., allow_module_level=True)`` in the not-win32 branch of an ``if`` on ``sys.platform``, or a
module-level ``pytest.importorskip("<module>")`` naming the use's own module.

The two tables below are the only names the scan knows. A use inside a ``def`` or ``lambda`` body does not run at
import and is not reported; a name inside a string constant is never an attribute node and is not reported.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent

# Attributes of the ctypes module that exist only on Windows: reading one at module level raises on Linux.
WINDOWS_ONLY_CTYPES_ATTRS = frozenset({
    "WinDLL", "windll", "WINFUNCTYPE", "OleDLL", "oledll",
    "FormatError", "GetLastError", "WinError", "get_last_error",
})
# Modules that exist only on Windows: importing one at module level raises on Linux.
WINDOWS_ONLY_MODULES = frozenset({"winreg", "msvcrt", "_winapi", "_overlapped"})


def _is_sys_platform(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "platform" and isinstance(node.value, ast.Name) \
        and node.value.id == "sys"


def _platform_branches(test: ast.AST) -> tuple[bool, bool]:
    """(the body is the not-win32 branch, the orelse is the not-win32 branch) for an ``if`` on sys.platform.

    ``sys.platform != "win32"`` makes the body the not-win32 branch; ``sys.platform == "win32"`` makes the orelse
    that branch; ``not`` swaps the two. Any other test is not a platform test: neither branch counts."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        body, orelse = _platform_branches(test.operand)
        return orelse, body
    if (isinstance(test, ast.Compare) and len(test.ops) == 1 and _is_sys_platform(test.left)
            and isinstance(test.comparators[0], ast.Constant) and test.comparators[0].value == "win32"):
        if isinstance(test.ops[0], ast.NotEq):
            return True, False
        if isinstance(test.ops[0], ast.Eq):
            return False, True
    return False, False


def _keyword_is_true(call: ast.Call, name: str) -> bool:
    return any(kw.arg == name and isinstance(kw.value, ast.Constant) and kw.value.value is True
               for kw in call.keywords)


def _ctypes_bindings(tree: ast.AST) -> frozenset[str]:
    """Local names bound to the ctypes module: ``ctypes`` itself and every ``import ctypes as <name>``."""
    names = {"ctypes"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "ctypes" and alias.asname:
                    names.add(alias.asname)
    return frozenset(names)


class _ImportTimeScan(ast.NodeVisitor):
    """Walks what runs at import: module level, class bodies, and if/try/with blocks at module level.
    A def or lambda body does not run at import; its decorators and default values do."""

    def __init__(self, ctypes_names: frozenset[str]) -> None:
        self.ctypes_names = ctypes_names
        self.not_win32 = 0  # > 0 inside the not-win32 branch of an if on sys.platform
        self.uses: list[tuple[int, str, str]] = []  # (line, module, what)
        self.platform_guard: int | None = None  # first module-level pytest.skip(..., allow_module_level=True) in that branch
        self.importorskip: dict[str, int] = {}  # module name -> line of its module-level pytest.importorskip

    def _use(self, node: ast.AST, module: str, what: str) -> None:
        self.uses.append((node.lineno, module, what))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root in WINDOWS_ONLY_MODULES:
                self._use(node, root, f"import {alias.name}")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if module.split(".")[0] in WINDOWS_ONLY_MODULES:
            self._use(node, module.split(".")[0], f"from {module} import ...")
        elif module == "ctypes":
            for alias in node.names:
                if alias.name == "*" or alias.name in WINDOWS_ONLY_CTYPES_ATTRS:
                    self._use(node, "ctypes", f"from ctypes import {alias.name}")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (isinstance(node.value, ast.Name) and node.value.id in self.ctypes_names
                and node.attr in WINDOWS_ONLY_CTYPES_ATTRS):
            self._use(node, "ctypes", f"{node.value.id}.{node.attr}")
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        body_is_not_win32, orelse_is_not_win32 = _platform_branches(node.test)
        for stmts, counts in ((node.body, body_is_not_win32), (node.orelse, orelse_is_not_win32)):
            self.not_win32 += counts
            for stmt in stmts:
                self.visit(stmt)
            self.not_win32 -= counts

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "pytest":
            if func.attr == "skip" and self.not_win32 and _keyword_is_true(node, "allow_module_level"):
                if self.platform_guard is None:
                    self.platform_guard = node.lineno
            if (func.attr == "importorskip" and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                self.importorskip.setdefault(node.args[0].value, node.lineno)
        self.generic_visit(node)

    # A def or lambda body never runs at import and records nothing, so it is not walked: only the expressions
    # evaluated when the def itself runs (decorators, defaults) are.
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for expr in [*node.decorator_list, *node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)]:
            self.visit(expr)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for expr in [*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)]:
            self.visit(expr)


def find_offenders(source: str) -> list[str]:
    """One line per module-level Windows-only use with no guard textually before it; empty when the module is clean."""
    tree = ast.parse(source)
    scan = _ImportTimeScan(_ctypes_bindings(tree))
    scan.visit(tree)
    offenders = []
    for line, module, what in scan.uses:
        if scan.platform_guard is not None and scan.platform_guard < line:
            continue
        if module in scan.importorskip and scan.importorskip[module] < line:
            continue
        offenders.append(f"line {line}: {what}")
    return offenders


def scan_tests_dir() -> dict[str, list[str]]:
    """Offenders per tests/*.py file; a file with none is absent."""
    found = {}
    for path in sorted(TESTS_DIR.glob("*.py")):
        offenders = find_offenders(path.read_text(encoding="utf-8"))
        if offenders:
            found[path.name] = offenders
    return found


def test_no_test_module_reads_a_windows_only_name_at_import():
    scanned = {path.name for path in TESTS_DIR.glob("*.py")}
    assert Path(__file__).name in scanned  # the scan saw the real folder, not an empty one
    offenders = scan_tests_dir()
    assert offenders == {}, offenders


@pytest.mark.parametrize("source", [
    "import ctypes\nKERNEL = ctypes.WinDLL('kernel32')\n",
    "import ctypes\nWNDPROC = ctypes.WINFUNCTYPE(None)\n",
    "import winreg\n",
    "from msvcrt import getch\n",
    "from ctypes import windll\n",
    "import ctypes\nif True:\n    DLL = ctypes.windll\n",  # a module-level if is module level
    "import ctypes\nclass Holder:\n    DLL = ctypes.windll\n",  # a class body is module level
    # finding 1: the == "win32" form is not a guard, its skip runs on Windows only
    "import sys\nimport pytest\nimport ctypes\nif sys.platform == 'win32':\n"
    "    pytest.skip('windows', allow_module_level=True)\nKERNEL = ctypes.windll.kernel32\n",
    # finding 2: an aliased ctypes module and a star import are the same module-level read
    "import ctypes as ct\nKERNEL = ct.windll.kernel32\n",
    "from ctypes import *\n",
    # finding 3: an importorskip of another module does not guard winreg
    "import pytest\npytest.importorskip('msvcrt')\nimport winreg\n",
], ids=["windll-call", "winfunctype", "import-winreg", "from-msvcrt", "from-ctypes-windll",
        "in-module-if", "in-class-body", "eq-win32-skip-is-not-a-guard", "alias-ctypes-windll",
        "star-import-ctypes", "importorskip-of-another-module"])
def test_unguarded_module_level_use_is_reported(source):
    assert find_offenders(source)


@pytest.mark.parametrize("source", [
    "import ctypes\ndef load():\n    return ctypes.windll.kernel32\n",
    "import ctypes\nload = lambda: ctypes.windll\n",
    "import ctypes\nSTAND_IN = '''\nctypes.WinDLL('kernel32')\nimport winreg\n'''\n",
    "import sys\nimport pytest\nif sys.platform != 'win32':\n"
    "    pytest.skip('windows only', allow_module_level=True)\n"
    "import ctypes\nKERNEL = ctypes.WinDLL('kernel32')\n",
    "import pytest\npytest.importorskip('winreg')\nimport winreg\n",
    # the equivalent not-win32 branches: the orelse of == "win32", and not (... == "win32")
    "import sys\nimport pytest\nimport ctypes\nif sys.platform == 'win32':\n    KERNEL = None\n"
    "else:\n    pytest.skip('windows only', allow_module_level=True)\nDLL = ctypes.windll\n",
    "import sys\nimport pytest\nimport ctypes\nif not (sys.platform == 'win32'):\n"
    "    pytest.skip('windows only', allow_module_level=True)\nDLL = ctypes.windll\n",
    # an aliased module under the guard
    "import sys\nimport pytest\nimport ctypes as ct\nif sys.platform != 'win32':\n"
    "    pytest.skip('windows only', allow_module_level=True)\nKERNEL = ct.windll.kernel32\n",
], ids=["def-body", "lambda-body", "string-constant", "after-platform-skip", "after-importorskip",
        "orelse-of-eq-win32", "not-eq-win32", "alias-after-platform-skip"])
def test_guarded_or_deferred_use_is_not_reported(source):
    assert find_offenders(source) == []


def test_a_use_before_the_guard_is_still_reported():
    source = ("import ctypes\nimport sys\nimport pytest\nKERNEL = ctypes.WinDLL('kernel32')\n"
              "if sys.platform != 'win32':\n    pytest.skip('windows only', allow_module_level=True)\n")
    assert find_offenders(source) == ["line 4: ctypes.WinDLL"]


def test_a_skip_outside_a_platform_if_is_not_a_guard():
    source = "import pytest\npytest.skip('always', allow_module_level=True)\nimport winreg\n"
    assert find_offenders(source) == ["line 3: import winreg"]
