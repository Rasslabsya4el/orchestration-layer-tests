"""Rule C of the test selection: the tests of a ``.py`` file the test map has no row for = the tests of its referrers at ``head``.

A referrer is a tracked ``.py`` file under tests/ src/ scripts/ tools/ that (a) imports the file statically
(``ast``: function-level and relative imports included) or (b) carries the file's name with its extension
inside a string literal that is not a docstring (never a comment, never a docstring). A test file referrer
runs whole; a referrer with rows in the map gives its mapped tests; any other referrer is expanded the
same way (visited set).
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import PurePosixPath

from .util import no_window_flags

ROOTS = ("tests/", "src/", "scripts/", "tools/")
_IMPORT_SPAN = re.compile(r"(?m)^[ 	]*(?:from|import)\b(?:[^\n(]*|[^\n]*\([^)]*\))")
_REAL_IMPORT = re.compile(r"(?m)^[ 	]*(?:from|import)\b[^\n]*")
TEST_FILE = re.compile(r"tests/test_[^/]*\.py")


def _tracked_sources(root: str, head: str) -> dict[str, str]:
    """``{path: text}`` of every ``.py`` under ROOTS at ``head`` (one ``git ls-tree`` + one ``cat-file --batch``)."""
    ls = subprocess.run(["git", "-C", root, "ls-tree", "-r", head, "--", *[r.rstrip("/") for r in ROOTS]],
                        capture_output=True, text=True, check=True, encoding="utf-8",
                        creationflags=no_window_flags()).stdout
    entries = []
    for line in ls.splitlines():
        meta, path = line.split("\t", 1)
        if path.endswith(".py"):
            entries.append((path, meta.split()[2]))
    proc = subprocess.run(["git", "-C", root, "cat-file", "--batch"], input="".join(f"{s}\n" for _, s in entries).encode(),
                          capture_output=True, check=True, creationflags=no_window_flags())
    out, pos, res = proc.stdout, 0, {}
    for path, _ in entries:
        nl = out.index(b"\n", pos)
        size = int(out[pos:nl].split()[2])
        res[path] = out[nl + 1:nl + 1 + size].decode("utf-8", "replace")
        pos = nl + 1 + size + 1
    return res


def _dotted(path: str) -> str:
    """Module name of a file by its place: ``src/`` is a source root, everything else is named from the repo root."""
    p = PurePosixPath(path)
    parts = list(p.with_suffix("").parts)
    if parts[0] == "src":
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _stem(path: str) -> str:
    p = PurePosixPath(path)
    return p.parent.name if p.stem == "__init__" else p.stem


def _names(path: str, by_name: dict[str, str]) -> set[str]:
    """Every dotted name an import statement may use for ``path`` (a bare name only for a top file of
    scripts/ or tools/, and only when no src package or root-dotted file already owns that name)."""
    names = {_dotted(path)}
    p = PurePosixPath(path)
    if p.parts[0] in ("scripts", "tools") and len(p.parts) == 2 and p.stem != "__init__" and p.stem not in by_name:
        names.add(p.stem)
    return names


def _imports(path: str, tree: ast.AST) -> set[str]:
    """Absolute dotted names a module imports, each name plus its ``from x import y`` extension."""
    pkg = _dotted(path).split(".")
    if PurePosixPath(path).stem != "__init__":
        pkg = pkg[:-1]
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg[:len(pkg) - (node.level - 1)] if node.level > 1 else list(pkg)
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            if mod:
                out.add(mod)
            for a in node.names:
                out.add(f"{mod}.{a.name}" if mod else a.name)
    return out


def _string_text(tree: ast.AST) -> str:
    """All string constants of a module except docstrings, joined."""
    doc_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                doc_ids.add(id(body[0].value))
    return "\n".join(n.value for n in ast.walk(tree)
                     if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc_ids)


def _holds_string(tree: ast.AST, value: str) -> bool:
    """True when a string constant of the module is exactly ``value`` (a longer string starting with it is not)."""
    return any(isinstance(n, ast.Constant) and n.value == value for n in ast.walk(tree))


class _Graph:
    def __init__(self, sources: dict[str, str]):
        self.sources = sources
        self.by_name: dict[str, str] = {}
        for path in sorted(sources):  # root-dotted and src names first, bare aliases after
            self.by_name.setdefault(_dotted(path), path)
        for path in sorted(sources):
            for n in _names(path, self.by_name):
                self.by_name.setdefault(n, path)
        self._trees: dict[str, ast.AST | None] = {}

    def tree(self, path: str):
        if path not in self._trees:
            try:
                self._trees[path] = ast.parse(self.sources[path])
            except (SyntaxError, ValueError):
                self._trees[path] = None
        return self._trees[path]

    def referrers(self, target: str) -> set[str]:
        stem = _stem(target)
        base = PurePosixPath(target).name
        names = {n for n, p in self.by_name.items() if p == target} | _names(target, self.by_name)
        name_re = re.compile(r"(?<![\w])" + re.escape(base))
        # a string that is Python source (``"import <package>"`` run in a child process) names the module too;
        # the bare name of a scripts/ or tools/ top file counts here, a child process puts that folder first
        # only when the stem names this target: a src package of that name owns it, and ``import <stem>`` is not this file
        # unless that same file holds a string equal to the folder name: it puts the folder on the import path itself
        folder = PurePosixPath(target).parts[0]
        owner = self.by_name.get(stem)
        top = folder in ("scripts", "tools")
        alias = {stem} if top and owner in (None, target) else set()
        code_re = re.compile(r"(?:import|from)\s+(?:" + "|".join(re.escape(n) for n in sorted(names | alias)) + r")(?![\w])")
        folder_code_re = None
        if top and not alias:
            folder_code_re = re.compile(r"(?:import|from)\s+(?:" + "|".join(re.escape(n) for n in sorted(names | {stem})) + r")(?![\w])")
        stem_re = re.compile(r"(?<![\w])" + re.escape(stem) + r"(?![\w])")
        # ``import <stem>`` resolving to another file (a src package shadowing a scripts/ file) is not this file
        shadow = None if stem in names else re.compile(r"(?:from|import)\s+" + re.escape(stem) + r"(?![\w])")
        out: set[str] = set()
        for path, text in self.sources.items():
            if path == target:
                continue
            by_import = any(stem_re.search(shadow.sub('', m.group()) if shadow else m.group()) for m in _IMPORT_SPAN.finditer(text))  # cheap filter before ast
            plain = _REAL_IMPORT.sub('', text)  # a real import is not a string
            by_string = base in text or bool(code_re.search(plain)) or bool(folder_code_re and folder_code_re.search(plain))
            if not (by_import or by_string):
                continue
            tree = self.tree(path)
            if tree is None:
                continue
            if by_import and names & _imports(path, tree):
                out.add(path)
            elif by_string:
                strings = _string_text(tree)
                if name_re.search(strings) or code_re.search(strings):
                    out.add(path)
                elif folder_code_re and folder_code_re.search(strings) and _holds_string(tree, folder):
                    out.add(path)
        return out


def referrer_tests(root: str, head: str, files: list[str], code_deps: dict[str, set[str]]
                   ) -> tuple[set[str], set[str], list[str]]:
    """``(mapped test ids, test files to run whole, files nothing reaches)`` for the unmapped ``files``."""
    graph = _Graph(_tracked_sources(root, head))
    ids: set[str] = set()
    whole: set[str] = set()
    unreached: list[str] = []
    for f in files:
        seen = {f}
        todo = [f]
        reached = False
        while todo:
            cur = todo.pop()
            for r in sorted(graph.referrers(cur) - seen):
                seen.add(r)
                reached = True
                if TEST_FILE.fullmatch(r):
                    whole.add(r)
                elif r in code_deps:
                    ids |= code_deps[r]
                else:
                    todo.append(r)
        if not reached:
            unreached.append(f)
    return ids, whole, unreached
