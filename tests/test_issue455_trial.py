"""#455 unit 1: the `trial_repo_prefix` setting and `python -m tools.agentops trial create | list`.

Every gh and git call goes through the runner seam of `tools.agentops.trial`; the tests pass a fake runner that
records each argv (and stdin text) and answers from a table. No network, no real repo, no real settings write.
"""

from __future__ import annotations

import argparse
import json

import pytest

from tools.agentops import core_settings, trial

SOURCE = "acme-owner/orch-it"
OWNER = "acme-owner"
NAME = "orch-trial-t455a"
FULL = f"{OWNER}/{NAME}"
SHA = "0123456789abcdef0123456789abcdef01234567"


class Fake:
    """A runner double: records (argv, input_text), raises TrialError for a needle in `fail`, answers from fixed text."""

    def __init__(self, labels=None, fail=None):
        self.calls = []
        self.labels = labels or []
        self.fail = dict(fail or {})

    def __call__(self, argv, input_text=None):
        line = " ".join(argv)
        self.calls.append((list(argv), input_text))
        for needle, message in self.fail.items():
            if needle in line:
                raise trial.TrialError(message, output=message)
        if "--jq .default_branch" in line:
            return "main\n"
        if "--jq .sha" in line:
            return SHA + "\n"
        if "label list" in line:
            return json.dumps(self.labels)
        return ""

    def argvs(self):
        return [argv for argv, _ in self.calls]


def _record_lines(root):
    return (root / "var" / "trials.jsonl").read_text(encoding="utf-8").splitlines()


def test_create_private_builds_calls_in_order(tmp_path):
    fake = Fake(labels=[{"name": "ready-for-agent", "color": "0e8a16", "description": "worked by ORCH"}])
    lines = []
    code = trial.create_trial("t455a", SOURCE, owner=OWNER, root=tmp_path, runner=fake, out=lines.append)
    assert code == 0
    argvs = fake.argvs()

    def index(pred):
        return next(i for i, argv in enumerate(argvs) if pred(argv))

    create_i = index(lambda a: a[:3] == ["gh", "repo", "create"])
    fetch_i = index(lambda a: "fetch" in a)
    push_i = index(lambda a: "push" in a)
    label_list_i = index(lambda a: a[:3] == ["gh", "label", "list"])
    label_create_i = index(lambda a: a[:3] == ["gh", "label", "create"])
    clone_i = index(lambda a: a[:2] == ["git", "clone"])
    assert create_i < fetch_i < push_i < label_list_i < label_create_i < clone_i
    assert argvs[create_i] == ["gh", "repo", "create", FULL, "--private"]
    assert f"https://github.com/{SOURCE}.git" in argvs[fetch_i] and SHA in argvs[fetch_i]
    assert f"https://github.com/{FULL}.git" in argvs[push_i]
    assert argvs[push_i][-1] == "HEAD:refs/heads/main"
    assert argvs[label_create_i] == ["gh", "label", "create", "ready-for-agent", "-R", FULL,
                                     "--color", "0e8a16", "--description", "worked by ORCH", "--force"]
    assert argvs[clone_i][-1] == str(tmp_path / "var" / "trials" / NAME)
    assert not any("protection" in " ".join(a) for a in argvs)
    assert not any("delete" in " ".join(a) for a in argvs)
    assert any(str(tmp_path / "var" / "trials" / NAME) in line for line in lines)


def test_create_then_list_shows_the_written_values(tmp_path):
    fake = Fake()
    trial.create_trial("t455a", SOURCE, owner=OWNER, root=tmp_path, runner=fake, out=lambda s: None)
    lines = []
    assert trial.list_trials(root=tmp_path, runner=fake, out=lines.append) == 0
    (line,) = lines
    created = json.loads(_record_lines(tmp_path)[0])["created"]
    assert FULL in line
    assert f"from {SOURCE}" in line
    assert f"at {SHA}" in line
    assert "private" in line
    assert f"created {created}" in line
    assert "still exists yes" in line


def test_public_with_require_check_sets_protection_once(tmp_path):
    fake = Fake()
    code = trial.create_trial("t455a", SOURCE, public=True, require_checks=["ci"], owner=OWNER,
                              root=tmp_path, runner=fake, out=lambda s: None)
    assert code == 0
    assert ["gh", "repo", "create", FULL, "--public"] in fake.argvs()
    protection = [(argv, body) for argv, body in fake.calls if "protection" in " ".join(argv)]
    assert len(protection) == 1
    argv, body = protection[0]
    assert argv[:4] == ["gh", "api", "-X", "PUT"]
    assert argv[4] == f"repos/{FULL}/branches/main/protection"
    assert json.loads(body)["required_status_checks"]["contexts"] == ["ci"]


def test_require_check_without_public_refuses_before_any_call(tmp_path):
    fake = Fake()
    lines = []
    code = trial.create_trial("t455c", SOURCE, require_checks=["ci"], owner=OWNER,
                              root=tmp_path, runner=fake, out=lines.append)
    assert code != 0
    assert fake.calls == []
    assert "403" in "\n".join(lines)
    assert not (tmp_path / "var").exists()


def test_push_failure_keeps_record_and_deletes_nothing(tmp_path):
    fake = Fake(fail={"push --quiet": "remote rejected"})
    lines = []
    code = trial.create_trial("t455a", SOURCE, owner=OWNER, root=tmp_path, runner=fake, out=lines.append)
    assert code != 0
    (record_line,) = _record_lines(tmp_path)
    assert json.loads(record_line)["name"] == FULL
    argvs = fake.argvs()
    assert not any("delete" in " ".join(a) for a in argvs)
    assert not any(a[:3] == ["gh", "label", "create"] or "protection" in " ".join(a) or a[:2] == ["git", "clone"]
                   for a in argvs)
    text = "\n".join(lines)
    assert FULL in text and "exists so far" in text


def test_list_prints_each_record_with_still_exists(tmp_path):
    def record(name, visibility):
        return {"name": f"{OWNER}/{name}", "from": SOURCE, "at": SHA, "visibility": visibility,
                "created": "2026-10-30T10:00:00+00:00"}

    (tmp_path / "var").mkdir()
    (tmp_path / "var" / "trials.jsonl").write_text(
        json.dumps(record("orch-trial-t455a", "private")) + "\n" + json.dumps(record("orch-trial-t455b", "public")) + "\n",
        encoding="utf-8")
    fake = Fake(fail={f"repo view {OWNER}/orch-trial-t455b": "GraphQL: Could not resolve to a Repository"})
    lines = []
    code = trial.list_trials(root=tmp_path, runner=fake, out=lines.append)
    assert code == 0
    first, second = lines
    assert f"{OWNER}/orch-trial-t455a" in first and "from acme-owner/orch-it" in first
    assert SHA in first and "private" in first and "2026-10-30T10:00:00+00:00" in first
    assert "still exists yes" in first
    assert f"{OWNER}/orch-trial-t455b" in second and "public" in second
    assert "still exists no" in second


def test_list_without_record_file_says_so(tmp_path):
    lines = []
    assert trial.list_trials(root=tmp_path, runner=Fake(), out=lines.append) == 0
    assert lines and "no trials" in lines[0]


def test_prefix_comes_from_the_setting(tmp_path, monkeypatch):
    monkeypatch.setattr(core_settings, "trial_repo_prefix", lambda path=None: "other-x-")
    fake = Fake()
    trial.create_trial("t455a", SOURCE, owner=OWNER, root=tmp_path, runner=fake, out=lambda s: None)
    assert ["gh", "repo", "create", f"{OWNER}/other-x-t455a", "--private"] in fake.argvs()


def test_prefix_setting_defaults_and_validates(tmp_path):
    path = tmp_path / "orchestration.json"
    data = {k: "x" for k in core_settings.REQUIRED_KEYS}
    data["local_modules"] = []
    path.write_text(json.dumps(data), encoding="utf-8")
    assert core_settings.trial_repo_prefix(path) == "orch-trial-"
    data["trial_repo_prefix"] = "mine-"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert core_settings.trial_repo_prefix(path) == "mine-"
    for bad in ("", 3):
        data["trial_repo_prefix"] = bad
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ValueError) as exc_info:
            core_settings.trial_repo_prefix(path)
        assert "trial_repo_prefix" in str(exc_info.value)


@pytest.mark.parametrize("slug,source,at", [
    ("bad slug", SOURCE, None),
    ("t455a", "noslash", None),
    ("t455a", SOURCE, "not-hex"),
])
def test_bad_arguments_refuse_before_any_call(tmp_path, slug, source, at):
    fake = Fake()
    code = trial.create_trial(slug, source, at=at, owner=OWNER, root=tmp_path, runner=fake, out=lambda s: None)
    assert code == 2
    assert fake.calls == []


def test_parser_accepts_create_and_list():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    trial.add_subparser(sub)
    args = parser.parse_args(["trial", "create", "t455b", "--from", SOURCE, "--public",
                              "--require-check", "ci", "lint"])
    assert (args.slug, args.source, args.public, args.require_check) == ("t455b", SOURCE, True, ["ci", "lint"])
    assert parser.parse_args(["trial", "list"]).trial_command == "list"
