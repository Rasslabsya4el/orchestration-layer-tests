"""Monitor snapshots for debugging (#136, part B): every input the Agents tile reads and the rows it drew,
saved in one JSON file per tick, plus a picture of the monitor pane when that pane is visible.

No model call, no token, no alert: a snapshot is read-only (the transcripts under the Claude projects folder,
`claude agents --json --all`, the collector's `shared-fast.json`) and writes only under its own output root.
The rows come from the monitor mod's own `agents()` (loaded by its path, `monitor.source`), so they are the rows
the tile draws. The window lookup and the grab are injectable, so a test needs no desktop.

Layout: `<out_root>/<YYYYMMDD-HHMMSS>/snapshot.json`, and `pane.png` beside it when the picture was taken.
The folder name is the wall clock of the snapshot's `now`; `prune` reads that name back.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import math
import os
import shutil
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from . import monitor_agents as ma
from .core_settings import load as _load_settings
from .dispatch import _TASK_GROUP_RE, _TASK_ID_RE, launch_detached_module
from .stop import exit_code, stop_processes
from .util import holder_is_live, read_pid_lock
from .worktree import main_checkout_root

DEFAULT_OUT_ROOT = ma.ROOT / "var" / "agentops" / "monitor_snapshots"
FOLDER_FORMAT = "%Y%m%d-%H%M%S"
SNAPSHOT_NAME = "snapshot.json"
PICTURE_NAME = "pane.png"
COLLECTOR_MODULE = "monitor_collector"  # the name the collector is loaded under: no entry of sys.modules is taken
TAIL_BYTES = 64 * 1024  # the first read of a subagent file's last record; doubled while no record is found

#: defaults of the three `monitor.snapshot_*` keys of config/orchestration.json (a missing key takes these).
SNAPSHOT_DEFAULTS = {"every_s": 120, "keep_h": 24, "window_title": "Claude"}

Rect = tuple[int, int, int, int]  # left, top, right, bottom: screen pixels
WindowRow = tuple[str, bool, bool, Rect]  # (title, visible, minimized, rect)


# ------------------------------------------------------------------------------------- settings ---

def _monitor_block() -> dict[str, Any]:
    """The `monitor` object of config/orchestration.json; empty when the object is absent."""
    mon = _load_settings().get("monitor")
    return mon if isinstance(mon, dict) else {}


def _positive(value: object, default: float) -> float:
    """A number above zero from the settings, else the default (a bool, a text or a non-finite is no number)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return default
    return value


def snapshot_settings() -> dict[str, Any]:
    """`every_s`, `keep_h` and `window_title` from the `monitor.snapshot_*` keys, read at each call."""
    mon = _monitor_block()
    title = mon.get("snapshot_window_title")
    return {
        "every_s": _positive(mon.get("snapshot_every_s"), SNAPSHOT_DEFAULTS["every_s"]),
        "keep_h": _positive(mon.get("snapshot_keep_h"), SNAPSHOT_DEFAULTS["keep_h"]),
        "window_title": title if isinstance(title, str) and title else SNAPSHOT_DEFAULTS["window_title"],
    }


def _mod_folder() -> Path | None:
    """The monitor mod's folder under `mods/`, named by `monitor.source`; None when no source is set."""
    source = _monitor_block().get("source")
    if not isinstance(source, str) or not source:
        return None
    return ma.ROOT / "mods" / source


def default_pane_cache() -> Path | None:
    """The collector's cache folder of the monitor mod (`bin/.cache`); None when no mod is named."""
    folder = _mod_folder()
    return folder / "bin" / ".cache" if folder is not None else None


# ------------------------------------------------------------------------------------- collector ---

@functools.lru_cache(maxsize=4)
def _load_collector_at(path: str):
    spec = importlib.util.spec_from_file_location(COLLECTOR_MODULE, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the monitor collector: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_collector(path: Path | None = None):
    """The monitor mod's collector (`bin/collect.py`), loaded by its path: its `agents()` draws the Agents tile.
    Raises ImportError when no mod is named (`monitor.source`) or the file is missing."""
    folder = _mod_folder()
    target = path or (folder / "bin" / "collect.py" if folder is not None else None)
    if target is None or not target.is_file():
        raise ImportError(f"no monitor collector file: {target}")
    return _load_collector_at(str(target))


def drawn_rows(raw: list[dict], now: datetime) -> list[dict]:
    """The rows the Agents tile draws from `raw` at `now`: the collector's own `agents(raw=raw)`. The collector's
    clock is set to `now` (aware), so its durations and its live filter read the same instant as the app list."""
    collector = load_collector()
    collector.NOW = now
    return collector.agents(raw=raw)


# ------------------------------------------------------------------------------------- picture ---

def _win32_top_windows() -> list[WindowRow]:
    """(title, visible, minimized, rect) of every top-level window: one EnumWindows pass, titles cut at 512 characters."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    rows: list[WindowRow] = []

    def visit(hwnd: int, _lparam: int) -> bool:
        try:
            size = min(user32.GetWindowTextLengthW(hwnd), 512) + 1
            buffer = ctypes.create_unicode_buffer(size)
            user32.GetWindowTextW(hwnd, buffer, size)
            rect = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            rows.append((buffer.value, bool(user32.IsWindowVisible(hwnd)), bool(user32.IsIconic(hwnd)),
                         (rect.left, rect.top, rect.right, rect.bottom)))
        except Exception:  # noqa: BLE001 - one unreadable window is no reason to stop the pass
            pass
        return True

    user32.EnumWindows(proto(visit), 0)
    return rows


def find_pane(title: str = SNAPSHOT_DEFAULTS["window_title"], *,
              windows: Iterable[WindowRow] | None = None) -> Rect | None:
    """The rectangle of the monitor pane's window, or None when the pane is not visible.

    "Visible" is an approximation: a visible, non-minimised top-level window whose title contains `title`
    (the setting `monitor.snapshot_window_title`). The first such window is taken for the pane; the pane itself
    is not identified, so another window with that text in its title would be taken too. `windows` replaces the
    Win32 pass (tests); a non-Windows host has no such window."""
    if not title:
        return None
    rows = _win32_top_windows() if windows is None else windows
    for name, shown, minimised, rect in rows:
        left, top, right, bottom = rect
        if shown and not minimised and title in name and right > left and bottom > top:
            return rect
    return None


def pil_available() -> bool:
    return importlib.util.find_spec("PIL") is not None


def grab_pane(target: Path, rect: Rect) -> None:
    """The screen inside `rect` (the pane's window), through PIL.ImageGrab, saved as PNG at `target`."""
    from PIL import ImageGrab  # imported only when a picture is taken

    ImageGrab.grab(bbox=rect).save(target, "PNG")


def _take_picture(folder: Path, title: str, rect_fn: Callable[[str], Rect | None] | None,
                  grab_fn: Callable[[Path, Rect], None] | None) -> tuple[str, str | None]:
    """(picture, reason): `pane.png` when the pane was visible and grabbed; else `hidden` and why. Never raises:
    the JSON is saved whatever the picture does."""
    try:
        rect = (rect_fn or find_pane)(title)
        if rect is None:
            return "hidden", None
        if grab_fn is None:
            if not pil_available():
                return "hidden", "no PIL"
            grab_fn = grab_pane
        grab_fn(folder / PICTURE_NAME, rect)
        return PICTURE_NAME, None
    except Exception as exc:  # noqa: BLE001
        return "hidden", f"picture failed: {exc!r}"[:200]


# -------------------------------------------------------------------------------- transcripts ---

def _line_time(raw: bytes | str) -> datetime | None:
    """The `timestamp` of one transcript record line, or None (a partial or non-record line is no time)."""
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    stamp = obj.get("timestamp") if isinstance(obj, dict) else None
    return ma._token_meter().parse_timestamp(stamp) if isinstance(stamp, str) else None


def last_record_time(path: Path) -> datetime | None:
    """The timestamp of the last record of a transcript that has one. Reads from the end, doubling the window
    until a record is found, so a single long line does not hide the record before it."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            window = TAIL_BYTES
            while True:
                start = max(0, size - window)
                fh.seek(start)
                chunk = fh.read(size - start)
                for raw in reversed(chunk.splitlines()):
                    stamp = _line_time(raw)
                    if stamp is not None:
                        return stamp
                if start == 0:
                    return None
                window *= 2
    except OSError:
        return None


def _agent_meta(path: Path) -> dict[str, Any]:
    """The `<agent>.meta.json` beside a subagent transcript, read the way the collector reads it; {} when absent or bad."""
    meta_path = path.parent / (path.stem + ".meta.json")
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def subagent_files(projects_dir: Path, session_id: str) -> list[dict[str, Any]]:
    """Each subagent file of a session, by name, with the time of its last record (None when it has none), and the
    request shape and tool_use id of its meta (a foreground helper is ended by its tool_result in the parent)."""
    folder = projects_dir / session_id / "subagents"
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("agent-*.jsonl")):
        stamp = last_record_time(path)
        meta = _agent_meta(path)
        out.append({"file": path.name, "agent_id": path.stem.removeprefix("agent-"),
                    "last_record_at": stamp.isoformat() if stamp else None,
                    "request_shape": meta.get("requestShape"), "tool_use_id": meta.get("toolUseId")})
    return out


def notifications(parent: Path) -> list[dict[str, Any]]:
    """Every task id a `<task-notification>` of the parent transcript names, with its status and the time of the
    record it sits in. Read the way dispatch's `ended_task_ids` reads them (the raw line, all record shapes)."""
    out: list[dict[str, Any]] = []
    if not parent.is_file():
        return out
    with parent.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "task-notification" not in line:
                continue
            stamp = _line_time(line)
            # one group of ids shares one status (#136): the group regex, as `ended_task_ids` reads it
            for ids, status in _TASK_GROUP_RE.findall(line):
                for task_id in _TASK_ID_RE.findall(ids):
                    out.append({"task_id": task_id.strip(), "status": status.strip(),
                                "timestamp": stamp.isoformat() if stamp else None})
    return out


def pane_refresh(cache_dir: Path | None) -> tuple[str | None, str, str | None]:
    """(pane_refresh_time, pane_refresh_source, pane_refresh_mtime) of the collector's `shared-fast.json`.
    The time is the `at` its fast-mode result carries (HH:MM:SS, no date); the mtime is the file's own time, ISO
    local, and dates it. Each is None when absent; the source is "shared-fast.json" when the `at` is there."""
    if cache_dir is None:
        return None, "none", None
    path = cache_dir / "shared-fast.json"
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        return None, "none", None
    try:
        at = json.loads(path.read_text(encoding="utf-8")).get("at")
    except (OSError, ValueError, AttributeError):
        at = None
    if isinstance(at, str):
        return at, "shared-fast.json", mtime
    return None, "none", mtime


# ------------------------------------------------------------------------------------ mismatch ---
# #136: the helper states the Agents tile drew, against the app-side state, both taken from one snapshot.
#
# The app list (`claude agents --json --all`) holds no per-helper data. So the app side is derived from two things
# only: the app session list (a parent that is absent or done/stopped/failed ends its helpers) and transcript facts
# read on their own, not from the tile's drawing: the parent's `<task-notification>` ids and times, each helper's
# own last record time and its `.meta.json` request shape, and the parent's answered tool_use ids. The rules are
# those of `monitor_agents` (RESUME_MARGIN, the resume of a helper, a foreground helper ended by its tool_result).
#
# `mismatches` is the per-snapshot list: the helpers whose drawn state differs from the derived one, each with a
# `cause`. `mismatches_confirmed` is what counts: a mismatch the newest earlier snapshot of the same out_root holds
# too, taken no more than CONFIRM_WITHIN_INTERVALS loop intervals before this one (two snapshots in a row).

_ENDED_APP_STATES = ("done", "stopped", "failed")
CONFIRM_WITHIN_INTERVALS = 3
CAUSE_PARENT = "parent session ended in the app"
CAUSE_NOTIFIED = "ended notification newer than last record"
CAUSE_NO_TIME = "ended notification has no time"
CAUSE_FOREGROUND = "foreground tool_result answered"
CAUSE_NO_NOTICE = "no ended notification"
CAUSE_RESUMED = "record newer than its ended notification"
MISMATCH_KEYS = ("session", "helper", "tile", "app")


def _when(value: object) -> datetime | None:
    """An ISO time of a snapshot as an aware datetime (a naive one reads as UTC), or None."""
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp


def _app_state(parent: dict | None, session: dict[str, Any], agent_id: str) -> tuple[str, str]:
    """(`running` or `ended`, cause) for one helper, the way `monitor_agents.build_subagent_row` ends it: a parent
    absent from the app list, or done/stopped/failed, ends it; a helper no ended notification names is running; a
    named one is ended unless its own last record is later than its last ended notification by more than
    `ma.RESUME_MARGIN` (a resume; a notification with no time never resumes); then a foreground helper whose
    tool_use the parent answered is ended."""
    if parent is None or parent.get("state") in _ENDED_APP_STATES:
        return "ended", CAUSE_PARENT
    files = [f for f in session.get("subagent_files", []) if f.get("agent_id") == agent_id]
    entry = files[-1] if files else {}
    cause = CAUSE_NO_NOTICE
    if agent_id in session.get("ended_task_ids", []):
        ends = [n for n in session.get("notifications", [])
                if n.get("task_id") == agent_id and n.get("status") != "running"]
        ended_at = _when(ends[-1].get("timestamp")) if ends else None  # the file's last ended notification, as ma reads it
        if ended_at is None:
            return "ended", CAUSE_NO_TIME
        last = _when(entry.get("last_record_at"))
        if last is None or last - ended_at <= ma.RESUME_MARGIN:
            return "ended", CAUSE_NOTIFIED
        cause = CAUSE_RESUMED
    if entry.get("request_shape") == "foreground" and entry.get("tool_use_id") in session.get("answered_tool_use_ids", []):
        return "ended", CAUSE_FOREGROUND
    return "running", cause


def mismatches(rows: list[dict], app_agents: list[dict], per_session: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per helper the tile draws in a state the app-side state does not give: `tile` is `running` when the
    row's subagent is `busy`, else `ended`; `app` is `_app_state`. Same instant: all three inputs come from one snapshot."""
    parents = {}
    for app in app_agents:
        sid = (app.get("sessionId") or app.get("id")) if isinstance(app, dict) else None
        if sid:
            parents[sid] = app
    out: list[dict[str, Any]] = []
    for row in rows:
        sid = row.get("sid")
        if not sid:
            continue
        session = per_session.get(sid) or {}
        for sub in row.get("subagents") or []:
            tile = "running" if sub.get("busy") else "ended"
            app, cause = _app_state(parents.get(sid), session, sub.get("id") or "")
            if tile != app:
                out.append({"session": sid, "helper": sub.get("id") or "", "tile": tile, "app": app, "cause": cause})
    return out


def _newest_earlier(out_root: Path, now: datetime, every_s: float) -> dict[str, Any] | None:
    """The snapshot.json of the newest folder of `out_root` named before `now` (the names are times, so the string
    order is the time order), when that folder is no more than CONFIRM_WITHIN_INTERVALS x `every_s` before `now`;
    None when there is none, it is older, or it does not read."""
    if not out_root.is_dir():
        return None
    name = now.strftime(FOLDER_FORMAT)
    earlier: list[str] = []
    for folder in out_root.iterdir():
        if not folder.is_dir() or folder.name >= name or not (folder / SNAPSHOT_NAME).is_file():
            continue
        try:
            datetime.strptime(folder.name, FOLDER_FORMAT)
        except ValueError:
            continue
        earlier.append(folder.name)
    if not earlier:
        return None
    newest = max(earlier)
    age = now.replace(tzinfo=None) - datetime.strptime(newest, FOLDER_FORMAT)
    if age > timedelta(seconds=CONFIRM_WITHIN_INTERVALS * every_s):
        return None
    try:
        data = json.loads((out_root / newest / SNAPSHOT_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _key(mismatch: dict[str, Any]) -> tuple:
    return tuple(mismatch.get(k) for k in MISMATCH_KEYS)


def confirmed(found: list[dict[str, Any]], earlier: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The mismatches of this snapshot that the earlier snapshot holds too: same session, helper, tile and app (the
    cause is not part of the match)."""
    held = earlier.get("mismatches") if earlier else None
    if not isinstance(held, list):
        return []
    held_keys = {_key(m) for m in held if isinstance(m, dict)}
    return [m for m in found if _key(m) in held_keys]


# ----------------------------------------------------------------------------------- snapshot ---

def take_snapshot(out_root: Path = DEFAULT_OUT_ROOT, *, now: datetime | None = None,
                  live_sessions_raw: Callable[[], list[dict]] | None = None,
                  agents_fn: Callable[[list[dict]], list[dict]] | None = None,
                  projects_dir: Path | None = None, pane_cache: Path | None = None,
                  window_title: str | None = None,
                  pane_rect_fn: Callable[[str], Rect | None] | None = None,
                  grab_fn: Callable[[Path, Rect], None] | None = None,
                  every_s: float | None = None) -> Path:
    """Save one snapshot under `out_root` and return the path of its `snapshot.json`.

    The app list is read first (`claude agents --json --all`); the rows the Agents tile draws come from the
    collector's `agents()` fed with that same list, the collector's clock set to `now`; then the subagent files and
    the notifications of each row's session, then the pane's picture and the collector's cache stamp.

    Not one atomic read: these inputs are read one after another within a few seconds, so a row can be a few seconds
    older or newer than the app list, and the transcripts a few seconds apart from both. Every argument is injectable
    for a test; the defaults are the installation's own."""
    now = now or datetime.now()
    aware = now.astimezone(timezone.utc)  # naive = local wall clock; the collector compares aware times
    raw = (live_sessions_raw or ma.live_sessions_raw)()
    rows = (agents_fn or (lambda app: drawn_rows(app, aware)))(raw)

    projects = projects_dir or ma.DEFAULT_PROJECTS_DIR
    per_session: dict[str, Any] = {}
    for row in rows:
        sid = row.get("sid")
        if not sid:
            continue
        parent = projects / f"{sid}.jsonl"
        per_session[sid] = {
            "subagent_files": subagent_files(projects, sid),
            "notifications": notifications(parent),
            "ended_task_ids": sorted(ma.completed_agent_ids(parent)),
            "answered_tool_use_ids": sorted(ma.answered_tool_use_ids(parent)),
        }

    name = now.strftime(FOLDER_FORMAT)
    found = mismatches(rows, raw, per_session)
    interval = every_s if every_s is not None else snapshot_settings()["every_s"]
    held = confirmed(found, _newest_earlier(out_root, now, interval))

    folder = out_root / name
    folder.mkdir(parents=True, exist_ok=True)
    title = window_title or snapshot_settings()["window_title"]
    picture, reason = _take_picture(folder, title, pane_rect_fn, grab_fn)
    pane_time, pane_source, pane_mtime = pane_refresh(pane_cache if pane_cache is not None else default_pane_cache())

    data: dict[str, Any] = {
        "taken_at": aware.isoformat(timespec="seconds"),
        "app_agents": raw,
        "rows": rows,
        "per_session": per_session,
        "mismatches": found,
        "mismatches_confirmed": held,
        "pane_refresh_time": pane_time,
        "pane_refresh_source": pane_source,
        "pane_refresh_mtime": pane_mtime,
        "picture": picture,
    }
    if reason is not None:
        data["picture_reason"] = reason
    path = folder / SNAPSHOT_NAME
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return path


def prune(out_root: Path, *, now: datetime, keep_hours: float) -> list[str]:
    """Remove the snapshot folders whose name time is older than `keep_hours` before `now`; a folder whose name is
    not a snapshot time is left alone. Returns the names removed."""
    cutoff = now.replace(tzinfo=None) - timedelta(hours=keep_hours)
    removed: list[str] = []
    if not out_root.is_dir():
        return removed
    for folder in sorted(out_root.iterdir()):
        if not folder.is_dir():
            continue
        try:
            when = datetime.strptime(folder.name, FOLDER_FORMAT)
        except ValueError:
            continue
        if when < cutoff:
            shutil.rmtree(folder)
            removed.append(folder.name)
    return removed


def _one_round(out_root: Path, snap: Callable[..., Path], keep_hours: float,
               log: Callable[[str], None], kwargs: dict[str, Any]) -> None:
    """One snapshot and one prune. A failed snapshot is one log line; the loop goes on."""
    try:
        path = snap(out_root, **kwargs)
        log(f"monitor snapshot: {path}")
    except Exception as exc:  # noqa: BLE001
        log(f"monitor snapshot failed: {exc!r}"[:300])
    try:
        prune(out_root, now=datetime.now(), keep_hours=keep_hours)
    except OSError as exc:
        log(f"monitor snapshot prune failed: {exc!r}"[:300])


def run_loop(out_root: Path = DEFAULT_OUT_ROOT, *, every_s: float | None = None,
             keep_hours: float | None = None, stop_event: threading.Event | None = None,
             sleep: Callable[[float], object] | None = None, take: Callable[..., Path] | None = None,
             log: Callable[[str], None] = print, **snapshot_kwargs: Any) -> None:
    """Take a snapshot and prune, then wait `every_s`, until `stop_event` is set. The defaults are the
    `monitor.snapshot_*` settings; the wait is `stop_event.wait` unless a `sleep` is injected."""
    settings = snapshot_settings()
    every = settings["every_s"] if every_s is None else every_s
    keep = settings["keep_h"] if keep_hours is None else keep_hours
    stop = stop_event or threading.Event()
    wait = sleep or (lambda seconds: stop.wait(seconds))
    snap = take or take_snapshot
    while not stop.is_set():
        _one_round(out_root, snap, keep, log, snapshot_kwargs)
        if stop.is_set():
            break
        wait(every)


# ------------------------------------------------------------------------------------ lifecycle ---
# #136 part B: one snapshot loop at a time, started and stopped by one command, launched hidden, and
# started again by every `dispatch --ensure` tick while it is not live. No alert, no ticket, no model call.

LOCK_PATH = main_checkout_root(ma.ROOT) / "var" / "agentops" / "monitor_snapshots.lock"
SNAPSHOT_OUT = ma.ROOT / "artifacts" / "runs" / "monitor_snapshots.out"
_LOCK_ATTEMPTS = 3


def lock_status(lock_path: Path = LOCK_PATH) -> tuple[bool, int | None]:
    """``(live, pid)`` of whoever holds the snapshot lock; a dead or reused holder reads as not live
    (`holder_is_live`, the same reading the dispatcher's lock uses)."""
    holder = read_pid_lock(lock_path)
    pid = holder.get("pid") if holder else None
    started = holder.get("started") if holder else None
    return (holder_is_live(pid, started), pid)


def _acquire(lock_path: Path) -> bool:
    """Take the lock for this process. A live holder refuses at once; a stale file is removed and the
    creation retried (bounded, so a file that cannot be removed does not spin)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(_LOCK_ATTEMPTS):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if lock_status(lock_path)[0]:
                return False
            try:
                lock_path.unlink()
            except OSError:
                pass
            continue
        try:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            os.write(fd, json.dumps({"pid": os.getpid(), "started": stamp}).encode())
        finally:
            os.close(fd)
        return True
    return False


def _release(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except OSError:
        pass


def run_locked(*, lock_path: Path = LOCK_PATH, out_root: Path = DEFAULT_OUT_ROOT,
               loop: Callable[..., None] | None = None, **loop_kwargs: Any) -> int:
    """`monitor-snapshots run`: the foreground loop (`run_loop`) under its own lock, removed on exit. A
    second run while the holder is live prints `live pid N` and exits 0 at once, without looping."""
    if not _acquire(lock_path):
        print(f"live pid {lock_status(lock_path)[1]}")
        return 0
    try:
        (loop or run_loop)(out_root, **loop_kwargs)
    finally:
        _release(lock_path)
    return 0


def ensure_snapshots(*, lock_path: Path = LOCK_PATH, out_path: Path = SNAPSHOT_OUT,
                     launch: Callable[..., int] | None = None, prefix: str | None = None) -> int:
    """Starts the loop hidden when no holder is live (`launch(args, out_path=...)`, default
    `launch_detached_module`, args `["monitor-snapshots", "run"]`). A failed start is one stderr line and rc 1.
    `prefix` None (the CLI `start`): prints `live pid N` / `started pid N`. Given (the `dispatch --ensure`
    tick): prints nothing when live and `<prefix>: started pid N` when it started one, so the line never
    reads as the dispatcher's own."""
    alive, pid = lock_status(lock_path)
    if alive:
        if prefix is None:
            print(f"live pid {pid}")
        return 0
    spawn = launch or launch_detached_module
    try:
        new_pid = spawn(["monitor-snapshots", "run"], out_path=out_path)
    except OSError as exc:
        print(f"monitor snapshots: failed to start: {exc}", file=sys.stderr)
        return 1
    print(f"started pid {new_pid}" if prefix is None else f"{prefix}: started pid {new_pid}")
    return 0


def stop_snapshots(*, lock_path: Path = LOCK_PATH,
                   stopper: Callable[[list[int]], list] | None = None) -> int:
    """`monitor-snapshots stop`: stops the lock holder's pid through `stop.stop_processes` (never by name);
    `stopper` replaces it for a test. Says `not live` and stops nothing when no holder is live."""
    alive, pid = lock_status(lock_path)
    if not alive:
        print("monitor snapshots: not live")
        return 0
    outcomes = (stopper or stop_processes)([pid])
    return exit_code(outcomes)


def newest_snapshot(out_root: Path = DEFAULT_OUT_ROOT) -> str | None:
    """The name of the newest snapshot folder (its name is its time, so the largest name is the newest)."""
    names: list[str] = []
    if out_root.is_dir():
        for folder in out_root.iterdir():
            if not folder.is_dir():
                continue
            try:
                datetime.strptime(folder.name, FOLDER_FORMAT)
            except ValueError:
                continue
            names.append(folder.name)
    return max(names) if names else None


def status_text(*, lock_path: Path = LOCK_PATH, out_root: Path = DEFAULT_OUT_ROOT) -> str:
    alive, pid = lock_status(lock_path)
    state = f"live pid {pid}" if alive else "not live"
    return f"monitor snapshots: {state}; newest snapshot: {newest_snapshot(out_root) or 'none'}"


def add_subparser(sub) -> None:
    parser = sub.add_parser("monitor-snapshots",
                            help="the monitor snapshots loop: start | stop | status | run (#136 part B)")
    parser.add_argument("action", choices=["start", "stop", "status", "run"])


def run_command(args) -> int:
    if args.action == "start":
        return ensure_snapshots()
    if args.action == "stop":
        return stop_snapshots()
    if args.action == "status":
        print(status_text())
        return 0
    return run_locked()
