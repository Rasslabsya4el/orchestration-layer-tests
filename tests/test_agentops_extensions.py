"""tools/agentops/extensions.py (issue #150, ADR 0109 extension point 4): the loader that joins an
installation's own modules to the core. Every test drives a fresh `Registry` with modules written
into `tmp_path`, never this repo's five, and touches no `gh`, no `claude.exe` and no heavy tool.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

from tools.agentops.extensions import Registry


def _module(tmp_path: Path, name: str, body: str) -> str:
    """Write a local module into `tmp_path` and make it importable; returns its import name."""

    (tmp_path / f"{name}.py").write_text(body, encoding="utf-8")
    if str(tmp_path) not in sys.path:
        sys.path.insert(0, str(tmp_path))
    sys.modules.pop(name, None)
    return name


def test_no_module_named_registers_nothing(tmp_path: Path):
    core = Registry()

    assert core.load([]) == []
    assert (core.commands, core.ticks, core.candidate_steps, core.setups, core.starts) == ([], [], [], [], [])


def test_a_module_without_register_is_one_error_naming_it(tmp_path: Path):
    name = _module(tmp_path, "local_no_register", "VALUE = 1\n")
    core = Registry()

    with pytest.raises(RuntimeError) as exc:
        core.load([name])

    assert "local_no_register" in str(exc.value) and "register(core)" in str(exc.value)


def test_load_is_idempotent(tmp_path: Path):
    name = _module(tmp_path, "local_counting", """
CALLS = []


def register(core):
    CALLS.append(1)
    core.candidates(lambda d, c, *, offering: c)
""")
    core = Registry()

    assert core.load([name]) == [name]
    assert core.load([name]) == [name]
    assert len(core.candidate_steps) == 1


def test_a_command_is_listed_after_the_core_command_it_names(tmp_path: Path):
    name = _module(tmp_path, "local_cmd", """
def _add(sub):
    sub.add_parser("mine", help="a local command")


def _add_late(sub):
    sub.add_parser("late", help="an anchor the core does not have")


def register(core):
    core.command("mine", _add, lambda args: 7, after="dispatch")
    core.command("late", _add_late, lambda args: 9, after="nothing-like-this")
""")
    core = Registry()
    core.load([name])
    ap = argparse.ArgumentParser(prog="t")
    sub = ap.add_subparsers(dest="command", required=True)
    added: set[str] = set()

    sub.add_parser("worker")
    core.add_commands(sub, after="worker", added=added)
    sub.add_parser("dispatch")
    core.add_commands(sub, after="dispatch", added=added)
    sub.add_parser("core")
    core.add_commands(sub, after="core", added=added)
    core.add_remaining_commands(sub, added=added)

    assert list(sub.choices) == ["worker", "dispatch", "mine", "core", "late"]
    assert core.run_command(ap.parse_args(["mine"])) == 7
    assert core.run_command(ap.parse_args(["dispatch"])) is None  # a core command: not ours


def test_a_guarded_tick_step_logs_its_failure_and_the_tick_goes_on(tmp_path: Path):
    core = Registry()
    ran: list[str] = []
    core.tick("my step", lambda d, r: (_ for _ in ()).throw(RuntimeError("boom")), guard=True)
    core.tick("second", lambda d, r: ran.append("second"), guard=True)

    class FakeDispatcher:
        log = staticmethod(ran.append)

    core.run_ticks(FakeDispatcher(), object(), phase="before")

    assert ran == ["my step tick failed (RuntimeError('boom')); continuing", "second"]


def test_an_unguarded_tick_step_raises_like_a_core_step(tmp_path: Path):
    core = Registry()
    core.tick("mine", lambda d, r: (_ for _ in ()).throw(RuntimeError("boom")), phase="after")

    with pytest.raises(RuntimeError):
        core.run_ticks(object(), object(), phase="after")


def test_only_the_steps_of_the_asked_phase_run(tmp_path: Path):
    core = Registry()
    ran: list[str] = []
    core.tick("before one", lambda d, r: ran.append("before"), phase="before")
    core.tick("after one", lambda d, r: ran.append("after"), phase="after")

    core.run_ticks(object(), object(), phase="after")

    assert ran == ["after"]


def test_an_unknown_tick_phase_is_refused_at_registration():
    with pytest.raises(ValueError):
        Registry().tick("mine", lambda d, r: None, phase="sometime")


def test_candidate_steps_run_in_registration_order_with_the_offering_flag():
    core = Registry()
    seen: list[bool] = []
    core.candidates(lambda d, c, *, offering: seen.append(offering) or [n for n in c if n != 2])
    core.candidates(lambda d, c, *, offering: list(reversed(c)))

    assert core.run_candidates(object(), [1, 2, 3], offering=True) == [3, 1]
    assert seen == [True]


def test_a_method_is_added_to_a_core_class_but_never_replaces_one():
    class Core:
        def existing(self):
            return "core"

    core = Registry()
    core.method(Core, "mine", lambda self: "local")

    assert Core().mine() == "local" and core.methods == [("Core", "mine")]
    with pytest.raises(RuntimeError):
        core.method(Core, "existing", lambda self: "shadow")


def test_setup_and_start_run_for_every_module_in_order():
    core = Registry()
    calls: list[str] = []
    core.setup(lambda d: calls.append("setup-a"))
    core.setup(lambda d: calls.append("setup-b"))
    core.start(lambda d, args: calls.append(f"start {args}"))

    core.run_setup(object())
    core.run_start(object(), "ARGS")

    assert calls == ["setup-a", "setup-b", "start ARGS"]


def test_a_bad_local_modules_value_in_the_settings_file_is_one_error(monkeypatch):
    import tools.agentops.extensions as extensions

    monkeypatch.setattr(extensions, "_load_settings", lambda: {"local_modules": "cloud"})

    with pytest.raises(RuntimeError) as exc:
        Registry().load()

    assert "local_modules" in str(exc.value)
