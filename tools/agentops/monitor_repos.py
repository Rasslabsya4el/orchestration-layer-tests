"""#340 unit 10: the monitor page's repos. The page serves this repo and every target of `config/orchestration.json`;
this module names them and reads a target's data through that checkout's own collector.

- `repos(settings)` -> `[{"id", "label", "folder"}]`: this repo first (`settings["repo"]`, folder = the repo root), then
  one entry per `settings["targets"]` item. `id` is the last part of the repo name, `label` the full name.
- `read(entry, mode)` runs `python <folder>/mods/<monitor.source>/bin/collect.py <mode>` with cwd = the folder, where
  `monitor.source` is read from that checkout's own config and must be a bare folder name. The child gets UTF-8 forced
  (PYTHONUTF8, PYTHONIOENCODING); its stdout is decoded as strict UTF-8 and parsed as JSON: that is the data.
  Mode `github` is not a collector mode: a target spawns nothing for it.
  Every failure is a `RepoError` whose text names the repo: not installed, a non-zero exit (with the stderr tail), a
  timeout (`CALL_TIMEOUT_S`, per mode), output that is not UTF-8, output that is not JSON.

The process start is injected (`runner`, default `run`); the server caches the result per (repo, mode) at the mode's
cadence. Nothing here names a repo or a path: the names and folders come from the settings.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from tools.agentops import core_settings
from tools.agentops.util import no_window_flags

#: the longest one collector call may run, per mode (machine values, seconds); past it the answer is `timed out`
CALL_TIMEOUT_S = {"fast": 30, "tickets": 60, "slow": 300, "orch": 300}
GITHUB_MODE = "github"  # the home checkout's own reader (monitor_github); a target has no such collector mode
#: an add-on read `addon:<name>` is the collector's own command `<name>` (#340 unit 8): this bound, seconds
ADDON_TIMEOUT_S = 30
ADDON_PREFIX = "addon:"
#: the only add-on reads served: each one is a cached source and a thread, so the list is closed (a name not here is 404)
ADDON_MODES = ("fast",)
#: the tail of a failed collector's stderr shown in the error text
STDERR_TAIL_CHARS = 200
#: forced on the child: its stdout is UTF-8 whatever the owner's console codepage is
CHILD_ENV = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
#: characters a monitor folder name may never hold: a path separator or a drive letter
_PATH_CHARS = ("/", "\\", ":")

Runner = Callable[[list, str, float, dict], subprocess.CompletedProcess]


class RepoError(Exception):
    """A repo's collector could not give data; `str()` is the error text the envelope shows (it names the repo)."""


def repo_id(name: str) -> str:
    """The id of a repo name: its last path part (`owner/other-repo` -> `other-repo`)."""
    return str(name).rstrip("/").split("/")[-1]


def repos(settings: dict, home_folder: Path = core_settings.ROOT) -> list[dict]:
    """This repo first (folder = `home_folder`, the repo root), then each target of `settings["targets"]`, in order."""
    home = str(settings["repo"])
    listed = [{"id": repo_id(home), "label": home, "folder": str(home_folder)}]
    for target in settings.get("targets") or []:
        name = str(target["repo"])
        listed.append({"id": repo_id(name), "label": name, "folder": str(target["folder"])})
    return listed


def child_python(executable: str | None = None) -> str:
    """The console interpreter for the page server's children: the `python.exe` beside a `pythonw.exe` (a GUI
    interpreter gives its children no console to inherit, so a console program they start opens a visible window);
    any other interpreter unchanged. `executable` defaults to `sys.executable`."""
    exe = sys.executable if executable is None else str(executable)
    path = Path(exe)
    if path.name.lower() == "pythonw.exe":
        console = path.with_name("python.exe")
        if console.is_file():
            return str(console)
    return exe


def run(args: list, cwd: str, timeout: float, env: dict) -> subprocess.CompletedProcess:
    """The real process start: hidden window, the given env, output as bytes (decoded by `read`), bounded by `timeout`
    (raises TimeoutExpired)."""
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, creationflags=no_window_flags(), timeout=timeout)


def human_duration(seconds: float) -> str:
    """A duration for a human reader: seconds under 90, minutes from there on (`300` -> `5 min`)."""
    if seconds < 90:
        return f"{seconds:g} s"
    return f"{seconds / 60:g} min"


def _bare_name(name: str) -> bool:
    """True for a plain folder name: not empty, not `.` or `..`, no separator and no drive colon."""
    return bool(name) and name not in (".", "..") and not any(char in name for char in _PATH_CHARS)


def collector_script(folder: Path, label: str) -> Path | None:
    """The checkout's collector file, named by its own config's `monitor.source`; None when the checkout has no monitor
    (no config, no source, or no collector file there). A source that is not a bare folder name is a `RepoError`."""
    try:
        settings = json.loads((folder / "config" / "orchestration.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    monitor = settings.get("monitor") if isinstance(settings, dict) else None
    source = monitor.get("source") if isinstance(monitor, dict) else None
    if not isinstance(source, str):
        return None
    if not _bare_name(source):
        raise RepoError(f"{label}: monitor.source is not a bare folder name: {source!r}")
    script = folder / "mods" / source / "bin" / "collect.py"
    return script if script.is_file() else None


def addon_name(mode: object) -> str | None:
    """The add-on's collector command of an `addon:<name>` mode: `<name>` when it is one of `ADDON_MODES`; None for
    anything else (#340 unit 8: an unlisted name starts no process and no cached source)."""
    if not isinstance(mode, str) or not mode.startswith(ADDON_PREFIX):
        return None
    name = mode[len(ADDON_PREFIX):]
    return name if name in ADDON_MODES else None


def read(entry: dict, mode: str, *, runner: Runner = run) -> object:
    """One read of `mode` from the repo `entry` (an item of `repos()`): its collector's JSON, or a `RepoError`.
    `mode` is a collector mode, or `addon:<name>` (`addon_name`): the collector runs with `<name>` as its one argument,
    under `ADDON_TIMEOUT_S`, through the same process start as a mode."""
    label = entry["label"]
    if mode == GITHUB_MODE:  # refused before anything is looked up or started
        raise RepoError(f"{label}: github mode is read in the home checkout only")
    name = addon_name(mode)
    if name is None and mode not in CALL_TIMEOUT_S:
        raise RepoError(f"{label}: unknown mode {mode!r}")
    folder = Path(entry["folder"])
    script = collector_script(folder, label)
    if script is None:
        raise RepoError(f"monitor not installed in {label}")
    arg, timeout = (name, ADDON_TIMEOUT_S) if name is not None else (mode, CALL_TIMEOUT_S[mode])
    try:
        done = runner([child_python(), str(script), arg], str(folder), timeout, {**os.environ, **CHILD_ENV})
    except subprocess.TimeoutExpired:
        raise RepoError(f"{label}: timed out after {human_duration(timeout)}") from None
    except OSError as exc:  # the interpreter could not start at all
        raise RepoError(f"{label}: cannot start the collector: {exc}") from None
    if done.returncode != 0:
        try:
            tail = (done.stderr or b"").decode("utf-8").strip()[-STDERR_TAIL_CHARS:]
        except UnicodeDecodeError:
            raise RepoError(f"{label}: exit {done.returncode}: stderr is not UTF-8") from None
        raise RepoError(f"{label}: exit {done.returncode}: {tail}")
    try:
        text = (done.stdout or b"").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RepoError(f"{label}: output is not UTF-8: {exc}") from None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise RepoError(f"{label}: unreadable output: {exc}") from None
