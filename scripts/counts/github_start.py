"""#352: the measured start time of a GitHub machine per runner kind: the seconds from a run's start to its first pytest
step (the wait for a machine plus setup), its median over the runs. It feeds the settings `github_start_seconds_linux`
and `github_start_seconds_windows` (`config/orchestration.json`, and their defaults in `core_settings.py`).

Usage: python -I scripts/counts/github_start.py [--write] [--need N] [--last N]

Reads the machine's `github_runs.jsonl` rows that carry `start_seconds`. When a kind has fewer than --need (10) of them,
it also re-reads the last --last (30) runs of the repo's `test_copy_remote` with `gh run list` and `gh run view --json
createdAt,jobs` (each call with a 60 s timeout), keeping each run once. Prints per kind the median (seconds under 90, then `M min S s`), the
count and the run ids. --write sets both settings in config/orchestration.json as a text edit (the rest of the file
stays as it is). Human text is minutes and seconds; the settings keep seconds.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.agentops import github_run, github_suite  # noqa: E402
from tools.agentops.github_minutes import RUNS_LOG  # noqa: E402
from tools.agentops.machine_mode import machine_dir  # noqa: E402
from tools.agentops.util import no_window_flags  # noqa: E402

KINDS = ("linux", "windows")
SETTING = {"linux": "github_start_seconds_linux", "windows": "github_start_seconds_windows"}
GH_TIMEOUT_S = 60
CONFIG = ROOT / "config" / "orchestration.json"


def minutes_text(seconds: int) -> str:
    """Human text (principle 7): seconds under 90, then minutes and seconds."""
    if seconds < 90:
        return f"{seconds} s"
    return f"{seconds // 60} min {seconds % 60} s"


def runs_repo(settings: dict) -> str:
    """The repo whose runs are read, as `merge_github.plan_for` picks it: the test copy when `test_copy_remote` is set,
    else the repo's own (`repo`, own runners). ValueError when neither is set."""
    remote = str(settings.get("test_copy_remote") or "").strip()
    if remote:
        return github_suite.copy_slug(remote)
    repo = str(settings.get("repo") or "").strip()
    if not repo:
        raise ValueError("neither test_copy_remote nor repo is set in the settings")
    return repo


def gh_json(args: list[str], run=subprocess.run):
    """One `gh` call, its stdout parsed as JSON; a failed call or a timeout raises."""
    proc = run(["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
               timeout=GH_TIMEOUT_S, creationflags=no_window_flags(), check=True)
    return json.loads(proc.stdout)


def read_rows(path: Path) -> list[tuple[int, dict]]:
    """(run_id, {kind: seconds|None}) of every log row that carries `start_seconds`; older rows are skipped."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or type(row.get("run_id")) is not int or not isinstance(row.get("start_seconds"), dict):
            continue
        seconds = {kind: row["start_seconds"].get(kind) for kind in KINDS}
        rows.append((row["run_id"], {kind: v if type(v) is int else None for kind, v in seconds.items()}))
    return rows


def fetch_runs(repo: str, run=subprocess.run, limit: int = 30) -> list[tuple[int, dict]]:
    """(run_id, start_to_first_test_seconds) of the last `limit` runs of the tests workflow of `repo`. A run whose read
    fails is left out and named on stderr."""
    listed = gh_json(["run", "list", "-R", repo, "--workflow", github_run.WORKFLOW, "--limit", str(limit),
                      "--json", "databaseId,createdAt"], run)
    rows = []
    for item in listed:
        run_id = item["databaseId"]
        try:
            view = gh_json(["run", "view", str(run_id), "-R", repo, "--json", "createdAt,jobs"], run)
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            print(f"run {run_id}: not read ({exc})", file=sys.stderr)
            continue
        rows.append((run_id, github_run.start_to_first_test_seconds(view)))
    return rows


def collect(rows: list[tuple[int, dict]], fetch, need: int = 10) -> dict[str, list[tuple[int, int]]]:
    """Per kind the (run_id, seconds) samples: the log's rows, and `fetch()`'s runs too when a kind has fewer than
    `need` of them. A run is counted once per kind."""
    samples: dict[str, list[tuple[int, int]]] = {kind: [] for kind in KINDS}
    for run_id, seconds in rows:
        for kind in KINDS:
            if seconds.get(kind) is not None:
                samples[kind].append((run_id, seconds[kind]))
    if any(len(samples[kind]) < need for kind in KINDS):
        for run_id, seconds in fetch():
            for kind in KINDS:
                if seconds.get(kind) is not None and run_id not in [r for r, _ in samples[kind]]:
                    samples[kind].append((run_id, seconds[kind]))
    return samples


def summary(samples: dict[str, list[tuple[int, int]]]) -> dict[str, dict]:
    out = {}
    for kind in KINDS:
        values = [seconds for _, seconds in samples[kind]]
        out[kind] = {"median": github_run.median_seconds(values), "count": len(values),
                     "runs": [run_id for run_id, _ in samples[kind]]}
    return out


def _set_member(text: str, key: str, value: int) -> str:
    """One `"key": value` member of the config text, the file's other lines untouched. An existing line keeps its own
    trailing comma (or none, when it is the object's last member). A missing key goes on a new line after the
    `test_copy_remote` line, or under the opening brace when there is none; a `test_copy_remote` that is the last
    member takes the comma and the new key is the last one."""
    member = f'"{key}": {value}'
    existing = re.search(rf'^([ \t]*)"{key}":[^\n]*$', text, re.M)
    if existing:
        comma = "," if existing.group(0).rstrip().endswith(",") else ""
        return text[:existing.start()] + f"{existing.group(1)}{member}{comma}" + text[existing.end():]
    anchor = re.search(r'^([ \t]*)"test_copy_remote":[^\n]*$', text, re.M)
    if anchor is not None:
        indent, end = anchor.group(1), anchor.end()
        if anchor.group(0).rstrip().endswith(","):
            return text[:end] + f"\n{indent}{member}," + text[end:]
        return text[:end] + "," + f"\n{indent}{member}" + text[end:]
    brace = re.search(r'^\{[ \t]*$', text, re.M)
    if brace is None:
        raise ValueError("config has no object to hold the settings")
    return text[:brace.end()] + f"\n  {member}," + text[brace.end():]


def write_settings(path: Path, medians: dict[str, int]) -> None:
    """Sets both settings in the config text (`_set_member`). The result must still parse as JSON, else nothing is
    written."""
    text = path.read_text(encoding="utf-8")
    for kind in KINDS:
        key = SETTING[kind]
        value = medians[kind]
        if type(value) is not int:
            raise ValueError(f"no median for {kind}: {key} is not written")
        text = _set_member(text, key, value)
    json.loads(text)
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None, *, config: Path = CONFIG, run=subprocess.run, log: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--write", action="store_true", help="set both settings in the config file")
    parser.add_argument("--need", type=int, default=10, help="runs per kind below which gh is read (default 10)")
    parser.add_argument("--last", type=int, default=30, help="runs of the repo read with gh (default 30)")
    args = parser.parse_args(argv)

    settings = json.loads(config.read_text(encoding="utf-8"))
    try:
        repo = runs_repo(settings)
    except ValueError as exc:
        print(f"no repo to read the runs from: {exc}")
        return 1
    source = "test copy" if str(settings.get("test_copy_remote") or "").strip() else "own repo, no test copy"
    rows = read_rows(log if log is not None else machine_dir() / RUNS_LOG)
    samples = collect(rows, lambda: fetch_runs(repo, run=run, limit=args.last), need=args.need)
    out = summary(samples)

    print(f"start to first test on GitHub machines, runs read from {repo} ({source}); "
          f"log rows with start_seconds: {len(rows)}")
    for kind in KINDS:
        result = out[kind]
        if result["median"] is None:
            print(f"{kind}: no run with a {kind} job's pytest step; count 0")
            continue
        print(f"{kind}: median {minutes_text(result['median'])}, count {result['count']}, "
              f"runs {' '.join(str(r) for r in result['runs'])}")
        if result["count"] < args.need:
            print(f"{kind}: fewer than {args.need} runs available ({result['count']}); the median is a small sample")
    if args.write:
        try:
            write_settings(config, {kind: out[kind]["median"] for kind in KINDS})
        except ValueError as exc:
            print(f"not written: {exc}")
            return 1
        for kind in KINDS:
            print(f"set {SETTING[kind]} = {out[kind]['median']} ({minutes_text(out[kind]['median'])})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
