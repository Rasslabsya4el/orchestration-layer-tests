"""CPU the always-running monitor collector adds, per `monitor.refresh_s` (#142 row 3).

For each refresh interval: set `monitor.refresh_s` in this checkout's config, start `collect.py serve` hidden, let
4 simulated panes read `collector-fast.json` every interval, and poll the collector's whole process tree (children
included) every 50 ms, keeping each pid's highest user+system CPU time seen. The result is that CPU as a share of
the whole machine (all logical cores). The config is restored at the end.

    python scripts/monitor_cpu_probe.py --refresh 1 2 5 --minutes 10 [--panes 4]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "orchestration.json"
COLLECT = ROOT / "mods" / "pob-slots" / "bin" / "collect.py"
OUT = ROOT / "mods" / "pob-slots" / "bin" / ".cache" / "collector-fast.json"
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _tree_cpu(root_pid: int, seen: dict[int, float]) -> None:
    try:
        procs = [psutil.Process(root_pid)]
        procs += procs[0].children(recursive=True)
    except psutil.Error:
        return
    for p in procs:
        try:
            t = p.cpu_times()
            seen[p.pid] = max(seen.get(p.pid, 0.0), t.user + t.system)
        except psutil.Error:
            pass


def _pane(stop: threading.Event, every_s: float, reads: list[int]) -> None:
    while not stop.wait(every_s):
        try:
            json.loads(OUT.read_text(encoding="utf-8"))
            reads[0] += 1
        except (OSError, ValueError):
            pass


LOCK = OUT.with_name("collector.lock")


def _refuse_if_collector_live() -> None:
    """A live collector already holds the lock: a second `serve` would exit at once and the table would read 0."""
    try:
        pid = json.loads(LOCK.read_text(encoding="utf-8"))["pid"]
    except (OSError, ValueError, KeyError, TypeError):
        return
    if psutil.pid_exists(pid):
        raise SystemExit(f"a collector (pid {pid}) is already running; stop it first")


def measure(refresh_s: float, seconds: float, panes: int) -> dict:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    cfg["monitor"]["refresh_s"] = refresh_s
    CONFIG.write_bytes(json.dumps(cfg, indent=2).encode("utf-8"))
    proc = subprocess.Popen([sys.executable, str(COLLECT), "serve"], cwd=ROOT, creationflags=NO_WINDOW,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stop, reads = threading.Event(), [0]
    threads = [threading.Thread(target=_pane, args=(stop, refresh_s, reads), daemon=True) for _ in range(panes)]
    seen: dict[int, float] = {}
    try:
        time.sleep(15)  # warm-up: first computation, imports
        for t in threads:
            t.start()
        seen.clear()
        start = time.time()
        while time.time() - start < seconds:
            _tree_cpu(proc.pid, seen)
            time.sleep(0.05)
        wall = time.time() - start
    finally:
        stop.set()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    cpu_s = sum(seen.values())
    return {"refresh_s": refresh_s, "wall_s": round(wall), "tree_cpu_s": round(cpu_s, 1),
            "pct_of_machine": round(100 * cpu_s / (wall * psutil.cpu_count()), 2), "pane_reads": reads[0]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", type=float, nargs="+", default=[1, 2, 5])
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--panes", type=int, default=4)
    args = ap.parse_args()
    _refuse_if_collector_live()
    original = CONFIG.read_bytes()
    try:
        for r in args.refresh:
            print(json.dumps(measure(r, args.minutes * 60, args.panes)), flush=True)
    finally:
        CONFIG.write_bytes(original)
    print(f"cores: {psutil.cpu_count()}")


if __name__ == "__main__":
    main()
