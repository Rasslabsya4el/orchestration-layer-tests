"""`python -m tools.agentops merge-status [--json]` (#680): the merge line as one read-only snapshot.

A running suite's total is its own (#698 its selected N, #116 else its plan's tests) and its done its own ended
tests; the full run on main is its own row.

Reads the merge lock, its queue and batch dirs, the running suite's `.out`, `merge.log` and the
result files; never takes the lock, starts nothing. JSON keeps raw seconds; the
text form renders durations through `human_duration`.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import time
from pathlib import Path
from types import SimpleNamespace

from . import merge_priority
from .merge import ROOT
from .suite_events import ended_count
from .suite_plan import finished_count, load_plan, rest_seconds, work_share
from .util import human_duration, parse_started, pid_alive

ISO = "%Y-%m-%dT%H:%M:%S"
MIN_PERCENT_FOR_RATE = 5
LANDED_MAX = 60
MESSAGE_SHOWN = 60  # #37: characters of a commit's message shown on the running row
MEDIAN_OF = 5
MIN_ETA_S = 60.0  # #61: the least time left shown below MIN_PERCENT_FOR_RATE
ETA_CAP_RUNS = 2  # #61: from MIN_PERCENT_FOR_RATE on, a green run is capped at this many median suites
FALLBACK_SUITE_S = 300.0  # no finished suite in merge.log yet

_PROGRESS_RE = re.compile(r"^([.sFEx]+)\s+\[\s*(\d+)%\]\s*$", re.MULTILINE)
_LOG_SUITE_RE = re.compile(r"\| suite (?:(\d+) h (\d+) min|([\d.]+) (s|min))")
_SELECTED_RE = re.compile(r"^selected (\d+) of (\d+)")
_WORKERS_RE = re.compile(r"suite [^|]*?, (\d+) workers")
_SUMMARY_SECS_RE = re.compile(r"\d+ (?:passed|failed)\b.* in (\d+(?:\.\d+)?)s\b")
_RED_SUMMARY_RE = re.compile(r"\b[1-9]\d* (?:failed|errors?)\b")


def _iso(epoch: float) -> str:
    return time.strftime(ISO, time.localtime(epoch))


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _issues_of(info: dict, branch: str) -> list[int]:
    raw = info.get("issues")
    if isinstance(raw, list) and raw:
        return [int(i) for i in raw]
    m = re.fullmatch(r"orch-(\d+)", branch or "")
    return [int(m.group(1))] if m else []


def _suite_totals(text: str) -> int | None:
    """passed + skipped + failed of a finished pytest summary line, or None."""
    for line in reversed(text.splitlines()):
        if " in " in line and re.search(r"\d+ (passed|failed)", line):
            return sum(int(m.group(1)) for m in re.finditer(r"(\d+) (?:passed|skipped|failed)", line))
    return None


def _selected_count(text: str) -> int | None:
    """#698: N of the first line `selected N of M (map ...)` of a merge suite out (M is the whole suite, never the total)."""
    m = _SELECTED_RE.match(text)
    return int(m.group(1)) if m else None


def _progress(text: str) -> tuple[int | None, int]:
    """(last percent, count of F in the progress lines)."""
    percent, failed = None, 0
    from .suite_plan import strip_poll
    text = strip_poll(text)  # #900: a poll line inside a progress line hid its F
    for m in _PROGRESS_RE.finditer(text):
        percent = int(m.group(2))
        failed += m.group(1).count("F")
    from .suite_plan import partial_tail
    return percent, failed + partial_tail(text).count("F")


def _log_facts(log_path: Path) -> tuple[list[float], int | None]:
    """(durations in s of the last MEDIAN_OF real suites, workers of the newest suite line)."""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], None
    durations: list[float] = []
    workers = None
    for line in lines:
        m = _LOG_SUITE_RE.search(line)
        if not m or " | selected " in line:  # a selected run is no yardstick for a full suite
            continue
        if " | cut | " in line:  # #122: a suite a Priority merge cut never finished: no yardstick either
            continue
        if m.group(1) is not None:
            secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60
        else:
            secs = float(m.group(3)) * (60 if m.group(4) == "min" else 1)
        if secs <= 0:
            continue
        durations.append(secs)
        w = _WORKERS_RE.search(line)
        if w:
            workers = int(w.group(1))
    return durations[-MEDIAN_OF:], workers


def _newest_finished_total(suite_dir: Path, skip: str | None) -> int | None:
    try:
        outs = sorted((p for p in suite_dir.glob("*.out") if "-check-" not in p.name), key=lambda p: p.name, reverse=True)
    except OSError:
        return None
    for p in outs:
        if skip and str(p) == skip:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        total = None if text.startswith("selected ") else _suite_totals(text)  # a selected run's total is not the suite's
        if total:
            return total
    return None


def _plan_count(progress: str) -> int | None:
    """#116 A1: the tests of this run's own plan (`<out>.plan.json`, one number per test), or None."""
    plan = load_plan(progress) if progress else None
    return len(plan) if plan else None


def _tests_done(progress: str, percent: int | None, total: int | None) -> int | None:
    """#116 A2: the tests ended in this run (its events files' E lines), capped at total; a run with no events file
    yet (a selected run, a plan not started) takes its percent of the total."""
    if not total:
        return None
    ended = ended_count(progress)
    if ended is not None:
        return min(ended, total)
    return round(percent * total / 100) if percent is not None else None


def _check_of(lock_path: Path, ticket: str) -> dict | None:
    """#765: the branch's own check status (`<lock>.checks/<ticket>.json`); a `running` one with a dead pid is stale."""
    st = _read_json(lock_path.with_name(lock_path.name + ".checks") / f"{ticket}.json")
    if not st or st.get("state") not in ("running", "green"):
        return None
    if st["state"] == "running" and not pid_alive(st.get("pid")):
        return None
    return {"state": st["state"], "since": st.get("since")}


def _epoch(stamp: object) -> float | None:
    return parse_started(str(stamp).replace("T", " ")) if stamp else None


def _landed(results_dir: Path) -> list[dict]:
    """#693: one row per branch run of attempts (a landing, rc 0, ends a run); an already-queued file is no attempt."""
    try:
        files = sorted(results_dir.glob("*-merge-*.json"), key=lambda p: p.name)
    except OSError:
        return []
    rows: list[dict] = []
    open_run: dict[str, dict] = {}  # branch -> its row so far (attempts since the previous landing)
    for p in files:
        d = _read_json(p)
        if not d or not d.get("ended") or d.get("reason") == "already-queued":
            continue
        argv = d.get("argv") or []
        branch = argv[4] if len(argv) > 4 else p.stem.split("-merge-", 1)[1]
        s, e = _epoch(d.get("started")), _epoch(d["ended"])
        prev = open_run.pop(branch, None)
        n = prev["attempts"] if prev else 0
        row = {"branch": branch, "issues": _issues_of(d, branch), "queued": d.get("started"),
               "landed": d["ended"], "queued_to_land_s": round(e - s) if s and e else None,
               "rc": d.get("rc"), "reason": d.get("reason"), "failed": d.get("failed"),
               "suite_place": d.get("suite_place"),  # #345: where the suite ran; a fallback's reason is in it
               "attempts": n + 1, "failed_before": n}
        if prev:
            rows.remove(prev)
        rows.append(row)
        if d.get("rc") != 0:
            open_run[branch] = row
    rows.reverse()
    return rows[:LANDED_MAX]


def _birth(p: Path) -> float | None:
    """#764: when the out file was created = when the run of its commit began (Windows st_ctime is the creation time)."""
    try:
        st = p.stat()
    except OSError:
        return None
    return getattr(st, "st_birthtime", None) or (st.st_ctime if os.name == "nt" else None)


def _full_run_durations(out_dir: Path, skip: str | None) -> list[float]:
    """Seconds of the last MEDIAN_OF finished full runs (their pytest summary `in 1260.00s`), newest first."""
    try:
        outs = sorted((p for p in out_dir.glob("*.out") if "-check-" not in p.name and str(p) != skip),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    durations: list[float] = []
    for p in outs:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        secs = _summary_seconds(text)
        if secs:
            durations.append(secs)
        if len(durations) == MEDIAN_OF:
            break
    return durations


def _summary_seconds(text: str) -> float | None:
    """The seconds of a finished pytest summary line (`4190 passed, 80 skipped in 285.43s`), or None."""
    for line in reversed(text.splitlines()):
        m = _SUMMARY_SECS_RE.search(line)
        if m:
            if _RED_SUMMARY_RE.search(line):
                return None  # a red run stops early; only green runs count (the merge rows count `0 failed` suites only)
            return float(m.group(1)) or None
    return None


def _full_run(root: Path, t: float) -> dict | None:
    """#698: the full run on main (`map_run.py`, its own lock and out folder) as a row of its own, or None."""
    base = root / "var" / "agentops"
    holder = _read_json(base / "full_run.lock")
    if not holder or not pid_alive(holder.get("pid")):
        return None
    started = _epoch(holder.get("started"))
    out_dir = base / "full_run"
    try:
        outs = sorted((p for p in out_dir.glob("*.out") if "-check-" not in p.name),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        outs = []
    cur = outs[0] if outs else None
    if cur is not None and started and cur.stat().st_mtime < started:
        cur = None  # the newest out is an earlier run's: this one has written nothing yet
    born = _birth(cur) if cur is not None else None
    if born and (not started or born > started):
        started = born  # #764: the lock's start may be the chain's; the out file's birth is this commit's run
    text = ""
    if cur is not None:
        try:
            text = cur.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    percent, failed = _progress(text)
    total = _plan_count(str(cur)) if cur is not None else None  # #123: this run's own plan, never a finished run's total
    done = round(percent * total / 100) if percent is not None and total else None
    elapsed = max(0.0, t - started) if started else None
    plan = load_plan(cur) if cur is not None else None
    # the part of the run counted: a plan weighs each test by its seconds (#758), without one every test weighs the same
    share = work_share(plan, finished_count(text)) if plan is not None else (percent or 0) / 100
    eta, eta_basis, eta_unknown = None, None, False
    if elapsed is not None and share < MIN_PERCENT_FOR_RATE / 100:  # #70: too little counted, as merge rows do (#61)
        durations = _full_run_durations(out_dir, str(cur) if cur else None)
        if durations:
            median = statistics.median(durations)
            eta, eta_basis = min(max(MIN_ETA_S, median - elapsed), ETA_CAP_RUNS * median), "recent runs"
        else:
            eta_unknown = True
    elif plan is not None and elapsed is not None:  # #758: only with a plan; share 0 = no estimate
        eta = rest_seconds(elapsed, share)
    return {"commit": cur.stem if cur else None, "started": _iso(started) if started else holder.get("started"),
            "elapsed_s": round(max(0.0, t - started)) if started else None,
            "percent": percent, "tests_done": done, "tests_total": total, "failed": failed,
            "out": str(cur) if cur else None, "eta_s": None if eta is None else round(eta),
            "eta_basis": eta_basis, "eta_unknown": eta_unknown}


BATCH_KEYS = ("suite", "remote", "main", "cwd")  # a waiter joins the holder's batch only when all four match (merge.py)


def _batch_key(info: dict) -> tuple:
    """A merge waiter's batch keys, plus whether it is a Priority ticket: a Priority batch holds only Priority
    waiters and ordinary waiters form the later batches (#122 rule 4, merge_priority.batch_for)."""
    return tuple(info.get(k) for k in BATCH_KEYS) + (merge_priority.rank_key(info.get("priority")) is not None,)


def _batch_walk(entries: list[tuple | None], base_eta: float, median: float) -> tuple[list[float], list[int], float]:
    """The start second and the 1-based batch number of each entry, and the second the queue ends. A barrier is a
    batch of its own; a waiter takes every later waiter with equal keys up to the next barrier. Each batch costs
    one median suite."""
    starts: list[float] = [0.0] * len(entries)
    batches: list[int] = [0] * len(entries)
    left = list(range(len(entries)))
    t = float(base_eta)
    n = 0
    while left:
        n += 1
        head = entries[left[0]]
        if head is None:
            idx = left.pop(0)
            starts[idx], batches[idx] = t, n
        else:
            joined: list[int] = []
            for idx in left:
                if entries[idx] is None:
                    break
                if entries[idx] == head:
                    joined.append(idx)
            for idx in joined:
                starts[idx], batches[idx] = t, n
            left = [idx for idx in left if idx not in joined]
        t += median
    return starts, batches, t


def batch_starts(entries: list[tuple | None], base_eta: float, median: float) -> tuple[list[float], float]:
    """#120: the start second of each queued ticket and the second the queue ends. An entry is a merge waiter's
    batch keys (``_batch_key``), or None for a barrier (a ticket that takes a suite of its own). The first remaining
    ticket opens a batch: a barrier runs alone; a waiter takes every later waiter with equal keys up to the next
    barrier. Each batch costs one median suite. The pre-filter's conflict outcomes are not predicted."""
    starts, _batches, end = _batch_walk(entries, base_eta, median)
    return starts, end


def _line_order(pending: list) -> list:
    """#122 rule 3 on the line as the queue files hold it: a barrier (None) keeps its place and splits the line;
    inside each stretch between barriers the Priority waiters come first by rank, then the rest in arrival order."""
    ordered: list = []
    stretch: list = []
    for rec in pending:
        if rec is None or rec.core:  # a core update (#203) is a barrier too, and keeps its record for its row
            ordered += merge_priority.order_waiters(stretch) + [rec]
            stretch = []
        else:
            stretch.append(rec)
    return ordered + merge_priority.order_waiters(stretch)


def _machine_waiting() -> list[dict]:
    """#224: the heavy runs of every repo on the PC that wait for a machine berth, head of the line first."""
    try:
        from .machine_berths import waiting_rows
        return waiting_rows()
    except Exception:  # noqa: BLE001 -- a status read never fails on the berth folder
        return []


def _machine_counts() -> tuple[int, int]:
    """#232: the GitHub machines in use now and the account's total (setting `github_machines_total`)."""
    try:
        from . import core_settings
        from .machine_berths import machines_in_use
        total = core_settings.load().get("github_machines_total", core_settings.DEFAULTS["github_machines_total"])
        return machines_in_use(), int(total)
    except Exception:  # noqa: BLE001 -- a status read never fails on the berth folder
        return 0, 0


def collect(root: Path = ROOT, clock: float | None = None) -> dict:
    t = time.time() if clock is None else clock
    lock_path = root / "var" / "locks" / "merge.lock"
    queue_dir = lock_path.with_name(lock_path.name + ".queue")
    batch_dir = lock_path.with_name(lock_path.name + ".batch")
    durations, workers = _log_facts(root / "var" / "agentops" / "merge.log")
    median = statistics.median(durations) if durations else FALLBACK_SUITE_S

    holder = _read_json(lock_path)
    running = None
    claimed: set[str] = set()
    if holder and pid_alive(holder.get("pid")):
        started = parse_started(holder.get("started"))
        elapsed = max(0.0, t - started) if started else 0.0
        progress = str(holder.get("progress") or "")
        text = ""
        if progress:
            try:
                text = Path(progress).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        percent, failed = _progress(text)
        total = _selected_count(text) or _plan_count(progress)  # #116 A1: never another run's out
        finished = _suite_totals(text) is not None
        phase = "suite" if not finished else ("re-check" if failed else "push")
        if finished:
            percent = 100
        plan = load_plan(progress) if progress else None
        eta, eta_basis = None, None
        # the part of the run counted: a plan weighs each test by its seconds (#758), without one every test weighs the same
        share_done = (work_share(plan, finished_count(text)) if plan is not None and not finished
                      else (percent or 0) / 100)
        if percent is not None and finished:
            eta = 0.0
        elif share_done < MIN_PERCENT_FOR_RATE / 100:  # #61: too little counted for a ratio (work share with a plan, else tests)
            if durations:
                eta, eta_basis = max(MIN_ETA_S, median - elapsed), "recent runs"
        else:
            if plan is not None:  # #758: the rest from the share of work done
                eta = rest_seconds(elapsed, work_share(plan, finished_count(text)))
            else:
                eta = elapsed * (100 - percent) / percent
            if eta is not None and durations and not failed:
                eta = min(eta, ETA_CAP_RUNS * median)  # #61: a green run is no longer than twice a recent one
        hold = _read_json(lock_path.with_name(lock_path.name + ".holding"))
        infos = [hold["info"]] if hold and isinstance(hold.get("info"), dict) else []
        try:
            claims = sorted(batch_dir.glob("*.claim"))
        except OSError:
            claims = []
        queued_ns: list[int] = []  # #6: the enqueue times of the running tickets (their queue names start with time_ns)
        for c in claims:
            tname = c.name[: -len(".claim")]
            claimed.add(tname)
            info = _read_json(queue_dir / tname)
            if info:
                infos.append(info)
            try:
                queued_ns.append(int(tname.split("-", 1)[0]))
            except ValueError:
                pass
        branches: list[str] = []
        issues: list[int] = []
        for info in infos:
            b = str(info.get("branch") or "")
            if b and b not in branches:
                branches.append(b)
                issues += [i for i in _issues_of(info, b) if i not in issues]
        kind = holder.get("kind")
        message = None
        if kind == "commit":  # #37: a commit holds the line by its own message, not by a branch
            branches = []
            issues = [int(i) for i in holder.get("issues") or []]
            message = str(holder.get("message") or "").split("\n", 1)[0][:MESSAGE_SHOWN]
        done = _tests_done(progress, percent, total)
        running = {"kind": kind, "message": message, "branches": branches, "issues": issues, "phase": phase,
                   "started": _iso(started) if started else holder.get("started"),
                   "queued": _iso(min(queued_ns) / 1e9) if queued_ns else None,
                   "percent": percent, "tests_done": done, "tests_total": total, "failed": failed,
                   "eta_s": None if eta is None else round(eta), "eta_basis": eta_basis, "workers": workers, "suite_out": progress or None}
        if kind == "core-update":  # #203: the update's own row: its sha, no branch, no tests of its own
            running.update({"branches": [], "issues": [], "tests_done": None, "tests_total": None})
            if holder.get("sha"):  # #207: a sha key only once the home head is known
                running["sha"] = str(holder["sha"])

    waiting: list[dict] = []
    base_eta = (running["eta_s"] if running["eta_s"] is not None else max(0.0, median)) if running else 0
    try:
        tickets = sorted(queue_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        tickets = []
    pending: list = []  # per live unclaimed ticket in queue-name order: a merge waiter's record, None for a barrier
    for tk in tickets:
        if tk.name in claimed:
            continue
        parts = tk.name.split("-")
        try:
            ns, pid = int(parts[0]), int(parts[1])
        except (IndexError, ValueError):
            continue
        if not pid_alive(pid):
            continue
        info = _read_json(tk)
        if isinstance(info, dict) and info.get("kind") == "commit" and info.get("needs_suite") is False:
            continue  # the holder passes it over (merge.py waiting_line)
        if not isinstance(info, dict) or info.get("kind") != "merge":
            # a barrier: a suite of its own, ends the holder's batch walk; a core update (#203) is one, with its own row
            core = isinstance(info, dict) and info.get("kind") == "core-update"
            pending.append(SimpleNamespace(tk=tk, ns=ns, info=info, core=True) if core else None)
            continue
        pending.append(SimpleNamespace(tk=tk, ns=ns, info=info, core=False))
    entries: list[tuple | None] = []  # the line in its order (#122 rule 3): a merge waiter's batch key, None for a barrier
    row_entry: list[int] = []  # the entry index of each waiting row, in row order
    for rec in _line_order(pending):
        if rec is None:
            entries.append(None)
            continue
        info, ns = rec.info, rec.ns
        if rec.core:  # #203: a core update's row: its sha, no branch, no batch
            entries.append(None)
            row_entry.append(len(entries) - 1)
            row = {"kind": "core-update", "position": len(waiting) + 1,
                   "queued": _iso(ns / 1e9), "waited_s": max(0, round(t - ns / 1e9))}
            if info.get("sha"):  # #207: a sha key only once the home head is known
                row["sha"] = str(info["sha"])
            waiting.append(row)
            continue
        branch = str(info.get("branch") or "")
        prio = merge_priority.rank_key(info.get("priority"))
        entries.append(_batch_key(info))
        row_entry.append(len(entries) - 1)
        waiting.append({"branch": branch, "issues": _issues_of(info, branch), "position": len(waiting) + 1,
                        "queued": _iso(ns / 1e9), "waited_s": max(0, round(t - ns / 1e9)),
                        "check": _check_of(lock_path, rec.tk.name),
                        "priority_class": info["priority"]["class"] if prio is not None else None})
    starts, batches, queue_end = _batch_walk(entries, base_eta, median)
    for w, idx in zip(waiting, row_entry):  # every row starts when its barrier or batch does
        w["eta_start_s"] = round(starts[idx])
        if w.get("kind") != "core-update":
            w["batch"] = batches[idx]

    return {"at": _iso(t), "running": running, "full_run": _full_run(root, t), "waiting": waiting,
            "queue_end_s": round(queue_end), "landed": _landed(root / "var" / "agentops" / "results"),
            "machine_waiting": _machine_waiting(),
            **dict(zip(("machines_in_use", "machines_total"), _machine_counts()))}


def render_text(snap: dict) -> str:
    def tix(issues: list[int]) -> str:
        return " ".join(f"#{i}" for i in issues)

    r = snap["running"]
    if r is None:
        lines = ["merge line: no suite running"]
    else:
        pct = f"{r['percent'] if r['percent'] is not None else 0}%"
        have_tests = r["tests_total"] and r["tests_done"] is not None
        shown = f"{pct}, {r['tests_done']}/{r['tests_total']} tests" if have_tests else pct
        if r["kind"] == "commit":  # #37: `running: commit #29 "<message's first 60 chars>" suite, ...`
            who = " ".join(["commit"] + ([tix(r["issues"])] if r["issues"] else [])
                           + ([f'"{r["message"]}"'] if r["message"] else []))
        elif r["kind"] == "core-update":  # #203: the update's row, named by its home head (#207: when known)
            who = " ".join(["core update"] + ([r["sha"]] if r.get("sha") else []))
        else:
            who = f"{', '.join(r['branches']) or '?'} ({tix(r['issues'])})"
        lines = [f"running: {who} {r['phase']}, "
                 f"{shown}, {r['failed']} failed"
                 + (f", about {human_duration(r['eta_s'])} left"
                    + (" (from recent runs)" if r.get("eta_basis") == "recent runs" else "")
                    if r["eta_s"] is not None else ", estimate not known yet")]
    f = snap.get("full_run")
    if f:
        ftests = (f"{f['tests_done']}/{f['tests_total']} tests"
                  if f["tests_total"] and f["tests_done"] is not None else f"{f['percent']}%")
        lines.append(f"full run on main {f['commit'] or '?'}: {f['percent'] if f['percent'] is not None else 0}%, "
                     f"{ftests}, {f['failed']} failed, running {human_duration(f['elapsed_s']) if f['elapsed_s'] is not None else '?'}"
                     + (f", about {human_duration(f['eta_s'])} left"
                        + (" (from recent runs)" if f.get("eta_basis") == "recent runs" else "")
                        if f.get("eta_s") is not None else ", estimate not known yet" if f.get("eta_unknown") else ""))
    for w in snap["waiting"]:
        if w.get("kind") == "core-update":  # #203: the update's row, no branch to name
            name = " ".join(["core update"] + ([w["sha"]] if w.get("sha") else []))  # #207: no sha, no "?"
            lines.append(f"waiting {w['position']}: {name}, queued "
                         f"{human_duration(w['waited_s'])}, starts in about {human_duration(w['eta_start_s'])}")
            continue
        words = {"running": ", checking its tests", "green": ", its tests passed alone"}.get((w.get("check") or {}).get("state"), "")
        mark = {"flow-fix": ", flow fix", "priority": ", priority"}.get(w.get("priority_class"), "")  # #122: plain words
        lines.append(f"waiting {w['position']}: {w['branch']} ({tix(w['issues'])}), queued "
                     f"{human_duration(w['waited_s'])}, starts in about {human_duration(w['eta_start_s'])}{mark}{words}")
    for m in snap.get("machine_waiting") or []:  # #224: a heavy run waiting for the machine (any repo on the PC)
        if m.get("kind") == "github":  # #232: a merge waiting for GitHub machines
            what = (f"waits for GitHub machines: {m['repo']} wants {m['machines']} since {m['since']}, "
                    f"{snap.get('machines_in_use', 0)} of {snap.get('machines_total', 0)} in use")
        else:
            what = f"waits for the machine: {m['repo']} runs {m['tests']} tests since {m['since']}"
        lines.append(what + (", priority" if m.get("priority") else "")
                     + (f" (order: {m['order']})" if m.get("order") is not None else ""))  # #233 rule 4
    for d in snap["landed"]:
        word = "landed" if d.get("rc") == 0 else "did not land"
        why = "" if d.get("rc") == 0 else f", reason {d.get('reason') or 'unknown'}"
        # #345: a fallback's place (the PC ran it, GitHub's reason) is named on the line, landed or not
        place = d.get("suite_place") or ""
        place = f", {place}" if place.startswith("suite on the PC (GitHub") else ""
        lines.append(f"{word}: {d['branch']} ({tix(d['issues'])}) {d['landed']}, queue to land "
                     f"{human_duration(d['queued_to_land_s'])}, rc {d['rc']}{why}{place}")
    return "\n".join(lines)


def add_subparser(sub) -> None:
    p = sub.add_parser("merge-status", help="read-only snapshot of the merge line (running suite, waiters, landed)")
    p.add_argument("--json", action="store_true", help="print the snapshot as JSON (raw seconds)")


def run_merge_status_command(args) -> int:
    snap = collect()
    print(json.dumps(snap, indent=2) if getattr(args, "json", False) else render_text(snap))
    return 0
