"""#265: the pure decision "is an ORCH session silent" (no clock, no file, no process; the caller passes every time).

A session is silent when neither it nor any of its still-running subagents wrote a timed record for ``limit_seconds``.
``silence_verdict`` returns what to do about it: ``"none"`` (nothing), ``"wake"`` (send the dispatcher's wake once),
``"end"`` (the session is finished with). The shells are carried into the verdict for the log line only."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class SilenceVerdict:
    action: str  # "none" | "wake" | "end"
    silent_seconds: float
    shells: tuple[str, ...]


def silent_orch_limit_seconds(settings: dict) -> float:
    return float(settings.get("silent_orch_minutes", 30)) * 60


def silence_verdict(
    *,
    now: float,
    own_last_record: float,
    subagent_last_records: Sequence[float],
    finishing: bool,
    woken_at: float | None,
    limit_seconds: float,
    shells: Iterable[str] = (),
) -> SilenceVerdict:
    last = max((own_last_record, *subagent_last_records))
    silent = now - last
    carried = tuple(sorted(shells))
    if woken_at is not None and last > woken_at:
        woken_at = None  # the wake was answered: a silence after it is a new one and is woken again
    if silent < limit_seconds:
        action = "none"
    elif finishing:
        action = "end"
    elif woken_at is None:
        action = "wake"
    elif now - woken_at >= limit_seconds:
        action = "end"
    else:
        action = "none"
    return SilenceVerdict(action=action, silent_seconds=silent, shells=carried)
