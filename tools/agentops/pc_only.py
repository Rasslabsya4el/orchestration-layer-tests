"""#232 (D119): the test files that run on the PC only, listed in ``tests/pc_only.txt``.

One repo-relative test file per line; blank lines and ``#`` lines are skipped. The shard plugin drops these files from
every GitHub part (``drop_pc_only``) and the merge runs them on the PC beside the GitHub run. A missing list is the
empty set; a list that exists and cannot be read raises, it is never read as "nothing listed". Stdlib only.
"""

from __future__ import annotations

from pathlib import Path

PC_ONLY_FILE = "tests/pc_only.txt"


def load_pc_only(root) -> set[str]:
    """The test files listed in ``<root>/tests/pc_only.txt``; ``set()`` when the file does not exist."""
    try:
        text = (Path(root) / PC_ONLY_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    return {line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")}


def drop_pc_only(items: list, names: set[str]) -> tuple[list, list]:
    """``(kept, dropped)``: ``dropped`` are the items whose test file (the nodeid before ``::``) is in ``names``."""
    def listed(item) -> bool:
        return item.nodeid.split("::", 1)[0].replace("\\", "/") in names
    return [item for item in items if not listed(item)], [item for item in items if listed(item)]
