"""#231 unit 1 (D119): what a test copy may take, and the check that the snapshot is clean.

A test copy receives the tracked files of a repo minus the excluded set (``docs/research/``, ``var/``,
``artifacts/``, ``config/core_leak_patterns.json``, and the path prefixes a repo's ``test_copy_exclude`` setting
lists). Each file of that snapshot is scanned for: the leak patterns of the repo (``config/core_leak_patterns.json``,
kinds L1-L3, when the file exists), the built-in personal-data shapes (email, user-path, telegram-id), and the
machine file's own ``product`` and ``owner-name`` patterns (``test_copy_patterns.json`` in ``machine_dir()``, outside
every repo). A hit is (file, line, kind); the matched text is never kept.

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

from tools.agentops import core_check
from tools.agentops.core_settings import DEFAULTS
from tools.agentops.machine_mode import machine_dir
from tools.agentops.util import no_window_flags

#: derived from this module's own location: tools/agentops/test_copy_check.py -> the repo root.
DEFAULT_ROOT = Path(__file__).resolve().parents[2]

#: the paths no test copy takes, whatever a repo's setting says.
EXCLUDED_PREFIXES = ("docs/research/", "var/", "artifacts/")
EXCLUDED_FILES = ("config/core_leak_patterns.json",)
LEAK_FILE = "config/core_leak_patterns.json"
SETTINGS_FILE = "config/orchestration.json"
MACHINE_FILE = "test_copy_patterns.json"
SETTING_KEY = "test_copy_exclude"

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


class Patterns(NamedTuple):
    compiled: dict  # kind -> compiled pattern, in report order
    not_checked: tuple  # kinds (or the leak group "L1-L3") this run did not judge


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
    """The patterns one check runs with: the repo's leak patterns (when the file exists), the built-in shapes, and
    the machine file's ``product`` and ``owner-name`` (a kind whose key is absent or empty is listed as not checked)."""
    compiled = dict(BUILTIN)
    not_checked = []
    leak = root / LEAK_FILE
    if leak.is_file():
        try:
            compiled.update(core_check.read_patterns(leak))
        except (ValueError, re.error) as exc:
            raise CheckError(f"{LEAK_FILE} does not parse ({type(exc).__name__})") from exc
    else:
        not_checked.append("L1-L3")
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


def _setting_prefixes(root: Path) -> list[str]:
    path = root / SETTINGS_FILE
    data = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CheckError(f"{SETTINGS_FILE} does not parse ({type(exc).__name__})") from exc
    given = data.get(SETTING_KEY, DEFAULTS[SETTING_KEY]) if isinstance(data, dict) else DEFAULTS[SETTING_KEY]
    if not isinstance(given, list) or not all(isinstance(p, str) for p in given):
        raise CheckError(f"{SETTING_KEY} in {SETTINGS_FILE} must be a list of path prefixes")
    return given


def snapshot_files(root: Path = DEFAULT_ROOT) -> list[str]:
    """The tracked files of ``root`` a test copy takes: ``git ls-files`` minus the excluded set, sorted."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True,
                             creationflags=no_window_flags(), env=git_env())
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CheckError(f"not a git repo: {root}") from exc
    listed = [entry.rstrip("/") for entry in _setting_prefixes(root) if entry.strip("/")]
    folders = (*EXCLUDED_PREFIXES, *(entry + "/" for entry in listed))
    exact = {*EXCLUDED_FILES, *listed}
    tracked = out.stdout.decode("utf-8", "replace").split("\0")
    return sorted(rel for rel in tracked
                  if rel and rel not in exact and not rel.startswith(folders))


def snapshot_bytes(root: Path, rel: str, patterns: Patterns) -> bytes:
    """The bytes of ``rel`` as they go into a test copy. Only ``config/orchestration.json`` is sanitized: every entry
    of its top-level ``targets`` list whose JSON text matches the ``product`` pattern is dropped (key order kept, the
    rest re-dumped with indent 2). A file that does not parse, has no ``targets`` list, drops nothing, or a run with no
    ``product`` pattern returns the raw bytes. The file on disk is never changed."""
    data = (root / rel).read_bytes()
    product = patterns.compiled.get("product")
    if rel != SETTINGS_FILE or product is None:
        return data
    try:
        settings = json.loads(data.decode("utf-8"))
    except ValueError:
        return data
    targets = settings.get("targets") if isinstance(settings, dict) else None
    if not isinstance(targets, list):
        return data
    kept = [t for t in targets if not product.search(json.dumps(t, ensure_ascii=False))]
    if len(kept) == len(targets):
        return data
    settings["targets"] = kept
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


def scan(root: Path = DEFAULT_ROOT, patterns: Patterns | None = None, files: list[str] | None = None) -> list[Hit]:
    """Every hit of the snapshot: one Hit per (file, line, kind), for each file's text (a binary file is scanned like
    text, see ``_texts``) and for each file's path (line 0 = the path itself)."""
    patterns = patterns or load_patterns(root)
    compiled = patterns.compiled
    seen: set[Hit] = set()
    hits: list[Hit] = []

    def add(hit: Hit) -> None:
        if hit not in seen:
            seen.add(hit)
            hits.append(hit)

    for rel in (files if files is not None else snapshot_files(root)):
        for kind, pattern in compiled.items():
            if pattern.search(rel):
                add(Hit(rel, 0, kind))
        try:
            data = snapshot_bytes(root, rel, patterns)
        except OSError:
            continue
        for text in _texts(data):
            for lineno, line in enumerate(text.splitlines(), start=1):
                for kind, pattern in compiled.items():
                    if pattern.search(ESCAPES.sub("  ", line) if kind == "email" else line):
                        add(Hit(rel, lineno, kind))
    return hits
