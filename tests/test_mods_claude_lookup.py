"""#176: ``mods.run_test`` finds ``claude`` when the process has it neither on PATH nor in CLAUDE_CODE_EXECPATH.

A merge started detached (no session environment) failed every mod with ``FileNotFoundError [WinError 2]`` at
``claude plugin validate``; the dispatcher's own lookup (``find_claude_exe``) finds the installed exe.
Seam: ``mods._claude()`` and the first command ``mods.run_test`` hands its runner."""

from __future__ import annotations

from pathlib import Path

from tools.agentops import mods


def _install(tmp_path: Path) -> Path:
    base = tmp_path / "Packages" / "Claude_abc" / "LocalCache" / "Roaming" / "Claude" / "claude-code"
    exe = base / "2.1.299" / "claude.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    return exe


def _bare_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.delenv("CLAUDE_CODE_EXECPATH", raising=False)
    monkeypatch.delenv("CLAUDE_EXE", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "no-appdata"))


def test_no_path_no_variable_finds_the_installed_exe(tmp_path, monkeypatch):
    exe = _install(tmp_path)
    _bare_env(monkeypatch, tmp_path)
    assert mods._claude() == str(exe)


def test_run_test_starts_the_installed_exe_when_the_process_has_no_claude(tmp_path, monkeypatch):
    exe = _install(tmp_path)
    _bare_env(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    (repo / "mods" / "m").mkdir(parents=True)
    monkeypatch.setattr(mods, "sync_shared", lambda name, root=None: None)
    seen: list[list[str]] = []

    def runner(cmd, cwd):
        seen.append(cmd)
        return 0, "ok\n"

    assert mods.run_test("m", runner=runner, root=repo) == 0
    assert seen[0][0] == str(exe) and seen[0][1:3] == ["plugin", "validate"]


def test_the_variable_still_wins_over_the_search(tmp_path, monkeypatch):
    _install(tmp_path)
    _bare_env(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_EXECPATH", r"C:\given\claude.exe")
    assert mods._claude() == r"C:\given\claude.exe"


def test_nothing_found_keeps_the_bare_name(tmp_path, monkeypatch):
    _bare_env(monkeypatch, tmp_path)
    assert mods._claude() == "claude"


def test_a_claude_on_path_wins_over_the_search(tmp_path, monkeypatch):
    _install(tmp_path)
    _bare_env(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: r"C:\on\path\claude.exe")
    assert mods._claude() == r"C:\on\path\claude.exe"
