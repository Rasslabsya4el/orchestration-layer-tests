"""The leak check (issue 147): one row per file of the manifest, then a total.

    python -m tools.agentops core check [--root PATH]

Stdlib only. Three patterns (L1, L2, L3) live in a local settings file
(``config/core_leak_patterns.json``, always read next to this repo, never
next to a checked target) so a copy of the manifest's files elsewhere can be
checked for the same three things with ``--root``. A fourth pattern (H) is
built into this module: it is the same in every installation, so it needs no
settings file at all. With no settings file present the check counts H alone, prints one "not checked" line
and shows '-' in the L1-L3 columns (issue 93).

A line counts once per pattern it matches, mirroring a ``grep -c`` per
pattern (not a match count): five occurrences on one line still count 1.

Pattern I (issue #1096) is built in too: every import statement of every ``.py`` file of the
manifest, at any depth (module level, function body, ``try``), relative imports resolved against
the file's package. It counts a statement that names a module under the checked root which is not
in the manifest; a file that does not parse counts 1. I > 0 fails the check like a leak.
An absolute import (issue #103) whose top name is no module under the root is also looked up in the
importing file's own folder and in each folder the file adds to ``sys.path`` (a literal string, or a
``Path(__file__)``-relative expression, read statically, never executed); a module found there counts
like one under the root, so a bare ``import shell_words`` beside the file is judged too.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve()
DEFAULT_ROOT = _HERE.parents[2]
MANIFEST_PATH = _HERE.with_name("core_manifest.txt")
PATTERNS_PATH = DEFAULT_ROOT / "config" / "core_leak_patterns.json"

H_PATTERN = re.compile(r"#\d+\b|\bD\d{2,3}\b|\bADR \d{3,4}\b|\b20\d\d-\d\d-\d\d\b")

PATTERN_KEYS = ("L1", "L2", "L3")

#: The one line a check prints when its settings file is absent (issue 93): L1-L3 were not judged, so their columns show '-'.
NOT_CHECKED_LINE = "L1-L3 not checked: no config/core_leak_patterns.json"

#: The monitor mod (#797, #830): its installed folder keeps its first name (grill 2026-10-11 Q4), so that literal is
#: stripped from the lines of the entries under MONITOR_PREFIXES only (spelled in parts: this file is checked too);
#: anywhere else it is matched like any word. Under those prefixes L2 is judged too, minus one word: the machine
#: card's own noun (the word MONITOR_L2_KEPT matches) is generic. The report's L2 column stays the raw count.
MONITOR_PREFIXES = ("mods/", "config/core_templates/monitor_")
MONITOR_LITERAL = "po" + "b" + "-s" + "lots"
#: The L2 alternative a monitor entry may keep: the machine card's own noun (generic); spelled in parts, this file is checked too.
MONITOR_L2_KEPT = r"\bs" + r"lots?\b(?!\s*=)"


@dataclass
class FileRow:
    path: str
    lines: int | None
    counts: dict[str, int]
    missing: bool = False
    skipped: bool = False  # H not judged: a target-owned entry (port.TARGET_OWNED_ENTRIES) in a target
    l2_judged: int | None = None  # monitor entries: L2 without MONITOR_L2_KEPT (None: L2 itself is judged)

    @property
    def h(self) -> int:
        return self.counts.get("H", 0)


def read_manifest(manifest_path: Path = MANIFEST_PATH) -> list[str]:
    """Repo-relative paths, in file order; blank lines and ``#`` comments skipped."""
    entries: list[str] = []
    for raw in manifest_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        entries.append(line)
    return entries


def read_patterns(patterns_path: Path = PATTERNS_PATH) -> dict[str, re.Pattern[str]]:
    """The three settings-defined patterns; empty if the settings file is absent. Only the pattern keys whose
    value is a string are compiled: the other keys of the file (#88 ``checked_avoid_words``, a list) are not patterns."""
    if not patterns_path.exists():
        return {}
    raw = json.loads(patterns_path.read_text(encoding="utf-8"))
    return {key: re.compile(value, re.IGNORECASE) for key, value in raw.items()
            if key in PATTERN_KEYS and isinstance(value, str)}


def patterns_file_exists(patterns_path: Path | None = None) -> bool:
    """Whether the settings file the check reads is present; absent, L1-L3 are not judged (issue 93).
    No argument: the same default path run_check reads (looked up at call time)."""
    return (PATTERNS_PATH if patterns_path is None else patterns_path).exists()


def _count_matching_lines(text_lines: list[str], pattern: re.Pattern[str]) -> int:
    return sum(1 for line in text_lines if pattern.search(line))


def _without_alternative(pattern: str, alternative: str) -> str:
    """``pattern`` with one ``|`` alternative cut out together with its separator (as is if absent)."""
    for cut in ("|" + alternative, alternative + "|", alternative):
        if cut in pattern:
            return pattern.replace(cut, "", 1)
    return pattern


def _judged_lines(text: str, rel: str) -> list[str]:
    """The lines of ``text`` as the check judges them: the monitor literal stripped where it is allowed."""
    text_lines = text.splitlines()
    if rel.startswith(MONITOR_PREFIXES):
        text_lines = [line.replace(MONITOR_LITERAL, "") for line in text_lines]
    elif rel == MANIFEST_PATH.name or rel.endswith("/" + MANIFEST_PATH.name):
        # the manifest names the monitor's own files by path: those lines (a path under MONITOR_PREFIXES) keep the literal out
        text_lines = [line.replace(MONITOR_LITERAL, "") if line.strip().startswith(MONITOR_PREFIXES) else line
                      for line in text_lines]
    return text_lines


def check_file(file_path: Path, rel: str, patterns: dict[str, re.Pattern[str]]) -> FileRow:
    if not file_path.is_file():
        return FileRow(path=rel, lines=None, counts={}, missing=True)
    text_lines = _judged_lines(file_path.read_text(encoding="utf-8", errors="replace"), rel)
    monitor = rel.startswith(MONITOR_PREFIXES)
    counts = {key: _count_matching_lines(text_lines, patterns[key]) for key in patterns}
    counts["H"] = _count_matching_lines(text_lines, H_PATTERN)
    l2_judged = None
    if monitor and "L2" in patterns:
        stripped = re.compile(_without_alternative(patterns["L2"].pattern, MONITOR_L2_KEPT), re.IGNORECASE)
        l2_judged = _count_matching_lines(text_lines, stripped)
    return FileRow(path=rel, lines=len(text_lines), counts=counts, missing=False, l2_judged=l2_judged)


def leak_hits(file_path: Path, rel: str, patterns: dict[str, re.Pattern[str]]) -> list[tuple[int, str, str]]:
    """Every judged L1-L3 hit of one file as (line number, pattern key, matched text), in line order.
    Judged as ``check_file`` and ``run_check`` judge: a monitor entry's L2 is read without MONITOR_L2_KEPT."""
    if not file_path.is_file():
        return []
    text_lines = _judged_lines(file_path.read_text(encoding="utf-8", errors="replace"), rel)
    judged = dict(patterns)
    if rel.startswith(MONITOR_PREFIXES) and "L2" in patterns:
        judged["L2"] = re.compile(_without_alternative(patterns["L2"].pattern, MONITOR_L2_KEPT), re.IGNORECASE)
    hits: list[tuple[int, str, str]] = []
    for number, line in enumerate(text_lines, start=1):
        for key in PATTERN_KEYS:
            found = judged[key].search(line) if key in judged else None
            if found:
                hits.append((number, key, found.group(0)))
    return hits


def _repo_module_files(root: Path, parts: list[str]) -> list[str]:
    """The repo-relative files that make the dotted module ``parts`` under ``root`` (empty: not a repo module)."""
    if not parts:
        return []
    base = root.joinpath(*parts)
    dotted = "/".join(parts)
    found: list[str] = []
    if base.with_name(base.name + ".py").is_file():
        found.append(dotted + ".py")
    if (base / "__init__.py").is_file():
        found.append(dotted + "/__init__.py")
    return found


def _statement_modules(node: ast.Import | ast.ImportFrom, pkg_parts: list[str]) -> list[list[str]]:
    """The dotted modules one import statement names, relative ones resolved against ``pkg_parts``.

    ``from M import a`` names M and M.a (a submodule, if the file is one); ``from . import a`` names
    only the package's submodule a, never the package itself.
    """
    if isinstance(node, ast.Import):
        return [alias.name.split(".") for alias in node.names]
    drop = max(node.level - 1, 0)  # level 0 is absolute: resolved from the root, not the package
    if drop > len(pkg_parts):
        return []
    base = pkg_parts[:len(pkg_parts) - drop] if node.level else []
    module = base + (node.module.split(".") if node.module else [])
    targets = [module] if node.module else []
    targets.extend(module + [alias.name] for alias in node.names if alias.name != "*")
    return targets


#: the package extensions.load resolves a leading-dot entry of local_modules in (extensions.py's own package).
LOADER_PACKAGE = ("tools", "agentops")


def local_module_parts(root: Path) -> list[list[str]]:
    """The dotted modules of the ``local_modules`` of ``root``'s settings file, resolved as
    extensions.load resolves them: ``.name`` is a module of LOADER_PACKAGE, any other name an absolute path.
    Empty when the file is absent or holds no such list."""
    try:
        settings = json.loads((root / "config" / "orchestration.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    names = settings.get("local_modules") if isinstance(settings, dict) else None
    if not isinstance(names, list):
        return []
    parts: list[list[str]] = []
    for name in names:
        if not isinstance(name, str) or not name:
            continue
        if name.startswith("."):
            parts.append(list(LOADER_PACKAGE) + name[1:].split("."))
        else:
            parts.append(name.split("."))
    return parts


def _dotted(node: ast.expr) -> str | None:
    """``a.b.c`` as text for a name or an attribute chain; None for anything else."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return None if head is None else head + "." + node.attr
    return None


def _joined(base: list[str], text: str) -> list[str] | None:
    """``base`` extended by the posix or windows path ``text``; None if it is absolute or climbs above the root."""
    text = text.replace("\\", "/")
    if text.startswith("/") or text[1:2] == ":":
        return None
    parts = list(base)
    for seg in text.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(seg)
    return parts


def _drop(parts: list[str] | None, count: int) -> list[str] | None:
    """``parts`` without its last ``count`` entries (``.parent`` is 1); None when that leaves the root."""
    if parts is None or count > len(parts):
        return None
    return parts[:len(parts) - count]


def _module_constants(tree: ast.AST) -> dict[str, ast.expr]:
    """The module-level names the file assigns exactly once (``X = v`` or ``X: T = v``), with their values.
    A name stored anywhere else in the file, or more than once, is left out (issue #103 review)."""
    stores = Counter(node.id for node in ast.walk(tree)
                     if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store))
    constants: dict[str, ast.expr] = {}
    for stmt in getattr(tree, "body", []):
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target, value = stmt.target, stmt.value
        else:
            continue
        if isinstance(target, ast.Name) and stores[target.id] == 1:
            constants[target.id] = value
    return constants


def _folder_parts(node: ast.expr, file_parts: list[str], names: dict[str, ast.expr]) -> list[str] | None:
    """The repo-relative parts of the folder (or file) a static path expression names, ``__file__`` being the
    file itself: ``Path(__file__)``, ``.parent``, ``.parents[k]``, ``/ "x"``, ``str(...)``, ``.resolve()``,
    ``os.path.dirname``, ``os.path.join``. A literal string is a path from the root; a name in ``names`` (a
    module constant, ``_module_constants``) stands for its value, and a name that leads back to itself is
    None. Never evaluated: None for any other expression, or for one that leaves the root."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _joined([], node.value)
    if isinstance(node, ast.Name) and node.id == "__file__":
        return list(file_parts)
    if isinstance(node, ast.Name):
        if node.id not in names:
            return None
        # resolved without this name, so a cycle runs out of names and ends in None
        rest = {key: value for key, value in names.items() if key != node.id}
        return _folder_parts(names[node.id], file_parts, rest)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        right = node.right
        base = _folder_parts(node.left, file_parts, names)
        if base is None or not (isinstance(right, ast.Constant) and isinstance(right.value, str)):
            return None
        return _joined(base, right.value)
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        return _drop(_folder_parts(node.value, file_parts, names), 1)
    if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
            and node.value.attr == "parents" and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, int)):
        return _drop(_folder_parts(node.value.value, file_parts, names), node.slice.value + 1)
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ("resolve", "absolute") and not node.args:
            return _folder_parts(func.value, file_parts, names)
        name = _dotted(func)
        if name in ("str", "Path", "os.path.abspath", "os.path.normpath") and len(node.args) == 1:
            return _folder_parts(node.args[0], file_parts, names)
        if name == "os.path.dirname" and len(node.args) == 1:
            return _drop(_folder_parts(node.args[0], file_parts, names), 1)
        if name == "os.path.join" and node.args:
            parts = _folder_parts(node.args[0], file_parts, names)
            for arg in node.args[1:]:
                if parts is None or not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                    return None
                parts = _joined(parts, arg.value)
            return parts
    return None


def _sys_path_folders(tree: ast.AST, rel: str) -> list[list[str]]:
    """The folders the file adds to ``sys.path`` (``sys.path.insert(i, X)`` or ``sys.path.append(X)``) whose X
    is a static path expression (``_folder_parts``); a folder outside the root is not in the list."""
    file_parts = rel.split("/")
    constants = _module_constants(tree)
    folders: list[list[str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("insert", "append") and _dotted(node.func.value) == "sys.path"
                and node.args):
            continue
        parts = _folder_parts(node.args[-1], file_parts, constants)
        if parts is not None:
            folders.append(parts)
    return folders


def count_outside_imports(file_path: Path, rel: str, root: Path, manifest_entries: set[str],
                          local: list[list[str]] = ()) -> int:
    """Pattern I for one file: the import statements that name a repo module outside the manifest,
    or a local module of ``local`` (a direct import of one is flagged whatever the manifest says:
    only the loader, through importlib, may load it). An absolute import whose top name is no module
    under the root (issue #103) is also looked up in the file's own folder and in the folders the file
    adds to ``sys.path``; a module found there counts as a repo module of the same file set."""
    text = file_path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(text, filename=rel)
    except (SyntaxError, ValueError):
        return 1  # an import closure that cannot be read is not certified clean
    pkg_parts = rel.split("/")[:-1]
    sibling_folders = [pkg_parts] + _sys_path_folders(tree, rel)
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        absolute = not isinstance(node, ast.ImportFrom) or node.level == 0
        for parts in _statement_modules(node, pkg_parts):
            if any(parts[:len(name)] == name for name in local):
                count += 1
                break
            found = _repo_module_files(root, parts)
            if not found and absolute and not _repo_module_files(root, parts[:1]):
                for folder in sibling_folders:
                    found = _repo_module_files(root, folder + parts)
                    if found:
                        break
            if found and not any(path in manifest_entries for path in found):
                count += 1
                break
    return count


def _is_target(root: Path) -> bool:
    """A root is a target iff ``<root>/config/orchestration.json`` exists and its
    ``update_rule`` is not ``"home"`` (read directly: importing core_update here would be
    circular, since port.py -- which owns TARGET_OWNED_ENTRIES -- imports from this module)."""
    settings_path = root / "config" / "orchestration.json"
    if not settings_path.exists():
        return False
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return settings.get("update_rule") != "home"


def run_check(root: Path = DEFAULT_ROOT, manifest_path: Path = MANIFEST_PATH,
              patterns_path: Path = PATTERNS_PATH) -> tuple[list[FileRow], bool]:
    """Returns (rows, ok). ``ok`` is False on any leak or missing file.

    In a target (``_is_target``), an entry of ``port.TARGET_OWNED_ENTRIES`` is the target's own
    text: its H count is still reported, but does not fail the check. L1-L3 still do.
    """
    from .port import TARGET_OWNED_ENTRIES

    patterns = read_patterns(patterns_path)
    is_target = _is_target(root)
    rows: list[FileRow] = []
    ok = True
    entries = read_manifest(manifest_path)
    manifest_set = set(entries)
    local = local_module_parts(root)
    for entry in entries:
        row = check_file(root / entry, entry, patterns)
        rows.append(row)
        if row.missing:
            ok = False
            continue
        if entry.endswith(".py"):
            outside = count_outside_imports(root / entry, entry, root, manifest_set, local)
            if outside:
                row.counts["I"] = outside
        for key in PATTERN_KEYS:
            count = row.l2_judged if key == "L2" and row.l2_judged is not None else row.counts.get(key, 0)
            if count > 0:
                ok = False
        if row.counts.get("I", 0) > 0:
            ok = False
        if is_target and entry in TARGET_OWNED_ENTRIES:
            row.skipped = True
        elif row.path.endswith(".md") and row.h > 0:
            ok = False
    return rows, ok


def tracked_under_shared(root: Path = DEFAULT_ROOT) -> list[str]:
    """Paths git tracks under a SHARED dir (worktree.SHARED): ``worktree add`` links those dirs,
    so a tracked file there makes it refuse. Empty if ``root`` is not a git repo."""
    from .worktree import SHARED

    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--", *SHARED],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def format_report(rows: list[FileRow], patterns_checked: bool = True) -> str:
    """The report. With ``patterns_checked`` False (no settings file) L1-L3 were not judged: their columns show
    '-' and NOT_CHECKED_LINE comes first, instead of a zero that reads as a clean check (issue 93)."""
    def cells(counts: dict[str, int]) -> str:
        return "".join(
            f"{'-' if key in PATTERN_KEYS and not patterns_checked else counts.get(key, 0):>5}"
            for key in ("L1", "L2", "L3", "H", "I"))

    header = f"{'path':<55}{'lines':>7}{'L1':>5}{'L2':>5}{'L3':>5}{'H':>5}{'I':>5}"
    out = [header]
    total_lines = 0
    total = {"L1": 0, "L2": 0, "L3": 0, "H": 0, "I": 0}
    skipped_paths: list[str] = []
    for row in rows:
        if row.missing:
            out.append(f"{row.path:<55}  MISSING")
            continue
        total_lines += row.lines or 0
        for key in ("L1", "L2", "L3", "H", "I"):
            total[key] += row.counts.get(key, 0)
        line = f"{row.path:<55}{row.lines:>7}" + cells(row.counts)
        if row.skipped:
            skipped_paths.append(row.path)
            line += " H skipped"
        out.append(line)
    out.append(f"{'total':<55}{total_lines:>7}" + cells(total))
    out.append(
        f"skipped {len(skipped_paths)} (H not judged on target-owned files): "
        f"{', '.join(skipped_paths)}"
    )
    if not patterns_checked:
        out.append(NOT_CHECKED_LINE)
    return "\n".join(out)


def add_subparser(sub) -> None:
    core = sub.add_parser("core", help="the orchestration core's own manifest and leak check")
    core_sub = core.add_subparsers(dest="core_command", required=True)
    check = core_sub.add_parser(
        "check", help="one row per manifest file, exit 1 on any leak or non-manifest import (pattern I)")
    check.add_argument("--root", type=Path, default=None, help="check a copy of the core elsewhere")
    # issue #1082: the core's contract with the Pocock plugin (tools/agentops/pocock_contract.py), one finding per line.
    pocock = core_sub.add_parser("pocock-check", help="the core's contract with the Pocock plugin, exit 1 on a finding")
    pocock.add_argument("--plugin-cache", type=Path, default=None,
                        help="the plugin's version cache (its newest version is read; default: the installed one)")
    pocock.add_argument("--root", type=Path, default=None, help="the core folder whose contract is checked")

    # issue #153: `core status` / `core update` (a target reading the home) and `core table`
    # (the home reading every target), added here so `core` stays the one subparser this repo's
    # CLI wires; the modules themselves are imported lazily below to avoid a circular import.
    from .core_update import add_status_update_subparsers
    add_status_update_subparsers(core_sub)

    from .core_table import add_table_subparser
    add_table_subparser(core_sub)

    # issue #263: `core issue`, written at the home under the one parent issue.
    from .core_issue import add_issue_subparser
    add_issue_subparser(core_sub)


def run_core_command(args, settings_path=None) -> int:
    if args.core_command == "check":
        root = args.root if args.root is not None else DEFAULT_ROOT
        rows, ok = run_check(root=root)
        print(format_report(rows, patterns_checked=patterns_file_exists()))
        tracked = tracked_under_shared(root)
        for path in tracked:
            print(f"tracked under a shared dir, breaks worktree add: {path}")
        return 0 if ok and not tracked else 1
    if args.core_command == "pocock-check":
        from .pocock_contract import run_pocock_check_command
        return run_pocock_check_command(args, settings_path)
    if args.core_command == "status":
        from .core_update import run_status_command
        return run_status_command(args)
    if args.core_command == "update":
        from .core_update import run_update_command
        return run_update_command(args)
    if args.core_command == "update-steps":
        from .core_update import run_update_steps_command
        return run_update_steps_command(args)
    if args.core_command == "table":
        from .core_table import run_table_command
        return run_table_command(args)
    if args.core_command == "issue":
        from .core_issue import run_issue_command
        return run_issue_command(args)
    return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="core_check")
    sub = ap.add_subparsers(dest="command", required=True)
    add_subparser(sub)
    args = ap.parse_args(argv)
    if args.command == "core":
        return run_core_command(args)
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(main())
