"""The `logic-audit` command of #191 (`tools/agentops/logic_audit.py`): the argparse path, the core check, and one
part that raises. Fixtures live in tmp_path; the command writes nothing and reads no real repo log here.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

from tools.agentops import logic_audit
from tools.agentops.logic_audit import add_subparser, run_logic_audit_command
from tools.agentops.logic_audit_rows import NO_SOURCE
from tools.agentops.util import no_window_flags

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_CHECK_TIMEOUT_S = 300
PART_HEADINGS = ("## 1 baselines", "## 2 exception lists", "## 3 until conditions", "## 4 guards and repairs")


def _parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="tools.agentops")
    sub = ap.add_subparsers(dest="command", required=True)
    add_subparser(sub)
    return ap.parse_args(argv)


def _sections(out: str) -> dict[str, str]:
    """The text under each part heading, keyed by the heading line."""
    found: dict[str, str] = {}
    current = None
    for line in out.splitlines():
        if line.startswith("## "):
            current = line.strip()
            found[current] = ""
        elif current is not None:
            found[current] += line + "\n"
    return found


def test_empty_dir_prints_no_source_row_per_part(tmp_path, capsys):
    args = _parse(["logic-audit", "--since", "2026-10-04", "--root", str(tmp_path)])

    rc = run_logic_audit_command(args)

    out = capsys.readouterr().out
    assert rc == 0
    sections = _sections(out)
    for heading in PART_HEADINGS:
        assert heading in sections, f"missing heading {heading!r}"
        assert f"| {NO_SOURCE} |" in sections[heading], f"no source row missing under {heading!r}"
    assert "| kind | finding | place A | place B | count | proposed verdict |" in out
    assert "rows: " in out.splitlines()[-1]
    assert "(no source: 4)" in out.splitlines()[-1]


def test_a_part_that_raises_does_not_stop_the_others(tmp_path, capsys, monkeypatch):
    def boom(root, since, until=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(logic_audit, "exception_rows", boom)
    args = _parse(["logic-audit", "--since", "2026-10-04", "--root", str(tmp_path)])

    rc = run_logic_audit_command(args)

    out = capsys.readouterr().out
    assert rc == 0
    assert "part 2 failed: boom" in out
    sections = _sections(out)
    for heading in PART_HEADINGS:
        assert heading in sections
    assert f"| {NO_SOURCE} |" in sections["## 1 baselines"]
    assert f"| {NO_SOURCE} |" in sections["## 3 until conditions"]
    assert f"| {NO_SOURCE} |" in sections["## 4 guards and repairs"]


@pytest.mark.live
def test_core_check_exits_zero():
    """Checks `python -m tools.agentops core check` exits 0 on this real checkout (the owner's own `core check` run)."""
    res = subprocess.run(
        [sys.executable, "-m", "tools.agentops", "core", "check"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=CORE_CHECK_TIMEOUT_S,
        creationflags=no_window_flags(),
    )
    assert res.returncode == 0, res.stdout[-2000:] + res.stderr[-2000:]
