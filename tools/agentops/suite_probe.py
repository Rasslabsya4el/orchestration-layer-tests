"""#230: the core's general suite-worker probe. ``python -m tools.agentops.suite_probe`` prints one line
``workers N: ... cap C``: N pytest-xdist workers from the measured free cores and free RAM of this machine.

The count: n = max(1, min(cap, floor(free_cores / 0.5), floor((free_ram_gb - 3.0) / 0.4))), with
free_cores = cores * (100 - cpu%) / 100. No cpu reading -> by RAM only; no RAM reading -> one worker.
Standalone on purpose: it imports nothing of the repo, so the probe starts fast in any target.
"""
from __future__ import annotations

import ctypes
import math
import os
import sys

RESERVE_GB = 3.0        # RAM kept free for the machine itself
PER_WORKER_GB = 0.4     # RAM one pytest worker takes
WORKER_CORES = 0.5      # cores one pytest worker takes
CAP = 6                 # never more workers than this, whatever the machine has
CORE_PROBE_COMMAND = f'"{sys.executable}" -m tools.agentops.suite_probe'


def workers_line(cores: int, cpu_percent: float | None, free_ram_gb: float | None, *,
                 reserve_gb: float = RESERVE_GB, per_worker_gb: float = PER_WORKER_GB,
                 worker_cores: float = WORKER_CORES, cap: int = CAP) -> tuple[int, str]:
    """``(n, line)``: the worker count and the probe line that states it (the line ends ``cap K``)."""
    if free_ram_gb is None:
        return 1, f"workers 1: RAM n/a -> 1 (reader failed), cap {cap}"
    by_ram = max(0, math.floor((free_ram_gb - reserve_gb) / per_worker_gb))
    if cpu_percent is None:
        n = max(1, min(cap, by_ram))
        return n, f"workers {n}: cpu n/a, RAM free {free_ram_gb:.1f} GB -> {by_ram} by RAM, cap {cap}"
    free_cores = cores * (100.0 - cpu_percent) / 100.0
    by_cpu = max(0, math.floor(free_cores / worker_cores))
    n = max(1, min(cap, by_cpu, by_ram))
    return n, (f"workers {n}: cores free {free_cores:.1f} of {cores} (cpu {cpu_percent:.0f}%), "
               f"{worker_cores:g} core a worker -> {by_cpu} by CPU, "
               f"RAM free {free_ram_gb:.1f} GB -> {by_ram} by RAM, cap {cap}")


def _cpu_percent_and_cores() -> tuple[int, float | None]:
    """``(cores, cpu_percent | None)``: psutil when importable, else the OS core count and no cpu reading."""
    try:
        import psutil
    except ImportError:
        return os.cpu_count() or 1, None
    try:
        cpu = float(psutil.cpu_percent(interval=1.0))
    except Exception:  # noqa: BLE001 -- an unreadable cpu means "count by RAM only"
        cpu = None
    cores = psutil.cpu_count(logical=True) or os.cpu_count() or 1
    return cores, cpu


def _free_ram_gb() -> float | None:
    """Free physical RAM in GB: psutil when importable, else GlobalMemoryStatusEx on Windows, else None."""
    try:
        import psutil
        return psutil.virtual_memory().available / 2 ** 30
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 -- an unreadable RAM is a reader failure, not a crash
        return None
    if sys.platform != "win32":
        return None
    try:
        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        stat = _MemoryStatusEx()
        stat.dwLength = ctypes.sizeof(stat)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return None
        return stat.ullAvailPhys / 2 ** 30
    except Exception:  # noqa: BLE001
        return None


def read_machine() -> tuple[int, float | None, float | None]:
    """``(cores, cpu_percent | None, free_ram_gb | None)`` of this machine, read now."""
    cores, cpu = _cpu_percent_and_cores()
    return cores, cpu, _free_ram_gb()


def main() -> int:
    _, line = workers_line(*read_machine())
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
