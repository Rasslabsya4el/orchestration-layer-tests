"""GitHub GraphQL budget sampler (#1113).

`python scripts/gh_budget_sampler.py --out <file.jsonl> [--every-minutes 10] [--hours 3] [--once]`
appends one JSON line per sample: `{"t", "used", "remaining", "limit", "reset_at"}`, or
`{"t", "error"}` when the read fails (the loop goes on, no tight retry: the next sample waits its interval).

`python scripts/gh_budget_sampler.py --summary <file.jsonl>` prints the points used per resetAt window.
Points of a window are its last `used` minus its first `used`. The first window is a lower bound: it may
have begun before the first sample, and that part is not counted. Error lines are skipped.

The loop sleeps in 1 s slices up to each interval and stops once `--hours` is spent. Starts no PoB and no
console window (`run_gh` starts `gh` with `no_window_flags`).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.agentops.core_format import human_duration  # noqa: E402
from tools.agentops.gh_call import run_gh  # noqa: E402

QUERY = "query={ rateLimit { limit remaining used resetAt } }"
SLICE_SECONDS = 1.0


def _first_line(text: object) -> str:
    if not isinstance(text, str):
        return ""
    stripped = text.strip()
    return stripped.splitlines()[0] if stripped else ""


def read_budget() -> dict:
    """One rateLimit read through `gh api graphql`; raises with the first line of stderr on failure."""
    result = run_gh(["api", "graphql", "-f", QUERY])
    if result.returncode:
        raise RuntimeError(_first_line(result.stderr) or _first_line(result.stdout)
                           or f"gh exit {result.returncode}")
    rate = json.loads(result.stdout)["data"]["rateLimit"]
    return {"used": rate["used"], "remaining": rate["remaining"], "limit": rate["limit"],
            "reset_at": rate["resetAt"]}


def sample_line() -> dict:
    """One sample row: the budget, or the error line when the read fails (never raises)."""
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        return {"t": stamp, **read_budget()}
    except Exception as exc:  # noqa: BLE001 - a failed read is one error line, the loop goes on
        return {"t": stamp, "error": _first_line(str(exc)) or type(exc).__name__}


def append(path: Path, row: dict) -> None:
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def run_loop(out: Path, every_minutes: float, hours: float) -> None:
    """Sample now, then every `every_minutes`, stopping once the next sample would fall past `hours`."""
    end = time.monotonic() + hours * 3600
    while True:
        append(out, sample_line())
        wake = time.monotonic() + every_minutes * 60
        if wake >= end:
            return
        while True:
            left = wake - time.monotonic()
            if left <= 0:
                break
            time.sleep(min(SLICE_SECONDS, left))


def _windows(rows: list[dict]) -> list[dict]:
    """Group the budget rows (error lines skipped) by resetAt, in order of first appearance."""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        if "used" in row:
            groups.setdefault(row["reset_at"], []).append(row)
    return [{"start": members[0]["t"], "first_t": members[0]["t"], "last_t": members[-1]["t"],
             "count": len(members), "points": members[-1]["used"] - members[0]["used"]}
            for members in groups.values()]


def hourly_points(samples: list[dict]) -> list[tuple[str, int]]:
    """(window_start_iso, points_used) per resetAt window.

    points_used = last `used` minus first `used` of the window; window_start_iso is the `t` of the window's
    first sample. The first window is a lower bound: it may have begun before the first sample, and the
    part before that sample is not counted. Error lines are skipped.
    """
    return [(w["start"], w["points"]) for w in _windows(samples)]


def _read_rows(path: Path) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def summary_text(path: Path) -> str:
    rows = _read_rows(path)
    lines = []
    for w in _windows(rows):
        span = (datetime.fromisoformat(w["last_t"]) - datetime.fromisoformat(w["first_t"])).total_seconds()
        lines.append(f"{w['start']}  points {w['points']}  samples {w['count']}  "
                     f"covered {human_duration(span)}")
    errors = sum(1 for row in rows if "error" in row)
    lines.append(f"errors: {errors}")
    lines.append("first window is a lower bound: it may have begun before the first sample")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sample the GitHub GraphQL rate limit (#1113).")
    parser.add_argument("--out", type=Path, help="JSONL file to append samples to")
    parser.add_argument("--every-minutes", type=float, default=10.0)
    parser.add_argument("--hours", type=float, default=3.0)
    parser.add_argument("--once", action="store_true", help="one sample, then exit")
    parser.add_argument("--summary", type=Path, help="print the points per window of a JSONL file")
    args = parser.parse_args(argv)
    if args.summary is not None:
        print(summary_text(args.summary))
        return 0
    if args.out is None:
        parser.error("--out is required (or --summary)")
    if args.once:
        append(args.out, sample_line())
        return 0
    run_loop(args.out, args.every_minutes, args.hours)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
