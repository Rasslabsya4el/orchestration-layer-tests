"""Issue #172: a monitor's ``hooks/project.ts`` must export every name the hooks import from ``'./project'``.

Read-only helpers: what the hooks import, what a project.ts exports, what is missing, and the monitor stub's own
declarations of the missing names with the placeholders filled (the text ``core update`` appends and ``mods install``
refuses on, units 2 and 3 of #172). The stub's filling is ``port.fill_stub``, the one filling the port uses.
"""

from __future__ import annotations

import re
from pathlib import Path

from tools.agentops import port

PROJECT_FILE = "project.ts"
STUB_TEMPLATE = dict(port.MONITOR_STUBS)["hooks/project.ts"]

_IMPORT_RE = re.compile(r"""import\s+(?:type\s+)?(?:\w+\s*,\s*)?\{([^}]*)\}\s*from\s*(['"])\./project\2""")
# one declaration line: `export [declare] [async] [abstract] const enum|const|let|var|function|class|enum|type|interface NAME`
_DECL_RE = re.compile(
    r"^export\s+(?:declare\s+)?(?:async\s+)?(?:abstract\s+)?"
    r"(?:const\s+enum|const|let|var|function|class|enum|type|interface)\s+(\w+)",
    re.MULTILINE,
)
# an export list `export { A, B as C }` or `export { x } from './y'`: the exported name is the alias
_EXPORT_LIST_RE = re.compile(r"^export\s+(?:type\s+)?\{([^}]*)\}", re.MULTILINE)


def _specifiers(braces: str) -> list[tuple[str, str]]:
    """(original, alias) per ``A``, ``A as B`` or ``type A`` in a brace list; the alias is the original when none."""
    out = []
    for part in braces.split(","):
        part = re.sub(r"^type\s+", "", part.strip())
        if part:
            pieces = re.split(r"\s+as\s+", part)
            out.append((pieces[0].strip(), pieces[-1].strip()))
    return out


def imported_names(hooks_dir: Path) -> set[str]:
    """Every name a ``*.ts`` or ``*.tsx`` file directly in ``hooks_dir`` imports from ``'./project'``."""
    names: set[str] = set()
    for path in [*Path(hooks_dir).glob("*.ts"), *Path(hooks_dir).glob("*.tsx")]:
        for braces, _quote in _IMPORT_RE.findall(path.read_text(encoding="utf-8")):
            names |= {original for original, _alias in _specifiers(braces)}
    return names


def exported_names(project_ts_text: str) -> set[str]:
    """The names a project.ts exports: its top-level declarations (every form of ``_DECL_RE``) and the aliases of its
    export lists (``_EXPORT_LIST_RE``)."""
    names = set(_DECL_RE.findall(project_ts_text))
    for braces in _EXPORT_LIST_RE.findall(project_ts_text):
        names |= {alias for _original, alias in _specifiers(braces)}
    return names


def missing_names(hooks_dir: Path) -> list[str]:
    """The sorted imported names that ``hooks_dir/project.ts`` does not export (all of them when it is absent)."""
    project = Path(hooks_dir) / PROJECT_FILE
    exported = exported_names(project.read_text(encoding="utf-8")) if project.is_file() else set()
    return sorted(imported_names(hooks_dir) - exported)


def _declarations(stub_text: str) -> list[tuple[str, str]]:
    """(name, text) per declaration in stub order: the ``//`` lines directly above it, then its lines up to the next
    line that starts with ``export `` or ``//`` at column 0; trailing blank lines dropped, each text ends in a newline."""
    found: list[list] = []
    pending: list[str] = []
    open_decl: list | None = None
    for line in stub_text.splitlines(keepends=True):
        match = _DECL_RE.match(line)
        if match:
            open_decl = [match.group(1), [*pending, line]]
            found.append(open_decl)
            pending = []
        elif line.startswith("//"):
            open_decl = None
            pending.append(line)
        elif open_decl is not None and not line.startswith("export "):
            open_decl[1].append(line)
        else:
            open_decl = None
            pending = []
    return [(name, "".join(lines).rstrip() + "\n") for name, lines in found]


def lines_to_append(hooks_dir: Path, stub_fill: dict[str, str], home_root: Path = port.HOME_ROOT) -> str:
    """The stub's declaration (with its comment, placeholders filled by ``stub_fill``) of each missing name, in stub
    order; ``""`` when nothing is missing. The stub is read from ``home_root``'s core templates. A missing name the stub
    does not declare raises ``ValueError`` naming it."""
    missing = missing_names(hooks_dir)
    if not missing:
        return ""
    declarations = _declarations((Path(home_root) / "config" / "core_templates" / STUB_TEMPLATE).read_text(encoding="utf-8"))
    declared = {name for name, _text in declarations}
    unknown = [name for name in missing if name not in declared]
    if unknown:
        raise ValueError(f"{STUB_TEMPLATE} declares no {', '.join(unknown)}: add it to the stub before it is appended")
    wanted = set(missing)
    emitted: set[str] = set()
    out = []
    for name, text in declarations:
        if name in wanted and name not in emitted:
            emitted.add(name)
            out.append(port.fill_stub(text, stub_fill))
    return "".join(out)


def append_missing(hooks_dir: Path, stub_fill: dict[str, str], home_root: Path = port.HOME_ROOT) -> bool:
    """Append ``lines_to_append`` to ``hooks_dir/project.ts``; True when bytes were written. Every existing byte stays:
    a file without a final newline gets one first, and the appended text takes the file's line endings (CRLF when the
    file holds any). Nothing missing or no project.ts: nothing is written and the file is not touched. A missing name the
    stub does not declare raises ``ValueError`` before any write."""
    project = Path(hooks_dir) / PROJECT_FILE
    if not project.is_file():
        return False
    text = lines_to_append(hooks_dir, stub_fill, home_root)
    if not text:
        return False
    old = project.read_bytes()
    eol = "\r\n" if b"\r\n" in old else "\n"
    lead = b"" if not old or old.endswith(b"\n") else eol.encode("ascii")
    project.write_bytes(old + lead + text.replace("\n", eol).encode("utf-8"))
    return True
