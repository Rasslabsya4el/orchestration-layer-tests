"""Which ticket comments are machine lines (a merge result, a checkpoint, a parking note) and which are a person's
words. Stdlib only.

The test reads the FIRST non-blank line of a comment body only. An empty body is a machine line: there is nothing
to read in it.
"""

from __future__ import annotations

import re

#: markers that a first line holds (anywhere in the line)
MACHINE_MARKERS = (
    "<!-- merge result -->",
    "<!-- runs-daemon result -->",
    "<!-- send-back -->",
    "<!-- accepted -->",
)
#: first-line prefixes of a parking note
MACHINE_PREFIXES = ("<!-- parked-on-run", "<!-- parked-on-merge")
#: first-line prefixes (after the markdown marks are dropped) of lines the tools write about a ticket, found by
#: reading the first lines of the four repos' comments since 2026-10-01 (#57): the title archive of a rewrite, the
#: autopilot's checkpoint, the runs daemon's note, a red full run's report
TOOL_PREFIXES = ("Title before the rewrite", "Autopilot checkpoint", "The runs daemon", "Another red full run")
#: a checkpoint, closing, diagnosis or loop stop written by an ORCH session: "Checkpoint 1 (ORCH 3302c1a1, #53). ..."
ORCH_LINE = re.compile(r"^(Checkpoint|Closing|Diagnosis|Loop stop)\b.*\(ORCH\b")
#: an ORCH session's finishing line: "ORCH a241f462 finishing: ..."
ORCH_FINISHING = re.compile(r"^ORCH\b.*\bfinishing\b")


def first_line(body: str) -> str | None:
    """The first non-blank line of `body`, stripped, or None when the body has none."""
    for line in body.splitlines():
        if line.strip():
            return line.strip()
    return None


def is_machine_comment(body: str) -> bool:
    """True when the comment is a machine line (or empty): judged on its first non-blank line only."""
    line = first_line(body or "")
    if line is None:
        return True
    if any(marker in line for marker in MACHINE_MARKERS):
        return True
    if line.startswith(MACHINE_PREFIXES):
        return True
    line = line.lstrip("#*_ 	")  # "**Checkpoint 2 (ORCH ...)**", "## Checkpoint 1 (ORCH ...)"
    return bool(line.startswith(TOOL_PREFIXES) or ORCH_LINE.match(line) or ORCH_FINISHING.match(line))
