"""One merge at a time, across every live session sharing one checkout.

    python -m tools.agentops merge <branch> [--suite "<cmd>"] [--cwd PATH]
        [--lock PATH] [--timeout SECONDS]

More than one session may want to merge into local ``main`` at once (several mergers
sharing one checkout, and other sessions with uncommitted changes of their own in it).
The whole sequence -- sync with ``origin/main``, merge, the suite, push -- is one
critical section, held by a file lock so no two sessions run it at the same time. A
lock whose holder process is gone is taken over at once (0 s wait). A failed step (a
conflict, a red suite) leaves local ``main`` at the head it had before the attempt and
pushes nothing, rolling back with ``git reset --keep`` -- never ``--hard`` -- so an
uncommitted change of another session that the merge never touched survives; the
pre-check that a merge would not touch an already-dirty file makes ``--keep`` safe to
use. If ``--keep`` itself refuses, nothing further is attempted: the failure names the
files. A push refused because ``origin/main`` moved is answered by rolling the failed
attempt back the same way, fetching, fast-forwarding onto the new ``origin/main``, and
re-merging the branch (never ``git rebase``, which chokes on any unstaged change and
flattens a merge commit); a conflict in that re-merge says the later branch must be
rebased by whoever owns it. Never a forced push.

The suite command is never hard-coded here: it comes from the caller (``--suite``, or
an installation's own settings key), so this module carries no line of the repo it
runs in. Stdlib only, like every module the core ships with.

    python -m tools.agentops commit -m <message> <path>...

The same lock, taken the same way, for a plain commit in the main checkout (e.g. a State
file rewrite) that is not a merge: it first brings the checkout up to ``origin/main`` (fetch,
fast-forward only -- the same helper ``merge`` uses), then commits only the named paths, pushes
to ``main``, and releases the lock in every case. A checkout that cannot be fast-forwarded (its
own unpushed commit, or a fast-forward that would overwrite a changed file of the working tree)
is refused before anything is added or committed: exit code 1, one line naming the state, the
named paths' changes left in the working tree. Once committed, a push refused because
``origin/main`` moved is answered by fetching, rebasing the one new commit onto the remote's
head, and pushing again, up to 3 times in total; after the third refusal it is reported (exit
code 1, one line saying how many times it tried) with the local commit kept -- no rollback; the
caller decides what to do next.
"""

from __future__ import annotations

import argparse
import itertools
import hashlib
import importlib
import json
import codecs
import io
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, redirect_stdout
from dataclasses import asdict, dataclass, field, fields, replace as _dc_replace
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from .core_format import human_duration
from .core_guard import manifest_edits, refusal_message
from .line_check import LineCheck
from .pytest_runs_plugin import with_no_runlog
from .pc_only import load_pc_only
from . import merge_failfast, merge_machine, merge_priority, monitor_install
from . import merge_load
from . import suite_events as _suite_events
from . import suite_probe, suite_reservation
from .machine_mode import MachineModeUnreadable, current_mode, read_gate, stood_between
from .suite_plan import plan_path
from .suite_worktree import SuiteWorktreeError, ensure_suite_worktree
from .gh_call import run_gh
from .util import holder_is_live, nested_env, no_window_flags, pid_alive, now, parse_started as _shared_parse_started
from .worktree import SHARED, main_checkout_root

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOCK_PATH = ROOT / "var" / "locks" / "merge.lock"
DEFAULT_REMOTE = "origin"
DEFAULT_MAIN = "main"
DEFAULT_MAX_AGE_MINUTES = 30  # settings key `merge_lock_max_age_min`
#: settings key `merge_lock_silent_takeover_min` (#62): a live holder whose suite output stood still this
#: long and whose process tree burns no CPU loses the merge line to the next waiter; 0 = never. 30 min is the
#: silence the Zorblax holder showed on 2026-10-16 (its load gate waiting for the machine), the same minutes
#: `merge_lock_max_age_min` already made every waiter give up after.
DEFAULT_SILENT_TAKEOVER_MINUTES = 30
SILENT_HOLDER_MARKER_SUFFIX = ".silent-holder"  # next to the holder's result file; `detach` reads it
BURN_WINDOW_SECONDS = 2.0  # the CPU window `_tree_is_burning` samples
REPORT_INTERVAL_SECONDS = 60.0  # one waiting line per minute
_TICKET_SEQ = itertools.count()
#: a waiter's per-turn hook in the lock's wait loop: ``(ticket name, holder is live, a holder claimed me)``
#: -> ``None`` or a result dict (the waiter leaves the line with it)
WaiterHook = Callable[[str, bool, bool], "dict | None"]
UNPARSABLE_GRACE_SECONDS = 10.0  # a holder file not yet readable is waited on this long
DEFAULT_MAX_REBASE_ATTEMPTS = 5
PUSH_ATTEMPTS = 3  # #248: pushes of the merged commit (the first and the retries) while origin/main stays unmoved
PUSH_RETRY_WAIT_S = 30  # ... this many seconds apart (tests set 0)
DEFAULT_MAX_COMMIT_PUSH_ATTEMPTS = 3  # fetch + rebase-onto-remote + retry, at most this many pushes


def _unlink_retrying(path: Path, attempts: int = 20, pause: float = 0.05) -> None:
    """Unlink ``path``; on Windows a file another thread or process has open at that moment answers
    ``PermissionError`` (the same pending-delete state `set_progress` retries on). A ticket left
    behind by one such answer would, for its live pid, keep every younger waiter from being head
    for good, so the unlink is tried again for up to ``attempts * pause`` (1 s). Any other
    ``OSError`` (a missing file) ends it at once."""
    for attempt in range(attempts):
        try:
            path.unlink()
            return
        except PermissionError:
            if attempt == attempts - 1:
                return
            time.sleep(pause)
        except OSError:
            return

# -- the suite's own output ---------------------------------------------------------------------

SUITE_OUTPUT_DIR = ROOT / "var" / "agentops" / "merge_suite"
MERGE_LOG_PATH = ROOT / "var" / "agentops" / "merge.log"
#: env overrides, read at call time (a detached runner passes its own paths this way)
ENV_MERGE_LOG = "AGENTOPS_MERGE_LOG"
#: the detach wrapper's result file, so a ticket can name it
ENV_MERGE_RESULT = "AGENTOPS_MERGE_RESULT"
#: exit code of a merge that was not queued because the same branch is already in the line
EXIT_ALREADY_QUEUED = 5
ENV_SUITE_OUTPUT_DIR = "AGENTOPS_MERGE_SUITE_OUTPUT_DIR"
MAX_SUITE_OUTPUTS = 20  # the newest N output files are kept; older ones pruned at merge start
MAX_FAILED_IDS_SHOWN = 5

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_PYTEST_RE = re.compile(r"(?<!\w)pytest(?!\w)")
_R_JOINED_RE = re.compile(r"(?<!\S)-r([A-Za-z]+)(?!\S)")
_R_SPACED_RE = re.compile(r"(?<!\S)-r\s+([A-Za-z]+)(?!\S)")
_FAILED_ID_RE = re.compile(r"^FAILED (.+?)(?: - .*)?$", re.MULTILINE)  # the id whole: up to pytest's " - message" or the line end
_NOT_FOUND_RE = re.compile(r"^ERROR: (?:file or directory )?not found: (.+)$", re.MULTILINE)  # the whole id: it may hold spaces
_SUMMARY_FAILED_RE = re.compile(r"(\d+) failed")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """A caller's own ``FORCE_COLOR``/``PY_COLORS`` env var reaches the suite subprocess (it
    inherits this process' environment); strip the escape codes before parsing pytest's own
    short summary so ``FAILED <id>`` and ``<n> failed`` still match plainly."""
    return _ANSI_RE.sub("", text)

_suite_output_seq = itertools.count()


def _safe_branch_name(branch: str) -> str:
    return _SAFE_NAME_RE.sub("_", branch).strip("_") or "branch"


def _suite_output_dir() -> Path:
    env = os.environ.get(ENV_SUITE_OUTPUT_DIR)
    return Path(env) if env else SUITE_OUTPUT_DIR


def _merge_log_path() -> Path:
    env = os.environ.get(ENV_MERGE_LOG)
    return Path(env) if env else MERGE_LOG_PATH


def _new_suite_output_path(branch: str, output_dir: Path | None = None) -> Path:
    """A fresh path under ``output_dir``; the sequence number (not just the timestamp)
    keeps names sorting in creation order even when several land in the same second.

    ``output_dir`` is looked up from ``SUITE_OUTPUT_DIR`` at call time, not bound as a
    def-time default, so a test that monkeypatches the module constant is honoured."""
    if output_dir is None:
        output_dir = _suite_output_dir()
    ts = time.strftime("%Y%m%dT%H%M%S")
    seq = next(_suite_output_seq)
    return output_dir / f"{ts}-{seq:06d}-{_safe_branch_name(branch)}.out"


def _prune_suite_outputs(keep: int, output_dir: Path | None = None) -> None:
    if output_dir is None:
        output_dir = _suite_output_dir()
    if not output_dir.exists():
        return
    files = sorted(output_dir.glob("*.out"))
    excess = len(files) - keep
    for stale in files[:max(0, excess)]:
        for gone in (stale, stale.with_name(stale.name + ".args")):
            try:
                gone.unlink()
            except OSError:
                pass


def _is_pytest_command(cmd: str) -> bool:
    return bool(_PYTEST_RE.search(cmd))


def _augmented_suite_cmd(cmd: str) -> str:
    """``-rf`` (pytest's own short summary of failed tests) added when the suite is a pytest
    command and does not already ask for it. pytest keeps only the last ``-r``, so an existing
    ``-r<letters>`` / ``-r <letters>`` gets ``f`` merged into its letters instead:
    ``-rs`` -> ``-rsf``, ``-r s`` -> ``-r sf``; letters already holding ``f``, ``a`` or ``A``
    (both summaries include failures) stay as they are."""
    if not _is_pytest_command(cmd):
        return cmd
    matches = [*_R_JOINED_RE.finditer(cmd), *_R_SPACED_RE.finditer(cmd)]
    if not matches:
        return f"{cmd} -rf"
    last = max(matches, key=lambda m: m.start())
    if any(c in last.group(1) for c in "faA"):
        return cmd
    return cmd[:last.end(1)] + "f" + cmd[last.end(1):]


# -- pytest-xdist workers from the machine budget ------------------------------------------

PROBE_TIMEOUT_SECONDS = 30
_CHUNK_FLAG = "--maxschedchunk=1"  # a worker takes one test at a time
_CHUNK_FLAG_RE = re.compile(r"\s--maxschedchunk(?:=|\s+)\d+\b")
_N_FLAG_RE = re.compile(r"(?:^|\s)(?:-n\s*\S|--numprocesses\b)")


_WORKERS_LINE_RE = re.compile(r"^workers (\d+):")


def suite_workers(probe) -> tuple[int, str]:
    """The worker count the probe states: ``probe() -> line`` whose text starts ``workers N:``
    (the count is computed by the probe from measured free cores and free RAM). A probe that
    raises or prints no such line -> one worker (the suite never blocks on the budget).
    Returns ``(k, line)``."""
    try:
        line = probe()
        match = _WORKERS_LINE_RE.match(line)
        if match is None:
            return 1, f"budget probe printed no worker count: {_first_line(line)}"
        return max(1, int(match.group(1))), line
    except Exception as exc:  # noqa: BLE001 -- any probe failure means "run with one worker"
        return 1, f"budget probe failed: {_first_line(str(exc)) or type(exc).__name__}"


def resolve_suite_workers_probe() -> str | None:
    """The settings file's ``merge_suite_workers_probe`` (a command that prints ``workers N: ...``).
    Key absent or settings unreadable -> the core's own probe (``suite_probe.CORE_PROBE_COMMAND``, #230);
    a non-empty string -> that string (the repo's own probe wins); an empty value or a non-string -> ``None``
    (explicitly off)."""
    try:
        from .core_settings import load as load_settings
        settings = load_settings()
    except Exception:
        return suite_probe.CORE_PROBE_COMMAND
    if "merge_suite_workers_probe" not in settings:
        return suite_probe.CORE_PROBE_COMMAND
    template = settings["merge_suite_workers_probe"]
    return template if isinstance(template, str) and template.strip() else None


_CAP_RE = re.compile(r"(?:^|[ ,])cap (\d+)\s*$")  # the probe line ends with the budget's cap
_RESERVE_OFF = threading.local()


@contextmanager
def no_suite_reservation():
    """Probes made inside this block (this thread) read the headroom but claim no shared reservation:
    the line check's suite and the restart watcher."""
    previous = getattr(_RESERVE_OFF, "on", False)
    _RESERVE_OFF.on = True
    try:
        yield
    finally:
        _RESERVE_OFF.on = previous


def _reservation_owner(cwd: Path | str) -> str:
    return f"{os.getpid()}:{cwd}"


def release_suite_workers(cwd: Path | str) -> None:
    """The suite that ran in ``cwd`` ended: its claim on the shared suite-worker reservation is dropped."""
    try:
        suite_reservation.release(_reservation_owner(cwd))
    except Exception:  # noqa: BLE001 -- a reservation that cannot be dropped ends by its lease
        pass


def _reserve_in_line(line: str, cwd: Path | str) -> str:
    """the probe's ``workers N: ... cap C`` line, with N cut to what the shared reservation grants
    (the merge suite and the full run read the same file, so together they never exceed C); a caller
    with no headroom waits for it, bounded. A line without a count or a cap, or a reservation file
    that fails, leaves the line as the probe stated it."""
    counted, capped = _WORKERS_LINE_RE.match(line), _CAP_RE.search(line)
    if counted is None or capped is None:
        return line
    wanted, cap = int(counted.group(1)), int(capped.group(1))
    try:
        granted, held, waited = suite_reservation.reserve_waiting(wanted, cap, _reservation_owner(cwd))
    except Exception:  # noqa: BLE001 -- the suite never blocks on its own bookkeeping
        return line
    if granted == wanted and waited < 1:
        return line
    note = f"shared reservation: cap {cap}, {held} held by another suite, wanted {wanted}"
    if waited >= 1:
        note += f", waited {human_duration(waited)}"
    return f"workers {granted}: {note}; {line.split(': ', 1)[1]}"


def _make_probe(template: str, cwd: Path):
    """The real probe: the command run with the shell in ``cwd``; the answer is its last stdout line
    (non-zero return code = failure). Bounded by ``PROBE_TIMEOUT_SECONDS``. Its worker count is claimed
    from the shared reservation unless made inside ``no_suite_reservation()``."""
    reserve = not getattr(_RESERVE_OFF, "on", False)

    def probe() -> str:
        line = raw()
        return _reserve_in_line(line, cwd) if reserve else line

    def raw() -> str:
        result = subprocess.run(template, shell=True, cwd=str(cwd), capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=PROBE_TIMEOUT_SECONDS, creationflags=no_window_flags())
        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        if result.returncode != 0:
            raise RuntimeError(f"probe exit {result.returncode}")
        return lines[-1].strip() if lines else ""
    return probe


def _xdist_available(interpreter: str, cwd: Path) -> bool:
    try:
        result = subprocess.run([interpreter, "-c", "import xdist"], cwd=str(cwd), capture_output=True,
                                timeout=PROBE_TIMEOUT_SECONDS, creationflags=no_window_flags())
    except Exception:
        return False
    return result.returncode == 0


_FLOOR_CUT_RE = re.compile(r"free RAM [\d.]+ GB under the floor [\d.]+ GB")  # the probe line's cut words


class _SuiteWorkers(int):
    """A suite's pytest worker count. ``cut`` holds the words of a cut to one worker under the run
    queue's RAM floor, so the merge log line can name them; it rides on the count so that every caller
    that already passes the count through (run, result, restart) carries it unchanged."""

    cut: str = ""

    def __new__(cls, value: int, cut: str = ""):
        obj = super().__new__(cls, value)
        obj.cut = cut
        return obj


def _with_workers(run_cmd: str, is_pytest: bool, suite_dir: Path) -> tuple[str, int, str]:
    """``(command, workers, head line)``: for a pytest suite, ``-n <k>`` appended when the budget
    grants k > 1 and the suite's interpreter has xdist. With the settings key explicitly off (empty): today's
    command (workers 0 = not decided, no head line); with no key, the core probe (#230)."""
    if not is_pytest:
        return run_cmd, 0, ""
    template = resolve_suite_workers_probe()
    if template is None or _N_FLAG_RE.search(run_cmd):
        return run_cmd, 0, ""
    k, line = suite_workers(_make_probe(template, suite_dir))
    if k > 1:
        interpreter = run_cmd.split()[0]
        if not _xdist_available(interpreter, suite_dir):
            return run_cmd, 1, "workers 1: pytest-xdist missing"
    cut = _FLOOR_CUT_RE.search(line)
    count = _SuiteWorkers(k, cut.group(0) if cut and k == 1 else "")
    cmd = f"{run_cmd} -n {k} {_CHUNK_FLAG}" if k > 1 else run_cmd
    return cmd, count, line if _WORKERS_LINE_RE.match(line) else f"workers {k}: {line}"


# -- selected tests --------------------------------------------------------------------

_SELECT_POSITIONAL_RE = re.compile(r'((?<!\w)pytest\s+)(?:"tests"|tests)(?=\s|$)')


def resolve_select_tests() -> bool:
    """The settings file's ``merge_select_tests``: true only when the key is the JSON ``true``
    (absent, unreadable file or anything else = off)."""
    try:
        from .core_settings import load as load_settings
        return load_settings().get("merge_select_tests") is True
    except Exception:
        return False


def _selected_cmd(run_cmd: str, ids: list[str], args_path: Path) -> str | None:
    """``run_cmd`` with its positional ``tests`` replaced by ``@<args_path>`` (pytest reads one
    argument per line from the file; the ``ids`` are written there). ``None`` when the command's
    shape is not ``... pytest tests ...``."""
    if _SELECT_POSITIONAL_RE.search(run_cmd) is None:
        return None
    args_path.parent.mkdir(parents=True, exist_ok=True)
    args_path.write_text("\n".join(ids) + "\n", encoding="utf-8")
    return _SELECT_POSITIONAL_RE.sub(lambda m: f'{m.group(1)}"@{args_path}"', run_cmd, count=1)


def _plan_selection(cwd: Path, base: str, sha: str, run_cmd: str, output_path: Path) -> tuple[str, str, bool]:
    """``(command, suite out line 1, skip)`` for a pytest merge suite. Any doubt or failure is the
    full suite with its reason as line 1; ``skip`` = the selection is empty, run no pytest."""
    from .test_select import map_dir, select_for_diff
    try:
        sel = select_for_diff(cwd, base, sha, map_dir(cwd))
    except Exception as exc:  # noqa: BLE001 -- the selection never breaks a merge
        return run_cmd, f"full suite: selection failed: {_first_line(str(exc)) or type(exc).__name__}", False
    if sel.full:
        return run_cmd, sel.line, False
    if not sel.ids:
        return run_cmd, sel.line, True
    cmd = _selected_cmd(run_cmd, sel.ids, output_path.with_name(output_path.name + ".args"))
    if cmd is None:
        return run_cmd, "full suite: suite command shape not recognised", False
    return cmd, sel.line, False


def _run_heavy_suite(cwd: Path, name: str, tests_line: str, priority: bool, body: Callable[[], "SuiteRun"],
                     output_path: Path, merge_log_path: Path, lock: "MergeLock") -> "SuiteRun":
    """#224: ``body()`` (a merge suite) under a machine berth when it is heavy (the full suite, or over 500 tests),
    freed when it ends. A wait is said in the suite output and the merge log, and keeps the merge line's lock
    alive; once the berth is taken the machine mode is read again (in play the run waits holding the berth)."""
    tests, full = merge_machine.run_size(tests_line, cwd)
    on_wait, sleep = merge_machine.wait_hooks(
        output_path, log=lambda text: _append_member_log(name, text, merge_log_path), alive=lock.heartbeat)
    return merge_machine.run_heavy(cwd, name, tests, full, priority, body, on_wait=on_wait, sleep=sleep,
                                   hold=lambda: wait_while_held(lock.heartbeat), out_path=output_path)


def _place_fields(run: "SuiteRun") -> dict:
    """The MergeResult fields (#232) that say where the suite ran."""
    fields_ = {"suite_place": run.place, "suite_machines": run.machines, "suite_part_seconds": list(run.part_seconds),
               "suite_minutes": run.minutes}
    return fields_


def _run_sized_suite(run_cmd: str, tests_line: str, suite_cmd: str, is_pytest: bool, suite_dir: Path,
                     run_env: dict[str, str] | None, output_path: Path, root: Path,
                     cut_check: Callable[[], str] | None = None) -> "SuiteRun":
    """The merge suite as the budget sizes it: ``run_cmd`` with its workers, ``tests_line`` over the budget's head line,
    through ``_run_merge_suite``. Only a full suite goes through the optional router."""
    cmd, n_workers, head = _with_workers(run_cmd, is_pytest, suite_dir)
    head = "\n".join(x for x in (tests_line, head) if x)
    return _run_merge_suite(cmd, suite_cmd, n_workers, head, suite_dir, run_env, output_path,
                            routed_ok=not tests_line.startswith(("selected", "re-check")), root=root,
                            **({"cut_check": cut_check} if cut_check is not None else {}))


_ARGS_FILE_RE = re.compile(r'"@([^"]+)"')


def _ids_in_cmd(run_cmd: str) -> list[str] | None:
    """The test ids a selected command reads from its ``"@<file>"`` argument (``_selected_cmd`` wrote them); ``None``
    when the command carries none or the file cannot be read."""
    found = _ARGS_FILE_RE.search(run_cmd)
    if found is None:
        return None
    try:
        return [x.strip() for x in Path(found.group(1)).read_text(encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        return None


def _pc_only_files(suite_dir: Path) -> list[str]:
    """The PC-only files (``tests/pc_only.txt``) that exist in the merged tree ``suite_dir``, sorted (``[]`` = none: no
    Windows job). A list that exists and cannot be read raises."""
    return sorted(f for f in load_pc_only(suite_dir) if (Path(suite_dir) / f).is_file())


def _windows_for(ids: list[str] | None, pc_files: list[str]) -> bool:
    """Whether a GitHub run needs its Windows job: the full suite (``ids`` None) when any PC-only file exists, a run of
    ``ids`` when one of them is in a PC-only file."""
    if ids is None:
        return bool(pc_files)
    named = set(pc_files)
    return any(i.split("::", 1)[0].replace("\\", "/") in named for i in ids)


def _pc_readers(ids: list[str] | None, pc_files: list[str], suite_dir: Path, root: Path) -> list[str]:
    """#260: the ``ids`` in a PC-only file that read a file the public test copy does not take, in ``ids`` order; for a
    full run (``ids`` None) every such test the reads data names in a PC-only file, sorted. The plan's reads data
    (``test_copy_plan``: the map's ``reads/`` under ``root``) decides; an id with no reads data, no reads data at all, or
    any trouble reading it is not named, so it stays on the Windows job as before."""
    named = set(pc_files)

    def in_pc_file(i: str) -> bool:
        return i.split("::", 1)[0].replace("\\", "/") in named
    candidates = [i for i in ids if in_pc_file(i)] if ids is not None else None
    if candidates == [] or (ids is None and not named):
        return []
    try:
        from .test_copy_check import snapshot_files
        from .test_copy_plan import load_reads, pc_only_tests, tracked_files
        from .test_select import map_dir
        reads_dir = map_dir(root) / "reads"
        if not reads_dir.is_dir():
            return []
        reads, dir_reads = load_reads(reads_dir)
        missing = pc_only_tests(reads, set(snapshot_files(suite_dir)), dir_reads, tracked_files(suite_dir))
    except Exception:  # noqa: BLE001 -- the split never breaks a merge: no answer = the Windows job as today
        return []
    if candidates is None:  # a full run: the reads data names them
        return sorted(i for i in missing if in_pc_file(i))
    return [i for i in candidates if i in missing]


def _run_ids_on_pc(suite_cmd: str, ids: list[str], suite_dir: Path, run_env: dict[str, str] | None) -> tuple[bool, str]:
    """#260: a plain pytest of ``ids`` in ``suite_dir`` (the ids go through an args file); ``(ok, output)``."""
    fd, name = tempfile.mkstemp(prefix="pc-readers-", suffix=".args")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(ids) + "\n")
    args_file = Path(name)
    try:
        return _run_suite(_recheck_cmd(suite_cmd, ids, args_file), suite_dir, run_env)
    finally:
        args_file.unlink(missing_ok=True)


def _fold_pc_readers(run: "SuiteRun", readers: list[str], pc_ok: bool, pc_output: str) -> None:
    """#260: the PC run of the ``readers`` joined to ``run``, the GitHub verdict. The place says the count; a red PC run
    makes ``run`` red and names its ids next to GitHub's (``red_ids`` / ``still``, or the output's summary when a
    GitHub red is still waiting for the caller's PC re-check)."""
    run.place += f" (+ {len(readers)} {'test' if len(readers) == 1 else 'tests'} on the PC)"
    waiting = _failed_ids(run.output)  # GitHub's own red ids, read before the PC output joins
    run.output += ("\n" if run.output and not run.output.endswith("\n") else "") + pc_output
    if pc_ok:
        return
    failed = _failed_ids(pc_output) or list(readers)
    run.failed_tests = [*run.failed_tests, *[i for i in failed if i not in run.failed_tests]]
    if run.rechecked_done or run.ok:
        run.still = [*run.still, *failed]
        run.red_ids = list(run.still)  # a GitHub red that its re-check recovered is no longer red: only what still fails
        run.rechecked_done, run.ok = True, False
        return
    summary = "\n".join(f"FAILED {i}" for i in dict.fromkeys([*waiting, *failed]))
    run.output += f"\nshort test summary info\n{summary}\n"


def _run_suite_placed(cwd: Path, branch: str, sha: str, suite_dir: Path, is_pytest: bool, tests_line: str,
                      priority: bool, body: Callable[[], "SuiteRun"], output_path: Path, merge_log_path: Path,
                      lock: "MergeLock", issues: Sequence[int] = (), ids: list[str] | None = None,
                      pc_only: Callable[[], list[str]] | None = None,
                      run_readers: Callable[[list[str]], tuple[bool, str]] | None = None) -> "SuiteRun":
    """#232 (D119), #247: a pytest suite goes to GitHub (the full suite in parts, a selected run or a re-check after main
    moved as its test ``ids``) whenever the settings give a plan (``merge_github.plan_for``); the PC takes it only on a
    #232 rule-4 fallback, through ``_run_heavy_suite`` and its berth. The machines asked follow the test count
    (``github_suite.machines_for``). The PC-only test files (``pc_only()``, from ``tests/pc_only.txt``) run on GitHub's
    Windows job of the same run, so the PC runs nothing beside it. A PC route outside a real fallback (settings
    unreadable, a partial run whose ids cannot be read) says its reason in the merge log and the place.
    GitHub green = a green run, no PC berth taken; red = a red run naming the failed ids, which are re-run on GitHub
    as a 1-machine run (``rechecked_done``): a re-check GitHub cannot start is left to the caller's PC re-check. A
    fallback (any reason), or a red GitHub could not name a test for, runs the whole run on the PC and leaves one
    brain line. GitHub never raises here.

    #260: the PC-only tests among ``ids`` that read a file the public copy does not take (``_pc_readers``) leave the
    Windows job and run on the PC through ``run_readers(ids) -> (ok, output)``, after the GitHub run, under a berth only
    when they are over the line's bound; the merge is red when either run is. Only readers selected: no GitHub run. A
    full run (``ids`` None) does the same for every reader the reads data names: the Windows job is told to leave them
    out (``skip``, the workflow's input) and the PC runs them."""
    def on_pc() -> "SuiteRun":
        return _run_heavy_suite(cwd, branch, tests_line, priority, body, output_path, merge_log_path, lock)

    def on_pc_because(why: str) -> "SuiteRun":
        _append_plain_log(branch, f"suite on the PC ({why})", merge_log_path)
        run = on_pc()
        run.place = f"suite on the PC ({why})"
        return run

    if not is_pytest:
        return on_pc()  # no pytest: nothing to send to GitHub
    _tests, full = merge_machine.run_size(tests_line, cwd)
    try:
        from . import core_settings, github_run, merge_github
        settings = core_settings.load()
        plan, note = merge_github.plan_for(settings, cwd)
    except Exception:  # noqa: BLE001 - an unreadable setting is "off", said in the log
        return on_pc_because("github settings unreadable")
    if plan is None:
        run = on_pc()
        run.place = f"suite on the PC ({note})" if note else ""
        return run
    if not full and not ids:
        return on_pc_because("partial run, ids unreadable")
    ids = None if full else list(ids)
    ticket = merge_github.ticket_of(branch, issues)
    pc_files = pc_only() if pc_only is not None else []
    readers = _pc_readers(ids, pc_files, suite_dir, cwd) if run_readers is not None else []
    github_ids = [i for i in ids if i not in set(readers)] if (readers and ids is not None) else ids

    def readers_on_pc() -> tuple[bool, str]:
        part = _run_heavy_suite(cwd, branch, f"selected {len(readers)} of {len(readers)}", priority,
                                lambda: SuiteRun(*run_readers(readers), 0), output_path, merge_log_path, lock)
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "a", encoding="utf-8") as fh:  # the record keeps the PC run beside GitHub's
                fh.write(part.output)
        except OSError:
            pass
        return part.ok, part.output

    if readers and ids is not None and not github_ids:  # nothing is left for GitHub: no GitHub run, no Windows job
        pc_ok, pc_output = readers_on_pc()
        run = SuiteRun(True, "", 0, place="suite on GitHub")
        _fold_pc_readers(run, readers, pc_ok, pc_output)
        run.place += ": no test left for GitHub (every selected test reads a file kept out of the copy)"
        return run

    # #271: a selection whose inputs GitHub would refuse (over its input limit) runs the whole suite on GitHub instead
    # of falling back to the PC; the place says so. ``parts`` is the most machines the run can ask (``per_merge``).
    too_big = ""
    per_merge = settings.get("github_machines_per_merge") or core_settings.DEFAULTS["github_machines_per_merge"]
    if github_ids and not github_run.inputs_fit(merge_github.ref_name(branch), per_merge, ids=github_ids,
                                                windows=_windows_for(github_ids, pc_files)):
        too_big = f": selection too big for one dispatch ({len(github_ids)} ids), full suite"
        github_ids = None
        # the full run's readers: every PC-only reader the reads data names, not only the selected ones (#260)
        readers = _pc_readers(None, pc_files, suite_dir, cwd) if run_readers is not None else []

    def on_github(run_ids: list[str] | None):
        return merge_github.run(plan, cwd=cwd, suite_dir=suite_dir, sha=sha, branch=branch, priority=priority,
                                settings=settings, alive=lock.heartbeat, output_path=output_path, ticket=ticket,
                                tests=None if run_ids is None else len(run_ids), ids=run_ids,
                                windows=_windows_for(run_ids, pc_files),
                                **({"skip": readers} if (readers and run_ids is None) else {}))

    res = on_github(github_ids)
    if merge_github.goes_to_pc(res):
        merge_github.append_brain_line(Path(merge_log_path).parent, plan, merge_github.brain_line_for(plan, ticket, res))
        run = on_pc()
        run.place = merge_github.pc_place_text(merge_github.pc_reason(res))
        run.machines, run.part_seconds, run.minutes = res.machines, list(res.part_seconds), res.minutes_used
        return run
    place = merge_github.place_text(res) + too_big
    text = merge_github.output_text(res, place)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
    except OSError:  # the output file is a record: a failed write never fails the merge
        pass
    run = SuiteRun(res.status == "green", text, 0, failed_tests=list(res.failed_ids), place=place,
                   machines=res.machines, part_seconds=list(res.part_seconds), minutes=res.minutes_used)
    if res.status == "red":
        _recheck_on_github(run, res, on_github, plan, ticket, merge_log_path, output_path)
    if readers:
        _fold_pc_readers(run, readers, *readers_on_pc())
    return run


def _recheck_on_github(run: "SuiteRun", res, on_github: Callable, plan, ticket: str, merge_log_path: Path,
                       output_path: Path) -> None:
    """#247: the failed ids of a red GitHub run, run again on GitHub as a 1-machine run (``len(ids)`` tests). Its
    verdict is carried in ``run`` (``rechecked_done``, ``red_ids``, ``still``, ``ok``); a re-check GitHub could not
    start or judge leaves ``run`` red and untouched, so the caller re-checks the ids on the PC."""
    from . import merge_github
    ids = list(res.failed_ids)
    second = on_github(ids)
    if merge_github.goes_to_pc(second):
        merge_github.append_brain_line(Path(merge_log_path).parent, plan,
                                       merge_github.brain_line_for(plan, ticket, second))
        run.place += f"; re-check of {len(ids)} on the PC ({merge_github.pc_reason(second)})"
        return
    note = f"re-check of {len(ids)} on GitHub: " + merge_github.place_text(second).removeprefix("suite on GitHub: ")
    text = merge_github.output_text(second, note)
    try:
        with open(output_path, "a", encoding="utf-8") as fh:  # the record keeps both runs
            fh.write("\n" + text)
    except OSError:
        pass
    still = [i for i in second.failed_ids if i]
    run.rechecked_done, run.red_ids, run.still = True, ids, still
    run.ok = not still
    run.output += "\n" + text
    run.place += f"; {note}"
    run.minutes += second.minutes_used
    run.part_seconds = [*run.part_seconds, *second.part_seconds]


def _plan_recheck(cwd: Path, old: str, new: str, is_pytest: bool) -> tuple[list[str] | None, str]:
    """#212: after a green suite, main moved from ``old`` to ``new`` by commits that need a suite. ``(ids, why)``:
    ``ids`` are the tests those commits reach (``[]`` = none), or ``None`` for the whole suite with ``why`` its
    reason. Any doubt or failure is the whole suite; the selection never breaks a merge."""
    if not is_pytest:
        return None, "suite is not pytest"
    if old == new:
        return None, "main did not move (push refused)"
    if not _is_ancestor(cwd, old, new):
        return None, "main history rewritten"
    from .test_select import map_dir, select_for_diff
    try:
        sel = select_for_diff(cwd, old, new, map_dir(cwd))
    except Exception as exc:  # noqa: BLE001 -- the selection never breaks a merge
        return None, f"selection failed: {_first_line(str(exc)) or type(exc).__name__}"
    if sel.full:
        return None, sel.reason
    return list(sel.ids), sel.line


HOLD_POLL_SECONDS = 0.5  # how often a held (play) merge line re-reads the machine mode


def machine_holds() -> bool:
    """True while the machine is in play, or the mode file cannot be read: the merge line then starts nothing
    (no suite, no line check, no full run), a queued entry just waits."""
    return read_gate(current_mode, MachineModeUnreadable).holds


def wait_while_held(heartbeat: Callable[[], None] | None = None) -> None:
    """Block until the machine is out of play; nothing here times out (the stood time is unit 6's)."""
    while machine_holds():
        if heartbeat is not None:
            heartbeat()
        time.sleep(HOLD_POLL_SECONDS)


SAFETY_RED_FILE = "safety_red.json"  # the safety run's red marker in the map dir (#195); map_run writes it


def safety_red_path(root) -> Path:
    from .test_select import map_dir  # lazy, as the other map_dir uses in this module

    return map_dir(root) / SAFETY_RED_FILE


def hold_while_safety_red(root=None, heartbeat: Callable[[], None] | None = None,
                          log_path: Path | None = None, lock_path: Path | None = None) -> None:
    """A merge takes no suite on top of a red full run (#195). While the marker stands: one log line, then the
    caller, which holds the merge lock, resolves it (``safety_revert.resolve_safety_red`` reverts the landing the
    failing test depends on, or logs why none, and removes the marker); the wait ends. Both call sites hold the
    merge lock. ``lock_path`` (the batch path, which holds no checkout lock at this point) makes the revert take the
    checkout lock; the commit gate already holds it and passes none (the lock is not reentrant). A marker a live safety run still rewrites is resolved too: the next landing spawns a new run."""
    root = ROOT if root is None else root
    marker = safety_red_path(root)
    if not marker.exists():
        return
    try:
        full_sha = str(json.loads(marker.read_text(encoding="utf-8")).get("sha") or "")
    except (OSError, ValueError, AttributeError):
        full_sha = ""
    sha = full_sha[:12] or "?"
    path = log_path or _merge_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    from .safety_revert import already_reverted, resolve_safety_red  # lazy: it imports this module
    from .test_select import map_dir

    try:
        settled = bool(full_sha) and already_reverted(root, full_sha)
    except RuntimeError:  # an unreadable history: resolve_safety_red reads it again and logs "not reverted: <reason>"
        settled = False
    if not settled:  # a marker of a landing main already reverted holds nothing
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{now()} | map-run | safety run: queue held by red {sha}\n")
    if heartbeat is not None:
        heartbeat()

    if lock_path is None:
        resolve_safety_red(root, map_dir(root), path)
    else:
        with _checkout_locked(lock_path):  # the revert moves the checkout's own main
            resolve_safety_red(root, map_dir(root), path)


def _spawn_map_run(cwd: Path, sha: str) -> str:
    """After a landing: ``python -m tools.agentops map-run --commit <sha> --detach`` in the main
    checkout, hidden, not waited for. Returns "" or one line saying why it did not start.
    In play (or an unreadable mode) nothing is spawned; the next landing after ``normal`` asks again."""
    if machine_holds():
        return "machine mode play: no full run spawned"
    err_path = _merge_log_path().parent / "full_run.err"  # the child's output: a file, never DEVNULL (#41)
    try:
        err_path.parent.mkdir(parents=True, exist_ok=True)
        with open(err_path, "a", encoding="utf-8") as err:
            subprocess.Popen([sys.executable, "-m", "tools.agentops", "map-run", "--commit", sha, "--detach"],
                             cwd=str(cwd), creationflags=no_window_flags(), stdin=subprocess.DEVNULL,
                             stdout=err, stderr=subprocess.STDOUT)
    except Exception as exc:  # noqa: BLE001 -- never fails the merge
        return _first_line(str(exc)) or type(exc).__name__
    return ""


def _log_map_run_spawn(branch: str, sha: str, problem: str, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(f"{now()} | {branch} | map-run-not-started | {sha[:12]} | {problem}\n")


def _failed_ids(output: str) -> list[str]:
    """The ``FAILED <id>`` ids of pytest's own "short test summary info" section (the last one in
    ``output``); a line a test printed (``FAILED tests/t.py::bad - boom`` in captured stdout) is never an
    id. An output with no such section (a stub suite) is read whole."""
    output = _strip_ansi(output)
    marker = output.rfind("short test summary info")
    if marker >= 0:
        output = output[marker:]
    return list(dict.fromkeys(i.rstrip() for i in _FAILED_ID_RE.findall(output)))


def _parse_pytest_failures(output: str) -> tuple[int | None, list[str]]:
    """``(failed_count, failed_ids)`` from pytest's own short summary section; ``failed_count``
    is ``None`` when the output holds no final ``N failed`` summary line to read it from."""
    output = _strip_ansi(output)
    ids = _failed_ids(output)
    match = _SUMMARY_FAILED_RE.search(output)
    if match is None:
        return None, ids
    return int(match.group(1)), ids


def _red_suite_message(failed_count: int | None, failed_ids: list[str], output_path: Path) -> str:
    if failed_count is None:
        return f"the suite failed (the output: {output_path})"
    shown = failed_ids[:MAX_FAILED_IDS_SHOWN]
    id_str = ", ".join(shown)
    extra = len(failed_ids) - len(shown)
    if extra > 0:
        id_str = f"{id_str}, and {extra} more" if id_str else f"and {extra} more"
    return f"{failed_count} failed: {id_str} (the output: {output_path})"


def _red_with_fail_fast(run: "SuiteRun", failed_count: int | None, failed_ids: list[str], output_path: Path) -> str:
    """#219: the red message. A run the fail-fast watch ended names its test and both failures (when the ids the
    result ends with are still that one test: batch triage may name others); the watch's notes (a reading or a
    re-run that could not tell) follow any red message."""
    if run.fail_fast and failed_ids == [run.fail_fast.test]:
        message = f"{run.fail_fast.message()} (the output: {output_path})"
    else:
        message = _red_suite_message(failed_count, failed_ids, output_path)
    return "; ".join([message, *run.fail_fast_notes])


def _workers_suffix(result: "MergeResult") -> str:
    k = result.suite_workers
    cut = getattr(k, "cut", "")  # a count cut to one under the RAM floor names why
    base = f", {k} workers" if k > 1 else (f", 1 worker ({cut})" if cut else (", 1 worker" if k == 1 else ""))
    if not result.suite_host:
        return base
    note = f", {result.skipped_note}" if result.skipped_note else ""
    return f"{base}, host {result.suite_host}{note}"


def _recheck_suffix(result: "MergeResult") -> str:
    note = result.recheck_note
    return note if not note or note[0] in " ,;(" else f" ({note})"  # #223: `re-check under load` reads as a note


def _verdict_text(verdicts: dict[str, dict]) -> str:
    return "".join(merge_load.verdict_line(rec) + "\n" for rec in verdicts.values())


def _verdict_lines(result: "MergeResult") -> str:
    """#223: one merge.log line per failed test, after the attempt's own line; "" when nothing was judged."""
    return _verdict_text(result.verdicts)


def _append_verdict_log(verdicts: dict[str, dict], log_path: Path, note: str = "") -> None:
    """#223: verdict lines (and the note) of tests no merge-log attempt line carries (an ejected batch member's, a
    line check's green re-check)."""
    if not verdicts and not note:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(_verdict_text(verdicts) + (note + "\n" if note else ""))


def _place_part(result: "MergeResult") -> str:
    """The merge.log field of where the suite ran (#232), with its closing separator; "" when GitHub was not asked."""
    return f"{result.suite_place} | " if result.suite_place else ""


def _mod_tests_part(result: "MergeResult") -> str:
    """The merge.log field of the mods' results (#194), with its closing separator; "" when no mod ran."""
    return f"mod tests: {', '.join(result.mod_tests)} | " if result.mod_tests else ""


def _append_merge_log(branch: str, result: "MergeResult", log_path: Path | None = None) -> None:
    """One line per merge attempt in ``log_path``: time, branch, reason, the suite's
    duration (a human unit, principle 7), the count of failed tests, the output's path.

    ``log_path`` is looked up from ``MERGE_LOG_PATH`` at call time, not bound as a
    def-time default, so a test that monkeypatches the module constant is honoured."""
    if log_path is None:
        log_path = _merge_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    failed = str(result.failed_count) if result.failed_count is not None else "0"
    if result.rechecked:
        failed = (f"0 failed after re-check of {len(result.rechecked)}{_recheck_suffix(result)}: "
                  f"{', '.join(result.rechecked)}")
    else:
        failed += " failed"
    line = (f"{now()} | {branch} | {result.reason} | suite {human_duration(result.suite_duration)}{_workers_suffix(result)} | "
           f"{failed} | {result.selection + ' | ' if result.selection else ''}"  # next to the suite time
           f"{_place_part(result)}"  # #232: where the suite ran
           f"{_mod_tests_part(result)}"
           f"output {result.suite_output_path or '-'} | "
           f"suite dir {result.suite_dir or '-'}")
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n" + _verdict_lines(result))


def _append_lock_timeout_log(branch: str, exc: "MergeLockTimeout", log_path: Path | None = None) -> None:
    """One merge-log line for a lock timeout: the holder pid, held for, silent for (human form)."""
    if log_path is None:
        log_path = _merge_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    held = human_duration(exc.held) if exc.held is not None else "?"
    silent = human_duration(exc.silent) if exc.silent is not None else "no progress file"
    line = (f"{now()} | {branch} | lock-timeout | holder pid {exc.pid} | held for {held} | "
            f"silent for {silent}")
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _append_batch_log(branches: list[str], result: "MergeResult", log_path: Path) -> None:
    """ONE line for a whole batch that one suite run pushed."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    failed = (f"0 failed after re-check of {len(result.rechecked)}{_recheck_suffix(result)}: "
              f"{', '.join(result.rechecked)}" if result.rechecked else "0 failed")
    line = (f"{now()} | batch of {len(branches)}: {', '.join(branches)} | {result.reason} | "
            f"suite {human_duration(result.suite_duration)}{_workers_suffix(result)} | {failed} | "
            f"pushed {result.head_after[:12]}{' | ' + result.selection if result.selection else ''} | "
            f"{_place_part(result)}"
            f"{_mod_tests_part(result)}"
            f"output {result.suite_output_path or '-'}")
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n" + _verdict_lines(result))


def _append_left_in_line_log(branch: str, reason: str, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(f"{now()} | {branch} | left in line: {reason}\n")


def _append_red_batch_log(branches: list[str], duration: float, failed: str, output_path: Path,
                          log_path: Path) -> None:
    """ONE line for a batch leg whose suite stayed red."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = (f"{now()} | batch of {len(branches)}: {', '.join(branches)} | red-suite | "
            f"suite {human_duration(duration)} | {failed} failed | output {output_path}")
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _append_member_log(branch: str, text: str, log_path: Path) -> None:
    """One line for a batch member that leaves the line: ``ejected: ...`` or ``re-queued after red batch``."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(f"{now()} | {branch} | {text}\n")


def read_issue_state(number: int) -> tuple[str, str]:
    """(state, stateReason) of issue ``number`` as ``gh issue view`` reports them: ``OPEN``/``CLOSED``,
    and ``COMPLETED``/``NOT_PLANNED``/``""``. Raises on any failure. Under pytest the real ``gh`` is not run
    (a test stubs this function; the same refusal as ``detach``'s hand-back)."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("the real gh is not read under pytest")
    result = run_gh(["issue", "view", str(number), "--json", "state,stateReason"])
    if result.returncode != 0:
        raise RuntimeError(f"gh issue view {number} failed: {(result.stderr or '').strip()[:200]}")
    data = json.loads(result.stdout)
    return str(data.get("state") or "").upper(), str(data.get("stateReason") or "").upper()


def _dropped_for_not_planned(issues: Sequence[int], branch: str, log_path: Path) -> "MergeResult | None":
    """Read at the moment the merge takes the lock. An issue closed as not planned drops the merge
    (nothing pushed); a failed read merges as today and is logged."""
    for number in issues:
        try:
            state, reason = read_issue_state(number)
        except Exception as exc:  # noqa: BLE001 - a failed read merges as today
            _append_plain_log(branch, f"issue state not read: #{number} ({exc})", log_path)
            continue
        if state == "CLOSED" and reason == "NOT_PLANNED":
            return MergeResult(ok=False, reason="not-planned",
                               message=f"issue #{number} closed as not planned: merge dropped")
    return None


def _append_plain_log(branch: str, text: str, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(f"{now()} | {branch} | {text}\n")


def _priority_unread_log(branch: str, log_path: Path) -> Callable[[int, Exception], None]:
    """The one plain ``priority not read`` line for an issue whose labels could not be read (#122, #140)."""
    return lambda n, exc: _append_plain_log(
        branch, f"priority not read: #{n} ({type(exc).__name__}: {str(exc)[:200]})", log_path)


def _append_line_poll_log(branch: str, stop: str | None, dropped: list[str], kept: int, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(f"{now()} | {branch} | line poll: {kept} claimable; stopped at {stop or 'end of queue'}; "
                 f"filter dropped {len(dropped)}{': ' + ', '.join(dropped) if dropped else ''}\n")


class MergeError(RuntimeError):
    """A setup problem (no suite command, bad arguments): never a merge outcome."""


class MergeLockLost(RuntimeError):
    """The lock file no longer names this holder: nothing the lock protects may be touched."""


class MergeLockTimeout(RuntimeError):
    """A live holder has been silent longer than the ceiling: never waited for forever.
    ``pid``, ``held`` and ``silent`` (seconds, ``silent`` None without a progress file) name it."""

    def __init__(self, message: str, pid=None, held: float | None = None, silent: float | None = None):
        super().__init__(message)
        self.pid, self.held, self.silent = pid, held, silent


class MergeLockBusy(RuntimeError):
    """The lock stayed taken for the whole ``wait_s`` the caller allowed (#49): not a stuck holder
    (``MergeLockTimeout``) but a bounded wait that ran out. ``pid`` names the holder when readable."""

    def __init__(self, message: str, pid=None):
        super().__init__(message)
        self.pid = pid


class MergeWithdrawn(RuntimeError):
    """#219 rule 3: the queued ticket of this waiter was withdrawn (`merge withdraw`): it never takes the line."""


class MergeAlreadyQueued(RuntimeError):
    """A live merge ticket of the same branch (and cwd) is already in the line: no new
    ticket was written. ``pid``, ``since`` (epoch seconds) and ``result`` name that entry."""

    def __init__(self, branch: str, pid: int, since: float, result: str = ""):
        self.branch, self.pid, self.since, self.result = branch, pid, since, result
        stamp = time.strftime("%H:%M", time.localtime(since))
        super().__init__(f"already queued: {branch} since {stamp} ({human_duration(max(0.0, time.time() - since))}) "
                         f"by pid {pid}, result {result or 'see merge.log'}")


def _resolve_max_age_seconds(override: float | None) -> float:
    """``override`` wins (tests scale it down); otherwise the settings key
    ``merge_lock_max_age_min`` (minutes), default 30."""
    if override is not None:
        return float(override)
    try:
        from .core_settings import load as load_settings
        settings = load_settings()
    except Exception:
        settings = {}
    minutes = settings.get("merge_lock_max_age_min", DEFAULT_MAX_AGE_MINUTES)
    return float(minutes) * 60.0


def _resolve_silent_takeover_seconds(override: float | None) -> float:
    """``override`` wins (tests); otherwise the settings key ``merge_lock_silent_takeover_min`` (minutes),
    default 30; 0 or less means no takeover."""
    if override is not None:
        return float(override)
    try:
        from .core_settings import load as load_settings
        settings = load_settings()
    except Exception:
        settings = {}
    minutes = settings.get("merge_lock_silent_takeover_min", DEFAULT_SILENT_TAKEOVER_MINUTES)
    try:
        return float(minutes) * 60.0
    except (TypeError, ValueError):
        return DEFAULT_SILENT_TAKEOVER_MINUTES * 60.0


def _tree_is_burning(pid: int, *, cpu_for: Callable[[int], Callable[[], float]] | None = None,
                     sleep: Callable[[float], None] = time.sleep, window: float = BURN_WINDOW_SECONDS) -> bool:
    """True when ``pid``'s process tree used at least ``STALL_MIN_SHARE`` of a core over ``window`` seconds:
    a suite that is only collecting prints nothing but burns CPU (#47), so it is never a silent holder."""
    probe = (cpu_for or TreeCpu)(pid)
    before = probe()
    sleep(window)
    return probe() - before >= STALL_MIN_SHARE * window


def _holder_tree_burning(pid) -> bool:
    try:
        return _tree_is_burning(int(pid))
    except Exception:  # noqa: BLE001 -- an unreadable tree is not proof of life: judged by the silence alone
        return False


def _parse_started(started: str | None) -> float | None:
    """``started`` is ``now()``'s own format; unparsable or missing reads as "just now". A thin
    wrapper over `util.parse_started`, the same parse `holder_is_live` shares below."""
    return _shared_parse_started(started)


# -- the lock -------------------------------------------------------------------------------

def _is_free_commit(info: object) -> bool:
    """A queue entry of a code-free commit: passed over by the line, it stays queued."""
    return isinstance(info, dict) and info.get("kind") == "commit" and info.get("needs_suite") is False


def _is_merge(info: object) -> bool:
    return isinstance(info, dict) and info.get("kind") == "merge"


@dataclass
class Waiter:
    ticket: str
    pid: int
    info: dict


class MergeLock:
    """A file holding the pid and start time of whoever runs the critical section.

    A holder whose pid is gone is taken over at once: the stale file is removed and
    creation retried immediately, no sleep. A live holder is waited for while it stays
    alive and its lock is younger than the age ceiling (``max_age_seconds``, resolved from
    the settings key ``merge_lock_max_age_min`` when not given); a holder never freed
    within that ceiling is named, not waited for forever (``MergeLockTimeout``).
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._own = False
        self.token = uuid.uuid4().hex  # written into the holder file at acquire: names this instance
        self._holder_fields: dict = {}  # #37: what the holder file says of its holder (kind, message/issues or branch)
        self.wait_s: float | None = None  # #49: a bound on the wait for the lock (``MergeLockBusy``); None = unbounded

    def _read_holder(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return None

    @contextmanager
    def _guard(self, deadline: float):
        """A short OS-level lock on a side file, held around one create-or-
        inspect step. Creating and writing the holder file, and judging a holder dead
        and removing it, never interleave across processes: without it a waiter could read a
        file another waiter had just created (still empty) as dead and remove it, or remove a
        new holder's file in the gap between its own check and its unlink. The OS frees the
        guard when its process dies, so a crash never leaves it held. The side file stays, so it
        lives in the temp directory, named by the lock's resolved path, never inside a repo."""
        key = hashlib.sha1(str(self.path.resolve()).lower().encode("utf-8")).hexdigest()[:16]
        guard_path = Path(tempfile.gettempdir()) / f"agentops-merge-lock-{key}.guard"
        while True:
            try:
                fd = os.open(guard_path, os.O_CREAT | os.O_RDWR)
                break
            except PermissionError:  # Windows: a scanner or a racing create holds it a moment
                if time.time() > deadline:
                    raise
                time.sleep(0.01)
        try:
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    if os.name == "nt":
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.time() > deadline:
                        raise MergeLockTimeout(f"the merge lock's guard {guard_path} stayed taken")
                    time.sleep(0.01)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _file_age(self) -> float | None:
        try:
            mtime = self.path.stat().st_mtime
            return time.time() - mtime - stood_between(mtime, time.time())  # play time not counted
        except OSError:
            return None

    def _is_head(self, ticket: Path) -> bool:
        """The next holder: True when ``ticket`` is the first of the line, which is Priority tickets by rank
        (#122), then the oldest ticket of a live waiter. Tickets of dead pids ahead of the first live one are
        removed here (called under the guard). Name = ``<time_ns>-<pid>-<seq>``."""
        try:
            names = sorted(self._queue_dir.iterdir(), key=lambda t: t.name)
        except OSError:
            return True
        live: list[Path] = []
        for t in names:
            try:
                pid = int(t.name.split("-")[1])
            except (IndexError, ValueError):
                pid = 0
            if t == ticket or pid_alive(pid):
                live.append(t)
                continue
            if not live:
                try:
                    t.unlink()
                except OSError:
                    pass
        if not live:
            return True
        # the same barrier as `waiting_line`: Priority order applies only to the tickets ahead of the first
        # non-merge ticket that needs a suite (a Priority merge does not jump a code commit)
        ahead: list[Waiter] = []
        first = live[0]
        for t in live:
            try:
                info = json.loads(t.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                break
            if not _is_free_commit(info) and not _is_merge(info):
                break
            ahead.append(Waiter(t.name, 0, info))
        head = merge_priority.order_waiters(ahead)[0].ticket if ahead else first.name
        return head == ticket.name

    @property
    def _batch_dir(self) -> Path:
        return self.path.with_name(self.path.name + ".batch")

    def _write_atomic(self, dest: Path, text: str) -> None:
        """Temp file in ``<lock>.batch/`` (never inside the queue dir), then ``os.replace``."""
        self._batch_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._batch_dir / f"tmp-{uuid.uuid4().hex}"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, dest)

    holds_in_play = True  # the merge line's lock; the short checkout lock overrides it
    takes_silent_holder = True  # #62: the merge line's lock only; the short checkout lock overrides it

    def acquire(self, max_age_seconds: float | None = None, *, ticket_info: dict | None = None,
                waiter_hook: "WaiterHook | None" = None,
                silent_takeover_seconds: float | None = None, arrival_ns: int | None = None) -> dict | None:
        return self._take(max_age_seconds, ticket_info, waiter_hook, silent_takeover_seconds, arrival_ns)

    ticket_ns = 0  # #122: the arrival time (ns) in the name of the ticket this lock queued with

    def _take(self, max_age_seconds: float | None, ticket_info: dict | None,
              waiter_hook: "WaiterHook | None" = None, silent_takeover_seconds: float | None = None,
              arrival_ns: int | None = None) -> dict | None:
        """Waiters are served in arrival order: each writes a ticket ``<lock>.queue/<time_ns>-<pid>``
        and may take the lock only when it is the oldest ticket of a live pid. The ticket goes
        on acquire and on every exit path. Its content is ``json.dumps(ticket_info or {})``.
        Returns ``None`` when the lock is now held, or the dict a holder posted for this ticket
        (the lock is then NOT held).

        ``waiter_hook(ticket_name, holder_live, claimed)`` (default None = no hook) is called once per
        turn of the wait, outside the guard; a dict it returns is handled like a posted result: returned,
        the lock NOT held, the ticket removed here.

        ``arrival_ns`` (#122) names the ticket with an earlier arrival time: a batch cut for a Priority ticket goes
        back in line at its place, not at the end."""
        self._queue_dir = self.path.with_name(self.path.name + ".queue")
        self._queue_dir.mkdir(parents=True, exist_ok=True)
        self._holder_fields = _holder_record(ticket_info)
        self.ticket_ns = int(arrival_ns) if arrival_ns else time.time_ns()
        ticket_name = f"{os.getpid()}-{next(_TICKET_SEQ)}"
        ticket = self._queue_dir / f"{self.ticket_ns:020d}-{ticket_name}"
        if ticket_info and ticket_info.get("kind") == "merge":
            for _attempt in range(3):  # #219 rule 2: an older head of this branch is ended, then the new one is written
                with self._guard(time.time() + 30.0):
                    older = self._older_heads(ticket_info)
                    if not older:
                        self._write_atomic(ticket, json.dumps(ticket_info))
                        break
                older_ns = self._supersede(older, ticket_info)  # outside the guard: the take-out takes it itself
                if not arrival_ns:  # the new head takes the older entry's place in line (its arrival time)
                    self.ticket_ns = older_ns
                    ticket = self._queue_dir / f"{self.ticket_ns:020d}-{ticket_name}"
            else:
                raise MergeAlreadyQueued(str(ticket_info.get("branch")), os.getpid(), time.time())
        else:
            self._write_atomic(ticket, json.dumps(ticket_info or {}))
        try:
            posted = self._acquire(ticket, max_age_seconds, waiter_hook, silent_takeover_seconds)
            if posted is None and ticket_info and ticket_info.get("kind") == "merge":
                # the holder's ticket leaves the queue now: this sidecar keeps its branch visible to a
                # duplicate until release(); written before the ticket goes, so no gap
                self._write_atomic(self._holding_path, json.dumps(
                    {"pid": os.getpid(), "ns": self.ticket_ns, "info": ticket_info}))
            return posted
        finally:
            for f in (ticket, self._batch_dir / f"{ticket.name}.claim",
                      self._batch_dir / f"{ticket.name}.result"):
                _unlink_retrying(f)

    @property
    def _holding_path(self) -> Path:
        return self.path.with_name(self.path.name + ".holding")

    def _older_heads(self, ticket_info: dict) -> list[tuple[int, int, dict, Path | None]]:
        """The live merge entries of this branch and cwd (queued tickets and the holder running it) that a merge at
        ``ticket_info["head"]`` replaces (#219 rule 2); ``[]`` when there are none. Raises ``MergeAlreadyQueued`` when one
        cannot be replaced: the same head, a head not recorded (a record made before rule 2), or a member of a running
        batch (its leader runs it). Each is ``(arrival ns, pid, record, ticket file or None for the holder)``."""
        entries: list[tuple[int, int, object, Path | None]] = []
        try:
            names = sorted(self._queue_dir.iterdir(), key=lambda t: t.name)
        except OSError:
            names = []
        for t in names:
            try:
                entries.append((int(t.name.split("-")[0]), int(t.name.split("-")[1]), t, t))
            except (IndexError, ValueError):
                continue
        try:
            h = json.loads(self._holding_path.read_text(encoding="utf-8"))
            entries.append((int(h["ns"]), int(h["pid"]), h["info"], None))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        older: list[tuple[int, int, dict, Path | None]] = []
        new_head = str(ticket_info.get("head") or "")
        for ns, pid, src, ticket in entries:
            try:
                info = src if isinstance(src, dict) else json.loads(src.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if not (isinstance(info, dict) and info.get("kind") == "merge"
                    and info.get("branch") == ticket_info.get("branch")
                    and info.get("cwd") == ticket_info.get("cwd") and pid_alive(pid)):
                continue
            old_head = str(info.get("head") or "")
            in_batch = ticket is not None and (self._batch_dir / f"{ticket.name}.claim").exists()
            past_return = ticket is None and (self._leads_a_batch() or self._head_landed(info, old_head))
            if not new_head or not old_head or old_head == new_head or in_batch or past_return:
                raise MergeAlreadyQueued(info["branch"], pid, ns / 1e9, str(info.get("result") or ""))
            older.append((ns, pid, info, ticket))
        return older

    def _leads_a_batch(self) -> bool:
        """The lock's holder has live batch members claimed (their `<ticket>.claim` names the holder's token)."""
        holder = self._read_holder() or {}
        if not holder.get("token"):
            return False
        try:
            claims = list(self._batch_dir.glob("*.claim"))
        except OSError:
            return False
        for c in claims:
            try:
                data = json.loads(c.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (isinstance(data, dict) and data.get("token") == holder["token"]
                    and holder_is_live(data.get("pid"), data.get("started"))):
                return True
        return False

    @staticmethod
    def _head_landed(info: dict, head: str) -> bool:
        """#219 rule 2: the running older head is already in `<remote>/<main>`: its run landed or is pushing, past the
        point of no return, so it is never ended. A git that cannot answer says no (such a run cannot land either)."""
        try:
            return _is_ancestor(Path(str(info.get("cwd") or ".")), head,
                                f"{info.get('remote') or 'origin'}/{info.get('main') or 'main'}")
        except (OSError, ValueError):
            return False

    def _supersede(self, older: list[tuple[int, int, dict, Path | None]], ticket_info: dict) -> int:
        """Ends ``older`` (merge_withdraw.supersede); an entry that cannot be ended stays in line and the new merge
        answers ``already queued`` as before rule 2. Returns the place (arrival ns) the new head takes."""
        from . import merge_withdraw  # lazily: merge_withdraw imports this module
        try:
            arrival_ns = merge_withdraw.supersede(self, older, str(ticket_info.get("head") or ""))
        except merge_withdraw.SupersedeFailed as exc:
            ns, pid, info, _ticket = older[0]
            print(f"merge supersede: {exc}; {info.get('branch')} stays in line", file=sys.stderr)
            raise MergeAlreadyQueued(info["branch"], pid, ns / 1e9, str(info.get("result") or "")) from exc
        inherited = sorted({int(n) for _ns, _pid, info, _t in older for n in info.get("issues") or []
                            if isinstance(n, int)} - set(ticket_info.get("issues") or []))
        if inherited:  # the tickets parked on the older result file get the new run's result
            ticket_info["issues"] = [*(ticket_info.get("issues") or []), *inherited]
            print(f"merge-inherited: {','.join(str(n) for n in inherited)}", file=sys.stderr)  # detach reads it
        return arrival_ns

    def _acquire(self, ticket: Path, max_age_seconds: float | None,
                 waiter_hook: "WaiterHook | None" = None,
                 silent_takeover_seconds: float | None = None) -> dict | None:
        ceiling = _resolve_max_age_seconds(max_age_seconds)
        takeover_limit = (_resolve_silent_takeover_seconds(silent_takeover_seconds)
                          if self.takes_silent_holder else 0.0)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        last_report = time.time()
        wait_deadline = None if self.wait_s is None else time.time() + self.wait_s
        unreadable_since: float | None = None  # this waiter's own clock for an unreadable holder
        while True:
            if self.holds_in_play and machine_holds():  # in play the line takes no lock; the entry waits
                if wait_deadline is not None and time.time() >= wait_deadline:
                    raise MergeLockBusy(f"the merge lock {self.path} stayed unavailable (machine in play) for "
                                        f"{human_duration(self.wait_s)}")
                time.sleep(HOLD_POLL_SECONDS)
                continue
            head_ok = False  # set when this turn finds this waiter at the head of the line (it alone may take over)
            with self._guard(time.time() + max(ceiling, 30.0)):
                result_file =self._batch_dir / f"{ticket.name}.result"
                claim_file = self._batch_dir / f"{ticket.name}.claim"
                if result_file.exists():
                    try:
                        posted = json.loads(result_file.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        posted = None
                    if isinstance(posted, dict):
                        for f in (result_file, claim_file):
                            try:
                                f.unlink()
                            except OSError:
                                pass
                        return posted
                if self._holder_fields.get("kind") == "merge" and not ticket.exists():  # #219: withdrawn while queued
                    raise MergeWithdrawn(f"the queued merge of {self._holder_fields.get('branch')} was withdrawn")
                claimed = False
                if claim_file.exists():
                    try:
                        claim = json.loads(claim_file.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        claim = None
                    if isinstance(claim, dict) and holder_is_live(claim.get("pid"), claim.get("started")):
                        claimed = True
                    else:
                        try:
                            claim_file.unlink()
                        except OSError:
                            pass
                try:
                    if claimed:
                        raise FileExistsError  # a holder works for me: no head check
                    if not self._is_head(ticket):
                        raise FileExistsError  # not my turn: wait like on a held lock
                    head_ok = True
                    fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    try:
                        os.write(fd, json.dumps({"pid": os.getpid(), "started": now(), "token": self.token,
                                                 **self._holder_fields}).encode())
                    finally:
                        os.close(fd)
                    self._own = True
                    unreadable_since = None
                    return None
                except (FileExistsError, PermissionError):
                    # Windows: a lock file just unlinked and still open elsewhere answers the
                    # create with `PermissionError` (its pending-delete state), not
                    # `FileExistsError` -- treated the same way, one more turn of the loop.
                    holder = self._read_holder()
                    file_age = None
                    if holder is None:
                        # Exists but cannot be parsed: just created and not yet written, or in
                        # pending-delete. Waited on while young; past the grace it is stale.
                        # The waiter keeps its own clock: `stat` may fail every turn.
                        if unreadable_since is None:
                            unreadable_since = time.time()
                        file_age = self._file_age()
                        clock_age = time.time() - unreadable_since - stood_between(unreadable_since, time.time())
                        file_age = clock_age if file_age is None else max(file_age, clock_age)
                        if file_age > UNPARSABLE_GRACE_SECONDS:
                            try:
                                self.path.unlink()
                            except OSError:
                                pass  # pending-delete: the name frees itself, wait one turn
                            else:
                                print(f"merge lock: removed a stale unreadable holder file "
                                      f"{self.path}, age {human_duration(file_age)}")
                                unreadable_since = None
                                continue
                    else:
                        unreadable_since = None
                    pid = holder.get("pid") if holder else None
                    started = holder.get("started", "?") if holder else "?"
                    if holder is not None and not holder_is_live(pid, started if started != "?" else None):
                        try:
                            self.path.unlink()
                        except OSError:
                            pass
                        continue  # a dead or stale (pid reused) holder is taken over at once, no wait
            held = silent = None
            if holder is None:
                age = held = file_age or 0.0
            else:
                started_epoch = _parse_started(started if started != "?" else None)
                held = (time.time() - started_epoch - stood_between(started_epoch, time.time())
                        if started_epoch is not None else 0.0)  # play time is not a hung holder
                age = held
                progress = holder.get("progress")
                if progress:
                    try:
                        mtime = Path(progress).stat().st_mtime
                        silent = max(0.0, time.time() - mtime - stood_between(mtime, time.time()))
                    except OSError:
                        silent = None
                    if silent is not None:
                        age = min(held, silent)  # age counts from the newer of started and the output's mtime
                if (head_ok and not claimed and silent is not None and takeover_limit > 0
                        and silent > takeover_limit and not _holder_tree_burning(pid)
                        and self._take_over_silent_holder(holder, held, silent, takeover_limit)):
                    continue  # the line is free: the next turn creates the lock file
            if holder is None and age > ceiling and not claimed:
                if self.path.exists():
                    what = f"the merge lock file {self.path} has been unreadable for {human_duration(held)}"
                else:
                    what = (f"the merge lock {self.path} is free but this waiter has not been "
                            f"served for {human_duration(held)}")
                raise MergeLockTimeout(what, pid=None, held=held, silent=None)
            if age > ceiling and not claimed:
                silent_txt = human_duration(silent) if silent is not None else "no progress file"
                raise MergeLockTimeout(
                    f"the merge lock is held by pid {pid} (since {started}), "
                    f"held for {human_duration(held)}, output silent for {silent_txt}",
                    pid=pid, held=held, silent=silent,
                )
            now_t = time.time()
            if wait_deadline is not None and now_t >= wait_deadline:
                raise MergeLockBusy(f"the merge lock {self.path} was held by pid {pid} for the whole "
                                    f"{human_duration(self.wait_s)} wait", pid=pid)
            if now_t - last_report >= REPORT_INTERVAL_SECONDS:
                print(f"waiting on the merge lock: pid {pid}, age {human_duration(age)}")
                last_report = now_t
            if waiter_hook is not None:
                left = waiter_hook(ticket.name, holder is not None, claimed)
                if isinstance(left, dict):
                    with self._guard(time.time() + max(ceiling, 30.0)):
                        if not self._claim_is_live(claim_file):
                            # leaves the line: the ticket goes under the guard, so no holder can claim it now
                            try:
                                ticket.unlink()
                            except OSError:
                                pass
                            return left
                    # a holder claimed this waiter meanwhile: the line takes it as it always did
            time.sleep(0.1)

    def _take_over_silent_holder(self, holder: dict, held: float, silent: float, limit: float) -> bool:
        """#62: ``holder`` (read this turn) stood silent past ``limit`` with no CPU burning. Under the guard, if
        the lock file still names it: its process tree is ended (the repo's `_kill_tree`), the lock file and the
        holder's sidecar go, a merge-log line is written, and a marker next to the holder's result file tells
        its ticket why (`detach` reads it). True when the lock file is gone."""
        with self._guard(time.time() + 30.0):
            now_holder = self._read_holder()
            if (now_holder is None or now_holder.get("pid") != holder.get("pid")
                    or now_holder.get("token") != holder.get("token")):
                return False  # the holder changed or left meanwhile: judged again next turn
            try:
                sidecar = json.loads(self._holding_path.read_text(encoding="utf-8")).get("info") or {}
            except (OSError, ValueError, AttributeError):
                sidecar = {}
            pid = holder.get("pid")
            mine = self._holder_fields.get("branch") or "commit"
            theirs = holder.get("branch") or sidecar.get("branch") or "?"
            note = (f"silent for {human_duration(silent)} (merge_lock_silent_takeover_min "
                    f"{human_duration(limit)}): the merge line went to {mine}")
            result = str(sidecar.get("result") or "")
            if result:
                try:
                    marker = Path(result + SILENT_HOLDER_MARKER_SUFFIX)
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_text(note, encoding="utf-8")
                except OSError:
                    pass
            try:
                if int(pid) == os.getpid():  # never this process's own tree
                    raise OSError("the holder is this process")
                _kill_tree(int(pid))
                ended = "its process tree ended"
            except Exception as exc:  # noqa: BLE001 -- the line is freed all the same
                ended = f"its process tree not ended ({type(exc).__name__})"
            for f in (self.path, self._holding_path):
                _unlink_retrying(f)
            try:
                log_path = _merge_log_path()
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(f"{now()} | {mine} | silent-holder | took the merge line from pid {pid} ({theirs}) | "
                             f"held for {human_duration(held)} | silent for {human_duration(silent)} | {ended}\n")
            except OSError:
                pass
        return not self.path.exists()

    @staticmethod
    def _claim_is_live(claim_file: Path) -> bool:
        try:
            claim = json.loads(claim_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(claim, dict) and holder_is_live(claim.get("pid"), claim.get("started"))

    def set_progress(self, path: Path | str) -> None:
        """Names the suite output file in the holder file (atomic rewrite under the guard, pid,
        started and token kept): a waiter counts the holder's silence from that file's mtime."""
        self.assert_held()
        with self._guard(time.time() + 30.0):
            holder = self._read_holder()
            if (holder is None or holder.get("pid") != os.getpid()
                    or holder.get("token") != self.token):
                raise MergeLockLost(f"the merge lock {self.path} does not name this holder: on disk {holder}")
            holder["progress"] = str(path)
            tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(holder), encoding="utf-8")
            for attempt in range(50):
                try:
                    os.replace(tmp, self.path)
                    break
                except PermissionError:  # Windows: a reader has it open a moment
                    if attempt == 49:
                        raise
                    time.sleep(0.02)

    def set_holder_fields(self, fields: dict) -> None:
        """#207: adds ``fields`` to the holder file in place, under the held lock (no release, no re-take): an atomic
        rewrite as in `set_progress`, pid, started and token kept. The fields also go into ``_holder_fields``, so a
        later rewrite of the holder keeps them."""
        self.assert_held()
        self._holder_fields = {**self._holder_fields, **fields}
        with self._guard(time.time() + 30.0):
            holder = self._read_holder()
            if (holder is None or holder.get("pid") != os.getpid()
                    or holder.get("token") != self.token):
                raise MergeLockLost(f"the merge lock {self.path} does not name this holder: on disk {holder}")
            holder.update(fields)
            tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(holder), encoding="utf-8")
            for attempt in range(50):
                try:
                    os.replace(tmp, self.path)
                    break
                except PermissionError:  # Windows: a reader has it open a moment
                    if attempt == 49:
                        raise
                    time.sleep(0.02)

    def assert_held(self) -> None:
        """Raises ``MergeLockLost`` unless the lock file on disk still names this very holder
        (pid and token written at acquire). The one check before anything the lock exists to
        protect (the suite worktree's git commands)."""
        holder = self._read_holder()
        if (not self._own or holder is None or holder.get("pid") != os.getpid()
                or holder.get("token") != self.token):
            raise MergeLockLost(f"the merge lock {self.path} does not name this holder "
                                f"(pid {os.getpid()}, token {self.token[:8]}): on disk {holder}")

    # -- batch protocol (holder side) ---------------------------------------------------------

    def _remove_own_claims(self) -> None:
        try:
            claims = list(self._batch_dir.glob("*.claim"))
        except OSError:
            return
        for c in claims:
            try:
                data = json.loads(c.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("token") == self.token:
                try:
                    c.unlink()
                except OSError:
                    pass

    def waiting_line(self) -> list[Waiter]:
        """Holder only: the merge waiters, live pids, stopping before the first ticket that needs a suite and is
        not a merge (a barrier: a code commit or an older client). Priority waiters come first by rank, then
        the rest in arrival order (#122).
        A live ``{"kind": "commit", "needs_suite": false}`` ticket is passed over: it stays queued."""
        self.assert_held()
        queue_dir = self.path.with_name(self.path.name + ".queue")
        line: list[Waiter] = []
        self.line_stop = None  # what ended the walk: "<ticket> <kind/needs_suite|{}|unreadable>"
        with self._guard(time.time() + 30.0):
            try:
                names = sorted(queue_dir.iterdir(), key=lambda t: t.name)
            except OSError:
                return line
            for t in names:
                try:
                    pid = int(t.name.split("-")[1])
                except (IndexError, ValueError):
                    pid = 0
                if not pid_alive(pid):
                    continue
                try:
                    info = json.loads(t.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    self.line_stop = f"{t.name} unreadable"
                    break
                if _is_free_commit(info):
                    continue
                if not _is_merge(info):
                    what = "{}" if not info else f"kind={info.get('kind')} needs_suite={info.get('needs_suite')}"
                    self.line_stop = f"{t.name} {what}"
                    break
                line.append(Waiter(t.name, pid, info))
        return merge_priority.order_waiters(line)

    def refresh_priorities(self, reader: Callable[[int], Mapping[str, str]] | None = None,
                           log_path: Path | None = None,
                           blockers: Callable[[int], Sequence[int]] | None = None) -> None:
        """#140, holder only: before the line is ordered for a batch, read each waiting merge's issues again (one
        label read and one blocker read per issue, outside the guard) and write a changed ``priority`` and
        ``blocked_by`` (the open native blockers) back into that waiter's queue entry, so ``waiting_line``, the
        waiter-side head check and ``merge-status`` order from one entry. A failed read keeps the entry's value
        and is logged (``priority not read``, ``blockers not read``); a failed write skips that waiter and is
        logged: neither ever fails the merge. A blocker cycle in the line is logged once."""
        read_blockers = blockers if blockers is not None else merge_priority.gh_blockers
        line = self.waiting_line()
        for w in line:
            issues = [int(n) for n in (w.info.get("issues") or ())]
            branch = w.info.get("branch", w.ticket)
            old_p, old_b = w.info.get("priority"), list(w.info.get("blocked_by") or [])
            new_p = merge_priority.reread_priority(
                old_p, issues, reader, _priority_unread_log(branch, log_path) if log_path else None)
            new_b, failed = set(), False
            for n in issues:
                try:
                    new_b |= {int(b) for b in read_blockers(n)}
                except Exception as exc:  # noqa: BLE001 - a failed read changes nothing
                    failed = True
                    if log_path:
                        _append_plain_log(branch, f"blockers not read: #{n} ({type(exc).__name__}: "
                                                  f"{str(exc)[:200]})", log_path)
            new_b = old_b if failed else sorted(new_b)
            if new_p == old_p and new_b == old_b:
                continue
            w.info["priority"], w.info["blocked_by"] = new_p, new_b  # what the cycle check below reads
            try:
                with self._guard(time.time() + 30.0):
                    entry = self._queue_dir / w.ticket
                    try:
                        info = json.loads(entry.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue  # the waiter left the line meanwhile
                    info["priority"], info["blocked_by"] = new_p, new_b
                    self._write_atomic(entry, json.dumps(info))
            except OSError as exc:  # a reader holds the file (Windows): this waiter keeps its entry
                if log_path:
                    _append_plain_log(branch, f"priority not written ({type(exc).__name__}: {str(exc)[:200]})",
                                      log_path)
        cycle = merge_priority.blocker_cycle(line)
        if cycle and log_path:
            names = ", ".join(str(w.info.get("branch", w.ticket)) for w in cycle)
            _append_plain_log(cycle[0].info.get("branch", cycle[0].ticket),
                              f"blocker cycle in the line, arrival order kept: {names}", log_path)

    def claim(self, waiters: Sequence[Waiter]) -> list[Waiter]:
        """Marks each waiter whose ticket still exists as served by this holder."""
        queue_dir = self.path.with_name(self.path.name + ".queue")
        claimed: list[Waiter] = []
        with self._guard(time.time() + 30.0):
            body = json.dumps({"pid": os.getpid(), "token": self.token, "started": now()})
            for w in waiters:
                if (queue_dir / w.ticket).exists():
                    self._write_atomic(self._batch_dir / f"{w.ticket}.claim", body)
                    claimed.append(w)
        return claimed

    def post_result(self, waiter: Waiter, result: dict) -> None:
        with self._guard(time.time() + 30.0):
            self._write_atomic(self._batch_dir / f"{waiter.ticket}.result", json.dumps(result))
            try:
                (self._batch_dir / f"{waiter.ticket}.claim").unlink()
            except OSError:
                pass

    def unclaim(self, waiters: Sequence[Waiter]) -> None:
        with self._guard(time.time() + 30.0):
            for w in waiters:
                try:
                    (self._batch_dir / f"{w.ticket}.claim").unlink()
                except OSError:
                    pass

    def heartbeat(self) -> None:
        """Restarts the age ceiling's clock: rewrites the holder file with ``started = now()``."""
        with self._guard(time.time() + 30.0):
            holder = self._read_holder()
            if holder is not None and holder.get("token") == self.token:
                self.path.write_text(json.dumps({**holder, "started": now()}), encoding="utf-8")

    def release(self) -> None:
        """Unlinks the holder file under the guard (no waiter has it open then) and retries a
        failed unlink: on Windows an unlink fails with ``PermissionError`` while any process
        has the file open, and a file left behind names this live pid, so every waiter would
        wait until the age ceiling. An unlink that still fails is raised, never swallowed."""
        if not self._own:
            return
        self._own = False
        with self._guard(time.time() + 30.0):
            self._remove_own_claims()
            try:
                self._holding_path.unlink()
            except OSError:
                pass
            holder = self._read_holder()
            if holder is not None and holder.get("token") not in (None, self.token):
                return  # the file names another holder now: never remove theirs
            for attempt in range(100):
                try:
                    self.path.unlink()
                    return
                except FileNotFoundError:
                    return
                except OSError as exc:
                    if attempt == 99:
                        print(f"merge lock: cannot remove {self.path}: {exc!r}")
                        raise
                    time.sleep(0.05)


# -- git plumbing -----------------------------------------------------------------------------

def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """``env`` (when given) is merged over ``os.environ`` for this one subprocess."""
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", creationflags=no_window_flags(),
                          env={**os.environ, **env} if env else None)


# The env a push to the main branch carries: a repo's pre-push hook refuses a plain `git push`
# of main unless it sees this, so only the verified paths (run_merge, run_commit) publish.
MAIN_PUSH_ENV = {"AGENTOPS_MAIN_PUSH": "1"}


def _push_main(cwd: Path, remote: str, sha: str, main: str) -> subprocess.CompletedProcess:
    """The one push of a merged commit to the main branch (a seam: tests replace it)."""
    return _git(cwd, "push", remote, f"{sha}:refs/heads/{main}", env=MAIN_PUSH_ENV)


def _head(cwd: Path) -> str:
    result = _git(cwd, "rev-parse", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else ""


def _porcelain_paths(cwd: Path) -> set[str]:
    """Every path ``git status --porcelain`` reports as changed (staged or not)."""
    result = _git(cwd, "status", "--porcelain=v1", "--untracked-files=all")
    paths: set[str] = set()
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:]
        if " -> " in entry:  # a rename: both sides count
            old, new = entry.split(" -> ", 1)
            paths.add(old)
            paths.add(new)
        else:
            paths.add(entry)
    return paths


def _touched_by_branch(cwd: Path, base: str, branch: str) -> set[str]:
    """Files the branch changed since it diverged from ``base`` -- what the merge brings in."""
    result = _git(cwd, "diff", "--name-only", f"{base}...{branch}")
    if result.returncode != 0:
        return set()
    return {line for line in result.stdout.splitlines() if line}


#: The mod runner of a merge (#194): ``(name, root=...) -> exit code`` (0 = green); None = the settings key
#: ``mod_test_runner`` (``module:attr``); a repo with neither runs no mod tests.
MOD_TEST_RUNNER: Callable[[str, Path], int] | None = None
MOD_ID = "mod:"  # a red mod stands in the failed ids as ``mod:<name>``, so the red-batch triage reads it like a test id
_MOD_COUNTS_RE = re.compile(r":\s*(\d+) pass, (\d+) fail")
_NO_MOD_TESTS = "plugin test: no tests in this project"


def _mod_dirs(root: Path) -> list[str]:
    """Every folder of ``root/mods`` but ``shared``, read at call time in the checkout that is tested."""
    mods_dir = Path(root) / "mods"
    return sorted(p.name for p in mods_dir.iterdir() if p.is_dir() and p.name != "shared") if mods_dir.is_dir() else []


def _touched_mods(cwd: Path, base: str, target: str, root: Path) -> list[str]:
    """The mods whose folder the change ``base``..``target`` touches (``mods/shared/`` touches every mod)."""
    names = _mod_dirs(root)
    touched = _touched_by_branch(cwd, base, target)
    if any(path.startswith("mods/shared/") for path in touched):
        return names
    return [n for n in names if any(path.startswith(f"mods/{n}/") for path in touched)]


def _run_mod_tests(names: Sequence[str], root: Path, output_path: Path,
                   heartbeat: Callable[[], None]) -> tuple[list[str], list[str]]:
    """Run each mod's tests in ``root``; ``(["<name> <passed>/<failed>", ...], red names)``. A mod whose run
    printed no test part and came out green has no tests: it gets no entry and is not red."""
    runner = MOD_TEST_RUNNER or _settings_callable("mod_test_runner")
    if runner is None:
        return [], []
    summaries: list[str] = []
    red: list[str] = []
    for name in names:
        heartbeat()
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = runner(name, root=root)
        except Exception as exc:  # noqa: BLE001 - a runner that raised (timeout, no claude) is a red mod
            code = 1
            buf.write(f"{name}: {type(exc).__name__}: {str(exc)[:200]}\n")
        out = buf.getvalue()
        counts = _MOD_COUNTS_RE.findall(out)
        passed, failed = sum(int(p) for p, _f in counts), sum(int(f) for _p, f in counts)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "a", encoding="utf-8") as fh:
            fh.write(f"\nmod tests: {name} (exit {code})\n{out}")
        if code:
            red.append(name)
            failed = max(failed, 1)
        elif not counts and _NO_MOD_TESTS in out:
            continue
        summaries.append(f"{name} {passed}/{failed}")
    return summaries, red


def _core_branch_refusal(cwd: Path, base: str, target: str) -> str:
    """Issue 1116: the refusal line when a target's merge would change a core file outside a core
    update's own commit; "" when the merge may go on. Commits are read as (subject, paths)."""
    touched = _touched_by_branch(cwd, base, target)
    log = _git(cwd, "log", "--name-only", "--format=%x00%s", f"{base}..{target}")
    commits: list[tuple[str, list[str]]] = []
    if log.returncode == 0:
        for chunk in log.stdout.split("\x00")[1:]:
            lines = [line for line in chunk.splitlines() if line]
            if lines:
                commits.append((lines[0], lines[1:]))
    edited = manifest_edits(cwd, touched, commits)
    return refusal_message(edited) if edited else ""


def _changed_under(cwd: Path, pathspecs: Sequence[str]) -> list[str]:
    """The changed paths git itself selects for ``pathspecs`` (``git status -- <paths>``, read with -z):
    git resolves ``.``, ``./dir``, backslash forms and quoting, so no path form is matched by hand.
    A rename entry carries two paths (new, then old); both are returned."""
    result = _git(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *pathspecs)
    if result.returncode != 0:
        raise MergeError(f"git status failed for {list(pathspecs)}: {(result.stderr or '').strip()[:200]}")
    fields = result.stdout.split("\0")
    paths: set[str] = set()
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        paths.add(entry[3:])
        if entry[0] in "RC" or entry[1] in "RC":  # the next NUL field is the other side of the rename
            if index < len(fields) and fields[index]:
                paths.add(fields[index])
            index += 1
    return sorted(paths)


def _core_commit_refusal(cwd: Path, paths: Sequence[str]) -> str:
    """Issue 1116: the refusal line when an `agentops commit` of ``paths`` would change a core file.
    The changes it would commit are the ones git selects for ``paths`` (``_changed_under``). A commit
    made here is never a core update's own: that one is made by core_update with git."""
    changed = _changed_under(cwd, paths)
    edited = manifest_edits(cwd, changed, [("commit", changed)])
    return refusal_message(edited) if edited else ""


def _conflicted_files(cwd: Path) -> list[str]:
    result = _git(cwd, "diff", "--name-only", "--diff-filter=U")
    return sorted(line for line in result.stdout.splitlines() if line)


# -- restart a running suite with more workers when the PC frees up ----------------------

SUITE_PLAN_PLUGIN = "tools.agentops.suite_plan_plugin"
SUITE_PLAN_ENV = "SUITE_PLAN_FILE"
RESTART_POLL_SECONDS = 60
RESTART_MIN_PERCENT = 5
RESTART_MARGIN_SECONDS = 30
_LOG_FULL_RE = re.compile(r"\| suite (?:(\d+) h (\d+) min|([\d.]+) (s|min))(?:, (\d+) workers?)? \| 0 failed")
_WORKERS_FLAG_RE = re.compile(r"(\s)-n\s*\d+\b")


def decide_restart(k: int, k_new: int, elapsed: float, percent: int | None,
                   full_new: float | None, restarted: bool, share: float | None = None) -> tuple[bool, float | None]:
    """``(restart, rest_now_seconds)``: restart when the new count is at least twice the running one
    and a whole suite at the new count (``full_new`` s) plus 30 s beats the rest of the running
    suite (``elapsed x (100 - percent) / percent``). Under 5 % or percent unknown there is no
    estimate and little to lose: restart at once. A suite may restart again: ``restarted``
    no longer blocks; ``k`` is the current count, so each restart at least doubles it.
    With a ``share`` of work done (from the suite's plan file) ``share x 100`` plays the percent."""
    if share is not None:
        percent = share * 100
    if percent is None or percent < RESTART_MIN_PERCENT:
        return k_new >= 2 * k, None
    rest_now = elapsed * (100 - percent) / percent
    if full_new is None or k_new < 2 * k:
        return False, rest_now
    return full_new + RESTART_MARGIN_SECONDS < rest_now, rest_now


def last_full_suite_by_workers(log_path: Path) -> dict[int, float]:
    """Seconds of the last green full suite per worker count in ``merge.log`` (a line without a
    workers suffix is 1 worker)."""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    out: dict[int, float] = {}
    for line in lines:
        m = _LOG_FULL_RE.search(line)
        if not m or " | selected " in line:  # a selected run's time is not a full suite's
            continue
        secs = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 if m.group(1) is not None
                else float(m.group(3)) * (60 if m.group(4) == "min" else 1))
        if secs > 0:
            out[int(m.group(5) or 1)] = secs
    return out


def nearest_full_suite(by_workers: dict[int, float], k: int) -> float | None:
    """The time at the nearest known worker count (a tie goes to the larger count)."""
    if not by_workers:
        return None
    return by_workers[min(by_workers, key=lambda w: (abs(w - k), -w))]


def _kill_tree(pid: int) -> None:
    """Kill ``pid`` and its whole tree (``taskkill /T /F``); a pid already gone is fine."""
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30,
                   creationflags=no_window_flags())


def _workers_cmd(cmd: str, k: int) -> str:
    """``cmd`` with ``-n k``; k above 1 carries exactly one ``--maxschedchunk=1``, k = 1 neither flag."""
    cmd = _CHUNK_FLAG_RE.sub("", cmd)
    if k <= 1:
        return _WORKERS_FLAG_RE.sub("", cmd, count=1)
    if _WORKERS_FLAG_RE.search(cmd):
        cmd = _WORKERS_FLAG_RE.sub(lambda m: f"{m.group(1)}-n {k}", cmd, count=1)
    else:
        cmd = f"{cmd} -n {k}"
    return f"{cmd} {_CHUNK_FLAG}"


class SuiteRestart:
    """Watches one running suite every ``interval`` s (a thread) and, once, asks to restart it
    with more workers (again later). All inputs are callables so a test drives it with fixed readings."""

    def __init__(self, k: int, probe_k: Callable[[], int], full_for: Callable[[int], float | None],
                 interval: float = RESTART_POLL_SECONDS, clock: Callable[[], float] = time.monotonic,
                 kill: Callable[[int], object] | None = None, out_path: Path | str | None = None):
        self.out_path = out_path   # the suite's out path; its `.plan.json` gives the share of work
        self.k, self.probe_k, self.full_for = k, probe_k, full_for
        self.interval, self.clock, self.kill = interval, clock, kill
        self.pending: int | None = None   # k_new once a restart was decided
        self.note = ""
        self.restarted = False
        self.log: Callable[[str], None] | None = None  # one line per poll
        self._start = clock()
        self._stop = None

    def begin(self) -> None:
        self._start = self.clock()

    def tick(self, text: str) -> tuple[bool, int, str]:
        """One check on the output so far: ``(restart, k_new, note)``."""
        from .merge_status import _progress
        from .suite_plan import finished_count, load_plan, work_share
        percent, _ = _progress(text)
        plan = load_plan(self.out_path)
        share = work_share(plan, finished_count(text)) if plan is not None else None
        k_new = self.probe_k()
        eff = percent if share is None else share * 100
        full_new = self.full_for(k_new) if eff is not None and eff >= RESTART_MIN_PERCENT else None
        go, rest = decide_restart(self.k, k_new, self.clock() - self._start, percent, full_new, self.restarted, share)
        note = ""
        if self.log is not None:
            self.log(self._poll_line(k_new, percent if share is None else share * 100, go, rest, full_new))
        if go:
            note = (f"restarted: workers {self.k} -> {k_new} "
                    + ("(percent under 5 or unknown: little to lose)" if rest is None else
                       f"(rest ~{human_duration(rest)} vs full ~{human_duration(full_new)})"))
        return go, k_new, note

    def _poll_line(self, k_new: int, percent: int | None, go: bool, rest: float | None,
                   full_new: float | None) -> str:
        """The reading of one poll: probe workers, percent done, verdict with its reason."""
        pct = "n/a" if percent is None else f"{percent:.0f}%"
        head = f"restart poll: k_new {k_new}, k {self.k}, {pct} done"
        if go and rest is None:
            return (f"{head} -> restart (k_new {k_new} >= 2*{self.k}, percent under "
                    f"{RESTART_MIN_PERCENT} or unknown: little to lose)")
        if go:
            why = (f"k_new {k_new} >= 2*{self.k} and full ~{human_duration(full_new)} + "
                   f"{RESTART_MARGIN_SECONDS} s < rest ~{human_duration(rest)}")
            return f"{head} -> restart ({why})"
        if k_new < 2 * self.k:
            why = f"k_new {k_new} < 2*{self.k}"
        elif full_new is None:
            why = "no known full suite time"
        else:
            why = (f"full ~{human_duration(full_new)} + {RESTART_MARGIN_SECONDS} s >= "
                   f"rest ~{human_duration(rest)}")
        return f"{head} -> keep ({why})"

    def watch(self, proc, get_text: Callable[[], str], stop) -> None:
        """Thread body: ``stop`` is a ``threading.Event``; returns after a restart was decided."""
        while not stop.wait(self.interval):
            if proc.poll() is not None:
                return
            try:
                go, k_new, note = self.tick(get_text())
            except Exception:  # noqa: BLE001 -- a failed reading never hurts the suite
                continue
            if go:
                self.pending, self.note, self.restarted = k_new, note, True
                (self.kill or _kill_tree)(proc.pid)
                return


# -- a run in which no test finishes ends with one reason line ---------------------------------------

STALL_FACTOR = 3          # a test may take this many times the plan's longest before the run counts as stalled
STALL_MIN_SHARE = 0.05    # the least share of a core a window is credited with (a hung test uses none)
STALL_POLL_SECONDS = 10
STALL_MIN_CPU_SECONDS = 30.0        # #63: a silent quiet window is "stuck" only if the whole tree gained less CPU than this
STALL_CEILING_SECONDS = 4 * 3600.0  # #63: a suite with no merge_suite_total_limit is ended after this, whatever its CPU


class TreeCpu:
    """Cumulative CPU seconds of a process and everything below it, read from psutil. A process that
    exited keeps what it consumed (per pid the growth is added, never the level), so the sum only rises."""

    def __init__(self, pid: int):
        self.pid, self.total, self._last = pid, 0.0, {}

    def __call__(self) -> float:
        import psutil
        try:
            root = psutil.Process(self.pid)
            procs = [root, *root.children(recursive=True)]
        except psutil.Error:
            return self.total
        for p in procs:
            try:
                t = p.cpu_times()
            except psutil.Error:
                continue
            level = t.user + t.system
            self.total += max(0.0, level - self._last.get(p.pid, 0.0))
            self._last[p.pid] = level
        return self.total


class MachineCpu:
    """The machine's CPU % since the previous call (psutil cpu_times: busy share of all cores); None when unreadable.
    The first call only sets the mark and returns None."""

    def __init__(self):
        self._last: tuple[float, float] | None = None

    def __call__(self) -> float | None:
        try:
            import psutil
            t = psutil.cpu_times()
            total, idle = sum(t), t.idle
        except Exception:  # noqa: BLE001 -- a failed reading never hurts the suite
            return None
        last, self._last = self._last, (total, idle)
        if last is None or total <= last[0]:
            return None
        return max(0.0, min(100.0, 100.0 * (1.0 - (idle - last[1]) / (total - last[0]))))


class StallWatch:
    """Ends a run in which no test finishes (no new progress character) for longer than a bound derived
    from the run itself, never a count of minutes: ``bound = STALL_FACTOR x the plan's longest test /
    share``, where ``share`` is the part of one core the child's tree got over the window since the last
    new dot (CPU time delta over wall time, held between ``STALL_MIN_SHARE`` and 1). A starved run has a
    small share and so a long bound; the same rule says a window is a stall when the tree has burnt
    ``STALL_FACTOR x longest`` CPU seconds without one new dot, or (a hung test burning nothing) when the
    wall time reached ``longest x STALL_FACTOR / STALL_MIN_SHARE``. No plan file yet means no bound.
    No window before the first progress character (start-up) nor after pytest's summary line (exit);
    the quiet and total limits cover those phases. The quiet window (progress unchanged for ``quiet_limit``) is a
    stall only if the whole tree gained less than ``STALL_MIN_CPU_SECONDS`` of CPU in it (#63): a silent suite that
    keeps using CPU is slow, so the window restarts and one ``suite slow:`` line goes to ``log``.
    A run with no total limit ends after ``STALL_CEILING_SECONDS`` whatever its CPU. ``reason`` is the one line a stalled run leaves."""

    def __init__(self, out_path: Path | str | None, interval: float | None = None, factor: float | None = None,
                 min_share: float | None = None, clock: Callable[[], float] = time.monotonic,
                 cpu_for: Callable[[int], Callable[[], float]] = TreeCpu,
                 kill: Callable[[int], object] | None = None, expected: int | None = None,
                 total_limit: float | None = None, quiet_limit: float | None = None,
                 machine_cpu: Callable[[], float | None] | None = None,
                 cut_check: Callable[[], str] | None = None):
        self.out_path, self.clock, self.cpu_for, self.kill = out_path, clock, cpu_for, kill
        self.cut_check = cut_check  # #122: answers the ticket a Priority merge cut this suite for, or ""
        self.cut = ""  # set when the suite was ended by a cut request (a cut is no stall: ``reason`` stays empty)
        self.machine_cpu = MachineCpu() if machine_cpu is None else machine_cpu  # machine CPU % since its previous call
        self.log: Callable[[str], object] | None = None  # where the once-per-window slow line goes (the suite's out file); set by the runner
        self.expected = expected  # tests the run selected: a plan with fewer numbers is no bound
        self.pids: list[int] = []
        self.total_limit = total_limit  # wall seconds of the whole run, plan or no plan; None = no limit
        self._start: float | None = None
        self.quiet_limit = quiet_limit  # seconds the pytest progress (percent, progress characters) may stand still
        self._q_sig: tuple | None = None
        self._q_t = 0.0
        self._q_cpu = 0.0  # tree CPU at the window's start (a window restart or a progress change)
        self.interval = STALL_POLL_SECONDS if interval is None else interval
        self.factor = STALL_FACTOR if factor is None else factor
        self.min_share = STALL_MIN_SHARE if min_share is None else min_share
        self.reason = ""
        self.begin()

    def begin(self) -> None:
        """A new child starts: the window opens at zero dots and zero CPU."""
        self._count, self._t0, self._c0 = 0, self.clock(), 0.0
        if self._start is None:
            self._start = self._t0
        self._plan_ready = self._plan_complete()  # the window opens once the plan is complete too
        self._q_t = self._t0  # a restarted child gets a whole quiet window; its CPU counter starts at zero
        self._q_cpu = 0.0
        self.machine_cpu()  # the machine-CPU window opens with the child

    def _plan_complete(self) -> bool:
        from .suite_plan import load_plan
        plan = load_plan(self.out_path)
        return plan is not None and not (self.expected and len(plan) < self.expected)

    def tick(self, text: str, cpu_now: float, now: float | None = None) -> str:
        """The reason line when the window is a stall, else ``""``."""
        from .suite_plan import finished_count, load_plan, summary_printed
        now = self.clock() if now is None else now
        if self.total_limit and self._start is not None and now - self._start >= self.total_limit:
            return (f"stalled: the suite run passed its total limit {human_duration(self.total_limit)} "
                    f"(merge_suite_total_limit); run ended")
        if not self.total_limit and self._start is not None and now - self._start >= STALL_CEILING_SECONDS:
            return (f"stalled: the suite run passed the ceiling {human_duration(STALL_CEILING_SECONDS)} "
                    f"(no merge_suite_total_limit set); run ended")
        count = finished_count(text)
        if self.quiet_limit:
            from .suite_plan import last_percent
            sig = (count, last_percent(text))
            if sig != self._q_sig:
                self._q_sig, self._q_t, self._q_cpu = sig, now, cpu_now
                self.machine_cpu()  # the machine-CPU window opens here
            elif now - self._q_t >= self.quiet_limit:
                gained, span = cpu_now - self._q_cpu, now - self._q_t
                line = self._cpu_text(gained, span)
                if gained < STALL_MIN_CPU_SECONDS:
                    return (f"stalled: the suite's pytest progress stood still for {human_duration(span)} "
                            f"(merge_suite_quiet_limit {human_duration(self.quiet_limit)}), {line}; run ended")
                self._q_t, self._q_cpu = now, cpu_now  # the window restarts first: a failing log is not repeated every tick
                if self.log is not None:  # `watch` catches a failing log like any failed reading
                    self.log(f"suite slow: no suite progress for {human_duration(span)}, {line}; the suite goes on")
        if not self._plan_ready and self._plan_complete():
            self._plan_ready, self._t0, self._c0 = True, now, cpu_now  # later of first dot and plan complete
        if count != self._count:
            self._count, self._t0, self._c0 = count, now, cpu_now
            return ""
        if count == 0 or summary_printed(text):  # no test is due before the first one finished or after the summary
            return ""
        plan = load_plan(self.out_path)
        wall = now - self._t0
        if plan is None or wall <= 0 or (self.expected and len(plan) < self.expected):
            return ""
        longest = max(plan)
        share = min(1.0, max(self.min_share, (cpu_now - self._c0) / wall))
        bound = self.factor * longest / share
        if wall < bound:
            return ""
        return (f"stalled: no test finished for {human_duration(wall)}; bound {human_duration(bound)} = "
                f"{self.factor:g} x longest test {human_duration(longest)} / {share:.2f} of a core the run got; "
                f"run ended")

    def _cpu_text(self, gained: float, span: float) -> str:
        """``42 s of CPU in 15 min, machine CPU 87 %`` (the machine part left out when the probe reads nothing)."""
        pct = self.machine_cpu()
        text = f"the tree gained {human_duration(gained)} of CPU in {human_duration(span)}"
        return text + (f", machine CPU {pct:.0f} %" if pct is not None else "")

    def watch(self, proc, get_text: Callable[[], str], stop) -> None:
        """Thread body: ends the child's tree when the window is a stall, then returns."""
        cpu = self.cpu_for(proc.pid)
        while not stop.wait(self.interval):
            if proc.poll() is not None:
                return
            if self.cut_check is not None:
                try:
                    cut = self.cut_check()
                except Exception:  # noqa: BLE001 -- a failed reading never hurts the suite
                    cut = ""
                if cut:
                    self.cut = cut
                    (self.kill or _kill_tree)(proc.pid)
                    return
            try:
                reason = self.tick(get_text(), cpu())
            except Exception:  # noqa: BLE001 -- a failed reading never hurts the suite
                continue
            if reason:
                self.reason = reason
                try:
                    import psutil
                    self.pids = [proc.pid, *(c.pid for c in psutil.Process(proc.pid).children(recursive=True))]
                except Exception:  # noqa: BLE001
                    self.pids = [proc.pid]
                (self.kill or _kill_tree)(proc.pid)
                return


def _default_restart(cwd: Path, head_line: str, append: bool,
                     out_path: Path | str | None = None) -> "SuiteRestart | None":
    """The restart watcher for a suite the budget sized (head line ``workers k:``)."""
    m = _WORKERS_LINE_RE.match((head_line or "").splitlines()[-1]) if head_line else None  # last line: the selection line comes first
    template = resolve_suite_workers_probe() if m and not append else None
    if template is None:
        return None
    with no_suite_reservation():  # the running suite already holds its claim; a restart reading is not a new one
        probe = _make_probe(template, cwd)
    return SuiteRestart(int(m.group(1)), lambda: suite_workers(probe)[0],
                        lambda kn: nearest_full_suite(last_full_suite_by_workers(_merge_log_path()), kn),
                        out_path=out_path)


_SELECTED_LINE_RE = re.compile(r"selected (\d+) of (\d+)")  # head line 1 of a selected suite


def _chunks(stream, decoder):
    """Decoded text pieces of a raw pipe as they arrive (a read returns what is there, no waiting for a newline)."""
    while True:
        data = stream.read(4096) if not hasattr(stream, "read1") else stream.read1(4096)
        if not data:
            tail = decoder.decode(b"", final=True)
            if tail:
                yield tail
            return
        text = decoder.decode(data)
        if text:
            yield text


def _settings_callable(key: str):
    """The callable the settings key names as ``module:attr`` (dispatch.py's ``start_admission`` style); None when
    the key is absent, unreadable, or its module is not in this installation (the core names no repo module)."""
    from .core_settings import load as load_settings
    try:
        spec = load_settings().get(key)
    except Exception:
        return None
    if not isinstance(spec, str) or ":" not in spec:
        return None
    module, _, name = spec.partition(":")
    try:
        return getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError):
        return None


def _local_suite_flags() -> int:
    """Extra ``creationflags`` this repo wants on a suite it starts (the settings key ``suite_priority_flags``, the
    full run's priority rule); 0 when absent."""
    try:
        flags = _settings_callable("suite_priority_flags")
        return flags() if flags is not None else 0
    except Exception:
        return 0


def _run_suite(suite_cmd: str, cwd: Path, *args, **kwargs) -> tuple[bool, str]:
    """``_run_suite_unreserved`` and, when it ends, the suite's claim on the shared worker reservation
    (``_with_workers`` made it,) is dropped."""
    try:
        return _run_suite_unreserved(suite_cmd, cwd, *args, **kwargs)
    finally:
        release_suite_workers(cwd)


def _run_suite_unreserved(suite_cmd: str, cwd: Path, env: dict[str, str] | None = None,
              output_path: Path | None = None, head_line: str = "",
              append: bool = False, restart: "SuiteRestart | None" = None,
              stall: "StallWatch | None" = None, failfast: "merge_failfast.FailFast | None" = None) -> tuple[bool, str]:
    """Run the suite, still showing its output to the caller live (as before); when
    ``output_path`` is given, the whole output is also kept there (``append`` adds to an
    existing file instead of replacing it). A suite sized by the budget may be restarted (repeatedly, each time at least
    doubling the workers) (``restart``); that is never a red result. A ``stall`` watch ends a run in which no
    test finishes for longer than its bound: the result is red and the output ends with its one reason line.
    A ``failfast`` (#219) re-runs each test that fails during the run alone and ends the run red at the first that
    fails again; the output then ends with its one note."""
    import threading
    run_env = {**nested_env(), **with_no_runlog(env), "PYTHONUNBUFFERED": "1"}  # the child writes each dot at once; #195: no outer build vars; #235: not logged
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    if restart is None:
        restart = _default_restart(cwd, head_line, append, output_path)

    def start(cmd: str):
        return subprocess.Popen(cmd, shell=True, cwd=str(cwd), env=run_env,
                                creationflags=no_window_flags() | _local_suite_flags(), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, bufsize=0)

    proc = start(suite_cmd)
    lines: list[str] = []
    # streamed: the file exists and grows while the suite runs (a waiter reads its mtime as progress)
    out_fh = open(output_path, "a" if append else "w", encoding="utf-8", errors="replace") if output_path is not None else None
    stop = threading.Event()
    out_lock = threading.Lock()
    if out_fh is not None and (restart is not None or stall is not None):
        def _poll_log(text: str) -> None:  # a watcher thread writes its reading into the suite's own output
            with out_lock:
                if not out_fh.closed:
                    out_fh.write(text + "\n")
                    out_fh.flush()
        if restart is not None:
            restart.log = _poll_log
        if stall is not None:
            stall.log = _poll_log  # #63: the once-per-window "slow" line, by the route the stalled reason takes
    try:
        if out_fh is not None and head_line:
            out_fh.write(head_line + "\n")
            out_fh.flush()
        while True:
            assert proc.stdout is not None
            watcher = None
            if restart is not None:
                restart.begin()
                watcher = threading.Thread(target=restart.watch, daemon=True,
                                           args=(proc, lambda: "".join(lines), stop))
                watcher.start()
            stall_watcher = None
            if stall is not None:
                stall.begin()
                stall_watcher = threading.Thread(target=stall.watch, daemon=True,
                                                 args=(proc, lambda: "".join(lines), stop))
                stall_watcher.start()
            ff_watcher = None
            if failfast is not None:
                ff_watcher = threading.Thread(target=failfast.watch, daemon=True, args=(proc, stop))
                ff_watcher.start()
            # chunks, not lines: a dot reaches the file and the poll when pytest writes it
            decoder = io.IncrementalNewlineDecoder(codecs.getincrementaldecoder("utf-8")(errors="replace"), translate=True)
            for line in _chunks(proc.stdout, decoder):
                print(line, end="", flush=True)
                lines.append(line)
                if out_fh is not None:
                    with out_lock:
                        out_fh.write(line)
                        out_fh.flush()
            proc.wait()
            if watcher is not None:  # the finished run's watcher is stopped before the next starts
                stop.set()
                watcher.join(timeout=5)
            if stall_watcher is not None:
                stop.set()
                stall_watcher.join(timeout=5)
            if ff_watcher is not None:  # a re-run still going is left to end alone: its verdict is dropped
                stop.set()
                ff_watcher.join(timeout=5)
            stop.clear()
            if restart is None or restart.pending is None or proc.returncode == 0:
                break
            k_new, restart.pending = restart.pending, None
            restart.k = k_new
            print(restart.note)
            if out_fh is not None:
                out_fh.write(restart.note + "\n")
                out_fh.flush()
            lines = []
            proc = start(_workers_cmd(suite_cmd, k_new))
        end_lines = [*(failfast.lines() if failfast is not None else []),
                     *([stall.reason] if stall is not None and stall.reason else [])]
        if end_lines:  # the lines a fail-fast or stalled run leaves (the stall reason last), in the file and the output
            seen = "".join(lines)
            line = ("\n" if seen and not seen.endswith("\n") else "") + "\n".join(end_lines) + "\n"
            lines.append(line)
            if out_fh is not None:
                out_fh.write(line)
                out_fh.flush()
    finally:
        stop.set()
        if out_fh is not None:
            out_fh.close()
    output = "".join(lines)
    if (stall is not None and stall.reason) or (failfast is not None and failfast.confirmed):
        return False, output
    sel = _SELECTED_LINE_RE.match(head_line)
    if proc.returncode == 5 and sel is not None:
        # pytest exit 5 = no test collected: a selected suite whose tests all fall under the suite's own -m
        note = f"selected {sel.group(1)} of {sel.group(2)}; all {sel.group(1)} deselected by the suite's -m: 0 run"
        print(note)
        output += note + "\n"
        if output_path is not None:
            with open(output_path, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(note + "\n")
        return True, output
    return proc.returncode == 0, output


def _recheck_cmd(suite_cmd: str, ids: list[str], args_file: Path | None = None) -> str:
    """The command that runs only ``ids``: ``<interpreter> -m pytest <ids> -q -p no:faulthandler``
    for a pytest suite, ``<suite_cmd> <ids>`` for a stub suite (it gets the ids as arguments).
    With ``args_file`` (one id per line) a pytest suite reads the ids from it as ``@file``, so no id passes
    through the shell: ``"``, spaces, ``[``, ``{`` and ``:`` in an id stay in it."""
    match = _PYTEST_RE.search(suite_cmd)
    if match is None:
        return f"{suite_cmd} " + " ".join(f'"{i}"' for i in ids)
    prefix = suite_cmd[:match.start()].rstrip()
    if prefix.endswith(" -m"):
        prefix = prefix[:-3].rstrip()
    head = f"{prefix} -m pytest" if prefix else "pytest"
    given = f'"@{args_file}"' if args_file is not None else " ".join(f'"{i}"' for i in ids)
    return f"{head} {given} -q -p no:faulthandler"


class RecheckInconclusive(Exception):
    """The re-run of failed ids ended red and named none of them: it could not tell. A caller reads this as
    "no ids to triage with" (everyone back in line), never as "every id still fails"."""


def _recheck_note(output: str, output_path: Path | None, note: str) -> str:
    print(note)
    if output_path is not None:
        with open(output_path, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(note + "\n")
    return output + note + "\n"


def _seen_failing(output: str, ids: list[str]) -> list[str]:
    """The ``ids`` pytest's own summary names on a ``FAILED``/``ERROR`` line (the id whole, then end of line or
    `` - message``); an output with no summary section (a stub suite) is read whole."""
    output = _strip_ansi(output)
    marker = output.rfind("short test summary info")
    if marker >= 0:
        output = output[marker:]
    named = [ln.split(" ", 1)[1].rstrip() for ln in output.splitlines()
             if ln.startswith(("FAILED ", "ERROR ")) and " " in ln]
    return [i for i in ids if any(n == i or n.startswith(i + " - ") for n in named)]


def _ids_not_found(found: str, ids: list[str], suite_dir: Path) -> list[str]:
    """The ``ids`` a pytest ``not found: <found>`` line stands for: the id whole when the text is one; else by
    path: pytest prints the file as an absolute path and cuts the text at ``[`` (a test function that is gone),
    so the ids with the same file and the same function name are meant."""
    exact = [i for i in ids if i == found]
    if exact:
        return exact

    def key(text: str) -> tuple[str, str]:
        path, _sep, rest = text.partition("::")
        return os.path.normcase(os.path.abspath(suite_dir / path)), rest.partition("[")[0]

    want = key(found)
    return [i for i in ids if key(i) == want]


def _recheck_failed(suite_cmd: str, ids: list[str], suite_dir: Path, run_env: dict[str, str] | None,
                    output_path: Path | None) -> tuple[list[str], str]:
    """Run the failed ``ids`` once in ``suite_dir`` (same env; output appended to ``output_path``).
    ``(still_failing_ids, output)``: the ids its own output names as failed. An id whose test file is not in
    ``suite_dir`` is not failing there and is dropped before the run. A red run that names none of the ids
    raises ``RecheckInconclusive``: it could not tell, which is not "failing"."""
    here = [i for i in ids if (suite_dir / i.split("::", 1)[0]).exists()]
    output = ""
    if len(here) < len(ids):
        output = _recheck_note(output, output_path,
                               f"re-check: {len(ids) - len(here)} of {len(ids)} failing ids are in test files "
                               f"absent from this tree: dropped from the failing ids")
    if not here:
        return [], output
    args_file = None
    if _PYTEST_RE.search(suite_cmd) is not None:
        fd, name = tempfile.mkstemp(prefix="recheck-ids-", suffix=".args")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(here) + "\n")
        args_file = Path(name)
    try:
        ok, run_out = _run_suite(_recheck_cmd(suite_cmd, here, args_file), suite_dir, run_env, output_path, append=True)
    finally:
        if args_file is not None:
            args_file.unlink(missing_ok=True)
    output += run_out
    if ok:
        return [], output
    if not run_out.strip():
        raise RecheckInconclusive("the re-run ended red with no output")
    clean = _strip_ansi(run_out)
    dropped: list[str] = []
    for found in dict.fromkeys(f.rstrip() for f in _NOT_FOUND_RE.findall(clean)):
        dropped += [i for i in _ids_not_found(found, here, suite_dir) if i not in dropped]
    if dropped:
        output = _recheck_note(output, output_path,
                               f"re-check: pytest could not find {', '.join(dropped)}: dropped from the failing ids")
        rest = [i for i in here if i not in dropped]
        if not rest:
            return [], output
        still, more = _recheck_failed(suite_cmd, rest, suite_dir, run_env, output_path)
        return still, output + more
    still = _seen_failing(run_out, here)
    if not still:
        raise RecheckInconclusive("the re-run ended red and named none of the ids")
    return still, output


_machine_clock = time.monotonic  # #223: the wait's clock and sleep (a test injects its own)
_machine_sleep = time.sleep


def _machine_reading() -> "merge_load.LoadReading":
    """The machine now (the sampler's CPU over 60 s, the other pytest sessions); a reading that fails is an empty one
    (no CPU, no runs): a verdict is information, never a reason to break a merge."""
    try:
        return merge_load.read_load(os.getpid())
    except Exception:
        return merge_load.LoadReading(cpu_percent=None, runs=0, at=time.time())


def _start_machine_cpu() -> None:
    """#223: starts the 60 s CPU sampler. It reads the others only: the merge's own process tree (the suite, a re-run
    alone, a re-check) is taken out, or a red `-n` suite would stamp `load` on every flaky test."""
    if merge_load.MACHINE_CPU.own_busy is None:
        merge_load.MACHINE_CPU.own_busy = TreeCpu(os.getpid())
    merge_load.MACHINE_CPU.start()


def _failfast_rerun(suite_cmd: str, test: str, suite_dir: Path, env: dict[str, str] | None, stop,
                    readings: dict[str, tuple["merge_load.LoadReading", "merge_load.LoadReading"]],
                    ) -> tuple[bool, str]:
    """#219 + #223: ``_rerun_alone`` with the machine read when the failure is first seen and when the confirm ends
    (kept in ``readings`` only for a confirm that told; one that raised leaves none)."""
    first = _machine_reading()
    answer = _rerun_alone(suite_cmd, test, suite_dir, env, stop)
    readings[test] = (first, _machine_reading())
    return answer


def _failfast_verdicts(confirmed: "merge_failfast.Confirmed | None",
                       readings: dict[str, tuple["merge_load.LoadReading", "merge_load.LoadReading"]],
                       output: str, output_path: Path | None) -> tuple[dict[str, dict], str]:
    """#223: the verdict of the test the fail-fast confirmed; its confirm alone is the re-check (no third run).
    ``({test: record}, output with the verdict line)``; nothing confirmed or read: ``({}, output)``."""
    if confirmed is None or confirmed.test not in readings:
        return {}, output
    failure, confirm = readings[confirmed.test]
    rec = merge_load.record(confirmed.test, failure, confirm, merge_load.failfast_verdict(failure, confirm))
    return {confirmed.test: rec}, _recheck_note(output, output_path, merge_load.verdict_line(rec))


def _ids_not_run(ids: list[str], output: str, suite_dir: Path) -> set[str]:
    """The ``ids`` a re-check did not run: their test file is not in ``suite_dir``, or pytest printed ``not found``
    for them in ``output`` (the same two causes ``_recheck_failed`` drops them for)."""
    absent = {i for i in ids if not (suite_dir / i.split("::", 1)[0]).exists()}
    left = [i for i in ids if i not in absent]
    skipped = set(absent)
    for found in dict.fromkeys(f.rstrip() for f in _NOT_FOUND_RE.findall(_strip_ansi(output))):
        skipped.update(_ids_not_found(found, left, suite_dir))
    return skipped


def _fill_verdicts(verdicts: dict[str, dict], ids: list[str], reason: str,
                   reading: "merge_load.LoadReading | None",
                   readings: "dict[str, merge_load.LoadReading] | None" = None) -> None:
    """#223: every failed test has a word. An id no re-check judged (a stuck run, a PC-only or mod test, a re-check
    that could not tell) gets ``unclear`` with a ``reason`` (free text, no code reads it); a verdict already made
    stays. An id in ``readings`` (taken when its failure first showed) uses that reading, any other ``reading``."""
    early = readings or {}
    for test in ids:
        if test not in verdicts:
            verdicts[test] = merge_load.unjudged(test, reason, early.get(test, reading))


def _judged_recheck_failed(suite_cmd: str, ids: list[str], suite_dir: Path, run_env: dict[str, str] | None,
                           output_path: Path | None, failure: "merge_load.LoadReading | None" = None,
                           heartbeat: Callable[[], None] | None = None,
                           failure_readings: "dict[str, merge_load.LoadReading] | None" = None,
                           ) -> tuple[list[str], str, dict[str, dict], str]:
    """#223: ``_recheck_failed`` run once the machine is quiet (at most 10 min), each id with its verdict word.
    ``(still_failing_ids, output, verdicts, note)``; ``note`` is ``re-check under load`` when the wait hit the cap.
    ``failure`` is the reading taken when the ids were known (none: read now); ``failure_readings`` the one taken
    when each id's failure first showed in the suite (an id without one uses ``failure``). The verdict lines are printed and
    appended to the suite output. Raises ``RecheckInconclusive`` like ``_recheck_failed``."""
    outputs: list[str] = []
    skipped: set[str] = set()

    def recheck(got: list[str]) -> list[str]:
        still, out = _recheck_failed(suite_cmd, got, suite_dir, run_env, output_path)
        outputs.append(out)
        skipped.update(_ids_not_run(got, out, suite_dir))
        return still

    def sleep(seconds: float) -> None:
        if heartbeat is not None:
            heartbeat()  # the lock holder stays alive through a wait of up to 10 min
        _machine_sleep(seconds)

    _start_machine_cpu()
    try:
        still, verdicts, note = merge_load.judged_recheck(
            ids, recheck, read_failure=lambda: failure or _machine_reading(),
            wait_quiet=lambda: merge_load.wait_until_quiet(_machine_reading, _machine_clock, sleep),
            not_run=lambda: skipped, failure_readings=failure_readings)
    finally:
        merge_load.MACHINE_CPU.stop()
    output = outputs[0] if outputs else ""
    for rec in verdicts.values():
        output = _recheck_note(output, output_path, merge_load.verdict_line(rec))
    return still, output, verdicts, note


def _with_note(run: "SuiteRun", note: str) -> str:
    return f"{run.recheck_note}; {note}" if run.recheck_note and note else (note or run.recheck_note)


# -- where the merge suite runs ------------------------------------------------------------------

@dataclass
class SuiteRun:
    ok: bool
    output: str
    workers: int
    host: str = ""                 # "" = no host choice ran
    rechecked_done: bool = False   # the router already re-ran its candidates through _recheck_failed
    red_ids: list[str] = field(default_factory=list)   # the candidates it re-ran
    still: list[str] = field(default_factory=list)     # those that still fail
    recheck_note: str = ""         # shown after "re-check of N" in the log line
    stuck: str = ""                # the stall watch ended the run; its reason, last suite line and pids
    skipped_note: str = ""         # a remote suite host's "skipped N (pc last M)", shown after the host
    failed_tests: list[str] = field(default_factory=list)  # node ids that failed, from the live events file
    unfinished: list[str] = field(default_factory=list)    # started and not finished (one per worker)
    cut_for: str = ""              # #122: a Priority merge's hard cut ended the run; the issue it named ("#N" or "?")
    fail_fast: "merge_failfast.Confirmed | None" = None  # #219: a failed test failed again alone and ended the run
    fail_fast_notes: list[str] = field(default_factory=list)  # #219: what the fail-fast watch could not read or judge
    load_reading: "merge_load.LoadReading | None" = None  # #223: the machine when the suite ended (the failed ids are known)
    failure_readings: "dict[str, merge_load.LoadReading]" = field(default_factory=dict)  # #223: {id: the machine when its failure first showed}
    verdicts: dict[str, dict] = field(default_factory=dict)  # #223: {test id: merge_load.record(...)} of the main paths
    place: str = ""                # #232: where the suite ran (see MergeResult.suite_place); "" = the PC, no GitHub asked
    machines: int = 0              # #232: GitHub machines held
    part_seconds: list[int] = field(default_factory=list)
    minutes: int = 0               # #232: GitHub minutes used


def _suite_router():
    """The installation's optional router, the callable the settings key ``suite_router`` names
    (``route(...) -> SuiteRun | None``); None when absent."""
    return _settings_callable("suite_router")


def resolve_suite_quiet_limit() -> float | None:
    """The settings file's ``merge_suite_quiet_limit`` (seconds); None when absent or unreadable."""
    try:
        from .core_settings import load as load_settings
        value = load_settings().get("merge_suite_quiet_limit")
    except Exception:
        return None
    return float(value) if isinstance(value, (int, float)) and value > 0 else None


def resolve_suite_total_limit() -> float | None:
    """The settings file's ``merge_suite_total_limit`` (seconds); None when absent or unreadable."""
    try:
        from .core_settings import load as load_settings
        value = load_settings().get("merge_suite_total_limit")
    except Exception:
        return None
    return float(value) if isinstance(value, (int, float)) and value > 0 else None


FAILFAST_RERUN_SECONDS = 5 * 60.0  # #219: a re-run alone that takes longer is a hang: no verdict, its tree is ended


def _rerun_alone(suite_cmd: str, test: str, suite_dir: Path, env: dict[str, str] | None,
                 stop=None, limit: float | None = None) -> tuple[bool, str]:
    """#219: ``test`` run alone while the suite goes on, ``(passed_alone, its one-line failure)``. It touches neither
    the suite's output file nor its worker claim. Only a red run whose own summary names ``test`` counts as failing
    again. Raises ``merge_failfast.NoVerdict`` (the suite goes on, the reason is noted) when the run passes the time
    bound (``FAILFAST_RERUN_SECONDS``; its process tree is ended), when ``stop`` is set (the suite ended or was
    stopped; the tree is ended), or when it ended red without naming ``test``."""
    limit = FAILFAST_RERUN_SECONDS if limit is None else limit
    args_file = None
    if _PYTEST_RE.search(suite_cmd) is not None:
        fd, name = tempfile.mkstemp(prefix="failfast-id-", suffix=".args")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(test + "\n")
        args_file = Path(name)
    try:
        proc = subprocess.Popen(_recheck_cmd(suite_cmd, [test], args_file), shell=True, cwd=str(suite_dir),
                                env={**nested_env(), **(env or {}), "PYTHONUNBUFFERED": "1"},  # #273: as every nested run
                                creationflags=no_window_flags() | _local_suite_flags(),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + limit
        while True:
            try:
                raw, _ = proc.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                why = ("the suite ended" if stop is not None and stop.is_set()
                       else f"it ran past {human_duration(limit)}" if time.monotonic() >= deadline else "")
                if why:
                    _kill_tree(proc.pid)
                    proc.communicate()
                    raise merge_failfast.NoVerdict(f"the re-run alone was ended ({why}); its process tree was stopped")
    finally:
        if args_file is not None:
            args_file.unlink(missing_ok=True)
    out = raw.decode("utf-8", errors="replace") if raw else ""
    if proc.returncode == 0:
        return True, ""
    if not _seen_failing(out, [test]):
        raise merge_failfast.NoVerdict(f"the re-run alone ended red (exit {proc.returncode}) without naming the test")
    clean = _strip_ansi(out)
    line = next((ln for ln in clean.splitlines() if ln.startswith("FAILED ") and test in ln), "")
    return False, line.partition(" - ")[2].strip() or line.strip()


def _run_merge_suite(run_cmd: str, suite_cmd: str, workers: int, head_line: str, suite_dir: Path,
                     run_env: dict[str, str] | None, output_path: Path, *, routed_ok: bool = True,
                     root: Path | None = None, cut_check: Callable[[], str] | None = None) -> SuiteRun:
    """The real merge suite. A suite sized by the budget (``workers k:`` head line) may be routed
    elsewhere by the optional router; ``None`` or no router = today's run in ``suite_dir``.
    ``cut_check`` (#122) answers a Priority merge's cut request while the local suite runs; a suite the router
    runs elsewhere is not cut (a limit of the hard push)."""
    alone_env = run_env  # #219: a test re-run alone is not a suite run: no plan, no events of its own
    plugin_file = Path(suite_dir) / (SUITE_PLAN_PLUGIN.replace(".", "/") + ".py")
    if _is_pytest_command(run_cmd) and plugin_file.is_file():  # the suite writes its plan file next to its out; the command stays untouched
        run_env = {**(run_env or {}), SUITE_PLAN_ENV: str(plan_path(output_path))}
        addopts = run_env.get("PYTEST_ADDOPTS", "")  # #195: the process' own PYTEST_ADDOPTS never reaches a suite
        run_env["PYTEST_ADDOPTS"] = f"{addopts} -p {SUITE_PLAN_PLUGIN}".strip()
        from .test_select import map_dir  # the promoted map's stored durations, only when the file exists
        datafile = map_dir(root if root is not None else ROOT) / "testmondata"
        if datafile.is_file():
            run_env["SUITE_DURATIONS_FILE"] = str(datafile)
    events_file = Path(suite_dir) / (_suite_events.PLUGIN.replace(".", "/") + ".py")
    if _is_pytest_command(run_cmd) and events_file.is_file():  # failures with node ids, and who is mid-test when a run is ended
        run_env = {**(run_env or {}), _suite_events.ENV_EVENTS: str(_suite_events.events_path(output_path))}
        _suite_events.clear_events(output_path)
        addopts = run_env.get("PYTEST_ADDOPTS", "")  # #195: the process' own PYTEST_ADDOPTS never reaches a suite
        run_env["PYTEST_ADDOPTS"] = f"{addopts} -p {_suite_events.PLUGIN}".strip()
    route = _suite_router()
    if route is not None and routed_ok and head_line and _WORKERS_LINE_RE.match(head_line.splitlines()[-1]):
        routed = route(run_cmd, suite_cmd, workers, head_line, suite_dir, run_env, output_path)
        if routed is not None:
            routed.failed_tests, routed.unfinished = _suite_events.read_events(output_path)
            return routed
    sel = _SELECTED_LINE_RE.match(head_line.splitlines()[0]) if head_line else None
    stall = StallWatch(output_path, expected=int(sel.group(1)) if sel else None,
                      total_limit=resolve_suite_total_limit(), quiet_limit=resolve_suite_quiet_limit(),
                      cut_check=cut_check)
    failfast = None
    failure_readings: dict[str, merge_load.LoadReading] = {}  # #223: {id: the machine when its failure first showed}
    alone_readings: dict[str, tuple[merge_load.LoadReading, merge_load.LoadReading]] = {}  # #223: per confirmed test
    if _is_pytest_command(run_cmd) and events_file.is_file():  # the live failures with node ids come from the events plugin
        def failed_now() -> list[str]:
            """The fail-fast poll (every 2 s) also reads the machine for each id new in the list (no second thread)."""
            ids = _suite_events.read_events(output_path)[0]
            for test in ids:
                if test not in failure_readings:
                    failure_readings[test] = _machine_reading()
            return ids
        failfast = merge_failfast.FailFast(
            failed_now, lambda: _suite_events.read_reasons(output_path),
            lambda test, stop: _failfast_rerun(suite_cmd, test, Path(suite_dir), alone_env, stop, alone_readings),
            _kill_tree)
    _start_machine_cpu()  # #223: the others' CPU over the last 60 s, read when the failed tests are known
    try:
        ok, out = _run_suite(run_cmd, suite_dir, run_env, output_path, *((head_line,) if head_line else ()),
                             stall=stall, failfast=failfast)
        load_reading = None if ok else _machine_reading()
    finally:
        merge_load.MACHINE_CPU.stop()  # a re-check starts it again: its samples stay, so its first reading has a window
    verdicts: dict[str, dict] = {}
    if failfast is not None and failfast.confirmed:  # #223: the confirm alone was the re-check
        verdicts, out = _failfast_verdicts(failfast.confirmed, alone_readings, out, output_path)
    stuck = ""
    if stall.reason:
        before = out.rsplit(stall.reason, 1)[0].rstrip("\n")
        last = before.rsplit("\n", 1)[-1][-300:]
        stuck = f"{stall.reason}; last suite line: {last!r}; pids held: {', '.join(map(str, stall.pids))}"
    failed_tests, unfinished = _suite_events.read_events(output_path)
    if stuck and unfinished:
        stuck += f"; unfinished: {', '.join(unfinished)}"
    if stuck and failed_tests:
        stuck += f"; failed so far: {', '.join(failed_tests)}"
    return SuiteRun(ok, out, workers, stuck=stuck, failed_tests=failed_tests, unfinished=unfinished,
                    cut_for=stall.cut, fail_fast=failfast.confirmed if failfast is not None else None,
                    fail_fast_notes=list(failfast.notes) if failfast is not None else [], load_reading=load_reading,
                    verdicts=verdicts, failure_readings=dict(failure_readings))


_NOT_UPTODATE_RE = re.compile(r"error: (?:Entry|Your local changes to) '([^']+)'")


def _overwritten_files(stderr: str) -> list[str]:
    """The files named by git's own ``error: Your local changes to the following files
    would be overwritten by merge:`` (or ``... by checkout:``) -- one tab-indented path per
    line, no quotes, unlike ``_NOT_UPTODATE_RE``'s ``reset --keep`` message."""
    lines = stderr.splitlines()
    files: list[str] = []
    collecting = False
    for line in lines:
        if "would be overwritten by merge" in line or "would be overwritten by checkout" in line:
            collecting = True
            continue
        if collecting:
            if line.startswith("\t"):
                files.append(line.strip())
                continue
            break
    return files


def _is_shallow(cwd: Path) -> bool:
    """``true`` when ``cwd`` is a shallow checkout (``.git/shallow`` present) -- one
    ``rev-parse``, no fetch (a full checkout costs 0 extra fetches for this check)."""
    result = _git(cwd, "rev-parse", "--is-shallow-repository")
    return result.stdout.strip() == "true"


def _repair_shallow(cwd: Path, remote: str) -> tuple[bool, str]:
    """``git fetch --unshallow <remote>`` once, converting the checkout back to a
    complete history. Returns ``(ok, message)``; ``message`` is git's own first stderr
    line when the repair itself fails (e.g. the remote is unreachable)."""
    result = _git(cwd, "fetch", "--unshallow", remote)
    if result.returncode == 0:
        return True, ""
    return False, _first_line(result.stderr)


def _append_shallow_repair_log(branch: str, remote: str, log_path: Path) -> None:
    """One line noting the repair, ahead of the attempt's own line ``_append_merge_log``
    writes once the merge itself is done."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{now()} | {branch} | shallow-repaired | fetch --unshallow {remote}"
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _merge_in_progress(cwd: Path) -> bool:
    """``true`` only once ``git merge`` has actually left a ``MERGE_HEAD`` behind (a real
    conflict to resolve). A merge refused outright -- e.g. ``fatal: refusing to merge
    unrelated histories`` -- never reaches that state, so ``git merge --abort`` has
    nothing to undo."""
    result = _git(cwd, "rev-parse", "-q", "--verify", "MERGE_HEAD")
    return result.returncode == 0


def half_integration(cwd: Path) -> str | None:
    """#196: ``"merge"`` while ``cwd`` has a ``MERGE_HEAD``, ``"rebase"`` while its git dir has a
    ``rebase-merge`` / ``rebase-apply`` dir (resolved by ``git rev-parse --git-path``, so a worktree's
    ``.git`` file works), else ``None``. Read only; a failed git read raises ``OSError`` or
    ``subprocess.SubprocessError`` for the caller to turn into "no line"."""
    if _merge_in_progress(cwd):
        return "merge"
    for name in ("rebase-merge", "rebase-apply"):
        result = _git(cwd, "rev-parse", "--git-path", name)
        if result.returncode == 0 and (Path(cwd) / result.stdout.strip()).is_dir():
            return "rebase"
    return None


def _sync_up(cwd: Path, remote: str, main: str) -> tuple[bool, str, str]:
    """``fetch <remote>`` then ``merge --ff-only <remote>/<main>``, the pair ``run_merge`` and
    ``run_commit`` both need to bring a checkout up before touching it. Returns
    ``(ok, reason, message)``; on failure ``reason`` is ``"fetch-failed"`` or ``"sync-failed"``
    and ``message`` is one line: the file(s) a fast-forward would overwrite, ``"diverged: <a>
    ahead, <b> behind <remote>/<main>"`` when the checkout has its own unpushed commit(s), or
    git's own first stderr line otherwise."""
    fetch = _git(cwd, "fetch", remote)
    if fetch.returncode != 0:
        return False, "fetch-failed", _first_line(fetch.stderr)

    ff = _git(cwd, "merge", "--ff-only", f"{remote}/{main}")
    if ff.returncode == 0:
        return True, "", ""

    overwritten = _overwritten_files(ff.stderr)
    if overwritten:
        return False, "sync-failed", (
            f"local changes to {', '.join(overwritten)} would be overwritten by merge"
        )

    counts = _git(cwd, "rev-list", "--left-right", "--count", f"HEAD...{remote}/{main}")
    if counts.returncode == 0:
        parts = counts.stdout.split()
        if len(parts) == 2 and parts != ["0", "0"]:
            ahead, behind = parts
            return False, "sync-failed", f"diverged: {ahead} ahead, {behind} behind {remote}/{main}"

    return False, "sync-failed", _first_line(ff.stderr)


def _docs_only_since(cwd: Path, old: str, new: str) -> bool:
    """True when ``new`` is a strict descendant of ``old`` and every path any commit of
    ``old..new`` touched needs no suite (``commit_needs_suite``: every path matches ``suite_free_paths``).
    Anything else -- a rewritten history, no commits, a git error -- is False."""
    if not old or not new or old == new or not _is_ancestor(cwd, old, new):
        return False
    log = _git(cwd, "log", "-m", "--name-only", "--format=", f"{old}..{new}")
    if log.returncode != 0:
        return False
    paths = [line for line in log.stdout.splitlines() if line.strip()]
    return bool(paths) and not commit_needs_suite(paths)


def _rev(cwd: Path, ref: str) -> str:
    """The commit ``ref`` names, or ``""`` when it names none."""
    result = _git(cwd, "rev-parse", "-q", "--verify", f"{ref}^{{commit}}")
    return result.stdout.strip() if result.returncode == 0 else ""


def _queue_head(cwd: Path, branch: str, remote: str) -> str:
    """#219 rule 2: the head a run of ``branch`` would merge. The remote branch when it exists and is not an ancestor of
    the local one (the run fast-forwards to it), else the local sha; ``""`` when the branch names no commit."""
    local, remote_sha = _rev(cwd, branch), _rev(cwd, f"{remote}/{branch}")
    if remote_sha and (not local or (remote_sha != local and not _is_ancestor(cwd, remote_sha, local))):
        return remote_sha
    return local


def _is_ancestor(cwd: Path, older: str, newer: str) -> bool:
    return _git(cwd, "merge-base", "--is-ancestor", older, newer).returncode == 0


def _checked_out_elsewhere(cwd: Path, branch: str) -> bool:
    """``True`` when ``refs/heads/<branch>`` is checked out in any worktree of this repo."""
    result = _git(cwd, "worktree", "list", "--porcelain")
    return f"branch refs/heads/{branch}" in result.stdout.splitlines()


def _resolve_target(cwd: Path, branch: str, remote: str) -> tuple[str, str, str, str]:
    """What to merge for ``branch`` (after the fetch): ``(target, refusal_reason, message, note)``.
    ``target`` is the commit to merge; ``note`` is the fast-forward line to log ('' when none).
    A ``<remote>/<name>`` argument, or a name with only one side existing, is taken as is; a
    local branch behind its remote merges the remote's commit (the local ref is moved to it
    later, ``_advance_local``); a diverged pair is refused with both tips named."""
    if branch.startswith(f"{remote}/"):
        return branch, "", "", ""
    local, remote_tip = _rev(cwd, f"refs/heads/{branch}"), _rev(cwd, f"refs/remotes/{remote}/{branch}")
    if not remote_tip:
        return branch, "", "", ""
    if not local:
        return remote_tip, "", "", f"merging {remote}/{branch} {remote_tip[:12]} (no local branch)"
    if local == remote_tip or _is_ancestor(cwd, remote_tip, local):
        return local, "", "", ""
    if _is_ancestor(cwd, local, remote_tip):
        return remote_tip, "", "", f"fast-forwarded {branch} {local[:12]} -> {remote_tip[:12]} from {remote}/{branch}"
    return "", "diverged", (f"local {branch} {local[:12]} and {remote}/{branch} {remote_tip[:12]} "
                            f"have diverged"), ""


def _advance_local(cwd: Path, branch: str, remote: str, note: str) -> str:
    """Move ``refs/heads/<branch>`` to the remote tip it was found behind. Safest rule: a branch
    checked out in any worktree is never touched (its files would go stale under the moved
    ref), the remote's commit is merged and the note says so. Returns the final note."""
    if not note.startswith("fast-forwarded "):
        return note
    old, new = _rev(cwd, f"refs/heads/{branch}"), _rev(cwd, f"refs/remotes/{remote}/{branch}")
    if _checked_out_elsewhere(cwd, branch):
        return (f"merged {remote}/{branch} {new[:12]}; local {branch} {old[:12]} left in place "
                f"(checked out in a worktree)")
    moved = _git(cwd, "update-ref", f"refs/heads/{branch}", new, old)
    if moved.returncode != 0:
        return f"merged {remote}/{branch} {new[:12]}; local {branch} not moved ({_first_line(moved.stderr)})"
    return note


def _merge_tree_conflicts(stdout: str) -> list[str]:
    """Conflicted paths of ``git merge-tree --write-tree --name-only``: the lines after the tree
    id up to the first blank line."""
    out: list[str] = []
    for line in stdout.splitlines()[1:]:
        if not line.strip():
            break
        out.append(line.strip())
    return sorted(set(out))


def _rollback(cwd: Path, target: str, *, in_merge: bool) -> tuple[bool, list[str]]:
    """Undo back to ``target``, keeping every uncommitted change a merge never touched
    (``git reset --keep``, never ``--hard``). A merge left mid-conflict (``in_merge``)
    has no commit to reset away from yet: ``git reset --keep`` refuses outright while a
    merge is in progress, so the merge is aborted first (touches only the conflicted
    paths, confirmed on this checkout: an unrelated uncommitted file survives an abort).
    Returns ``(ok, files)``; ``files`` is the offending paths when ``--keep`` refuses."""

    if in_merge:
        abort = _git(cwd, "merge", "--abort")
        if abort.returncode != 0:
            return False, _NOT_UPTODATE_RE.findall(abort.stderr)

    result = _git(cwd, "reset", "--keep", target)
    if result.returncode == 0:
        return True, []
    return False, sorted(set(_NOT_UPTODATE_RE.findall(result.stderr)))


# -- the result ---------------------------------------------------------------------------------

@dataclass
class MergeResult:
    ok: bool
    reason: str
    message: str = ""
    conflicting_files: list[str] = field(default_factory=list)
    suite_runs: int = 0
    rebases: int = 0
    pushed: bool = False
    head_before: str = ""
    head_after: str = ""
    suite_duration: float = 0.0
    failed_count: int | None = None
    failed_tests: list[str] = field(default_factory=list)
    unfinished: list[str] = field(default_factory=list)  # ids a worker started and did not finish when a run was ended
    rechecked: list[str] = field(default_factory=list)  # ids that failed the suite, then passed the re-check
    suite_output_path: str = ""
    suite_dir: str = ""
    suite_workers: int = 0  # pytest-xdist workers the suite ran with; 0 = not decided (probe explicitly off)
    suite_host: str = ""  # the host the suite ran on when a router chose one; "" = not chosen
    recheck_note: str = ""
    skipped_note: str = ""
    selection: str = ""  # line 1 of the suite out (``selected N of M (map <8>)`` / ``full suite: <reason>``); "" = not asked
    batched: bool = False  # True: another holder's batch pushed this branch (it wrote the log line)
    batch_branches: list[str] = field(default_factory=list)  # holder only: the whole batch, holder first
    target_notes: list[str] = field(default_factory=list)  # #93: a target with no leak-words file, named; never a refusal
    mod_tests: list[str] = field(default_factory=list)  # #194: ``<name> <passed>/<failed>`` per mod the change touched
    monitor_note: str = ""  # #149: ``monitor installed`` / ``monitor install failed: <reason>`` of a landed merge; "" = nothing to install
    verdicts: dict[str, dict] = field(default_factory=dict)  # #223: {failed test id: merge_load.record(...)}; empty = none judged
    suite_place: str = ""  # #232: ``suite on GitHub: ...`` / ``suite on the PC (GitHub <reason>)``; "" = the PC as always
    suite_machines: int = 0  # GitHub machines the suite held; 0 = none
    suite_part_seconds: list[int] = field(default_factory=list)  # seconds each GitHub part took (machine file: raw seconds)
    suite_minutes: int = 0  # GitHub minutes the run used
    suite_pc_only: int | None = None  # #232, no longer set (#247: the PC-only tests run on the GitHub Windows job); old records carry it

    def render(self) -> str:
        if self.ok:
            return (f"merged: {self.message} (suite ran {self.suite_runs}x, "
                    f"{self.rebases} rebase(s), head {self.head_after[:12]})")
        files = f" [{', '.join(self.conflicting_files)}]" if self.conflicting_files else ""
        return f"not merged ({self.reason}): {self.message}{files}"


# -- the merge ----------------------------------------------------------------------------------

def worktree_refusal(cwd: Path) -> str | None:
    """One line when ``cwd`` is a linked worktree (its HEAD is the branch being merged, so the
    branch would be compared with itself and answer "already in main"); None for a main checkout."""
    cwd = Path(cwd)
    main_root = main_checkout_root(cwd)
    if main_root.resolve() == cwd.resolve():
        return None
    return f"merge: started in a worktree ({cwd}); start it from the main checkout {main_root}"


def _tracked_cache_count(cwd: Path, branch: str) -> int | None:
    """Files the branch's tree tracks under a ``__pycache__`` directory or named ``*.pyc`` (issue 54);
    None when git cannot read the ref, so the merge's own path reports that."""
    result = _git(cwd, "ls-tree", "-r", "--name-only", branch)
    if result.returncode != 0:
        return None
    return sum(1 for path in result.stdout.splitlines()
               if "__pycache__" in path.split("/") or path.endswith(".pyc"))


def run_merge(branch: str, suite_cmd: str, *, cwd: Path, lock_path: Path = DEFAULT_LOCK_PATH,
              remote: str = DEFAULT_REMOTE, main: str = DEFAULT_MAIN,
              lock_timeout: float | None = None,
              max_rebase_attempts: int = DEFAULT_MAX_REBASE_ATTEMPTS,
              suite_env: dict[str, str] | None = None,
              suite_output_dir: Path | None = None,
              merge_log_path: Path | None = None,
              suite_shared: Sequence[str] | None = None,
              issues: Sequence[int] = (), timing: dict | None = None) -> MergeResult:
    """Sync, merge ``branch`` into ``main``, run the suite, push -- one critical section,
    held by ``lock_path``. See the module docstring for the failure/rollback rules.

    ``suite_env`` is applied on top of this process' own environment for every run of
    ``suite_cmd`` -- including an explicit ``--suite`` -- so an installation's own settings
    (``merge_suite_env``) reach the suite whatever words its selection holds.

    Every attempt logs one line to ``merge_log_path``; the newest
    ``MAX_SUITE_OUTPUTS`` files under ``suite_output_dir`` are kept, older ones pruned before
    this attempt's own output (if any) is written. Both default to ``SUITE_OUTPUT_DIR`` /
    ``MERGE_LOG_PATH`` (the real installation's ``var/agentops/`` paths), looked up at call
    time so a test can monkeypatch the module constants instead of passing its own
    ``tmp_path``-scoped ones.

    The suite runs not in ``cwd`` but in the standing suite worktree (``ensure_suite_worktree``),
    brought to the merge commit, with its path first on ``PYTHONPATH``; ``suite_shared`` names the
    directories linked into it (default ``worktree.SHARED``). The merge itself stays in ``cwd``.
    A ``SuiteWorktreeError`` is a failed merge (reason ``suite-worktree``), rolled back like a red suite.

    A shallow ``cwd`` (``.git/shallow`` present -- one ``rev-parse``, no cost on a full
    checkout) is repaired first, once, with ``git fetch --unshallow <remote>``: on success
    an extra line notes the repair in ``merge_log_path`` and the merge goes on as normal; on
    failure (e.g. the remote is unreachable) nothing else runs and the reason is ``"shallow"``."""

    refusal = worktree_refusal(cwd)
    if refusal:
        return MergeResult(ok=False, reason="worktree", message=refusal)
    caches = _tracked_cache_count(cwd, branch)  # before the merge lock: a refused branch takes no line
    if caches:
        head = _head(cwd)
        return MergeResult(ok=False, reason="tracked-caches", head_before=head, head_after=head,
                           message=f"{branch} tracks {caches} cache file(s) (__pycache__ or *.pyc); "
                                   "git rm -r --cached them on the branch")
    if suite_output_dir is None:
        suite_output_dir = _suite_output_dir()
    if merge_log_path is None:
        merge_log_path = _merge_log_path()
    _prune_suite_outputs(keep=MAX_SUITE_OUTPUTS - 1, output_dir=suite_output_dir)
    result = _run_merge_locked(branch, suite_cmd, cwd=cwd, lock_path=lock_path, remote=remote,
                               main=main, lock_timeout=lock_timeout,
                               max_rebase_attempts=max_rebase_attempts, suite_env=suite_env,
                               suite_output_dir=suite_output_dir, merge_log_path=merge_log_path,
                               suite_shared=tuple(SHARED if suite_shared is None else suite_shared),
                               issues=tuple(issues), timing=timing)
    if result.batched:
        pass  # the holder wrote the batch line
    elif len(result.batch_branches) >= 2:
        _append_batch_log(result.batch_branches, result, merge_log_path)
    elif result.reason == "not-planned":  # the drop line names the issue, nothing else ran
        _append_plain_log(branch, f"not-planned | {result.message}", merge_log_path)
    else:
        _append_merge_log(branch, result, log_path=merge_log_path)
    return result


def _triage_red_batch(cwd: Path, base: str, branches: list[tuple[str, str]], suite_cmd: str,
                      ids: list[str], suite_dir: Path, run_env: dict[str, str] | None,
                      output_path: Path, suite_shared: Sequence[str],
                      lock: "MergeLock", failure: "merge_load.LoadReading | None" = None,
                      verdicts: dict[str, dict] | None = None,
                      failure_readings: "dict[str, merge_load.LoadReading] | None" = None) -> tuple[list[str], dict[str, list[str]]]:
    """Run only ``ids`` on ``base`` alone, then on ``base`` + each ``(name, target)`` alone.
    ``(ids failing on base, {name: ids failing on base + that branch})``; the per-branch
    checks are skipped when ``base`` itself is red.

    #223: each re-run waits for a quiet machine (at most 10 min) and judges every id with a verdict word
    (``failure``: the reading taken when the ids failed, ``failure_readings``: one per id; none: read at the first re-run). The records are
    added to ``verdicts`` when given (an id that fails again keeps its newest record) and the verdict lines are
    appended to the suite output."""
    def check(commit: str) -> list[str]:
        ensure_suite_worktree(cwd, commit, shared=suite_shared, lock=lock)
        lock.heartbeat()
        test_ids = [i for i in ids if not i.startswith(MOD_ID)]
        failing = []
        if test_ids:
            failing, _out, got, _note = _judged_recheck_failed(suite_cmd, test_ids, suite_dir, run_env, output_path,
                                                               failure, lock.heartbeat, failure_readings)
            if verdicts is not None:
                for test_id, rec in got.items():
                    if test_id in failing or test_id not in verdicts:
                        verdicts[test_id] = rec
        present = set(_mod_dirs(suite_dir))  # a mod this commit does not hold has no test to fail
        mods_to_run = [i[len(MOD_ID):] for i in ids if i.startswith(MOD_ID) and i[len(MOD_ID):] in present]
        if mods_to_run:
            failing += [MOD_ID + n for n in _run_mod_tests(mods_to_run, suite_dir, output_path, lock.heartbeat)[1]]
        return failing

    on_base = check(base)
    if on_base:
        return on_base, {}
    culprits: dict[str, list[str]] = {}
    for name, tgt in branches:
        tree = _git(cwd, "merge-tree", "--write-tree", "--name-only", base, tgt)
        if tree.returncode != 0:
            continue
        commit = _git(cwd, "commit-tree", tree.stdout.split()[0], "-p", base, "-p", tgt,
                      "-m", f"Triage {name}")
        if commit.returncode != 0:
            continue
        failing = check(commit.stdout.strip())
        if failing:
            culprits[name] = failing
    return [], culprits


class CheckoutLock(MergeLock):
    """The merge lock's file protocol for the second, short lock. A caller with no ticket info (a merge's
    own short steps, a commit that holds the merge lock too) goes through ``_take`` directly, so a count of
    ``MergeLock.acquire`` calls (the merge line's waits) sees only the merge line; a suite-free commit
    names itself in the ticket (``kind`` commit, ``needs_suite`` False) and goes the ordinary way."""

    holds_in_play = False
    takes_silent_holder = False

    def acquire(self, max_age_seconds: float | None = None, *, ticket_info: dict | None = None) -> dict | None:
        if ticket_info is not None:
            return super().acquire(max_age_seconds, ticket_info=ticket_info)
        return self._take(max_age_seconds, None)


def _checkout_lock_path(lock_path: Path) -> Path:
    return Path(lock_path).with_name(Path(lock_path).name + ".cwd")


@contextmanager
def _checkout_locked(lock_path: Path, lock_timeout: float | None = None):
    """The second lock, held for seconds, around every step that moves the checkout's own
    ``main`` (its fetch, fast-forward, commit, push). The merge lock stays held by a merge for its
    whole suite; a commit that needs no suite takes only this one, so it lands while a suite runs.
    Order everywhere: merge lock first, this one second, never the other way round."""
    lock = CheckoutLock(_checkout_lock_path(lock_path))
    lock.acquire(lock_timeout)
    try:
        yield
    finally:
        lock.release()


# -- the lock order (one definition) ------------------------------------------------
# Two locks, one order everywhere: the merge lock first, the checkout lock second, released in reverse.
# Callers never build that sequence themselves: a merge takes its place in the line through
# ``acquire_merge_line`` (its short checkout steps then go through ``_checkout_locked``), a commit or a core
# update holds both through ``merge_then_checkout_locked``, and the line check's seat file is guarded through
# ``seat_guard``.

def acquire_merge_line(lock_path: Path, lock_timeout: float | None, ticket_info: dict,
                       waiter_hook: "_LineCheckHook | None" = None,
                       arrival_ns: int | None = None) -> "tuple[MergeLock, dict | None]":
    """The merge's place in the line: ``MergeLock(lock_path).acquire`` with the line-check waiter hook (when
    there is one), the hook closed on every exit path. Returns ``(lock, posted)``: ``posted`` is ``None`` when
    the lock is now held (the caller releases it), or the dict a holder posted for this ticket (the lock is
    NOT held). ``arrival_ns`` (a re-queue after a cut, #122) keeps the old place in the line."""
    lock = MergeLock(lock_path)
    try:
        posted = lock.acquire(lock_timeout, ticket_info=ticket_info,
                              **({"waiter_hook": waiter_hook} if waiter_hook else {}),
                              **({"arrival_ns": arrival_ns} if arrival_ns else {}))
    finally:
        if waiter_hook is not None:
            waiter_hook.close()
    return lock, posted


@contextmanager
def merge_then_checkout_locked(lock_path: Path, lock_timeout: float | None = None, *,
                               merge_ticket_info: dict | None = None, hold_merge: bool = True,
                               checkout_ticket_info: dict | None = None, wait_s: float | None = None):
    """Holds the merge lock, then the checkout lock of the same path (``_checkout_lock_path``), and yields
    ``(merge_lock, checkout_lock)``; released in reverse. ``hold_merge=False`` (a suite-free commit) takes only
    the checkout lock, so it lands while a merge's suite runs; ``merge_lock`` is then ``None``. A checkout lock
    that cannot be taken releases the merge lock before the exception goes on. ``wait_s`` (#49) bounds the wait
    for each of the two locks: a lock still taken after it raises ``MergeLockBusy`` (None = wait as long as the
    holder lives and stays under the age ceiling)."""
    lock = MergeLock(lock_path)
    lock.wait_s = wait_s
    if hold_merge:
        lock.acquire(lock_timeout, ticket_info=merge_ticket_info)
    checkout_lock = CheckoutLock(_checkout_lock_path(lock_path))
    checkout_lock.wait_s = wait_s
    try:
        checkout_lock.acquire(lock_timeout, ticket_info=checkout_ticket_info)
    except BaseException:
        lock.release()
        raise
    try:
        yield (lock if hold_merge else None), checkout_lock
    finally:
        checkout_lock.release()
        lock.release()


def seat_guard(seat_path: Path, deadline: float):
    """The short OS-level guard (``MergeLock._guard``) of one side file, for a caller that reads or writes a
    seat file next to the lock (the line check's seat). A context manager; raises ``MergeLockTimeout`` or
    ``OSError`` like the lock's own guard."""
    return MergeLock(seat_path)._guard(deadline)


def _make_line_check_hook(cwd: Path, lock_path: Path, branch: str, suite_cmd: str, **kw) -> "_LineCheckHook | None":
    """The check of this branch while it waits behind a live holder; one place to switch it off."""
    return _LineCheckHook(cwd, lock_path, branch, suite_cmd, **kw)


class _LineCheckHook:
    """The waiter's hook for ``MergeLock``: while a live holder runs its suite, this waiter's own branch
    is checked alone (``LineCheck``, polled once per turn, non-blocking). A check that ends red after the
    re-run of its failed ids alone makes the waiter leave the line with ``red-on-branch`` (the dict is built
    like a ``MergeResult``, never ``batched``: the waiter writes its own log line). A claim by a holder, a
    result, the lock, a timeout or any exception ends the check (``close()``): an unfinished check never delays
    a branch. A waiter without a check (claimed before one started, or a refused target) still leaves a final
    ``stopped`` status, so a younger waiter's front-first rule does not wait for it."""

    def __init__(self, cwd: Path, lock_path: Path, branch: str, suite_cmd: str, *, remote: str, main: str,
                 suite_env: dict[str, str] | None, suite_shared: Sequence[str], suite_output_dir: Path,
                 priority: bool = False, merge_log_path: Path | None = None):
        self.priority = priority
        self.merge_log_path = merge_log_path   # #223: where a green re-check's verdict lines go
        self._green_logged = False
        self.cwd, self.lock_path, self.branch, self.suite_cmd = Path(cwd), Path(lock_path), branch, suite_cmd
        self.remote, self.main, self.suite_env = remote, main, dict(suite_env or {})
        self.suite_shared, self.suite_output_dir = tuple(suite_shared), suite_output_dir
        self.chk: LineCheck | None = None
        self.done = False
        self.red: dict | None = None
        self.hard: "merge_priority.HardWatch | None" = None  # #122: set by a Priority merge; asks to cut the batch ahead

    def _build(self, ticket: str) -> None:
        from . import core_settings, merge_github
        target, refusal, _message, _note = _resolve_target(self.cwd, self.branch, self.remote)
        cwd, suite_cmd = self.cwd, self.suite_cmd  # #254: the same settings and cwd the merge's own suite placement asks plan_for with
        self.chk = LineCheck(self.cwd, self.lock_path, ticket, self.branch, target or self.branch, self.suite_cmd,
                             suite_env=self.suite_env, suite_shared=self.suite_shared, remote=self.remote,
                             main=self.main, output_dir=self.suite_output_dir, priority=self.priority,
                             github_plan=lambda: ((None, "") if not _is_pytest_command(suite_cmd)  # as the merge: no pytest, no GitHub
                                             else merge_github.plan_for(core_settings.load(), cwd)))
        if refusal:  # no check for a refused target; a final status keeps the line's order moving
            self.chk.stop()
            self.done = True

    def __call__(self, ticket: str, holder_live: bool, claimed: bool) -> dict | None:
        if self.hard is not None:
            try:
                self.hard.poll(ticket)
            except Exception:  # noqa: BLE001 -- a failed label read never breaks the wait
                pass
        try:
            return self._turn(ticket, holder_live, claimed)
        except Exception:  # noqa: BLE001 -- a check never breaks the wait
            self.done = True
            self.close()
            return None

    def _turn(self, ticket: str, holder_live: bool, claimed: bool) -> dict | None:
        if self.done or machine_holds():  # in play no check is built, started or polled
            return None
        if claimed:  # a holder works for me: the check ends this turn, the branch goes as it always did
            if self.chk is None:
                self._build(ticket)
            if self.chk is not None:
                self.chk.stop()
            self.done = True
            return None
        if self.chk is None:
            if not holder_live:
                return None
            self._build(ticket)
            if self.done:
                return None
        outcome = self.chk.poll(holder_live)
        if outcome is not None and outcome.red:
            self.done = True
            ids = list(outcome.ids)
            self.red = asdict(MergeResult(
                ok=False, reason="red-on-branch", message=f"red-on-branch: {', '.join(ids)} (the output: {outcome.output_path})"
                        + (f"; {outcome.note}" if outcome.note else ""),
                suite_runs=0, failed_count=len(ids), failed_tests=ids, suite_output_path=outcome.output_path,
                verdicts=outcome.verdicts, recheck_note=outcome.note))
            return self.red
        if outcome is None and self.chk.state == "green" and self.chk.verdicts and not self._green_logged:
            self._green_logged = True  # #223: a flaky test judged by the green re-check is still told, once
            _append_verdict_log(self.chk.verdicts, self.merge_log_path or _merge_log_path(), self.chk.notes())
        return None

    def close(self) -> None:
        """The waiter has left (lock, result, red, timeout, error): no check child outlives it."""
        if self.chk is not None:
            try:
                self.chk.cleanup()
            except Exception:  # noqa: BLE001
                pass


#: #73 / #88: scan the targets' `_Avoid_` words over the merged `docs/agents/**/*.md` too, but only the words each target
#: lists in its own `checked_avoid_words` (target_check.checked_avoid_words); a target without that key scans none.
SCAN_AVOID_WORDS = True


def _home_targets(cwd: Path) -> list:
    """The settings' ``targets`` when ``cwd`` is a home (`update_rule: home`) that has any; [] otherwise."""
    try:
        settings = json.loads((Path(cwd) / "config" / "orchestration.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    targets = settings.get("targets") if isinstance(settings, dict) else None
    if not isinstance(settings, dict) or settings.get("update_rule") != "home" or not isinstance(targets, list):
        return []
    return targets


def _target_check_hits(cwd: Path, sha: str, tree: Path | None, suite_shared: Sequence[str], lock) -> list:
    """#73: the target-check hits (``TargetHit``) of the merge commit ``sha``; [] when none, or when this is not a
    home (`update_rule: home`) with `targets`. ``tree`` is a checkout of ``sha`` (None: the suite worktree is
    brought to ``sha`` first)."""
    targets = _home_targets(cwd)
    if not targets:
        return []
    if tree is None:
        tree = ensure_suite_worktree(cwd, sha, shared=suite_shared, lock=lock)
    from .target_check import check_targets
    return check_targets(tree, targets, Path(cwd), scan_avoid=SCAN_AVOID_WORDS)


def _target_check_refusal(cwd: Path, sha: str, tree: Path | None, suite_shared: Sequence[str], lock) -> str:
    """#73: the refusal line when a target's own checks reject the core text of the merge commit ``sha``;
    "" when none does, or when this is not a home (`update_rule: home`) with `targets`."""
    hits = _target_check_hits(cwd, sha, tree, suite_shared, lock)
    return f"refused: {hits[0].line()}" if hits else ""


def _target_hit_authors(cwd: Path, base: str, batch: Sequence[tuple[str, str]],
                        suite_shared: Sequence[str], lock) -> dict:
    """#95: ``{branch: first hit}`` of the batch members whose own diff adds a target-check hit. Each member
    (``batch`` is ``(branch, merge target)`` pairs) is merged alone onto ``base`` and checked; a hit ``base`` already
    holds is not the member's. A member that does not merge alone on ``base`` holds none."""
    base_hits = {(h.repo, h.path, h.line_no, h.word) for h in _target_check_hits(cwd, base, None, suite_shared, lock)}
    authors: dict = {}
    for name, ref in batch:
        solo = _git(cwd, "merge-tree", "--write-tree", base, ref)
        if solo.returncode != 0:
            continue
        commit = _git(cwd, "commit-tree", solo.stdout.split()[0], "-p", base, "-p", ref, "-m", f"Merge branch '{name}'")
        if commit.returncode != 0:
            continue
        new = [h for h in _target_check_hits(cwd, commit.stdout.strip(), None, suite_shared, lock)
               if (h.repo, h.path, h.line_no, h.word) not in base_hits]
        if new:
            authors[name] = new[0]
    return authors


def _archive_copy(cwd: Path, treeish: str, dest: Path) -> bool:
    """Write the files of ``treeish`` into ``dest`` (``git archive``: no worktree, no index, no lock)."""
    packed = subprocess.run(["git", "archive", "--format=zip", treeish], cwd=str(cwd), capture_output=True,
                            creationflags=no_window_flags())
    if packed.returncode != 0:
        return False
    import zipfile
    with zipfile.ZipFile(io.BytesIO(packed.stdout)) as archive:
        archive.extractall(dest)
    return True


def queue_target_refusals(branch: str, *, cwd: Path) -> list[str] | None:
    """#125, then #110: the lines refusing ``branch`` at queue time, in every repo (with or without `targets`):
    ``not merged (conflict): <files>`` when the branch conflicts with the freshly fetched ``origin/main``, and
    otherwise one line per target-check hit the branch itself adds (a home with `targets`). [] when it was checked and
    is clean; None when it was not checked (a git step failed, or the branch does not resolve): the merge itself, with
    its head-of-line check, then decides.
    The branch is merged onto the current ``origin/main`` in a temporary copy (no suite worktree, no merge lock); a hit
    main already holds is not the branch's (#95), and main's copy that cannot be built makes the answer None."""
    if _git(cwd, "fetch", DEFAULT_REMOTE).returncode != 0:
        return None
    target, reason, _message, _note = _resolve_target(cwd, branch, DEFAULT_REMOTE)
    base = _rev(cwd, f"{DEFAULT_REMOTE}/{DEFAULT_MAIN}")
    if reason or not base or not _rev(cwd, target):
        return None
    merged = _git(cwd, "merge-tree", "--write-tree", "--name-only", base, target)
    if merged.returncode == 1:  # #125: the conflict the head of the line would find, found now
        return [f"not merged (conflict): {', '.join(_merge_tree_conflicts(merged.stdout))}"]
    if merged.returncode != 0:
        return None
    targets = _home_targets(cwd)
    if not targets:
        return []
    from .target_check import check_targets
    with tempfile.TemporaryDirectory(prefix="queue-check-") as tmp:
        merged_dir, base_dir = Path(tmp) / "merged", Path(tmp) / "base"
        if not _archive_copy(cwd, merged.stdout.split()[0], merged_dir):
            return None
        hits = check_targets(merged_dir, targets, Path(cwd), scan_avoid=SCAN_AVOID_WORDS)
        if hits:
            if not _archive_copy(cwd, base, base_dir):
                return None
            held = {(h.repo, h.path, h.line_no, h.word)
                    for h in check_targets(base_dir, targets, Path(cwd), scan_avoid=SCAN_AVOID_WORDS)}
            hits = [h for h in hits if (h.repo, h.path, h.line_no, h.word) not in held]
    return [f"refused at queue: {h.line()} (branch {branch})" for h in hits]


def _target_check_notes(cwd: Path) -> list[str]:
    """#93: one note per target of a home whose folder has no leak-words file (``target_check.unchecked_targets``).
    A note never refuses the merge; [] when this is not a home with targets."""
    from .target_check import unchecked_targets
    targets = _home_targets(cwd)
    return unchecked_targets(targets, Path(cwd)) if targets else []


def _cut_checker(lock_path: Path) -> Callable[[], str]:
    """The holder's look at the cut request (#122 rule 5): the issue a standing request names, else ``""``."""
    max_age = _resolve_max_age_seconds(None)

    def check() -> str:
        req = merge_priority.pending_cut(lock_path, wall=time.time, max_age_s=max_age, pid_alive=pid_alive)
        return str(req.get("issue") or "?") if req else ""

    return check


def _run_merge_locked(branch: str, suite_cmd: str, *, cwd: Path, lock_path: Path,
                      remote: str, main: str, lock_timeout: float | None,
                      max_rebase_attempts: int,
                      suite_env: dict[str, str] | None,
                      suite_output_dir: Path,
                      merge_log_path: Path,
                      suite_shared: Sequence[str] = SHARED,
                      issues: Sequence[int] = (), timing: dict | None = None) -> MergeResult:
    """The merge's queue entry, around ``_merge_attempt``. A batch cut for a Priority ticket (``reason == "cut"``)
    goes back in line at its arrival place and runs again."""
    if not suite_cmd:
        raise MergeError("no suite command: pass --suite or a settings-provided one")

    ticket_info = {"kind": "merge", "branch": branch, "suite": suite_cmd, "remote": remote,
                   "main": main, "cwd": str(Path(cwd).resolve()), "issues": [int(i) for i in issues],
                   "result": os.environ.get(ENV_MERGE_RESULT, ""),
                   "head": _queue_head(cwd, branch, remote),  # #219 rule 2: what a newer head replaces
                   "priority": merge_priority.priority_for_issues(
                       issues, log=_priority_unread_log(branch, merge_log_path))}
    state: dict = {}
    while True:
        result = _merge_attempt(branch, suite_cmd, cwd=cwd, lock_path=lock_path, remote=remote, main=main,
                                lock_timeout=lock_timeout, max_rebase_attempts=max_rebase_attempts,
                                suite_env=suite_env, suite_output_dir=suite_output_dir,
                                merge_log_path=merge_log_path, suite_shared=suite_shared, issues=issues,
                                timing=timing, ticket_info=ticket_info, state=state)
        if result.reason != "cut":
            return result


def _merge_attempt(branch: str, suite_cmd: str, *, cwd: Path, lock_path: Path,
                   remote: str, main: str, lock_timeout: float | None,
                   max_rebase_attempts: int,
                   suite_env: dict[str, str] | None,
                   suite_output_dir: Path,
                   merge_log_path: Path,
                   suite_shared: Sequence[str],
                   issues: Sequence[int], timing: dict | None,
                   ticket_info: dict, state: dict) -> MergeResult:
    """One stay at the lock: queue, hold, merge, test, push. An attempt that a Priority ticket cut (reason ``cut``)
    leaves ``state["arrival_ns"]`` set: the next attempt queues at that place."""
    priority_ticket = merge_priority.rank_key(ticket_info.get("priority")) is not None
    hook = _make_line_check_hook(cwd, lock_path, branch, suite_cmd, remote=remote, main=main,
                                 suite_env=suite_env, suite_shared=suite_shared,
                                 suite_output_dir=suite_output_dir, priority=priority_ticket,
                                 merge_log_path=merge_log_path)
    watch = None
    if priority_ticket and hook is not None:  # rule 5: a hard-push on the issues cuts the batch ahead
        watch = merge_priority.HardWatch(
            lock_path, ticket_info["priority"], issues,
            log=lambda n, exc: _append_plain_log(
                branch, f"hard-push not read: #{n} ({type(exc).__name__}: {str(exc)[:200]})", merge_log_path))
        hook.hard = watch
    arrival = state.get("arrival_ns")
    try:
        lock, posted = acquire_merge_line(lock_path, lock_timeout, ticket_info, hook,
                                          **({"arrival_ns": arrival} if arrival else {}))
    finally:
        if watch is not None:
            watch.withdraw()
    state["arrival_ns"] = getattr(lock, "ticket_ns", 0) or state.get("arrival_ns")
    if timing is not None:
        timing["lock_at"] = time.monotonic()
    if posted is not None:
        known = {f.name for f in fields(MergeResult)}
        own_red = hook is not None and posted is hook.red  # this waiter's own check ended red: not a batch member
        return MergeResult(**{**{k: v for k, v in posted.items() if k in known}, "batched": not own_red})
    try:
        dropped = _dropped_for_not_planned(issues, branch, merge_log_path) if issues else None
        if dropped is not None:  # the lock is released by the finally below
            return dropped
        with _checkout_locked(lock_path):  # a suite-free commit may move the checkout's main meanwhile
            if _is_shallow(cwd):
                repaired, repair_message = _repair_shallow(cwd, remote)
                if not repaired:
                    head = _head(cwd)
                    return MergeResult(ok=False, reason="shallow",
                                       message=repair_message or f"git fetch --unshallow {remote} failed",
                                       head_before=head, head_after=head)
                _append_shallow_repair_log(branch, remote, merge_log_path)

            ok, reason, message = _sync_up(cwd, remote, main)
            if not ok:
                return MergeResult(ok=False, reason=reason, message=message)

            base = _head(cwd)
        suite_runs = 0
        rebases = 0
        retried = False

        target, refusal, refusal_message, note = _resolve_target(cwd, branch, remote)
        if refusal:
            return MergeResult(ok=False, reason=refusal, message=refusal_message,
                               head_before=base, head_after=base)
        if target and _is_ancestor(cwd, target, base):
            return MergeResult(ok=False, reason="nothing-to-merge",
                               message=f"nothing to merge: {branch} ({target[:12]}) is already in {main}",
                               head_before=base, head_after=base)
        target = target or branch
        core_refusal = _core_branch_refusal(cwd, base, target)
        if core_refusal:  # after the lock and the sync, before the line claim and the suite: main does not move
            return MergeResult(ok=False, reason="core-manifest", message=core_refusal,
                               head_before=base, head_after=base)

        green: dict | None = None  # a green suite's result, carried over docs-only re-merges
        recheck: dict | None = None  # #212: after a green suite and a code move of main, the ids the next run takes
        suite_spent = 0.0  # seconds of the suite runs a move of main has already made green
        claimed: list[Waiter] = []
        mine = {k: ticket_info[k] for k in ("suite", "remote", "main", "cwd")}
        lock.refresh_priorities(log_path=merge_log_path)  # #140: a label given while waiting counts at this pick
        polled = lock.waiting_line()
        line = [w for w in polled if all(w.info.get(k) == v for k, v in mine.items())]
        dropped = [w.info.get("branch", w.ticket) for w in polled if w not in line]
        line = merge_priority.batch_for(ticket_info.get("priority"), line)  # #122: a Priority holder takes Priority waiters only
        if lock.line_stop or dropped:  # silent when the walk simply reached the end of the queue
            _append_line_poll_log(branch, lock.line_stop, dropped, len(line), merge_log_path)
        if line:
            claimed = lock.claim(line)

        while True:
            ran_suite = False  # #212: set where this round runs a suite
            touched = _touched_by_branch(cwd, base, target)
            dirty = _porcelain_paths(cwd)
            overlap = sorted(touched & dirty)
            if overlap:
                return MergeResult(ok=False, reason="dirty", conflicting_files=overlap,
                                   message="uncommitted changes touch files this merge would change",
                                   suite_runs=suite_runs, rebases=rebases, head_before=base, head_after=base)

            # Build the merge commit without touching any checkout: local main and
            # origin/main stay at `base` until the suite is green and the exact commit is pushed.
            tree = _git(cwd, "merge-tree", "--write-tree", "--name-only", base, target)
            if tree.returncode == 1:
                conflicts = _merge_tree_conflicts(tree.stdout)
                reason = "rebase-conflict" if rebases else "conflict"
                message = ("origin/main moved and the re-merge conflicts: the later branch's worker must rebase"
                          if rebases else "merge conflict")
                return MergeResult(ok=False, reason=reason, conflicting_files=conflicts, message=message,
                                   suite_runs=suite_runs, rebases=rebases, head_before=base, head_after=base)
            if tree.returncode != 0:
                return MergeResult(ok=False, reason="merge-refused",
                                   message=_first_line(tree.stderr) or _first_line(tree.stdout),
                                   suite_runs=suite_runs, rebases=rebases,
                                   head_before=base, head_after=base)
            commit = _git(cwd, "commit-tree", tree.stdout.split()[0], "-p", base, "-p", target,
                          "-m", f"Merge branch '{branch}'")
            if commit.returncode != 0:
                return MergeResult(ok=False, reason="merge-refused", message=_first_line(commit.stderr),
                                   suite_runs=suite_runs, rebases=rebases, head_before=base, head_after=base)
            sha = commit.stdout.strip()

            # the claimed waiters, one more merge commit each, in line order
            members: list[tuple[Waiter, str, str]] = []  # (waiter, branch, note)
            for w in list(claimed):
                wb = w.info["branch"]
                dropped = _dropped_for_not_planned(w.info.get("issues") or (), wb, merge_log_path)
                if dropped is not None:  # a member closed as not planned leaves the batch with its own result
                    lock.post_result(w, asdict(dropped))
                    claimed.remove(w)
                    _append_plain_log(wb, f"not-planned | {dropped.message}", merge_log_path)
                    continue
                wt, wrefusal, _wmsg, wnote = _resolve_target(cwd, wb, remote)
                left = ""
                new_tree = ""
                if wrefusal:
                    left = wrefusal
                elif wt and (_is_ancestor(cwd, wt, base) or _is_ancestor(cwd, wt, sha)):
                    left = "nothing-to-merge"
                else:
                    wt = wt or wb
                    if sorted(_touched_by_branch(cwd, base, wt) & dirty):
                        left = "dirty"
                    else:
                        wtree = _git(cwd, "merge-tree", "--write-tree", "--name-only", sha, wt)
                        if wtree.returncode == 1:
                            left = "conflict"
                        elif wtree.returncode != 0:
                            left = "merge-refused"
                        else:
                            new_tree = wtree.stdout.split()[0]
                if not left:
                    wcommit = _git(cwd, "commit-tree", new_tree, "-p", sha, "-p", wt,
                                   "-m", f"Merge branch '{wb}'")
                    if wcommit.returncode != 0:
                        left = "merge-refused"
                if left:
                    lock.unclaim([w])
                    claimed.remove(w)
                    _append_left_in_line_log(wb, left, merge_log_path)
                    continue
                sha = wcommit.stdout.strip()
                members.append((w, wb, wnote))

            is_pytest = _is_pytest_command(suite_cmd)
            wait_while_held(lock.heartbeat)  # in play no suite starts; the holder waits for normal
            hold_while_safety_red(cwd, lock.heartbeat, merge_log_path, lock_path)  # #195: a red full run holds the line
            lock.heartbeat()
            if green is not None and [b for _w, b, _n in members] != green["members"]:
                green = None  # a waiter left the batch: the tested set changed, the suite runs
            if green is not None:
                # a docs-only commit moved main after a green suite: the merge was rebuilt on it,
                # the green result carries over (suite_runs does not grow)
                (suite_ok, suite_output, workers, suite_host_name, suite_note, run, rechecked_ids,
                 red_ids, still, suite_duration, output_path, suite_dir, tests_line, select_on, mod_summaries) = green["state"]
            else:
                suite_runs += 1
                ran_suite = True
                recheck_ids = recheck["ids"] if recheck and recheck["members"] == [b for _w, b, _n in members] else None
                recheck = None  # a changed batch (rule 5) or a used plan: the whole suite
                run_cmd = _augmented_suite_cmd(suite_cmd) if is_pytest else suite_cmd
                cut_check = None if priority_ticket else _cut_checker(lock_path)  # a Priority batch is never cut
                output_path = _new_suite_output_path(branch, suite_output_dir)
                lock.set_progress(output_path)
                try:
                    suite_dir = ensure_suite_worktree(cwd, sha, shared=suite_shared, lock=lock)
                except SuiteWorktreeError as exc:
                    return MergeResult(ok=False, reason="suite-worktree", message=_first_line(str(exc)),
                                       suite_runs=suite_runs, rebases=rebases, head_before=base, head_after=base)
                suite_t0 = time.monotonic()
                tail = (suite_env or {}).get("PYTHONPATH", os.environ.get("PYTHONPATH"))
                run_env = {**(suite_env or {}),
                           "PYTHONPATH": str(suite_dir) + (os.pathsep + tail if tail else "")}
                select_on = is_pytest and resolve_select_tests()
                tests_line, skip_suite = "", False
                placed_ids = None  # the ids a partial run is narrowed to
                recheck_cmd = (_selected_cmd(run_cmd, recheck_ids, output_path.with_name(output_path.name + ".args"))
                               if recheck_ids else None)
                if recheck_cmd is not None:  # #212: only the tests the move of main reaches
                    run_cmd, tests_line = recheck_cmd, f"re-check after main moved: {len(recheck_ids)} tests"
                    placed_ids = list(recheck_ids)
                elif select_on:
                    run_cmd, tests_line, skip_suite = _plan_selection(cwd, base, sha, run_cmd, output_path)
                    if tests_line.startswith("selected"):
                        placed_ids = _ids_in_cmd(run_cmd)
                if skip_suite:  # nothing the change can affect: no pytest (it exits 5 on no tests)
                    run = SuiteRun(True, "", 0)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_text(tests_line + "\n", encoding="utf-8")
                else:
                    def _suite_body(run_cmd=run_cmd, tests_line=tests_line):
                        return _run_sized_suite(run_cmd, tests_line, suite_cmd, is_pytest, suite_dir, run_env,
                                                output_path, cwd, cut_check)

                    run = _run_suite_placed(cwd, branch, sha, suite_dir, is_pytest, tests_line, priority_ticket,
                                            _suite_body, output_path, merge_log_path, lock, issues, ids=placed_ids,
                                            pc_only=lambda: _pc_only_files(suite_dir),
                                            run_readers=lambda rids: _run_ids_on_pc(suite_cmd, rids, suite_dir, run_env))
                if run.cut_for:  # rule 5: a Priority merge cut this batch; everyone goes back in line at their place
                    if members:
                        lock.unclaim([w for w, _b, _n in members])
                    merge_priority.clear_cut(lock_path)
                    result = MergeResult(ok=False, reason="cut", suite_runs=suite_runs, rebases=rebases,
                                         message=f"cut for a Priority ticket (#{run.cut_for}): back in line at its place",
                                         head_before=base, head_after=base,
                                         suite_duration=time.monotonic() - suite_t0, suite_workers=run.workers,
                                         suite_output_path=str(output_path), suite_dir=str(suite_dir),
                                         selection=tests_line)
                    _append_merge_log(branch, result, log_path=merge_log_path)
                    for name in [branch] + [b for _w, b, _n in members]:
                        _append_member_log(name, f"cut for a Priority ticket (#{run.cut_for}): back in line at its place",
                                           merge_log_path)
                    return result
                if run.stuck:  # the run went silent and was ended; the lock is freed, the next merge starts
                    if members:
                        lock.unclaim([w for w, _b, _n in members])
                    _fill_verdicts(run.verdicts, run.failed_tests, "stuck", run.load_reading, run.failure_readings)
                    result = MergeResult(ok=False, reason="stuck", message=run.stuck, suite_runs=suite_runs,
                                         rebases=rebases, head_before=base, head_after=base,
                                         suite_duration=time.monotonic() - suite_t0, suite_workers=run.workers,
                                         suite_output_path=str(output_path), suite_dir=str(suite_dir),
                                         selection=tests_line, failed_count=len(run.failed_tests),
                                         failed_tests=run.failed_tests, unfinished=run.unfinished,
                                         verdicts=run.verdicts)
                    return result
                suite_ok, suite_output, workers, suite_host_name, suite_note = run.ok, run.output, run.workers, run.host, run.recheck_note
                rechecked_ids: list[str] = []
                red_ids: list[str] = []
                still: list[str] = []
                if run.rechecked_done:
                    red_ids, still = run.red_ids, run.still
                    if suite_ok:
                        rechecked_ids = red_ids
                elif run.fail_fast:  # #219: confirmed alone while the suite ran; no third run (#223: the confirm gave run.verdicts)
                    red_ids = still = [run.fail_fast.test]
                elif not suite_ok:
                    red_ids = _failed_ids(suite_output)
                    if red_ids:
                        lock.heartbeat()
                        try:
                            still, recheck_output, run.verdicts, judged_note = _judged_recheck_failed(
                                suite_cmd, red_ids, suite_dir, run_env, output_path, run.load_reading, lock.heartbeat,
                                run.failure_readings)
                            suite_note = _with_note(run, judged_note)
                        except RecheckInconclusive:  # could not tell: the run stays red with no ids, never "still failing"
                            _fill_verdicts(run.verdicts, red_ids, "inconclusive", run.load_reading, run.failure_readings)
                            red_ids = []
                        else:
                            if still:
                                suite_output += recheck_output
                            else:
                                suite_ok, rechecked_ids = True, red_ids
                mod_summaries: list[str] = []
                if suite_ok:  # #194: after the pytest suite, the tests of every mod the change touches
                    mod_names = _touched_mods(cwd, base, sha, suite_dir)
                    if mod_names:
                        mod_summaries, mod_red = _run_mod_tests(mod_names, suite_dir, output_path, lock.heartbeat)
                        if mod_red:
                            suite_ok = False
                            red_ids = still = [MOD_ID + n for n in mod_red]
                            _fill_verdicts(run.verdicts, red_ids, "mod-test", run.load_reading, run.failure_readings)
                suite_duration = time.monotonic() - suite_t0
                if recheck_cmd is not None:  # the result names both runs
                    tests_line = (f"suite {human_duration(suite_spent)} + {tests_line}, "
                                  f"{human_duration(suite_duration)}")
            if not suite_ok and members:
                batch_names = [branch] + [b for _w, b, _n in members]
                if red_ids:
                    leg_failed = str(len(still))
                else:
                    leg_failed = str(_parse_pytest_failures(suite_output)[0]) if is_pytest else "?"
                _append_red_batch_log(batch_names, suite_duration, leg_failed, output_path, merge_log_path)
                if not red_ids:
                    # no test ids to triage with: everyone back in line, then the holder's branch alone
                    lock.unclaim([w for w, _b, _n in members])
                    claimed = []
                    continue
                if retried:
                    # the retry after triage is red too: final, nobody gets a third full suite
                    lock.unclaim([w for w, _b, _n in members])
                    for _w, wb, _n in members:
                        _append_member_log(wb, "re-queued after red batch", merge_log_path)
                    claimed = []
                    members = []
                else:
                    triage_verdicts: dict[str, dict] = {}  # #223: {id: record} of the triage re-runs
                    try:
                        base_failed, culprits = _triage_red_batch(
                            cwd, base, [(branch, target)] + [
                                (wb, _resolve_target(cwd, wb, remote)[0] or wb) for _w, wb, _n in members],
                            suite_cmd, still, suite_dir, run_env, output_path, suite_shared, lock,
                            failure=run.load_reading, verdicts=triage_verdicts,
                            failure_readings=run.failure_readings)
                    except RecheckInconclusive:
                        # a triage re-run could not tell: no culprit is named, everyone back in line
                        lock.unclaim([w for w, _b, _n in members])
                        claimed = []
                        continue
                    except SuiteWorktreeError as exc:
                        return MergeResult(ok=False, reason="suite-worktree", message=_first_line(str(exc)),
                                           suite_runs=suite_runs, rebases=rebases, head_before=base,
                                           head_after=base)
                    lock.unclaim([w for w, _b, _n in members])
                    if base_failed:
                        for _w, wb, _n in members:
                            _append_member_log(wb, "re-queued after red batch", merge_log_path)
                        _append_member_log(branch, f"red-on-base: {', '.join(base_failed)}", merge_log_path)
                        return MergeResult(ok=False, reason="red-on-base",
                                           message=f"red-on-base: {', '.join(base_failed)} (the output: {output_path})",
                                           suite_runs=suite_runs, rebases=rebases, head_before=base,
                                           head_after=base, suite_duration=suite_duration,
                                           failed_count=len(base_failed), failed_tests=base_failed,
                                           suite_output_path=str(output_path), suite_dir=str(suite_dir),
                                           verdicts={i: triage_verdicts[i] for i in base_failed if i in triage_verdicts})
                    innocent: list[Waiter] = []
                    for w, wb, _n in members:
                        if wb in culprits:
                            ids = ", ".join(culprits[wb])
                            lock.post_result(w, asdict(MergeResult(
                                ok=False, reason="red-suite", message=f"red-suite: {ids}", suite_runs=1,
                                head_before=base, head_after=base, suite_duration=suite_duration,
                                failed_count=len(culprits[wb]), failed_tests=culprits[wb],
                                suite_output_path=str(output_path), suite_dir=str(suite_dir), batched=True,
                                verdicts={i: triage_verdicts[i] for i in culprits[wb] if i in triage_verdicts})))
                            _append_member_log(wb, f"ejected: red-suite {ids}", merge_log_path)
                            _append_verdict_log({i: triage_verdicts[i] for i in culprits[wb] if i in triage_verdicts},
                                                merge_log_path)
                        else:
                            innocent.append(w)
                    if branch not in culprits and culprits:
                        # the holder is innocent: the innocent branches go once more as one batch
                        claimed = [w for w in innocent if lock.claim([w])]
                        retried = True
                        continue
                    for w in innocent:
                        _append_member_log(w.info["branch"], "re-queued after red batch", merge_log_path)
                    if branch in culprits:
                        still = culprits[branch]
                    claimed = []
                    members = []
            if not suite_ok:
                if is_pytest:
                    failed_count, failed_ids = _parse_pytest_failures(suite_output)
                else:
                    failed_count, failed_ids = None, []
                if red_ids:
                    failed_ids = still
                    failed_count = len(still)
                elif not failed_ids and run.failed_tests:  # the live events name what pytest's summary did not
                    failed_ids = run.failed_tests
                if failed_ids:
                    failed_count = len(failed_ids)
                _fill_verdicts(run.verdicts, failed_ids, "unjudged", run.load_reading, run.failure_readings)  # #223: no id without a word
                message = _red_with_fail_fast(run, failed_count, failed_ids, output_path)
                return MergeResult(ok=False, reason="red-suite", message=message,
                                   suite_runs=suite_runs, rebases=rebases, head_before=base, head_after=base,
                                   suite_duration=suite_duration, suite_workers=workers, suite_host=suite_host_name, recheck_note=suite_note, skipped_note=run.skipped_note, failed_count=failed_count, **_place_fields(run),
                                   failed_tests=failed_ids, unfinished=run.unfinished,
                                   suite_output_path=str(output_path),
                                   suite_dir=str(suite_dir), selection=tests_line, mod_tests=mod_summaries,
                                   verdicts=run.verdicts)

            try:
                target_hits = _target_check_hits(cwd, sha, suite_dir if green is None else None, suite_shared, lock)
                authors: dict = {}
                if target_hits and members:  # #95: whose diff adds the hit, one solo check per batch member
                    authors = _target_hit_authors(
                        cwd, base, [(branch, target)] + [(wb, _resolve_target(cwd, wb, remote)[0] or wb)
                                                         for _w, wb, _n in members], suite_shared, lock)
            except SuiteWorktreeError as exc:
                return MergeResult(ok=False, reason="suite-worktree", message=_first_line(str(exc)),
                                   suite_runs=suite_runs, rebases=rebases, head_before=base, head_after=base)
            target_notes = _target_check_notes(cwd)  # #93: a target without a leak-words file is named, not refused
            if target_hits and members and authors:
                # #95: eject the members that add a hit, then the rest are rebuilt, tested and checked again
                innocent = []
                for w, wb, _n in members:
                    if wb not in authors:
                        innocent.append(w)
                        continue
                    lock.post_result(w, asdict(MergeResult(
                        ok=False, reason="target-check", message=f"refused: {authors[wb].line()} (branch {wb})",
                        suite_runs=1, head_before=base, head_after=base, suite_duration=suite_duration,
                        suite_output_path=str(output_path), suite_dir=str(suite_dir), batched=True,
                        target_notes=target_notes)))
                    _append_member_log(wb, f"ejected: target-check {authors[wb].line()}", merge_log_path)
                if branch in authors:  # the holder's own branch is the guilty one: it is refused, the rest wait in line
                    lock.unclaim(innocent)
                    return MergeResult(ok=False, reason="target-check",
                                       message=f"refused: {authors[branch].line()} (branch {branch})",
                                       suite_runs=suite_runs, rebases=rebases, head_before=base, head_after=base,
                                       suite_duration=suite_duration, suite_workers=workers, suite_host=suite_host_name,
                                       suite_output_path=str(output_path), suite_dir=str(suite_dir),
                                       selection=tests_line, target_notes=target_notes)
                claimed = innocent
                continue
            if target_hits:  # #73: a target's own checks reject the merged core text; main does not move
                names = [branch] + [b for _w, b, _n in members]
                suffix = f" (branches {', '.join(names)})" if members else ""
                if members:
                    lock.unclaim([w for w, _b, _n in members])
                return MergeResult(ok=False, reason="target-check", message=f"refused: {target_hits[0].line()}{suffix}",
                                   suite_runs=suite_runs, rebases=rebases, head_before=base, head_after=base,
                                   suite_duration=suite_duration, suite_workers=workers, suite_host=suite_host_name,
                                   suite_output_path=str(output_path), suite_dir=str(suite_dir),
                                   selection=tests_line, target_notes=target_notes, **_place_fields(run))

            push_error = ""
            landed = None
            push_refusals = 0
            while True:  # #248: a refused push with origin/main unmoved is retried, same commit, no suite run
                push_error = ""
                attempted = False
                with _checkout_locked(lock_path):  # the push step is one at a time with a suite-free commit
                    if _head(cwd) == base:
                        push = _push_main(cwd, remote, sha, main)
                        attempted = True
                        if push.returncode == 0:
                            note = _advance_local(cwd, branch, remote, note)
                            ff = _git(cwd, "merge", "--ff-only", sha)
                            if ff.returncode != 0:
                                note = (note + "; " if note else "") + \
                                    f"local main not fast-forwarded: {_first_line(ff.stderr)}"
                            result = MergeResult(ok=True, reason="pushed",
                                                 message=f"{branch} -> {main}" + (f"; {note}" if note else ""),
                                                 suite_runs=suite_runs, rebases=rebases, pushed=True,
                                                 head_before=base, head_after=_head(cwd),
                                                 suite_duration=suite_duration, suite_workers=workers, suite_host=suite_host_name, recheck_note=suite_note, skipped_note=run.skipped_note, failed_count=0, rechecked=rechecked_ids, **_place_fields(run),
                                                 suite_output_path=str(output_path), suite_dir=str(suite_dir),
                                                 selection=tests_line, target_notes=target_notes,
                                                 mod_tests=mod_summaries, verdicts=run.verdicts)
                            posts: list[tuple[Waiter, MergeResult]] = []
                            if members:
                                result.batch_branches = [branch] + [b for _w, b, _n in members]
                                for w, wb, wnote in members:
                                    _advance_local(cwd, wb, remote, wnote)
                                    posts.append((w, MergeResult(
                                        ok=True, reason="pushed",
                                        message=f"{wb} -> {main} (batch of {len(result.batch_branches)} with {branch})",
                                        suite_runs=1, pushed=True, head_before=base, head_after=sha,
                                        suite_duration=suite_duration, suite_workers=workers, suite_host=suite_host_name, recheck_note=suite_note, skipped_note=run.skipped_note, failed_count=0,
                                        suite_output_path=str(output_path), suite_dir=str(suite_dir),
                                        selection=tests_line, batched=True, target_notes=target_notes)))
                            if select_on:  # the full suite runs on main after the landing
                                problem = _spawn_map_run(cwd, sha)
                                if problem:
                                    _log_map_run_spawn(branch, sha, problem, merge_log_path)
                            listing = _git(cwd, "diff", "--name-only", base, sha)
                            landed = (result, posts, listing.stdout.split() if listing.returncode == 0 else None,
                                      ff.returncode == 0)
                        else:
                            push_error = _first_line(push.stderr)
                if landed is not None or not attempted:
                    break
                push_refusals += 1
                _append_plain_log(branch, f"push refused: {push_error or 'no message'} | push {push_refusals} of {PUSH_ATTEMPTS}",
                                  merge_log_path)
                with _checkout_locked(lock_path):
                    synced, _r, _m = _sync_up(cwd, remote, main)
                    # a fetch that failed says nothing about main: the push is retried like an unmoved main
                    unmoved = (not synced) or _head(cwd) == base
                if not unmoved:
                    break  # main moved: the re-merge path below
                if push_refusals >= PUSH_ATTEMPTS:
                    if members:
                        lock.unclaim([w for w, _b, _n in members])
                    return MergeResult(ok=False, reason="push-refused", message=f"push refused: {push_error}",
                                       suite_runs=suite_runs, rebases=rebases, head_before=base, head_after=_head(cwd),
                                       suite_duration=suite_duration, suite_workers=workers, suite_host=suite_host_name,
                                       recheck_note=suite_note, skipped_note=run.skipped_note, failed_count=0,
                                       suite_output_path=str(output_path), suite_dir=str(suite_dir),
                                       selection=tests_line, target_notes=target_notes, **_place_fields(run))
                time.sleep(PUSH_RETRY_WAIT_S)
                lock.heartbeat()

            if landed is not None:  # #149: the monitor install runs outside the checkout lock; it can take minutes
                result, posts, paths, fast_forwarded = landed
                if paths is None:  # a failed listing must not silently skip the install
                    result.monitor_note = monitor_install.FAILED + "cannot list the landed files"
                elif fast_forwarded:
                    result.monitor_note = monitor_install.install_after(paths, cwd) or ""
                elif monitor_install.touches_monitor(paths, cwd):
                    result.monitor_note = monitor_install.FAILED + "local main not fast-forwarded"
                for w, posted_result in posts:
                    posted_result.monitor_note = result.monitor_note
                    lock.post_result(w, asdict(posted_result))
                return result

            # another writer moved local main during the suite, or origin refused: redo on the new head
            rebases += 1
            if rebases > max_rebase_attempts:
                return MergeResult(ok=False, reason="push-refused",
                                   message="too many re-merge attempts" + (f" ({push_error})" if push_error else ""),
                                   suite_runs=suite_runs, rebases=rebases, head_before=base, head_after=_head(cwd))

            with _checkout_locked(lock_path):
                ok, reason, message = _sync_up(cwd, remote, main)
                if not ok:
                    return MergeResult(ok=False, reason=reason, message=message,
                                       suite_runs=suite_runs, rebases=rebases, head_before=base, head_after=_head(cwd))

                new_base = _head(cwd)
            if ran_suite:
                suite_spent += suite_duration
            carried = {"members": [b for _w, b, _n in members],
                       "state": (suite_ok, suite_output, workers, suite_host_name, suite_note, run,
                                 rechecked_ids, red_ids, still, suite_duration, output_path, suite_dir,
                                 tests_line, select_on, mod_summaries)}
            if _docs_only_since(cwd, base, new_base):
                # a green suite is carried over: main moved only by commits that need no suite
                green = carried
            else:
                # #212: the green is thrown away; main's new commits name the tests to run, else the whole suite
                green = None
                ids, why = _plan_recheck(cwd, base, new_base, is_pytest)
                if ids is None:
                    verdict, detail = "thrown away", f"full: {why}"
                elif ids:
                    recheck = {"members": carried["members"], "ids": ids}
                    verdict, detail = "thrown away", f"re-check: {len(ids)} tests ({why})"
                else:
                    green = carried  # the new commits reach no test: the green stands
                    verdict, detail = "kept", f"re-check: 0 tests ({why})"
                _append_plain_log(branch, f"green suite {verdict}: main {base[:12]} -> {new_base[:12]} | {detail}",
                                  merge_log_path)
            base = new_base
    finally:
        lock.release()


# -- suite command resolution ---------------------------------------------------------------------

def resolve_suite_cmd(explicit: str | None) -> str:
    """``--suite`` wins; otherwise the settings file's own key, if any; otherwise refuse."""
    if explicit:
        return explicit
    try:
        from .core_settings import load as load_settings
        settings = load_settings()
    except Exception:
        settings = {}
    suite = settings.get("merge_suite")
    if not suite:
        raise MergeError("no suite command: pass --suite, or set 'merge_suite' in the settings file")
    return suite


def resolve_suite_env() -> dict[str, str]:
    """The settings file's own ``merge_suite_env`` (a dict of env vars), applied on top of this
    process' environment for every suite run -- ``{}`` when the key is absent or the settings
    file cannot be read."""
    try:
        from .core_settings import load as load_settings
        settings = load_settings()
    except Exception:
        return {}
    env = settings.get("merge_suite_env") or {}
    return dict(env) if isinstance(env, dict) else {}


def resolve_suite_shared() -> tuple[str, ...]:
    """The settings file's own ``merge_suite_shared_dirs`` (a list of directory names linked into the
    suite worktree) -- ``worktree.SHARED`` when the key is absent or the settings file cannot be read."""
    try:
        from .core_settings import load as load_settings
        settings = load_settings()
    except Exception:
        return tuple(SHARED)
    names = settings.get("merge_suite_shared_dirs")
    return tuple(names) if isinstance(names, list) else tuple(SHARED)


# -- committing named paths under the same lock ----------------------------------------------------

def _first_line(text: str) -> str:
    """The CLI's one-line style (``run_merge_command``'s own ``MergeLockTimeout`` print):
    a multi-line ``git`` stderr (e.g. the push-rejected hints) collapses to its first line."""
    stripped = text.strip()
    return stripped.splitlines()[0] if stripped else ""


# -- one `Updated:` line per writer in a State-style file -----------------------------------------

MAX_STATE_WRITERS = 6

# ``Updated: <text>`` (no writer -- the file's own shape today) or ``Updated (<writer>): <text>``.
# Found by the word, never by a line number.
_UPDATED_LINE_RE = re.compile(r"^Updated(?:\s*\(([^)]*)\))?:[ \t]?(.*)$")


_ENTRY_TIME_RE = re.compile(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})(?::\d{2})?")


def _entry_time(text: str) -> str:
    """The entry's own time as a sortable string: the first ``YYYY-MM-DD HH:MM`` anywhere in the
    text; an entry with no readable time is the oldest."""
    m = _ENTRY_TIME_RE.search(text)
    return f"{m.group(1)} {m.group(2)}" if m else ""


_ENTRY_HEADING_RE = re.compile(r"^Updated(?:\s*\([^)]*\))?:[ \t]?(.*)$", re.DOTALL)


def _strip_entry_heading(entry: str) -> str:
    """An entry that already starts with its own ``Updated (<writer>): `` heading loses it, so the
    rendered line carries exactly one."""
    m = _ENTRY_HEADING_RE.match(entry)
    return m.group(1) if m else entry


def _is_orch_writer(writer: str) -> bool:
    return writer.startswith("ORCH ")


def _render_state_line(writer: str, text: str) -> str:
    if writer:
        return f"Updated ({writer}): {text}"
    return f"Updated: {text}"


def _splice_state_entry(text: str, writer: str, entry: str) -> str:
    """Write ``entry`` as ``writer``'s ``Updated`` line into ``text`` (a whole file's contents).

    The block is every ``Updated``-style line above the first ``Last review`` line (every such
    line in the file when there is none), gaps included. It is rendered as one contiguous run at
    the place of the first old ``Updated`` line; the other old ``Updated`` lines and the blank
    lines between the first and last of them are removed; non-blank other lines between them
    stay, after the new block. It is replaced as a whole: ``writer``'s own
    old line is removed and the new one goes on top; if more than ``MAX_STATE_WRITERS`` lines
    remain, the others are cut: writers named ``ORCH <id>`` first, then any other writer only
    when no ORCH line is left to cut (the brain's hand-off line is never lost to a finished
    ORCH's update); within a group the oldest own time (the first ``YYYY-MM-DD HH:MM`` anywhere
    in the text; none readable = oldest) goes first, never the one being written -- history is
    in git. An entry that starts with its own ``Updated (...):`` heading has it stripped.
    The kept others stay in their order. A writer
    is identified by ``writer`` (``""`` for the unnamed writer, including the file's own
    single-line shape today, read as one line of an unnamed writer)."""
    entry = _strip_entry_heading(entry)
    ends_with_newline = text.endswith("\n")
    lines = text.split("\n")
    if ends_with_newline:
        lines = lines[:-1]  # split's trailing empty element from the final "\n"

    limit = next((i for i, line in enumerate(lines) if line.startswith("Last review")), len(lines))
    upd_idx: list[int] = []
    entries: list[tuple[str, str]] = []  # (writer, text), top to bottom
    for i in range(limit):
        m = _UPDATED_LINE_RE.match(lines[i])
        if m is not None:
            upd_idx.append(i)
            entries.append((m.group(1) or "", m.group(2)))
    block_start = upd_idx[0] if upd_idx else None

    # The entry being written is always kept and goes on top (its writer's old line removed).
    # Past the cap the entries with the oldest own time are cut; the kept others keep their order.
    others = [e for e in entries if e[0] != writer]
    room = MAX_STATE_WRITERS - 1
    if len(others) > room:
        # ORCH lines first, then the rest; oldest own time first; ties: the lower one in the
        # file goes first
        order = sorted(range(len(others)),
                       key=lambda i: (0 if _is_orch_writer(others[i][0]) else 1,
                                      _entry_time(others[i][1]), -i))
        cut = set(order[:len(others) - room])
        others = [e for i, e in enumerate(others) if i not in cut]
    entries = [(writer, entry)] + others

    new_block = [_render_state_line(w, t) for w, t in entries]

    if block_start is None:
        # No existing `Updated` line at all: insert right before `Last review`, or at the
        # top of the file when there is none either -- found by the word, not a line number.
        insert_at = next((i for i, line in enumerate(lines) if line.startswith("Last review")), 0)
        lines[insert_at:insert_at] = new_block
    else:
        first, last = upd_idx[0], upd_idx[-1]
        middle = [lines[i] for i in range(first, last + 1)
                  if i not in set(upd_idx) and lines[i].strip()]
        lines[first:last + 1] = new_block + middle

    result = "\n".join(lines)
    if ends_with_newline:
        result += "\n"
    return result


MAX_REVIEWS = 3
MAX_REVIEW_LINE_BYTES = 3072  # the whole `Last review:` line stays under this many UTF-8 bytes


def _review_line(segments: list[str]) -> str:
    return "Last review: " + ". Before it: ".join(segments) + "."

_BEFORE_IT_RE = re.compile(r"\s*Before it:\s*")


def _strip_dot(seg: str) -> str:
    seg = seg.strip()
    return seg[:-1] if seg.endswith(".") else seg


def _splice_review_entry(text: str, entry: str) -> str:
    """Write ``entry`` as the newest review of the ``Last review`` line of ``text`` (a whole file's
    contents), keeping at most ``MAX_REVIEWS`` reviews: the new one plus the newest old ones
    (older ones are dropped -- git holds them). The line is the first one starting with
    ``Last review``; its shape is ``Last review: <s>. Before it: <s>. Before it: <s>``.
    With no such line, ``Last review: <entry>.`` goes right after the last ``Updated`` line (a
    blank line between them), or at the top of the file when there is no ``Updated`` line."""
    ends_with_newline = text.endswith("\n")
    lines = text.split("\n")
    if ends_with_newline:
        lines = lines[:-1]

    idx = next((i for i, line in enumerate(lines) if line.startswith("Last review")), None)
    old: list[str] = []
    if idx is not None:
        body = lines[idx][len("Last review"):].lstrip()
        if body.startswith(":"):
            body = body[1:]
        old = [_strip_dot(seg) for seg in _BEFORE_IT_RE.split(body)]
        old = [seg for seg in old if seg]
    segments = [_strip_dot(entry)]
    for seg in old[:MAX_REVIEWS - 1]:
        trial = _review_line(segments + [seg])
        if len(trial.encode("utf-8")) >= MAX_REVIEW_LINE_BYTES:
            break  # older entries only get older: whole entries are dropped, never cut
        segments.append(seg)
    line = _review_line(segments)

    if idx is not None:
        lines[idx] = line
    else:
        last_upd = max((i for i, l in enumerate(lines) if _UPDATED_LINE_RE.match(l)), default=None)
        if last_upd is None:
            lines[0:0] = [line, ""]
        else:
            lines[last_upd + 1:last_upd + 1] = ["", line]

    result = "\n".join(lines)
    if ends_with_newline:
        result += "\n"
    return result


@dataclass
class CommitResult:
    ok: bool
    reason: str
    message: str = ""
    pushed: bool = False
    head_before: str = ""
    head_after: str = ""
    # {"path", "changed_lines", "save_with"} when a --state-entry/--review-entry commit left
    # other changes of the path unsaved in the working file; None otherwise.
    unsaved: dict | None = None
    # #40: {"files", "tests", "seconds", "status"} when a data commit ran its quick check; None otherwise.
    quick_check: dict | None = None

    def render(self) -> str:
        if self.ok:
            line = f"committed: {self.message} (head {self.head_after[:12]})"
            if self.unsaved:
                line += "\n" + self.warning()
            return line
        return f"not committed ({self.reason}): {self.message}"

    def warning(self) -> str:
        u = self.unsaved or {}
        return (f"warning: {u.get('path')} still has {u.get('changed_lines')} changed line(s) outside "
                f"the entry, NOT saved; save them with: {u.get('save_with')}")

    def to_json(self) -> str:
        return json.dumps(asdict(self))


def _push_committed(cwd: Path, remote: str, main: str, message: str, head_before: str,
                    head_after: str, max_push_attempts: int,
                    gate: "Callable[[], CommitResult | None] | None" = None) -> CommitResult:
    """The push-retry loop shared by both ``run_commit`` flows: a commit already sits at
    ``head_after``; push it, rebasing onto a moved ``origin/main`` and retrying up to
    ``max_push_attempts`` times in total. ``gate`` (the commit suite) runs first; a
    ``CommitResult`` it returns is the answer and nothing is pushed."""
    if gate is not None:
        refused = gate()
        if refused is not None:
            return refused
    attempts = 0
    while True:
        attempts += 1
        push = _git(cwd, "push", remote, main, env=MAIN_PUSH_ENV)
        if push.returncode == 0:
            return CommitResult(ok=True, reason="pushed", message=message, pushed=True,
                               head_before=head_before, head_after=head_after)

        if attempts >= max_push_attempts:
            return CommitResult(
                ok=False, reason="push-refused",
                message=f"push refused after {attempts} attempt(s): {_first_line(push.stderr)}",
                head_before=head_before, head_after=head_after)

        fetch_again = _git(cwd, "fetch", remote)
        if fetch_again.returncode != 0:
            return CommitResult(
                ok=False, reason="push-refused",
                message=f"push refused after {attempts} attempt(s): {_first_line(fetch_again.stderr)}",
                head_before=head_before, head_after=head_after)

        rebase = _git(cwd, "rebase", f"{remote}/{main}")
        if rebase.returncode != 0:
            _git(cwd, "rebase", "--abort")
            return CommitResult(
                ok=False, reason="push-refused",
                message=f"push refused after {attempts} attempt(s): rebase onto {remote}/{main} "
                        f"failed: {_first_line(rebase.stderr)}",
                head_before=head_before, head_after=_head(cwd))

        head_after = _head(cwd)


def _commit_state_entry(path: str, transform: "Callable[[str], str]", message: str, *, cwd: Path,
                        remote: str, main: str, max_push_attempts: int,
                        head_before: str,
                        gate: "Callable[[], CommitResult | None] | None" = None) -> CommitResult:
    """Apply ``transform`` (the ``Updated`` line and/or ``Last review`` splice) to ``path``,
    committing only that change -- built from ``HEAD``'s own blob of ``path`` plus the entry, never from whatever else the
    working tree holds (``git commit -- <pathspec>`` re-reads the named path from the working
    tree, so the commit's own version of ``path`` is written to disk first). So another writer's
    uncommitted change elsewhere in ``path`` is never picked up by this commit; once it lands,
    the working file gets the entry spliced into its own (possibly still-changed) text instead,
    restoring that other change byte for byte."""
    show = _git(cwd, "show", f"HEAD:{path}")
    if show.returncode != 0:
        return CommitResult(ok=False, reason="add-failed", message=_first_line(show.stderr),
                           head_before=head_before, head_after=_head(cwd))
    head_text = show.stdout
    new_head_text = transform(head_text)
    if new_head_text == head_text:
        return CommitResult(ok=False, reason="nothing-to-commit", message=path,
                           head_before=head_before, head_after=head_before)

    working_path = cwd / path
    try:
        original_working_text = working_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        original_working_text = head_text

    working_path.write_text(new_head_text, encoding="utf-8")
    try:
        add = _git(cwd, "add", "--", path)
        if add.returncode != 0:
            return CommitResult(ok=False, reason="add-failed", message=_first_line(add.stderr),
                               head_before=head_before, head_after=_head(cwd))

        commit = _git(cwd, "commit", "-m", message, "--", path)
        if commit.returncode != 0:
            return CommitResult(ok=False, reason="commit-failed",
                               message=_first_line(commit.stderr),
                               head_before=head_before, head_after=_head(cwd))
    finally:
        working_path.write_text(transform(original_working_text), encoding="utf-8")

    head_after = _head(cwd)
    result = _push_committed(cwd, remote, main, message, head_before, head_after, max_push_attempts,
                             gate=gate)
    if result.ok:
        try:
            left = working_path.read_text(encoding="utf-8")
        except OSError:
            left = new_head_text
        if left != new_head_text:
            import difflib
            a, b = new_head_text.splitlines(), left.splitlines()
            changed = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2
                          in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
                          if tag != "equal")
            result = _dc_replace(result, unsaved={
                "path": path, "changed_lines": changed,
                "save_with": f"python -m tools.agentops commit -m <message> {path}"})
    return result


def _glob_regex(glob: str) -> "re.Pattern[str]":
    """A ``suite_free_paths`` glob as a regex over one repo-relative path: ``**/`` is zero or more
    folders, ``**`` any rest of the path, ``*`` and ``?`` stay inside one folder."""
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        else:
            char = glob[i]
            out.append("[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char))
            i += 1
    return re.compile("".join(out))


def resolve_suite_free_globs() -> list[str]:
    """The settings file's ``suite_free_paths`` (the core's defaults plus the target's own, #40);
    the core's defaults alone when the settings file cannot be read."""
    try:
        from .core_settings import load as load_settings
        return list(load_settings()["suite_free_paths"])
    except Exception:
        from .core_settings import SUITE_FREE_PATHS
        return list(SUITE_FREE_PATHS)


def message_issues(message: str) -> list[int]:
    """#37: the ticket numbers (``#N``) of a commit message's first line, in order."""
    first = message.split("\n", 1)[0]
    return [int(n) for n in re.findall(r"#(\d+)", first)]


def _holder_record(ticket_info: dict | None) -> dict:
    """#37: what the merge lock's holder file records of its holder: a commit by its message and the tickets
    of its first line, a merge by its branch. Any other ticket (or none) adds nothing; old readers ignore it."""
    if not ticket_info:
        return {}
    if ticket_info.get("kind") == "commit":
        return {"kind": "commit", "message": str(ticket_info.get("message") or ""),
                "issues": [int(i) for i in ticket_info.get("issues") or []]}
    if ticket_info.get("kind") == "merge":
        return {"kind": "merge", "branch": str(ticket_info.get("branch") or "")}
    if ticket_info.get("kind") == "core-update":  # #203: a core update's holder names its home head; #207: no sha
        sha = str(ticket_info.get("sha") or "")  # key until the head is read
        return {"kind": "core-update", **({"sha": sha} if sha else {})}
    return {}


def commit_needs_suite(paths: Sequence[str]) -> bool:
    """True when at least one committed path matches none of the ``suite_free_paths`` globs (#40:
    the settings file, the core's defaults when the key is absent)."""
    free = [_glob_regex(glob) for glob in resolve_suite_free_globs()]
    for raw in paths:
        norm = raw.replace("\\", "/")
        while norm.startswith("./"):
            norm = norm[2:]
        if not any(pattern.fullmatch(norm) for pattern in free):
            return True
    return False


# -- the quick check of a data commit (#40) -------------------------------------------------------

QUICK_CHECK_NAME = "quick-check"  # its own suite worktree: a merge's suite worktree is never reset under a data commit
QUICK_CHECK_TIMEOUT = 900  # seconds: bounds a hang only; a few test files take seconds
QUICK_LINE = "commit-quick-check: "  # the one machine line run_commit_command prints (detach reads it into the result)


def _norm_path(raw: str) -> str:
    norm = raw.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def _quick_check_needed(paths: Sequence[str]) -> bool:
    """A data commit with at least one non-Markdown path runs the quick check; a Markdown-only commit runs nothing."""
    return any(not _norm_path(raw).endswith(".md") for raw in paths)


def _test_files_naming(suite_dir: Path, names: set[str]) -> list[str]:
    """The ``*.py`` files under ``tests/`` of ``suite_dir`` (conftest included) whose text contains one of
    ``names``, sorted, repo-relative."""
    found = []
    for path in sorted((suite_dir / "tests").rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(name in text for name in names):
            found.append(path.relative_to(suite_dir).as_posix())
    return found


def _quick_check_cmd(suite_cmd: str, files: Sequence[str]) -> str:
    """The merge suite command with its ``tests`` argument replaced by ``files`` (the same flags)."""
    quoted = " ".join(f'"{f}"' for f in files)
    match = re.search(r"(?<!\S)tests(?!\S)", suite_cmd)
    if match is None:
        return f"{suite_cmd} {quoted}"
    return f"{suite_cmd[:match.start()]}{quoted}{suite_cmd[match.end():]}"


def _quick_check(cwd: Path, head_commit: str, paths: Sequence[str], suite_cmd: str,
                 suite_env: dict[str, str], suite_shared: Sequence[str]) -> tuple[bool, str, dict]:
    """``(green, message, record)`` for the new local commit ``head_commit``: (a) every changed ``.json`` path
    loads; (b) the ``tests/`` files that name a changed non-Markdown file pass the suite command with those
    files in place of ``tests``. ``record`` is the result's ``quick_check``. Runs in the quick-check worktree
    (the commit's own tree), never in the checkout or the merge's suite worktree."""
    t0 = time.monotonic()
    rels = [_norm_path(p) for p in paths]

    def done(ok: bool, message: str, tests: int) -> tuple[bool, str, dict]:
        seconds = round(time.monotonic() - t0, 2)
        if not ok:
            message = f"{message} ({human_duration(seconds)})"
        return ok, message, {"files": len(paths), "tests": tests, "seconds": seconds,
                             "status": "green" if ok else "red"}

    try:
        suite_dir = ensure_suite_worktree(cwd, head_commit, shared=tuple(suite_shared), lock=None,
                                          name=QUICK_CHECK_NAME)
    except SuiteWorktreeError as exc:
        return done(False, f"no worktree at {head_commit[:12]}: {_first_line(str(exc))}", 0)
    for rel in rels:
        if not rel.endswith(".json"):
            continue
        try:
            json.loads((suite_dir / rel).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return done(False, f"{rel} is not valid JSON: {_first_line(str(exc))}", 0)
    names = {Path(rel).name for rel in rels if not rel.endswith(".md")}
    files = _test_files_naming(suite_dir, names)
    if not files:
        return done(True, "", 0)
    cmd = _quick_check_cmd(suite_cmd, files)
    if _is_pytest_command(suite_cmd):
        cmd = _augmented_suite_cmd(cmd)
    tail = suite_env.get("PYTHONPATH", os.environ.get("PYTHONPATH"))
    run_env = {**nested_env(), **with_no_runlog(suite_env), "PYTHONUNBUFFERED": "1",
               "PYTHONPATH": str(suite_dir) + (os.pathsep + tail if tail else "")}
    try:
        proc = subprocess.run(cmd, shell=True, cwd=str(suite_dir), env=run_env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              creationflags=no_window_flags(), timeout=QUICK_CHECK_TIMEOUT)
    except subprocess.TimeoutExpired:
        return done(False, f"the tests of {len(files)} file(s) ran past {human_duration(QUICK_CHECK_TIMEOUT)}",
                    len(files))
    if proc.returncode in (0, 5):  # 5: pytest found no test to run (every one deselected)
        return done(True, "", len(files))
    output = (proc.stdout or "") + (proc.stderr or "")
    _count, ids = _parse_pytest_failures(output)
    if ids:
        where = ids[0] + (f" (and {len(ids) - 1} more)" if len(ids) > 1 else "") + " failed"
    else:
        where = f"suite exit {proc.returncode}: {_first_line(output)}"
    return done(False, where, len(files))


def _quick_check_gate(cwd: Path, head_before: str, paths: Sequence[str], suite_cmd: str,
                      suite_env: dict[str, str], suite_shared: Sequence[str],
                      record: dict) -> "Callable[[], CommitResult | None]":
    """The gate ``_push_committed`` runs for a data commit: the quick check on the new local commit. Green ->
    ``None`` (the push goes on; ``record`` keeps the check). Red -> local ``main`` goes back to ``head_before``
    with ``reset --mixed`` (the changes stay in the working tree), nothing is pushed: as a red suite's commit."""
    def gate() -> CommitResult | None:
        head_commit = _head(cwd)
        ok, message, quick = _quick_check(cwd, head_commit, paths, suite_cmd, suite_env, suite_shared)
        record.update(quick)
        if ok:
            return None
        parent = _git(cwd, "rev-parse", head_commit + "^").stdout.strip() or head_before
        reset = _git(cwd, "reset", "--mixed", parent)
        if reset.returncode != 0:
            message += f"; git reset --mixed {parent[:12]} failed: {_first_line(reset.stderr)}"
        return CommitResult(ok=False, reason="quick-check-red", message=message, head_before=head_before,
                            head_after=_head(cwd))
    return gate


def _commit_suite_gate(cwd: Path, head_before: str, suite_cmd: str, suite_env: dict[str, str],
                       suite_shared: Sequence[str], suite_output_dir: Path,
                       merge_log_path: Path, lock: MergeLock,
                       checkout_lock: MergeLock | None = None) -> "Callable[[], CommitResult | None]":
    """The gate ``_push_committed`` runs: the merge's own suite on the new local commit (in the
    standing suite worktree). Green -> ``None`` (push goes on). Red -> local ``main`` goes back to
    ``head_before`` with ``reset --mixed`` (the changes stay in the working tree), nothing is pushed."""
    def gate() -> CommitResult | None:
        head_commit = _head(cwd)
        is_pytest = _is_pytest_command(suite_cmd)
        run_cmd = _augmented_suite_cmd(suite_cmd) if is_pytest else suite_cmd
        output_path = _new_suite_output_path("commit", suite_output_dir)
        lock.set_progress(output_path)
        if checkout_lock is not None:  # a suite-free commit waiting on it counts silence from this output too
            checkout_lock.set_progress(output_path)
        tests_line, run = "", SuiteRun(False, "", 0)  # read by the red path when the suite worktree failed
        hold_while_safety_red(cwd, lock.heartbeat, merge_log_path)  # #195: a red full run holds the suite line
        try:
            suite_dir = ensure_suite_worktree(cwd, head_commit, shared=tuple(suite_shared), lock=lock)
        except SuiteWorktreeError as exc:
            reason, message = "suite-worktree", _first_line(str(exc))
            suite_dir, failed_count, failed_ids, duration, workers, suite_host_name, suite_note = None, None, [], 0.0, 0, "", ""
        else:
            t0 = time.monotonic()
            tail = suite_env.get("PYTHONPATH", os.environ.get("PYTHONPATH"))
            run_env = {**suite_env, "PYTHONPATH": str(suite_dir) + (os.pathsep + tail if tail else "")}
            tests_line, skip_suite = "", False
            if is_pytest and resolve_select_tests():
                parent_sha = _git(cwd, "rev-parse", head_commit + "^").stdout.strip() or head_before
                run_cmd, tests_line, skip_suite = _plan_selection(cwd, parent_sha, head_commit, run_cmd, output_path)
            if skip_suite:  # nothing the change can affect: no pytest (it exits 5 on no tests)
                run = SuiteRun(True, "", 0)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(tests_line + "\n", encoding="utf-8")
            else:
                def _suite_body(run_cmd=run_cmd, tests_line=tests_line):
                    cmd, n_workers, head = _with_workers(run_cmd, is_pytest, suite_dir)
                    head = "\n".join(x for x in (tests_line, head) if x)
                    return _run_merge_suite(cmd, suite_cmd, n_workers, head, suite_dir, run_env, output_path,
                                            routed_ok=not tests_line.startswith("selected"), root=cwd)

                run = _run_heavy_suite(cwd, "commit", tests_line, False, _suite_body, output_path, merge_log_path, lock)
            ok_suite, output, workers, suite_host_name, suite_note = run.ok, run.output, run.workers, run.host, run.recheck_note
            duration = time.monotonic() - t0
            if run.stuck:  # a stalled run is a red result with its reason, never an ordinary red
                ok_suite, failed_count, failed_ids = False, None, []
                reason, message = "stuck", run.stuck
            elif ok_suite:
                _append_merge_log("commit", MergeResult(ok=True, reason="suite-green", suite_runs=1,
                                                        suite_duration=duration, suite_workers=workers, suite_host=suite_host_name, recheck_note=suite_note, skipped_note=run.skipped_note,
                                                        rechecked=run.red_ids if run.rechecked_done and run.ok else [],
                                                        suite_output_path=str(output_path), selection=tests_line,
                                                        suite_dir=str(suite_dir)),
                                  log_path=merge_log_path)
                return None
            if run.stuck:
                pass
            else:
                if is_pytest:
                    failed_count, failed_ids = _parse_pytest_failures(output)
                else:
                    failed_count, failed_ids = None, []
                if run.fail_fast:  # #219: the test and both failures
                    failed_count, failed_ids = 1, [run.fail_fast.test]
                if is_pytest and failed_ids and not run.rechecked_done and not run.fail_fast:  # #223: the verdict of each failed test; the commit stays red either way
                    try:
                        _s, _o, run.verdicts, judged_note = _judged_recheck_failed(
                            suite_cmd, failed_ids, suite_dir, run_env, output_path, run.load_reading, lock.heartbeat,
                            run.failure_readings)
                        suite_note = _with_note(run, judged_note)
                    except RecheckInconclusive:
                        _fill_verdicts(run.verdicts, failed_ids, "inconclusive", run.load_reading, run.failure_readings)
                _fill_verdicts(run.verdicts, failed_ids, "unjudged", run.load_reading, run.failure_readings)  # #223: no id without a word
                reason, message = "red-suite", _red_with_fail_fast(run, failed_count, failed_ids, output_path)
        # the commit's parent is the synced head it was made on (``head_before`` predates _sync_up)
        parent = _git(cwd, "rev-parse", head_commit + "^").stdout.strip() or head_before
        reset = _git(cwd, "reset", "--mixed", parent)
        if reset.returncode != 0:
            message += f"; git reset --mixed {parent[:12]} failed: {_first_line(reset.stderr)}"
        _append_merge_log("commit", MergeResult(ok=False, reason=reason, message=message, suite_runs=1,
                                                suite_duration=duration, suite_workers=workers, suite_host=suite_host_name, recheck_note=suite_note, skipped_note=run.skipped_note,
                                                failed_count=failed_count,
                                                failed_tests=failed_ids, selection=tests_line, verdicts=run.verdicts,
                                                suite_output_path=str(output_path) if suite_dir else "",
                                                suite_dir=str(suite_dir) if suite_dir else ""),
                          log_path=merge_log_path)
        return CommitResult(ok=False, reason=reason, message=message, head_before=head_before,
                            head_after=_head(cwd))
    return gate


def _spawn_after_commit(cwd: Path, result: CommitResult, select_on: bool,
                        merge_log_path: Path | None) -> CommitResult:
    """A commit that pushed under a selected suite: the full run rebuilds the map at the landed commit (#41).
    A failed spawn is one merge-log line."""
    if select_on and result.pushed:
        sha = _head(cwd)
        problem = _spawn_map_run(cwd, sha)
        if problem:
            _log_map_run_spawn("commit", sha, problem, merge_log_path or _merge_log_path())
    return result


def run_commit(paths: list[str], message: str, *, cwd: Path, lock_path: Path = DEFAULT_LOCK_PATH,
               remote: str = DEFAULT_REMOTE, main: str = DEFAULT_MAIN,
               lock_timeout: float | None = None,
               max_push_attempts: int = DEFAULT_MAX_COMMIT_PUSH_ATTEMPTS,
               state_entry: str | None = None, state_as: str = "",
               review_entry: str | None = None,
               suite_cmd: str | None = None, suite_env: dict[str, str] | None = None,
               suite_shared: Sequence[str] | None = None,
               suite_output_dir: Path | None = None,
               merge_log_path: Path | None = None) -> CommitResult:
    """The commit command (``_run_commit`` does the work): a data commit's result carries its
    ``quick_check`` (#40); every other result carries none."""
    record: dict = {}
    result = _run_commit(paths, message, cwd=cwd, lock_path=lock_path, remote=remote, main=main,
                         lock_timeout=lock_timeout, max_push_attempts=max_push_attempts,
                         state_entry=state_entry, state_as=state_as, review_entry=review_entry,
                         suite_cmd=suite_cmd, suite_env=suite_env, suite_shared=suite_shared,
                         suite_output_dir=suite_output_dir, merge_log_path=merge_log_path, record=record)
    result.quick_check = dict(record) if record else None
    return result


def _run_commit(paths: list[str], message: str, *, cwd: Path, lock_path: Path = DEFAULT_LOCK_PATH,
                remote: str = DEFAULT_REMOTE, main: str = DEFAULT_MAIN,
                lock_timeout: float | None = None,
                max_push_attempts: int = DEFAULT_MAX_COMMIT_PUSH_ATTEMPTS,
                state_entry: str | None = None, state_as: str = "",
                review_entry: str | None = None,
                suite_cmd: str | None = None, suite_env: dict[str, str] | None = None,
                suite_shared: Sequence[str] | None = None,
                suite_output_dir: Path | None = None,
                merge_log_path: Path | None = None,
                record: dict | None = None) -> CommitResult:
    """Commit only ``paths`` (an untracked file or a modified one left off the list is untouched)
    and push to ``main`` -- under the same lock ``merge`` takes, so a commit in the main checkout
    never lands on a head a live merge has not settled yet, and never races a merge's own push.

    Before touching anything, the checkout is brought up to ``remote``/``main`` (fetch, then
    fast-forward only -- ``_sync_up``, the same helper ``run_merge`` uses). A checkout that
    cannot be fast-forwarded (its own unpushed commit, or a fast-forward that would overwrite a
    changed file of the working tree) is refused right there: exit non-zero, nothing added or
    committed, ``paths``' changes left in the working tree as they were.

    ``review_entry`` does the same for the ``Last review`` line (``_splice_review_entry``: the newest
    review plus at most 2 older ones); it may be given alone or with ``state_entry``.

    ``state_entry`` hands a ``docs/STATE.md``-style ``Updated`` line's text to the
    command itself, written under the lock (``_commit_state_entry``) instead of trusting whatever
    the shared checkout's working tree holds when the lock frees; ``paths`` must then be exactly
    one path. Without it, ``paths`` are added and committed as they sit in the working tree, as
    before; either way, nothing staged for ``paths`` answers ``nothing-to-commit``, never an empty
    reason.

    A push refused once the commit is made (``origin/main`` moved between the fetch above and
    the push) is answered by fetching again, rebasing the one new commit onto the remote's head,
    and pushing again -- at most ``max_push_attempts`` pushes in total. After the last refusal the
    local commit is kept (no rollback) and the message says how many times it tried; the caller
    decides what to do next.

    A shallow ``cwd`` is repaired first, once, with ``git fetch --unshallow <remote>`` (the
    same check ``run_merge`` makes): on success the repair is named in the result's own
    ``message``; on failure (e.g. the remote is unreachable) nothing is added or committed and
    the reason is ``"shallow"``.

    The merge suite gates the commit: when at least one path is not a Markdown file under
    ``suite_free_paths`` (``commit_needs_suite``), the suite (``resolve_suite_cmd`` / ``resolve_suite_env``
    unless given) runs on the new local commit in the standing suite worktree, before any push. A red
    suite moves local ``main`` back to the head it had (``reset --mixed``: the changes stay in the
    working tree uncommitted), pushes nothing, and answers ``red-suite`` with the merge's own message.
    A commit whose every path is a data path (``suite_free_paths``) stays suite-free; there is no flag that turns the suite off."""

    if not paths:
        raise MergeError("no paths: pass at least one path to commit")
    if not message:
        raise MergeError("no message: pass -m <message>")
    if state_entry is not None and len(paths) != 1:
        raise MergeError("--state-entry takes exactly one path")
    if review_entry is not None and len(paths) != 1:
        raise MergeError("--review-entry takes exactly one path")
    if state_entry is not None and not _entry_time(_strip_entry_heading(state_entry)):
        raise MergeError("--state-entry has no readable 'YYYY-MM-DD HH:MM' time (a time like "
                         "'19:4x' ranks the line oldest and the cap cuts it): write the full date "
                         "and time")

    core_refusal = _core_commit_refusal(cwd, paths)  # before any add, lock or suite
    if core_refusal:
        return CommitResult(ok=False, reason="core-manifest", message=core_refusal,
                            head_before=_head(cwd), head_after=_head(cwd))

    needs_suite = commit_needs_suite(paths)
    select_on = False  # a selected pytest suite: the commit's push is followed by the full run (#41)
    quick_needed = not needs_suite and _quick_check_needed(paths)  # #40: a data commit's JSON and tests
    if needs_suite or quick_needed:
        suite_cmd = resolve_suite_cmd(suite_cmd)  # refuses before anything is touched
        select_on = _is_pytest_command(suite_cmd) and resolve_select_tests()
        if suite_env is None:
            suite_env = resolve_suite_env()
        if suite_shared is None:
            suite_shared = resolve_suite_shared()
    if needs_suite:
        if suite_output_dir is None:
            suite_output_dir = _suite_output_dir()
        if merge_log_path is None:
            merge_log_path = _merge_log_path()
        _prune_suite_outputs(keep=MAX_SUITE_OUTPUTS - 1, output_dir=suite_output_dir)

    # A commit that needs no suite takes only the short checkout lock, so it lands while a merge's
    # suite runs (the merge then takes its docs-only re-merge); one that needs a suite keeps its place in
    # the merge line and holds the checkout lock too, since its local commit sits in the checkout through
    # its own suite.
    commit_info = {"kind": "commit", "message": message, "issues": message_issues(message)}  # #37: the holder names it
    with merge_then_checkout_locked(lock_path, lock_timeout, hold_merge=needs_suite,
                                    merge_ticket_info={**commit_info, "needs_suite": True},
                                    checkout_ticket_info=None if needs_suite else
                                    {**commit_info, "needs_suite": False}) as (lock, checkout_lock):
        head_before = _head(cwd)
        if needs_suite:
            gate = _commit_suite_gate(cwd, head_before, suite_cmd, suite_env, suite_shared,
                                      suite_output_dir, merge_log_path, lock, checkout_lock)
        elif quick_needed:
            gate = _quick_check_gate(cwd, head_before, paths, suite_cmd, suite_env, suite_shared,
                                     record if record is not None else {})
        else:
            gate = None

        repair_note = ""
        if _is_shallow(cwd):
            repaired, repair_message = _repair_shallow(cwd, remote)
            if not repaired:
                return CommitResult(ok=False, reason="shallow",
                                   message=repair_message or f"git fetch --unshallow {remote} failed",
                                   head_before=head_before, head_after=head_before)
            repair_note = f"unshallowed (fetch --unshallow {remote}); "

        ok, reason, sync_message = _sync_up(cwd, remote, main)
        if not ok:
            return CommitResult(ok=False, reason=reason, message=sync_message,
                               head_before=head_before, head_after=head_before)

        if state_entry is not None or review_entry is not None:
            def transform(text: str) -> str:
                if state_entry is not None:
                    text = _splice_state_entry(text, state_as, state_entry)
                if review_entry is not None:
                    text = _splice_review_entry(text, review_entry)
                return text
            return _spawn_after_commit(cwd, _commit_state_entry(paths[0], transform, message, cwd=cwd,
                                       remote=remote, main=main,
                                       max_push_attempts=max_push_attempts,
                                       head_before=head_before, gate=gate), select_on, merge_log_path)

        add = _git(cwd, "add", "--", *paths)
        if add.returncode != 0:
            return CommitResult(ok=False, reason="add-failed", message=_first_line(add.stderr),
                               head_before=head_before, head_after=_head(cwd))

        staged = _git(cwd, "diff", "--cached", "--quiet", "--", *paths)
        if staged.returncode == 0:
            return CommitResult(ok=False, reason="nothing-to-commit", message=", ".join(paths),
                               head_before=head_before, head_after=head_before)

        commit = _git(cwd, "commit", "-m", message, "--", *paths)
        if commit.returncode != 0:
            return CommitResult(ok=False, reason="commit-failed", message=_first_line(commit.stderr),
                               head_before=head_before, head_after=_head(cwd))

        head_after = _head(cwd)
        return _spawn_after_commit(cwd, _push_committed(cwd, remote, main, f"{repair_note}{message}", head_before,
                                                        head_after, max_push_attempts, gate=gate),
                                   select_on, merge_log_path)


# -- CLI ------------------------------------------------------------------------------------------

FOREIGN_ISSUE_RE = re.compile(r"^[\w.-]+/[\w.-]+#\d+$")


def issue_arg(value: str) -> int | str:
    """#145: an `--issue` value is `N` (this repo's ticket, an int) or `owner/repo#N` (another repo's, kept as the
    string); anything else is an argparse error."""
    if value.isascii() and value.isdigit():
        return int(value)
    if FOREIGN_ISSUE_RE.match(value):
        return value
    raise argparse.ArgumentTypeError(f"invalid --issue value {value!r}: expected N or owner/repo#N")


def add_subparser(sub) -> None:
    merge = sub.add_parser("merge", help="one merge at a time across every live session")
    merge.add_argument("branch")
    merge.add_argument("--suite", help="the suite command; defaults to the settings file's own")
    merge.add_argument("--cwd", type=Path, default=ROOT)
    merge.add_argument("--lock", type=Path, default=DEFAULT_LOCK_PATH)
    merge.add_argument("--timeout", type=float, default=None,
                       help="the lock's age ceiling in seconds; defaults to the settings key "
                            "'merge_lock_max_age_min' (minutes), or 30 min")
    merge.add_argument("--issue", type=issue_arg, action="append", default=[],
                       help="an issue number this merge closes, or owner/repo#N for another repo's; repeatable")
    merge.add_argument("--queue-checked", dest="queue_checked", action="store_true",
                       help=argparse.SUPPRESS)  # #110: set by `--detach`, which ran the queue-time target check
    merge.add_argument("target", nargs="?", help="with `merge withdraw`: the issue number or the branch")  # #219 rule 3
    merge.add_argument("--why", default="", help="with `merge withdraw`: the reason, one line")
    merge.add_argument("--by", default="", help="with `merge withdraw`: who withdraws (default: the user)")


TESTS_LINE_PREFIX = "merge-tests: "
RUNS_LINE_PREFIX = "merge-runs: "  # #212: rebases and suite runs of the attempt, for the result record (detach reads it)
SELECTION_LINE_PREFIX = "merge-selection: "  # #74 unit 2: the suite's line 1, for the result record (detach reads it)


def _stats_line(t0: float, timing: dict, result: MergeResult | None) -> str:
    in_line = timing.get("lock_at", time.monotonic()) - t0
    suite_s, failed = "-", "-"
    if result is not None and result.suite_duration > 0:
        suite_s = f"{result.suite_duration:.1f}"
        failed = str(result.failed_count if result.failed_count is not None else len(result.failed_tests))  # a run that started has a count
    elif result is not None and result.reason == "red-on-branch" and result.failed_count is not None:
        failed = str(result.failed_count)  # no suite of this waiter's own ran: the check's failed ids
    line = f"merge-stats: in_line_s={in_line:.1f} suite_s={suite_s} failed={failed}"
    if result is not None and (result.suite_duration > 0 or result.verdicts):  # the same run's node ids, for the result file; the stats line stays LAST
        tests = {"failed_tests": result.failed_tests, "unfinished": result.unfinished}
        if result.verdicts:  # #223: {test id: {word, raw numbers}}
            tests["verdicts"] = {i: merge_load.verdict_summary(rec) for i, rec in result.verdicts.items()}
        line = TESTS_LINE_PREFIX + json.dumps(tests) + chr(10) + line
    return line


def _preload_repo_modules() -> None:
    """#156 rule 1: every ``tools.agentops`` module the path after the lock wait imports lazily is loaded NOW, before
    this process waits. A batch the leader lands may rewrite those files while this waiter sleeps; a first import
    afterwards would pair a module of the new files with one of the old files already loaded (an ImportError).
    ``target_check`` pulls ``core_targets`` and ``core_update`` with it; the settings' named router and priority
    flags are resolved once, which imports their modules. #163: ``core_table`` (``merge_priority.gh_marks``'s
    ``GhIssueReader``) and ``dispatch`` (``gh_blockers``'s ``parse_candidates``) are read by the holder right after
    the wait, for the next waiter's labels and blockers; ``core_table`` pulls ``scripts/token_meter.py`` with it."""
    from . import (core_check, core_settings, core_table, dispatch, gh_call, merge_priority, merge_status,  # noqa: F401 -- loaded for their side effect
                   target_check, test_referrers, test_select)
    _settings_callable("suite_router")
    _settings_callable("suite_priority_flags")


def run_merge_command(args) -> int:
    if args.branch == "withdraw":  # #219 rule 3: `merge withdraw <issue|branch> --why "..."` (merge_withdraw.py)
        from . import merge_withdraw
        return merge_withdraw.run_command(args)
    _preload_repo_modules()
    refusal = worktree_refusal(args.cwd)
    if refusal:
        print(refusal)
        return 1
    if not getattr(args, "queue_checked", False):  # #110: a refused branch takes no line
        refused = queue_target_refusals(args.branch, cwd=args.cwd)
        if refused:
            print("\n".join(refused))
            return 1
    t0 = time.monotonic()
    timing: dict = {}
    try:
        suite_cmd = resolve_suite_cmd(args.suite)
    except MergeError as exc:
        print(str(exc))
        return 2
    try:
        result = run_merge(args.branch, suite_cmd, cwd=args.cwd, lock_path=args.lock,
                           lock_timeout=args.timeout, suite_env=resolve_suite_env(),
                           suite_shared=resolve_suite_shared(),
                           issues=[n for n in (getattr(args, "issue", None) or ()) if isinstance(n, int)],
                           timing=timing)
    except MergeLockTimeout as exc:
        print(str(exc))  # one line: pid, start time, held for, silent for -- no traceback, lock untouched
        _append_lock_timeout_log(args.branch, exc)
        print(_stats_line(t0, timing, None))
        return 3
    except MergeAlreadyQueued as exc:
        print(str(exc))
        return EXIT_ALREADY_QUEUED
    except MergeWithdrawn as exc:  # #219: the result says did not land (withdrawn / superseded), by detach's marker
        from . import merge_withdraw
        result_env = os.environ.get(ENV_MERGE_RESULT)
        superseded = bool(result_env) and merge_withdraw.marker_reason(Path(result_env)) == merge_withdraw.SUPERSEDED
        print(f"did not land ({merge_withdraw.withdrawn_note(Path(result_env))})" if superseded
              else f"did not land (withdrawn): {exc}")
        return 1
    print(result.render())
    try:
        if result.monitor_note:  # #149
            print(result.monitor_note)
        from .target_check import note_lines
        for line in note_lines(result.target_notes):  # #93: each target with no leak-words file, named (never a refusal)
            print(line)
        if result.selection:  # the stats line stays last
            print(f"{SELECTION_LINE_PREFIX}{result.selection}")
        print(f"{RUNS_LINE_PREFIX}rebases={result.rebases} suite_runs={result.suite_runs}")  # #212: for the result record
        print(_stats_line(t0, timing, result))
    except Exception as exc:  # noqa: BLE001 -- #156: a branch on main is landed whatever a later report step does
        if not result.ok:
            raise
        print(f"after-landing step failed: {type(exc).__name__}: {' '.join(str(exc).split())}")
    return 0 if result.ok else 1


def add_commit_subparser(sub) -> None:
    commit = sub.add_parser("commit", help="commit named paths under the merge lock and push to main; a commit whose paths are "
                                  "not all Markdown under docs/ runs the merge suite first, and a red suite refuses it "
                                  "(changes stay in the working tree, nothing is pushed)")
    commit.add_argument("-m", "--message", required=True, help="the commit message")
    commit.add_argument("paths", nargs="+", help="the paths to add and commit -- nothing else is touched")
    commit.add_argument("--state-entry", dest="state_entry", default=None,
                       help="the text of one Updated line of the one named path (a docs/STATE.md-"
                            "style file); written under the lock, replacing this writer's own "
                            "line (see --state-as) instead of committing the working tree as it "
                            "sits. Takes exactly one path. Any other change of the file stays "
                            "unsaved: the command prints one 'warning:' line for it; a plain "
                            "commit of the path saves it")
    commit.add_argument("--state-as", dest="state_as", default="",
                       help="the writer's name for --state-entry; omitted, the entry is the "
                            "unnamed writer's line")
    commit.add_argument("--review-entry", dest="review_entry", default=None,
                       help="the text of the newest review of the one named path's 'Last review' "
                            "line (a docs/STATE.md-style file); written under the lock, the line "
                            "keeps the newest review plus at most 2 'Before it:' entries. May be "
                            "combined with --state-entry. Takes exactly one path")
    commit.add_argument("--cwd", type=Path, default=ROOT)
    commit.add_argument("--lock", type=Path, default=DEFAULT_LOCK_PATH)
    commit.add_argument("--timeout", type=float, default=None,
                       help="the lock's age ceiling in seconds; defaults to the settings key "
                            "'merge_lock_max_age_min' (minutes), or 30 min")


def run_commit_command(args) -> int:
    try:
        result = run_commit(args.paths, args.message, cwd=args.cwd, lock_path=args.lock,
                           lock_timeout=args.timeout,
                           state_entry=getattr(args, "state_entry", None),
                           state_as=getattr(args, "state_as", "") or "",
                           review_entry=getattr(args, "review_entry", None))
    except MergeError as exc:
        print(str(exc))
        return 2
    except MergeLockTimeout as exc:
        print(str(exc))  # one line: pid, start time, held for, silent for -- no traceback, lock untouched
        _append_lock_timeout_log("commit", exc)
        return 3
    print(result.render())
    if result.quick_check is not None:
        print(f"{QUICK_LINE}{json.dumps(result.quick_check)}")
    if result.ok:
        return 0
    if result.reason == "nothing-to-commit":
        return 4
    return 1
