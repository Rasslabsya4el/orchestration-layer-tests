"""#340 unit 1: the monitor page's server. Stdlib only, bound to 127.0.0.1 and nothing else.

    python -m tools.agentops.monitor_server      # serves until stopped (the logon task runs it windowless, see monitor_boot)

- `GET /api/<mode>` for mode in fast|tickets|slow|orch|github -> `{"at_ms", "refresh_s", "data"}`. The data is read by
  a background refresher thread per mode, calling the collector's own mode function (`mods/<monitor.source>/bin/collect.py`:
  `fast()`, `tickets()`, `slow()`, `orch()`), one collector call at a time (COLLECTOR_LOCK: the collector keeps a
  module-level NOW). A read that raises keeps the last good `data` with `error` beside it; `data` is null only when no
  read has ever succeeded, and then `error` says why. HTTP 200 in every case, never an empty list. `github` calls
  `tools.agentops.monitor_github.collect()` when that module exists, else its error is `not built`.
- `GET /api/tabs` -> the ids of the `web/monitor/tab-<id>.js` files, sorted.
- `GET /api/repos` -> `[{"id", "label"}]`: this repo first, then the targets (`monitor_repos.repos`). Every
  `GET /api/<mode>?repo=<id>` with another id reads that checkout's own collector through `monitor_repos.read`, cached
  per (repo, mode) at the mode's cadence; no repo or the home id reads this repo; an unknown id is 404.
- `GET /api/addon:<name>` (`?repo=<id>` as above, the home repo by default) -> the add-on's collector read,
  `collect.py <name>` through `monitor_repos.read`, at fast's cadence. A name that is not one bare word
  (`monitor_repos.addon_name`) is 404 and starts nothing (#340 unit 8).
- `GET /` and every other path -> the static file under `web/monitor/`; a path that leaves that folder is 404.
- A Host header other than 127.0.0.1 or localhost (with or without a port) is 403, so a page of another site cannot
  read the answers through the browser.

Refresh cadence: fast = `monitor.refresh_s` (config, default 2 s), tickets 15 s, slow and orch 10 min, github 10 min.
The collector is loaded from `mods/<monitor.source>/bin/collect.py` (`monitor.source`, a setting the repo's config carries).
The port is the setting `monitor_port` (default 8750). Tests inject a fake collector module and an ephemeral port.
"""
from __future__ import annotations

import importlib.util
import json
import mimetypes
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit

from tools.agentops import core_settings, monitor_repos
from tools.agentops.monitor_repos import RepoError

ROOT = core_settings.ROOT
STATIC_DIR = ROOT / "web" / "monitor"
HOST = "127.0.0.1"  # the loopback address only: never 0.0.0.0, never the LAN address
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")  # the Host header names accepted, without the port
MODES = ("fast", "tickets", "slow", "orch", "github")
#: refresh of each mode in seconds; `fast` takes `monitor.refresh_s` from the settings.
DEFAULT_REFRESH_S = {"fast": 2, "tickets": 15, "slow": 600, "orch": 600, "github": 600}
#: the longest a first request waits for a mode's first read before it answers `not collected yet`.
WAIT_FIRST_S = 5
NOT_BUILT = "not built"
NOT_COLLECTED = "not collected yet"
#: one collector call at a time across all refresher threads: the collector keeps a module-level NOW.
COLLECTOR_LOCK = threading.Lock()
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}


class _NotBuilt(Exception):
    """The mode has no reader yet; its answer is `error: not built`."""


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, _NotBuilt):
        return NOT_BUILT
    if isinstance(exc, RepoError):  # its text already names the repo and the reason
        return str(exc)[:300]
    return f"{type(exc).__name__}: {exc}"[:300]


def _ms(epoch: float) -> int:
    return int(epoch * 1000)


def _envelope(at_ms: int | None, refresh_s: float, data: object, error: str | None = None) -> dict:
    """The one answer shape of every mode: `{at_ms, refresh_s, data}`, with `error` beside `data` when a read failed."""
    doc = {"at_ms": at_ms, "refresh_s": refresh_s, "data": data}
    if error is not None:
        doc["error"] = error
    return doc


class _Source:
    """One mode's cached read: the latest envelope, the last good one, and the event that says a first read has landed."""

    def __init__(self, mode: str, compute: Callable[[], object], refresh_s: float):
        self.mode = mode
        self.compute = compute
        self.refresh_s = refresh_s
        self._lock = threading.Lock()
        self._latest: dict | None = None
        self._good: dict | None = None
        self._ready = threading.Event()

    def refresh(self) -> None:
        started = _ms(time.time())
        try:
            good = _envelope(started, self.refresh_s, self.compute())
        except Exception as exc:  # noqa: BLE001 - a failed read is shown as its error text, never as empty data
            error = _error_text(exc)
            with self._lock:
                last_good = self._good
            if last_good is None:
                envelope = _envelope(started, self.refresh_s, None, error)
            else:  # the last good data stays; its at_ms is when that data was read, so its age shows
                envelope = _envelope(last_good["at_ms"], self.refresh_s, last_good["data"], error)
        else:
            with self._lock:
                self._good = good
            envelope = good
        with self._lock:
            self._latest = envelope
        self._ready.set()

    def read(self) -> dict:
        if not self._ready.wait(WAIT_FIRST_S):
            return _envelope(None, self.refresh_s, None, NOT_COLLECTED)
        with self._lock:
            return dict(self._latest)


class _Refresher(threading.Thread):
    """The cached thread of one mode: reads, stores, waits the refresh (at least 0.2 s), goes on until stopped."""

    def __init__(self, source: _Source):
        super().__init__(name=f"monitor-{source.mode}", daemon=True)
        self.source = source
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            self.source.refresh()
            self.stop_event.wait(max(0.2, self.source.refresh_s - (time.monotonic() - started)))


def tab_ids(folder: Path) -> list[str]:
    """The ids of the `tab-<id>.js` files of `folder`, sorted; an empty folder or a missing one gives []."""
    if not folder.is_dir():
        return []
    ids = []
    for path in folder.glob("tab-*.js"):
        if path.is_file() and len(path.name) > len("tab-.js"):
            ids.append(path.name[len("tab-"):-len(".js")])
    return sorted(ids)


class MonitorServer(ThreadingHTTPServer):
    daemon_threads = True
    #: Windows' SO_REUSEADDR would let a second process take this port from the first; off here.
    allow_reuse_address = False

    def __init__(self, port: int, sources: dict[str, _Source], static_dir: Path,
                 repos: list[dict] | None = None,
                 repo_source: Callable[[dict, str], _Source] | None = None):
        super().__init__((HOST, port), _Handler)
        self.sources = sources
        self.static_dir = Path(static_dir).resolve()
        #: this repo first, then the targets (`monitor_repos.repos`); the first one is the home id
        self.repos = list(repos or [])
        self._repo_source = repo_source
        self._repo_sources: dict[tuple[str, str], _Source] = {}
        self._repo_lock = threading.Lock()
        self.refreshers = [_Refresher(source) for source in sources.values()]

    def start_refreshers(self) -> None:
        for refresher in self.refreshers:
            refresher.start()

    def stop_refreshers(self) -> None:
        for refresher in list(self.refreshers):
            refresher.stop_event.set()

    def home_id(self) -> str | None:
        return self.repos[0]["id"] if self.repos else None

    def repo_entry(self, repo_id: str) -> dict | None:
        return next((entry for entry in self.repos if entry["id"] == repo_id), None)

    def repo_source(self, repo_id: str, mode: str) -> _Source | None:
        """The cached read of `mode` for another repo: created and its refresher started on the first request (a
        thread per (repo, mode), at the mode's cadence). None for an id that is not in the repo list."""
        entry = self.repo_entry(repo_id)
        if entry is None or self._repo_source is None:
            return None
        key = (repo_id, mode)
        with self._repo_lock:
            source = self._repo_sources.get(key)
            if source is None:
                source = self._repo_source(entry, mode)
                self._repo_sources[key] = source
                refresher = _Refresher(source)
                self.refreshers.append(refresher)
                refresher.start()
        return source


def _loopback_host(host_header: str | None) -> bool:
    """True when the Host header names this machine's loopback, with or without a port (case-insensitive)."""
    if not host_header:
        return False
    return host_header.strip().lower().split(":", 1)[0] in LOOPBACK_HOSTS


class _Handler(BaseHTTPRequestHandler):
    server: MonitorServer

    def log_message(self, format: str, *args) -> None:  # the page polls every 2 s: no line per request
        return

    def do_GET(self) -> None:  # noqa: N802 - the http.server name
        if not _loopback_host(self.headers.get("Host")):
            self._send(403, "text/plain; charset=utf-8", b"forbidden")
            return
        parts = urlsplit(self.path)
        path = parts.path
        if path == "/api/tabs":
            self._json(200, tab_ids(self.server.static_dir))
            return
        if path == "/api/repos":
            self._json(200, [{"id": entry["id"], "label": entry["label"]} for entry in self.server.repos])
            return
        if path.startswith("/api/addon:"):  # #340 unit 8: the add-on's read, of this repo or another one (home included)
            mode = path[len("/api/"):]
            if monitor_repos.addon_name(mode) is None:  # a name that is not one bare word starts nothing
                self._json(404, {"error": "unknown addon mode"})
                return
            repo = parse_qs(parts.query).get("repo", [""])[0] or self.server.home_id() or ""
            source = self.server.repo_source(repo, mode)
            if source is None:
                self._json(404, {"error": "unknown repo"})
                return
            self._json(200, source.read())
            return
        if path.startswith("/api/"):
            source = self.server.sources.get(path[len("/api/"):])
            if source is None:
                self._json(404, {"error": "unknown mode"})
                return
            repo = parse_qs(parts.query).get("repo", [""])[0]
            if repo and repo != self.server.home_id():  # no repo, or the home id: this repo, read as before
                source = self.server.repo_source(repo, path[len("/api/"):])
                if source is None:
                    self._json(404, {"error": "unknown repo"})
                    return
            self._json(200, source.read())
            return
        self._static(path)

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body)

    def _static(self, path: str) -> None:
        rel = unquote(path).lstrip("/") or "index.html"
        root = self.server.static_dir
        try:
            target = (root / rel).resolve()
            inside = target.is_relative_to(root) and target.is_file()
        except (OSError, ValueError, RuntimeError):
            inside = False
        if not inside:
            self._send(404, "text/plain; charset=utf-8", b"not found")
            return
        ctype = CONTENT_TYPES.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, ctype, target.read_bytes())

    def _send(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _collector_compute(collector: object, mode: str) -> Callable[[], object]:
    def compute():
        with COLLECTOR_LOCK:  # the refresher threads never overlap inside the collector
            set_now = getattr(collector, "_set_now", None)
            if set_now is not None:  # the collector's NOW is per read, as its own `serve` loop sets it
                set_now(time.time())
            return getattr(collector, mode)()
    return compute


def _github_compute() -> object:
    if importlib.util.find_spec("tools.agentops.monitor_github") is None:  # unit 7 adds this module
        raise _NotBuilt(NOT_BUILT)
    from tools.agentops import monitor_github
    return monitor_github.collect()


def _monitor_settings() -> dict:
    """The `monitor` section of the settings: the one reader `_default_refresh` and `load_collector` both use."""
    return core_settings.load().get("monitor") or {}


def load_collector(root: Path = ROOT) -> ModuleType:
    """The collector module `mods/<monitor.source>/bin/collect.py`, loaded once under the name `collect` (its add-on
    imports it by that name)."""
    if "collect" in sys.modules:
        return sys.modules["collect"]
    source = str(_monitor_settings().get("source") or "")
    if not source:
        raise RuntimeError("monitor.source is not set in the settings")
    path = root / "mods" / source / "bin" / "collect.py"
    spec = importlib.util.spec_from_file_location("collect", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["collect"] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop("collect", None)
        raise
    return module


def _default_refresh() -> dict[str, float]:
    value = _monitor_settings().get("refresh_s")
    fast = value if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else DEFAULT_REFRESH_S["fast"]
    return {**DEFAULT_REFRESH_S, "fast": fast}


def _repo_source_factory(cadence: dict[str, float], runner: monitor_repos.Runner) -> Callable[[dict, str], _Source]:
    """Builds the cached read of one (repo, mode): the repo's own collector through `runner`, at the mode's cadence."""
    def make(entry: dict, mode: str) -> _Source:
        def compute():
            return monitor_repos.read(entry, mode, runner=runner)
        # an add-on read (`addon:<name>`) refreshes at fast's cadence (#340 unit 8)
        at = cadence["fast"] if monitor_repos.addon_name(mode) is not None else cadence[mode]
        return _Source(mode, compute, at)
    return make


def make_server(port: int, *, collector: object | None = None, static_dir: Path | None = None,
                refresh: dict[str, float] | None = None, settings: dict | None = None,
                repo_runner: monitor_repos.Runner | None = None) -> MonitorServer:
    """A started-refreshers server on `127.0.0.1:port` (0 = ephemeral). `collector` defaults to the real collector module;
    tests pass a fake. `refresh` overrides the cadence per mode. `settings` (default: the repo's settings) names the
    repos; `repo_runner` (default `monitor_repos.run`) starts another repo's collector process. Call `serve_forever()`
    to answer requests."""
    cadence = {**_default_refresh(), **(refresh or {})}
    if collector is None:
        collector = load_collector()
    sources: dict[str, _Source] = {}
    for mode in MODES:
        if mode == "github":
            compute = _github_compute
        else:
            compute = _collector_compute(collector, mode)
        sources[mode] = _Source(mode, compute, cadence[mode])
    doc = settings if settings is not None else core_settings.load()
    server = MonitorServer(port, sources, static_dir if static_dir is not None else STATIC_DIR,
                           repos=monitor_repos.repos(doc),
                           repo_source=_repo_source_factory(cadence, repo_runner or monitor_repos.run))
    server.start_refreshers()
    return server


def serve_port() -> int:
    return int(core_settings.load()["monitor_port"])


def main() -> int:
    server = make_server(serve_port())
    try:
        server.serve_forever()
    finally:
        server.stop_refreshers()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
