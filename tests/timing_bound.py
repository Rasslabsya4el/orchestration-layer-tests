"""Load-scaled wall-clock bound for tests (#824, same shape as #823).

bound(floor, factor) = max(floor, factor x a CPU spin timed now). A load that slows the spin slows the timed call
too, so the bound follows the machine; on a quiet machine (spin ~0.15 s) the floor rules.
"""

from __future__ import annotations

import time


def spin_seconds() -> float:
    t = time.perf_counter()
    n = 0
    for i in range(3_000_000):
        n += i
    return time.perf_counter() - t


def bound(floor: float, factor: float) -> float:
    return max(floor, factor * spin_seconds())
