"""The starter leak-word file of a target (#93): `config/core_leak_patterns.json` with one pattern, `L3`, that
matches the target's own folder path and repo name. The leak check reads the file (`core_check.read_patterns`);
a target without it has its names judged never, silently. `target add` (the port) and `core update` write this
starter where the file is absent; a file already there is never touched. The file is the target's own: it is no
core file (not a line of core_manifest.txt), and L1 and L2 (the product words) are the target's brain's call."""

from __future__ import annotations

import json
import re
from pathlib import Path

PATTERNS_ENTRY = "config/core_leak_patterns.json"
_SEPARATOR = r"[\\/]+"  # a path separator, either kind, one or more of them


def _escaped(text: str) -> str:
    """``text`` regex-escaped part by part, the path separators of either kind read as one separator."""
    return _SEPARATOR.join(re.escape(part) for part in re.split(r"[\\/]+", text) if part)


def _alternatives(folder: Path, repo: str) -> list[str]:
    """The folder path, the repo as ``owner/name``, and its bare name (after the slash), each one alternative."""
    options = [_escaped(str(folder)), _escaped(repo)]
    if "/" in repo:
        options.append(_escaped(repo.rsplit("/", 1)[1]))
    return list(dict.fromkeys(option for option in options if option))


def starter_text(folder: Path, repo: str) -> str:
    """The starter file's text: JSON with the one key ``L3``, a regex over the folder path and the repo name."""
    return json.dumps({"L3": "|".join(_alternatives(folder, repo))}, ensure_ascii=False, indent=2) + "\n"


def write_starter(root: Path, repo: str) -> Path | None:
    """Write the starter into ``root`` when the target has no ``config/core_leak_patterns.json``; returns its
    path, or None when the file is already there (left byte for byte)."""
    path = root / PATTERNS_ENTRY
    if path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(starter_text(root.resolve(), repo), encoding="utf-8")
    return path
