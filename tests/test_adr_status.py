"""Every decision record in docs/adr/ carries one Status line at line 3, in the fixed wording (#1034).

Wording set (docs/agents/domain.md, the ADR shape):
- Accepted
- Accepted; amended by D<n> [amd <k>][, D<n> [amd <k>]...]
- Superseded by D<n>
- Withdrawn
"""

import re
from pathlib import Path

import pytest

ADR_DIR = Path(__file__).resolve().parents[1] / "docs" / "adr"
STATUS_RE = re.compile(
    r"^Status: ("
    r"Accepted"
    r"|Superseded by D\d+"
    r"|Withdrawn"
    r"|Accepted; amended by D\d+( amd \d+)?(, D\d+( amd \d+)?)*"
    r")$"
)
ADR_FILES = sorted(ADR_DIR.glob("*.md"))


@pytest.mark.parametrize("path", ADR_FILES, ids=lambda p: p.name)
def test_adr_has_status_line_three_in_fixed_wording(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("# "), f"{path.name}: line 1 is not a title"
    assert lines[1] == "", f"{path.name}: line 2 is not blank"
    assert STATUS_RE.match(lines[2]), f"{path.name}: line 3 is {lines[2]!r}"
