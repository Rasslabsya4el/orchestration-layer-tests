"""Issue 122 unit 8: a core update ensures the core's labels exist in the target. The label sync is a step of
`run_update_steps`, which runs in the UPDATED code after the copy, on every taken update: a home-self update calls
nothing; a taken update calls `port.sync_labels` once with the target's own repo and list, creates only the missing
labels, and logs only what it created or a failure; a failing label call is one log line and never fails the
update, and the next update retries it by itself. The bootstrap case: the update that lands the new list and the new
code runs the new step, so that same update creates the labels. Real git on a tmp target (the #49 helpers); the gh
runner is a fake, no network runs."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from tests import test_issue49_core_update as base
from tools.agentops import core_update
from tools.agentops.core_update import run_core_update

LABELS = "config/core_labels.json"
HOME_LABELS = {
    "needs-triage": {"color": "fbca04", "description": "Maintainer needs to evaluate this issue"},
    "flow-fix": {"color": "b60205", "description": "A flow fix: removes a slowdown on the way to main"},
}


def _home_files(labels: dict) -> dict[str, str]:
    return {**base.NEW_HOME_FILES, LABELS: json.dumps(labels, indent=2) + "\n"}


class _Gh:
    """The home's files through the #49 fake; the `gh label` calls are recorded and answered here."""

    def __init__(self, home_files: dict[str, str], existing: list[str], label_rc: int = 0, label_err: str = "") -> None:
        self.home = base._fake_gh(home_files)
        self.existing = existing
        self.label_rc = label_rc
        self.label_err = label_err
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]):
        if args[0] != "label":
            return self.home(args)
        self.calls.append(list(args))
        if args[1] == "list":
            if self.label_rc:
                return SimpleNamespace(returncode=self.label_rc, stdout="", stderr=self.label_err)
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"name": n} for n in self.existing]), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _update(root: Path, gh: _Gh):
    """A taken update whose post-copy step is the updated code's `run_update_steps` with the fake gh."""
    return run_core_update(root, gh_runner=gh, live_check=lambda: False,
                           step_runner=lambda _root: core_update.run_update_steps(_root, gh_runner=gh),
                           lock_wait_s=5.0)


def _set_rule(root: Path, rule: str) -> None:
    settings = json.loads((root / base.ORCH_JSON).read_text(encoding="utf-8"))
    settings["update_rule"] = rule
    (root / base.ORCH_JSON).write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def test_update_that_gains_labels_calls_sync_labels_with_target_repo_and_injected_runner(tmp_path: Path) -> None:
    root = base._target(tmp_path)
    repo = core_update._target_repo(root)
    gh = _Gh(_home_files(HOME_LABELS), existing=["needs-triage"])

    result = _update(root, gh)

    assert base._version(root) == base.NEW_VERSION
    assert LABELS in result.changed
    assert gh.calls[0] == ["label", "list", "-R", repo, "--json", "name", "--limit", "200"]
    creates = [c for c in gh.calls if c[:2] == ["label", "create"]]
    assert [c[2] for c in creates] == ["flow-fix"]  # only the label the target lacked
    assert creates[0][3:5] == ["-R", repo]
    assert "core update labels: created flow-fix" in base._dispatch_out(root)


def test_bootstrap_the_update_that_lands_the_list_and_the_step_creates_its_labels(tmp_path: Path) -> None:
    """The auto update runs the target's old code, whose tail makes no label call; the step is a subprocess of the
    code the update just landed, so the step (not the tail) is what creates the labels in that same update."""
    root = base._target(tmp_path)
    gh = _Gh(_home_files(HOME_LABELS), existing=["needs-triage"])

    result = _update(root, gh)

    assert LABELS in result.changed
    assert [c[1] for c in gh.calls] == ["list", "create"]
    assert "core update labels: created flow-fix" in base._dispatch_out(root)


def test_home_self_update_does_not_call_sync_labels(tmp_path: Path) -> None:
    root = base._target(tmp_path)
    _set_rule(root, "home")
    gh = _Gh(_home_files(HOME_LABELS), existing=[])

    result = _update(root, gh)

    assert "this is the home" in result.message
    assert gh.calls == []


def test_update_at_the_home_head_makes_no_gh_call_and_logs_nothing(tmp_path: Path) -> None:
    """A target already at the home's head: the update stops after the head-sha call, so it lists no labels (#148)
    and logs nothing more than the first update's one `created flow-fix` line."""

    root = base._target(tmp_path)
    home = _home_files(HOME_LABELS)
    _update(root, _Gh(home, existing=["needs-triage"]))  # the first update creates flow-fix and logs it
    gh = _Gh(home, existing=["needs-triage", "flow-fix"])

    result = _update(root, gh)

    assert result.changed == [] and result.removed == []
    assert gh.calls == []  # #148: a target at the home's head stops after the head-sha call, no step runs
    assert base._dispatch_out(root).count("core update labels") == 1


def test_gh_failure_leaves_update_taken_logs_one_line_and_the_next_update_retries_by_itself(tmp_path: Path) -> None:
    root = base._target(tmp_path)
    home = _home_files(HOME_LABELS)
    failing = _Gh(home, existing=[], label_rc=1, label_err="boom\n")

    result = _update(root, failing)  # no exception: the label call is best-effort

    assert base._version(root) == base.NEW_VERSION
    assert (root / "docs" / "agents" / "three.md").read_text(encoding="utf-8") == "three v2 (new)\n"
    assert result.message.startswith("updated:")
    assert base._dispatch_out(root).count("core update labels: not synced: gh label list failed: boom") == 1

    # #148: at head nothing runs (no step, so no label retry); the retry needs the home to move on, which is the update after it
    # (here: the target is one home commit behind again, no file of the manifest changed)
    orch = root / "config" / "orchestration.json"
    settings = json.loads(orch.read_text(encoding="utf-8"))
    settings["core_version"] = base.OLD_VERSION
    orch.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    assert base._git(root, "commit", "-q", "-am", "target one home commit behind").returncode == 0
    retry = _Gh(home, existing=["needs-triage"])
    _update(root, retry)

    assert [c[1] for c in retry.calls] == ["list", "create"]
    assert [c[2] for c in retry.calls if c[:2] == ["label", "create"]] == ["flow-fix"]
    assert "core update labels: created flow-fix" in base._dispatch_out(root)
