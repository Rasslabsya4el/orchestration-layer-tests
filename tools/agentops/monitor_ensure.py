"""#340 unit 2: keeps the monitor page's server up, the step `dispatch --ensure` calls beside `ensure_collector`.

`ensure_server(root)` returns one word:

- `"absent"`: `root/tools/agentops/monitor_server.py` does not exist; nothing happens.
- `"live"`: a listener is on the port and nothing is to be done (no state file, another checkout, the same commit,
  an empty HEAD); or a listener is on the port, its state is this checkout at an older commit, and the stop did not
  clear it in time (one stderr line).
- `"restarted"`: the old server of this checkout (state pid) was stopped because the checkout's HEAD moved past the
  state's commit, and a new one is listening.
- `"started"`: no listener; the server was started and is listening.
- `"died"`: no listener; the server was started but never listened in time; one `dispatch.out` line carries its
  last lines.
- `"failed"`: anything raised; one stderr line. Never raises.

The state file `var/agentops/monitor_server.json` is written by the server (unit 1) as
`{"pid", "checkout", "commit", "port"}`; this module only reads it. `dispatch` is imported lazily, inside the
function, for its `_launch_detached` (no import cycle with `dispatch`).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from .core_settings import load as _load_settings
from .stop import stop_processes
from .util import pid_alive as _pid_alive
from .worktree import ROOT

SERVER_ARGV = ["-m", "tools.agentops.monitor_server"]
#: the poll step of every wait in this module (seconds)
POLL_S = 0.5
#: the loopback address the server binds, and the bound of one listener probe
_HOST = "127.0.0.1"
_PROBE_TIMEOUT_S = 1.0


def port_listening(port: int) -> bool:
    """True when a TCP connect to 127.0.0.1:`port` succeeds within 1 s (a listener is there)."""
    try:
        with socket.create_connection((_HOST, int(port)), timeout=_PROBE_TIMEOUT_S):
            return True
    except OSError:
        return False


def _git_head(root: Path) -> str:
    """Short sha (8 chars) of `root`'s HEAD; `""` when git fails."""
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--short=8", "HEAD"],
                             capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001 -- an unreadable HEAD is "no head": nothing is restarted
        return ""


def _read_state(path: Path) -> dict[str, Any] | None:
    """The state file as a dict; `None` when it is missing, unreadable or not a JSON object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _same_checkout(recorded: Any, root: Path) -> bool:
    if not isinstance(recorded, str):
        return False
    return os.path.normcase(os.path.abspath(recorded)) == os.path.normcase(os.path.abspath(str(root)))


def _poll(done: Callable[[], bool], seconds: float, wait: Callable[[float], Any]) -> bool:
    """`done()` at once, then every `POLL_S` through `wait`, up to `seconds`; True as soon as it holds."""
    for _ in range(int(seconds / POLL_S)):
        if done():
            return True
        wait(POLL_S)
    return done()


def _lazy_launch() -> Callable[..., Any]:
    from .dispatch import _launch_detached  # lazy: unit 3 makes dispatch import this module, so no top-level import
    return _launch_detached


def _lazy_append_line(root: Path, text: str) -> None:
    from .dispatch import _dispatch_line_or_stderr
    _dispatch_line_or_stderr(root, text)


def _last_lines(path: Path, count: int = 3) -> str:
    """The last `count` lines of `path` joined by ` | `; a missing file says so (one dispatch.out line)."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return f"no {path.name}"
    return " | ".join(lines[-count:]) if lines else f"{path.name} is empty"


def ensure_server(root: Path = ROOT, *, port: int | None = None, state_path: Path | None = None,
                  listening: Callable[[int], bool] | None = None,
                  head: Callable[[Path], str] | None = None,
                  launch: Callable[..., Any] | None = None,
                  stop: Callable[[Sequence[int]], Any] | None = None,
                  pid_alive: Callable[[int], bool] | None = None,
                  sleep: Callable[[float], Any] | None = None,
                  out_path: Path | None = None,
                  start_wait_s: float = 15, stop_wait_s: float = 10) -> str:
    """#340 unit 2 (see the module doc for the returned words). Every dependency defaults to the real one:
    `port` the setting `monitor_port`, `state_path` `root/var/agentops/monitor_server.json`, `out_path`
    `root/artifacts/runs/monitor_server.out`, `listening` `port_listening`, `head` `_git_head`, `launch`
    `dispatch._launch_detached`, `stop` `stop.stop_processes`, `sleep` `time.sleep`.

    Never raises: any exception is one stderr line `dispatch --ensure: monitor server start failed: <err>` and
    `"failed"`."""
    try:
        base = Path(root)
        if not (base / "tools" / "agentops" / "monitor_server.py").is_file():
            return "absent"
        port_n = int(port) if port is not None else int(_load_settings()["monitor_port"])
        state_file = Path(state_path) if state_path is not None else base / "var" / "agentops" / "monitor_server.json"
        is_up = listening if listening is not None else port_listening
        head_of = head if head is not None else _git_head
        alive = pid_alive if pid_alive is not None else _pid_alive
        wait = sleep if sleep is not None else time.sleep
        log = out_path if out_path is not None else base / "artifacts" / "runs" / "monitor_server.out"

        restarted = False
        if is_up(port_n):
            state = _read_state(state_file)
            if state is None or not _same_checkout(state.get("checkout"), base):
                return "live"
            new_head = head_of(base)
            pid = state.get("pid")
            if not new_head or new_head == state.get("commit") or not isinstance(pid, int):
                return "live"
            stopper = stop if stop is not None else stop_processes
            stopper([pid])
            gone = _poll(lambda: not is_up(port_n) and not alive(pid), stop_wait_s, wait)
            if not gone:
                print(f"dispatch --ensure: monitor server pid {pid} did not stop; left running", file=sys.stderr)
                return "live"
            restarted = True

        starter = launch if launch is not None else _lazy_launch()
        starter(list(SERVER_ARGV), cwd=base, out_path=log)
        if _poll(lambda: is_up(port_n), start_wait_s, wait):
            return "restarted" if restarted else "started"
        _lazy_append_line(base, f"monitor server died after start: {_last_lines(log)}")
        return "died"
    except Exception as exc:  # noqa: BLE001 -- the server never changes --ensure's own result
        print(f"dispatch --ensure: monitor server start failed: {exc}", file=sys.stderr)
        return "failed"
