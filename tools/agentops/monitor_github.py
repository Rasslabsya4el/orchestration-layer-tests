"""#340 unit 7: the merges tab's data (`/api/github`): where each merge's tests ran, the GitHub machines in use, and the
free GitHub minutes of each repo this month.

    collect(root=ROOT, clock=None) -> {"at", "merges", "machines", "minutes"}

Each part is read on its own. A part that fails is None with `<part>_error` naming the failure; it is never an empty
list, so a broken read cannot pass for "no merges":
- merges: the newest MERGES_SHOWN merge rows of `var/agentops/merge.log` (see `merge_rows`).
- machines: {"used", "total"} from `machine_berths.machines_in_use()` and the setting `github_machines_total`.
- minutes: one entry per setting `targets` ({"slug", "name", "used", "cap", "month"}) from `github_minutes`.

Read-only: nothing here writes a file or starts a process.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import core_settings, github_minutes, machine_berths

ROOT = core_settings.ROOT
MERGES_SHOWN = 20
MATCH_TOLERANCE_S = 5  # a log stamp may come this many seconds before a run's start or after its end
_STAMP = "%Y-%m-%d %H:%M:%S"
_BATCH = re.compile(r"^batch of \d+: (.*)$")
_PART = re.compile(r"^\d+(\.\d+)? (min|s)$")          # one part that finished, e.g. "7.2 min", "63 s"
_MACHINES = re.compile(r"(\d+) machines?\b")          # "suite on GitHub: 5 machines, parts …"
_PARTS = re.compile(r"parts ([^,]*)")                 # the part list of one GitHub run, up to its comma
_RECHECK_PC = re.compile(r"re-check of \d+ on the PC \(.*?\)(?=$| \(|;)")  # "re-check of 12 on the PC (red, …)"
_PC_READERS = re.compile(r"\(\+ (\d+) tests? on the PC\)")  # the PC tests beside a GitHub run, at the field's end
_BRANCH_ISSUE = re.compile(r"(?<![A-Za-z0-9])i(\d+)")  # "i316-u1" -> 316


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _members(branch: str) -> list[str]:
    """A batch line names its branches ("batch of 2: i319-fix, i333"); any other line is one branch."""
    m = _BATCH.match(branch)
    if m is None:
        return [branch]
    return [b.strip() for b in m.group(1).split(",") if b.strip()]


def _branch_issues(text: str) -> list[int]:
    return [int(i) for i in _BRANCH_ISSUE.findall(text)]


def _place(fields: list[str]) -> str:
    """Where the merge's tests ran, in the writers' words (merge.py, merge_github.py):

    - a GitHub field (`suite on GitHub: K machines, parts …; re-check of N on GitHub: …`): `GitHub k/n parts done`,
      k and n over every GitHub run of the line (the suite and its re-checks), then `, K machines` (the most machines
      one of its runs asked), then `; re-check of N on the PC (<why>)` when a re-check went to the PC, then
      ` (+ k tests on the PC)` when PC-only tests ran beside it;
    - else a PC field (`suite on the PC (<why>)`): `PC (<why>)`, the reason kept as written;
    - else no place field at all: `PC (GitHub off)`.
    """
    text = next((f for f in fields if f.startswith("suite on GitHub:")), None)
    if text is None:
        pc = next((f for f in fields if f.startswith("suite on the PC (")), None)
        return "PC " + pc.removeprefix("suite on the PC ") if pc is not None else "PC (GitHub off)"
    done = total = 0
    machines: list[int] = []
    for clause in text.split("; "):
        if "on GitHub:" not in clause:
            continue
        m = _MACHINES.search(clause)
        if m:
            machines.append(int(m.group(1)))
        p = _PARTS.search(clause)
        if p:
            parts = [x.strip() for x in p.group(1).split("/")]
            total += len(parts)
            done += sum(1 for x in parts if _PART.match(x))
    place = f"GitHub {done}/{total} parts done"
    if machines:
        place += f", {max(machines)} machines"
    recheck = _RECHECK_PC.search(text)
    if recheck:
        place += f"; {recheck.group(0)}"
    readers = _PC_READERS.search(text)
    if readers:
        k = int(readers.group(1))
        place += f" (+ {k} {'test' if k == 1 else 'tests'} on the PC)"
    return place


def _read_result(path: Path) -> dict | None:
    """One merge result file as the fields the matching needs; None for a file that is no attempt or cannot be read."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("ended") or data.get("reason") == "already-queued":
        return None
    try:
        started = datetime.fromisoformat(str(data["started"]))
        ended = datetime.fromisoformat(str(data["ended"]))
    except (KeyError, ValueError):
        return None
    # the branch is in the file name (`<stamp>-merge-<branch>.json`): argv has no fixed place for it (`--issue N` may come first)
    branch = path.stem.split("-merge-", 1)[1]
    raw = data.get("issues")
    issues = [i for i in raw if isinstance(i, int)] if isinstance(raw, list) else []
    return {"branch": branch, "started": started, "ended": ended, "rc": data.get("rc"), "issues": issues}


def _results(results_dir: Path) -> dict[str, list[dict]]:
    by_branch: dict[str, list[dict]] = {}
    for path in Path(results_dir).glob("*-merge-*.json"):
        result = _read_result(path)
        if result is not None:
            by_branch.setdefault(result["branch"], []).append(result)
    return by_branch


def _match(results: list[dict], at: datetime) -> dict | None:
    """The result of one branch whose run spans the log line: started <= at <= ended, each side with MATCH_TOLERANCE_S
    of slack (the batch line is written about a second before its ended). Of several, the one that ended nearest; None
    when none spans it."""
    slack = timedelta(seconds=MATCH_TOLERANCE_S)
    best, best_gap = None, None
    for result in results:
        if not (result["started"] - slack <= at <= result["ended"] + slack):
            continue
        gap = abs((result["ended"] - at).total_seconds())
        if best_gap is None or gap < best_gap:
            best, best_gap = result, gap
    return best


def _output_is_a_file(fields: list[str]) -> bool:
    """A merge.log line is a suite run when its `output` field names a file; `output -` (or none) is a status that ran
    no suite: conflict, dirty, rebase-conflict, nothing-to-merge."""
    out = next((f for f in fields if f.startswith("output ")), "output -")
    return out.removeprefix("output ").strip() not in ("-", "")


def merge_rows(log: Path, results_dir: Path, limit: int | None) -> list[dict]:
    """One row per suite run of `merge.log` (a 4th field `suite …` and an `output` file), newest first, at most `limit`
    (None: all). Row: {"at", "branch", "issues", "outcome", "reason", "place"}.

    outcome: `landed` when the matched result file (same branch, its run spans the log stamp within MATCH_TOLERANCE_S)
    has rc 0, `failed` when it has any other rc, `lost` when none matches. A batch line is matched per member branch:
    `failed` if a member failed, else `lost` if one has no match, else `landed`. Heavy-run lines (`map-…`) are not
    merges and are skipped. A read that fails raises: the caller turns it into `merges_error`.
    """
    text = Path(log).read_text(encoding="utf-8", errors="replace")
    results = _results(Path(results_dir))
    rows: list[dict] = []
    for line in text.splitlines():
        fields = [f.strip() for f in line.split(" | ")]
        if len(fields) < 4 or not fields[3].startswith("suite ") or not _output_is_a_file(fields):
            continue
        try:
            at = datetime.strptime(fields[0], _STAMP)
        except ValueError:
            continue
        reason = fields[2]
        if reason.startswith("map-"):
            continue
        members = _members(fields[1])
        hits = [_match(results.get(member, []), at) for member in members]
        if any(hit is not None and hit["rc"] != 0 for hit in hits):
            outcome = "failed"
        elif any(hit is None for hit in hits):
            outcome = "lost"
        else:
            outcome = "landed"
        issues: list[int] = []
        for member, hit in zip(members, hits):
            for issue in (hit["issues"] if hit and hit["issues"] else _branch_issues(member)):
                if issue not in issues:
                    issues.append(issue)
        rows.append({"at": fields[0], "branch": fields[1], "issues": issues, "outcome": outcome,
                     "reason": reason, "place": _place(fields)})
    rows.reverse()
    return rows if limit is None else rows[:limit]


def collect(root: Path = ROOT, clock: float | None = None) -> dict:
    now = time.time() if clock is None else clock
    doc: dict = {"at": datetime.fromtimestamp(now).isoformat(timespec="seconds")}
    agentops = Path(root) / "var" / "agentops"

    try:
        doc["merges"] = merge_rows(agentops / "merge.log", agentops / "results", MERGES_SHOWN)
    except Exception as exc:  # noqa: BLE001 -- the error text is the part's answer, the other parts still read
        doc["merges"] = None
        doc["merges_error"] = _error_text(exc)

    try:
        doc["machines"] = {
            "used": machine_berths.machines_in_use(),
            "total": int(core_settings.load()["github_machines_total"]),
        }
    except Exception as exc:  # noqa: BLE001 -- see merges
        doc["machines"] = None
        doc["machines_error"] = _error_text(exc)

    try:
        settings = core_settings.load()
        month = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")
        minutes = []
        for target in settings.get("targets", []):
            name = Path(target["folder"]).name
            minutes.append({
                "slug": target["repo"],
                "name": name,
                "used": github_minutes.minutes_used(name, month),
                "cap": settings["github_free_minutes"],
                "month": month,
            })
        doc["minutes"] = minutes
    except Exception as exc:  # noqa: BLE001 -- see merges
        doc["minutes"] = None
        doc["minutes_error"] = _error_text(exc)

    return doc
