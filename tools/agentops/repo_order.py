"""#233 unit 1: the owner's repo order on this PC, one file outside every repo.

`<machine_dir>/repo_order.json` = a JSON list of repo folder names, first = served first. A repo absent from a
non-empty list ranks after every listed one; an empty, missing or unreadable file is "no ranking" (`rank` returns
`(0, 0)`). Each real change appends one line `{"time", "old", "new"}` to `<machine_dir>/repo_order.jsonl`.

`python -m tools.agentops repo-order set <name> ...` sets it (no names = empty order), `repo-order show` prints it.
"""

from __future__ import annotations

from pathlib import Path

from .machine_mode import machine_dir
from .util import AgentOpsError, append_jsonl, now, read_json, write_json

ORDER_FILE = "repo_order.json"
LOG_FILE = "repo_order.jsonl"
EMPTY_LINE = "(empty: today's order)"


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
    """(position, N): the 0-based place of `repo_name` in the order and N = len(order). A repo absent from a
    non-empty order gets position N. An empty order gives (0, 0): no ranking."""
    order = get()
    n = len(order)
    if n == 0:
        return (0, 0)
    folded = [name.casefold() for name in order]  # folder names are case-insensitive on this PC
    try:
        return (folded.index(repo_name.casefold()), n)
    except ValueError:
        return (n, n)


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
    for name in order:
        print(name)


def run_repo_order_command(args) -> int:
    if args.repo_order_action == "set":
        set_order(args.names)
    _print_order()
    return 0
