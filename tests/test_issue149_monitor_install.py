"""#149: a merge or a core update that changes the monitor installs it in the app.

The shared helper ``tools.agentops.monitor_install``: ``touches_monitor`` tells whether a diff reaches the
monitor's source folder (or ``mods/shared/``); ``install_after`` runs the install once and returns one line.
The runner is faked here; the default runner is checked only by its command line and its exit-code mapping.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools.agentops import core_check, install_folder, monitor_install


def _settings(root: Path, monitor: dict | None) -> None:
    cfg = root / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    data = {} if monitor is None else {"monitor": monitor}
    (cfg / "orchestration.json").write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    _settings(tmp_path, {"source": "demo-monitor", "plugin_id": "orch-monitor"})
    return tmp_path


class FakeRunner:
    def __init__(self, error: BaseException | None = None):
        self.calls: list[tuple[Path, str]] = []
        self.error = error

    def __call__(self, root: Path, source: str) -> None:
        self.calls.append((root, source))
        if self.error is not None:
            raise self.error


def test_monitor_source_path_installs_once(root):
    runner = FakeRunner()
    assert monitor_install.install_after(["mods/demo-monitor/hooks/x.tsx"], root, runner=runner) == "monitor installed"
    assert runner.calls == [(root, "demo-monitor")]


def test_shared_path_installs_once(root):
    runner = FakeRunner()
    assert monitor_install.install_after(["mods/shared/a.ts"], root, runner=runner) == "monitor installed"
    assert runner.calls == [(root, "demo-monitor")]


def test_docs_and_other_mod_do_not_install(root):
    runner = FakeRunner()
    assert monitor_install.install_after(["docs/x.md", "mods/other/y.ts"], root, runner=runner) is None
    assert runner.calls == []


def test_runner_error_is_reported_not_raised(root):
    runner = FakeRunner(RuntimeError("boom"))
    line = monitor_install.install_after(["mods/demo-monitor/hooks/x.tsx"], root, runner=runner)
    assert line is not None
    assert line.startswith("monitor install failed: ")
    assert "boom" in line
    assert len(runner.calls) == 1


def test_backslash_paths_are_matched(root):
    runner = FakeRunner()
    assert monitor_install.install_after(["mods\\demo-monitor\\hooks\\x.tsx"], root, runner=runner) == "monitor installed"
    assert len(runner.calls) == 1


def test_touches_monitor_reads_source_from_settings(root):
    assert monitor_install.touches_monitor(["mods/demo-monitor/a.tsx"], root) is True
    assert monitor_install.touches_monitor(["mods\\shared\\b.ts"], root) is True
    assert monitor_install.touches_monitor(["mods/other/y.ts"], root) is False
    assert monitor_install.touches_monitor(["docs/x.md"], root) is False


def test_no_monitor_source_is_nothing_to_install(tmp_path):
    _settings(tmp_path, None)
    runner = FakeRunner()
    paths = ["mods/shared/a.ts", "mods/demo-monitor/a.tsx"]
    assert monitor_install.touches_monitor(paths, tmp_path) is False
    assert monitor_install.install_after(paths, tmp_path, runner=runner) is None
    assert runner.calls == []


def test_unreadable_settings_give_false_and_a_failed_line(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "orchestration.json").write_text('{"monitor": {"source": "bad name!"}}', encoding="utf-8")
    runner = FakeRunner()
    assert monitor_install.touches_monitor(["mods/shared/a.ts"], tmp_path) is False
    line = monitor_install.install_after(["mods/shared/a.ts"], tmp_path, runner=runner)
    assert line is not None and line.startswith("monitor install failed: ")
    assert "monitor.source" in line
    assert runner.calls == []


def test_target_without_source_uses_the_literal_monitor_folder(tmp_path, monkeypatch):
    literal = core_check.MONITOR_LITERAL
    _settings(tmp_path, {"plugin_id": "my-target-monitor"})
    (tmp_path / "mods" / literal).mkdir(parents=True)
    monkeypatch.setenv("AGENTOPS_MODS_REPO", str(tmp_path))
    assert install_folder.source_name(tmp_path) == literal
    assert install_folder.install_name(literal) == "my-target-monitor"
    paths = [f"mods/{literal}/hooks/x.tsx"]
    assert monitor_install.touches_monitor(paths, tmp_path) is True
    runner = FakeRunner()
    assert monitor_install.install_after(paths, tmp_path, runner=runner) == "monitor installed"
    assert runner.calls == [(tmp_path, literal)]


def test_default_runner_command_and_nonzero_exit(root, monkeypatch):
    seen: dict = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 1, stdout="\nrefused: newer in the install\nmore\n", stderr="")

    monkeypatch.setattr(monitor_install.subprocess, "run", fake_run)
    line = monitor_install.install_after(["mods/demo-monitor/a.tsx"], root)
    assert seen["args"][1:] == ["-m", "tools.agentops", "mods", "install", "demo-monitor"]
    assert seen["kwargs"]["cwd"] == str(root)
    assert seen["kwargs"]["timeout"] == monitor_install.TIMEOUT_S
    assert line == "monitor install failed: refused: newer in the install"


def test_default_runner_timeout_is_a_failed_line(root, monkeypatch):
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, monitor_install.TIMEOUT_S)

    monkeypatch.setattr(monitor_install.subprocess, "run", fake_run)
    line = monitor_install.install_after(["mods/demo-monitor/a.tsx"], root)
    assert line == "monitor install failed: timed out after 3 min"
