"""`python -m tools.agentops loop-stop` (#924 unit 2): replay the counter over a ticket's comments, and count false stops.

`--replay N --ids a,b,c [--owner-ids x] [--brain-ids y] [--merge-ids m] [--landed-ids l]`: the listed comment ids are
send-backs (owner ids: the owner's answers, brain ids: the brain's answers, #209; merge ids: merge results that did not
land, landed ids: merge results that landed, #209), fed to `loop_status` in time order. `--false-stops --days 14`:
tickets closed in the window, by markers and by a proxy from the outcome log (>= 3 rows of class `redone` /
`wrong-fact`). `gh` output is injected in tests.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .gh_call import run_gh
from .loop_stop import BRAIN_ANSWER, MERGE_RESULT, OWNER_ANSWER, SEND_BACK, loop_status
from .outcomes import DEFAULT_OUTCOMES_LOG, load_jsonl

PROXY_MIN_ROWS = 3
PROXY_CLASSES = ("redone", "wrong-fact")


def _gh(args: list[str]) -> str:
    return run_gh(args, check=True).stdout


def fetch_comments(issue: int) -> list[dict]:
    """Comments with the numeric `id` (gh api, all pages), as `{id, createdAt, body}`."""
    pages = json.loads(_gh(["api", f"repos/{{owner}}/{{repo}}/issues/{issue}/comments", "--paginate", "--slurp"]))
    return [{"id": c["id"], "createdAt": c["created_at"], "body": c.get("body") or ""}
            for page in pages for c in page]


def fetch_closed(days: int, now: datetime) -> list[dict]:
    since = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    out = _gh(["issue", "list", "--state", "closed", "--search", f"closed:>={since}",
               "--limit", "200", "--json", "number,closedAt"])
    return json.loads(out)


def _dt(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _local(stamp: str) -> str:
    return _dt(stamp).astimezone().strftime("%d.%m %H:%M")


#: the body each replayed kind is fed to the counter as (the merge bodies carry the first two lines a real one has)
_KIND_BODY = {
    "owner-answer": OWNER_ANSWER,
    "brain-answer": BRAIN_ANSWER,
    "send-back": SEND_BACK,
    "merge-failed": f"{MERGE_RESULT}\nMerge of replay did not land (replayed)",
    "merge-landed": f"{MERGE_RESULT}\nMerge of replay landed on main (replayed)",
}


def _kind(cid: int, owner_ids, brain_ids, landed_ids, merge_ids) -> str:
    if cid in owner_ids:
        return "owner-answer"
    if cid in brain_ids:
        return "brain-answer"
    if cid in landed_ids:
        return "merge-landed"
    if cid in merge_ids:
        return "merge-failed"
    return "send-back"


def replay(comments: list[dict], send_ids: set[int], owner_ids: set[int],
           brain_ids: set[int] | frozenset[int] = frozenset(),
           merge_ids: set[int] | frozenset[int] = frozenset(),
           landed_ids: set[int] | frozenset[int] = frozenset()) -> dict:
    """Feed the counter in time order. Returns rows `{id, createdAt, kind, count, by_count}`, the row where the
    failed-attempt count first reaches the stop (`count_fire`), and the count at the end (`final_count`)."""
    ids = send_ids | owner_ids | brain_ids | merge_ids | landed_ids
    ordered = sorted((c for c in comments if int(c["id"]) in ids), key=lambda c: c["createdAt"])
    fed: list[dict] = []
    rows, count_fire = [], None
    for c in ordered:
        cid = int(c["id"])
        kind = _kind(cid, owner_ids, brain_ids, landed_ids, merge_ids)
        fed.append({"body": _KIND_BODY[kind], "createdAt": c["createdAt"]})
        st = loop_status(fed)
        rows.append({"id": cid, "createdAt": c["createdAt"], "kind": kind, "count": st.count,
                     "by_count": st.by_count})
        if st.by_count and count_fire is None:
            count_fire = rows[-1]
    return {"rows": rows, "count_fire": count_fire, "final_count": rows[-1]["count"] if rows else 0}


def render_replay(res: dict) -> str:
    lines = ["| # | comment id | createdAt (local) | kind | failed in a row | stopped |", "|---|---|---|---|---|---|"]
    for i, r in enumerate(res["rows"], 1):
        stopped = "by count" if r["by_count"] else ""
        lines.append(f"| {i} | {r['id']} | {_local(r['createdAt'])} | {r['kind']} | {r['count']} | {stopped} |")
    cf = res["count_fire"]
    if cf:
        n = next(i for i, r in enumerate(res["rows"], 1) if r is cf)
        lines.append(f"\nstop by count fires at row {n} (comment {cf['id']}, {_local(cf['createdAt'])})")
    else:
        lines.append("\nstop by count never fires")
    lines.append(f"failed attempts in a row at the end: {res['final_count']}")
    return "\n".join(lines)


def false_stops(closed: list[dict], comments_of, outcomes: list[dict], now: datetime) -> dict:
    """`closed`: dicts with `number`; `comments_of(n)` gives the comments. Marker count: stopped by `loop_status`.
    Proxy: >= 3 outcome rows of a class in PROXY_CLASSES."""
    numbers = {int(i["number"]) for i in closed}
    marker = sorted(n for n in numbers if loop_status(comments_of(n), now).stopped)
    per: dict[int, int] = {}
    for o in outcomes:
        n = o.get("issue")
        if n in numbers and o.get("class") in PROXY_CLASSES:
            per[n] = per.get(n, 0) + 1
    proxy = sorted(n for n, k in per.items() if k >= PROXY_MIN_ROWS)
    return {"closed": len(numbers), "marker": marker, "proxy": proxy}


def render_false_stops(res: dict, days: int) -> str:
    fmt = lambda xs: ", ".join(f"#{n}" for n in xs) or "none"
    return (f"closed in the last {days} days: {res['closed']}\n"
            f"stopped by markers: {len(res['marker'])} ({fmt(res['marker'])})\n"
            f"proxy (>= {PROXY_MIN_ROWS} outcome rows redone/wrong-fact): {len(res['proxy'])} ({fmt(res['proxy'])})")


def add_subparser(sub) -> None:
    p = sub.add_parser("loop-stop", help="replay the loop-stop counter over a ticket, or count false stops (#924)")
    p.add_argument("--replay", type=int, help="issue number to replay")
    p.add_argument("--ids", default="", help="comma separated comment ids that are send-backs")
    p.add_argument("--owner-ids", default="", help="comma separated comment ids that are the owner's answers")
    p.add_argument("--brain-ids", default="", help="comma separated comment ids that are the brain's answers (#209)")
    p.add_argument("--merge-ids", default="", help="comma separated comment ids of merge results that did not land (#209)")
    p.add_argument("--landed-ids", default="", help="comma separated comment ids of merge results that landed (#209)")
    p.add_argument("--false-stops", action="store_true", help="count stops over recently closed issues")
    p.add_argument("--days", type=int, default=14)


def _ids(text: str) -> set[int]:
    return {int(x) for x in text.split(",") if x.strip()}


def run_loop_stop_command(args, *, fetch=fetch_comments, closed_fn=fetch_closed, outcomes_path=None) -> int:
    if args.replay:
        print(render_replay(replay(fetch(args.replay), _ids(args.ids), _ids(args.owner_ids),
                                   _ids(getattr(args, "brain_ids", "")),
                                   merge_ids=_ids(getattr(args, "merge_ids", "")),
                                   landed_ids=_ids(getattr(args, "landed_ids", "")))))
        return 0
    if args.false_stops:
        now = datetime.now(timezone.utc)
        res = false_stops(closed_fn(args.days, now), fetch, load_jsonl(outcomes_path or DEFAULT_OUTCOMES_LOG), now)
        print(render_false_stops(res, args.days))
        return 0
    print("loop-stop: give --replay N --ids ... or --false-stops")
    return 2
