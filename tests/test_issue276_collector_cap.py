"""#276 unit 2: the monitor collector runs under a 2 GiB process-memory cap.

`ensure_collector` calls the cap (`tools/agentops/jobcap.py`) right after the launch, on Windows only; a cap that returns
a message or raises is one `dispatch.out` line and the collector still starts. A collector that ended with a
`MemoryError` (lock pid dead, `collector.out` names it) is restarted by the existing `collector_tick` path and one line
`collector <repo> ended at the 2 GB cap; restarted` is written, once. `collect.py serve` re-raises a `MemoryError`
so a capped collector ends instead of limping on.

No job, no process and no allocation: every test injects `popen`, `cap`, `pid_alive`, `sleep` and a fake clock.

    python -m pytest tests/test_issue276_collector_cap.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agentops import dispatch as dispatch_mod
from tools.agentops.dispatch import collector_tick, ensure_collector

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mods" / "pob-slots" / "bin"
sys.path.insert(0, str(BIN))

COLLECT_REL = Path("mods") / "monitor-mod" / "bin" / "collect.py"
LOCK_REL = Path("mods") / "monitor-mod" / "bin" / ".cache" / "collector.lock"
OLD_PID = 1111
NEW_PID = 2222
RULE_2_LINE = "collector {repo} ended at the 2 GB cap; restarted"


class _FakeProcess:
    def __init__(self, pid: int):
        self.stdout = f"rc=0 pid={pid}"
        self.stderr = ""
        self.returncode = 0
        self.pid = pid


def _popen(calls: list, pid: int = NEW_PID):
    def fake(command, **kwargs):
        calls.append((command, kwargs))
        return _FakeProcess(pid)
    return fake


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    dispatch_mod._NOT_COLLECTOR_LOGGED.clear()
    dispatch_mod._REFUSED_LOGGED.clear()
    # the cap is wired on Windows only; the wiring tests run the same way on every platform
    monkeypatch.setattr(dispatch_mod, "_caps_apply", lambda: True)


def _make_mod(root: Path) -> Path:
    (root / "config").mkdir(parents=True, exist_ok=True)
    monitor = {"source": "monitor-mod", "collector_start_check_s": 0}
    (root / "config" / "orchestration.json").write_text(json.dumps({"monitor": monitor}), encoding="utf-8")
    collect = root / COLLECT_REL
    collect.parent.mkdir(parents=True, exist_ok=True)
    collect.write_text("# stand-in for the collector\n", encoding="utf-8")
    return collect


def _write_lock(root: Path, pid: int) -> None:
    lock = root / LOCK_REL
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": pid}), encoding="utf-8")


def _dispatch_lines(root: Path) -> list[str]:
    path = root / "artifacts" / "runs" / "dispatch.out"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _collector_out(root: Path) -> Path:
    path = root / "artifacts" / "runs" / "collector.out"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _start(root: Path, *, cap, calls=None, alive=None) -> str:
    return ensure_collector(root, popen=_popen([] if calls is None else calls), env={},
                            pid_alive=alive or (lambda pid: pid == NEW_PID), sleep=lambda s: None,
                            wait_sleep=lambda s: None, cap=cap)


def test_cap_is_called_once_with_the_launched_pid_and_2_gib(tmp_path: Path):
    _make_mod(tmp_path)
    seen: list = []
    result = _start(tmp_path, cap=lambda pid, limit: seen.append((pid, limit)))

    assert result == "started"
    assert seen == [(NEW_PID, 2 * 1024 ** 3)]
    assert _dispatch_lines(tmp_path) == []


@pytest.mark.parametrize("message", ["jobcap: SetInformationJobObject failed",
                                     "capped, limit not readable: the job name is taken"])
def test_a_cap_message_is_one_log_line_and_the_collector_still_starts(tmp_path: Path, message: str):
    _make_mod(tmp_path)
    result = _start(tmp_path, cap=lambda pid, limit: message)

    assert result == "started"
    lines = _dispatch_lines(tmp_path)
    assert len(lines) == 1, lines
    assert message in lines[0]


def test_a_raising_cap_is_one_log_line_and_the_collector_still_starts(tmp_path: Path):
    _make_mod(tmp_path)

    def boom(pid, limit):
        raise RuntimeError("no job for you")

    result = _start(tmp_path, cap=boom)

    assert result == "started"
    lines = _dispatch_lines(tmp_path)
    assert len(lines) == 1, lines
    assert "no job for you" in lines[0]


def test_off_windows_the_cap_is_not_called(tmp_path: Path, monkeypatch):
    monkeypatch.undo()  # the real platform check, then the platform named below
    monkeypatch.setattr(sys, "platform", "linux")
    seen: list = []
    dispatch_mod._cap_collector(tmp_path, NEW_PID, lambda pid, limit: seen.append(pid))

    assert seen == []
    assert _dispatch_lines(tmp_path) == []


def test_an_injected_launcher_without_an_injected_cap_never_caps(tmp_path: Path, monkeypatch):
    """The default cap is the real job: a test's fake pid must never reach it."""
    _make_mod(tmp_path)
    monkeypatch.setattr(dispatch_mod.jobcap, "cap_process_memory",
                        lambda pid, limit: pytest.fail("the real cap was called"))

    assert _start(tmp_path, cap=None) == "started"


def _tick_dispatcher(root: Path, clock: list, calls: list, alive):
    return SimpleNamespace(
        collector_ensure=lambda: ensure_collector(root, popen=_popen(calls), env={}, pid_alive=alive,
                                                  sleep=lambda s: None, wait_sleep=lambda s: None,
                                                  cap=lambda pid, limit: None),
        clock=lambda: clock[0], _machine_mode_now="normal", log=lambda text: pytest.fail(text))


def test_a_collector_ended_by_memory_error_is_restarted_with_one_line(tmp_path: Path):
    root = tmp_path / "some-repo"
    _make_mod(root)
    _write_lock(root, OLD_PID)  # a dead lock: the old collector is gone
    _collector_out(root).write_text(
        "collect serve: MemoryError: ended at the memory cap\nTraceback (most recent call last):\n"
        '  File "collect.py", line 1, in serve\nMemoryError\n', encoding="utf-8")
    alive_pids = {NEW_PID}
    calls: list = []
    clock = [1000.0]
    dispatcher = _tick_dispatcher(root, clock, calls, lambda pid: pid in alive_pids)

    collector_tick(dispatcher, None)

    assert len(calls) == 1  # one new collector
    assert _dispatch_lines(root)[-1].endswith(RULE_2_LINE.format(repo="some-repo"))
    assert sum(RULE_2_LINE.format(repo="some-repo") in line for line in _dispatch_lines(root)) == 1

    clock[0] += 61.0  # the next due tick: the new collector is live
    _write_lock(root, NEW_PID)
    collector_tick(dispatcher, None)
    assert len(calls) == 1
    assert sum("ended at the 2 GB cap" in line for line in _dispatch_lines(root)) == 1

    alive_pids.clear()  # it dies again, with no MemoryError after the first report: restarted, no second line
    clock[0] += 61.0
    collector_tick(dispatcher, None)
    assert len(calls) == 2
    assert sum("ended at the 2 GB cap" in line for line in _dispatch_lines(root)) == 1


def test_a_dead_collector_without_memory_error_writes_no_cap_line(tmp_path: Path):
    _make_mod(tmp_path)
    _write_lock(tmp_path, OLD_PID)
    _collector_out(tmp_path).write_text("Traceback (most recent call last):\nValueError: nope\n", encoding="utf-8")
    calls: list = []

    assert _start(tmp_path, cap=lambda pid, limit: None, calls=calls) == "started"

    assert len(calls) == 1
    assert not any("2 GB cap" in line for line in _dispatch_lines(tmp_path))


def test_serve_reraises_memory_error_and_removes_the_lock(tmp_path: Path, monkeypatch, capsys):
    import collect

    cache = tmp_path / ".cache"
    monkeypatch.setattr(collect, "CACHE_DIR", cache)
    monkeypatch.setattr(collect, "REPO", tmp_path)

    def boom():
        raise MemoryError

    monkeypatch.setattr(collect, "fast", boom)
    with pytest.raises(MemoryError):
        collect.serve(iterations=1, sleep=lambda s: None)

    assert not (cache / collect.COLLECTOR_LOCK).exists()
    assert "collect serve: MemoryError: ended at the memory cap" in capsys.readouterr().err


MEMORY_ERROR_OUT = ("collect serve: MemoryError: ended at the memory cap\nTraceback (most recent call last):\n"
                    '  File "collect.py", line 1, in serve\nMemoryError\n')


def _cap_lines(root: Path) -> int:
    return sum("ended at the 2 GB cap" in line for line in _dispatch_lines(root))


def test_the_line_is_written_once_across_separate_ensure_processes(tmp_path: Path):
    """Each call stands for a fresh `dispatch --ensure` process: no module state, only collector.out remembers."""
    _make_mod(tmp_path)
    _write_lock(tmp_path, OLD_PID)
    out = _collector_out(tmp_path)
    out.write_text(MEMORY_ERROR_OUT, encoding="utf-8")

    assert _start(tmp_path, cap=lambda pid, limit: None) == "started"
    assert _cap_lines(tmp_path) == 1
    assert dispatch_mod.CAP_END_MARKER in out.read_text(encoding="utf-8").splitlines()

    assert _start(tmp_path, cap=lambda pid, limit: None) == "started"  # the old tail is still in the file
    assert _cap_lines(tmp_path) == 1

    out.write_text(out.read_text(encoding="utf-8") + MEMORY_ERROR_OUT, encoding="utf-8")  # a new end
    assert _start(tmp_path, cap=lambda pid, limit: None) == "started"
    assert _cap_lines(tmp_path) == 2


def test_off_windows_no_cap_line_and_no_marker_is_written(tmp_path: Path, monkeypatch):
    _make_mod(tmp_path)
    out = _collector_out(tmp_path)
    out.write_text(MEMORY_ERROR_OUT, encoding="utf-8")
    monkeypatch.setattr(dispatch_mod, "_caps_apply", lambda: False)

    assert _start(tmp_path, cap=lambda pid, limit: None) == "started"

    assert _cap_lines(tmp_path) == 0
    assert out.read_text(encoding="utf-8") == MEMORY_ERROR_OUT


def test_no_cap_line_when_the_restarted_collector_dies_at_once(tmp_path: Path):
    _make_mod(tmp_path)
    _collector_out(tmp_path).write_text(MEMORY_ERROR_OUT, encoding="utf-8")

    assert _start(tmp_path, cap=lambda pid, limit: None, alive=lambda pid: False) == "died"

    assert _cap_lines(tmp_path) == 0
