"""The foreign core tests of a target folder: its test files that import a core module of the home manifest.

    python -m tools.agentops.core_tests <folder> [<folder> ...]

Prints, per folder, ``<folder>: a=<n> b=<m>`` and then one line per file, ``a <name>`` or ``b <name>``: the a group
first, each group sorted. Exit 0 always.

A test file counts when it is a top-level ``tests/test_*.py`` of the folder, its own path is not a manifest
line, and it imports (anywhere in the file, AST) a module the manifest lists under ``tools/agentops/`` or
``tools/hooks/``: ``from pkg import mod``, ``from pkg.mod import name``, ``import pkg.mod``, with the alias forms.
A file that does not parse is not judged. ``poe_only`` keeps the foreign tests with no same-named file in the
home's tests folder.

The split (``split_core_tests``) tells the foreign tests apart by what else they touch of the target's own:
``a`` touches only manifest modules; ``b`` also touches something of the target's own, that is an absolute import
of a ``.py`` file or package inside the folder or its ``src/``, ``scripts/`` or ``tests/`` root that no manifest line covers, an import of a helper module in
``tests/`` (``tests/x.py`` or ``tests/x/__init__.py``, named by its first segment), or a string constant that names
an existing file under the folder which is no manifest line and no top-level ``tests/test_*.py``. ``a + b`` is
``foreign_core_tests``.
"""

from __future__ import annotations

import ast
import fnmatch
import sys
from pathlib import Path, PurePosixPath

from tools.agentops.core_check import MANIFEST_PATH, read_manifest

CORE_PREFIXES = ("tools/agentops/", "tools/hooks/")
CORE_PACKAGES = ("tools.agentops", "tools.hooks")


def _core_modules(entries: list[str]) -> set[str]:
    """Dotted names of the manifest's ``.py`` modules under the core packages (package ``__init__`` files excluded)."""
    modules: set[str] = set()
    for entry in entries:
        if not entry.endswith(".py") or not entry.startswith(CORE_PREFIXES):
            continue
        dotted = entry[: -len(".py")].replace("/", ".")
        if dotted.rsplit(".", 1)[-1] == "__init__":
            continue
        modules.add(dotted)
    return modules


def _imports_core(tree: ast.AST, modules: set[str]) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name in modules for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module in modules:
                return True
            if node.module in CORE_PACKAGES and any(
                f"{node.module}.{alias.name}" in modules for alias in node.names
            ):
                return True
    return False


def _parse(path: Path) -> ast.AST | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    except SyntaxError:
        return None


def foreign_core_tests(folder: Path | str, manifest_path: Path | None = None) -> list[str]:
    """Sorted file names of ``<folder>/tests/test_*.py`` that import a manifest core module and are not manifest files."""
    folder = Path(folder)
    tests = folder / "tests"
    if not tests.is_dir():
        return []
    entries = read_manifest(manifest_path or MANIFEST_PATH)
    listed = set(entries)
    modules = _core_modules(entries)
    names: list[str] = []
    for path in tests.glob("test_*.py"):
        if not path.is_file() or f"tests/{path.name}" in listed:
            continue
        tree = _parse(path)
        if tree is not None and _imports_core(tree, modules):
            names.append(path.name)
    return sorted(names)


def poe_only(folder: Path | str, home_tests_dir: Path | str, manifest_path: Path | None = None) -> list[str]:
    """The foreign core tests of ``folder`` with no same-named file in the home's tests folder, sorted."""
    home = Path(home_tests_dir)
    return [name for name in foreign_core_tests(folder, manifest_path) if not (home / name).is_file()]


def _absolute_names(tree: ast.AST) -> set[str]:
    """Every absolute dotted name the file imports: each module, and ``pkg.name`` for each from-import alias."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _prefixes(dotted: str) -> list[str]:
    parts = dotted.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts) + 1)]


OWN_ROOTS = ("", "src", "scripts", "tests")


def _present(folder: Path, rel: str, absent: frozenset[str] | set[str]) -> bool:
    """True when ``folder/rel`` is a file, except a top-level ``tests/<name>`` whose name is in ``absent``."""
    parts = PurePosixPath(rel).parts
    if absent and len(parts) == 2 and parts[0] == "tests" and parts[1] in absent:
        return False
    return (folder / rel).is_file()


def _own_module(folder: Path, dotted: str, listed: set[str], absent: frozenset[str] | set[str] = frozenset()) -> bool:
    """True when ``dotted`` is a ``.py`` file, or a package with ``__init__.py``, under one of ``OWN_ROOTS`` of folder, that no manifest line covers.

    The roots are ``<folder>``, ``<folder>/src``, ``<folder>/scripts`` and ``<folder>/tests``; a hit under any of them
    that a manifest line covers is core and wins over the others.
    A package holding a manifest line is core, not the target's own, whether or not its ``__init__.py`` is listed.
    """
    rel = dotted.replace(".", "/")
    own = False
    for root in OWN_ROOTS:
        base = f"{root}/{rel}" if root else rel
        if _present(folder, base + ".py", absent):
            if base + ".py" in listed:
                return False
            own = True
        elif _present(folder, base + "/__init__.py", absent):
            if any(entry.startswith(base + "/") for entry in listed):
                return False
            own = True
    return own


def _own_helper(folder: Path, names: set[str], absent: frozenset[str] | set[str] = frozenset()) -> bool:
    """True when an import names a helper module of ``<folder>/tests/`` by its first segment."""
    for name in names:
        first = name.split(".")[0]
        if _present(folder, f"tests/{first}.py", absent) or _present(folder, f"tests/{first}/__init__.py", absent):
            return True
    return False


def _string_constants(tree: ast.AST) -> set[str]:
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _own_file_string(folder: Path, text: str, listed: set[str]) -> bool:
    """True when a string constant is a relative posix path to an existing file of the folder that is the target's own."""
    if not text or "\n" in text or "\x00" in text or "\\" in text or ":" in text:
        return False
    posix = PurePosixPath(text)
    if posix.is_absolute() or ".." in posix.parts or not posix.parts:
        return False
    rel = posix.as_posix()
    if rel in listed:
        return False
    if len(posix.parts) == 2 and posix.parts[0] == "tests" and fnmatch.fnmatchcase(posix.parts[1], "test_*.py"):
        return False
    try:
        return (folder / rel).is_file()
    except (OSError, ValueError):
        return False


def _touches_own(tree: ast.AST, folder: Path, listed: set[str], absent: frozenset[str] | set[str] = frozenset()) -> bool:
    names = _absolute_names(tree)
    for name in names:
        if any(_own_module(folder, prefix, listed, absent) for prefix in _prefixes(name)):
            return True
    if _own_helper(folder, names, absent):
        return True
    return any(_own_file_string(folder, text, listed) for text in _string_constants(tree))


def split_core_tests(folder: Path | str, manifest_path: Path | None = None,
                     absent: frozenset[str] | set[str] = frozenset()) -> tuple[list[str], list[str]]:
    """The foreign core tests of ``folder`` as ``(a, b)``: a touches only manifest modules, b also touches the target's own.

    Each list is sorted; ``a + b`` equals ``foreign_core_tests(folder, manifest_path)``. ``absent`` names top-level
    ``tests/`` files (``test_x.py``) to treat as not existing when judging what a file touches (#73 removal fixed
    point): an importer of a helper that is being removed no longer touches the target's own through it.
    """
    folder = Path(folder)
    names = foreign_core_tests(folder, manifest_path)
    if not names:
        return [], []
    listed = set(read_manifest(manifest_path or MANIFEST_PATH))
    only_core: list[str] = []
    own: list[str] = []
    for name in names:
        tree = _parse(folder / "tests" / name)
        if tree is None:
            continue
        if _touches_own(tree, folder, listed, absent):
            own.append(name)
        else:
            only_core.append(name)
    return sorted(only_core), sorted(own)


def imported_test_modules(folder: Path | str) -> dict[str, set[str]]:
    """``{file name: stems of the top-level tests/ modules it imports}`` for every ``*.py`` directly in ``<folder>/tests/``.

    A module counts by ``tests.<stem>`` or by a bare ``<stem>`` first segment (a helper next to the test); the stem is
    returned whether or not that module exists.
    """
    result: dict[str, set[str]] = {}
    tests = Path(folder) / "tests"
    if not tests.is_dir():
        return result
    for path in tests.glob("*.py"):
        tree = _parse(path)
        if tree is None:
            continue
        stems: set[str] = set()
        for name in _absolute_names(tree):
            parts = name.split(".")
            stems.add(parts[0])
            if parts[0] == "tests" and len(parts) > 1:
                stems.add(parts[1])
        result[path.name] = stems
    return result


def main(argv: list[str] | None = None) -> int:
    folders = sys.argv[1:] if argv is None else argv
    for folder in folders:
        only_core, own = split_core_tests(folder)
        print(f"{folder}: a={len(only_core)} b={len(own)}")
        for name in only_core:
            print(f"a {name}")
        for name in own:
            print(f"b {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
