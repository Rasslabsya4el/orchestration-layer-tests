"""Issue #349 unit 5: the launcher of the brain-role replays.

No network and no real claude: ``cfg.claude_bin`` is a python file that prints stream-json like the real one,
echoes its argv, cwd and environment into its answer text and, by variant, leaks into a transcript, names another
model or effort, or sleeps. The run tree is faked by monkeypatching ``launcher.make_run_tree`` (it still names the
tree ``<case>-<arm>-<n>`` like the real one, so the move to an opaque name is exercised); the projects folder is a tmp
dir given through the cfg.
"""

import inspect
import json
import os
import re
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from tools.replays import launcher
from tools.replays.launcher import Config, finish, issue_counts, run_all, run_one

#: a mode applies to the first FAKE_FIRST started processes (all of them when unset): a rerun is a later process.
FAKE_CLAUDE = textwrap.dedent(
    r'''
    import json, os, re, sys, time
    from pathlib import Path

    argv = sys.argv[1:]
    cwd = os.getcwd()
    name = Path(cwd).name
    sid = "sess-" + name
    k = 0
    while True:
        try:
            os.close(os.open(os.path.join(os.environ["FAKE_ORD"], str(k)), os.O_CREAT | os.O_EXCL))
            break
        except FileExistsError:
            k += 1
    mode = os.environ.get("FAKE_MODE", "")
    if k >= int(os.environ.get("FAKE_FIRST", "999")):
        mode = ""
    log = os.environ.get("FAKE_LOG")
    if log:
        Path(log, "start-" + name).write_text("%.3f" % time.time())
    init = {"type": "system", "subtype": "init", "session_id": sid}
    if os.environ.get("FAKE_INIT_EFFORT", "high") != "none":
        init["effort"] = os.environ.get("FAKE_INIT_EFFORT", "high")
    print(json.dumps(init), flush=True)
    if mode == "sleep":
        time.sleep(60)
    leak = mode == "leak"
    model = "claude-fable-5-1" if mode == "wrong_model" else os.environ.get("FAKE_MODEL", "claude-opus-5-5")
    if mode != "no_transcript":
        folder = Path(os.environ["FAKE_PROJECTS"]) / re.sub(r"[^A-Za-z0-9]", "-", cwd)
        folder.mkdir(parents=True, exist_ok=True)
        text = "see the parity page" if leak else "nothing special"
        recs = [{"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": text}]}}]
        if mode != "no_assistant":
            usage = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 2,
                     "cache_read_input_tokens": 3}
            msg = {"id": "m1", "model": model, "usage": usage, "content": [{"type": "text", "text": "x"}]}
            recs.append({"type": "assistant", "message": msg})
            recs.append({"type": "assistant", "message": msg})  # the same message written twice
            recs.append({"type": "assistant", "message": {"model": "<synthetic>", "content": []}})
        if mode == "mixed_model":
            recs.append({"type": "assistant", "message": {"model": "claude-haiku-5-5", "content": []}})
        (folder / (sid + ".jsonl")).write_text(
            "\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    state = os.environ.get("REPLAY_STATE_DIR")
    if state and os.environ.get("FAKE_WRITE"):
        with open(os.path.join(state, "writes.jsonl"), "a") as fh:
            fh.write(json.dumps({"argv": ["issue", "create"], "body": "B"}) + "\n")
    echo = {
        "argv": argv,
        "env": {k: v for k, v in os.environ.items()
                if k.startswith(("REPLAY_", "CLAUDE_CODE_", "GH_", "GITHUB_"))},
        "first_path": os.environ["PATH"].split(os.pathsep)[0],
        "cwd": cwd,
    }
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "first part"},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
        {"type": "text", "text": "ECHO " + json.dumps(echo)}]}}), flush=True)
    print(json.dumps({"type": "assistant", "parent_tool_use_id": "x", "message": {"content": [
        {"type": "text", "text": "HELPER TEXT"}]}}), flush=True)
    time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
    print(json.dumps({"type": "result", "subtype": "success", "session_id": sid,
                      "total_cost_usd": float(os.environ.get("FAKE_COST", "1.5"))}), flush=True)
    if log:
        Path(log, "end-" + name).write_text("%.3f" % time.time())
    '''
)

OPAQUE = re.compile(r"^r[0-9a-f]{8}$")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE_CLAUDE, encoding="utf-8")
    projects = tmp_path / "projects"
    projects.mkdir()
    ordinals = tmp_path / "ordinals"
    ordinals.mkdir()
    monkeypatch.setenv("FAKE_PROJECTS", str(projects))
    monkeypatch.setenv("FAKE_ORD", str(ordinals))
    for name in launcher.TOKEN_VARS:
        monkeypatch.delenv(name, raising=False)

    def fake_tree(case, arm, n, base_dir, role_repo, draft_dir):
        tree = Path(base_dir) / f"{case['key']}-{arm}-{n}"
        tree.mkdir(parents=True)
        return tree

    monkeypatch.setattr(launcher, "make_run_tree", fake_tree)
    (tmp_path / "harness" / "config").mkdir(parents=True)  # the deny list reads it (#357 unit 5)
    (tmp_path / "harness" / "config" / "orchestration.json").write_text('{"targets": []}', encoding="utf-8")
    # #363: the committed cases carry a turn-2 answer, and the fake's echo of the owner message holds a "?": these
    # tests pin the one-turn contract, so they run on a copy of the cases without the answers
    cases = json.loads(launcher.DEFAULT_CASES_PATH.read_text(encoding="utf-8"))
    for case in cases["cases"].values():
        case.pop("turn2_answer", None)
    one_turn = tmp_path / "cases-one-turn.json"
    one_turn.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    return Config(
        cases_path=one_turn,
        claude_bin=str(fake),
        role_repo=tmp_path / "role",
        draft_dir=tmp_path / "draft",
        runs_dir=tmp_path / "runs",
        trees_dir=tmp_path / "trees",
        harness_root=tmp_path / "harness",
        projects_dir=projects,
        state_dir=tmp_path / "state",
        timeout_s=60,
    )


def _echo(record: dict) -> dict:
    return json.loads(record["text"].split("ECHO ", 1)[1])


def _progress(cfg) -> dict:
    return json.loads((cfg.runs_dir / "progress.json").read_text("utf-8"))


def test_run_one_record_fields_command_and_environment(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_WRITE", "1")
    record = run_one("a", "old", 1, cfg)
    for field in ("run_id", "case", "arm", "n", "opaque_id", "session_id", "model", "effort", "cost_usd", "text",
                  "writes", "void", "hits", "timed_out", "tree"):
        assert field in record
    assert record["run_id"] == "a-old-1"
    assert (record["case"], record["arm"], record["n"]) == ("a", "old", 1)
    opaque = record["opaque_id"]
    assert OPAQUE.match(opaque)
    assert record["session_id"] == f"sess-{opaque}"
    assert record["model"] == "claude-opus-5-5" and record["effort"] == "high"
    assert record["cost_usd"] == 1.5
    assert record["void"] is False and record["timed_out"] is False and record["hits"] == []
    assert record["tree"] == str(cfg.trees_dir / opaque)
    # first-turn text: every assistant text block of the main thread in order, blank-line joined; no helper text
    assert record["text"].startswith("first part\n\nECHO ")
    assert "HELPER TEXT" not in record["text"]
    assert record["writes"] == [{"argv": ["issue", "create"], "body": "B"}]
    # tokens of the transcript, a message written twice counted once
    assert record["usage"] == {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 2,
                               "cache_read_input_tokens": 3}

    echo = _echo(record)
    argv = echo["argv"]
    owner_message = json.loads(launcher.DEFAULT_CASES_PATH.read_text(encoding="utf-8"))["cases"]["a"]["owner_message"]
    assert argv[0] == "-p" and argv[1] == owner_message
    flags = argv[2:]
    for pair in (["--model", "claude-opus-5-5"], ["--effort", "high"], ["--agent", "brain"],
                 ["--permission-mode", "bypassPermissions"], ["--output-format", "stream-json"]):
        assert flags[flags.index(pair[0]) + 1] == pair[1]
    assert "--verbose" in flags

    state = cfg.state_dir / opaque
    assert echo["first_path"] == str(state)
    env = echo["env"]
    assert env["REPLAY_STATE_DIR"] == str(state)
    assert env["REPLAY_MOMENT"] == "2026-10-23T15:15:00Z"
    assert env["REPLAY_REPO"] == "Rasslabsya4el/orchestration-layer"
    assert "REPLAY_REAL_GH" in env
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    shim = (state / "gh.cmd").read_text(encoding="utf-8")
    assert sys.executable in shim and "tools.replays.gh_stub" in shim and "%*" in shim
    assert str(cfg.harness_root) in shim  # PYTHONPATH of the harness lives in the shim
    assert (state / "gh").is_file()
    settings = json.loads((cfg.trees_dir / opaque / ".claude" / "settings.local.json").read_text("utf-8"))
    assert settings["autoMemoryEnabled"] is False
    assert json.loads((cfg.runs_dir / "a-old-1.json").read_text("utf-8")) == record


def test_paths_and_env_handed_to_the_brain_name_no_arm_or_case(cfg, monkeypatch):
    _case_b_entry()  # skips when the config has no case b target
    word =re.compile(r"(?i)(?<![a-z])(old|new|case)(?![a-z])|(?<![a-z])[abc]-(old|new)-\d")
    for case, arm in (("a", "old"), ("b", "new")):
        record = run_one(case, arm, 1, cfg)
        echo = _echo(record)
        seen = [echo["cwd"], echo["first_path"], *echo["env"].values()]
        for value in seen:
            for root in (cfg.trees_dir, cfg.state_dir):
                if value.startswith(str(root)):
                    assert OPAQUE.match(value[len(str(root)) + 1:].split(os.sep)[0]), value
            assert not word.search(value.replace(str(cfg.trees_dir.parent), "")), value
        assert echo["cwd"] == record["tree"] and Path(echo["cwd"]).name == record["opaque_id"]
        assert Path(echo["first_path"]).name == record["opaque_id"]
        assert {p.name for p in cfg.trees_dir.iterdir() if p.name != launcher.STAGE_DIR} >= {record["opaque_id"]}
    assert all(OPAQUE.match(p.name) for p in cfg.state_dir.iterdir())
    assert list((cfg.trees_dir / launcher.STAGE_DIR).iterdir()) == []  # every staged tree was moved


def test_git_worktree_moved_to_an_opaque_name_keeps_working_and_names_no_arm(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-c", "user.name=x", "-c", "user.email=x@x"]
    subprocess.run([*git, "init", "-q"], cwd=repo, check=True)
    (repo / "f.txt").write_text("1")
    subprocess.run([*git, "add", "."], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-qm", "c"], cwd=repo, check=True)
    staged = tmp_path / "stage" / "a-old-1"
    subprocess.run([*git, "worktree", "add", "--detach", "-q", str(staged)], cwd=repo, check=True)
    dest = launcher._move_tree(staged, tmp_path / "trees" / "r0123abcd")
    assert not staged.exists() and (dest / "f.txt").read_text() == "1"
    assert "old" not in (dest / ".git").read_text()
    head = subprocess.run([*git, "rev-parse", "HEAD"], cwd=dest, capture_output=True, text=True, check=True)
    assert len(head.stdout.strip()) == 40
    listing = subprocess.run([*git, "worktree", "list"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert "r0123abcd" in listing and "a-old-1" not in listing


def _case_b_entry(harness_root: Path = launcher.HARNESS_ROOT) -> dict:
    """The config/orchestration.json target case b resolves to, read without the resolver. The test skips, never fails,
    when the config has no such target: the public test copy drops every product entry of its config."""
    config = json.loads((harness_root / "config" / "orchestration.json").read_text(encoding="utf-8"))
    hits = [t for t in config.get("targets", []) if t["repo"].endswith("cabinet-dashboard")]
    if len(hits) != 1:
        pytest.skip(f"case b's config target is absent here ({len(hits)} matches)")
    return hits[0]


def test_repo_of_case_b_comes_from_the_config(cfg):
    entry = _case_b_entry()
    case = launcher._load_case("b", cfg)
    assert case["repo"] == entry["folder"]
    assert _echo(run_one("b", "new", 2, cfg))["env"]["REPLAY_REPO"] == entry["repo"]


def test_case_b_tests_skip_when_the_config_has_no_its_target(tmp_path):
    real = json.loads((launcher.HARNESS_ROOT / "config" / "orchestration.json").read_text(encoding="utf-8"))
    real["targets"] = [t for t in real.get("targets", []) if not t["repo"].endswith("cabinet-dashboard")]
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "orchestration.json").write_text(json.dumps(real), encoding="utf-8")
    cfg = Config(claude_bin="unused", role_repo=tmp_path, draft_dir=tmp_path, runs_dir=tmp_path / "runs",
                 harness_root=tmp_path)
    with pytest.raises(pytest.skip.Exception, match="absent"):
        _case_b_entry(cfg.harness_root)


def test_resolver_matches_the_one_config_entry(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "orchestration.json").write_text(json.dumps({"targets": [
        {"repo": "o/alpha-tool", "folder": "C:/x/alpha"},
        {"repo": "o/beta-dashboard", "folder": "C:/x/beta"}]}), encoding="utf-8")
    assert launcher.resolve_repo("config:beta-dashboard", tmp_path) == ("C:/x/beta", "o/beta-dashboard")


def test_resolver_no_match_raises_naming_the_suffix(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "orchestration.json").write_text(json.dumps({"targets": [
        {"repo": "o/alpha-tool", "folder": "C:/x/alpha"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="gamma-dashboard"):
        launcher.resolve_repo("config:gamma-dashboard", tmp_path)


def test_resolver_suffix_is_case_sensitive(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "orchestration.json").write_text(json.dumps({"targets": [
        {"repo": "o/beta-dashboard", "folder": "C:/x/beta"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="Beta-Dashboard"):
        launcher.resolve_repo("config:Beta-Dashboard", tmp_path)


def test_resolver_two_matches_raises_naming_the_suffix(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "orchestration.json").write_text(json.dumps({"targets": [
        {"repo": "o/one-dashboard", "folder": "C:/x/one"},
        {"repo": "p/two-dashboard", "folder": "C:/x/two"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="dashboard"):
        launcher.resolve_repo("config:dashboard", tmp_path)


def test_resolver_non_config_ref_is_unchanged(tmp_path):
    assert launcher.resolve_repo("C:/Coding/orchestration-layer", tmp_path) == (
        "C:/Coding/orchestration-layer", "Rasslabsya4el/orchestration-layer")


def test_resolver_missing_config_raises_naming_the_suffix(tmp_path):
    with pytest.raises(ValueError, match="alpha-tool"):
        launcher.resolve_repo("config:alpha-tool", tmp_path)


def test_resolver_unreadable_config_raises_naming_the_suffix(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "orchestration.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="alpha-tool"):
        launcher.resolve_repo("config:alpha-tool", tmp_path)


def test_tokens_are_scrubbed_from_the_run_env_and_handed_to_the_stub_only(cfg, monkeypatch):
    for name in launcher.TOKEN_VARS:
        monkeypatch.setenv(name, "tok-" + name)
    echo = _echo(run_one("a", "old", 1, cfg))
    env = echo["env"]
    assert not any(name in env for name in launcher.TOKEN_VARS)
    assert env["REPLAY_GH_TOKEN"] == "tok-GH_TOKEN"
    shim = (cfg.state_dir / Path(echo["cwd"]).name / "gh.cmd").read_text(encoding="utf-8")
    assert "REPLAY_GH_TOKEN" in shim and "REPLAY_GITHUB_TOKEN" in shim


@pytest.mark.skipif(os.name != "nt", reason="runs the .cmd shim")
def test_shim_gives_the_real_gh_the_token_and_runs_from_a_cwd_with_its_own_tools_package(cfg, tmp_path):
    cfg.harness_root = launcher.HARNESS_ROOT
    state = tmp_path / "st"
    state.mkdir()
    launcher._write_shims(state, cfg)
    real = tmp_path / "real_gh.cmd"
    real.write_text("@echo off\r\necho token=%GH_TOKEN%\r\n", encoding="utf-8")
    cwd = tmp_path / "cw"
    (cwd / "tools").mkdir(parents=True)
    (cwd / "tools" / "__init__.py").write_text("")  # a tree's own `tools` must not shadow the harness's
    env = {**os.environ, "REPLAY_STATE_DIR": str(state), "REPLAY_MOMENT": "2026-10-23T15:15:00Z",
           "REPLAY_REPO": "o/r", "REPLAY_REAL_GH": str(real), "REPLAY_GH_TOKEN": "sekret"}
    env.pop("GH_TOKEN", None)
    done = subprocess.run(["cmd", "/c", str(state / "gh.cmd"), "--version"], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=60)
    assert "token=sekret" in done.stdout, done.stderr


def test_existing_settings_local_keys_are_kept(cfg, monkeypatch):
    original = launcher.make_run_tree

    def tree_with_settings(*args, **kwargs):
        tree = original(*args, **kwargs)
        (tree / ".claude").mkdir()
        (tree / ".claude" / "settings.local.json").write_text('{"other": 1}', encoding="utf-8")
        return tree

    monkeypatch.setattr(launcher, "make_run_tree", tree_with_settings)
    record = run_one("a", "old", 1, cfg)
    settings = json.loads((Path(record["tree"]) / ".claude" / "settings.local.json").read_text("utf-8"))
    assert settings["other"] == 1 and settings["autoMemoryEnabled"] is False  # the deny rules are added (#357 unit 5)


def test_leaking_run_is_void_with_its_hits(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "leak")
    record = run_one("a", "new", 1, cfg)
    assert record["void"] is True and record["void_reason"] == "leak marker"
    assert [h["marker"] for h in record["hits"]] == ["parity"]
    assert json.loads((cfg.runs_dir / "a-new-1.json").read_text("utf-8"))["void"] is True


def test_clean_transcript_is_kept_and_a_missing_transcript_is_void(cfg, monkeypatch):
    kept = run_one("c", "old", 1, cfg)
    assert kept["void"] is False and kept["void_reason"] is None
    monkeypatch.setenv("FAKE_MODE", "no_transcript")
    missing = run_one("c", "old", 2, cfg)
    assert missing["void"] is True and missing["void_reason"] == "transcript not found"


def test_model_is_read_from_the_transcript(cfg):
    record = run_one("a", "old", 1, cfg)
    assert record["model"] == "claude-opus-5-5" and record["wrong_model"] is False
    assert record["effort"] == "high" and record["init_effort"] == "high"  # <synthetic> records are not a model


def test_other_model_in_the_transcript_is_wrong_model_and_void(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_MODEL", "claude-fable-5-1")
    record = run_one("a", "old", 1, cfg)
    assert record["model"] == "claude-fable-5-1"  # what the transcript says, not the launch argument
    assert record["wrong_model"] is True and record["void"] is True and record["void_reason"] == "wrong model"


def test_two_models_in_one_transcript_are_wrong_model(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "mixed_model")
    record = run_one("a", "old", 1, cfg)
    assert record["wrong_model"] is True and record["void"] is True
    assert "claude-opus-5-5" in record["model"] and "claude-haiku-5-5" in record["model"]


def test_transcript_without_assistant_record_is_void(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "no_assistant")
    record = run_one("a", "old", 1, cfg)
    assert record["void"] is True and record["void_reason"] == "no assistant record in transcript"
    assert record["model"] is None


def test_effort_named_by_the_init_record_must_equal_the_launch_effort(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_INIT_EFFORT", "low")
    record = run_one("a", "old", 1, cfg)
    assert record["wrong_effort"] is True and record["void"] is True and record["void_reason"] == "wrong effort"
    assert record["effort"] == "high" and record["init_effort"] == "low"
    monkeypatch.setenv("FAKE_INIT_EFFORT", "none")  # an init record that names no effort cannot contradict
    silent = run_one("a", "old", 2, cfg)
    assert silent["wrong_effort"] is False and silent["void"] is False and silent["init_effort"] is None


def test_timeout_kills_the_process_and_sets_timed_out(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "sleep")
    cfg.timeout_s = 3
    record = run_one("a", "old", 1, cfg)
    assert record["timed_out"] is True and record["void"] is True and record["void_reason"] == "timed out"
    assert record["session_id"] == f"sess-{record['opaque_id']}"  # the init record was read before the kill
    assert record["cost_known"] is False and record["cost_usd"] == 0.0


def test_run_all_reruns_voids_and_counts_them(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "leak")
    monkeypatch.setenv("FAKE_FIRST", "2")  # the first two processes leak: the first run of each slot
    records = run_all(cfg, cases=("a",), arms=("old", "new"), per_arm=1, concurrency=2)
    voids = [r for r in records if r["void"]]
    kept = [r for r in records if not r["void"]]
    assert len(voids) == 2 and len(kept) == 2
    assert len({(r["arm"], r["n"]) for r in records}) == 4  # a rerun gets a fresh n
    progress = _progress(cfg)
    assert progress["voids"] == 2 and progress["done"] == 4 and progress["running"] == []
    assert progress["void_reasons"] == {"leak marker": 2}
    assert progress["total_cost_usd"] == 6.0 and progress["unknown_cost_runs"] == 0
    assert "stopped" not in progress


def test_run_all_reruns_a_wrong_model_run_and_counts_it(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "wrong_model")
    monkeypatch.setenv("FAKE_FIRST", "1")
    records = run_all(cfg, cases=("a",), arms=("old",), per_arm=1, concurrency=1)
    assert [(r["n"], r["wrong_model"], r["void"]) for r in records] == [(1, True, True), (2, False, False)]
    assert _progress(cfg)["void_reasons"] == {"wrong model": 1}


def test_run_all_reruns_a_timed_out_run_and_counts_it_as_void_with_unknown_cost(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "sleep")
    monkeypatch.setenv("FAKE_FIRST", "1")
    cfg.timeout_s = 3
    records = run_all(cfg, cases=("a",), arms=("old",), per_arm=1, concurrency=1)
    assert [r["timed_out"] for r in records] == [True, False]
    progress = _progress(cfg)
    assert progress["voids"] == 1 and progress["void_reasons"] == {"timed out": 1}
    assert progress["unknown_cost_runs"] == 1 and progress["kept"] == 1


def test_run_all_gives_up_a_slot_after_max_reruns_and_defaults_to_four():
    assert inspect.signature(run_all).parameters["max_reruns"].default == 4


def test_run_all_gives_up_a_slot_after_max_reruns(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "leak")
    records = run_all(cfg, cases=("a",), arms=("old",), per_arm=1, concurrency=1, max_reruns=2)
    assert len(records) == 3 and all(r["void"] for r in records)


def test_concurrency_never_exceeds_the_limit(cfg, monkeypatch, tmp_path):
    _case_b_entry()  # skips when the config has no case b target
    log = tmp_path / "overlap"
    log.mkdir()
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_SLEEP", "0.6")
    records = run_all(cfg, cases=("a", "b"), arms=("old", "new"), per_arm=2, concurrency=3)
    assert len(records) == 8
    events = sorted((float(f.read_text()), 0 if f.name.startswith("end") else 1) for f in log.iterdir())
    live = peak = 0
    for _, is_start in events:  # at equal times an end sorts before a start
        live += 1 if is_start else -1
        peak = max(peak, live)
    assert 2 <= peak <= 3


def test_budget_stop_counts_the_next_run_at_the_average_cost(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_COST", "25")
    records = run_all(cfg, cases=("a",), arms=("old",), per_arm=6, concurrency=1, budget_usd=60.0)
    assert len(records) == 2  # 0 -> start; 25 + 25 <= 60 -> start; 50 + 25 = 75 > 60 -> stop
    progress = _progress(cfg)
    assert progress["stopped"] == "budget"
    assert progress["total_cost_usd"] == 50.0 and progress["done"] == 2


def test_voided_cost_counts_toward_the_budget(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "leak")
    monkeypatch.setenv("FAKE_COST", "40")
    records = run_all(cfg, cases=("a",), arms=("old",), per_arm=1, concurrency=1, budget_usd=60.0, max_reruns=5)
    assert len(records) == 1  # 0 -> start; 40 + 40 = 80 > 60 stops the slot's reruns
    assert _progress(cfg)["stopped"] == "budget"


def test_budget_projection_counts_runs_in_flight():
    progress = launcher._Progress(Path("unused.json"), 3)
    progress.state.update(done=2, total_cost_usd=40.0, running=["a-old-3"])  # average 20, one in flight
    assert progress.over_budget(79.0) is True  # 40 + (1 in flight + this one) * 20 = 80
    assert progress.over_budget(80.0) is False
    progress.state["running"] = []
    assert progress.over_budget(59.0) is True  # 40 + this one * 20 = 60
    assert progress.over_budget(60.0) is False
    progress.state.update(done=0, total_cost_usd=0.0)
    assert progress.over_budget(1.0) is False  # nothing finished: no average, the first runs start


def _kept_records(cfg) -> list[dict]:
    records = []
    for case in "abc":
        for arm in ("old", "new"):
            for n in (1, 2, 3):
                records.append({
                    "run_id": f"{case}-{arm}-{n}", "case": case, "arm": arm, "n": n,
                    "session_id": f"11111111-{case}{n}-{arm}", "cost_usd": 1.25 + n / 100, "model": "claude-opus-5-5",
                    "effort": "high", "text": f"answer {case} {arm} {n}", "writes": [], "void": False,
                    "timed_out": False,
                })
    # void and timed-out records of the same slots must be ignored
    records.append({**records[0], "run_id": "a-old-4", "n": 4, "void": True, "text": "leaked"})
    records.append({**records[1], "run_id": "a-old-5", "n": 5, "timed_out": True, "text": ""})
    return records


def test_finish_builds_three_case_files_and_the_key(cfg, tmp_path):
    out_dir = tmp_path / "out"
    key_path = tmp_path / "key.json"
    problems = finish(cfg, _kept_records(cfg), out_dir, key_path, seed=7)
    assert problems == []
    assert sorted(p.name for p in out_dir.iterdir()) == ["case-a.md", "case-b.md", "case-c.md"]
    key = json.loads(key_path.read_text("utf-8"))
    assert len(key["entries"]) == 18
    assert {e["arm"] for e in key["entries"] if e["case"] == "a"} == {"old", "new"}
    assert "leaked" not in (out_dir / "case-a.md").read_text("utf-8")


def test_finish_names_every_case_and_arm_short_of_three_and_writes_nothing(cfg, tmp_path):
    records = [r for r in _kept_records(cfg) if r["run_id"] not in ("b-new-2", "c-old-1")]
    with pytest.raises(ValueError) as caught:
        finish(cfg, records, tmp_path / "out", tmp_path / "key.json", seed=1)
    assert "case b arm new: 2 kept runs, need 3" in str(caught.value)
    assert "case c arm old: 2 kept runs, need 3" in str(caught.value)
    assert not (tmp_path / "out").exists() and not (tmp_path / "key.json").exists()


def test_issue_counts_reads_the_highest_number_per_repo(tmp_path):
    fake_gh = tmp_path / "fake_gh.py"
    fake_gh.write_text(
        "import json, sys\n"
        "a = sys.argv[1:]\n"
        "assert a[:2] == ['issue', 'list'] and '--state' in a and a[a.index('--state') + 1] == 'all'\n"
        "assert a[a.index('--limit') + 1] == '1' and a[a.index('--json') + 1] == 'number'\n"
        "n = 352 if a[a.index('-R') + 1] == 'o/one' else 77\n"
        "print(json.dumps([{'number': n}]))\n",
        encoding="utf-8",
    )
    counts = issue_counts(["o/one", "o/two"], gh_cmd=[sys.executable, str(fake_gh)])
    assert counts == {"o/one": 352, "o/two": 77}


def test_progress_file_is_valid_json_while_runs_are_live(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "0.5")
    seen = []
    stop = threading.Event()

    def watch():
        path = cfg.runs_dir / "progress.json"
        while not stop.is_set():
            if path.exists():
                try:
                    seen.append(json.loads(path.read_text("utf-8")))
                except json.JSONDecodeError:
                    seen.append(None)
            stop.wait(0.02)

    thread = threading.Thread(target=watch)
    thread.start()
    try:
        run_all(cfg, cases=("a",), arms=("old", "new"), per_arm=1, concurrency=2)
    finally:
        stop.set()
        thread.join(timeout=30)
    assert seen and None not in seen
    assert any(p["running"] for p in seen)
