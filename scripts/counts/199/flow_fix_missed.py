"""#199: core issues created in a day window whose labels at creation lacked `flow-fix` (read-only).
Reads (through one injectable `run_gh(args) -> str`): the open and closed issues labelled `core-issue`, then each
issue's timeline (`gh api repos/{repo}/issues/N/timeline --paginate`). Label set at creation = the `labeled` events
whose created_at is within 120 s of the issue's createdAt. "flow-fix later" = a `labeled` flow-fix event after that
window, or flow-fix in the current labels. Whether a ticket is a flow fix is the closer's reading of the titles; this
script does not guess it. Writes nothing.

Run (repo root):  python -I scripts/counts/199/flow_fix_missed.py <since> <until>
Args: since (first day created, inclusive), until (end day, exclusive); both YYYY-MM-DD.
Output: one line per listed issue, "#N <createdAt> <title> | flow-fix later: yes/no", then the last line
"total: K" (K = the number of listed issues).
"""
import argparse
import datetime
import json
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.normpath(os.path.join(HERE, "..", "..", "..", "config", "orchestration.json"))
CORE_LABEL = "core-issue"
FLOW_FIX = "flow-fix"
CREATE_WINDOW_S = 120


def run_gh(args):
    """The one place a gh process starts (the test fakes this function)."""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    res = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", check=True, timeout=120, **kwargs)
    return res.stdout


def json_docs(text):
    """gh api --paginate prints one JSON array per page; return all their items as one list."""
    dec = json.JSONDecoder()
    out = []
    i = 0
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return out
        obj, i = dec.raw_decode(text, i)
        out.extend(obj if isinstance(obj, list) else [obj])


def parse_ts(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def repo_slug(gh):
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            repo = json.load(fh).get("repo")
    except (OSError, ValueError):
        repo = None
    if repo:
        return repo
    return json.loads(gh(["repo", "view", "--json", "nameWithOwner"]))["nameWithOwner"]


def fetch_core_issues(gh, repo):
    rows = json_docs(gh(["issue", "list", "-R", repo, "--label", CORE_LABEL, "--state", "all",
                         "--limit", "1000", "--json", "number,title,createdAt,labels"]))
    return [i for i in rows if CORE_LABEL in {lab["name"] for lab in i["labels"]}]


def timeline(gh, repo, number):
    return json_docs(gh(["api", f"repos/{repo}/issues/{number}/timeline", "--paginate"]))


def assess(issue, tl):
    """(label names at creation, flow-fix added later) for one issue and its timeline."""
    created = parse_ts(issue["createdAt"])
    events = [(e["label"]["name"], parse_ts(e["created_at"])) for e in tl
              if e.get("event") == "labeled" and e.get("label") and e.get("created_at")]
    at_creation = {name for name, at in events if abs((at - created).total_seconds()) <= CREATE_WINDOW_S}
    current = {lab["name"] for lab in issue["labels"]}
    later = FLOW_FIX in current or any(
        name == FLOW_FIX and (at - created).total_seconds() > CREATE_WINDOW_S for name, at in events)
    return at_creation, later


def missed(gh, repo, since, until):
    """[(issue, flow-fix later)] for core issues created in [since, until) without flow-fix at creation."""
    out = []
    for issue in sorted(fetch_core_issues(gh, repo), key=lambda i: i["number"]):
        if not (since <= issue["createdAt"][:10] < until):
            continue
        at_creation, later = assess(issue, timeline(gh, repo, issue["number"]))
        if FLOW_FIX in at_creation:
            continue
        out.append((issue, later))
    return out


def _day(s):
    return datetime.date.fromisoformat(s).isoformat()


def main(argv=None):
    p = argparse.ArgumentParser(description="Core issues created without flow-fix, in a day window (read-only).")
    p.add_argument("since", type=_day, metavar="YYYY-MM-DD", help="first day created (inclusive)")
    p.add_argument("until", type=_day, metavar="YYYY-MM-DD", help="end day (exclusive)")
    a = p.parse_args(argv)
    gh = run_gh
    rows = missed(gh, repo_slug(gh), a.since, a.until)
    for issue, later in rows:
        print(f"#{issue['number']} {issue['createdAt']} {issue['title']} | "
              f"flow-fix later: {'yes' if later else 'no'}")
    print(f"total: {len(rows)}")


if __name__ == "__main__":
    main()
