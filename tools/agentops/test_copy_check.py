"""#231 unit 1 (D119): what a test copy may take, and the check that the snapshot is clean.

A test copy receives the tracked files of a repo minus the excluded set (``docs/research/``, ``var/``,
``artifacts/``, ``config/core_leak_patterns.json``, and the path prefixes a repo's ``test_copy_exclude`` setting
lists). A repo's ``test_copy_standins`` setting maps a path in the copy to a tracked stand-in file: the copy holds the
stand-in's bytes at that path and the stand-in's own path is not in the snapshot (#285). Each file of that snapshot is
scanned for: the built-in personal-data shapes (email, user-path, telegram-id), and the machine file's own ``product``
and ``owner-name`` patterns (``test_copy_patterns.json`` in ``machine_dir()``, outside every repo). The repo's own
leak words (``config/core_leak_patterns.json``) are not judged here (#285): they guard the core files, see
``core_check``. A hit is (file, line, kind); the matched text is never kept.

Stdlib only, and no import of anything under ``src/``.
"""

from __future__ import annotations

import codecs
import json
import os
import re
import subprocess
from pathlib import Path
from typing import NamedTuple

from tools.agentops.core_settings import DEFAULTS
from tools.agentops.machine_mode import machine_dir
from tools.agentops.util import no_window_flags

#: derived from this module's own location: tools/agentops/test_copy_check.py -> the repo root.
DEFAULT_ROOT = Path(__file__).resolve().parents[2]

#: the paths no test copy takes, whatever a repo's setting says.
EXCLUDED_PREFIXES = ("docs/research/", "var/", "artifacts/")
EXCLUDED_FILES = ("config/core_leak_patterns.json",)
SETTINGS_FILE = "config/orchestration.json"
MACHINE_FILE = "test_copy_patterns.json"
SETTING_KEY = "test_copy_exclude"
STANDINS_KEY = "test_copy_standins"

#: the kinds every check judges, in the order the report prints them.
BUILTIN = {
    # not an address: a reserved example domain (also as a subdomain) with nothing after it; ``scan`` blanks the
    # string escapes ``\n \r \t`` before this one kind is matched, so ``\n@name`` has no local part
    "email": re.compile(r"(?<![\w.+-])[\w.+-]+@(?!(?:(?:[\w-]+\.)*example\.(?:com|org|net)|[\w.-]*\.invalid"
                        r"|[\w.-]*\.test|localhost)(?![\w-]|\.[\w-]))[\w-]+(?:\.[\w-]+)+"),
    # not an account: a bare placeholder name (user, u, username, you, name) or a name opened by < [ { .
    "user-path": re.compile(r"(?i)(?:[a-z]:[\\/]+|/)(?:users|home)[\\/]+"
                            r"(?!(?:user|u|username|you|name)(?![\w.-])|[<\[{.])[^\\/\s'\"`]+"),
    "telegram-id": re.compile(r"(?i)(?:\btelegram\b|\btg\b).*\b\d{6,12}\b|\b\d{6,12}\b.*(?:\btelegram\b|\btg\b)"),
}
#: the kinds the machine file may give (absent key = that kind is not checked).
MACHINE_KINDS = ("product", "owner-name")
#: #346: a copied test file under ``tests/`` that names an excluded file (by its path, or by its file name or module
#: stem when it is a ``.py``) is a hit of this kind; a stem under MIN_STEM characters, or ``__init__``, is not matched.
EXCLUDED_REF = "excluded-ref"
MIN_STEM = 4
#: #354: a quoted bare word (``"build"``) counts as a reference only on a line that shows a file context: a path
#: separator, a ``.py``, or an import / load / spec word.
FILE_CONTEXT = re.compile(r"[\\/]|\.py\b|(?<![a-z])(?:import|importlib|load|spec)(?![a-z])")
#: only an excluded source file is checked: a data or markdown file a temp-repo fixture names is not a read of it.
SOURCE_SUFFIXES = (".py", ".ts", ".js", ".sh", ".ps1")


def git_env() -> dict:
    """The environment a git call of the test copy runs in: the process's own with every ``GIT_*`` variable removed
    (GIT_DIR, GIT_INDEX_FILE, GIT_AUTHOR_* ... would point git at another repo or leak an identity)."""
    return {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}


class CheckError(Exception):
    """The check cannot run: not a git repo, a settings or machine file that does not parse. The message names
    the file and the problem, never a matched value."""


class Hit(NamedTuple):
    file: str
    line: int
    kind: str
    ref: str = ""  # the excluded file an ``excluded-ref`` hit names (#346); "" for every other kind

    @property
    def text(self) -> str:
        """``file:line kind``, then the excluded file's path for an ``excluded-ref`` hit (#346)."""
        return f"{self.file}:{self.line} {self.kind}" + (f" {self.ref}" if self.ref else "")


class Patterns(NamedTuple):
    compiled: dict  # kind -> compiled pattern, in report order
    not_checked: tuple  # kinds this run did not judge


def _machine_patterns() -> dict | None:
    path = machine_dir() / MACHINE_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CheckError(f"{path} does not parse ({type(exc).__name__})") from exc
    if not isinstance(data, dict):
        raise CheckError(f"{path} must hold a JSON object")
    return data


def load_patterns(root: Path = DEFAULT_ROOT) -> Patterns:
    """The patterns one check runs with: the built-in shapes, and the machine file's ``product`` and ``owner-name``
    (a kind whose key is absent or empty is listed as not checked). ``root`` is unused: the repo's own leak words are
    not read (#285)."""
    compiled = dict(BUILTIN)
    not_checked = []
    machine = _machine_patterns()
    for kind in MACHINE_KINDS:
        raw = machine.get(kind) if machine else None
        if isinstance(raw, str) and raw:
            try:
                compiled[kind] = re.compile(raw, re.IGNORECASE)
            except re.error as exc:
                raise CheckError(f"{kind} in {machine_dir() / MACHINE_FILE} is not a regex ({exc.msg})") from exc
        else:
            not_checked.append(kind)
    return Patterns(compiled, tuple(not_checked))


def _setting(root: Path, key: str):
    """The value of ``key`` in the repo's settings file, else its default."""
    path = root / SETTINGS_FILE
    data = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CheckError(f"{SETTINGS_FILE} does not parse ({type(exc).__name__})") from exc
    return data.get(key, DEFAULTS[key]) if isinstance(data, dict) else DEFAULTS[key]


def _setting_prefixes(root: Path) -> list[str]:
    given = _setting(root, SETTING_KEY)
    if not isinstance(given, list) or not all(isinstance(p, str) for p in given):
        raise CheckError(f"{SETTING_KEY} in {SETTINGS_FILE} must be a list of path prefixes")
    return given


def _setting_standins(root: Path) -> dict[str, str]:
    given = _setting(root, STANDINS_KEY)
    if not isinstance(given, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in given.items()):
        raise CheckError(f"{STANDINS_KEY} in {SETTINGS_FILE} must map a path in the copy to a stand-in path")
    return {k.strip("/"): v.strip("/") for k, v in given.items()}


def _tracked(root: Path) -> list[str]:
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True,
                             creationflags=no_window_flags(), env=git_env())
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CheckError(f"not a git repo: {root}") from exc
    return [rel for rel in out.stdout.decode("utf-8", "replace").split("\0") if rel]


def _check_standin(root: Path, rel: str, standin: str, tracked: set[str]) -> None:
    """Raises CheckError naming both paths when the mapped path is not tracked or the stand-in file is missing."""
    if rel not in tracked:
        raise CheckError(f"stand-in {standin} for {rel}: {rel} is not a tracked file")
    if not (root / standin).is_file():
        raise CheckError(f"stand-in {standin} for {rel}: the stand-in file is missing")


def snapshot_files(root: Path = DEFAULT_ROOT) -> list[str]:
    """The tracked files of ``root`` a test copy takes: ``git ls-files`` minus the excluded set and minus each
    stand-in's own path (#285), sorted. A stand-in mapping with an untracked path or a missing file is a CheckError."""
    tracked = _tracked(root)
    standins = _setting_standins(root)
    have = set(tracked)
    for rel, standin in standins.items():
        _check_standin(root, rel, standin, have)
    listed = [entry.rstrip("/") for entry in _setting_prefixes(root) if entry.strip("/")]
    folders = (*EXCLUDED_PREFIXES, *(entry + "/" for entry in listed))
    exact = {*EXCLUDED_FILES, *listed, *standins.values()}
    return sorted(rel for rel in tracked if rel not in exact and not rel.startswith(folders))


def snapshot_bytes(root: Path, rel: str, patterns: Patterns, standins: bool = True) -> bytes:
    """The bytes of ``rel`` as they go into a test copy. With ``standins`` (the default; the scan of an already built
    copy passes False) a path mapped by ``test_copy_standins`` returns its stand-in's bytes (#285); a mapped path that
    is not tracked or a stand-in file that is missing is a CheckError naming both. Only ``config/orchestration.json``
    is sanitized: every entry of each of its top-level lists whose JSON text matches the ``product`` pattern is dropped
    (key order kept, the rest re-dumped with indent 2). A file that does not parse, has no list, drops nothing, or a
    run with no ``product`` pattern returns the raw bytes. The file on disk is never changed."""
    if standins:
        try:
            standin = _setting_standins(root).get(rel)
        except CheckError:
            standin = None  # an unreadable setting is raised by ``snapshot_files``; the bytes stay the file's own
        if standin is not None:
            _check_standin(root, rel, standin, set(_tracked(root)))
            return (root / standin).read_bytes()
    data = (root / rel).read_bytes()
    product = patterns.compiled.get("product")
    if rel != SETTINGS_FILE or product is None:
        return data
    try:
        settings = json.loads(data.decode("utf-8"))
    except ValueError:
        return data
    if not isinstance(settings, dict):
        return data
    dropped = False
    for key, value in settings.items():
        if isinstance(value, list):
            kept = [entry for entry in value if not product.search(json.dumps(entry, ensure_ascii=False))]
            if len(kept) != len(value):
                settings[key] = kept
                dropped = True
    if not dropped:
        return data
    text = json.dumps(settings, indent=2, ensure_ascii=False)
    tail = "\n" if data.endswith(b"\n") else ""
    return (text + tail).encode("utf-8")


NUL = bytes([0])
#: the two-char string escapes blanked (same length) in a line before the ``email`` kind is matched.
ESCAPES = re.compile(r"\\[nrt]")


def _texts(data: bytes) -> list[str]:
    """The text(s) of one file's bytes the patterns are matched on: UTF-16 when a BOM says so, else UTF-8 with
    replacement; a file with NUL bytes and no BOM is also read with its NULs dropped (UTF-16 without a BOM)."""
    for bom in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
        if data.startswith(bom):
            return [data.decode("utf-16", "replace")]
    texts = [data.decode("utf-8", "replace")]
    if NUL in data:
        texts.append(data.replace(NUL, b"").decode("utf-8", "replace"))
    return texts


def excluded_files(root: Path = DEFAULT_ROOT, files: list[str] | None = None) -> list[str]:
    """#346: the tracked files of ``root`` a test copy does not take (what ``snapshot_files`` drops), sorted. ``files``
    is the snapshot when the caller already has it."""
    kept = set(snapshot_files(root) if files is None else files)
    return sorted(rel for rel in _tracked(root) if rel not in kept)


def excluded_refs(root: Path = DEFAULT_ROOT, files: list[str] | None = None) -> list[str]:
    """#346: the excluded source files (``SOURCE_SUFFIXES``) a copied test may not name: ``excluded_files`` filtered."""
    return [rel for rel in excluded_files(root, files) if rel.endswith(SOURCE_SUFFIXES)]


class RefMatcher(NamedTuple):
    """#346: the regex of ``_ref_matcher`` and the maps from the text a match found to the excluded file(s) it names:
    ``paths`` (repo path), ``names`` (a ``.py`` file name), ``stems`` (a ``.py`` module stem), ``dotted`` (a ``.py``
    module path, ``scripts.secret``)."""
    pattern: re.Pattern
    paths: dict
    names: dict
    stems: dict
    dotted: dict


def _ref_matcher(excluded: list[str]) -> RefMatcher | None:
    """#346: one regex that finds, on a line, a name of an excluded source file: its path (``/`` or ``\\`` between the
    parts); for a ``.py``, its file name, its stem as a quoted string or as an import / ``from ... import`` module name,
    and its dotted module path (``scripts.secret``) anywhere in the line. Whole words only. None when there is nothing
    to find."""
    paths: dict[str, list[str]] = {}
    names: dict[str, list[str]] = {}
    stems: dict[str, list[str]] = {}
    dotted: dict[str, list[str]] = {}
    for ref in excluded:
        paths.setdefault(ref, []).append(ref)
        base = ref.rsplit("/", 1)[-1]
        stem = base[:-3] if base.endswith(".py") else ""
        if stem and len(stem) >= MIN_STEM and stem != "__init__":
            names.setdefault(base, []).append(ref)
            stems.setdefault(stem, []).append(ref)
            dotted.setdefault(ref[:-3].replace("/", "."), []).append(ref)
    if not (paths or names):
        return None
    parts = []
    if paths:
        alt = "|".join(r"[\\/]+".join(re.escape(piece) for piece in ref.split("/")) for ref in paths)
        parts.append(rf"(?<![\w.-])(?P<p>{alt})(?!\w)")
    if names:
        parts.append(rf"(?<![\w.-])(?P<f>{'|'.join(re.escape(name) for name in names)})(?!\w)")
    if stems:
        alt = "|".join(re.escape(stem) for stem in stems)
        parts.append(rf"[\"'](?P<q>{alt})[\"']")
        parts.append(rf"\b(?:from|import)\s+(?:\w+\.)*(?P<m>{alt})(?!\w)")
    if dotted:
        alt = "|".join(re.escape(name) for name in dotted)
        parts.append(rf"(?<![\w.])(?P<d>{alt})(?!\w)")
    return RefMatcher(re.compile("|".join(parts)), paths, names, stems, dotted)


def _named_refs(matcher: RefMatcher, line: str) -> list[str]:
    """#346: the excluded files a line names, in the order the line names them (one entry per name found)."""
    found = []
    for match in matcher.pattern.finditer(line):
        if match.group("p") is not None:
            found += matcher.paths.get(re.sub(r"[\\/]+", "/", match.group("p")), [])
        elif match.group("f") is not None:
            found += matcher.names.get(match.group("f"), [])
        elif match.group("d") is not None:
            found += matcher.dotted.get(match.group("d"), [])
        elif match.group("q") is not None:
            # #354: a quoted bare word is a reference only when its line shows a file context
            if FILE_CONTEXT.search(line):
                found += matcher.stems.get(match.group("q"), [])
        elif match.group("m") is not None:
            found += matcher.stems.get(match.group("m"), [])
    return found


def scan(root: Path = DEFAULT_ROOT, patterns: Patterns | None = None, files: list[str] | None = None,
         standins: bool | None = None, excluded: list[str] | None = None) -> list[Hit]:
    """Every hit of the snapshot: one Hit per (file, line, kind), for each file's text (a binary file is scanned like
    text, see ``_texts``) and for each file's path (line 0 = the path itself). ``standins`` says whether the files are
    read as the copy gets them (``test_copy_standins`` applied, see ``snapshot_bytes``); None = yes when ``files`` is
    None (the snapshot of a real repo), no when the file list of an already built copy is given.
    #346: a copied file under ``tests/`` that names an excluded file is an ``excluded-ref`` hit naming that file.
    ``excluded`` is the tracked-but-not-copied list (``excluded_files``): None = computed from ``root`` for the snapshot
    of a real repo, and no such check for the file list of an already built copy unless the caller passes it."""
    if standins is None:
        standins = files is None
    patterns = patterns or load_patterns(root)
    compiled = patterns.compiled
    if files is None:
        files = snapshot_files(root)
        if excluded is None:
            excluded = excluded_refs(root, files)
    matcher = _ref_matcher(excluded or [])
    seen: set[Hit] = set()
    hits: list[Hit] = []

    def add(hit: Hit) -> None:
        if hit not in seen:
            seen.add(hit)
            hits.append(hit)

    for rel in files:
        for kind, pattern in compiled.items():
            if pattern.search(rel):
                add(Hit(rel, 0, kind))
        try:
            data = snapshot_bytes(root, rel, patterns, standins)
        except OSError:
            continue
        for text in _texts(data):
            for lineno, line in enumerate(text.splitlines(), start=1):
                for kind, pattern in compiled.items():
                    if pattern.search(ESCAPES.sub("  ", line) if kind == "email" else line):
                        add(Hit(rel, lineno, kind))
                if matcher is not None and rel.startswith("tests/"):
                    for ref in _named_refs(matcher, line):
                        add(Hit(rel, lineno, EXCLUDED_REF, ref))
    return hits
