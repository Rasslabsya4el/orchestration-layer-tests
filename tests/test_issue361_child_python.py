"""#361: the page server's children run on the console `python.exe`, not on a GUI `pythonw.exe`.

A GUI interpreter gives its children no console to inherit, so a console program they start (git under a collector)
opens a visible window. Seams under test: `child_python` (picks the interpreter), `read` (starts the collector with it,
with a fake runner and with the default runner), and `run` (keeps CREATE_NO_WINDOW; Windows only). No real collector
runs, no real checkout is read.

    python -m pytest -q -p no:faulthandler tests/test_issue361_child_python.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.agentops import monitor_repos

CREATE_NO_WINDOW = 0x08000000
SOURCE = "monitor-src"


def _fake_interpreters(folder: Path, python: bool) -> Path:
    """A fake `pythonw.exe` in `folder`, with a fake `python.exe` beside it when `python` is True."""
    folder.mkdir(parents=True, exist_ok=True)
    pythonw = folder / "pythonw.exe"
    pythonw.write_bytes(b"")
    if python:
        (folder / "python.exe").write_bytes(b"")
    return pythonw


def _checkout(root: Path) -> Path:
    """A fake checkout with a monitor config and its collector file (the runner is fake; the file is never run)."""
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "orchestration.json").write_text(json.dumps({"monitor": {"source": SOURCE}}), encoding="utf-8")
    bin_dir = root / "mods" / SOURCE / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "collect.py").write_text("# stub: the runner is fake, this file is never run\n", encoding="utf-8")
    return root


def test_a_pythonw_with_a_python_beside_it_gives_that_python(tmp_path):
    pythonw = _fake_interpreters(tmp_path / "bin", python=True)
    assert monitor_repos.child_python(str(pythonw)) == str(tmp_path / "bin" / "python.exe")


def test_a_pythonw_with_no_python_beside_it_gives_the_input_unchanged(tmp_path):
    pythonw = _fake_interpreters(tmp_path / "bin", python=False)
    assert monitor_repos.child_python(str(pythonw)) == str(pythonw)


def test_a_python_input_is_returned_unchanged(tmp_path):
    python = tmp_path / "bin" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    assert monitor_repos.child_python(str(python)) == str(python)


def test_read_starts_the_collector_on_the_console_python_under_pythonw(tmp_path, monkeypatch):
    pythonw = _fake_interpreters(tmp_path / "bin", python=True)
    monkeypatch.setattr(sys, "executable", str(pythonw))
    folder = _checkout(tmp_path / "checkout")
    entry = {"id": "other-repo", "label": "owner/other-repo", "folder": str(folder)}
    seen: list = []

    def fake_runner(args, cwd, timeout, env):
        seen.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout=b"{}", stderr=b"")

    assert monitor_repos.read(entry, "fast", runner=fake_runner) == {}
    argv0 = seen[0][0]
    assert argv0.endswith("python.exe")
    assert not argv0.endswith("pythonw.exe")


@pytest.mark.skipif(os.name != "nt", reason="CREATE_NO_WINDOW is a Windows creation flag")
def test_read_under_pythonw_starts_console_python_with_the_hidden_console_flag(tmp_path, monkeypatch):
    pythonw = _fake_interpreters(tmp_path / "bin", python=True)
    monkeypatch.setattr(sys, "executable", str(pythonw))
    folder = _checkout(tmp_path / "checkout")
    entry = {"id": "other-repo", "label": "owner/other-repo", "folder": str(folder)}
    seen: dict = {}

    def fake_run(args, **kwargs):
        seen["args"] = list(args)
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout=b"{}", stderr=b"")

    monkeypatch.setattr(monitor_repos.subprocess, "run", fake_run)
    assert monitor_repos.read(entry, "fast") == {}
    assert seen["args"][0].endswith("python.exe")
    assert not seen["args"][0].endswith("pythonw.exe")
    assert seen["creationflags"] & CREATE_NO_WINDOW


@pytest.mark.skipif(os.name != "nt", reason="CREATE_NO_WINDOW is a Windows creation flag")
def test_run_keeps_the_hidden_console_flag(monkeypatch):
    seen: dict = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout=b"{}", stderr=b"")

    monkeypatch.setattr(monitor_repos.subprocess, "run", fake_run)
    monitor_repos.run(["python", "x"], "C:/x", 30, {})
    assert seen["creationflags"] & CREATE_NO_WINDOW
