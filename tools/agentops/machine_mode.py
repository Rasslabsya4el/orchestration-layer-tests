"""Machine mode: one file per user, outside every repo, says `play` or `normal`.

`<dir>/machine_mode.json` = {"mode": "play" | "normal", "since": <iso>}; `<dir>` is `AGENTOPS_MACHINE_DIR`, else
`~/.claude/agentops`. Absent file = `normal`. A file that does not parse raises `MachineModeUnreadable`; a loop whose
read raised starts nothing on that tick. Nothing here names a repo or a path of one.

`<dir>/machine_spans.jsonl` is the append-only log of switches; `stood_seconds` reads it.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NamedTuple

MODES = ("play", "normal")
MODE_FILE = "machine_mode.json"
SPAN_LOG = "machine_spans.jsonl"


class MachineModeUnreadable(Exception):
    """The mode file or the span log exists and does not parse."""


def machine_dir() -> Path:
    raw = os.environ.get("AGENTOPS_MACHINE_DIR")
    return Path(raw) if raw else Path.home() / ".claude" / "agentops"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(raw) -> datetime:
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def current_mode() -> str:
    path = machine_dir() / MODE_FILE
    if not path.exists():
        return "normal"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        mode = data["mode"]
        _parse_iso(data["since"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise MachineModeUnreadable(f"{path}: {exc}") from exc
    if mode not in MODES:
        raise MachineModeUnreadable(f"{path}: unknown mode {mode!r}")
    return mode


class ModeRead(NamedTuple):
    """One read of the mode: `mode` is `play` or `normal`, or None when the read raised (`error` holds it)."""

    mode: str | None
    error: BaseException | None = None

    @property
    def holds(self) -> bool:
        """The gate: True in play and when the mode could not be read (a loop then starts nothing)."""
        return self.mode is None or self.mode == "play"


def read_gate(reader: Callable[[], str] | None = None, catch: type | tuple = Exception) -> ModeRead:
    """The one place a loop reads the mode and decides "start nothing". `reader` replaces `current_mode` (a
    test seam); an exception of type `catch` becomes `ModeRead(None, exc)`, any other propagates."""
    try:
        return ModeRead((reader or current_mode)())
    except catch as exc:
        return ModeRead(None, exc)


def set_mode(mode: str, now: datetime | None = None) -> None:
    """Write the mode atomically and, when it changes, append the switch to the span log."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    at = (now or _now()).isoformat()
    directory = machine_dir()
    directory.mkdir(parents=True, exist_ok=True)
    before = current_mode()
    tmp = directory / (MODE_FILE + ".tmp")
    tmp.write_text(json.dumps({"mode": mode, "since": at}), encoding="utf-8")
    os.replace(tmp, directory / MODE_FILE)
    if mode != before:
        with open(directory / SPAN_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"mode": mode, "at": at}) + "\n")


def _spans(now: datetime) -> list[tuple[datetime, datetime]]:
    path = machine_dir() / SPAN_LOG
    if not path.exists():
        return []
    spans: list[tuple[datetime, datetime]] = []
    open_at: datetime | None = None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            at = _parse_iso(rec["at"])
            if rec["mode"] == "play" and open_at is None:
                open_at = at
            elif rec["mode"] == "normal" and open_at is not None:
                spans.append((open_at, at))
                open_at = None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise MachineModeUnreadable(f"{path}: {exc}") from exc
    if open_at is not None:
        spans.append((open_at, max(now, open_at)))
    return spans


def stood_seconds(start: datetime, end: datetime, now: datetime | None = None) -> float:
    """Seconds of [start, end] that lie inside play spans; an open span counts up to `now`."""
    start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
    total = 0.0
    for span_start, span_end in _spans(now or _now()):
        lo, hi = max(start, span_start), min(end, span_end)
        if hi > lo:
            total += (hi - lo).total_seconds()
    return total


def stood_between(start_epoch: float, end_epoch: float) -> float:
    """`stood_seconds` for two epoch times; 0.0 when the log is unreadable or the times are absurd (a watchdog
    then behaves as before the mode existed and never raises because of the mode)."""
    if end_epoch <= start_epoch:
        return 0.0
    try:
        return stood_seconds(datetime.fromtimestamp(start_epoch, timezone.utc),
                             datetime.fromtimestamp(end_epoch, timezone.utc))
    except (MachineModeUnreadable, OverflowError, OSError, ValueError):
        return 0.0


def add_subparser(sub) -> None:
    parser = sub.add_parser("machine", help="the machine mode: play stands the pipeline, normal runs it")
    parser.add_argument("mode", nargs="?", choices=MODES)
    parser.add_argument("--current", action="store_true", help="print the current mode")


def run_first_pass(mode: str) -> None:
    """The switch runs the first hold pass itself (`claude agents` may take up to 60 s)."""
    from . import hold
    done = hold.keeper_pass(mode)
    print(f"hold: suspended {len(done['suspended'])}, resumed {len(done['resumed'])}")


def run_machine_command(args) -> int:
    try:
        if args.mode:
            set_mode(args.mode)
        elif not args.current:
            print("error: give play, normal or --current")
            return 2
        print(current_mode())
        if args.mode:
            run_first_pass(args.mode)
    except MachineModeUnreadable as exc:
        print(f"error: {exc}")
        return 1
    return 0
