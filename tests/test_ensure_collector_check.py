"""tools/agentops/dispatch.py `ensure_collector` (issue #166, unit 1): the collector writes its own log
`artifacts/runs/collector.out`, and its launched pid is checked `monitor.collector_start_check_s` seconds
after the start (default 10). Every test injects `popen`, `pid_alive` and `sleep`: nothing waits and no
real collector starts."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

from tools.agentops.dispatch import ensure_collector

COLLECT_REL = Path("mods") / "pob-slots" / "bin" / "collect.py"
DEAD_PID = 999999999
LIVE_PID = 4242


class _FakeProcess:
    """Stands in for `subprocess.run` (Windows: `.stdout` carries the WMI `rc=.. pid=..` line) and
    `subprocess.Popen` (elsewhere: `.pid`)."""

    def __init__(self, pid: int):
        self.stdout = f"rc=0 pid={pid}"
        self.stderr = ""
        self.returncode = 0
        self.pid = pid


def _make_collect(root: Path, check_s: float | None = None) -> None:
    monitor: dict = {"source": "pob-slots"}
    if check_s is not None:
        monitor["collector_start_check_s"] = check_s
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "orchestration.json").write_text(json.dumps({"monitor": monitor}), encoding="utf-8")
    collect = root / COLLECT_REL
    collect.parent.mkdir(parents=True, exist_ok=True)
    collect.write_text("# stand-in for the collector\n", encoding="utf-8")


def _command_text(command) -> str:
    """The command as one readable line: the decoded PowerShell script on Windows, the argv joined otherwise."""
    if "-EncodedCommand" in command:
        encoded = command[command.index("-EncodedCommand") + 1]
        return base64.b64decode(encoded).decode("utf-16-le")
    return " ".join(str(part) for part in command)


def _dispatch_lines(root: Path) -> list[str]:
    path = root / "artifacts" / "runs" / "dispatch.out"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _recording_popen(calls: list, pid: int, before=None):
    """A fake launcher: records the call, runs `before` (the stand-in collector's own writes), returns `pid`."""
    def fake(command, **kwargs):
        calls.append((command, kwargs))
        if before is not None:
            before()
        return _FakeProcess(pid)
    return fake


def test_dead_collector_at_the_check_logs_one_dispatch_line_with_its_last_three_lines(tmp_path: Path):
    _make_collect(tmp_path)
    runs = tmp_path / "artifacts" / "runs"

    def collector_writes_then_dies():
        runs.mkdir(parents=True, exist_ok=True)
        (runs / "collector.out").write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")

    calls: list = []
    waits: list = []
    checked: list = []
    result = ensure_collector(
        tmp_path, popen=_recording_popen(calls, DEAD_PID, collector_writes_then_dies), env={},
        pid_alive=lambda pid: checked.append(pid) or False, sleep=waits.append)

    assert result == "died"
    assert waits == [10]          # the default `monitor.collector_start_check_s`
    assert checked == [DEAD_PID]  # the pid the launcher returned is the one checked
    lines = _dispatch_lines(tmp_path)
    assert len(lines) == 1, lines
    assert lines[0].endswith("collector died after start: three | four | five")


def test_live_collector_at_the_check_is_started_with_no_died_line(tmp_path: Path):
    _make_collect(tmp_path, check_s=3)
    calls: list = []
    waits: list = []
    checked: list = []
    result = ensure_collector(
        tmp_path, popen=_recording_popen(calls, LIVE_PID), env={},
        pid_alive=lambda pid: checked.append(pid) or pid == LIVE_PID, sleep=waits.append)

    assert result == "started"
    assert waits == [3]
    assert checked == [LIVE_PID]
    assert not any("died" in line for line in _dispatch_lines(tmp_path))


def test_collector_start_logs_to_its_own_file_never_dispatch_out(tmp_path: Path):
    _make_collect(tmp_path)
    calls: list = []
    ensure_collector(tmp_path, popen=_recording_popen(calls, LIVE_PID), env={},
                     pid_alive=lambda pid: True, sleep=lambda seconds: None)
    command, kwargs = calls[0]
    collector_out = tmp_path / "artifacts" / "runs" / "collector.out"
    if sys.platform == "win32":
        text = _command_text(command)
        assert str(collector_out) in text
        assert "dispatch.out" not in text
    else:
        assert kwargs["stdout"].name == str(collector_out)
