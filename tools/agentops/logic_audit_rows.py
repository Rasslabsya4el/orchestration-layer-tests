"""The row every part of `logic-audit` returns (issue #191). Stdlib only.

One row is one line of the owner's table: ``kind | finding | place A | place B | count | proposed verdict``. A part with no
record source returns one row whose ``kind`` is ``no source`` (never an error).
"""

from __future__ import annotations

from dataclasses import dataclass

#: The kind of the row a part returns when the repo has none of its records.
NO_SOURCE = "no source"


@dataclass(frozen=True)
class Row:
    """One finding. ``place_a`` / ``place_b`` are ``file:line`` strings (or a log path); ``count`` is how many times it holds."""

    kind: str
    finding: str
    place_a: str = ""
    place_b: str = ""
    count: int = 1
    verdict: str = ""


def no_source_row(part: str, what: str) -> Row:
    """The row of a part whose records are missing: ``part`` names it, ``what`` names the missing source."""

    return Row(kind=NO_SOURCE, finding=f"{part}: {what}", count=0)
