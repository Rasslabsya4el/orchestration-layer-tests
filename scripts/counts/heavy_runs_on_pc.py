"""Heavy runs of the merge flow by place (#343): map builds, prequeue's full suite, red-on-base.

Usage: python -I scripts/counts/heavy_runs_on_pc.py <repo> [--since YYYY-MM-DD[ HH:MM[:SS]]]
       [--until YYYY-MM-DD[ HH:MM[:SS]]] [--last N]
Reads <repo>/var/agentops/merge.log and <repo>/var/agentops/test_map/build-*.out; a missing file counts 0.
Window: --since and --until compare the prefix of each line's time (merge.log) or file's mtime (build-*.out), both
inclusive. Places:
- a map build's merge.log line names the place: `map-build on github` or `map-build on pc (<reason>)`;
- a build-*.out is a legacy PC build only when its file time is older than the first place line of the window (no such
  line in the window: every build-*.out of the window is legacy); a newer one is never counted on its own;
- a prequeue or red-on-base line names its place: `prequeue on github|pc`, `red-on-base on github|pc`.
A landing is a merge.log line whose event is `pushed`. Its map build is the first map-build line after it and before
the next landing: a place line (its place), `map-build skipped (coalesced)` (`coalesced`); with none, the first older
outcome line (`map built`, `safety run: green`, `safety run: red`, `map failed`), which is PC; else `none`.
Prints the last N landings (default 5).
"""
import argparse
import re
from datetime import datetime
from pathlib import Path

# The log-line wording of units 2, 5 and 6, in one place: every matcher below reads these names.
MAP_BUILD_ON_GITHUB = "map-build on github"
MAP_BUILD_ON_PC = "map-build on pc"  # followed by " (<reason>)"
MAP_BUILD_SKIPPED = ("map-build skipped (coalesced)", "map-build on skip (coalesced)")  # the second is map_run's line
RUN_NAMES = ("prequeue", "red-on-base")
RUN_PLACE = re.compile(r"\b(%s) on (github|pc)\b" % "|".join(RUN_NAMES))
OLDER_OUTCOMES = ("safety run: green", "safety run: red", "map built", "map failed")

STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| ")


def in_window(stamp, since, until):
    if since and stamp[:len(since)] < since:
        return False
    return not (until and stamp[:len(until)] > until)


def place_of_map_build(text):
    if MAP_BUILD_ON_GITHUB in text:
        return "github"
    if MAP_BUILD_ON_PC in text:
        return "pc"
    return None


def merge_log_entries(path, since, until):
    """(stamp, text) of every timestamped merge.log line in the window, in log order; none when the file is missing."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = STAMP.match(line)
        if m and in_window(m.group(1), since, until):
            rows.append((m.group(1), line[m.end():]))
    return rows


def first_place_stamp(entries):
    """The stamp of the first map-build place line of the window, or None when it has none."""
    for stamp, text in entries:
        if place_of_map_build(text):
            return stamp
    return None


def legacy_build_outs(folder, since, until, first_place):
    """The build-*.out files of the window older than the first place line: the PC builds of the older form."""
    if not folder.is_dir():
        return []
    legacy = []
    for out in sorted(folder.glob("build-*.out")):
        stamp = datetime.fromtimestamp(out.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        if not in_window(stamp, since, until):
            continue
        if first_place is None or stamp < first_place:
            legacy.append(out)
    return legacy


def map_place_after(entries, start, end):
    """The place of the map build a landing started: the first place or coalesced line in entries[start:end], else the
    first older outcome line there (PC), else none."""
    segment = entries[start:end]
    for _stamp, text in segment:
        if any(form in text for form in MAP_BUILD_SKIPPED):
            return "coalesced"
        place = place_of_map_build(text)
        if place:
            return place
    for _stamp, text in segment:
        if text.startswith("map-run | ") and any(word in text for word in OLDER_OUTCOMES):
            return "pc"
    return "none"


def find_landings(entries):
    """(index, stamp, branch) of each landing: a line whose event (second field) is `pushed`."""
    landings = []
    for index, (stamp, text) in enumerate(entries):
        fields = text.split(" | ")
        if len(fields) > 1 and fields[1] == "pushed":
            landings.append((index, stamp, fields[0]))
    return landings


def main():
    ap = argparse.ArgumentParser(description="Heavy runs of the merge flow by place (#343).")
    ap.add_argument("repo", type=Path)
    ap.add_argument("--since", default=None, help="window start (default: all)")
    ap.add_argument("--until", default=None, help="window end, inclusive (default: all)")
    ap.add_argument("--last", type=int, default=5, help="how many last landings to print (default 5)")
    args = ap.parse_args()
    since, until = args.since, args.until
    agentops = args.repo / "var" / "agentops"

    entries = merge_log_entries(agentops / "merge.log", since, until)
    logged_gh = logged_pc = 0
    runs = {(name, place): 0 for name in RUN_NAMES for place in ("pc", "github")}
    for _stamp, text in entries:
        place = place_of_map_build(text)
        if place == "github":
            logged_gh += 1
        elif place == "pc":
            logged_pc += 1
        run_match = RUN_PLACE.search(text)
        if run_match:
            runs[(run_match.group(1), run_match.group(2))] += 1
    legacy = legacy_build_outs(agentops / "test_map", since, until, first_place_stamp(entries))

    print(f"window: since {since or 'all'}, until {until or 'all'}")
    print(f"map builds on the PC: {logged_pc + len(legacy)}")
    print(f"map builds on GitHub: {logged_gh}")
    for name in RUN_NAMES:
        print(f"{name} on the PC: {runs[(name, 'pc')]}")
        print(f"{name} on GitHub: {runs[(name, 'github')]}")

    landing_rows = find_landings(entries)
    ends = [index for index, _stamp, _branch in landing_rows[1:]] + [len(entries)]  # a segment ends at the next landing
    shown = list(zip(landing_rows, ends))[-args.last:] if args.last > 0 else []
    print(f"last {args.last} landings, place of their map build:")
    for (index, stamp, branch), end in shown:
        print(f"  {stamp} {branch} -> {map_place_after(entries, index + 1, end)}")


if __name__ == "__main__":
    main()
