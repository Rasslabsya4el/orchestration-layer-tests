"""pytest plugin (``-p tools.agentops.suite_events_plugin``): the merge suite's live test events (#900).

When the env var ``SUITE_EVENTS_FILE`` names a path, every process that runs tests (the one process without xdist,
each xdist worker; never the xdist controller, which would repeat the workers' calls) appends one line per event
to a file of its own, ``<path>.<worker>``: on Windows an O_APPEND write seeks to the end and then writes, so two
processes appending to ONE file overwrite each other's lines (measured: 2 x 5000 lines gave 9349); one writer per
file cannot lose a line. Each line starts with the time in ns, so the reader puts the workers' lines in order:

    ``<ns> S <worker> <nodeid>``  the test started
    ``<ns> E <worker> <nodeid>``  the test finished (its teardown included)
    ``<ns> F <worker> <nodeid>``  a phase of the test failed (setup, call or teardown)
    ``<ns> R <worker> <nodeid>	<reason>``  (#219) right after an F: the failure's one-line reason

``tools.agentops.suite_events.read_events`` turns the file into the failed ids and the ids a worker started and has not
finished. A killed run leaves the file as it stood: that is why this is not pytest's own end-of-run summary."""
from __future__ import annotations

import os
import time

ENV_EVENTS = "SUITE_EVENTS_FILE"


def _active(config) -> bool:
    if not os.environ.get(ENV_EVENTS):
        return False
    if hasattr(config, "workerinput"):
        return True
    return not getattr(config.option, "numprocesses", None)  # xdist controller: the workers write


def _write(kind: str, nodeid: str) -> None:
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    line = f"{time.time_ns()} {kind} {worker} {nodeid}\n".encode("utf-8", "replace")
    fd = os.open(f"{os.environ[ENV_EVENTS]}.{worker}", os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


_config = {}


def pytest_configure(config):
    _config["c"] = config


def pytest_runtest_logstart(nodeid, location):
    if _active(_config["c"]):
        _write("S", nodeid)


def _reason(report) -> str:
    """The failure's first line (pytest's own crash message), at most 300 characters."""
    crash = getattr(report.longrepr, "reprcrash", None)
    text = getattr(crash, "message", None) or str(report.longrepr)
    return (text.strip().splitlines() or [""])[0].replace("	", " ")[:300]


def pytest_runtest_logreport(report):
    if _active(_config["c"]) and report.failed:
        _write("F", report.nodeid)
        _write("R", f"{report.nodeid}	{_reason(report)}")


def pytest_runtest_logfinish(nodeid, location):
    if _active(_config["c"]):
        _write("E", nodeid)
