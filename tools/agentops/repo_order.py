"""#233 unit 1: the owner's repo order on this PC, one file outside every repo.

`<machine_dir>/repo_order.json` = a JSON list of repo names (GitHub names, #239; an order name matches a repo name
whole or up to a dash, `repo_identity.matches`), first = served first. A repo absent from a non-empty list ranks after
every listed one; an empty, missing or unreadable file is "no ranking" (`rank` returns `(0, 0)`). Each real change
appends one line `{"time", "old", "new"}` to `<machine_dir>/repo_order.jsonl`.

`python -m tools.agentops repo-order set <name> ...` sets it (no names = empty order), `repo-order show` prints it, each
name with the repo it matched. `set` refuses a name that matches no repo of this one or its `targets` (#239).
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import core_settings, repo_identity
from .machine_mode import machine_dir
from .util import AgentOpsError, append_jsonl, now, read_json, write_json

ORDER_FILE = "repo_order.json"
LOG_FILE = "repo_order.jsonl"
EMPTY_LINE = "(empty: today's order)"
#: the repo this order is read in (#239: `set` and `show` name its repo and its targets); a test points it elsewhere.
THIS_REPO = core_settings.ROOT


def _order_path() -> Path:
    return machine_dir() / ORDER_FILE


def _log_path() -> Path:
    return machine_dir() / LOG_FILE


def get() -> list[str]:
    """The ordered repo folder names; a missing, empty or unreadable file gives []."""
    try:
        data = read_json(_order_path(), default=[])
    except (AgentOpsError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return [str(name) for name in data]


def rank(repo_name: str) -> tuple[int, int]:
    """(position, N): the 0-based place of `repo_name` (a GitHub repo name, #239) in the order and N = len(order). The
    first order name that matches it (`repo_identity.matches`) gives the place. A repo absent from a non-empty order
    gets position N. An empty order gives (0, 0): no ranking."""
    order = get()
    n = len(order)
    if n == 0:
        return (0, 0)
    for position, name in enumerate(order):
        if repo_identity.matches(name, repo_name):
            return (position, n)
    return (n, n)


def _known() -> list[str]:
    return repo_identity.known_names(THIS_REPO)


def unknown_names(names: list[str]) -> list[str]:
    """The names that match no repo of this one or its targets (#239): `set` refuses them."""
    known = _known()
    return [name for name in names if repo_identity.matched_name(name, known) is None]


def set_order(names: list[str]) -> None:
    """Write the order; a change (and only a change) appends one log line with the old and the new list."""
    new = [str(name) for name in names]
    old = get()
    if new == old:
        return
    write_json(_order_path(), new)
    append_jsonl(_log_path(), {"time": now(), "old": old, "new": new})


def add_subparser(sub) -> None:
    parser = sub.add_parser("repo-order", help="the repo order on this PC: set <name> ... | show")
    actions = parser.add_subparsers(dest="repo_order_action", required=True)
    set_parser = actions.add_parser("set", help="set the order, first = served first (no names = empty order)")
    set_parser.add_argument("names", nargs="*")
    actions.add_parser("show", help="print the order, one name per line")


def _print_order() -> None:
    order = get()
    if not order:
        print(EMPTY_LINE)
        return
    known = _known()
    for name in order:
        print(f"{name} -> {repo_identity.matched_name(name, known) or '(no repo)'}")


def run_repo_order_command(args) -> int:
    if args.repo_order_action == "set":
        unknown = unknown_names(args.names)
        if unknown:  # #239: one line naming the refused names; the order stays as it was
            print(f"refused: {', '.join(repr(name) for name in unknown)} matches no repo of this one or its targets "
                  f"({', '.join(_known())}); order unchanged", file=sys.stderr)
            return 1
        set_order(args.names)
    _print_order()
    return 0
