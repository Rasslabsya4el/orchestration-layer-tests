"""Duration formatting for the core (issue #147, half b). Stdlib only.

The same rendering as the product's own ``report_format.human_duration`` (durations are
written for a human, never a bare count of seconds), kept here as its own module so the
core has no import back into ``src/``.
"""

from __future__ import annotations

import math


def _one_decimal(value: float) -> str:
    """One decimal, with a zero fraction dropped (2.0 -> "2")."""

    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


def human_duration(seconds: float | None) -> str:
    """Format a duration with a unit a person can readily read."""

    if seconds is None or not math.isfinite(seconds):
        return "-"
    sign = "-" if seconds < 0 else ""
    value = abs(seconds)
    if value < 10:
        return f"{sign}{_one_decimal(value)} s"
    if value < 90:
        return f"{sign}{value:.0f} s"
    minutes = value / 60
    if minutes < 10:
        return f"{sign}{_one_decimal(minutes)} min"
    if minutes < 90:
        return f"{sign}{minutes:.0f} min"
    hours, remaining_minutes = divmod(int(round(minutes)), 60)
    return f"{sign}{hours} h {remaining_minutes:02d} min"
