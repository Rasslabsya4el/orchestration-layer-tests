"""Fixtures of the source repo's conftest that the seeded tests use (written once by `home seed`)."""
from __future__ import annotations

import pytest

# #235: the run log for every pytest run of this repo (after `pythonpath = .` applies, so bare `pytest` works)
# #319: the tests red on main before a ticket are skipped by tests/red_on_main.txt (the plugin's hooks, no hook here)
pytest_plugins = ["tools.agentops.pytest_runs_plugin", "tools.agentops.red_on_main_plugin"]

from _commands_guard import (
    REAL_COMMANDS, REAL_JOURNAL, REAL_MODS, changes, mod_folder_names, name_changes, note_line, snapshot,
    unexplained_changes,
)
from tools.agentops import install_folder


from datetime import datetime
from pathlib import Path


@pytest.fixture(autouse=True)
def _machine_mode_dir_per_test(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """Every test gets its own machine-mode dir (#779). One dir per session was not enough: `runs.py mode pc play`
    and `hosts.revert_expired_modes` write the machine mode, so a test of them left `play` in the dir of its xdist
    worker and every later test of that worker saw a held machine (the merge line's `_acquire` waited for ever in
    play; the dispatcher tests started nothing), 10 failed and the run stood at 99 %."""
    monkeypatch.setenv("AGENTOPS_MACHINE_DIR", str(tmp_path_factory.mktemp("machine")))


@pytest.fixture(autouse=True)
def _no_test_part_variables_in_a_test(monkeypatch: pytest.MonkeyPatch):
    """#256: the workflow's pytest step sets TEST_SHARD and TEST_IDS_FILE for the whole process, and every test of the shard
    plugin (its subprocess runs, its hook calls) inherited them: 3 of test_issue232_shard.py and 8 of
    test_issue232_pc_only.py failed on GitHub. A test that needs one sets it itself."""
    for name in ("TEST_SHARD", "TEST_IDS_FILE", "TEST_PC_ONLY", "TEST_SKIP_FILE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _boot_time_is_never_the_real_one(monkeypatch: pytest.MonkeyPatch):
    """No test reads the machine's real boot time (#259). A rig's fake session start is an old date; a freshly booted
    GitHub runner booted after it, so `_restart_boot_time` called the end a `machine_restart` and 14 tests failed there
    while they passed on the PC. A test that injects `Dispatcher.boot_time` itself is unaffected."""
    try:
        import psutil
    except ImportError:
        return
    monkeypatch.setattr(psutil, "boot_time", lambda: 0.0)


@pytest.fixture(autouse=True)
def _hold_never_touches_the_real_root(monkeypatch: pytest.MonkeyPatch):
    """A keeper pass on this checkout's own root is a no-op in every test (#779): `var/` is linked to the main
    checkout's, so a dispatcher-tick test calling `hold.keeper_pass(mode, ROOT)` rewrote the real
    `var/agentops/hold_record.json` (and, in `normal`, would resume what the live keeper suspended). A test of the
    hold itself passes its own tmp root and runs the real pass."""
    from pathlib import Path

    from tools.agentops import hold

    real, root_of_repo = hold.keeper_pass, Path(hold.ROOT).resolve()

    def guarded(mode, root=hold.ROOT, **kwargs):
        if Path(root).resolve() == root_of_repo:
            return {"suspended": [], "resumed": []}
        return real(mode, root, **kwargs)

    monkeypatch.setattr(hold, "keeper_pass", guarded)


@pytest.fixture(autouse=True, scope="session")
def _agentops_run_log_in_tmp(tmp_path_factory: pytest.TempPathFactory):
    """No test writes the real `artifacts/runs/agentops.jsonl` (issue #256).

    `tools.agentops.metering.record_run` falls back to `DEFAULT_LOG` when no `log_path` is
    given, and `run_research` / the GPT worker call it that way, so three research tests had
    added 289 fixture rows to the real log (shared by every worktree through the junction).
    The default is pointed at a temporary file for the whole session.
    """
    log = tmp_path_factory.mktemp("agentops_log") / "agentops.jsonl"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("tools.agentops.metering.DEFAULT_LOG", log)
        yield log


@pytest.fixture(autouse=True)
def _gh_calls_log_in_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """#148 rule 4: `gh_call.run_gh` appends one line per attempted call to `var/agentops/gh_calls.jsonl`, and
    `var/` is linked to the main checkout's, so the older rate-limit tests (which run `run_gh` with a fake `gh`)
    wrote 30 fake lines into the owner's real log. Every test gets a log of its own."""

    monkeypatch.setattr("tools.agentops.gh_call.GH_CALLS_LOG", tmp_path / "gh_calls_test.jsonl")


@pytest.fixture(autouse=True)
def _suite_reservations_in_tmp(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """#397: no test claims workers in the live `var/suite_workers.json` (the merge suite's and the full
    run's shared reservation); every test has its own file."""
    monkeypatch.setenv("AGENTOPS_SUITE_RESERVATIONS", str(tmp_path_factory.mktemp("suite_res") / "suite_workers.json"))


@pytest.fixture(autouse=True)
def _no_laptop_suite(monkeypatch: pytest.MonkeyPatch):
    """No test reads the live laptop (#457, #685): the merge's laptop suite route is off for every
    test; a test of that route deletes the variable and injects fakes."""
    monkeypatch.setenv("JUNKROAN_NO_LAPTOP_SUITE", "1")


from tests._merge_paths import _agentops_merge_paths_in_tmp  # noqa: F401


@pytest.fixture(autouse=True)
def _merge_reads_a_quiet_machine(monkeypatch: pytest.MonkeyPatch):
    """#223: the merge waits for a quiet machine (up to 10 min) before it re-checks failed tests, by readings of the
    real CPU and the real pytest sessions, so a test whose merge goes through a re-check would wait for whatever else
    runs on the PC. Every test reads a quiet machine; a test of the wait injects its own readings."""
    import time

    from tools.agentops import merge, merge_load

    monkeypatch.setattr(merge, "_machine_reading",
                        lambda: merge_load.LoadReading(cpu_percent=0.0, runs=0, heavy_runs=0, at=time.time()))


@pytest.fixture(autouse=True)
def _modes_file_in_tmp(monkeypatch, tmp_path):
    """#376: the mode store resolves to the MAIN checkout's var/modes.json (`hosts.modes_path`) even
    from a worktree; a test that reaches `record_mode` / `apply_timed_mode` (or starts `runs.py mode`
    as a subprocess, which inherits this env) must never write the owner's real file."""

    monkeypatch.setenv("JUNKROAN_MODES_FILE", str(tmp_path / "modes.json"))


@pytest.fixture(autouse=True)
def _reservations_file_in_tmp(monkeypatch, tmp_path):
    """#411: `slot_budget.reservations_file()` falls back to the MAIN checkout's live
    var/reservations.json; with `strict_reserve` on, a live `runs reserve` is added to `others` and
    changed slot-budget limits under test (and `machine_load.py` subprocesses inherit this env). Every
    test reads an empty reservations file of its own unless it writes one or passes a path."""

    monkeypatch.setenv("JUNKROAN_POB_RESERVATIONS", str(tmp_path / "no_live_reservations.json"))


@pytest.fixture(autouse=True)
def _runs_events_in_tmp(monkeypatch, tmp_path):
    """#138: `hosts.events_path()` resolves to the MAIN checkout's artifacts/runs/events.jsonl even from a
    worktree, and that feed drives the anomaly detector that starts keymaster sessions; a test that
    appends an event (`echo x`, `wrong-host`, `keymaster_start`) must never write the live one."""

    monkeypatch.setenv("JUNKROAN_RUNS_EVENTS", str(tmp_path / "events.jsonl"))


import os as _os


import subprocess as _subprocess


_SSH_REFUSAL = "real host call from a test: fake run_on_host"


_REAL_RUN = _subprocess.run


_REAL_POPEN = _subprocess.Popen


def _is_ssh_call(args) -> bool:
    """True when `args` (a Popen/run argv or a shell string) starts an ssh client."""

    if isinstance(args, (str, bytes, _os.PathLike)):
        text = _os.fsdecode(args).strip()
        first = text.split(None, 1)[0] if text else ""
    else:
        try:
            first = _os.fsdecode(args[0]) if args else ""
        except (TypeError, IndexError):
            return False
    return _os.path.basename(first.replace("\\", "/")).lower() in ("ssh", "ssh.exe")


class _SshGuardState:
    """`real_run` is the function beneath the guard (a test may swap it for a recorder);
    `passed` records every argv the guard let through."""

    def __init__(self) -> None:
        self.real_run = _REAL_RUN
        self.passed: list = []


class _GuardedPopen(_REAL_POPEN):
    def __init__(self, args, *a, **kw):
        if _is_ssh_call(args):
            raise RuntimeError(_SSH_REFUSAL)
        super().__init__(args, *a, **kw)


@pytest.fixture(autouse=True)
def ssh_guard(monkeypatch: pytest.MonkeyPatch) -> _SshGuardState:
    """No test reaches a real host over ssh (#451): on 2026-10-01 `runs.py mode laptop off` with no
    fake switched the real laptop to `off`. `subprocess.run` / `Popen` raise before spawning when the
    argv is ssh; a test's own later monkeypatch (of `run_on_host`, an injected transport, or
    `subprocess.run`) replaces the guard and still works."""

    state = _SshGuardState()

    def guarded_run(*popenargs, **kwargs):
        args = popenargs[0] if popenargs else kwargs.get("args")
        if _is_ssh_call(args):
            raise RuntimeError(_SSH_REFUSAL)
        state.passed.append(args)
        return state.real_run(*popenargs, **kwargs)

    monkeypatch.setattr(_subprocess, "run", guarded_run)
    monkeypatch.setattr(_subprocess, "Popen", _GuardedPopen)
    return state


@pytest.fixture(autouse=True)
def _no_merge_test_selection(monkeypatch):
    """#690: `merge_select_tests` is true in config/orchestration.json; a test that lands a pytest-shaped
    suite through `run_merge` must neither select tests nor spawn a real `map-run`. A test of the
    selection turns it on by patching `tools.agentops.merge.resolve_select_tests`."""

    try:
        import tools.agentops.merge as merge
    except ImportError:
        return
    monkeypatch.setattr(merge, "resolve_select_tests", lambda: False)


@pytest.fixture(autouse=True)
def _commands_folder_in_tmp(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """#100: the default slash-command folder is a temp folder for every test. `install_folder.commands_dir()` is the
    default of `mods.run_install` and of the port's install, so no test writes the owner's `~/.claude/commands`
    (a test of a file there sets its own folder or home)."""
    from tools.agentops import install_folder

    folder = tmp_path_factory.mktemp("commands") / "commands"
    monkeypatch.setattr(install_folder, "commands_dir", lambda: folder)


@pytest.fixture(autouse=True)
def _mods_folder_in_tmp(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """#173: the install root (`install_folder.install_root()`, env `AGENTOPS_MODS_DIR`, default `~/.claude/mods`) is a
    temp folder for every test, so no test writes the owner's `~/.claude/mods`. A test that sets its own value (or
    deletes it) overrides this, as monkeypatch allows."""
    monkeypatch.setenv("AGENTOPS_MODS_DIR", str(tmp_path_factory.mktemp("mods") / "mods"))


@pytest.fixture(scope="session", autouse=True)
def _real_commands_folder_untouched():
    """#100 guard: the owner's real commands folder keeps its entries (names, mtimes, sizes) through the whole run;
    any change fails the session (an error at teardown). Its helpers live in tests/_commands_guard.py."""
    before = snapshot(REAL_COMMANDS)
    yield
    moved = changes(before, snapshot(REAL_COMMANDS))
    assert not any(moved.values()), f"the owner's real commands folder {REAL_COMMANDS} changed during the run: {moved}"


@pytest.fixture(scope="session", autouse=True)
def _real_mods_folder_names_untouched(request: pytest.FixtureRequest):
    """#173 guard: the owner's real mods folder keeps its set of top-level mod folder names through the whole run
    (taken at the session's start, compared at its end). A test that creates or removes a mod folder there (the
    `some-repo-monitor` incident) fails the session, naming the folders. File content, mtimes and sizes under a mod
    folder are not compared: the live monitors rewrite their caches. Helpers: tests/_commands_guard.py.
    #210: a change that the real mods journal explains (an `install` or `remove` line for that name, dated inside the
    session) is not an error; it is written as one note line. Any other change still fails the session."""
    start = datetime.now()  # before the snapshot, so an install between the two is dated inside the session
    before = mod_folder_names(REAL_MODS)
    yield
    after = mod_folder_names(REAL_MODS)
    end = datetime.now()  # after the closing snapshot, for the same reason
    moved = name_changes(before, after)
    excused, unexplained = unexplained_changes(moved, install_folder.journal_entries(REAL_JOURNAL), start, end)
    note = note_line(excused)
    if note is not None:
        reporter = request.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(note)
        else:
            print(note)
    assert not any(unexplained.values()), f"the owner's real mods folder {REAL_MODS} changed during the run: {unexplained}"


