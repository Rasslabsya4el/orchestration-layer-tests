"""Issue 105 (unit 2): the dispatcher's tick returns a parked ticket whose `Parked:` event has happened.

`Dispatcher._maybe_return_parked` runs in `tick()` right after `_notify_live_outcomes()`, on every tick (a pass is a
tick, 30 s), only with a real `GhCli`, through one `ParkedReturn` built once, on the `gh` repo of that `GhCli`. A
fake `gh` runner (the seam `GhCli(runner=...)`) records every argv; the rows are the spec's (a) to (g) and the
review rows (h) to (k)."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

from tests.test_agentops_dispatch import FakeClaude, FakeClock, FakeGh, _dispatcher
from tools.agentops import dispatch as dispatch_mod
from tools.agentops import parked_return as parked_return_mod
from tools.agentops.dispatch import GhCli

MARKER = "<!-- parked-return -->"
REPO = "o/r"


class ParkedGh:
    """A fake `gh` runner: `issues` maps number -> (body, labels). The parked list answers from the labels
    as they are now, so a flipped ticket is no longer listed. `view_states` maps an issue number to the state
    `issue view ... --json state` answers (default OPEN). Every other read answers an empty list."""

    def __init__(self, issues: dict[int, tuple[str, tuple[str, ...]]],
                 view_states: dict[int, str] | None = None) -> None:
        self.issues = {n: {"body": body, "labels": list(labels)} for n, (body, labels) in issues.items()}
        self.view_states = dict(view_states or {})
        self.calls: list[list[str]] = []
        self.comments: list[tuple[int, str]] = []
        self.raise_on_parked: Exception | None = None

    def __call__(self, args, stdin=None):
        self.calls.append(list(args))
        if is_parked_list(args):
            if self.raise_on_parked is not None:
                raise self.raise_on_parked
            rows = [{"number": n, "body": i["body"], "labels": [{"name": name} for name in i["labels"]]}
                    for n, i in self.issues.items() if "parked" in i["labels"]]
            return subprocess.CompletedProcess(args, 0, json.dumps(rows), "")
        if args[:2] == ["issue", "view"] and "state" in args:
            state = self.view_states.get(int(args[2]), "OPEN")
            return subprocess.CompletedProcess(args, 0, state + "\n", "")
        if args[:2] == ["issue", "edit"]:
            n = int(args[2])
            if "--add-label" in args:
                self.issues[n]["labels"].append(args[args.index("--add-label") + 1])
            if "--remove-label" in args:
                name = args[args.index("--remove-label") + 1]
                self.issues[n]["labels"] = [x for x in self.issues[n]["labels"] if x != name]
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["issue", "comment"]:
            self.comments.append((int(args[2]), stdin or ""))
            return subprocess.CompletedProcess(args, 0, "", "")
        body = json.dumps({"data": {"search": {"nodes": []}}}) if args and args[0] == "api" else "[]"
        return subprocess.CompletedProcess(args, 0, body, "")

    def parked_lists(self) -> int:
        return sum(1 for a in self.calls if is_parked_list(a))

    def edits(self, flag: str) -> list[tuple[int, str]]:
        """The (issue number, label) pairs an `issue edit` with `flag` (`--add-label` / `--remove-label`) named."""
        out = []
        for a in self.calls:
            if a[:2] == ["issue", "edit"] and flag in a:
                out.append((int(a[2]), a[a.index(flag) + 1]))
        return out


def is_parked_list(a: list[str]) -> bool:
    return a[:2] == ["issue", "list"] and "--label" in a and a[a.index("--label") + 1] == "parked"


def _dispatch(tmp_path: Path, gh_fake: ParkedGh, clock: FakeClock):
    d = _dispatcher(tmp_path, claude=FakeClaude(), gh=GhCli(repo=REPO, runner=gh_fake), clock=clock)
    d.log_lines = []
    d.log = d.log_lines.append
    return d


def test_a_first_tick_returns_a_due_parked_issue(tmp_path: Path):
    gh_fake = ParkedGh({7: ("Parked: until 2020-01-01 00:00", ("parked",))})
    d = _dispatch(tmp_path, gh_fake, FakeClock())

    d.tick()

    assert gh_fake.edits("--add-label") == [(7, "ready-for-agent")]
    assert gh_fake.edits("--remove-label") == [(7, "parked")]
    assert len(gh_fake.comments) == 1
    assert gh_fake.comments[0][0] == 7
    assert gh_fake.comments[0][1].startswith(MARKER)


def test_b_two_ticks_100_seconds_apart_each_list_once_and_neither_reflips(tmp_path: Path):
    gh_fake = ParkedGh({7: ("Parked: until 2020-01-01 00:00", ("parked",))})
    clock = FakeClock(0.0)
    d = _dispatch(tmp_path, gh_fake, clock)

    d.tick()  # t=0: #7 is due and flips
    assert gh_fake.parked_lists() == 1
    flips_after_first = len(gh_fake.edits("--add-label"))
    comments_after_first = len(gh_fake.comments)

    clock.value = 100.0
    d.tick()  # t=100: one list again (no throttle), and #7 is no longer listed, so nothing flips
    assert gh_fake.parked_lists() == 2
    assert len(gh_fake.edits("--add-label")) == flips_after_first
    assert len(gh_fake.edits("--remove-label")) == flips_after_first
    assert len(gh_fake.comments) == comments_after_first


def test_c_free_text_parked_line_is_never_flipped(tmp_path: Path):
    gh_fake = ParkedGh({7: ("Parked: waiting for Dana", ("parked",))})
    d = _dispatch(tmp_path, gh_fake, FakeClock())

    d.tick()

    assert gh_fake.parked_lists() == 1
    assert gh_fake.edits("--add-label") == []
    assert gh_fake.edits("--remove-label") == []
    assert gh_fake.comments == []


def test_d_three_parked_issues_cost_one_list_call_per_pass(tmp_path: Path):
    gh_fake = ParkedGh({
        7: ("Parked: until 2020-01-01 00:00", ("parked",)),
        8: ("Parked: waiting for Dana", ("parked",)),
        9: ("Parked: until 2099-01-01 00:00", ("parked",)),
    })
    d = _dispatch(tmp_path, gh_fake, FakeClock())

    d.tick()

    assert gh_fake.parked_lists() == 1
    assert gh_fake.edits("--add-label") == [(7, "ready-for-agent")]
    assert [n for n, _ in gh_fake.comments] == [7]


def test_e_plain_fake_gh_is_not_a_real_gh_and_nothing_runs(tmp_path: Path):
    gh = FakeGh(candidates=[])
    d = _dispatcher(tmp_path, claude=FakeClaude(), gh=gh)

    d.tick()

    assert d._parked_return is None


def test_f_a_failing_parked_list_never_stops_the_tick(tmp_path: Path):
    gh_fake = ParkedGh({7: ("Parked: until 2020-01-01 00:00", ("parked",))})
    gh_fake.raise_on_parked = RuntimeError("boom")
    d = _dispatch(tmp_path, gh_fake, FakeClock())

    d.tick()  # must not raise

    parked_lines = [line for line in d.log_lines if "parked return" in line]
    assert len(parked_lines) == 1
    assert "boom" in parked_lines[0]
    assert gh_fake.edits("--add-label") == []


def test_g_a_failure_building_the_return_is_one_log_line_and_no_raise(tmp_path: Path, monkeypatch):
    def boom(settings, home_root):
        raise RuntimeError("no core seam")

    monkeypatch.setattr(dispatch_mod, "default_target_core", boom)
    gh_fake = ParkedGh({})
    d = _dispatch(tmp_path, gh_fake, FakeClock())

    d.tick()  # must not raise

    failed = [line for line in d.log_lines if line.startswith("parked return failed")]
    assert len(failed) == 1
    assert "no core seam" in failed[0]


def test_h_the_parked_list_and_the_edits_name_the_gh_repo(tmp_path: Path):
    gh_fake = ParkedGh({7: ("Parked: until 2020-01-01 00:00", ("parked",))})
    d = _dispatch(tmp_path, gh_fake, FakeClock())

    d.tick()

    listed = [a for a in gh_fake.calls if is_parked_list(a)]
    assert len(listed) == 1
    assert listed[0][listed[0].index("-R") + 1] == REPO
    edits = [a for a in gh_fake.calls if a[:2] == ["issue", "edit"]]
    assert edits and all(a[a.index("-R") + 1] == REPO for a in edits)


def test_i_core_form_flips_when_the_target_reached_the_sha_long_enough_ago(tmp_path: Path, monkeypatch):
    reached = datetime(2020, 1, 1)
    monkeypatch.setattr(dispatch_mod, "default_target_core",
                        lambda settings, home_root: (lambda owner_repo: ("abcdef1234567890", reached)))
    monkeypatch.setattr(parked_return_mod, "git_is_ancestor", lambda older, newer, cwd=None: True)
    gh_fake = ParkedGh({7: ("Parked: 0 h after core abcdef1 reaches o/t", ("parked",))})
    d = _dispatch(tmp_path, gh_fake, FakeClock())

    d.tick()

    assert gh_fake.edits("--add-label") == [(7, "ready-for-agent")]
    assert gh_fake.edits("--remove-label") == [(7, "parked")]
    assert len(gh_fake.comments) == 1
    assert gh_fake.comments[0][1].startswith(MARKER)


def test_j_issue_form_flips_when_the_named_issue_is_closed(tmp_path: Path):
    gh_fake = ParkedGh({7: ("Parked: until #5 closes", ("parked",))}, view_states={5: "CLOSED"})
    d = _dispatch(tmp_path, gh_fake, FakeClock())

    d.tick()

    assert gh_fake.edits("--add-label") == [(7, "ready-for-agent")]
    assert gh_fake.edits("--remove-label") == [(7, "parked")]
    assert len(gh_fake.comments) == 1


def test_k_issue_form_does_not_flip_while_the_named_issue_is_open(tmp_path: Path):
    gh_fake = ParkedGh({7: ("Parked: until #5 closes", ("parked",))}, view_states={5: "OPEN"})
    d = _dispatch(tmp_path, gh_fake, FakeClock())

    d.tick()

    assert gh_fake.parked_lists() == 1
    assert gh_fake.edits("--add-label") == []
    assert gh_fake.edits("--remove-label") == []
    assert gh_fake.comments == []
