"""The one place that starts, lists and stops background `claude` sessions.

Three callers used to carry their own copy of the `claude --bg` argv, the session-id regex and the
`agents --json --all` / `stop <id>` calls: the dispatcher (`dispatch.ClaudeCli`, its ORCH and autopilot
starts) and the anomaly guard's own CLI wrapper. Each caller keeps what is
its own (the name, the agent, the prompt, the logger, the process runner) and hands it in; the shape of
the call and the parsing of what `claude` prints live here, so a fix lands once.

Interface: `build_argv`, `parse_session_id`, `start_session`, `list_sessions`, `stop_session`.
A `run` is any callable with `subprocess.run`'s keyword shape (`run_bounded` is one); with none given,
`subprocess.run` is looked up at call time and the console-less creation flag is added.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from .util import no_window_flags

#: `claude.exe --bg ...` prints this on success; group 1 is the session id. The id may be wrapped in
#: ANSI colour codes ("backgrounded · \x1b[36ma97c7c60\x1b[39m ·", seen in a live smoke): they are skipped.
SESSION_ID_RE = re.compile(r"backgrounded\s*(?:\xb7|·|-)\s*(?:\x1b\[[0-9;]*m)*([\w-]+)")


def build_argv(exe: Path | str, *, name: str, agent: str, prompt: str, effort: str | None = None,
               permission_mode: str = "bypassPermissions", settings: Path | str | None = None) -> list[str]:
    """The exact `claude --bg` command line, as an argv list (never shell-quoted: the caller passes it
    straight to a subprocess). Flag order is fixed: `--effort` (when given) before `--permission-mode`,
    `--settings` (when given) after it, the prompt last."""

    argv = [str(exe), "--bg", "--name", name, "--agent", agent]
    if effort is not None:
        argv += ["--effort", effort]
    argv += ["--permission-mode", permission_mode]
    if settings is not None:
        argv += ["--settings", str(settings)]
    argv.append(prompt)
    return argv


def parse_session_id(stdout: str | None) -> str | None:
    """The session id `claude --bg` printed, ANSI colour codes around it stripped; None when absent."""

    match = SESSION_ID_RE.search(stdout or "")
    return match.group(1) if match else None


def _run_hidden(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """`subprocess.run` with no console window; looked up at call time so a test can replace it."""

    return subprocess.run(argv, creationflags=no_window_flags(), **kwargs)


def _call(run: Callable[..., Any] | None, argv: list[str], cwd: Path | str, timeout: float) -> Any:
    return (run or _run_hidden)(argv, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout)


def start_session(argv: list[str], *, cwd: Path | str, timeout: float, run: Callable[..., Any] | None = None,
                  who: str = "", stderr_chars: int = 200) -> str:
    """Run a `build_argv` command and return the new session's id. `RuntimeError` when `claude` exits
    non-zero (its stderr, cut to `stderr_chars`, in the message) or prints no id. `who` is the caller's
    own words for the message (`" for <key>"`)."""

    result = _call(run, argv, cwd, timeout)
    if result.returncode != 0:
        raise RuntimeError(f"claude --bg{who} failed: {(result.stderr or '').strip()[:stderr_chars]}")
    session_id = parse_session_id(result.stdout)
    if session_id is None:
        raise RuntimeError(f"claude --bg{who}: no session id in {result.stdout!r}")
    return session_id


def list_sessions(exe: Path | str, *, cwd: Path | str, timeout: float, log: Callable[[str], None],
                  run: Callable[..., Any] | None = None) -> list[dict[str, Any]]:
    """`claude agents --json --all` as a list of rows; `[]` (and one `log` line) on any failure."""

    result = _call(run, [str(exe), "agents", "--json", "--all"], cwd, timeout)
    if result.returncode != 0:
        log(f"claude agents --json --all failed: {(result.stderr or '').strip()[:200]}")
        return []
    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        log(f"claude agents --json --all: invalid JSON ({exc})")
        return []
    return data if isinstance(data, list) else []


def stop_session(exe: Path | str, session_id: str, *, cwd: Path | str, timeout: float,
                 log: Callable[[str], None], run: Callable[..., Any] | None = None) -> bool:
    """`claude stop <id>`: True on exit 0, else False (and one `log` line). It marks the conversation
    stopped but does not end the process: callers that need the process gone end it themselves."""

    result = _call(run, [str(exe), "stop", str(session_id)], cwd, timeout)
    if result.returncode != 0:
        log(f"claude stop {session_id} failed: {(result.stderr or '').strip()[:200]}")
        return False
    return True
