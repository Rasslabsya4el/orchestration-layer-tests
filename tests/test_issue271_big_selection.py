"""#271 unit 4: a selection whose encoded inputs pass GitHub's input limit runs the full suite on GitHub, not the PC.

Seam: `merge._run_suite_placed` with the real `merge_github.run` and the real `github_run.dispatch`; only `gh` (the
command adapter), the test copy send, the berth, the poll and the PC suite are fakes. The GitHub run is judged by the
inputs its `workflow run` call sends on stdin. Row 2's second clause: `merge_github.goes_to_pc` on a red result.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import types
from datetime import datetime, timezone

from tools.agentops import core_settings, github_run, merge, merge_github, merge_machine
from tools.agentops import github_suite as gs
from tools.agentops.test_copy_send import SendResult

COPY = "https://github.com/owner/copy-repo.git"
SETTINGS = {"github_suite": True, "test_copy_remote": COPY, "repo": "owner/orch", "github_machines_per_merge": 5,
            "github_machines_total": 20}
GREEN = {"run_id": 77, "parts": [{"part": 1, "seconds": 61}], "minutes_used": 4, "outcome": "green",
         "failed_ids": [], "full_recheck": False}


def ids_of(n: int) -> list[str]:
    """Fake ids of about 100 characters each: a shared path prefix and a per-id hash, so the gzip of the list is not tiny
    (a real selection compresses about the same way)."""
    return [f"tests/test_selection_module_with_a_long_name_{i % 40:02d}.py::TestCase{i}::test_case_"
            + hashlib.md5(str(i).encode()).hexdigest() for i in range(n)]


class FakeGh:
    """The `gh` adapter: answers the workflow dispatch and the run listing, and keeps every call with its stdin."""

    def __init__(self):
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, args, input=None):
        self.calls.append((list(args), input))
        if args[:2] == ["workflow", "run"]:
            return ""
        if args[:2] == ["run", "list"]:
            created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return json.dumps([{"databaseId": 77, "status": "queued", "createdAt": created,
                                "event": "workflow_dispatch"}])
        raise AssertionError(f"unexpected gh call {args}")

    def dispatched_inputs(self) -> list[dict]:
        return [json.loads(body) for args, body in self.calls if args[:2] == ["workflow", "run"]]


def deps_with(gh: FakeGh) -> gs.Deps:
    class Berth:
        def __init__(self, machines):
            self.machines = machines

        def release(self):
            pass

    return gs.Deps(
        send=lambda root, branch, remote: SendResult(True, "abc"),
        acquire_machines=lambda repo, run, want=None, **kw: Berth(want or 5),
        dispatch=lambda repo, branch, parts, **kw: github_run.dispatch(repo, branch, parts, gh, sleep=lambda s: None,
                                                                       **kw),
        poll=lambda repo, run_id, **kw: GREEN,
        record=lambda rec: None,
        clock=lambda: 1.0,
    )


def run_placed(tmp_path, monkeypatch, ids: list[str], gh: FakeGh, *, pc_only=None, run_readers=None):
    """A selected run of ``ids`` through `_run_suite_placed`; returns (the result, the PC suite's calls). A PC suite
    call runs its body, so a reader run is seen through ``run_readers``."""
    monkeypatch.setattr(core_settings, "load", lambda path=None: dict(SETTINGS))
    monkeypatch.setattr(merge_machine, "run_size", lambda tests_line, cwd: (len(ids), False))
    monkeypatch.setattr(merge_github, "DEPS", deps_with(gh))
    heavy: list[tuple] = []

    def fake_heavy(*args, **kwargs):
        heavy.append(args)
        return args[4]()  # the body: the suite's own run, or the readers' run on the PC

    monkeypatch.setattr(merge, "_run_heavy_suite", fake_heavy)
    lock = types.SimpleNamespace(heartbeat=lambda: None)
    run = merge._run_suite_placed(tmp_path, "i271-u4", "a" * 40, tmp_path, True, f"selected {len(ids)} of 4000",
                                  False, lambda: merge.SuiteRun(True, "", 0), tmp_path / "out.txt",
                                  tmp_path / "merge.log", lock, (), ids=list(ids), pc_only=pc_only,
                                  run_readers=run_readers)
    return run, heavy


# -- row 4: a selection too big for one dispatch runs the full suite on GitHub ----------------------------------------

def test_the_3000_id_fixture_is_over_the_input_limit():
    assert len(github_run.encode_ids(ids_of(3000))) > github_run.MAX_INPUTS_CHARS


def test_a_selection_too_big_for_one_dispatch_runs_the_full_suite_on_github(tmp_path, monkeypatch):
    ids = ids_of(3000)
    gh = FakeGh()
    run, heavy = run_placed(tmp_path, monkeypatch, ids, gh)
    assert heavy == []  # the PC runs nothing
    sent = gh.dispatched_inputs()
    assert len(sent) == 1 and "ids" not in sent[0]  # the GitHub run carries no ids input: the full suite
    assert sent[0]["parts"] == "5"
    assert run.ok
    assert run.place.startswith("suite on GitHub: 5 machines")
    assert run.place.endswith(": selection too big for one dispatch (3000 ids), full suite")


def test_a_selection_that_fits_still_goes_to_github_as_ids(tmp_path, monkeypatch):
    ids = ids_of(1500)
    assert len(github_run.encode_ids(ids)) <= github_run.MAX_INPUTS_CHARS
    gh = FakeGh()
    run, heavy = run_placed(tmp_path, monkeypatch, ids, gh)
    assert heavy == []
    sent = gh.dispatched_inputs()
    assert len(sent) == 1 and sent[0]["parts"] == "2"
    assert gzip.decompress(base64.b64decode(sent[0]["ids"])).decode("utf-8").split("\n") == ids
    assert run.ok and run.place.startswith("suite on GitHub: 2 machines")
    assert "too big" not in run.place


def test_inputs_fit_tells_the_two_sizes_apart():
    ref = merge_github.ref_name("i271-u4")
    assert github_run.inputs_fit(ref, 5, ids=ids_of(1500)) is True
    assert github_run.inputs_fit(ref, 5, ids=ids_of(3000)) is False
    assert github_run.inputs_fit(ref, 5) is True


# -- row 2, second clause: a red that names no id goes to the PC, a red that names one stays on GitHub -----------------

def red(failed_ids: list[str]) -> gs.GithubSuiteResult:
    return gs.GithubSuiteResult(status="red", failed_ids=failed_ids, full_recheck=False)


def test_a_red_naming_a_failed_id_does_not_go_to_the_pc():
    assert merge_github.goes_to_pc(red(["tests/x.py::y"])) is False


def test_a_red_with_no_ids_goes_to_the_pc():
    assert merge_github.goes_to_pc(red([])) is True


# -- #271 review: a too-big selection is replaced by the full run, its readers and its windows ---------------------

PC_FILE = "tests/test_pc_only.py"
FULL_READERS = [f"{PC_FILE}::test_reads_selected_none", f"{PC_FILE}::test_reads_unselected"]


def test_the_too_big_full_suite_skips_every_reader_and_asks_windows(tmp_path, monkeypatch):
    """(a): the reader list is the full run's (a reader no selected id names is in it), and the full dispatch asks
    for the Windows job; the PC runs every reader and nothing else."""
    ids = ids_of(3000)
    gh = FakeGh()

    def readers_of(ids_, pc_files, suite_dir, root):
        return list(FULL_READERS) if ids_ is None else [i for i in ids_ if i in FULL_READERS]

    monkeypatch.setattr(merge, "_pc_readers", readers_of)
    ran: list[list[str]] = []

    def read_on_pc(names):
        ran.append(list(names))
        return True, ""

    run, heavy = run_placed(tmp_path, monkeypatch, ids, gh, pc_only=lambda: [PC_FILE], run_readers=read_on_pc)
    sent = gh.dispatched_inputs()
    assert len(sent) == 1 and "ids" not in sent[0]
    assert sent[0]["windows"] == "true"
    assert gzip.decompress(base64.b64decode(sent[0]["skip"])).decode("utf-8").split("\n") == FULL_READERS
    assert ran == [FULL_READERS] and len(heavy) == 1
    assert run.place.startswith("suite on GitHub")
    assert "selection too big for one dispatch (3000 ids), full suite" in run.place


def test_a_selection_within_four_characters_of_the_limit_with_a_pc_only_id_is_too_big(tmp_path, monkeypatch):
    """(b): the windows input's 4 characters count. Without windows the selection fits within 4 characters of the
    limit; with the PC-only id's windows it does not, so it is judged too big and the full suite runs on GitHub."""
    limit = github_run.MAX_INPUTS_CHARS
    ref = merge_github.ref_name("i271-u4")

    def size(ids, windows):
        return sum(len(v) for v in github_run.dispatch_inputs(ref, 5, ids=ids, windows=windows).values())

    def pc_id(n):  # a PC-only id padded to ``n`` characters of pseudo-random text (incompressible, prefix-stable)
        text = "".join(hashlib.sha256(str(k).encode()).hexdigest() for k in range(n // 64 + 1))[:n]
        return f"{PC_FILE}::test_padded_{text}"

    base = ids_of(1500)
    lo, hi = 0, 40000
    while lo < hi:  # the smallest padding whose size reaches limit - 3
        mid = (lo + hi) // 2
        if size(base + [pc_id(mid)], False) >= limit - 3:
            hi = mid
        else:
            lo = mid + 1
    ids = base + [pc_id(lo)]
    assert limit - 3 <= size(ids, False) <= limit, size(ids, False)  # fits without windows, within 4 characters
    assert size(ids, True) > limit  # the windows input's "true" tips it over
    gh = FakeGh()
    run, heavy = run_placed(tmp_path, monkeypatch, ids, gh, pc_only=lambda: [PC_FILE])
    assert heavy == []
    sent = gh.dispatched_inputs()
    assert len(sent) == 1 and "ids" not in sent[0] and sent[0]["windows"] == "true"
    assert run.place.endswith(f": selection too big for one dispatch ({len(ids)} ids), full suite")
