"""Issue 53 (unit 1): a failing post-copy step names its last stderr line and the file that holds its full output, and
the target's reason carries that whole line into the brain notice (no cut at a newline, at most 400 characters).

Row 1 runs the real default step runner (`core_update._subprocess_step_runner`) on a tmp target whose
`tools/agentops` package is a fake `update-steps` that prints a 3-line traceback to stderr and exits 1.
Row 2 runs the real `run_core_update` (a fake home through `gh`, the #28 family's helpers) inside `core_targets`
and then the dispatcher's notice for the merge result it leaves. Row 3 is the 400-character cap on the reason
(a cut from the left, the `(full output: <path>)` end kept). Row 4 is a realistic ImportError line through the
real chain, which must survive whole within the cap.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests import test_issue28_core_update_failed_commit as base
from tests.test_agentops_dispatch import FakeClaude
from tests.test_issue28_failed_target_notice import (  # noqa: F401 -- `results` is the fixture, imported with its fakes
    O,
    _core_wakes,
    _dispatcher,
    _gh_with_issue_7,
    _post,
    results,
)
from tools.agentops import core_targets, core_update
from tools.agentops.core_update import CoreUpdateError

EXC_LINE = "KeyError: 'mods_stub_xyz'"
IMPORT_LINE = (
    "ImportError: cannot import name 'MODS_STUB_CONSTANTS' from 'tools.agentops.mods' "
    "(D:\\Work\\target-repo-v2\\tools\\agentops\\mods.py), the install_folder stubs are missing"
)
ENTRY_REPO = "orch-repo"
WRAPPER = ("core update failed in its post-copy steps; the copy is undone, core_version unchanged: ")


def _traceback(exc_line: str) -> list[str]:
    return ["Traceback (most recent call last):", '  File "fake_step.py", line 1, in <module>', exc_line]


def _fake_update_steps(root: Path, exc_line: str = EXC_LINE) -> None:
    """A target whose `python -m tools.agentops core update-steps` prints the traceback to stderr and exits 1."""
    package = root / "tools" / "agentops"
    package.mkdir(parents=True, exist_ok=True)
    stderr_text = "\n".join(_traceback(exc_line)) + "\n"
    (root / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import sys\n"
        f"sys.stderr.write({stderr_text!r})\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )


def _err_files(root: Path) -> list[Path]:
    return sorted((root / "artifacts" / "runs").glob("core_update_steps-*.err"))


def _real_update(monkeypatch) -> None:
    """core_targets runs the real `run_core_update` (real git target, the #28 fake home), with no ORCH live."""
    monkeypatch.setattr(core_targets, "blocking_orch_live", lambda folder: False)
    monkeypatch.setattr(core_targets, "run_core_update", lambda folder, gh_runner=None, lock_wait_s=None, home_count=None: core_update.run_core_update(
        folder, gh_runner=base._fake_gh(base.NEW_HOME_FILES), live_check=lambda: False, lock_wait_s=lock_wait_s))


def _reason_of(folder: Path, tmp_path: Path) -> str:
    lines = core_targets.run_core_targets([{"repo": ENTRY_REPO, "folder": str(folder)}], home_root=tmp_path)
    assert len(lines) == 1 and lines[0].startswith(f"{ENTRY_REPO}: failed: ")
    return lines[0].split(": failed: ", 1)[1]


# -- row 1: the raised message and the full output file ------------------------------------------------------------

def test_row1_failing_step_names_the_last_stderr_line_and_the_full_output_file(tmp_path: Path) -> None:
    root = tmp_path / "target"
    root.mkdir()
    _fake_update_steps(root)

    with pytest.raises(CoreUpdateError) as caught:
        core_update._subprocess_step_runner(root)

    message = str(caught.value)
    files = _err_files(root)
    assert len(files) == 1, "the full output is written once, under artifacts/runs in the target"
    assert re.fullmatch(r"core_update_steps-\d{8}-\d{6}\.err", files[0].name)
    assert message == f"{EXC_LINE} (full output: {files[0]})"
    assert "Traceback" not in message
    text = files[0].read_text(encoding="utf-8")
    for line in _traceback(EXC_LINE):
        assert line in text


# -- row 2: the reason through core_targets and the dispatcher's notice ---------------------------------------------

def test_row2_reason_and_notice_keep_the_exception_line_whole(tmp_path: Path, monkeypatch, results) -> None:
    root = base._target(tmp_path)  # a real git target with the #28 family's fake home
    _fake_update_steps(root)
    _real_update(monkeypatch)

    lines = core_targets.run_core_targets([{"repo": ENTRY_REPO, "folder": str(root)}], home_root=tmp_path)

    assert len(lines) == 1 and lines[0].startswith(f"{ENTRY_REPO}: failed: ")
    assert "\n" not in lines[0]
    assert EXC_LINE in lines[0]
    files = _err_files(root)
    assert len(files) == 1 and f"(full output: {files[0]})" in lines[0]

    claude = FakeClaude()
    claude.add_session("chat-o", name=O, state="idle")
    _post(results, [7], lines, branch="m1")
    _dispatcher(tmp_path, claude, _gh_with_issue_7()).tick()

    messages = [m for _, m in _core_wakes(claude)]
    assert len(messages) == 1
    notice = messages[0].splitlines()
    assert len(notice) == 1, "the notice is one line per reason"
    assert EXC_LINE in notice[0]
    assert f"(full output: {files[0]})" in notice[0]


# -- row 3: the reason is at most 400 characters; a cut from the left keeps the `(full output: <path>)` end ----------

@pytest.mark.parametrize("line_chars", [280, 500])  # 280: the line and the 98-character end fit in 400 with the `...`
def test_row3_reason_over_400_characters_is_cut_from_the_left_and_keeps_the_path(tmp_path: Path, monkeypatch,
                                                                                 line_chars: int) -> None:
    path = Path("D:/Work/orchestration-layer/artifacts/runs/core_update_steps-20261016-153508.err")  # 82 chars
    exc_line = "ImportError: " + "x" * (line_chars - len("ImportError: "))

    def failing(folder, gh_runner=None, lock_wait_s=None, home_count=None):
        raise CoreUpdateError(f"{WRAPPER}{exc_line} (full output: {path})")

    monkeypatch.setattr(core_targets, "blocking_orch_live", lambda folder: False)
    monkeypatch.setattr(core_targets, "run_core_update", failing)
    folder = tmp_path / "target"
    folder.mkdir()

    reason = _reason_of(folder, tmp_path)

    assert len(reason) <= 400
    assert reason.startswith("...")
    assert reason.endswith(f"(full output: {path})")
    if line_chars == 280:
        # the wrapper goes first; the line alone, with the end, fits, so it survives whole
        assert WRAPPER not in reason
        assert f"{exc_line} (full output: {path})" in reason
    else:
        # the line alone is over the cap: its head goes, its tail and the end stay
        assert f"{exc_line[-100:]} (full output: {path})" in reason


# -- row 4: a realistic ImportError line through the real chain survives whole within the cap -----------------------

def test_row4_realistic_import_error_line_survives_whole_within_the_cap(tmp_path: Path, monkeypatch) -> None:
    assert 140 <= len(IMPORT_LINE) <= 200
    root = base._target(tmp_path)
    _fake_update_steps(root, IMPORT_LINE)
    _real_update(monkeypatch)

    reason = _reason_of(root, tmp_path)

    files = _err_files(root)
    assert len(files) == 1
    assert len(reason) <= 400
    assert IMPORT_LINE in reason
    assert reason.endswith(f"{IMPORT_LINE} (full output: {files[0]})")
