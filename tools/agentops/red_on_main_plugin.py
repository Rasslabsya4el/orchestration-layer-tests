"""#319: the tests red on a base before a ticket, skipped by name; and the one parser of their list line.

The list is `tests/red_on_main.txt` in the folder pytest is started in (the repo, or a base worktree). Its lines are
`skip <nodeid or file path> # <reason>`; a `#` line and a blank line are ignored. `red-on-base` writes the list,
this module reads it. tests/conftest.py loads the module by `pytest_plugins`, so a core update never overwrites a
target's list or conftest. Undo: drop the module from `pytest_plugins` and delete the list.

Skip, not xfail: an xfail still runs the body. A file-level skip also stops the file's import, so a module that
raises at import is not collected. When the env var RED_ON_BASE_REPORT names a file, every failed or errored test id
and every failed collection (by file) is appended to it: `red-on-base` reads the reds from there, not from pytest's
output.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

PLUGIN = "tools.agentops.red_on_main_plugin"
LIST_RELATIVE = Path("tests") / "red_on_main.txt"
SEPARATOR = " # "
DEFAULT_REASON = "red on main"
REPORT_ENV = "RED_ON_BASE_REPORT"


def normalise(target: str) -> str:
    """Repo-style id: forward slashes, no surrounding blanks."""
    return target.strip().replace("\\", "/")


def parse_line(raw: str) -> tuple[str, str] | None:
    """`skip <target> # <reason>` -> (target, reason); None for a comment, a blank line or another kind of line.

    The line splits on the first " # " only, so a param id like `test[a#b]` survives; the target is never split on
    " - " (a param id may hold one)."""
    body = raw.strip()
    if body.endswith(" #"):
        body = body[:-2]
    if not body or body.startswith("#"):
        return None
    head, _, reason = body.partition(SEPARATOR)
    kind, _, target = head.strip().partition(" ")
    if kind != "skip" or not target.strip():
        return None
    return normalise(target), reason.strip()


def format_line(target: str, reason: str) -> str:
    """The line `parse_line` reads back: `skip <target> # <reason>`."""
    return f"skip {normalise(target)}{SEPARATOR}{reason}"


def read_entries(text: str) -> list[tuple[str, str]]:
    """Every skip line of a list file's text, in file order."""
    return [entry for entry in (parse_line(raw) for raw in text.splitlines()) if entry is not None]


def list_path(config) -> Path:
    """The list file of the folder pytest is started in."""
    return Path(config.invocation_params.dir) / LIST_RELATIVE


def _marks(config) -> dict[str, str]:
    if os.environ.get(REPORT_ENV):  # the red-on-base run itself: a test listed before is still red on the base
        return {}
    path = list_path(config)
    if not path.is_file():
        return {}
    return {target: reason or DEFAULT_REASON
            for target, reason in read_entries(path.read_text(encoding="utf-8"))}


def pytest_ignore_collect(collection_path, config):
    """A path listed as a file-level skip is not collected: a module that raises at import does not fail the run."""
    marks = _marks(config)
    if not marks:
        return None
    try:
        rel = normalise(os.path.relpath(collection_path, config.invocation_params.dir))
    except ValueError:  # another drive: cannot be listed
        return None
    return True if rel in marks else None


def pytest_collection_modifyitems(config, items):
    marks = _marks(config)
    if not marks:
        return
    for item in items:
        nodeid = normalise(item.nodeid)
        reason = marks.get(nodeid, marks.get(nodeid.split("::", 1)[0]))
        if reason is not None:
            item.add_marker(pytest.mark.skip(reason=reason))


def _record(nodeid: str) -> None:
    report = os.environ.get(REPORT_ENV)
    if report:
        with open(report, "a", encoding="utf-8") as handle:
            handle.write(normalise(nodeid) + "\n")


def pytest_runtest_logreport(report):
    if report.failed:  # a failed call, or an ERROR in setup or teardown (a fixture)
        _record(report.nodeid)


def pytest_collectreport(report):
    if report.failed:  # a module that raises at import: its file path is the id
        _record(report.nodeid)
