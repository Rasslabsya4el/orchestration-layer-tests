"""The loader for an installation's local modules (ADR 0109, extension point 4).

The core's dispatcher and CLI import no module of the repo they run in. At start they
load the modules named in ``local_modules`` of the settings file (``core_settings``)
and hand each one's ``register(core)`` a registry of the points it may join:

* ``command``: a CLI subcommand of its own, placed after a named core command, so the
  listing of ``--help`` stays in the installation's own order;
* ``dispatch_flag``: a flag on the ``dispatch`` command;
* ``tick``: a step in the dispatcher's tick, before or after the ORCH step, with or
  without a guard that keeps its failures out of the loop;
* ``candidates``: the ready queue -- a module may drop issues from it, order it, and,
  on the call that is about to start a session (``offering``), log what it holds back;
* ``setup`` / ``start``: state of its own on every ``Dispatcher``, and the wiring of
  the real one, which is where it reads its own flag off the parsed arguments;
* ``method``: a method of its own on a core class (``Dispatcher``, ``GhCli``,
  ``ClaudeCli``), which is how a module keeps a whole feature -- its label, its
  sessions, its own ``gh`` reads -- in one file of its own while the core stays unaware
  that it exists.

With ``local_modules`` empty nothing is imported and every point above is a no-op: the
dispatcher runs alone (one label, the session start, the held issues, the end detection,
the wake). Stdlib only, like every module the core ships with.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Callable

from .core_settings import load as _load_settings

#: `(subparsers) -> None`: a local command's own parser.
AddParser = Callable[[Any], None]
#: `(args) -> exit code`: what that command does.
RunCommand = Callable[[Any], int]
#: `(dispatcher, report) -> None`: one step of the tick.
TickStep = Callable[[Any, Any], None]
#: `(dispatcher, candidates, *, offering) -> candidates`.
CandidatesStep = Callable[..., list]


@dataclass
class _Command:
    name: str
    add: AddParser
    run: RunCommand
    #: the core command this one is listed after; `None` (or an unknown name) lists it last.
    after: str | None = None


@dataclass
class _Tick:
    #: the module's own name for this step, used verbatim in the log line of a guarded failure.
    name: str
    fn: TickStep
    #: `"before"` the ORCH step of the tick, or `"after"` it.
    phase: str = "before"
    #: `True`: an exception is logged and the tick goes on (the step is not allowed to end the
    #: dispatcher's loop). `False`: it propagates, exactly as a core step's would.
    guard: bool = False


class Registry:
    """What the loaded local modules added. One instance per process (`registry` below)."""

    def __init__(self) -> None:
        #: the module names loaded, or `None` while nothing has been loaded yet.
        self.loaded: list[str] | None = None
        self.commands: list[_Command] = []
        self.dispatch_flags: list[AddParser] = []
        self.ticks: list[_Tick] = []
        self.candidate_steps: list[CandidatesStep] = []
        self.setups: list[Callable[[Any], None]] = []
        self.starts: list[Callable[[Any, Any], None]] = []
        #: `(class name, method name)` per contributed method, for whoever wants to see them.
        self.methods: list[tuple[str, str]] = []

    # -- what a local module's `register(core)` calls ---------------------------------------------

    def command(self, name: str, add: AddParser, run: RunCommand, *, after: str | None = None) -> None:
        self.commands.append(_Command(name=name, add=add, run=run, after=after))

    def dispatch_flag(self, add: AddParser) -> None:
        self.dispatch_flags.append(add)

    def tick(self, name: str, fn: TickStep, *, phase: str = "before", guard: bool = False) -> None:
        if phase not in ("before", "after"):
            raise ValueError(f"unknown tick phase {phase!r} (expected 'before' or 'after')")
        self.ticks.append(_Tick(name=name, fn=fn, phase=phase, guard=guard))

    def candidates(self, fn: CandidatesStep) -> None:
        self.candidate_steps.append(fn)

    def setup(self, fn: Callable[[Any], None]) -> None:
        self.setups.append(fn)

    def start(self, fn: Callable[[Any, Any], None]) -> None:
        self.starts.append(fn)

    def method(self, cls: type, name: str, fn: Callable) -> None:
        """Give a core class one method of the module's own. Refuses to replace an existing
        attribute: two modules never silently overwrite each other, and no module shadows a core
        method by accident."""

        if hasattr(cls, name):
            raise RuntimeError(f"{cls.__name__}.{name} already exists: a local module may only add")
        setattr(cls, name, fn)
        self.methods.append((cls.__name__, name))

    # -- what the core calls ----------------------------------------------------------------------

    def load(self, names: list[str] | None = None) -> list[str]:
        """Import every named module and call its `register(core)`. Idempotent: the second call
        returns the same list without importing anything again. A name starting with `.` is
        resolved inside this package; any other is an absolute import path."""

        if self.loaded is not None:
            return self.loaded
        if names is None:
            names = _local_module_names()
        loaded: list[str] = []
        for name in names:
            module = importlib.import_module(name, package=__package__ if name.startswith(".") else None)
            register = getattr(module, "register", None)
            if register is None:
                raise RuntimeError(f"local module {name!r} has no register(core) function")
            register(self)
            loaded.append(module.__name__)
        self.loaded = loaded
        return loaded

    def add_commands(self, sub, *, after: str, added: set[str]) -> None:
        """Add every local command listed after the core command `after`, in registration order.
        `added` is the caller's own set for one parser, so a second parser built in the same process
        (a test calling `main()` twice) gets every command again."""

        for command in self.commands:
            if command.name in added or command.after != after:
                continue
            command.add(sub)
            added.add(command.name)

    def add_remaining_commands(self, sub, *, added: set[str]) -> None:
        """Add whatever is left: a command with no anchor, or one naming a command that does not
        exist here. Called last, so an unknown anchor lists the command rather than losing it."""

        for command in self.commands:
            if command.name in added:
                continue
            command.add(sub)
            added.add(command.name)

    def add_dispatch_flags(self, parser) -> None:
        for add in self.dispatch_flags:
            add(parser)

    def run_command(self, args) -> int | None:
        """The exit code of the local command named by `args.command`, or `None` when no local
        module owns that name (the core's own commands)."""

        for command in self.commands:
            if command.name == getattr(args, "command", None):
                return command.run(args)
        return None

    def run_ticks(self, dispatcher, report, *, phase: str) -> None:
        for hook in self.ticks:
            if hook.phase != phase:
                continue
            if not hook.guard:
                hook.fn(dispatcher, report)
                continue
            try:
                hook.fn(dispatcher, report)
            except Exception as exc:  # a guarded step must never end the dispatcher's loop
                dispatcher.log(f"{hook.name} tick failed ({exc!r}); continuing")

    def run_candidates(self, dispatcher, candidates: list, *, offering: bool) -> list:
        for step in self.candidate_steps:
            candidates = list(step(dispatcher, candidates, offering=offering))
        return candidates

    def run_setup(self, dispatcher) -> None:
        for fn in self.setups:
            fn(dispatcher)

    def run_start(self, dispatcher, args) -> None:
        for fn in self.starts:
            fn(dispatcher, args)


def _local_module_names() -> list[str]:
    settings = _load_settings()
    names = settings.get("local_modules")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise RuntimeError("invalid 'local_modules' in the settings file: expected a list of "
                           "module names (a dotted path, or '.name' inside this package)")
    return list(names)


#: the one registry of the process; `load()` fills it from the settings file.
registry = Registry()


def load(names: list[str] | None = None) -> list[str]:
    return registry.load(names)


__all__ = ["Registry", "load", "registry"]
