"""#267 unit 2: a dispatch that raises keeps its exception text in the fallback reason and in the merge.log place line.

Fakes only (no network, no git push, no machine line). The merge.log line is written by the real
``merge._append_merge_log`` into a temp file, from the place text ``merge_github`` builds for the result.
"""
from __future__ import annotations

from tools.agentops import github_suite as gs
from tools.agentops import merge
from tools.agentops import merge_github
from tools.agentops.test_copy_send import SendResult

COPY = "https://github.com/owner/copy-repo.git"
SETTINGS = {"github_machines_per_merge": 5, "github_machines_total": 20}
WINERR = "[WinError 206] The filename or extension is too long"


class Berth:
    machines = 3

    def __init__(self, fakes):
        self.fakes = fakes

    def release(self):
        self.fakes.released += 1


class DispatchFakes:
    def __init__(self, exc):
        self.exc = exc
        self.released = 0
        self.polled = False
        self.records: list[dict] = []

    def send(self, root, branch, remote):
        return SendResult(True, "abc")

    def acquire_machines(self, repo, run, want=None, **kw):
        return Berth(self)

    def dispatch(self, repo, branch, parts, *a, **kw):
        raise self.exc

    def poll(self, repo, run_id, *a, **kw):
        self.polled = True
        raise AssertionError("poll must not run after a failed dispatch")

    def push(self, root, branch, repo_slug):
        pass

    def clock(self):
        return 1.0

    def record(self, rec):
        self.records.append(rec)


def _run(exc, branch="i267-u2"):
    fakes = DispatchFakes(exc)
    deps = gs.Deps(send=fakes.send, acquire_machines=fakes.acquire_machines, dispatch=fakes.dispatch,
                   poll=fakes.poll, record=fakes.record, push=fakes.push, clock=fakes.clock)
    result = gs.run_github_suite(root="C:/x/repo", branch=branch, repo_slug="me/repo", remote=COPY,
                                 repo_name="orch", priority=False, settings=SETTINGS, deps=deps)
    return result, fakes


def test_winerror_text_is_in_the_fallback_reason_and_the_brain_line():
    result, fakes = _run(OSError(WINERR))
    assert (result.status, result.reason) == ("fallback", "dispatch-failed: " + WINERR)
    assert WINERR in result.brain_line
    assert fakes.released == 1 and fakes.polled is False
    assert fakes.records[0]["outcome"] == "dispatch-failed"


def test_winerror_text_is_in_the_place_line_and_the_merge_log_line(tmp_path):
    result, _fakes = _run(OSError(WINERR))
    place = merge_github.pc_place_text(merge_github.pc_reason(result))
    assert place == "suite on the PC (GitHub dispatch-failed: " + WINERR + ")"
    log = tmp_path / "merge.log"
    merge._append_merge_log("i267-u2", merge.MergeResult(ok=False, reason="suite-fallback", suite_place=place),
                            log_path=log)
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert ("dispatch-failed: " + WINERR) in lines[0]


def test_long_text_is_cut_to_300_characters():
    result, _fakes = _run(RuntimeError("x" * 500))
    assert result.reason == "dispatch-failed: " + "x" * 300


def test_only_the_first_line_of_a_multi_line_text_is_kept():
    result, _fakes = _run(RuntimeError("first line\nsecond line"))
    assert result.reason == "dispatch-failed: first line"


def test_an_exception_without_text_names_its_class():
    result, _fakes = _run(RuntimeError())
    assert result.reason == "dispatch-failed: RuntimeError"
