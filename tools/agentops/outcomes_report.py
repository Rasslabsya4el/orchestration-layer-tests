"""Tables 2-4 of `outcomes report`: pure functions over injected inputs (outcome rows, issue
labels, token-meter numbers per issue, Skill counts per session). No transcript read happens here;
the only file or `gh` reads are `load_labels` (the `--labels` JSON file) and `read_issue_list` (the
`gh issue list` call, through `gh_call.run_gh`, when no file is given). A number nobody measured prints "-"."""

from __future__ import annotations

import json
import statistics
import sys
import threading
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from tools.agentops import core_settings
from tools.agentops import outcomes as oc
from tools.agentops.core_format import human_duration
from tools.agentops.gh_call import run_gh


def issue_list_args() -> list[str]:
    """The `gh` call that replaces the `--labels` file: one call, enough rows for the whole tracker, in the
    tickets repo (#323)."""

    return ["issue", "list", "--repo", core_settings.tickets_repo(), "--state", "all", "--limit", "5000",
            "--json", "number,labels,state,closedAt"]


def labels_from_list(data: list) -> dict[int, dict]:
    """The parsed list `gh issue list --state all --json number,labels,state,closedAt` prints, as
    `{issue: {"labels": [names], "state", "closedAt"}}`."""

    out = {}
    for item in data:
        names = [(lb.get("name") if isinstance(lb, dict) else lb) for lb in item.get("labels") or []]
        out[int(item["number"])] = {"labels": names, "state": item.get("state"),
                                    "closedAt": item.get("closedAt")}
    return out


def load_labels(path: Path) -> dict[int, dict]:
    """`labels_from_list` on the JSON file given by `--labels`."""

    return labels_from_list(json.loads(Path(path).read_text(encoding="utf-8")))


def read_issue_list() -> tuple[dict[int, dict], str | None]:
    """`(labels, None)` from one `gh issue list` call, or `({}, reason)` when gh fails (a nonzero return
    code, a timeout, bad JSON): the caller prints "-" for the closed count, never a 0."""

    try:
        res = run_gh(issue_list_args())
    except Exception as exc:  # noqa: BLE001 - a missing gh or a timeout is one reason among the others
        return {}, f"gh call failed: {exc}"
    if res.returncode != 0:
        first = next((ln.strip() for ln in str(res.stderr or "").splitlines() if ln.strip()), "no message")
        return {}, f"gh exit {res.returncode}: {first}"
    try:
        return labels_from_list(json.loads(res.stdout or "")), None
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return {}, f"bad JSON: {exc}"


def area_of(info: dict | None) -> str:
    areas = [lb[len("area:"):] for lb in (info or {}).get("labels") or [] if lb.startswith("area:")]
    return "+".join(areas) if areas else "-"


def _role(folded: str) -> str:
    return folded.split("/", 1)[0].split(":", 1)[0]


def _iso(raw: str | None) -> float | None:
    if not raw:
        return None
    return datetime.fromisoformat(raw.strip().replace("Z", "+00:00")).timestamp()


def _lookup(d: dict | None, issue):
    d = d or {}
    return d.get(issue, d.get(str(issue)))


def _window_rows(outcomes, since, until, agents_dir) -> list[dict]:
    lo, hi = since.timestamp(), until.timestamp()
    rows = []
    for o in outcomes:
        ts = o.get("ts")
        if not isinstance(ts, (int, float)) or not (lo <= ts < hi):
            continue
        rows.append({**o, "_route": oc._fold_route(o.get("route") or "-", agents_dir, ts)})
    return rows


def per_issue(outcomes, labels, meter, since, until, agents_dir=None) -> list[dict]:
    by_issue: dict = defaultdict(list)
    for o in _window_rows(outcomes, since, until, agents_dir):
        if o.get("issue") is not None:
            by_issue[o["issue"]].append(o)
    out = []
    for issue in sorted(by_issue):
        rows = by_issue[issue]
        cnt = Counter(r["_route"] for r in rows)
        units = {r["unit"] for r in rows if r.get("unit")}
        m = _lookup(meter, issue)
        out.append({
            "issue": issue,
            "area": area_of(_lookup(labels, issue)),
            "routes": ", ".join(f"{k} x{cnt[k]}" for k in sorted(cnt)),
            "units": len(units) if units else None,
            "orch_calls": m.get("orch_calls") if m else None,
            "cost_usd": m.get("cost_usd") if m else None,
        })
    return out


def format_per_issue(rows: list[dict]) -> str:
    if not rows:
        return "no issues with delegations"
    header = f"{'issue':>6}  {'area':<14}{'units':>6}{'ORCH calls':>12}{'$':>9}  routes"
    lines = [header, "-" * len(header)]
    for r in rows:
        units = "-" if r["units"] is None else str(r["units"])
        calls = "-" if r["orch_calls"] is None else str(r["orch_calls"])
        cost = "-" if r["cost_usd"] is None else f"${r['cost_usd']:.2f}"
        lines.append(f"{'#' + str(r['issue']):>6}  {r['area']:<14}{units:>6}{calls:>12}{cost:>9}  {r['routes']}")
    return "\n".join(lines)


def pilot(outcomes, labels, totals, skill_counts, since, until, agents_dir=None, labels_error=None) -> dict:
    """`totals` = {"orch_calls": ORCH own real turns in the window, "cost_usd": $ of the six
    orchestration roles in the window} (research doc model-routing-5-5.md:40-56);
    both are divided by the issues closed in the window. A missing total prints "-".
    `labels_error` (the reason the issue list was not read) makes the closed count None: "-", not 0."""

    lo, hi = since.timestamp(), until.timestamp()
    closed = [i for i, info in (labels or {}).items()
              if (c := _iso(info.get("closedAt"))) is not None and lo <= c < hi]
    totals = totals or {}
    n = None if labels_error else len(closed)
    calls, costs = totals.get("orch_calls"), totals.get("cost_usd")
    own = Counter(r.get("class") for r in _window_rows(outcomes, since, until, agents_dir)
                  if r["_route"].startswith("orch"))
    skills = list((skill_counts or {}).values())
    return {
        "closed": n,
        "labels_error": labels_error,
        "orch_calls_per_closed": calls / n if calls is not None and n else None,
        "cost_per_closed": costs / n if costs is not None and n else None,
        "own": {c: own.get(c, 0) for c in ("accepted", "redone", "wrong-fact", "abandoned")},
        "skill_median": statistics.median(skills) if skills else None,
        "skill_max": max(skills) if skills else None,
    }


def format_pilot(p: dict) -> str:
    calls = "-" if p["orch_calls_per_closed"] is None else f"{p['orch_calls_per_closed']:.0f}"
    cost = "-" if p["cost_per_closed"] is None else f"${p['cost_per_closed']:.1f}"
    o = p["own"]
    own = f"{o['accepted']}/{o['redone']}/{o['wrong-fact']}/{o['abandoned']}"
    med = "-" if p["skill_median"] is None else f"{p['skill_median']:g}"
    mx = "-" if p["skill_max"] is None else str(p["skill_max"])
    closed = "-" if p["closed"] is None else str(p["closed"])
    not_read = "" if not p.get("labels_error") else f"issue list: not read ({p['labels_error']})\n"
    return (f"closed issues: {closed}\n"
            f"{not_read}"
            f"ORCH calls per closed issue: {calls}\n"
            f"$ per closed issue: {cost}\n"
            f"orch own units a/r/w/x: {own}\n"
            f"Skill per ORCH session: median {med}, max {mx}")


def split_shares(outcomes, labels, since, until, agents_dir=None) -> dict:
    hi = until.timestamp()
    roles: dict = defaultdict(set)
    for r in _window_rows(outcomes, since, until, agents_dir):
        if r.get("issue") is not None:
            roles[r["issue"]].add(_role(r["_route"]))
    area_rows: dict = defaultdict(lambda: [0, 0, 0])
    total = ws = both = outside = open_n = 0
    for issue, rs in roles.items():
        info = _lookup(labels, issue)
        area = area_of(info)
        is_ws, is_w = "worker-strong" in rs, "worker" in rs
        total += 1
        ws += is_ws
        both += is_ws and is_w
        if is_ws and area != "builds":
            outside += 1
        c = _iso((info or {}).get("closedAt"))
        if info and (info.get("state") == "OPEN" or (c is not None and c >= hi)):
            open_n += 1
        a = area_rows[area]
        a[0] += 1
        a[1] += is_ws
        a[2] += is_ws and is_w
    return {"delegations": total, "worker_strong": ws, "both": both,
            "ws_outside_builds": outside, "open_in_window": open_n,
            "by_area": {k: tuple(v) for k, v in sorted(area_rows.items())}}


def format_split(s: dict) -> str:
    lines = [f"issues with delegations: {s['delegations']}",
             f"with worker-strong: {s['worker_strong']}",
             f"with worker and worker-strong: {s['both']}",
             f"worker-strong outside area builds: {s['ws_outside_builds']}",
             f"open in the window: {s['open_in_window']}",
             f"{'area':<16}{'delegations':>12}{'worker-strong':>15}{'both':>6}"]
    for area, (d, w, b) in s["by_area"].items():
        lines.append(f"{area:<16}{d:>12}{w:>15}{b:>6}")
    return "\n".join(lines)


def build_report(*, outcomes, labels, meter, totals, skill_counts, since, until, agents_dir=None,
                 labels_error=None) -> str:
    return "\n\n".join([
        "Per issue\n" + format_per_issue(per_issue(outcomes, labels, meter, since, until, agents_dir)),
        "Pilot\n" + format_pilot(pilot(outcomes, labels, totals, skill_counts, since, until, agents_dir,
                                       labels_error=labels_error)),
        "Split shares\n" + format_split(split_shares(outcomes, labels, since, until, agents_dir)),
    ])


# ------------------------------------------------------------------ table 1 and the CLI ----


def format_routes_with_drift(rows: list[dict], drift: dict[str, dict]) -> str:
    """The `routes` table plus a `drift/spawns` column; drift is looked up by the route's agent
    type (the text before "/"). "-" when no transcript spawn of that type is in the window."""

    if not rows:
        return "no outcome lines"
    base = oc.format_routes_table(rows).splitlines()
    out = [base[0] + f"{'drift/spawns':>14}", base[1] + "-" * 14]
    for row, line in zip(rows, base[2:]):
        d = drift.get(row["route"].split("/", 1)[0])
        cell = "-"
        if d and d.get("spawns"):
            cell = f"{'-' if d['drift'] is None else d['drift']}/{d['spawns']}"
        out.append(line + f"{cell:>14}")
    return "\n".join(out)


SIX_ROLES = ("worker", "worker-strong", "researcher-find", "researcher-how")


def meter_numbers(turns, prices, since, until) -> tuple[dict, dict]:
    """From token-meter turns: ({issue: {orch_calls, cost_usd}}, window totals {orch_calls,
    cost_usd}). ORCH calls = ORCH main-session real turns (not `<synthetic>`, not 0 tokens),
    split over issues by the meter's own weights; cost_usd of an issue = all its attributed
    turns; the totals' cost = the six orchestration roles (brain, ORCH own, worker,
    worker-strong, researcher-find, researcher-how)."""

    tm = oc._token_meter()
    per: dict = defaultdict(lambda: {"orch_calls": 0.0, "cost_usd": 0.0})
    orch_total, six_cost = 0, 0.0
    for t in turns:
        if not tm.in_window(t, since, until) or t.model in (None, "<synthetic>"):
            continue
        if t.context + t.output_tokens == 0:
            continue
        is_orch = (not t.is_subagent) and bool(t.title and tm.ORCH_NAME_RE.match(t.title))
        cost = tm.turn_cost(t, prices)
        if is_orch:
            orch_total += 1
        if cost is not None and (is_orch or tm.is_brain_turn(t) or t.agent_type in SIX_ROLES):
            six_cost += cost
        for key, w in tm.issue_group_weights(t).items():
            if not key.startswith("#"):
                continue
            row = per[int(key[1:])]
            if is_orch:
                row["orch_calls"] += w
            if cost is not None:
                row["cost_usd"] += cost * w
    meter = {i: {"orch_calls": round(v["orch_calls"]), "cost_usd": v["cost_usd"]} for i, v in per.items()}
    return meter, {"orch_calls": orch_total, "cost_usd": six_cost}


def add_subparser(sub) -> None:
    outcomes_cmd = sub.add_parser("outcomes", help="reports over the outcome log")
    inner = outcomes_cmd.add_subparsers(dest="outcomes_command", required=True)
    rep = inner.add_parser("report", help="routes, per issue, pilot line and split shares for a window")
    rep.add_argument("--since", required=True, help="a date or ISO timestamp, local time unless it has an offset")
    rep.add_argument("--until", required=True)
    rep.add_argument("--labels", type=Path,
                     help="JSON of `gh issue list --state all --json number,labels,state,closedAt` (offline option); "
                          "without it the report reads the same fields from GitHub in one call")
    rep.add_argument("--projects-dir", type=Path, help="Claude transcripts dir (default: the meter's)")
    rep.add_argument("--max-seconds", type=float, default=50.0,
                     help="total wall bound: past it the sections done stay printed, one stderr line names the "
                          "running step, exit 2 (default 50; 0 = no bound)")


class _Abandoned(Exception):
    """Raised inside the worker once the wall bound has passed: the worker stops at its next step."""


class _Sections:
    """Prints the report from the worker thread. A step's name goes to stderr before the step starts.
    `close` (called when the bound passes) ends the printing for good: the sections already printed stay,
    and nothing after is printed. The lock makes the expiry line come after any section in flight."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._closed = False
        self.step = "setup"

    def begin(self, name: str) -> None:
        with self._lock:
            if self._closed:
                raise _Abandoned()
            self.step = name
            print(f"outcomes report: step {name} ...", file=sys.stderr, flush=True)

    def section(self, text: str) -> None:
        with self._lock:
            if self._closed:
                raise _Abandoned()
            print(text, flush=True)

    def close(self) -> str:
        with self._lock:
            self._closed = True
            return self.step


def _report_steps(args, sections: _Sections) -> None:
    from tools.agentops import outcomes_transcripts as ot

    tm = oc._token_meter()
    since, until = tm.parse_date_arg(args.since), tm.parse_date_arg(args.until)
    projects_dir = args.projects_dir or tm.DEFAULT_PROJECTS_DIR
    labels, labels_error = (load_labels(args.labels), None) if args.labels else read_issue_list()
    log = oc.load_jsonl(oc.DEFAULT_OUTCOMES_LOG)
    lo, hi = since.timestamp(), until.timestamp()
    window = [o for o in log if isinstance(o.get("ts"), (int, float)) and lo <= o["ts"] < hi]
    prices = tm.load_prices(tm.DEFAULT_PRICES_PATH)

    sections.begin("routes")
    route_rows = oc.routes(outcomes=window, projects_dir=projects_dir, prices=prices)
    drift = ot.drift_by_agent_type(projects_dir=projects_dir, since=since, until=until)
    sections.section("Routes\n" + format_routes_with_drift(route_rows, drift) + "\n")

    sections.begin("scan")
    scanned = tm.scan_projects_dir(
        projects_dir, dispatch_offers=tm.load_dispatch_offers(tm.DEFAULT_DISPATCH_OUT),
        orch_session_held=tm.load_orch_session_held(tm.DEFAULT_ORCH_SESSION_JSON), since=since, until=until)
    meter, totals = meter_numbers(scanned.turns, prices, since, until)

    sections.begin("skills")
    skills = ot.skill_calls_per_orch_session(projects_dir=projects_dir, since=since, until=until, scanned=scanned)
    sections.section(build_report(outcomes=log, labels=labels, meter=meter, totals=totals,
                                  skill_counts=skills, since=since, until=until, labels_error=labels_error))


def run_report(args) -> int:
    """Prints each section as soon as it is ready and one stderr line before each step. The steps run in a
    daemon worker thread; past `--max-seconds` (0 = no bound) the main thread stops waiting, prints one stderr
    line naming the running step, and returns 2. The abandoned worker is not killed; it prints nothing more."""

    sections = _Sections()
    failure: list[BaseException] = []

    def work() -> None:
        try:
            _report_steps(args, sections)
        except _Abandoned:
            pass
        except BaseException as exc:  # re-raised in the calling thread below
            failure.append(exc)

    worker = threading.Thread(target=work, name="outcomes-report", daemon=True)
    worker.start()
    limit = float(args.max_seconds)
    worker.join(limit if limit > 0 else None)
    if worker.is_alive():
        step = sections.close()
        print(f"outcomes report: out of time ({human_duration(limit)}) in step {step}; partial output above",
              file=sys.stderr, flush=True)
        return 2
    if failure:
        raise failure[0]
    return 0
