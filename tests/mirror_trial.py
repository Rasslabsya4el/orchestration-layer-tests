"""#425 unit 5: the live trial of the dispatcher's mirror pass. Run by ORCH as `python tests/mirror_trial.py`; not a pytest file.

It drives the real `gh` against the throwaway repo `Rasslabsya4el/orchestration-layer-tests` and nothing else: every write
goes through `write_refusal`, which refuses any write that names another repo. The pass is `Dispatcher._sweep_mirrors()`
over `GhCli(repo=REPO)`, with the dispatcher's `_mirror_repos` patched to the trial's list. It never calls `tick()`, so
no ORCH session starts. The tickets repo of the trial is the throwaway repo too.

Every issue the trial opens carries the marker `[mirror-trial-425 <timestamp>]` in its title; the `finally` block closes
every open issue carrying it, mirrors included. Each row prints `row N | what | result | pass/FAIL`; the summary line is
`mirror-trial: N of 6 rows pass`, then `gh calls: N`. The exit code is 1 on any FAIL.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.agentops import dispatch as dispatch_mod  # noqa: E402
from tools.agentops.dispatch import ClaudeCli, Dispatcher, GhCli  # noqa: E402
from tools.agentops.gh_call import run_gh  # noqa: E402
from tools.agentops.mirror_close import CLOSED_MARK, REOPENED_MARK  # noqa: E402
from tools.agentops.mirror_pass import MIRROR_LABEL  # noqa: E402

REPO = "Rasslabsya4el/orchestration-layer-tests"
BAD_REPO = "Rasslabsya4el/this-repo-does-not-exist-425"
ROWS = 6
LABELS = ("ready-for-agent", "needs-triage", "priority", MIRROR_LABEL)
#: the first line of the close comment the pass writes on a source (mirror_close._close_source).
SOURCE_CLOSE_START = "Closed from the mirror"
#: the first line of every sync comment the pass writes (mirror_close markers).
SYNC_PREFIX = "<!-- mirror-sync:"
GH_TIMEOUT = 120.0
#: the wait before and after a pass that reads a listed repo; see `run_pass`.
SETTLE_S = 15.0
_WRITE_VERBS = {("issue", "create"), ("issue", "close"), ("issue", "comment"), ("issue", "reopen"),
                ("issue", "edit"), ("label", "create")}


# ---- pure helpers (unit-tested in tests/test_mirror_trial.py) ----

def row_line(number: int, what: str, result: str, ok: bool) -> str:
    return f"row {number} | {what} | {result} | {'pass' if ok else 'FAIL'}"


def summary_line(oks: list[bool]) -> str:
    return f"mirror-trial: {sum(1 for ok in oks if ok)} of {ROWS} rows pass"


def exit_code(oks: list[bool]) -> int:
    """0 only when all six rows printed and passed; a row that never printed counts as a failure."""
    return 0 if len(oks) == ROWS and all(oks) else 1


def write_refusal(args: list[str], repo: str) -> str | None:
    """Why a `gh` call must not run, or None. A write (issue create/close/comment/reopen/edit, label create) must name
    `repo` with `--repo` or `-R`; an `api` call must be a GET. Reads of any repo pass."""
    if args[:1] == ["api"]:
        method = _flag(args, "-X", "--method")
        if method is not None and method.upper() != "GET":
            return f"write refused: api {method} is not a read"
        return None
    if tuple(args[:2]) not in _WRITE_VERBS:
        return None
    target = _flag(args, "--repo", "-R")
    if target != repo:
        return f"write refused: {' '.join(args[:2])} names {target or 'no repo'}, not {repo}"
    return None


def is_mirror_of(body: str | None, url: str) -> bool:
    """True when the body's first line is the mirror line of `url`: `Mirror of <url>;` as the whole prefix."""
    first = (body or "").split("\n", 1)[0].rstrip("\r")
    return first.startswith(f"Mirror of {url};")


def first_line(text: str | None) -> str:
    lines = (text or "").strip().splitlines()
    return lines[0] if lines else ""


def _flag(args: list[str], *names: str) -> str | None:
    for i, arg in enumerate(args[:-1]):
        if arg in names:
            return args[i + 1]
    return None


# ---- the live trial ----

class Gh:
    """The trial's one `gh`: every call is counted; a write that names anything but the throwaway repo is refused here."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        refusal = write_refusal(list(args), REPO)
        if refusal is not None:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr=refusal)
        self.calls.append(list(args))
        return run_gh(list(args), cwd=ROOT, input=stdin, timeout=GH_TIMEOUT)


class Trial:
    def __init__(self, stamp: str, gh: Gh, root: Path) -> None:
        self.marker = f"[mirror-trial-425 {stamp}]"
        self.gh = gh
        self.root = root
        self.lines: list[str] = []  # every dispatcher log line the trial's dispatchers wrote
        self.sources: dict[str, tuple[int, str]] = {}  # "A".."D" -> (issue number, URL)


def gh_ok(gh: Gh, args: list[str], stdin: str | None = None) -> str:
    result = gh(args, stdin)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {first_line(result.stderr)}")
    return result.stdout or ""


def all_issues(gh: Gh) -> list[dict]:
    out = gh_ok(gh, ["issue", "list", "--repo", REPO, "--state", "all", "--limit", "1000",
                     "--json", "number,title,body,labels,state,url"])
    return json.loads(out or "[]")


def mirror_total(gh: Gh) -> int:
    out = gh_ok(gh, ["issue", "list", "--repo", REPO, "--label", MIRROR_LABEL, "--state", "all",
                     "--limit", "1000", "--json", "number"])
    return len(json.loads(out or "[]"))


def view(gh: Gh, number: int) -> dict:
    return json.loads(gh_ok(gh, ["issue", "view", str(number), "--repo", REPO, "--json", "number,state,comments,url"]))


def state_of(gh: Gh, number: int) -> str:
    return str(view(gh, number)["state"]).upper()


def bodies_of(gh: Gh, number: int) -> list[str]:
    return [str(c.get("body") or "") for c in view(gh, number).get("comments") or []]


def count_starting(gh: Gh, number: int, prefix: str) -> int:
    return sum(1 for body in bodies_of(gh, number) if first_line(body).startswith(prefix))


def snapshot(gh: Gh, numbers: list[int]) -> dict[int, tuple[str, tuple[str, ...]]]:
    return {n: (state_of(gh, n), tuple(bodies_of(gh, n))) for n in numbers}


def open_issue(gh: Gh, title: str, labels: list[str], body: str) -> tuple[int, str]:
    args = ["issue", "create", "--repo", REPO, "--title", title, "--body-file", "-"]
    for label in labels:
        args += ["--label", label]
    out = gh_ok(gh, args, body)
    match = re.search(r"https://github\.com/\S+/issues/(\d+)", out)
    if match is None:
        raise RuntimeError(f"no issue URL in the answer of issue create: {first_line(out)[:200]}")
    return int(match.group(1)), match.group(0)


def mirrors_named(gh: Gh, url: str) -> list[dict]:
    """Every issue of the throwaway repo, any state, whose first line is the mirror line of `url`."""
    return [issue for issue in all_issues(gh) if is_mirror_of(issue.get("body"), url)]


def only_mirror(gh: Gh, url: str) -> dict:
    found = mirrors_named(gh, url)
    if len(found) != 1:
        raise RuntimeError(f"{len(found)} mirror(s) name {url}, expected 1")
    return found[0]


def labels_of(issue: dict) -> list[str]:
    return [str(label.get("name")) for label in issue.get("labels") or []]


def ready_numbers(gh: Gh) -> list[int]:
    """What the offer reads: the ready list of the throwaway repo, read through a fresh GhCli."""
    return sorted(c.number for c in GhCli(repo=REPO, runner=gh).ready_for_agent_issues())


def dispatcher(gh: Gh, state_dir: Path, lines: list[str]) -> Dispatcher:
    """A dispatcher over a real GhCli of the throwaway repo; its state lives in a scratch folder of this worktree."""
    state_dir.mkdir(parents=True, exist_ok=True)
    return Dispatcher(claude=ClaudeCli(), gh=GhCli(repo=REPO, runner=gh), max_hours=4.0,
                      events_file=state_dir / "events.jsonl", state_file=state_dir / "orch_session.json",
                      projects_dir=state_dir / "projects", log=lines.append)


def run_pass(d: Dispatcher, repos: list[str]) -> None:
    """One mirror pass: `_sweep_mirrors` (not `tick`), with the listed repos patched into the dispatcher's read.
    The trial waits SETTLE_S before and after a pass that reads a listed repo: GitHub's lists lag a fresh write by
    about 6-8 s (probe, 2026-10-29: issue, label and mirror all seen 6-8 s after create), and a pass right after the
    trial's own write misses it (a missed source, or a second mirror). The dispatcher ticks every 30 s
    (`TICK_SECONDS`, dispatch.py:165), so the wait stands in for the real spacing between two cycles."""
    if repos:
        time.sleep(SETTLE_S)
    with mock.patch.object(dispatch_mod, "_mirror_repos", return_value=list(repos)):
        d._sweep_mirrors()
    if repos:
        time.sleep(SETTLE_S)


def _error_after(line: str, repo: str) -> str:
    parts = line.split(repo + ":", 1)
    return parts[1].strip() if len(parts) == 2 else ""


# ---- the rows ----

BODY_MAIN_RED = ("Main is red after the last merge: 1 test failed.\n"
                 "The failing test: tests/test_example.py::test_one.\n"
                 "Run: the last main-check run of the trial's tracker.")
BODY_WORKFLOW = ("The main-check workflow did not end green.\n"
                 "It ended with a failure on the main branch.\n"
                 "Open the run from the workflow page to see the step that failed.")
BODY_SKILLS = ("The skills update found a new version of the skills set.\n"
               "The report lists the skills that moved.\n"
               "Nothing else in this issue needs an answer.")
BODY_OUTSIDE = ("Opened by hand as the outside case of the trial.\n"
                "It is an ordinary ready issue with an unrelated title.\n"
                "Nothing else in this issue needs an answer.")


def row1(t: Trial) -> tuple[bool, str]:
    before = mirror_total(t.gh)
    start = len(t.gh.calls)
    d = dispatcher(t.gh, t.root / "row1", t.lines)
    for _ in range(3):
        run_pass(d, [])
    calls = len(t.gh.calls) - start
    after = mirror_total(t.gh)
    return calls == 0 and after == before, f"{calls} gh calls and {after - before} new mirror issue(s) in 3 runs"


def row2(t: Trial) -> tuple[bool, str]:
    openers = [
        ("A", "Main is red: 1 test fail", ["ready-for-agent", "priority"], "ready-for-agent", BODY_MAIN_RED),
        ("B", "Workflow main-check did not end green", ["needs-triage"], "needs-triage", BODY_WORKFLOW),
        ("C", "His skills moved to a496f5513d4a", ["needs-triage"], "needs-triage", BODY_SKILLS),
    ]
    for key, title, labels, _role, body in openers:
        t.sources[key] = open_issue(t.gh, f"{title} {t.marker}", labels, body)
    run_pass(dispatcher(t.gh, t.root / "row2", t.lines), [REPO])
    good = 0
    for key, _title, _labels, role, _body in openers:
        found = mirrors_named(t.gh, t.sources[key][1])
        if len(found) == 1 and role in labels_of(found[0]) and MIRROR_LABEL in labels_of(found[0]):
            good += 1
    return good == len(openers), f"{good} of {len(openers)} sources with one mirror carrying role label and mirror"


def row3(t: Trial) -> tuple[bool, str]:
    t.sources["D"] = open_issue(t.gh, f"Outside case, opened by hand {t.marker}", ["ready-for-agent"], BODY_OUTSIDE)
    run_pass(dispatcher(t.gh, t.root / "row3", t.lines), [REPO])
    found = mirrors_named(t.gh, t.sources["D"][1])
    offered = ready_numbers(t.gh)
    ok = (len(found) == 1 and "ready-for-agent" in labels_of(found[0]) and MIRROR_LABEL in labels_of(found[0])
          and found[0]["number"] in offered)
    shown = found[0]["number"] if found else "none"
    return ok, f"{len(found)} mirror (#{shown}); in the ready list: {'yes' if found and found[0]['number'] in offered else 'no'}"


def row4(t: Trial) -> tuple[bool, str]:
    start = len(t.lines)
    d = dispatcher(t.gh, t.root / "row4a", t.lines)
    for _ in range(3):
        run_pass(d, [REPO])
    run_pass(dispatcher(t.gh, t.root / "row4b", t.lines), [REPO])  # a brand-new Dispatcher and GhCli, state dropped
    creates = sum(1 for line in t.lines[start:] if " -> #" in line)
    good = sum(1 for _key, (_n, url) in t.sources.items() if len(mirrors_named(t.gh, url)) == 1)
    return good == len(t.sources) == 4, f"{good} of {len(t.sources)} sources with one mirror after 3+1 runs ({creates} new)"


def row5(t: Trial) -> tuple[bool, str]:
    gh = t.gh
    numbers = {key: t.sources[key][0] for key in "ABCD"}
    mirror = {key: int(only_mirror(gh, t.sources[key][1])["number"]) for key in "ABCD"}
    checks: list[tuple[str, bool]] = []

    gh_ok(gh, ["issue", "close", str(mirror["A"]), "--repo", REPO])
    gh_ok(gh, ["issue", "close", str(mirror["B"]), "--repo", REPO])
    run_pass(dispatcher(gh, t.root / "row5a", t.lines), [REPO])
    checks.append(("A,B closed", state_of(gh, numbers["A"]) == "CLOSED" and state_of(gh, numbers["B"]) == "CLOSED"))
    checks.append(("A,B one comment each", all(count_starting(gh, numbers[k], SOURCE_CLOSE_START) == 1 for k in "AB")))

    gh_ok(gh, ["issue", "close", str(numbers["C"]), "--repo", REPO])
    gh_ok(gh, ["issue", "close", str(numbers["D"]), "--repo", REPO])
    run_pass(dispatcher(gh, t.root / "row5b", t.lines), [REPO])
    checks.append(("C,D mirrors closed", state_of(gh, mirror["C"]) == "CLOSED" and state_of(gh, mirror["D"]) == "CLOSED"))
    checks.append(("C,D mirrors one comment each",
                   all(count_starting(gh, mirror[k], SYNC_PREFIX) == 1 for k in "CD")))

    everything = list(numbers.values()) + list(mirror.values())
    before = snapshot(gh, everything)
    run_pass(dispatcher(gh, t.root / "row5c", t.lines), [REPO])
    checks.append(("second run adds no comment", snapshot(gh, everything) == before))

    gh_ok(gh, ["issue", "reopen", str(numbers["A"]), "--repo", REPO])
    run_pass(dispatcher(gh, t.root / "row5d", t.lines), [REPO])
    checks.append(("A reopened", state_of(gh, numbers["A"]) == "OPEN"))
    checks.append(("mirror A reopened once", state_of(gh, mirror["A"]) == "OPEN"
                   and count_starting(gh, mirror["A"], REOPENED_MARK) == 1))

    failed = [name for name, ok in checks if not ok]
    result = f"{len(checks) - len(failed)} of {len(checks)} checks: " + ("all as expected" if not failed else "NOT: " + ", ".join(failed))
    return not failed, result


def row6(t: Trial) -> tuple[bool, str]:
    repos = [REPO, BAD_REPO]
    ready_before = ready_numbers(t.gh)
    d = dispatcher(t.gh, t.root / "row6", t.lines)
    per_run: list[list[str]] = []
    for _ in range(2):
        start = len(t.lines)
        run_pass(d, repos)
        per_run.append(t.lines[start:])
    ready_after = ready_numbers(t.gh)
    named = [[line for line in run if BAD_REPO in line] for run in per_run]
    one_per_run = all(len(hits) == 1 and _error_after(hits[0], BAD_REPO) != "" and "cannot read" in hits[0]
                      for hits in named)
    unchanged = ready_before == ready_after
    total = sum(len(hits) for hits in named)
    return (one_per_run and unchanged,
            f"{total} log line(s) naming {BAD_REPO}, one per run with its error; ready list "
            f"{'unchanged' if unchanged else 'CHANGED'} ({len(ready_after)} ready)")


ROW_RUNS = [
    (1, "empty setting: 3 runs, 0 calls", row1),
    (2, "three named openers: one mirror each", row2),
    (3, "outside case: one mirror, offered", row3),
    (4, "no duplicates: 3+1 runs, 4 of 4", row4),
    (5, "close both ways", row5),
    (6, "unreadable listed repo", row6),
]


def make_labels(gh: Gh) -> None:
    for name in LABELS:
        gh_ok(gh, ["label", "create", name, "--repo", REPO, "--force", "--color", "ededed"])


def cleanup(gh: Gh, marker: str) -> int:
    """Close every open issue of the throwaway repo that carries the marker, mirrors included. GitHub's lists lag a
    fresh write, so it lists twice with a settle wait between; one failed close never stops the others."""
    closed = 0
    for round_ in range(2):
        if round_:
            time.sleep(SETTLE_S)
        for issue in all_issues(gh):
            if marker in str(issue.get("title") or "") and str(issue.get("state")).upper() == "OPEN":
                result = gh(["issue", "close", str(issue["number"]), "--repo", REPO], None)
                closed += 1 if result.returncode == 0 else 0
    return closed


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    gh = Gh()
    root = ROOT / "var" / f"mirror-trial-425-{stamp.replace(':', '')}"
    trial = Trial(stamp, gh, root)
    oks: list[bool] = []
    cleanup_ok = True
    try:
        make_labels(gh)
        for number, what, run in ROW_RUNS:
            start = len(trial.lines)
            try:
                ok, result = run(trial)
            except Exception as exc:  # noqa: BLE001 -- a row that raises is a FAIL with its reason
                ok, result = False, f"error: {type(exc).__name__}: {exc}"
            print(row_line(number, what, result, ok))
            for line in trial.lines[start:]:
                print(f"    log: {line}")
            oks.append(ok)
    except Exception as exc:  # noqa: BLE001 -- the setup failing ends the rows; the cleanup still runs
        print(f"setup failed: {type(exc).__name__}: {exc}")
    finally:
        try:
            closed = cleanup(gh, trial.marker)
            print(f"cleanup: closed {closed} open issue(s) carrying {trial.marker}")
        except Exception as exc:  # noqa: BLE001 -- a leftover open issue is a failure of the run
            cleanup_ok = False
            print(f"cleanup FAILED: {type(exc).__name__}: {exc}")
        shutil.rmtree(root, ignore_errors=True)
    print(summary_line(oks))
    print(f"gh calls: {len(gh.calls)}")
    return 0 if exit_code(oks) == 0 and cleanup_ok else 1


if __name__ == "__main__":
    sys.exit(main())
