"""#267 unit 1: `dispatch` sends the workflow inputs as JSON on stdin (`gh workflow run --json`), never as `-f` argv.

A 1,500-id selection encodes to ~40,000 chars, past Windows' 32,767-char command line, so CreateProcess for `gh.exe`
failed before any request left the PC. Fakes only: no network, no real `gh` process, no real time.
"""
from __future__ import annotations

import base64
import gzip
import json
import random
from datetime import datetime, timedelta, timezone

import pytest

from tools.agentops import gh_call, github_run, workflow_sync
from tools.agentops.util import AgentOpsError

REPO = "owner/copy"
BASE = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)


WORDS = ("queue merge ticket dispatch parked label checkpoint worker branch handoff budget reader writer retry "
         "stale wake card strip pipeline stage owner brain unblock closer limit session count suite part").split()


def fake_ids(count: int) -> list[str]:
    """Node ids of ~100 chars, every one unique, with test names built from seeded random words (the entropy of
    real names, so gzip does not shrink them more than it shrinks a real selection)."""
    rng = random.Random(267)
    ids = []
    for i in range(count):
        name = "_".join(rng.choice(WORDS) for _ in range(rng.randint(5, 9)))
        ids.append(f"tests/test_agentops_{rng.choice(WORDS)}_{rng.choice(WORDS)}.py::test_{i}_{name}[{rng.choice(WORDS)}-{i}]")
    return ids


def decode(value: str) -> list[str]:
    return gzip.decompress(base64.b64decode(value, validate=True)).decode("utf-8").split("\n")


class Launcher:
    """Stands in for the process launcher under `run_gh`: records the argument list and the stdin of every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | bytes | None]] = []

    def __call__(self, args, *, cwd, input, text, check, timeout):
        self.calls.append((list(args), input))
        stdout = ""
        if args[:2] == ["run", "list"]:
            created = (BASE + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            stdout = json.dumps([{"databaseId": 42, "status": "queued", "createdAt": created,
                                  "event": "workflow_dispatch"}])
        return gh_call.GhResult(["gh", *args], 0, stdout, "")


@pytest.fixture
def launcher(monkeypatch):
    fake = Launcher()
    monkeypatch.setattr(gh_call, "_spawn_gh", fake)
    monkeypatch.setattr(gh_call, "_log_gh_call", lambda args, blocked: None)
    return fake


def dispatch(**kw):
    return github_run.dispatch(REPO, "merge-suite/i267", 5, wall=lambda: BASE, sleep=lambda s: None, **kw)


def workflow_call(fake: Launcher):
    return next(call for call in fake.calls if call[0][:2] == ["workflow", "run"])


def test_1500_ids_leave_the_command_line_short_and_arrive_intact_on_stdin(launcher):
    ids = fake_ids(1500)
    assert 90 <= sum(map(len, ids)) / len(ids) <= 120 and len(set(ids)) == 1500
    assert len(github_run.encode_ids(ids)) > 32767  # the old argv value alone passed the Windows limit

    assert dispatch(ids=ids) == 42
    args, stdin = workflow_call(launcher)
    assert args == ["workflow", "run", "tests.yml", "-R", REPO, "--ref", "merge-suite/i267", "--json"]
    assert sum(len(a) for a in args) < 8000
    assert all(sum(len(a) for a in call_args) < 8000 for call_args, _ in launcher.calls)
    body = json.loads(stdin)
    assert decode(body["ids"]) == ids


def test_ref_parts_windows_and_skip_reach_the_body_as_before(launcher):
    skip = ["tests/test_win.py::test_a", "tests/test_win.py::test_b"]
    dispatch(ids=["tests/a.py::t1", "tests/b.py::t2"], windows=True, skip=skip)
    _, stdin = workflow_call(launcher)
    body = json.loads(stdin)
    assert body["ref"] == "merge-suite/i267"
    inputs = body
    assert set(inputs) == {"ref", "parts", "ids", "windows", "skip"}
    assert inputs["ref"] == "merge-suite/i267" and inputs["parts"] == "5" and inputs["windows"] == "true"
    assert decode(inputs["ids"]) == ["tests/a.py::t1", "tests/b.py::t2"]
    assert inputs["skip"] == github_run.encode_ids(skip) and decode(inputs["skip"]) == skip


def test_without_ids_windows_or_skip_the_body_carries_only_ref_and_parts(launcher):
    dispatch()
    _, stdin = workflow_call(launcher)
    assert json.loads(stdin) == {"ref": "merge-suite/i267", "parts": "5"}


def test_only_the_workflow_call_carries_stdin(launcher):
    dispatch(ids=fake_ids(3))
    assert [stdin is not None for _, stdin in launcher.calls] == [True, False]


def test_over_the_payload_limit_raises_with_the_size_and_the_limit_and_sends_nothing(launcher):
    ids = fake_ids(3000)
    with pytest.raises(AgentOpsError) as err:
        dispatch(ids=ids)
    text = str(err.value)
    size = len(github_run.encode_ids(ids)) + len("merge-suite/i267") + len("5")  # ids + ref + parts
    assert f"{github_run.MAX_INPUTS_CHARS:,}" in text and f"{size:,}" in text
    assert size > github_run.MAX_INPUTS_CHARS
    assert launcher.calls == []  # nothing was sent, in particular no truncated list


def test_a_selection_just_under_the_limit_is_sent(launcher):
    ids = fake_ids(2000)
    assert len(github_run.encode_ids(ids)) < github_run.MAX_INPUTS_CHARS
    dispatch(ids=ids)
    assert decode(json.loads(workflow_call(launcher)[1])["ids"]) == ids


@pytest.fixture(autouse=True)
def _workflow_files_in_sync(monkeypatch):
    """#343 rule 1: a dispatch first makes the workflow files exist on the default branch. The tests here are about the
    dispatch's own calls, so the sync is answered as done (tests/test_issue343_workflow_sync.py tests the sync itself)."""
    monkeypatch.setattr(workflow_sync, "prepare_dispatch", lambda *args, **kwargs: [])
