"""`areas check` / `areas apply`: every issue carries exactly one area label (issue #317).

`check` is read-only. `apply` hands the plan to `tools.agentops.areas_apply`; this module owns
the gh access (`GhAreas`), the pure check and the CLI wiring. Stdlib only.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from tools.agentops import core_settings
from tools.agentops.gh_call import run_gh

NO_AREAS_MESSAGE = "no areas in config/orchestration.json"
_RATE_MARKS = ("secondary rate limit", "was submitted too quickly")


class AreasError(RuntimeError):
    pass


@dataclass(frozen=True)
class Issue:
    number: int
    id: int
    state: str  # "open" | "closed"
    labels: tuple[str, ...]
    parent: int | None  # the native parent's number
    title: str = ""


def _default_runner(args: list[str], data: bytes | None = None):
    return run_gh(args, input=data, text=False)


def _parse_pages(text: str) -> list:
    """`gh api --paginate` may print several JSON arrays back to back (or one `--slurp` array
    of arrays): return the flat list of items."""
    dec = json.JSONDecoder()
    items: list = []
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        value, i = dec.raw_decode(text, i)
        if isinstance(value, list):
            for x in value:
                if isinstance(x, list):
                    items.extend(x)
                else:
                    items.append(x)
        else:
            items.append(value)
    return items


class GhAreas:
    def __init__(self, repo: str, runner=_default_runner, pace_seconds: float = 1.0, sleep=time.sleep):
        self.repo, self.runner, self.pace_seconds, self.sleep = repo, runner, pace_seconds, sleep

    def _run(self, args: list[str], payload=None):
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            args = [*args, "--input", "-"]
        r = self.runner(args, data)
        out = (r.stdout or b"").decode("utf-8", "replace")
        err = (r.stderr or b"").decode("utf-8", "replace")
        return r.returncode, out, err, args

    def _read(self, args: list[str]) -> list:
        code, out, err, args = self._run(args)
        if code != 0:
            raise AreasError(f"gh {' '.join(args[:3])} failed: {err.strip()[:200]}")
        return _parse_pages(out)

    def _write(self, args: list[str], payload=None) -> None:
        for attempt in range(4):
            self.sleep(self.pace_seconds)
            code, _out, err, args2 = self._run(args, payload)
            if code == 0:
                return
            low = err.lower()
            if any(m in low for m in _RATE_MARKS) and attempt < 3:
                self.sleep(60)
                continue
            raise AreasError(f"gh {' '.join(args2[:4])} failed: {err.strip()[:200]}")

    def load_issues(self) -> dict[int, Issue]:
        raw = [i for i in self._read(["api", f"repos/{self.repo}/issues?state=all&per_page=100", "--paginate"])
               if "pull_request" not in i]
        parent_of: dict[int, int] = {}
        for i in raw:
            if (i.get("sub_issues_summary") or {}).get("total", 0) > 0:
                kids = self._read(["api", f"repos/{self.repo}/issues/{i['number']}/sub_issues?per_page=100",
                                   "--paginate"])
                for k in kids:
                    parent_of[k["number"]] = i["number"]
        return {
            i["number"]: Issue(
                number=i["number"], id=i["id"], state=i["state"],
                labels=tuple(sorted(lb["name"] if isinstance(lb, dict) else lb for lb in i.get("labels", []))),
                parent=parent_of.get(i["number"]), title=i.get("title") or "",
            )
            for i in raw
        }

    def label_names(self) -> set[str]:
        return {lb["name"] for lb in self._read(["api", f"repos/{self.repo}/labels?per_page=100", "--paginate"])}

    def add_label(self, n: int, label: str) -> None:
        self._write(["issue", "edit", str(n), "--repo", self.repo, "--add-label", label])

    def remove_label(self, n: int, label: str) -> None:
        self._write(["issue", "edit", str(n), "--repo", self.repo, "--remove-label", label])

    def attach(self, parent: int, child_id: int) -> None:
        self._write(["api", "-X", "POST", f"repos/{self.repo}/issues/{parent}/sub_issues"],
                    {"sub_issue_id": child_id, "replace_parent": True})


@dataclass
class CheckResult:
    counts: dict[str, dict[str, int]] = field(default_factory=dict)  # area -> open/closed/all
    total: int = 0
    no_area: list[int] = field(default_factory=list)
    two_or_more: list[int] = field(default_factory=list)
    off_parent: list[int] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.no_area or self.two_or_more or self.off_parent)


def check(issues: dict[int, Issue], areas: list[str]) -> CheckResult:
    aset = set(areas)
    res = CheckResult(counts={a: {"open": 0, "closed": 0, "all": 0} for a in areas}, total=len(issues))
    own: dict[int, list[str]] = {n: [lb for lb in i.labels if lb in aset] for n, i in issues.items()}
    for n in sorted(issues):
        mine = own[n]
        if not mine:
            res.no_area.append(n)
        elif len(mine) > 1:
            res.two_or_more.append(n)
        for a in mine:
            res.counts[a]["closed" if issues[n].state == "closed" else "open"] += 1
            res.counts[a]["all"] += 1
    for n in sorted(issues):
        p = issues[n].parent
        if p is None or p not in own or len(own[n]) != 1 or len(own[p]) != 1:
            continue
        if own[n][0] != own[p][0]:
            res.off_parent.append(n)
    return res


def _nums(ns: list[int]) -> str:
    return ", ".join(f"#{n}" for n in ns) if ns else "-"


def format_check(result: CheckResult, repo: str) -> str:
    lines = [f"{a}  open {c['open']}  closed {c['closed']}  all {c['all']}" for a, c in result.counts.items()]
    lines.append(f"total  {result.total}")
    lines.append(f"without an area ({len(result.no_area)}): {_nums(result.no_area)}")
    lines.append(f"two or more areas ({len(result.two_or_more)}): {_nums(result.two_or_more)}")
    lines.append(f"off the parent's area ({len(result.off_parent)}): {_nums(result.off_parent)}")
    for a in result.counts:
        lines.append(f"https://github.com/{repo}/issues?q=is%3Aissue+label%3A{a.replace(':', '%3A')}")
    return "\n".join(lines)


def add_subparser(sub) -> None:
    p = sub.add_parser("areas", help="one area label per issue: check / apply a plan (issue #317)")
    asub = p.add_subparsers(dest="areas_command", required=True)
    asub.add_parser("check", help="read-only: every issue has exactly one area, a sub-issue its parent's")
    ap = asub.add_parser("apply", help="apply an area plan (JSON) to the issues; prints the write count and time estimate first, "
                                       "a progress line every 20 writes; a rerun skips what is already applied (unchanged N)")
    ap.add_argument("plan")
    ap.add_argument("--dry-run", action="store_true", help="print what would change, write nothing")
    asub.add_parser("propose", help="read-only: roots, loners and labels in use, for proposing the areas")


def run_areas(args, gh: GhAreas | None = None) -> int:
    settings = core_settings.load()
    areas = list(settings.get("areas") or [])
    if args.areas_command == "propose":
        from tools.agentops.areas_propose import run_propose

        return run_propose(settings, gh or GhAreas(settings["repo"]))
    if not areas:
        print(NO_AREAS_MESSAGE)
        return 2
    repo = settings["repo"]
    gh = gh or GhAreas(repo)
    if args.areas_command == "check":
        result = check(gh.load_issues(), areas)
        print(format_check(result, repo))
        return 0 if result.clean else 1
    issues = gh.load_issues()
    have = gh.label_names()
    for a in areas:
        if a not in have:
            print(f"error: label {a!r} does not exist in {repo}; this tool creates no label")
            return 2
    with open(args.plan, encoding="utf-8") as fh:
        plan = json.load(fh)
    from tools.agentops.areas_apply import ProgressWriter, apply_plan, count_writes, estimate_line

    writer = gh
    if not args.dry_run:
        total = count_writes(issues, plan, areas)
        print(estimate_line(total, gh.pace_seconds), flush=True)
        writer = ProgressWriter(gh, total, emit=lambda s: print(s, flush=True))
    for line in apply_plan(issues, plan, areas, writer=writer, dry_run=args.dry_run):
        print(line)
    return 0
