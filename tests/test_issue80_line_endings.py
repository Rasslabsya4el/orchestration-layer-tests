"""#80: every text file in the index is LF, and .gitattributes makes git keep it so."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _git(*args):
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True
    ).stdout


def test_no_tracked_file_has_crlf_in_the_index():
    crlf = [
        line.split("\t", 1)[1]
        for line in _git("ls-files", "--eol").splitlines()
        if line.split()[0] in ("i/crlf", "i/mixed")
    ]
    assert crlf == []


def test_gitattributes_forces_lf_for_text():
    lines = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "* text=auto eol=lf" in lines
