"""#149: a merge or a core update that changes the monitor installs it in the app.

Two functions cross the seam:

* ``touches_monitor(paths, root)``: True when a repo-relative path (``/`` or ``\\``) starts with ``mods/<source>/``
  or ``mods/shared/``; ``<source>`` is ``install_folder.source_name(root)`` (None: nothing to install, so False).
  An unreadable settings file gives False; ``install_after`` reports that error.
* ``install_after(paths, root, runner=None)``: None when no path touches the monitor (the runner is not called);
  otherwise the runner runs once and the result is one line, ``"monitor installed"`` or
  ``"monitor install failed: <reason>"``. It never raises and never retries. The default runner is the install
  command ``python -m tools.agentops mods install <source>``; it raises when that command exits non-zero, and the
  reason is the command's first non-empty output line.

Stdlib only, like the rest of the core.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable

from .install_folder import source_name
from .util import no_window_flags

INSTALLED = "monitor installed"
FAILED = "monitor install failed: "
SHARED_PREFIX = "mods/shared/"
TIMEOUT_S = 180  # the machine-readable limit; the failed line reads it in minutes

Runner = Callable[[Path, str], None]


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _touched(paths: Iterable[str], source: str | None) -> bool:
    if source is None:
        return False
    prefixes = (SHARED_PREFIX, f"mods/{source}/")
    return any(_norm(path).startswith(prefix) for path in paths for prefix in prefixes)


def touches_monitor(paths: Iterable[str], root: Path) -> bool:
    """True when a path of ``paths`` lies in the monitor's source folder or in ``mods/shared/``; False when the
    settings cannot be read (``install_after`` reports that)."""
    try:
        return _touched(paths, source_name(root))
    except Exception:
        return False


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _reason(exc: BaseException) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"timed out after {TIMEOUT_S // 60} min"
    return _first_line(str(exc)) or type(exc).__name__


def _default_runner(root: Path, source: str) -> None:
    proc = subprocess.run([sys.executable, "-m", "tools.agentops", "mods", "install", source],
                          cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=TIMEOUT_S, creationflags=no_window_flags())
    if proc.returncode != 0:
        raise RuntimeError(_first_line(f"{proc.stdout or ''}\n{proc.stderr or ''}") or f"exit code {proc.returncode}")


def install_after(paths: Iterable[str], root: Path, runner: Runner | None = None) -> str | None:
    """None when ``paths`` do not touch the monitor; else one line for the install run once with ``runner``."""
    run = runner or _default_runner
    try:
        source = source_name(root)
        if not _touched(paths, source):
            return None
        run(root, source)
    except Exception as exc:  # the caller is a merge or a core update: one line, never a raise
        return FAILED + _reason(exc)
    return INSTALLED
