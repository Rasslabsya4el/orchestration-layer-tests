"""`python -m tools.agentops hook-timing [--settings FILE] [--runs N]`: how long a tool call waits on its hooks (#461).

Claude Code starts every hook whose matcher takes a tool call at once, each as a shell command line with
``$CLAUDE_PROJECT_DIR`` set and a JSON payload on stdin, and the call waits for the slowest. This command reads the
hooks the settings file registers (``PreToolUse``, ``PostToolUse``, ``PermissionRequest``), starts the hooks of one
tool kind and event together (a group) with a payload made for that kind, ``--runs`` times (one unmeasured warm-up
round first), and prints per hook and per group the median and 95th percentile in ms, next to the median of a bare
start of the same command line with the hook's script swapped for an empty program (through the same shell, measured in
the same run). A group over the bound is measured a second time, once, and the second pass counts (the output says so).
A hook that exits with a code other than 0 (allow) or 2 (deny), writes a traceback, or runs past ``HOOK_TIMEOUT_S`` fails
the check, named; a timed-out hook is killed with its whole process tree. Exit 1 when a group's median is over 3 x the bare start or its 95th percentile over 5 x, naming the
slowest hook of the group; exit 0 otherwise. The bound is relative, so it holds on any machine.

The hooks come from the settings file given (default: this repo's own ``.claude/settings.json``), so a hook added later
and a target repo's own hooks are covered when the command runs there. The project directory is the folder above the
settings file's folder. Hooks run for real, so the known log and marker paths of the core's hooks are pointed into a
scratch folder; a session id ``hook-timing`` marks anything else a hook writes.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from tools.agentops.util import no_window_flags

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS = REPO_ROOT / ".claude" / "settings.json"
EVENTS = ("PreToolUse", "PostToolUse", "PermissionRequest")
TOOL_KINDS = ("Bash", "Edit", "Read")
DEFAULT_RUNS = 7
MEDIAN_BOUND = 3.0
P95_BOUND = 5.0
HOOK_TIMEOUT_S = 30
#: how long to wait for a killed process tree to be reaped before giving up on it.
KILL_WAIT_S = 5
#: exit codes a hook may end with: 0 allows, 2 denies (both are decisions); any other code is a crash.
HOOK_EXIT_CODES = (0, 2)
#: the env vars the core's hooks take their log and marker paths from; pointed into the scratch folder.
_REDIRECTED = ("AGENTOPS_HOOK_LOG", "TRACKER_GUARD_LOG", "PERMISSION_GUARD_LOG", "AGENTOPS_SUBAGENT_SKILL_CHECK_LOG",
               "AGENTOPS_STATE_CLAIM_LOG", "AGENTOPS_SESSION_PAYLOAD_LOG", "AGENTOPS_BRAIN_INJECT_DIR")
_CONTROL_WORDS = ("||", "&&", ";", "|", "&")
#: the POSIX shells: as a hook's program they get `-c :` for a bare start.
_SHELLS = ("sh", "bash", "dash", "zsh")
#: words that start a simple command which is not the hook's program (`cd x && python hook.py`).
_NOT_PROGRAMS = ("cd", "exit", "export", "set", "unset", "echo", "true", "false", "test", "[")


class HookFault(Exception):
    """A hook crashed or hung: the check fails and names it (exit 1)."""


class HookTimingError(Exception):
    """The check could not measure (no shell, a bare start that fails): exit 2."""


@dataclass(frozen=True)
class Hook:
    event: str
    matcher: str
    command: str

    def takes(self, tool: str) -> bool:
        """Claude Code's matcher: empty, ``*`` or missing takes every tool; anything else is a regex over the tool name."""
        if self.matcher in ("", "*"):
            return True
        try:
            return re.fullmatch(self.matcher, tool) is not None
        except re.error:
            return self.matcher == tool

    @property
    def words(self) -> list[str]:
        try:
            return shlex.split(self.command)
        except ValueError:
            return self.command.split()

    @property
    def script(self) -> str:
        """File name of the script the command line runs (its first word that names a file with an extension)."""
        for word in self.words[1:]:
            name = word.replace("\\", "/").rsplit("/", 1)[-1]
            if "." in name and not word.startswith("-"):
                return name
        return self.command

    @property
    def label(self) -> str:
        """The script plus its own arguments (two hooks run the same file with different words)."""
        words, found = self.words, False
        out: list[str] = []
        for word in words[1:]:
            if word in _CONTROL_WORDS:
                break
            if found:
                out.append(word)
            elif word.replace("\\", "/").rsplit("/", 1)[-1] == self.script:
                found = True
        return " ".join([self.script, *out])


def read_hooks(settings: Path) -> list[Hook]:
    """Every command hook of ``PreToolUse``, ``PostToolUse`` and ``PermissionRequest``, in the file's order."""
    data = json.loads(Path(settings).read_text(encoding="utf-8"))
    hooks: list[Hook] = []
    for event in EVENTS:
        for entry in (data.get("hooks") or {}).get(event) or []:
            for hook in entry.get("hooks") or []:
                if hook.get("type", "command") == "command" and hook.get("command"):
                    hooks.append(Hook(event, entry.get("matcher") or "", hook["command"]))
    return hooks


def _shell() -> str:
    """The shell Claude Code runs a hook command line with: Git Bash on Windows (never the WSL ``bash.exe`` that
    System32 puts first), ``bash`` or ``sh`` elsewhere."""
    candidates: list[str] = []
    if os.environ.get("CLAUDE_CODE_GIT_BASH_PATH"):
        candidates.append(os.environ["CLAUDE_CODE_GIT_BASH_PATH"])
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            for folder in list(Path(git).resolve().parents)[:3]:  # Git\cmd\git.exe or Git\mingw64\bin\git.exe
                candidates.append(str(folder / "bin" / "bash.exe"))
    else:
        candidates.append(shutil.which("bash") or shutil.which("sh") or "")
    for path in candidates:
        if path and Path(path).is_file():
            return path
    raise RuntimeError("hook-timing: no bash found (set CLAUDE_CODE_GIT_BASH_PATH to Git Bash's bash.exe)")


def _payload(kind: str, event: str, project: Path, transcript: Path) -> dict:
    """The JSON a hook gets on stdin for one tool kind: a long Bash command, an Edit with a large new string, a Read
    of a small file. All benign, so a guard takes its whole decision path instead of an early refusal."""
    prose = "The value of line {i} is {i}3 and the total stays the same after the change; see docs/notes.md.\n"
    inputs = {
        "Bash": {"command": "git status --short && " + "; ".join(f"echo step {i} of the long command" for i in range(150)),
                 "description": "Show the working tree status"},
        "Edit": {"file_path": str(project / "docs" / "hook_timing_probe.md"), "old_string": "old line\n",
                 "new_string": "".join(prose.format(i=i) for i in range(220))},
        "Read": {"file_path": str(project / ".claude" / "settings.json")},
    }[kind]
    payload = {"session_id": "hook-timing", "transcript_path": str(transcript), "cwd": str(project),
               "permission_mode": "bypassPermissions", "hook_event_name": event, "tool_name": kind,
               "tool_input": inputs}
    if event == "PostToolUse":
        payload["tool_response"] = {"stdout": "ok\n" * 100, "stderr": "", "interrupted": False}
        payload["tool_use_id"] = "toolu_hook_timing"
    return payload


def _write_transcript(path: Path) -> None:
    rows = []
    for i in range(40):
        role = "user" if i % 2 == 0 else "assistant"
        rows.append(json.dumps({"type": role, "message": {"role": role, "content": f"message {i} " + "text " * 60}}))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _kill_tree(pid: int) -> None:
    """End ``pid`` and everything it started; a pid already gone is fine."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15,
                           creationflags=no_window_flags())
        else:
            os.killpg(pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass


def _run_command(shell: str, command: str, payload: str | None, env: dict, project: Path, what: str,
                 hook: bool = True) -> float:
    """Wall ms of ``shell -c command`` fed ``payload`` on stdin.

    No pipes: stdin, stdout and stderr are temp files, so a grandchild that outlives the hook holds nothing a read
    could wait on. Past ``HOOK_TIMEOUT_S`` the whole process tree is killed (the reap is bounded by ``KILL_WAIT_S``)
    and the wait is an error naming ``what``, never a measurement. A hook (``hook=True``) that exits with a code
    outside ``HOOK_EXIT_CODES`` or writes a traceback is a crash; the bare start must exit 0."""
    error = HookFault if hook else HookTimingError
    group = {"start_new_session": True} if os.name != "nt" else {}
    with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        stdin.write((payload or "").encode("utf-8"))
        stdin.seek(0)
        start = time.perf_counter()
        proc = subprocess.Popen([shell, "-c", command], stdin=stdin, stdout=out, stderr=err, env=env,
                                cwd=str(project), creationflags=no_window_flags(), **group)
        try:
            code = proc.wait(timeout=HOOK_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _kill_tree(proc.pid)
            try:
                proc.wait(timeout=KILL_WAIT_S)
            except subprocess.TimeoutExpired:
                pass
            raise error(f"{what} did not finish in {HOOK_TIMEOUT_S} s; it was killed with its process tree") from None
        ms = (time.perf_counter() - start) * 1000.0
        err.seek(0)
        stderr = err.read().decode("utf-8", errors="replace")
    crashed = (code not in HOOK_EXIT_CODES or "Traceback (most recent call last)" in stderr) if hook else code != 0
    if crashed:
        tail = " | ".join(line.strip() for line in stderr.strip().splitlines()[-3:])[:300]
        raise error(f"{what} crashed: exit code {code}" + (f", stderr: {tail}" if tail else ""))
    return ms


#: one word of a command line: quoted parts, backslash pairs and plain characters glued together (`"$DIR"/hooks/x` is
#: one word), a redirect (`>&2`) or a control operator.
_WORD = re.compile(r"""\d*[<>]{1,2}&\d*|\|\||&&|[;|&]|(?:"(?:\\.|[^"\\])*"|'[^']*'|\\.?|[^\s"'|&;\\])+""", re.S)


def _tokens(command: str) -> list[str]:
    """The command line's words as the shell reads them, quotes and backslashes kept as written."""
    return _WORD.findall(command)


def _is_shell(word: str) -> bool:
    """Whether ``word`` (a program as written, maybe quoted, a path, with ``.exe``) is a POSIX shell."""
    name = word.strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name.removesuffix(".exe") in _SHELLS


def bare_command(command: str, empty_dir: Path) -> str:
    """The hook command line with its script swapped for an empty program: the same shell, the same first word (an
    env-var path, a Windows path, behind a ``cd x &&``) and the same tail (``|| exit 2``), so the time is the bare
    start of exactly what the hook runs. After the program, the first word that is ``-m`` or names a file (has an
    extension) decides:

    * ``-m`` (a wrapper script that picks an interpreter, ``sh hooks/orch-it -m pkg args``, or ``python -m pkg
      args``): everything from ``-m`` on becomes ``-c pass``, so the wrapper starts its interpreter on an empty
      program, the cost the hook always pays;
    * a file: swapped for an empty file of the same extension in ``empty_dir``, its own arguments dropped;
    * neither: a shell program gets ``-c :``, any other program ``-c pass``."""
    tokens = _tokens(command)
    start = 0
    while start <= len(tokens):
        end = start
        while end < len(tokens) and tokens[end] not in _CONTROL_WORDS:
            end += 1
        first = start
        while first < end and re.fullmatch(r"\w+=\S*", tokens[first]):
            first += 1
        if first < end and tokens[first] not in _NOT_PROGRAMS:
            for j in range(first + 1, end):
                if tokens[j] == "-m":
                    return " ".join(tokens[:j] + ["-c", "pass"] + tokens[end:])
                word = tokens[j].strip("\"'")
                suffix = re.search(r"\.\w+$", word)
                if suffix and not word.startswith("-"):
                    empty = (empty_dir / f"empty{suffix.group(0)}")
                    empty.touch()
                    return " ".join(tokens[:j] + [shlex.quote(empty.as_posix())] + tokens[end:])
            program_code = ":" if _is_shell(tokens[first]) else "pass"
            return " ".join(tokens[:first + 1] + ["-c", program_code] + tokens[end:])
        start = end + 1
    raise HookTimingError(f"no program found in the hook command line: {command}")


def median(values: list[float]) -> float:
    return statistics.median(values)


def p95(values: list[float]) -> float:
    """Nearest-rank 95th percentile (with 7 runs: the slowest one)."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


@dataclass
class GroupResult:
    kind: str
    event: str
    hooks: list[Hook]
    hook_times: list[list[float]]
    group_times: list[float]
    bare_median: float

    @property
    def name(self) -> str:
        return f"{self.kind} {self.event}"

    def slowest(self) -> tuple[Hook, float]:
        index = max(range(len(self.hooks)), key=lambda i: median(self.hook_times[i]))
        return self.hooks[index], median(self.hook_times[index])

    def violations(self) -> list[str]:
        out = []
        if median(self.group_times) > MEDIAN_BOUND * self.bare_median:
            out.append(f"median {median(self.group_times):.0f} ms > {MEDIAN_BOUND:g} x bare {self.bare_median:.0f} ms")
        if p95(self.group_times) > P95_BOUND * self.bare_median:
            out.append(f"p95 {p95(self.group_times):.0f} ms > {P95_BOUND:g} x bare {self.bare_median:.0f} ms")
        return out


def measure(settings: Path, runs: int = DEFAULT_RUNS,
            only: set[tuple[str, str]] | None = None) -> list[GroupResult]:
    """Time every group of the settings' hooks (one per tool kind and event that has a hook; ``only`` = a set of
    (kind, event) to time just those), ``runs`` times each. Raises ``HookFault`` on a crashed or hung hook."""
    settings = Path(settings).resolve()
    project = settings.parent.parent
    hooks = read_hooks(settings)
    groups = [(kind, event, [h for h in hooks if h.event == event and h.takes(kind)])
              for kind in TOOL_KINDS for event in EVENTS]
    groups = [g for g in groups if g[2] and (only is None or (g[0], g[1]) in only)]
    shell = _shell()
    with tempfile.TemporaryDirectory(prefix="hook-timing-") as scratch_name:
        scratch = Path(scratch_name)
        transcript = scratch / "transcript.jsonl"
        _write_transcript(transcript)
        env = dict(os.environ, CLAUDE_PROJECT_DIR=str(project))
        for name in _REDIRECTED:
            env[name] = str(scratch / name.lower())
        bare_of = {h.command: bare_command(h.command, scratch) for _, _, hs in groups for h in hs}
        hook_times = {(k, e): [[] for _ in hs] for k, e, hs in groups}
        group_times = {(k, e): [] for k, e, _ in groups}
        bare_times: dict[str, list[float]] = {command: [] for command in set(bare_of.values())}
        for round_no in range(runs + 1):  # round 0 is the unmeasured warm-up (file cache, first imports)
            for command in bare_times:
                ms = _run_command(shell, command, None, env, project, f"bare start `{command}`", hook=False)
                if round_no:
                    bare_times[command].append(ms)
            for kind, event, hs in groups:
                payload = json.dumps(_payload(kind, event, project, transcript))
                start = time.perf_counter()
                with ThreadPoolExecutor(max_workers=len(hs)) as pool:
                    futures = [pool.submit(_run_command, shell, h.command, payload, env, project,
                                           f"{kind} {event} hook {h.label}") for h in hs]
                    times = [f.result() for f in futures]
                total = (time.perf_counter() - start) * 1000.0
                if round_no:
                    group_times[(kind, event)].append(total)
                    for i, ms in enumerate(times):
                        hook_times[(kind, event)][i].append(ms)
    bare = {command: median(values) for command, values in bare_times.items()}
    return [GroupResult(kind, event, hs, hook_times[(kind, event)], group_times[(kind, event)],
                        max(bare[bare_of[h.command]] for h in hs)) for kind, event, hs in groups]


def report(results: list[GroupResult], settings: Path, runs: int,
           remeasured: list[str] | None = None) -> tuple[list[str], int]:
    lines = [f"hook-timing: {settings}, {runs} runs per group (+1 unmeasured warm-up), ms; hooks of a group start at once"]
    if remeasured:
        lines.append(f"re-measured once: {', '.join(remeasured)} (over the bound in the first pass); "
                     "the figures of those groups are the second pass")
    failures: list[str] = []
    for r in results:
        lines.append(f"{r.name}: group median {median(r.group_times):.0f}, p95 {p95(r.group_times):.0f}; "
                     f"bare start median {r.bare_median:.0f} (limits {MEDIAN_BOUND:g} x = {MEDIAN_BOUND * r.bare_median:.0f}, "
                     f"p95 {P95_BOUND:g} x = {P95_BOUND * r.bare_median:.0f})")
        for hook, times in zip(r.hooks, r.hook_times):
            lines.append(f"  {hook.label}: median {median(times):.0f}, p95 {p95(times):.0f}")
        reasons = r.violations()
        if reasons:
            slowest, slow_median = r.slowest()
            failures.append(f"FAIL {r.name}: {'; '.join(reasons)}; slowest hook {slowest.label} "
                            f"(median {slow_median:.0f} ms)")
    lines.extend(failures)
    lines.append(f"hook-timing: {len(failures)} of {len(results)} groups over the bound" if failures
                 else f"hook-timing: all {len(results)} groups within {MEDIAN_BOUND:g} x median and {P95_BOUND:g} x p95 of the bare start")
    return lines, 1 if failures else 0


def add_subparser(sub) -> None:
    parser = sub.add_parser("hook-timing", help="time every hook a settings file registers against a bare Python start (#461)")
    _add_arguments(parser)


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS,
                        help="the settings file whose hooks are timed (default: this repo's .claude/settings.json)")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS, help=f"measured runs per group (default {DEFAULT_RUNS})")


def run_hook_timing_command(args) -> int:
    if args.runs < 1:
        print("hook-timing: --runs must be 1 or more", file=sys.stderr)
        return 2
    if not Path(args.settings).is_file():
        print(f"hook-timing: no settings file at {args.settings}", file=sys.stderr)
        return 2
    try:
        results = measure(args.settings, args.runs)
        if not results:
            print(f"hook-timing: {args.settings} registers no hook that takes Bash, Edit or Read", file=sys.stderr)
            return 2
        over = {(r.kind, r.event) for r in results if r.violations()}
        remeasured: list[str] = []
        if over:  # one machine hiccup trips the p95 of a few runs: the groups over the bound get a second pass
            second = {(r.kind, r.event): r for r in measure(args.settings, args.runs, only=over)}
            results = [second.get((r.kind, r.event), r) for r in results]
            remeasured = [f"{kind} {event}" for kind, event in sorted(over)]
    except HookFault as fault:
        print(f"FAIL {fault}")
        return 1
    except HookTimingError as error:
        print(f"hook-timing: {error}", file=sys.stderr)
        return 2
    lines, code = report(results, args.settings, args.runs, remeasured)
    print("\n".join(lines))
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hook-timing", description=__doc__.splitlines()[0])
    _add_arguments(parser)
    return run_hook_timing_command(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
