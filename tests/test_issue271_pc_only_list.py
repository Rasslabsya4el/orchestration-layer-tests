"""#271 unit 3: every test file with a win32 platform skip is listed in ``tests/pc_only.txt`` or carries a
``# not listed: <path> - <why>`` line there, so the Linux GitHub job never skips one silently (row 3 of #271).
#272 extends the check to the ``os.name != "nt"`` skips (directory junctions), the same two ways.

The scan is over ``tests/`` (subfolders too); the list is read with the same parser the shard plugin uses
(``tools.agentops.pc_only.load_pc_only``). This file is not scanned: its own patterns are written as strings.
Stdlib plus pytest only.
"""
from __future__ import annotations

import re
from pathlib import Path

from tools.agentops.pc_only import PC_ONLY_FILE, load_pc_only

REPO = Path(__file__).resolve().parents[1]
THIS_FILE = Path(__file__).name
NO_WINDOW = "tests/test_agentops_no_window.py"

# `skipif(...)` (a bare or `pytest.mark.` one, one line or several) whose arguments name the platform and win32 / "win".
SKIPIF_RE = re.compile(r"skipif\(((?:[^()]|\([^()]*\))*)\)", re.S)
# `if sys.platform != "win32":` (or `not sys.platform.startswith("win")`) followed by `pytest.skip(`.
IF_SKIP_RE = re.compile(r"if\s+((?:not\s+)?sys\.platform[^\n:]*):\s*\n?\s*pytest\.skip\(")
WIN_RE = re.compile(r"""win32|startswith\(\s*["']win["']\s*\)""")
# `if os.name != "nt":` followed by `pytest.skip(` (#272: directory junctions are Windows-only).
IF_OS_NAME_SKIP_RE = re.compile(r"""if\s+([^\n:]*os\.name\s*!=\s*["']nt["'][^\n:]*):\s*\n?\s*pytest\.skip\(""")
OS_NAME_NOT_NT_RE = re.compile(r"""os\.name\s*!=\s*["']nt["']""")
NOTE_RE = re.compile(r"^\s*#\s*not listed:\s*(tests/\S+)\s+-\s+(\S.*?)\s*$")


def has_win32_skip(text: str) -> bool:
    """True when the source holds a skip gated on the Windows platform (win32 / startswith("win"))."""
    for match in SKIPIF_RE.finditer(text):
        args = match.group(1)
        if "sys.platform" in args and WIN_RE.search(args):
            return True
    return any(WIN_RE.search(match.group(1)) for match in IF_SKIP_RE.finditer(text))


def has_os_name_skip(text: str) -> bool:
    """True when the source holds a skip gated on ``os.name != "nt"`` (the Windows-only junction tests, #272)."""
    for match in SKIPIF_RE.finditer(text):
        if OS_NAME_NOT_NT_RE.search(match.group(1)):
            return True
    return any(OS_NAME_NOT_NT_RE.search(match.group(1)) for match in IF_OS_NAME_SKIP_RE.finditer(text))


def has_windows_skip(text: str) -> bool:
    """True when the source holds a skip that runs only on Windows: a win32 platform skip or an os.name skip."""
    return has_win32_skip(text) or has_os_name_skip(text)


def windows_skip_files(root: Path = REPO) -> list[str]:
    """The repo-relative test files under ``<root>/tests/`` (subfolders too) with a Windows-only skip."""
    found = []
    for path in sorted((root / "tests").rglob("*.py")):
        if path.name == THIS_FILE or "__pycache__" in path.parts:
            continue
        if has_windows_skip(path.read_text(encoding="utf-8")):
            found.append(path.relative_to(root).as_posix())
    return found


def unlisted_windows_skip_files(root: Path = REPO) -> list[str]:
    """The Windows-skip test files neither listed in ``<root>/tests/pc_only.txt`` nor noted there."""
    listed = load_pc_only(root)
    noted = noted_files(root)
    return [path for path in windows_skip_files(root) if path not in listed and path not in noted]


def noted_files(root: Path = REPO) -> dict[str, str]:
    """``{path: why}`` for each ``# not listed: <path> - <why>`` comment line in ``tests/pc_only.txt``."""
    notes = {}
    for line in (root / PC_ONLY_FILE).read_text(encoding="utf-8").splitlines():
        match = NOTE_RE.match(line)
        if match:
            notes[match.group(1)] = match.group(2)
    return notes


def test_the_scanner_tells_a_win32_skip_from_a_win32_branch():
    assert has_win32_skip('pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="x")')
    assert has_win32_skip('@pytest.mark.skipif(\n    sys.platform != "win32",\n    reason="x",\n)\ndef t(): pass')
    assert has_win32_skip('if sys.platform != "win32":\n    pytest.skip("windows only")\n')
    assert has_win32_skip('if not sys.platform.startswith("win"):\n    pytest.skip("windows only")\n')
    assert not has_win32_skip('if sys.platform == "win32":\n    assert 1\n')


def test_the_scanner_finds_the_known_win32_skip_file():
    assert NO_WINDOW in windows_skip_files()
    assert "tests/test_worktree_junctions.py" in windows_skip_files()  # an os.name skip (#272)


def test_every_windows_skip_file_is_listed_or_has_a_not_listed_note():
    missing = unlisted_windows_skip_files()
    assert not missing, (
        f"win32-skip test files neither listed in {PC_ONLY_FILE} nor noted with '# not listed: <path> - <why>': "
        + ", ".join(missing)
    )


OS_NAME_SKIP = '@pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows-only")\n'


def test_the_scanner_tells_an_os_name_skip_from_a_branch():
    assert has_os_name_skip(OS_NAME_SKIP)
    assert has_os_name_skip('if os.name != "nt":\n    pytest.skip("junctions are Windows-only")\n')
    assert not has_os_name_skip('if os.name == "nt":\n    assert 1\n')
    assert not has_win32_skip(OS_NAME_SKIP)  # a junction skip is no win32 platform skip; the list test covers both (#272)


def test_the_list_test_flags_an_unlisted_os_name_skip_in_a_temp_tree(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_temp_junction.py").write_text(
        "import os\nimport pytest\n\n" + OS_NAME_SKIP + "\ndef test_x():\n    pass\n", encoding="utf-8"
    )
    (tmp_path / PC_ONLY_FILE).write_text("# header\n", encoding="utf-8")
    assert unlisted_windows_skip_files(tmp_path) == ["tests/test_temp_junction.py"]
    (tmp_path / PC_ONLY_FILE).write_text("# header\ntests/test_temp_junction.py\n", encoding="utf-8")
    assert unlisted_windows_skip_files(tmp_path) == []


def test_the_no_window_test_is_listed_in_pc_only():
    assert NO_WINDOW in load_pc_only(REPO), f"{NO_WINDOW} must be listed in {PC_ONLY_FILE} (#271 row 3)"
