"""`merge ... --detach` / `commit ... --detach` (issue #399): run the same command as a process no
Claude Code shell holds, so a session's low-memory stop (a tree kill, a job-object kill) cannot
take a waiting merge with it.

Launch: Windows -- WMI `Win32_Process.Create` through `dispatch._launch_via_wmi` (the child's
parent becomes WmiPrvSE); the child is `pythonw -m tools.agentops.detach run ...` (windowless, the
only program `_launch_via_wmi` accepts under pytest). Elsewhere `Popen(start_new_session=True)`.

The child (`run`) runs the real command, captures rc and output, and writes
`var/agentops/results/<YYYYmmddTHHMMSS>-<kind>-<branch or 'commit'>.json` plus one `merge.log` line.
`pushed_sha` is `git rev-parse origin/main` in the command's `--cwd`, read after rc 0 (a merge's
own output carries only a 12-char head)."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from tools.agentops.util import no_window_flags

ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = ROOT / "var" / "agentops" / "results"
MERGE_LOG = ROOT / "var" / "agentops" / "merge.log"
#: tests (and anyone who wants another place) point the two outputs elsewhere
ENV_RESULTS_DIR = "AGENTOPS_DETACH_DIR"
ENV_MERGE_LOG = "AGENTOPS_DETACH_MERGE_LOG"
#: a test points the hand-back's `gh` at a python script (`script args...`) instead of the real gh
ENV_GH_STUB = "AGENTOPS_GH_STUB"


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))


def result_path_for(kind: str, branch: str | None, results_dir: Path | None = None) -> Path:
    base = results_dir or Path(os.environ.get(ENV_RESULTS_DIR) or RESULTS_DIR)
    label = re.sub(r"[^A-Za-z0-9._-]+", "_", branch) if branch else "commit"
    return base / f"{time.strftime('%Y%m%dT%H%M%S')}-{kind}-{label}.json"


def classify(rc: int, output: str) -> tuple[str, str | None]:
    """(reason, suite_output path or None) from the command's exit code and printed output. The
    merge's own reasons ("red-suite", "conflict", ...) are read from `not merged (<reason>)`."""
    suite = re.search(r"\(the output: (.+?)\)", output)
    suite_path = suite.group(1) if suite else None
    if rc == 0:
        return "pushed", suite_path
    fixed = {2: "setup-error", 3: "lock-timeout", 4: "nothing-to-commit", 5: "already-queued"}
    if rc in fixed:
        return fixed[rc], suite_path
    m = re.search(r"not (?:merged|committed) \(([^)]+)\)", output)
    return (m.group(1) if m else "failed"), suite_path


def silent_holder_note(result_file: Path) -> str:
    """#62: the one line the merge line's next waiter left next to this result file when it took the line
    from this silent merge (`merge.SILENT_HOLDER_MARKER_SUFFIX`); "" when there is none."""
    try:
        return Path(str(result_file) + ".silent-holder").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _cwd_of(argv: list[str]) -> Path:
    for i, a in enumerate(argv):
        if a == "--cwd" and i + 1 < len(argv):
            return Path(argv[i + 1])
        if a.startswith("--cwd="):
            return Path(a[len("--cwd="):])
    return ROOT


FOREIGN_ISSUE = re.compile(r"^[\w.-]+/[\w.-]+#\d+$")


def _issues_of(argv: list[str]) -> list[int]:
    out: list[int] = []
    for i, a in enumerate(argv):
        v = argv[i + 1] if a == "--issue" and i + 1 < len(argv) else (
            a[len("--issue="):] if a.startswith("--issue=") else None)
        if v is not None and v.lstrip("-").isdigit():
            out.append(int(v))
    return out


def _inherited_of(output: str) -> list[int]:
    """#219 rule 2: the issues of the older entry this run superseded (``merge-inherited: 7,9``); [] when none."""
    found = re.findall(r"^merge-inherited: ([\d,]+)\s*$", output, re.M)
    return [int(n) for n in found[-1].split(",") if n] if found else []


def _record_issues(argv: list[str], output: str) -> list[int]:
    """The argv's own `--issue` numbers, then the inherited ones this run does not name already."""
    own = _issues_of(argv)
    return own + [n for n in _inherited_of(output) if n not in own]


def _foreign_issues_of(argv: list[str]) -> list[str]:
    """#145: the `--issue owner/repo#N` values of a merge argv, unchanged."""
    out: list[str] = []
    for i, a in enumerate(argv):
        v = argv[i + 1] if a == "--issue" and i + 1 < len(argv) else (
            a[len("--issue="):] if a.startswith("--issue=") else None)
        if v is not None and FOREIGN_ISSUE.match(v):
            out.append(v)
    return out


def _stats_of(output: str) -> dict:
    found = re.findall(r"^merge-stats: in_line_s=(\S+) suite_s=(\S+) failed=(\S+)\s*$", output, re.M)
    if not found:
        return {"in_line_s": None, "suite_s": None, "failed": None}
    a, b, c = found[-1]

    def conv(x, f):
        try:
            return f(x)
        except ValueError:
            return None
    return {"in_line_s": conv(a, float), "suite_s": conv(b, float), "failed": conv(c, int)}


def _runs_of(output: str) -> dict:
    """#212: ``rebases`` and ``suite_runs`` from the merge's ``merge-runs:`` line; ``None`` each when it is absent."""
    found = re.findall(r"^merge-runs: rebases=(\d+) suite_runs=(\d+)\s*$", output, re.M)
    if not found:
        return {"rebases": None, "suite_runs": None}
    return {"rebases": int(found[-1][0]), "suite_runs": int(found[-1][1])}


def _selection_of(output: str) -> dict:
    """#74 unit 2: the merge's ``merge-selection:`` line (the suite's line 1, e.g. ``full suite: map holds 0 tests
    (map <sha8>)``) as the result record's ``selection``; an empty dict when the merge printed none."""
    found = re.findall(r"^merge-selection: (.*?)\s*$", output, re.M)
    return {"selection": found[-1]} if found else {}


def _tests_of(output: str) -> dict:
    """#900: ``failed_tests`` / ``unfinished`` from the merge's ``merge-tests:`` line; empty lists when absent.
    ``failed`` is the count of ``failed_tests`` once a suite ran (the stats line says the same number)."""
    out: dict = {"failed_tests": [], "unfinished": []}
    found = re.findall(r"^merge-tests: (\{.*\})\s*$", output, re.M)
    if found:
        try:
            data = json.loads(found[-1])
            out = {k: [str(x) for x in data.get(k) or []] for k in out}
        except (ValueError, AttributeError):
            pass
    return out


def _quick_check_of(output: str) -> dict | None:
    """#40: a data commit's quick check, from its `commit-quick-check: {...}` line; None when the commit has none."""
    found = re.findall(r"^commit-quick-check: (\{.*\})\s*$", output, re.M)
    if not found:
        return None
    try:
        data = json.loads(found[-1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _default_gh(stub: str | None = None):
    """The dispatcher's own `GhCli` (repo from the settings, as `Dispatcher` is built)."""
    from tools.agentops.dispatch import GhCli
    stub = stub or os.environ.get(ENV_GH_STUB)
    if not stub:
        return GhCli()

    def runner(args, stdin):
        return subprocess.run([sys.executable, stub, *args], input=stdin, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              creationflags=no_window_flags())
    return GhCli(runner=runner)


class RealGhInTestError(RuntimeError):
    """A test reached the hand-back with the real `gh` (#712): it would comment on the real issue."""


def _refuse_real_gh_in_test(argv: list[str], kind: str, gh) -> None:
    if (kind == "merge" and gh is None and not os.environ.get(ENV_GH_STUB)
            and os.environ.get("PYTEST_CURRENT_TEST") and (_issues_of(argv) or _foreign_issues_of(argv))):
        raise RealGhInTestError(
            f"run_and_record with --issue {_issues_of(argv)} and no stub gh under pytest "
            f"({os.environ['PYTEST_CURRENT_TEST']}): pass gh=<fake> or set {ENV_GH_STUB}")


def _core_targets_gh_runner():
    """The `gh` runner the home's target reads use (issue #1115): None is the real `gh`; tests
    replace this function with a fake."""
    return None


def _core_target_lines(argv: list[str], kind: str, rc: int, pushed_sha: str | None) -> list[str] | None:
    """Issue #1115: after a merge pushed, a home (`update_rule: home`) with a `targets` key runs
    `core update` in each target (`core_targets.run_core_targets`); its lines go in the result record.
    None when the merge is not a pushed merge at a home with targets (the record then has no key)."""
    if kind != "merge" or rc != 0 or not pushed_sha:
        return None
    home_root = _cwd_of(argv)
    try:
        settings = json.loads((home_root / "config" / "orchestration.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    targets = settings.get("targets")
    if settings.get("update_rule") != "home" or not isinstance(targets, list) or not targets:
        return None
    from tools.agentops import core_targets
    try:
        return core_targets.run_core_targets(targets, home_root=home_root,
                                             gh_runner=_core_targets_gh_runner())
    except Exception as exc:  # noqa: BLE001 -- the home's merge rc never changes on a target
        return [f"failed: core targets: {exc!r}"]


def run_and_record(argv: list[str], result_file: Path, kind: str, branch: str | None,
                   merge_log: Path | None = None, gh=None) -> int:
    _refuse_real_gh_in_test(argv, kind, gh)
    started = time.time()
    log = merge_log or Path(os.environ.get(ENV_MERGE_LOG) or MERGE_LOG)
    try:
        # the inner merge writes where the runner's log is (the real one in production)
        env = {**os.environ, "AGENTOPS_MERGE_LOG": str(log),
               "AGENTOPS_MERGE_SUITE_OUTPUT_DIR": str(log.parent / "merge_suite"),
               "AGENTOPS_MERGE_RESULT": str(result_file)}
        proc = subprocess.run(argv, cwd=str(ROOT), env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              creationflags=no_window_flags())
        rc, output = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except OSError as exc:
        rc, output = 127, f"could not start {argv[:1]}: {exc}"
    reason, suite_output = classify(rc, output)
    taken = silent_holder_note(result_file) if rc != 0 else ""
    if taken:  # the line was taken from this merge while it stood silent: its ticket is told why
        reason, output = "silent-holder", output + taken + "\n"
    from tools.agentops import merge_withdraw  # #219 rule 3: a run `merge withdraw` ended says so
    ended_by = merge_withdraw.marker_reason(result_file) if rc != 0 else ""
    if ended_by:  # the marker names the reason (#219 rule 3: "withdrawn"; rule 2: "superseded")
        reason = ended_by
        if ended_by == "superseded":
            output += f"did not land ({merge_withdraw.withdrawn_note(result_file)})\n"
    pushed_sha = None
    if rc == 0:
        rp = subprocess.run(["git", "rev-parse", "origin/main"], cwd=str(_cwd_of(argv)),
                            capture_output=True, text=True, creationflags=no_window_flags())
        if rp.returncode == 0:
            pushed_sha = rp.stdout.strip() or None
    ended = time.time()
    core_lines = _core_target_lines(argv, kind, rc, pushed_sha)
    quick = _quick_check_of(output)
    result_file.parent.mkdir(parents=True, exist_ok=True)
    record = {"kind": kind, "argv": argv, "rc": rc, "pushed_sha": pushed_sha, "reason": reason,
              "suite_output": suite_output, "started": _iso(started), "ended": _iso(ended),
              "output": output[-4000:],
              "issues": _record_issues(argv, output) if kind == "merge" else [],
              "foreign_issues": _foreign_issues_of(argv) if kind == "merge" else [],
              **_stats_of(output), **_tests_of(output), **_selection_of(output),
              **(_runs_of(output) if kind == "merge" else {}),
              **({"core_targets": core_lines} if core_lines is not None else {}),
              **({"quick_check": quick} if quick is not None else {})}
    tmp = result_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(tmp, result_file)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {branch or 'commit'} | "
                 f"detached result rc {rc} | {result_file}\n")
    if kind == "merge" and (record["issues"] or record["foreign_issues"]):
        try:
            from tools.agentops import merge_handback
            merge_handback.post_merge_result(record, str(result_file), branch or "commit",
                                             gh if gh is not None else _default_gh())
        except Exception as exc:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {branch or 'commit'} | "
                         f"hand-back failed: {type(exc).__name__}: {exc}\n")
    return rc


def _windowless_python() -> str:
    w = Path(sys.executable).with_name("pythonw.exe")
    return str(w if w.exists() else sys.executable)


def launch(argv: list[str], kind: str, branch: str | None) -> tuple[Path, int]:
    """Starts the detached runner for `argv` (the full `python -m tools.agentops ...` argv without
    `--detach`); returns (result file, child pid)."""
    result_file = result_path_for(kind, branch)
    runner = [_windowless_python() if os.name == "nt" else sys.executable,
              "-m", "tools.agentops.detach", "run", "--result", str(result_file),
              "--kind", kind, "--branch", branch or "",
              "--log", str(Path(os.environ.get(ENV_MERGE_LOG) or MERGE_LOG)),
              *(["--gh-stub", os.environ[ENV_GH_STUB]] if os.environ.get(ENV_GH_STUB) else []),
              "--", *argv]
    if os.name == "nt":
        from tools.agentops.dispatch import _launch_via_wmi
        pid = _launch_via_wmi(subprocess.list2cmdline(runner), ROOT)
    else:
        pid = subprocess.Popen(runner, cwd=str(ROOT), start_new_session=True,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,
                               creationflags=no_window_flags()).pid
    return result_file, pid


def run_detach_command(args, argv: list[str]) -> int:
    """`argv` is the cli's original argument list (after the program name)."""
    rest = [a for a in argv if a != "--detach"]
    kind = args.command
    branch = getattr(args, "branch", None) if kind == "merge" else None
    result_file, pid = launch([sys.executable, "-m", "tools.agentops", *rest], kind, branch)
    print(f"detached: result {result_file} pid {pid}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="detach")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--result", type=Path, required=True)
    run.add_argument("--kind", default="merge")
    run.add_argument("--branch", default="")
    run.add_argument("--log", type=Path, default=None)  # WMI starts the child without our env
    run.add_argument("--gh-stub", default=None)  # tests only: WMI drops our env too
    run.add_argument("command", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    cmd = a.command[1:] if a.command and a.command[0] == "--" else a.command
    if not cmd:
        ap.error("no command after --")
    run_and_record(cmd, a.result, a.kind, a.branch or None, merge_log=a.log,
                   gh=_default_gh(a.gh_stub) if a.gh_stub else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
