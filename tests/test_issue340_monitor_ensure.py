"""#340 unit 2: `ensure_server` (tools/agentops/monitor_ensure.py), the step `dispatch --ensure` calls to keep the
monitor page's server up.

Seams under test (the spec's own): `ensure_server(...)` with every dependency injected. No real launch, no real stop,
no sleeping (a fake `sleep` counts the polls), no real git. One real-socket test covers the default `port_listening`.

    python -m pytest -q -p no:faulthandler tests/test_issue340_monitor_ensure.py
"""
from __future__ import annotations

import json
import socket
from pathlib import Path

from tools.agentops.monitor_ensure import ensure_server, port_listening

PORT = 8750
HEAD_NEW = "bbbbbbbb"
HEAD_OLD = "aaaaaaaa"


class Sleeps:
    """Fake `sleep`: records each wait; the fakes below may read `count` to change state after N polls."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)

    @property
    def count(self) -> int:
        return len(self.waits)


class Recorder:
    """A fake callable that records its calls and returns a fixed value (or raises when given an exception)."""

    def __init__(self, result=None, raises: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self.kwcalls: list[dict] = []
        self.result = result
        self.raises = raises

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        self.kwcalls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.result


class FakeServer:
    """One fake listener: up at first, a stop takes it down (the stopped pids are kept), a launch brings it back."""

    def __init__(self) -> None:
        self.up = True
        self.stopped: list[list[int]] = []

    def listening(self, port: int) -> bool:
        return self.up

    def stopper(self, pids):
        self.stopped.append(list(pids))
        self.up = False
        return []

    def launcher(self, pid: int) -> "_Launcher":
        def launch(argv, *, cwd, out_path):
            self.up = True
            return pid
        return _Launcher(launch)


class _Launcher(Recorder):
    """A recording launch that runs `inner` for its effect and returns its result."""

    def __init__(self, inner) -> None:
        super().__init__()
        self.inner = inner

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        self.kwcalls.append(kwargs)
        return self.inner(*args, **kwargs)


def make_root(tmp_path: Path) -> Path:
    """A fake checkout root: the monitor server file exists, nothing else is needed."""
    root = tmp_path / "checkout"
    (root / "tools" / "agentops").mkdir(parents=True)
    (root / "tools" / "agentops" / "monitor_server.py").write_text("# fake server\n", encoding="utf-8")
    return root


def write_state(path: Path, **fields) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields), encoding="utf-8")


def state_of(root: Path, *, checkout: str | None = None, commit: str = HEAD_OLD, pid: int = 4242) -> dict:
    return {"pid": pid, "checkout": checkout if checkout is not None else str(root), "commit": commit, "port": PORT}


def dispatch_out(root: Path) -> str:
    path = root / "artifacts" / "runs" / "dispatch.out"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_absent_when_server_file_missing(tmp_path, capsys):
    root = tmp_path / "bare"
    root.mkdir()
    listening, stop, launch = Recorder(False), Recorder(), Recorder(1)
    result = ensure_server(root, port=PORT, state_path=root / "s.json", listening=listening, head=Recorder(HEAD_NEW),
                           launch=launch, stop=stop, pid_alive=Recorder(False), sleep=Sleeps(),
                           out_path=root / "m.out")
    assert result == "absent"
    assert listening.calls == [] and stop.calls == [] and launch.calls == []
    assert capsys.readouterr().err == ""


def test_live_with_no_state_touches_nothing(tmp_path):
    root = make_root(tmp_path)
    stop, launch = Recorder(), Recorder(1)
    result = ensure_server(root, port=PORT, state_path=root / "var" / "monitor_server.json",
                           listening=Recorder(True), head=Recorder(HEAD_NEW), launch=launch, stop=stop,
                           pid_alive=Recorder(False), sleep=Sleeps(), out_path=root / "m.out")
    assert result == "live"
    assert stop.calls == [] and launch.calls == []


def test_live_with_state_of_another_checkout_touches_nothing(tmp_path):
    root = make_root(tmp_path)
    state = root / "var" / "monitor_server.json"
    write_state(state, **state_of(root, checkout=str(tmp_path / "other-checkout"), commit=HEAD_OLD))
    stop, launch = Recorder(), Recorder(1)
    result = ensure_server(root, port=PORT, state_path=state, listening=Recorder(True), head=Recorder(HEAD_NEW),
                           launch=launch, stop=stop, pid_alive=Recorder(False), sleep=Sleeps(),
                           out_path=root / "m.out")
    assert result == "live"
    assert stop.calls == [] and launch.calls == []


def test_live_with_same_commit_touches_nothing(tmp_path):
    root = make_root(tmp_path)
    state = root / "var" / "monitor_server.json"
    write_state(state, **state_of(root, commit=HEAD_NEW))
    stop, launch = Recorder(), Recorder(1)
    result = ensure_server(root, port=PORT, state_path=state, listening=Recorder(True), head=Recorder(HEAD_NEW),
                           launch=launch, stop=stop, pid_alive=Recorder(False), sleep=Sleeps(),
                           out_path=root / "m.out")
    assert result == "live"
    assert stop.calls == [] and launch.calls == []


def test_live_with_empty_head_touches_nothing(tmp_path):
    root = make_root(tmp_path)
    state = root / "var" / "monitor_server.json"
    write_state(state, **state_of(root, commit=HEAD_OLD))
    stop, launch = Recorder(), Recorder(1)
    result = ensure_server(root, port=PORT, state_path=state, listening=Recorder(True), head=Recorder(""),
                           launch=launch, stop=stop, pid_alive=Recorder(False), sleep=Sleeps(),
                           out_path=root / "m.out")
    assert result == "live"
    assert stop.calls == [] and launch.calls == []


def test_different_commit_same_checkout_stops_then_starts(tmp_path):
    root = make_root(tmp_path)
    state = root / "var" / "monitor_server.json"
    write_state(state, **state_of(root, commit=HEAD_OLD, pid=4242))
    sleeps = Sleeps()
    server = FakeServer()  # up at first; stop takes it down; a launch brings it back up

    launch = server.launcher(5555)
    result = ensure_server(root, port=PORT, state_path=state, listening=server.listening, head=Recorder(HEAD_NEW),
                           launch=launch, stop=server.stopper, pid_alive=Recorder(False), sleep=sleeps,
                           out_path=root / "m.out")
    assert result == "restarted"
    assert launch.calls == [(["-m", "tools.agentops.monitor_server"],)]
    assert launch.kwcalls[0]["cwd"] == root
    assert launch.kwcalls[0]["out_path"] == root / "m.out"


def test_restart_stop_is_called_with_the_state_pid(tmp_path):
    root = make_root(tmp_path)
    state = root / "var" / "monitor_server.json"
    write_state(state, **state_of(root, commit=HEAD_OLD, pid=4242))
    server = FakeServer()

    launch = server.launcher(5555)
    result = ensure_server(root, port=PORT, state_path=state, listening=server.listening, head=Recorder(HEAD_NEW),
                           launch=launch, stop=server.stopper, pid_alive=Recorder(False), sleep=Sleeps(),
                           out_path=root / "m.out")
    assert result == "restarted"
    assert server.stopped == [[4242]]  # the state's pid, one call
    assert len(launch.calls) == 1


def test_restart_listener_that_never_goes_is_live_with_one_stderr_line(tmp_path, capsys):
    root = make_root(tmp_path)
    state = root / "var" / "monitor_server.json"
    write_state(state, **state_of(root, commit=HEAD_OLD, pid=4242))
    stop, launch, sleeps = Recorder(), Recorder(5555), Sleeps()
    result = ensure_server(root, port=PORT, state_path=state, listening=Recorder(True), head=Recorder(HEAD_NEW),
                           launch=launch, stop=stop, pid_alive=Recorder(True), sleep=sleeps,
                           out_path=root / "m.out", stop_wait_s=10)
    assert result == "live"
    assert stop.calls == [([4242],)]
    assert launch.calls == []
    assert sleeps.count == 20  # 10 s at 0.5 s steps, then gave up
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert err[0].startswith("dispatch --ensure:")


def test_no_listener_starts_once_and_started_after_two_polls(tmp_path):
    root = make_root(tmp_path)
    sleeps = Sleeps()
    launch = Recorder(5555)
    result = ensure_server(root, port=PORT, state_path=root / "var" / "s.json",
                           listening=lambda port: sleeps.count >= 2, head=Recorder(HEAD_NEW),
                           launch=launch, stop=Recorder(), pid_alive=Recorder(False), sleep=sleeps,
                           out_path=root / "m.out", start_wait_s=15)
    assert result == "started"
    assert launch.calls == [(["-m", "tools.agentops.monitor_server"],)]
    assert sleeps.count == 2
    assert all(w == 0.5 for w in sleeps.waits)


def test_listener_never_appears_is_died_with_the_dispatch_out_line(tmp_path, capsys):
    root = make_root(tmp_path)
    out = root / "artifacts" / "runs" / "monitor_server.out"
    out.parent.mkdir(parents=True)

    def launch(argv, *, cwd, out_path):
        out_path.write_text("a\nb\nc\nd\n", encoding="utf-8")  # the server's own last words
        return 5555

    sleeps = Sleeps()
    result = ensure_server(root, port=PORT, state_path=root / "var" / "s.json", listening=lambda port: False,
                           head=Recorder(HEAD_NEW), launch=launch, stop=Recorder(), pid_alive=Recorder(False),
                           sleep=sleeps, out_path=out, start_wait_s=15)
    assert result == "died"
    assert sleeps.count == 30  # 15 s at 0.5 s steps
    line = dispatch_out(root)
    assert "monitor server died after start: b | c | d" in line
    assert capsys.readouterr().err == ""


def test_launch_raises_is_failed_with_one_stderr_line(tmp_path, capsys):
    root = make_root(tmp_path)
    result = ensure_server(root, port=PORT, state_path=root / "var" / "s.json", listening=lambda port: False,
                           head=Recorder(HEAD_NEW), launch=Recorder(raises=RuntimeError("boom")),
                           stop=Recorder(), pid_alive=Recorder(False), sleep=Sleeps(), out_path=root / "m.out")
    assert result == "failed"
    assert capsys.readouterr().err == "dispatch --ensure: monitor server start failed: boom\n"


def test_port_listening_real_socket_true_then_false(tmp_path):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert port_listening(port) is True
    finally:
        server.close()
    # the port is closed now: nothing listens on it
    assert port_listening(port) is False
